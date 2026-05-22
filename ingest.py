#!/usr/bin/env python3
"""
DPL Project — Sleeper EPL Fantasy ingest

Pulls data from Sleeper's public API (sport code: "clubsoccer:epl") and loads
it into a local SQLite database (dpl.db). Safe to re-run; uses upserts.

Usage:
    python3 ingest.py                # full refresh (current season + all weeks up to current)
    python3 ingest.py --week 30      # only (re)ingest a single week's stats/projections
    python3 ingest.py --skip-weekly  # only refresh league/players/fixtures/rosters

Config at top: SPORT, LEAGUE_ID, SEASON. User-level lookup uses USERNAME.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "dpl.db"
SCHEMA_PATH = HERE / "schema.sql"

GRAPHQL_URL = "https://api.sleeper.app/graphql"

SPORT = "clubsoccer:epl"
USERNAME = "willmoran"
LEAGUE_ID = "1244790289042776064"
SEASON = "2025"          # EPL 2025/26 season
SEASON_TYPE = "regular"

API_BASE = "https://api.sleeper.app"
# Note: /schedule is at root, not under /v1
SCHED_BASE = "https://api.sleeper.app"

HTTP_TIMEOUT = 30
RETRIES = 3


# --------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------

def fetch_graphql(query: str):
    """POST a GraphQL query; returns the data dict or None on error."""
    body = json.dumps({"query": query}).encode("utf-8")
    last_err = None
    for attempt in range(RETRIES):
        try:
            req = Request(
                GRAPHQL_URL,
                data=body,
                headers={"User-Agent": "dpl-ingest/1.0", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as r:
                raw = r.read()
                if not raw:
                    return None
                result = json.loads(raw.decode("utf-8"))
                if "errors" in result:
                    # Treat unauthorized as None (no data)
                    codes = [e.get("code") for e in result["errors"]]
                    if "unauthorized" in codes:
                        return None
                    raise RuntimeError(f"GraphQL errors: {result['errors']}")
                return result.get("data")
        except HTTPError as e:
            last_err = e
        except (URLError, TimeoutError) as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to POST {GRAPHQL_URL}: {last_err}")


def fetch_json(url: str):
    last_err = None
    for attempt in range(RETRIES):
        try:
            req = Request(url, headers={"User-Agent": "dpl-ingest/1.0"})
            with urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as r:
                raw = r.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except HTTPError as e:
            if e.code == 404:
                return None
            last_err = e
        except (URLError, TimeoutError) as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def jdump(v):
    if v is None:
        return None
    return json.dumps(v, separators=(",", ":"), default=str)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------
# DB
# --------------------------------------------------------------------

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_schema(conn: sqlite3.Connection):
    sql = SCHEMA_PATH.read_text()
    conn.executescript(sql)
    conn.commit()


# --------------------------------------------------------------------
# Ingest: state
# --------------------------------------------------------------------

def ingest_state(conn: sqlite3.Connection) -> dict:
    state = fetch_json(f"{API_BASE}/v1/state/{SPORT}")
    if not state:
        raise RuntimeError("state endpoint returned nothing")
    conn.execute(
        """
        INSERT INTO sport_state (sport, week, leg, season, season_type, league_season,
                                 league_create_season, season_start_date, display_week,
                                 season_has_scores, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sport) DO UPDATE SET
            week=excluded.week, leg=excluded.leg, season=excluded.season,
            season_type=excluded.season_type, league_season=excluded.league_season,
            league_create_season=excluded.league_create_season,
            season_start_date=excluded.season_start_date,
            display_week=excluded.display_week,
            season_has_scores=excluded.season_has_scores,
            fetched_at=excluded.fetched_at
        """,
        (
            SPORT,
            state.get("week"),
            state.get("leg"),
            state.get("season"),
            state.get("season_type"),
            state.get("league_season"),
            state.get("league_create_season"),
            state.get("season_start_date"),
            state.get("display_week"),
            1 if state.get("season_has_scores") else 0,
            now_iso(),
        ),
    )
    conn.commit()
    print(f"  state: week={state.get('week')} season={state.get('season')} "
          f"season_type={state.get('season_type')}")
    return state


# --------------------------------------------------------------------
# Ingest: league + users + rosters
# --------------------------------------------------------------------

def ingest_league(conn: sqlite3.Connection):
    lg = fetch_json(f"{API_BASE}/v1/league/{LEAGUE_ID}")
    if not lg:
        raise RuntimeError(f"league {LEAGUE_ID} not found")
    conn.execute(
        """
        INSERT OR REPLACE INTO league (
            league_id, name, sport, season, season_type, status, num_teams,
            draft_id, previous_league_id, avatar, roster_positions,
            scoring_settings, settings, metadata, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            lg["league_id"], lg.get("name"), lg.get("sport"), lg.get("season"),
            lg.get("season_type"), lg.get("status"),
            (lg.get("settings") or {}).get("num_teams"),
            lg.get("draft_id"), lg.get("previous_league_id"), lg.get("avatar"),
            jdump(lg.get("roster_positions")),
            jdump(lg.get("scoring_settings")),
            jdump(lg.get("settings")),
            jdump(lg.get("metadata")),
            now_iso(),
        ),
    )
    print(f"  league: {lg.get('name')} — {lg.get('sport')} {lg.get('season')}")

    users = fetch_json(f"{API_BASE}/v1/league/{LEAGUE_ID}/users") or []
    for u in users:
        md = u.get("metadata") or {}
        conn.execute(
            """
            INSERT OR REPLACE INTO league_users
            (league_id, user_id, display_name, team_name, avatar, is_owner, metadata)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                LEAGUE_ID, u["user_id"], u.get("display_name"),
                md.get("team_name"), u.get("avatar"),
                1 if u.get("is_owner") else 0, jdump(md),
            ),
        )
    print(f"  league_users: {len(users)}")

    rosters = fetch_json(f"{API_BASE}/v1/league/{LEAGUE_ID}/rosters") or []
    for r in rosters:
        s = r.get("settings") or {}
        conn.execute(
            """
            INSERT OR REPLACE INTO rosters (
                league_id, roster_id, owner_id, co_owners, players, starters,
                reserve, taxi, keepers, wins, losses, ties, fpts, fpts_decimal,
                fpts_against, fpts_against_decimal, waiver_budget_used,
                waiver_position, total_moves, settings, metadata, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                r["league_id"], r["roster_id"], r.get("owner_id"),
                jdump(r.get("co_owners")),
                jdump(r.get("players")), jdump(r.get("starters")),
                jdump(r.get("reserve")), jdump(r.get("taxi")),
                jdump(r.get("keepers")),
                s.get("wins"), s.get("losses"), s.get("ties"),
                s.get("fpts"), s.get("fpts_decimal"),
                s.get("fpts_against"), s.get("fpts_against_decimal"),
                s.get("waiver_budget_used"), s.get("waiver_position"),
                s.get("total_moves"),
                jdump(s), jdump(r.get("metadata")), now_iso(),
            ),
        )
    print(f"  rosters: {len(rosters)}")
    conn.commit()


# --------------------------------------------------------------------
# Ingest: fixtures
# --------------------------------------------------------------------

def ingest_fixtures(conn: sqlite3.Connection):
    sched = fetch_json(f"{SCHED_BASE}/schedule/{SPORT}/{SEASON_TYPE}/{SEASON}") or []
    team_map = {}  # abbr -> (team_id, name)
    for f in sched:
        home = f.get("home") or {}
        away = f.get("away") or {}
        for side in (home, away):
            if side.get("abbr") and side.get("team"):
                team_map[side["abbr"]] = (side["team"], side.get("name"))
        conn.execute(
            """
            INSERT OR REPLACE INTO fixtures (
                game_id, week, date, status, home_team_id, home_abbr, home_name,
                away_team_id, away_abbr, away_name, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f.get("game_id"), f.get("week"), f.get("date"), f.get("status"),
                home.get("team"), home.get("abbr"), home.get("name"),
                away.get("team"), away.get("abbr"), away.get("name"),
                now_iso(),
            ),
        )
    for abbr, (tid, tname) in team_map.items():
        conn.execute(
            "INSERT OR REPLACE INTO teams (team_id, abbr, name) VALUES (?,?,?)",
            (tid, abbr, tname),
        )
    conn.commit()
    print(f"  fixtures: {len(sched)}  teams: {len(team_map)}")


# --------------------------------------------------------------------
# Ingest: players
# --------------------------------------------------------------------

def ingest_players(conn: sqlite3.Connection):
    players = fetch_json(f"{API_BASE}/v1/players/{SPORT}") or {}

    # Team lookup by abbr (set up by fixtures ingest)
    team_by_abbr = dict(conn.execute("SELECT abbr, team_id FROM teams").fetchall())

    for pid, p in players.items():
        md = p.get("metadata") or {}
        full = md.get("full_name") or p.get("full_name")
        fpos = p.get("fantasy_positions") or []
        conn.execute(
            """
            INSERT OR REPLACE INTO players (
                player_id, full_name, first_name, last_name, search_full_name,
                team_abbr, team_id, position_primary, fantasy_positions,
                status, active, number, height, birth_date, birth_city, birth_country,
                rookie_year, hashtag, channel_id, search_rank, depth_chart_order,
                depth_chart_position, injury_status, injury_body_part,
                injury_start_date, injury_notes, news_updated, rotowire_id,
                rotoworld_id, yahoo_id, espn_id, sportradar_id, stats_id,
                oddsjam_id, swish_id, metadata, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pid, full, p.get("first_name"), p.get("last_name"),
                p.get("search_full_name"),
                p.get("team_abbr"), team_by_abbr.get(p.get("team_abbr") or ""),
                fpos[0] if fpos else None, jdump(fpos),
                p.get("status"),
                1 if p.get("active") else 0,
                _int(p.get("number")), p.get("height"),
                p.get("birth_date"), p.get("birth_city"), p.get("birth_country"),
                md.get("rookie_year"),
                p.get("hashtag"), md.get("channel_id"),
                _int(p.get("search_rank")), _int(p.get("depth_chart_order")),
                p.get("depth_chart_position"),
                p.get("injury_status"), p.get("injury_body_part"),
                p.get("injury_start_date"), p.get("injury_notes"),
                _int(p.get("news_updated")),
                _int(p.get("rotowire_id")), _int(p.get("rotoworld_id")),
                _int(p.get("yahoo_id")), _int(p.get("espn_id")),
                p.get("sportradar_id"), _int(p.get("stats_id")),
                p.get("oddsjam_id"), p.get("swish_id"),
                jdump(md), now_iso(),
            ),
        )
    conn.commit()
    print(f"  players: {len(players)}")


def _int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------
# Ingest: per-week stats + projections
# --------------------------------------------------------------------

def ingest_weekly(conn: sqlite3.Connection, week: int, *, kind: str):
    """kind is 'stats' or 'projections'."""
    assert kind in ("stats", "projections")
    table = "player_stats" if kind == "stats" else "player_projections"
    url = f"{API_BASE}/v1/{kind}/{SPORT}/{SEASON_TYPE}/{SEASON}/{week}"
    data = fetch_json(url)
    if not data:
        print(f"    week {week:>2} {kind:11s}: (empty)")
        return 0
    # Wipe this week slice first so stat keys that disappeared get cleaned up
    conn.execute(
        f"DELETE FROM {table} WHERE season = ? AND season_type = ? AND week = ?",
        (SEASON, SEASON_TYPE, week),
    )
    rows = 0
    for pid, stats in data.items():
        if not isinstance(stats, dict):
            continue
        for k, v in stats.items():
            if not isinstance(v, (int, float)):
                continue
            conn.execute(
                f"INSERT OR REPLACE INTO {table} "
                f"(player_id, season, season_type, week, stat_key, stat_value) "
                f"VALUES (?,?,?,?,?,?)",
                (pid, SEASON, SEASON_TYPE, week, k, float(v)),
            )
            rows += 1
    conn.commit()
    print(f"    week {week:>2} {kind:11s}: {rows:>6} rows across {len(data)} entities")
    return rows


# --------------------------------------------------------------------
# Ingest: transactions (per leg)
# --------------------------------------------------------------------

def ingest_transactions(conn: sqlite3.Connection, up_to_leg: int):
    total = 0
    for leg in range(1, up_to_leg + 1):
        txns = fetch_json(f"{API_BASE}/v1/league/{LEAGUE_ID}/transactions/{leg}") or []
        for t in txns:
            conn.execute(
                """
                INSERT OR REPLACE INTO transactions (
                    transaction_id, league_id, type, status, leg, created,
                    status_updated, creator, roster_ids, consenter_ids,
                    adds, drops, draft_picks, waiver_budget, settings, metadata
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    t.get("transaction_id"), LEAGUE_ID, t.get("type"),
                    t.get("status"), t.get("leg"), t.get("created"),
                    t.get("status_updated"), t.get("creator"),
                    jdump(t.get("roster_ids")), jdump(t.get("consenter_ids")),
                    jdump(t.get("adds")), jdump(t.get("drops")),
                    jdump(t.get("draft_picks")), jdump(t.get("waiver_budget")),
                    jdump(t.get("settings")), jdump(t.get("metadata")),
                ),
            )
            total += 1
        conn.commit()
    print(f"  transactions: {total} across legs 1..{up_to_leg}")


# --------------------------------------------------------------------
# Ingest: H2H matchups (via GraphQL matchup_legs_raw — no auth needed)
# --------------------------------------------------------------------

def ingest_matchups(conn: sqlite3.Connection, week: int):
    query = (
        f'{{ matchup_legs_raw(round: {week}, league_id: "{LEAGUE_ID}") '
        f'{{ roster_id matchup_id points custom_points starters }} }}'
    )
    data = fetch_graphql(query)
    if not data or not data.get("matchup_legs_raw"):
        print(f"    week {week:>2} matchups: (empty)")
        return 0
    legs = data["matchup_legs_raw"]
    # Delete this week's slice first for idempotency
    conn.execute(
        "DELETE FROM matchup_legs WHERE league_id=? AND season=? AND week=?",
        (LEAGUE_ID, SEASON, week),
    )
    adjusted = 0
    for leg in legs:
        if leg.get("custom_points") is not None:
            adjusted += 1
        conn.execute(
            """
            INSERT OR REPLACE INTO matchup_legs
            (league_id, season, week, roster_id, matchup_id, points, custom_points, starters, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                LEAGUE_ID, SEASON, week,
                leg["roster_id"], leg["matchup_id"],
                leg.get("points"), leg.get("custom_points"),
                jdump(leg.get("starters")),
                now_iso(),
            ),
        )
    conn.commit()
    if adjusted:
        print(f"    week {week:>2} matchups: {len(legs)} legs ({adjusted} custom-adjusted)")
        return len(legs)
    print(f"    week {week:>2} matchups: {len(legs)} legs (matchup_ids: "
          f"{sorted(set(l['matchup_id'] for l in legs))})")
    return len(legs)


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, help="only (re)ingest one week of stats/proj")
    ap.add_argument("--skip-weekly", action="store_true",
                    help="skip per-week stats/projections and transactions")
    ap.add_argument("--max-week", type=int,
                    help="override max week for weekly loop (default: current week)")
    args = ap.parse_args()

    print(f"DB:    {DB_PATH}")
    print(f"Sport: {SPORT}  Season: {SEASON}  League: {LEAGUE_ID}")

    conn = open_db()
    init_schema(conn)

    run = conn.execute(
        "INSERT INTO ingest_runs (started_at, league_id, season) VALUES (?, ?, ?)",
        (now_iso(), LEAGUE_ID, SEASON),
    )
    run_id = run.lastrowid
    conn.commit()

    notes = []

    try:
        print("[1/6] state")
        state = ingest_state(conn)
        current_week = state.get("week") or 1

        print("[2/6] league, users, rosters")
        ingest_league(conn)

        print("[3/6] fixtures + teams")
        ingest_fixtures(conn)

        print("[4/6] players")
        ingest_players(conn)

        if args.skip_weekly:
            print("[5/7] skipped weekly stats/projections")
            print("[6/7] skipped transactions")
            print("[7/7] skipped H2H matchups")
        else:
            max_week = args.max_week or current_week
            weeks = [args.week] if args.week else list(range(1, max_week + 1))
            print(f"[5/7] weekly stats + projections (weeks {weeks[0]}..{weeks[-1]})")
            for w in weeks:
                ingest_weekly(conn, w, kind="stats")
                ingest_weekly(conn, w, kind="projections")

            print(f"[6/7] transactions (legs 1..{max_week})")
            ingest_transactions(conn, max_week)

            print(f"[7/7] H2H matchups (weeks 1..{max_week})")
            for w in weeks:
                ingest_matchups(conn, w)

        notes.append("ok")
    except Exception as e:
        notes.append(f"error: {e!r}")
        raise
    finally:
        conn.execute(
            "UPDATE ingest_runs SET finished_at = ?, notes = ? WHERE run_id = ?",
            (now_iso(), "; ".join(notes), run_id),
        )
        conn.commit()
        conn.close()

    print("done.")


if __name__ == "__main__":
    main()
