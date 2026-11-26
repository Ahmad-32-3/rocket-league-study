import json
import os
import time
from collections import Counter, deque
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

BASE = "https://ballchasing.com/api"
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
CACHE.mkdir(exist_ok=True)
MATCHES_CSV = ROOT / "matches.csv"
PLAYERS_CSV = ROOT / "players.csv"
PROGRESS = ROOT / "progress.json"
USAGE = ROOT / ".cache" / "api_usage.json"

TIERS = {
    "diamond": {
        "min-rank": "diamond-1",
        "max-rank": "diamond-3",
        "allowed": {"diamond-1", "diamond-2", "diamond-3"},
    },
    "champion": {
        "min-rank": "champion-1",
        "max-rank": "champion-3",
        "allowed": {"champion-1", "champion-2", "champion-3"},
    },
    "grand_champion": {
        "min-rank": "grand-champion",
        "max-rank": "grand-champion",
        "allowed": {"grand-champion"},
    },
}

TARGET = 150
MIN_DURATION = 180
REQUEST_GAP = 1.05
LIST_HOUR_CAP = 450
DETAIL_HOUR_CAP = 900
MAX_PLAYER_APPEARANCES = 8
MAX_PREV_OVERLAP = 3
MATCH_COLUMNS = [
    "match_id",
    "match_date",
    "duration",
    "rank",
    "min_rank",
    "max_rank",
    "replay_url",
    "winner_speed",
    "loser_speed",
    "winner_behind_ball_pct",
    "loser_behind_ball_pct",
    "uncertain",
]
PLAYER_COLUMNS = [
    "match_id",
    "rank",
    "team",
    "won",
    "player_id",
    "avg_speed",
    "percent_behind_ball",
]

stats = {
    "list_requests": 0,
    "detail_requests": 0,
    "cache_hits": 0,
    "skipped_diversity": 0,
    "stopped_for_limits": False,
}
list_times = deque()
detail_times = deque()


def load_env():
    load_dotenv(ROOT / ".env")
    key = os.getenv("BALLCHASING_API_KEY")
    if not key:
        raise SystemExit("BALLCHASING_API_KEY missing from .env")
    return key


def load_usage():
    if USAGE.exists():
        data = json.loads(USAGE.read_text(encoding="utf-8"))
        now = time.time()
        for t in data.get("list", []):
            if now - t < 3600:
                list_times.append(t)
        for t in data.get("detail", []):
            if now - t < 3600:
                detail_times.append(t)


def save_usage():
    now = time.time()
    while list_times and now - list_times[0] >= 3600:
        list_times.popleft()
    while detail_times and now - detail_times[0] >= 3600:
        detail_times.popleft()
    USAGE.write_text(
        json.dumps({"list": list(list_times), "detail": list(detail_times)}),
        encoding="utf-8",
    )


def prune_usage():
    now = time.time()
    while list_times and now - list_times[0] >= 3600:
        list_times.popleft()
    while detail_times and now - detail_times[0] >= 3600:
        detail_times.popleft()


def under_hourly_budget(kind):
    prune_usage()
    if kind == "list":
        return len(list_times) < LIST_HOUR_CAP
    return len(detail_times) < DETAIL_HOUR_CAP


def record_usage(kind):
    now = time.time()
    if kind == "list":
        list_times.append(now)
        stats["list_requests"] += 1
    else:
        detail_times.append(now)
        stats["detail_requests"] += 1
    save_usage()


def empty_progress():
    return {
        "matches": [],
        "players": [],
        "seen_ids": [],
        "limitations": [],
        "schema_version": 2,
    }


def load_progress():
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    if MATCHES_CSV.exists() and PLAYERS_CSV.exists():
        mdf = pd.read_csv(MATCHES_CSV)
        pdf = pd.read_csv(PLAYERS_CSV)
        return {
            "matches": mdf.to_dict(orient="records"),
            "players": pdf.to_dict(orient="records"),
            "seen_ids": mdf["match_id"].astype(str).tolist(),
            "limitations": [],
            "schema_version": 1,
        }
    return empty_progress()


def save_progress(prog):
    PROGRESS.write_text(json.dumps(prog, indent=2), encoding="utf-8")
    write_csvs(prog)


def write_csvs(prog):
    mdf = pd.DataFrame(prog["matches"], columns=MATCH_COLUMNS)
    pdf = pd.DataFrame(prog["players"], columns=PLAYER_COLUMNS)
    mdf.to_csv(MATCHES_CSV, index=False)
    pdf.to_csv(PLAYERS_CSV, index=False)


def api_get(session, url, params=None):
    while True:
        t0 = time.monotonic()
        r = session.get(url, params=params, timeout=60)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", "2"))
            print(f"429 Retry-After={wait}")
            time.sleep(max(wait, 1.0))
            continue
        r.raise_for_status()
        elapsed = time.monotonic() - t0
        remaining = REQUEST_GAP - elapsed
        if remaining > 0:
            time.sleep(remaining)
        return r


def fetch_list(session, tier_cfg, after=None):
    if not under_hourly_budget("list"):
        stats["stopped_for_limits"] = True
        return None
    params = {
        "playlist": "ranked-standard",
        "min-rank": tier_cfg["min-rank"],
        "max-rank": tier_cfg["max-rank"],
        "count": 50,
        "sort-by": "replay-date",
        "sort-dir": "desc",
    }
    url = f"{BASE}/replays"
    if after:
        url = after
        params = None
    r = api_get(session, url, params=params)
    record_usage("list")
    return r.json()


def fetch_detail(session, replay_id):
    cache_path = CACHE / f"{replay_id}.json"
    if cache_path.exists():
        stats["cache_hits"] += 1
        return json.loads(cache_path.read_text(encoding="utf-8"))
    if not under_hourly_budget("detail"):
        stats["stopped_for_limits"] = True
        return None
    r = api_get(session, f"{BASE}/replays/{replay_id}")
    record_usage("detail")
    data = r.json()
    cache_path.write_text(json.dumps(data), encoding="utf-8")
    return data


def player_id_str(player):
    pid = player.get("id") or {}
    platform = pid.get("platform", "unknown")
    ident = pid.get("id", player.get("name", "unknown"))
    return f"{platform}:{ident}"


def extract_player_stats(player):
    stats_obj = player.get("stats") or {}
    movement = stats_obj.get("movement") or {}
    positioning = stats_obj.get("positioning") or {}
    avg_speed = movement.get("avg_speed")
    behind = positioning.get("percent_behind_ball")
    if avg_speed is None or behind is None:
        return None
    return avg_speed, behind


def rank_id(obj):
    if not obj:
        return None
    if isinstance(obj, dict):
        return obj.get("id")
    return None


def rank_label(obj):
    if not obj:
        return ""
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("id") or ""
    return str(obj)


def match_in_tier(detail, allowed):
    mn = rank_id(detail.get("min_rank"))
    mx = rank_id(detail.get("max_rank"))
    if not mn or not mx:
        return False
    return mn in allowed and mx in allowed


def is_complete_match(detail):
    if detail.get("status") != "ok":
        return False
    if detail.get("playlist_id") != "ranked-standard":
        return False
    if detail.get("team_size") != 3:
        return False
    duration = detail.get("duration") or 0
    if duration < MIN_DURATION:
        return False
    blue = detail.get("blue") or {}
    orange = detail.get("orange") or {}
    bp = blue.get("players") or []
    op = orange.get("players") or []
    if len(bp) != 3 or len(op) != 3:
        return False
    for p in bp + op:
        start = p.get("start_time", 0) or 0
        end = p.get("end_time", 0) or 0
        if start > 5:
            return False
        if end < duration * 0.85:
            return False
        if extract_player_stats(p) is None:
            return False
    b_goals = ((blue.get("stats") or {}).get("core") or {}).get("goals")
    o_goals = ((orange.get("stats") or {}).get("core") or {}).get("goals")
    if b_goals is None or o_goals is None:
        return False
    if b_goals == o_goals:
        return False
    return True


def compute_uncertain(detail, allowed):
    players = (detail.get("blue", {}).get("players") or []) + (
        detail.get("orange", {}).get("players") or []
    )
    ranks = [rank_id(p.get("rank")) for p in players]
    if any(r is None for r in ranks):
        return True
    if any(r not in allowed for r in ranks):
        return True
    return False


def build_rows(detail, tier_name, allowed):
    blue = detail["blue"]
    orange = detail["orange"]
    b_goals = blue["stats"]["core"]["goals"]
    o_goals = orange["stats"]["core"]["goals"]
    blue_won = b_goals > o_goals
    winner_side = "blue" if blue_won else "orange"
    loser_side = "orange" if blue_won else "blue"
    teams = {"blue": blue, "orange": orange}
    uncertain = compute_uncertain(detail, allowed)

    player_rows = []
    team_avgs = {}
    player_ids = []
    for side in ("blue", "orange"):
        speeds = []
        behinds = []
        won = side == winner_side
        for p in teams[side]["players"]:
            avg_speed, behind = extract_player_stats(p)
            pid = player_id_str(p)
            player_ids.append(pid)
            speeds.append(avg_speed)
            behinds.append(behind)
            player_rows.append(
                {
                    "match_id": detail["id"],
                    "rank": tier_name,
                    "team": side,
                    "won": won,
                    "player_id": pid,
                    "avg_speed": avg_speed,
                    "percent_behind_ball": behind,
                }
            )
        team_avgs[side] = {
            "speed": sum(speeds) / 3.0,
            "behind": sum(behinds) / 3.0,
        }

    match_row = {
        "match_id": detail["id"],
        "match_date": detail.get("date"),
        "duration": detail.get("duration"),
        "rank": tier_name,
        "min_rank": rank_label(detail.get("min_rank")),
        "max_rank": rank_label(detail.get("max_rank")),
        "replay_url": f"https://ballchasing.com/replay/{detail['id']}",
        "winner_speed": team_avgs[winner_side]["speed"],
        "loser_speed": team_avgs[loser_side]["speed"],
        "winner_behind_ball_pct": team_avgs[winner_side]["behind"],
        "loser_behind_ball_pct": team_avgs[loser_side]["behind"],
        "uncertain": uncertain,
    }
    return match_row, player_rows, player_ids


def count_for_tier(prog, tier_name):
    return sum(1 for m in prog["matches"] if m["rank"] == tier_name)


def player_counts(prog):
    return Counter(p["player_id"] for p in prog["players"])


def last_match_players(prog):
    if not prog["matches"]:
        return set()
    last_id = prog["matches"][-1]["match_id"]
    return {p["player_id"] for p in prog["players"] if p["match_id"] == last_id}


def passes_diversity(player_ids, counts, prev_players):
    overlap = sum(1 for pid in player_ids if pid in prev_players)
    if overlap > MAX_PREV_OVERLAP:
        return False
    frequent = sum(1 for pid in player_ids if counts.get(pid, 0) >= MAX_PLAYER_APPEARANCES)
    if frequent >= 3:
        return False
    return True


def migrate_progress(session, prog):
    print("Validating/migrating existing matches to expanded schema...")
    old_matches = list(prog.get("matches") or [])
    old_by_id = {str(m["match_id"]): m for m in old_matches}
    accepted_ids = [str(m["match_id"]) for m in old_matches]
    new_matches = []
    new_players = []
    kept = 0
    dropped = 0
    for mid in accepted_ids:
        old = old_by_id[mid]
        tier_name = old["rank"]
        tier_cfg = TIERS[tier_name]
        detail = fetch_detail(session, mid)
        if detail is None:
            stats["stopped_for_limits"] = True
            break
        if not is_complete_match(detail) or not match_in_tier(detail, tier_cfg["allowed"]):
            dropped += 1
            continue
        match_row, player_rows, _ = build_rows(detail, tier_name, tier_cfg["allowed"])
        new_matches.append(match_row)
        new_players.extend(player_rows)
        kept += 1
    prog["matches"] = new_matches
    prog["players"] = new_players
    prog["schema_version"] = 2
    if "limitations" not in prog:
        prog["limitations"] = []
    if dropped:
        msg = f"Dropped {dropped} pilot/prior matches that failed updated validation."
        prog["limitations"].append(msg)
        print(msg)
    print(f"Kept {kept} existing matches after migration.")
    save_progress(prog)
    return prog


def collect_tier(session, prog, tier_name, tier_cfg):
    needed = TARGET - count_for_tier(prog, tier_name)
    if needed <= 0:
        print(f"[{tier_name}] already at {TARGET}")
        return
    seen = set(prog["seen_ids"])
    counts = player_counts(prog)
    prev_players = last_match_players(prog)
    after = None
    pages = 0
    max_pages = 400
    while needed > 0 and pages < max_pages:
        if stats["stopped_for_limits"]:
            break
        data = fetch_list(session, tier_cfg, after=after)
        if data is None:
            break
        pages += 1
        items = data.get("list") or []
        if not items:
            break
        for item in items:
            if stats["stopped_for_limits"]:
                break
            rid = item["id"]
            if rid in seen:
                continue
            seen.add(rid)
            prog["seen_ids"].append(rid)
            detail = fetch_detail(session, rid)
            if detail is None:
                break
            if not is_complete_match(detail):
                if len(prog["seen_ids"]) % 25 == 0:
                    save_progress(prog)
                continue
            if not match_in_tier(detail, tier_cfg["allowed"]):
                continue
            match_row, player_rows, player_ids = build_rows(
                detail, tier_name, tier_cfg["allowed"]
            )
            if not passes_diversity(player_ids, counts, prev_players):
                stats["skipped_diversity"] += 1
                continue
            prog["matches"].append(match_row)
            prog["players"].extend(player_rows)
            for pid in player_ids:
                counts[pid] += 1
            prev_players = set(player_ids)
            needed -= 1
            save_progress(prog)
            print(
                f"[{tier_name}] {count_for_tier(prog, tier_name)}/{TARGET} "
                f"accepted {rid} uncertain={match_row['uncertain']}"
            )
            if needed <= 0:
                break
        after = data.get("next")
        if not after:
            break
    if needed > 0:
        if stats["stopped_for_limits"]:
            msg = (
                f"{tier_name}: stopped early at {TARGET - needed}/{TARGET} to stay "
                f"under hourly API caps (list<{LIST_HOUR_CAP}, detail<{DETAIL_HOUR_CAP}). "
                "Re-run collect.py to resume."
            )
        else:
            msg = (
                f"{tier_name}: only {TARGET - needed}/{TARGET} valid matches found "
                f"with filters min-rank={tier_cfg['min-rank']} "
                f"max-rank={tier_cfg['max-rank']}."
            )
        prog["limitations"].append(msg)
        save_progress(prog)
        print(msg)


def verify(prog):
    matches = prog["matches"]
    players = prog["players"]
    mdf = pd.DataFrame(matches)
    pdf = pd.DataFrame(players)
    print("\n=== VERIFICATION ===")
    if mdf.empty:
        print("No matches collected.")
        return
    per_rank = mdf["rank"].value_counts().to_dict()
    print("Matches per rank:", per_rank)
    print("Unique match IDs:", mdf["match_id"].nunique(), "total rows:", len(mdf))
    print("Duplicate match IDs:", int(mdf["match_id"].duplicated().sum()))
    players_per = pdf.groupby("match_id").size()
    print("Player rows per match min/max:", int(players_per.min()), int(players_per.max()))
    missing_speed = int(pdf["avg_speed"].isna().sum())
    missing_behind = int(pdf["percent_behind_ball"].isna().sum())
    print("Missing avg_speed:", missing_speed, "missing percent_behind_ball:", missing_behind)
    if "uncertain" in mdf.columns:
        print("Uncertain matches:", int(mdf["uncertain"].astype(bool).sum()))

    mismatches = 0
    for mid, group in pdf.groupby("match_id"):
        mrow = mdf[mdf["match_id"] == mid].iloc[0]
        winners = group[group["won"] == True]
        losers = group[group["won"] == False]
        if len(winners) != 3 or len(losers) != 3:
            mismatches += 1
            continue
        ws = winners["avg_speed"].mean()
        ls = losers["avg_speed"].mean()
        wb = winners["percent_behind_ball"].mean()
        lb = losers["percent_behind_ball"].mean()
        if abs(ws - mrow["winner_speed"]) > 1e-9:
            mismatches += 1
        if abs(ls - mrow["loser_speed"]) > 1e-9:
            mismatches += 1
        if abs(wb - mrow["winner_behind_ball_pct"]) > 1e-9:
            mismatches += 1
        if abs(lb - mrow["loser_behind_ball_pct"]) > 1e-9:
            mismatches += 1
    print("Team-average mismatches:", mismatches)

    freq = pdf["player_id"].value_counts()
    print(
        "Repeated-player frequency — unique players:",
        int(freq.size),
        "max appearances:",
        int(freq.max()) if len(freq) else 0,
        "players with >1 match:",
        int((freq > 1).sum()),
        "mean appearances:",
        float(freq.mean()) if len(freq) else 0.0,
    )
    prune_usage()
    print(
        "API requests this run — list:",
        stats["list_requests"],
        "detail:",
        stats["detail_requests"],
        "cache hits:",
        stats["cache_hits"],
        "diversity skips:",
        stats["skipped_diversity"],
    )
    print(
        "Hourly usage window — list:",
        len(list_times),
        "/",
        LIST_HOUR_CAP,
        "detail:",
        len(detail_times),
        "/",
        DETAIL_HOUR_CAP,
    )
    print(
        "Limitation: player-level rank is often null; match-level min_rank/max_rank "
        "are used for tier checks. Matches with missing/conflicting player ranks "
        "are kept and flagged uncertain=True."
    )
    print(
        "Limitation: Ballchasing maps GC I–III to rank id grand-champion; "
        "SSL is outside the documented filter choices."
    )
    for lim in prog.get("limitations", []):
        print("Limitation:", lim)


def main():
    key = load_env()
    load_usage()
    session = requests.Session()
    session.headers.update({"Authorization": key})
    print("Pinging API...")
    r = api_get(session, f"{BASE}/")
    r.raise_for_status()
    info = r.json()
    print("Auth OK. Account type:", info.get("type"))
    prog = load_progress()
    needs_migrate = prog.get("schema_version", 1) < 2 or (
        prog["matches"] and "duration" not in prog["matches"][0]
    )
    if needs_migrate and prog["matches"]:
        prog = migrate_progress(session, prog)
    elif prog["matches"] or prog["players"]:
        write_csvs(prog)
    for tier_name, tier_cfg in TIERS.items():
        if stats["stopped_for_limits"]:
            print("Stopping remaining tiers due to hourly API caps. Re-run to resume.")
            break
        print(f"\nCollecting {tier_name}...")
        collect_tier(session, prog, tier_name, tier_cfg)
    write_csvs(prog)
    verify(prog)
    print("\nWrote", MATCHES_CSV.name, "and", PLAYERS_CSV.name)
    if stats["stopped_for_limits"]:
        print("Incomplete due to rate caps — run: python collect.py")


if __name__ == "__main__":
    main()
