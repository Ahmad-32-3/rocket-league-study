import json
import os
import time
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

TARGET = 10
MIN_DURATION = 180
REQUEST_GAP = 1.05

stats = {"list_requests": 0, "detail_requests": 0, "cache_hits": 0}


def load_env():
    load_dotenv(ROOT / ".env")
    key = os.getenv("BALLCHASING_API_KEY")
    if not key:
        raise SystemExit("BALLCHASING_API_KEY missing from .env")
    return key


def load_progress():
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    return {
        "matches": [],
        "players": [],
        "seen_ids": [],
        "limitations": [],
    }


def save_progress(prog):
    PROGRESS.write_text(json.dumps(prog, indent=2), encoding="utf-8")
    write_csvs(prog)


def write_csvs(prog):
    matches = prog["matches"]
    players = prog["players"]
    mdf = pd.DataFrame(
        matches,
        columns=[
            "match_id",
            "match_date",
            "rank",
            "winner_speed",
            "loser_speed",
            "winner_behind_ball_pct",
            "loser_behind_ball_pct",
        ],
    )
    pdf = pd.DataFrame(
        players,
        columns=[
            "match_id",
            "rank",
            "team",
            "won",
            "player_id",
            "avg_speed",
            "percent_behind_ball",
        ],
    )
    mdf.to_csv(MATCHES_CSV, index=False)
    pdf.to_csv(PLAYERS_CSV, index=False)


def api_get(session, url, params=None):
    while True:
        t0 = time.monotonic()
        r = session.get(url, params=params, timeout=60)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", "2"))
            time.sleep(max(wait, 1.0))
            continue
        r.raise_for_status()
        elapsed = time.monotonic() - t0
        remaining = REQUEST_GAP - elapsed
        if remaining > 0:
            time.sleep(remaining)
        return r


def fetch_list(session, tier_cfg, after=None):
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
    stats["list_requests"] += 1
    r = api_get(session, url, params=params)
    return r.json()


def fetch_detail(session, replay_id):
    cache_path = CACHE / f"{replay_id}.json"
    if cache_path.exists():
        stats["cache_hits"] += 1
        return json.loads(cache_path.read_text(encoding="utf-8"))
    stats["detail_requests"] += 1
    r = api_get(session, f"{BASE}/replays/{replay_id}")
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


def build_rows(detail, tier_name):
    blue = detail["blue"]
    orange = detail["orange"]
    b_goals = blue["stats"]["core"]["goals"]
    o_goals = orange["stats"]["core"]["goals"]
    blue_won = b_goals > o_goals
    winner_side = "blue" if blue_won else "orange"
    loser_side = "orange" if blue_won else "blue"
    teams = {"blue": blue, "orange": orange}

    player_rows = []
    team_avgs = {}
    for side in ("blue", "orange"):
        speeds = []
        behinds = []
        won = side == winner_side
        for p in teams[side]["players"]:
            avg_speed, behind = extract_player_stats(p)
            speeds.append(avg_speed)
            behinds.append(behind)
            player_rows.append(
                {
                    "match_id": detail["id"],
                    "rank": tier_name,
                    "team": side,
                    "won": won,
                    "player_id": player_id_str(p),
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
        "rank": tier_name,
        "winner_speed": team_avgs[winner_side]["speed"],
        "loser_speed": team_avgs[loser_side]["speed"],
        "winner_behind_ball_pct": team_avgs[winner_side]["behind"],
        "loser_behind_ball_pct": team_avgs[loser_side]["behind"],
        "min_rank_id": rank_id(detail.get("min_rank")),
        "max_rank_id": rank_id(detail.get("max_rank")),
        "blue_goals": b_goals,
        "orange_goals": o_goals,
    }
    return match_row, player_rows


def count_for_tier(prog, tier_name):
    return sum(1 for m in prog["matches"] if m["rank"] == tier_name)


def collect_tier(session, prog, tier_name, tier_cfg):
    needed = TARGET - count_for_tier(prog, tier_name)
    if needed <= 0:
        return
    seen = set(prog["seen_ids"])
    after = None
    pages = 0
    max_pages = 40
    while needed > 0 and pages < max_pages:
        data = fetch_list(session, tier_cfg, after=after)
        pages += 1
        items = data.get("list") or []
        if not items:
            break
        for item in items:
            rid = item["id"]
            if rid in seen:
                continue
            seen.add(rid)
            prog["seen_ids"].append(rid)
            detail = fetch_detail(session, rid)
            if not is_complete_match(detail):
                save_progress(prog)
                continue
            if not match_in_tier(detail, tier_cfg["allowed"]):
                save_progress(prog)
                continue
            match_row, player_rows = build_rows(detail, tier_name)
            out_match = {
                k: match_row[k]
                for k in [
                    "match_id",
                    "match_date",
                    "rank",
                    "winner_speed",
                    "loser_speed",
                    "winner_behind_ball_pct",
                    "loser_behind_ball_pct",
                ]
            }
            prog["matches"].append(out_match)
            prog["players"].extend(player_rows)
            needed -= 1
            save_progress(prog)
            print(
                f"[{tier_name}] {count_for_tier(prog, tier_name)}/{TARGET} "
                f"accepted {rid}"
            )
            if needed <= 0:
                break
        after = data.get("next")
        if not after:
            break
    if needed > 0:
        msg = (
            f"{tier_name}: only {TARGET - needed}/{TARGET} valid matches found "
            f"after scanning available ranked-standard replays with "
            f"min-rank={tier_cfg['min-rank']} max-rank={tier_cfg['max-rank']}."
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
    print(
        "API requests — list:",
        stats["list_requests"],
        "detail:",
        stats["detail_requests"],
        "cache hits:",
        stats["cache_hits"],
    )
    print(
        "Limitation: player-level rank is often missing in Ballchasing responses; "
        "matches were classified using documented match-level min_rank/max_rank "
        "from the replay detail, constrained to the requested tier via search filters."
    )
    for lim in prog.get("limitations", []):
        print("Limitation:", lim)


def main():
    key = load_env()
    session = requests.Session()
    session.headers.update({"Authorization": key})
    print("Pinging API...")
    r = api_get(session, f"{BASE}/")
    r.raise_for_status()
    info = r.json()
    print("Auth OK. Account type:", info.get("type"))
    prog = load_progress()
    if prog["matches"] or prog["players"]:
        write_csvs(prog)
    for tier_name, tier_cfg in TIERS.items():
        print(f"\nCollecting {tier_name}...")
        collect_tier(session, prog, tier_name, tier_cfg)
    write_csvs(prog)
    verify(prog)
    print("\nWrote", MATCHES_CSV.name, "and", PLAYERS_CSV.name)


if __name__ == "__main__":
    main()
