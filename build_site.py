#!/usr/bin/env python3
"""
DPL build_site.py — queries dpl.db, renders Jinja2 templates → /dist

Pages built:
    /index.html                 home
    /table.html                 league table
    /clubs.html                 clubs index
    /clubs/{1..12}.html         per-club pages
    /gameweeks.html             gameweeks index
    /gameweek/{1..N}.html       per-gameweek detail
    /players.html               filterable rostered-player table
    /fixtures.html              upcoming GW pairings + real EPL schedule
    /stats.html                 season stats / awards page
    /draft.html                 pre-season power rankings
    /history.html               2024/25 recap
    /subscribe.html             email subscribe form

Usage:
    python3 build_site.py          # full rebuild
    python3 build_site.py --open   # rebuild + open dist/index.html
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────

HERE              = Path(__file__).resolve().parent
DB_PATH           = HERE / "dpl.db"
TEMPLATES_DIR     = HERE / "templates"
STATIC_DIR        = HERE / "static"
DIST_DIR          = HERE / "dist"
TEAM_MAP_PATH       = HERE / "team_mapping.yml"
HISTORY_2024_PATH   = HERE / "history_2024.json"   # legacy alias
FEATURED_PATH       = HERE / "featured_team.yml"
FEATURED_MATCHES_PATH = HERE / "featured_matches.yml"
DRAFT_PATH          = HERE / "draft_data.yml"

LEAGUE_ID     = "1385458928208343040"   # DPL 2026/27
SEASON        = "2026"
YOU_ROSTER_ID = 1  # willmoran

# Completed seasons, newest first — drives the History page and the Players
# page season selector. Add the current season here once it finishes.
PAST_SEASONS = [
    {"season": "2025", "label": "2025/26", "league_id": "1244790289042776064",
     "file": "history_2025.json"},
    {"season": "2024", "label": "2024/25", "league_id": "1121835436143435776",
     "file": "history_2024.json"},
]

# Subscribe form action — Formspree, Netlify Forms, etc.
# Update this to your actual endpoint when you have one.
SUBSCRIBE_ENDPOINT = "https://formspree.io/f/your-form-id"


# ────────────────────────────────────────────────────────────────────
# DB helpers
# ────────────────────────────────────────────────────────────────────

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON;")
    return conn


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def q1(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


# ────────────────────────────────────────────────────────────────────
# Config loaders
# ────────────────────────────────────────────────────────────────────

def load_team_mapping() -> dict:
    with open(TEAM_MAP_PATH) as f:
        raw = yaml.safe_load(f)
    return {int(k): v for k, v in raw.items()}


def load_featured() -> dict:
    if FEATURED_PATH.exists():
        with open(FEATURED_PATH) as f:
            return yaml.safe_load(f)
    return {}


def load_draft() -> dict:
    if DRAFT_PATH.exists():
        with open(DRAFT_PATH) as f:
            return yaml.safe_load(f)
    return {}


def load_histories() -> list[dict]:
    """Every completed season in PAST_SEASONS that has a frozen JSON file,
    newest first. Regenerate a season's file with make_history.py."""
    out = []
    for entry in PAST_SEASONS:
        path = HERE / entry["file"]
        if not path.exists():
            continue
        with open(path) as f:
            data = json.load(f)
        data["label"] = entry["label"]
        out.append(data)
    return out


def load_history_2024() -> dict:
    if HISTORY_2024_PATH.exists():
        with open(HISTORY_2024_PATH) as f:
            return json.load(f)
    return {}


def load_featured_matches() -> dict:
    if FEATURED_MATCHES_PATH.exists():
        with open(FEATURED_MATCHES_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


# ────────────────────────────────────────────────────────────────────
# Standings / rosters
# ────────────────────────────────────────────────────────────────────

def get_current_week(conn) -> int:
    """Last completed gameweek. Falls back to the highest week in matchup_legs
    with scored points when the sport state is off-season (week = 0)."""
    row = q1(conn, "SELECT week FROM sport_state WHERE sport = 'clubsoccer:epl'")
    state_week = row["week"] if row else 0
    if state_week and state_week > 0:
        return state_week - 1
    # Off-season fallback: highest week that has any scored matchup
    last = q1(conn, """
        SELECT MAX(week) AS w FROM matchup_legs
        WHERE league_id = ? AND season = ? AND points IS NOT NULL
    """, (LEAGUE_ID, SEASON))
    return (last["w"] if last and last["w"] else 1)


def get_standings(conn, team_map: dict) -> list[dict]:
    """Standings come from rosters.fpts (Sleeper's official totals, with any
    manual commissioner adjustments). If matchup_legs has more recently scored
    weeks that aren't yet reflected in rosters (e.g. Sleeper hasn't propagated
    the final week into the roster table), we add those weeks on top — wins,
    losses, PF, PA, and the form-guide record string — so the standings stay
    current without losing manual adjustments."""
    rows = q(conn, """
        SELECT r.roster_id, u.display_name, u.team_name,
               r.wins, r.losses, r.ties,
               ROUND(COALESCE(r.fpts,0) + COALESCE(r.fpts_decimal,0) / 100.0, 2)         AS pts_for,
               ROUND(COALESCE(r.fpts_against,0) + COALESCE(r.fpts_against_decimal,0) / 100.0, 2) AS pts_against,
               r.total_moves, r.waiver_budget_used,
               r.metadata AS metadata_json
        FROM rosters r
        LEFT JOIN league_users u ON u.league_id = r.league_id AND u.user_id = r.owner_id
        WHERE r.league_id = ?
    """, (LEAGUE_ID,))

    standings = []
    for row in rows:
        d = dict(row)
        rid = d["roster_id"]
        tm  = team_map.get(rid, {})
        d["team_name"]   = d["team_name"] or tm.get("team_name", f"Team {rid}")
        d["pl_club"]     = tm.get("pl_club", "")
        d["is_you"]      = rid == YOU_ROSTER_ID

        try:
            meta = json.loads(d.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        rec = meta.get("record") or ""

        # ── Merge any newer scored weeks that rosters hasn't picked up ──
        games_played = (d["wins"] or 0) + (d["losses"] or 0) + (d["ties"] or 0)
        extra = q(conn, """
            SELECT a.week, a.points AS my_pts, b.points AS opp_pts
            FROM matchup_legs a
            JOIN matchup_legs b
              ON b.league_id=a.league_id AND b.season=a.season
             AND b.week=a.week AND b.matchup_id=a.matchup_id AND b.roster_id != a.roster_id
            WHERE a.league_id=? AND a.season=? AND a.roster_id=?
              AND a.week > ?
              AND a.points IS NOT NULL AND b.points IS NOT NULL
            ORDER BY a.week
        """, (LEAGUE_ID, SEASON, rid, games_played))
        for e in extra:
            mp = e["my_pts"] or 0
            op = e["opp_pts"] or 0
            d["pts_for"]     = round((d["pts_for"] or 0) + mp, 2)
            d["pts_against"] = round((d["pts_against"] or 0) + op, 2)
            if mp > op:
                d["wins"]   = (d["wins"]   or 0) + 1
                rec += "W"
            elif mp < op:
                d["losses"] = (d["losses"] or 0) + 1
                rec += "L"
            else:
                d["ties"]   = (d["ties"]   or 0) + 1
                rec += "T"

        d["form"] = list(rec[-5:]) if rec else []
        d["record_str"] = rec
        standings.append(d)

    # Re-sort by wins desc, then PF desc (may have shifted after the merge)
    standings.sort(key=lambda s: (-(s["wins"] or 0), -(s["pts_for"] or 0)))
    for pos, d in enumerate(standings, 1):
        d["position"] = pos
    return standings


# ────────────────────────────────────────────────────────────────────
# Matchups
# ────────────────────────────────────────────────────────────────────

def get_week_matchups(conn, week: int, team_map: dict, standings_map: dict | None = None) -> list[dict]:
    rows = q(conn, """
        SELECT a.roster_id AS roster_a, a.points AS pts_a,
               b.roster_id AS roster_b, b.points AS pts_b,
               a.matchup_id, a.starters AS starters_a, b.starters AS starters_b
        FROM v_matchup_legs a
        JOIN v_matchup_legs b
          ON b.league_id = a.league_id AND b.season = a.season
         AND b.week = a.week AND b.matchup_id = a.matchup_id
         AND b.roster_id > a.roster_id
        WHERE a.league_id = ? AND a.season = ? AND a.week = ?
        ORDER BY a.matchup_id
    """, (LEAGUE_ID, SEASON, week))

    def enrich(roster_id, pts, starters_json):
        tm  = team_map.get(roster_id, {})
        std = (standings_map or {}).get(roster_id, {}) if standings_map else {}
        return {
            "roster_id": roster_id,
            "points": pts,
            "team_name": tm.get("team_name", f"Team {roster_id}"),
            "owner": std.get("display_name", ""),
            "starters": json.loads(starters_json) if starters_json else [],
        }

    matchups = []
    for row in rows:
        ta = enrich(row["roster_a"], row["pts_a"], row["starters_a"])
        tb = enrich(row["roster_b"], row["pts_b"], row["starters_b"])
        if (ta["points"] or 0) > (tb["points"] or 0):
            winner = ta["roster_id"]
        elif (tb["points"] or 0) > (ta["points"] or 0):
            winner = tb["roster_id"]
        else:
            winner = None
        matchups.append({
            "matchup_id": row["matchup_id"],
            "team_a": ta, "team_b": tb,
            "winner_roster_id": winner,
        })
    return matchups


def get_upcoming_matchups(conn, week: int, team_map: dict, standings: list[dict]) -> list[dict]:
    rows = q(conn, """
        SELECT a.roster_id AS roster_a, b.roster_id AS roster_b, a.matchup_id
        FROM v_matchup_legs a
        JOIN v_matchup_legs b
          ON b.league_id = a.league_id AND b.season = a.season
         AND b.week = a.week AND b.matchup_id = a.matchup_id
         AND b.roster_id > a.roster_id
        WHERE a.league_id = ? AND a.season = ? AND a.week = ?
        ORDER BY a.matchup_id
    """, (LEAGUE_ID, SEASON, week))

    std_map = {t["roster_id"]: t for t in standings}

    def enrich(roster_id):
        tm  = team_map.get(roster_id, {})
        std = std_map.get(roster_id, {})
        return {
            "roster_id": roster_id,
            "team_name": tm.get("team_name", f"Team {roster_id}"),
            "owner": std.get("display_name", ""),
            "record": f"{std.get('wins',0)}–{std.get('losses',0)}",
            "form": std.get("form", []),
        }

    return [
        {"matchup_id": r["matchup_id"],
         "team_a": enrich(r["roster_a"]),
         "team_b": enrich(r["roster_b"])}
        for r in rows
    ]


def get_all_weeks_summary(conn) -> list[dict]:
    rows = q(conn, """
        SELECT week,
               AVG(points) AS avg_pts,
               MAX(points) AS high_score,
               MIN(points) AS low_score
        FROM v_matchup_legs
        WHERE league_id = ? AND season = ? AND points IS NOT NULL
        GROUP BY week
        ORDER BY week
    """, (LEAGUE_ID, SEASON))
    return [dict(r) for r in rows]


def get_gw_detail(conn, week: int, team_map: dict, standings_map: dict) -> dict:
    matchups = get_week_matchups(conn, week, team_map, standings_map)

    top_scorer = q1(conn, """
        SELECT ps.player_id, p.full_name, p.team_abbr, p.position_primary,
               SUM(ps.stat_value) AS pts
        FROM player_stats ps
        JOIN players p ON p.player_id = ps.player_id
        WHERE ps.season = ? AND ps.week = ? AND ps.stat_key = 'pts_std'
          AND ps.player_id IN (
              SELECT json_each.value FROM v_matchup_legs ml, json_each(ml.starters)
              WHERE ml.league_id = ? AND ml.season = ? AND ml.week = ?
          )
        GROUP BY ps.player_id
        ORDER BY pts DESC
        LIMIT 1
    """, (SEASON, week, LEAGUE_ID, SEASON, week))

    scores = [p for p in
              [m["team_a"]["points"] for m in matchups] + [m["team_b"]["points"] for m in matchups]
              if p is not None]
    avg = sum(scores) / len(scores) if scores else 0

    return {
        "week": week,
        "matchups": matchups,
        "top_scorer": dict(top_scorer) if top_scorer else None,
        "high_score": max(scores) if scores else 0,
        "low_score":  min(scores) if scores else 0,
        "avg_score":  round(avg, 1),
    }


# ────────────────────────────────────────────────────────────────────
# Players
# ────────────────────────────────────────────────────────────────────

# The Draft Lab and the Players page both re-score historical stats. They use
# the *live* 2026/27 league settings — the 2026 rebalance was adopted on
# Sleeper, so the proposal JSON is now only a fallback for a cold DB.
LAB_SEASON = "2025"   # last season with a full set of stats to re-score


def load_proposed_scoring() -> dict | None:
    """Fallback only: the 2026/27 proposal as authored, if the DB has no league."""
    try:
        with open(HERE / "scoring_proposal_2026.json") as f:
            return json.load(f)["wills_config_aug9"]["full_scoring_settings"]
    except (OSError, KeyError, json.JSONDecodeError):
        return None


def get_season_start(conn) -> str | None:
    """The EPL season opener as 'Fri 21 Aug', or None if we don't have it."""
    row = q1(conn, "SELECT season_start_date FROM sport_state WHERE sport = 'clubsoccer:epl'")
    raw = row["season_start_date"] if row else None
    if not raw:
        return None
    from datetime import date
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        return raw
    return f"{d:%a} {d.day} {d:%b}"


def get_draft_summary(conn) -> dict | None:
    """Pick count and round count for this season's draft, if it has happened."""
    row = q1(conn, """
        SELECT COUNT(*) AS picks, MAX(round) AS rounds
        FROM draft_picks WHERE league_id = ? AND season = ?
    """, (LEAGUE_ID, SEASON))
    if not row or not row["picks"]:
        return None
    return {"picks": row["picks"], "rounds": row["rounds"]}


def season_label(season: str) -> str:
    """"2025" -> "2025/26"."""
    try:
        return f"{season}/{str(int(season) + 1)[2:]}"
    except (TypeError, ValueError):
        return str(season)


def get_stat_seasons(conn) -> list[dict]:
    """Seasons with player stats in the DB, newest first."""
    rows = q(conn, """
        SELECT season, COUNT(*) AS n
        FROM player_stats
        WHERE stat_value IS NOT NULL
        GROUP BY season
        HAVING n > 0
        ORDER BY season DESC
    """)
    return [{"season": r["season"], "label": season_label(r["season"])} for r in rows]


def get_league_scoring(conn, league_id: str = None) -> dict:
    """A league's scoring_settings from the DB ({} if we don't have it)."""
    row = q1(conn, "SELECT scoring_settings FROM league WHERE league_id = ?",
             (league_id or LEAGUE_ID,))
    if not row:
        return {}
    try:
        return json.loads(row["scoring_settings"] or "{}")
    except json.JSONDecodeError:
        return {}


def current_scoring(conn) -> dict:
    """This season's live scoring rules, falling back to the 2026 proposal."""
    return get_league_scoring(conn, LEAGUE_ID) or (load_proposed_scoring() or {})


FANTRAX_ADP_PATH = HERE / "data" / "fantrax_adp_epl_2026-27.csv"


# Letters NFKD can't decompose to ASCII (Ødegaard, Groß, …)
_TRANSLIT = str.maketrans({
    "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
    "æ": "ae", "Æ": "Ae", "ß": "ss", "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D",
})


def _name_tokens(s: str) -> frozenset[str]:
    """Lowercase, accent-strip and tokenize a player name."""
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", s.translate(_TRANSLIT))
    s = s.encode("ascii", "ignore").decode()
    return frozenset(t for t in re.split(r"[^a-z]+", s.lower()) if t)


def attach_fantrax_adp(players: list[dict]) -> None:
    """
    Join Fantrax ADP (2026/27 preseason) onto Sleeper players by name.
    Fantrax uses full legal names ("Santos Carneiro Da Cunha, Matheus"),
    Sleeper uses display names ("Matheus Cunha"), so match on token subset
    with position as tiebreak. Unmatched players get adp=None.
    """
    import csv
    for pl in players:
        pl["adp"] = None
    try:
        with open(FANTRAX_ADP_PATH, newline="") as f:
            entries = [
                {
                    "adp": float(r["adp"]),
                    "pos": {"G": "GK"}.get(r["pos"], r["pos"]),
                    "tokens": _name_tokens(r["name"]),
                    "taken": False,
                }
                for r in csv.DictReader(f)
            ]
    except OSError:
        return

    # Specific (multi-token) names first, then bigger seasons first, so the
    # obvious stars claim their entries before loose single-token names.
    order = sorted(players, key=lambda p: (-len(_name_tokens(p["full_name"] or "")),
                                           -(p["pts"] or 0)))
    for pl in order:
        s = _name_tokens(pl["full_name"] or "")
        if not s:
            continue
        avail = [e for e in entries if not e["taken"]]
        cands = [e for e in avail if s <= e["tokens"]]
        if not cands and len(s) > 1:
            cands = [e for e in avail if e["tokens"] <= s]
        if not cands and len(s) > 1:
            # Last resort: every token pairs up on a shared 3-letter prefix,
            # which bridges nicknames (Danny/Daniel, Oli/Oliver).
            cands = [
                e for e in avail
                if all(any(t[:3] == ft[:3] for ft in e["tokens"]) for t in s)
                and e["pos"] == pl["position_primary"]
            ]
        if len(s) == 1:
            # Mononyms ("Neto", "Igor") are too loose without a position check
            cands = [e for e in cands if e["pos"] == pl["position_primary"]]
        if len(cands) > 1:
            same_pos = [e for e in cands if e["pos"] == pl["position_primary"]]
            cands = same_pos or cands
        if len(cands) > 1:
            cands.sort(key=lambda e: (len(e["tokens"]), e["adp"]))
        if cands:
            cands[0]["taken"] = True
            pl["adp"] = cands[0]["adp"]


def get_rostered_players(conn) -> list[dict]:
    """Legacy shim kept for backward compatibility; now returns all active players."""
    return get_all_active_players(conn)


def get_all_active_players(conn, current_week: int | None = None,
                           season: str = SEASON) -> list[dict]:
    """
    Return every player who has played at least one minute or scored any
    points in `season`, rostered or not. Each row includes rich aggregate
    stats + a `last5` list for the form sparkline. Ownership is always the
    current league's rosters, whichever season's stats are requested.
    """
    # Pull per-player season aggregates
    rows = q(conn, """
        WITH agg AS (
            SELECT
                player_id,
                SUM(CASE WHEN stat_key = 'pts_std' THEN stat_value ELSE 0 END) AS pts,
                SUM(CASE WHEN stat_key = 'min'     THEN stat_value ELSE 0 END) AS mins,
                SUM(CASE WHEN stat_key = 'g'       THEN stat_value ELSE 0 END) AS goals,
                SUM(CASE WHEN stat_key = 'at'      THEN stat_value ELSE 0 END) AS assists,
                SUM(CASE WHEN stat_key = 'cs'      THEN stat_value ELSE 0 END) AS clean_sheets,
                SUM(CASE WHEN stat_key = 'yc'      THEN stat_value ELSE 0 END) AS yellow,
                SUM(CASE WHEN stat_key = 'rc'      THEN stat_value ELSE 0 END) AS red,
                COUNT(DISTINCT CASE
                    WHEN stat_key = 'min' AND stat_value > 0 THEN week
                END) AS games
            FROM player_stats
            WHERE season = ? AND stat_value IS NOT NULL
            GROUP BY player_id
        ),
        roster_lookup AS (
            SELECT j.value AS player_id, r.roster_id, u.display_name AS owner
            FROM rosters r, json_each(r.players) j
            LEFT JOIN league_users u ON u.user_id = r.owner_id AND u.league_id = r.league_id
            WHERE r.league_id = ?
        )
        SELECT p.player_id, p.full_name, p.team_abbr, p.position_primary,
               p.injury_status,
               COALESCE(a.pts,         0) AS pts,
               COALESCE(a.mins,        0) AS mins,
               COALESCE(a.goals,       0) AS goals,
               COALESCE(a.assists,     0) AS assists,
               COALESCE(a.clean_sheets,0) AS clean_sheets,
               COALESCE(a.yellow,      0) AS yellow,
               COALESCE(a.red,         0) AS red,
               COALESCE(a.games,       0) AS games,
               rl.owner     AS owner,
               rl.roster_id AS roster_id
        FROM players p
        LEFT JOIN agg           a  ON a.player_id  = p.player_id
        LEFT JOIN roster_lookup rl ON rl.player_id = p.player_id
        WHERE COALESCE(a.mins, 0) > 0 OR COALESCE(a.pts, 0) != 0
        ORDER BY a.pts DESC
    """, (season, LEAGUE_ID))
    players = [dict(r) for r in rows]

    # Re-score this table (and only this table) with the live 2026/27 weights —
    # the same set the Draft Lab defaults to — so past seasons read on this
    # year's terms. Match results, standings and history keep real scores.
    proposed = current_scoring(conn)
    prop_week: dict[str, dict[int, float]] = {}
    if proposed:
        prop_tot: dict[str, float] = {}
        for r in q(conn, """
            SELECT player_id, week, stat_key, stat_value
            FROM player_stats
            WHERE season = ? AND stat_key LIKE 'pos_%' AND stat_value IS NOT NULL
        """, (season,)):
            w = proposed.get(r["stat_key"], 0) or 0
            if w:
                pts = r["stat_value"] * w
                pid = r["player_id"]
                prop_tot[pid] = prop_tot.get(pid, 0.0) + pts
                wkmap = prop_week.setdefault(pid, {})
                wkmap[r["week"]] = wkmap.get(r["week"], 0.0) + pts
        for pl in players:
            pl["pts"] = round(prop_tot.get(pl["player_id"], 0.0), 1)
        players.sort(key=lambda p: -(p["pts"] or 0))

    # Pull last-5-weeks pts_std for each player (for the sparkline)
    # Determine the window of weeks we care about
    last_weeks = q(conn, """
        SELECT DISTINCT week FROM player_stats
        WHERE season=? AND stat_key='pts_std'
        ORDER BY week DESC LIMIT 5
    """, (season,))
    window_weeks = sorted([r["week"] for r in last_weeks])

    # Pull all pts_std rows in the window
    pts_rows = q(conn, f"""
        SELECT player_id, week, stat_value AS pts
        FROM player_stats
        WHERE season = ? AND stat_key = 'pts_std'
          AND week IN ({','.join('?' * len(window_weeks)) or '0'})
    """, (season, *window_weeks)) if window_weeks else []
    recent_map: dict[str, dict[int, float]] = {}
    for r in pts_rows:
        recent_map.setdefault(r["player_id"], {})[r["week"]] = r["pts"] or 0
    if proposed:
        # Same weeks-played structure, proposed-scoring values
        recent_map = {
            pid: {w: round(prop_week.get(pid, {}).get(w, 0.0), 1) for w in wkmap}
            for pid, wkmap in recent_map.items()
        }

    # Enrich each player with last5 array (aligned to window_weeks) + per-game avg + form
    for pl in players:
        pid  = pl["player_id"]
        last5 = [recent_map.get(pid, {}).get(w, None) for w in window_weeks]
        pl["last5"]       = last5   # may contain None for weeks the player didn't play
        pl["last5_sum"]   = round(sum(v for v in last5 if v is not None), 1)
        pl["last5_avg"]   = round(pl["last5_sum"] / max(len([v for v in last5 if v is not None]), 1), 2)
        pl["ppg"]         = round((pl["pts"] or 0) / pl["games"], 2) if pl["games"] else 0
        pl["per_90"]      = round(((pl["pts"] or 0) / (pl["mins"] or 1)) * 90, 2) if pl["mins"] else 0
        pl["goal_contrib"]= (pl["goals"] or 0) + (pl["assists"] or 0)
        pl["owner_label"] = pl["owner"] or "(free agent)"
        pl["is_free"]     = pl["owner"] is None

    attach_fantrax_adp(players)
    return players


# ────────────────────────────────────────────────────────────────────
# Draft Lab (dynamic re-scoring research view)
# ────────────────────────────────────────────────────────────────────

# Every player-level stat Sleeper records, in display order.
# (cnr and poss are team-level entities; g_at is a composite; pts_std is
# Sleeper standard scoring — all excluded.)
DRAFT_LAB_STATS = [
    ("min",   "Minutes",             "MIN"),
    ("gp",    "Games Played",        "GP"),
    ("gs",    "Games Started",       "GS"),
    ("g",     "Goals",               "G"),
    ("at",    "Assists",             "A"),
    ("sot",   "Shots on Target",     "SOT"),
    ("sat",   "Shots (Total)",       "SH"),
    ("kp",    "Key Passes",          "KP"),
    ("acnc",  "Accurate Crosses",    "ACR"),
    ("cos",   "Successful Dribbles", "SD"),
    ("pkd",   "Penalties Drawn",     "PKD"),
    ("pkm",   "Penalties Missed",    "PKM"),
    ("og",    "Own Goals",           "OG"),
    ("tkw",   "Tackles Won",         "TKW"),
    ("tc",    "Tackles (TC)",        "TC"),
    ("aer",   "Aerials Won",         "AER"),
    ("clr",   "Clearances",          "CLR"),
    ("bs",    "Blocked Shots",       "BS"),
    ("int",   "Interceptions",       "INT"),
    ("dis",   "Dispossessed",        "DIS"),
    ("fl",    "Fouls Committed",     "FL"),
    ("p_att", "Passes Attempted",    "PAS"),
    ("cs",    "Clean Sheets (60')",  "CS"),
    ("cs90",  "Clean Sheets (90')",  "CS90"),
    ("hcs",   "High Claims",         "HCS"),
    ("ga",    "Goals Against",       "GA"),
    ("sv",    "Saves",               "SV"),
    ("sm",    "Smothers",            "SM"),
    ("pks",   "Penalty Saves",       "PKS"),
    ("yc",    "Yellow Cards",        "YC"),
    ("yc2",   "Second Yellows",      "2YC"),
    ("rc",    "Red Cards",           "RC"),
]

_POS_CODE = {"GK": "gk", "D": "d", "M": "m", "F": "f"}


def get_draft_lab_data(conn) -> dict:
    """
    Season stat totals for every active player plus the league's
    position-specific scoring weights, packaged for client-side
    what-if re-scoring on the Draft Lab page.
    """
    stat_keys = [s[0] for s in DRAFT_LAB_STATS]

    # The lab re-scores a full season of stats, so it stays on the last
    # completed season (LAB_SEASON) rather than the in-progress one.
    prev_league = next((p["league_id"] for p in PAST_SEASONS
                        if p["season"] == LAB_SEASON), None)
    scoring = get_league_scoring(conn, prev_league)   # rules that season ran under

    # The live 2026/27 settings are the page default; the season's own
    # settings stay available as a one-click preset.
    proposed = current_scoring(conn) or scoring

    def _weights(src: dict) -> dict:
        # weights[stat] = {gk, d, m, f} — 0 where the league didn't score it
        return {
            key: {pc: src.get(f"pos_{pc}_{key}", 0) or 0 for pc in ("gk", "d", "m", "f")}
            for key in stat_keys
        }

    weights = _weights(proposed)
    weights_2526 = _weights(scoring)

    # Each player's *scoring* position = the pos_X_ prefix they accumulated
    # the most minutes under (Sleeper applies weights by this, and it can
    # drift from position_primary for a handful of players).
    pos_min_rows = q(conn, """
        SELECT player_id, stat_key, SUM(stat_value) AS v
        FROM player_stats
        WHERE season = ? AND stat_key IN ('pos_gk_min','pos_d_min','pos_m_min','pos_f_min')
        GROUP BY player_id, stat_key
    """, (LAB_SEASON,))
    scoring_pos: dict[str, tuple[float, str]] = {}
    for r in pos_min_rows:
        pc = r["stat_key"].split("_")[1]
        cur = scoring_pos.get(r["player_id"])
        if cur is None or (r["v"] or 0) > cur[0]:
            scoring_pos[r["player_id"]] = ((r["v"] or 0), pc)

    ph = ",".join("?" * len(stat_keys))
    rows = q(conn, f"""
        WITH roster_lookup AS (
            SELECT j.value AS player_id, u.display_name AS owner
            FROM rosters r, json_each(r.players) j
            LEFT JOIN league_users u ON u.user_id = r.owner_id AND u.league_id = r.league_id
            WHERE r.league_id = ?
        )
        SELECT p.player_id, p.full_name, p.team_abbr, p.position_primary,
               p.injury_status, rl.owner,
               s.stat_key, SUM(s.stat_value) AS v
        FROM players p
        JOIN player_stats s ON s.player_id = p.player_id
             AND s.season = ? AND s.stat_key IN ({ph})
        LEFT JOIN roster_lookup rl ON rl.player_id = p.player_id
        WHERE p.position_primary IN ('GK','D','M','F')
        GROUP BY p.player_id, s.stat_key
    """, (LEAGUE_ID, LAB_SEASON, *stat_keys))

    by_player: dict[str, dict] = {}
    for r in rows:
        pl = by_player.setdefault(r["player_id"], {
            "id":  r["player_id"],
            "n":   r["full_name"] or r["player_id"],
            "p":   r["position_primary"],
            "sp":  scoring_pos.get(r["player_id"], (0, _POS_CODE[r["position_primary"]]))[1],
            "c":   r["team_abbr"] or "",
            "o":   r["owner"],
            "inj": 1 if r["injury_status"] else 0,
            "s":   [0] * len(stat_keys),
        })
        pl["s"][stat_keys.index(r["stat_key"])] = round(r["v"] or 0, 2)

    # Keep only players who actually appeared
    min_idx = stat_keys.index("min")
    players = [pl for pl in by_player.values() if pl["s"][min_idx] > 0]

    # Fantrax ADP join — same matcher as the Players page (minutes stand in
    # for points when ordering who claims ambiguous name matches first).
    shim = [{"full_name": pl["n"], "position_primary": pl["p"], "pts": pl["s"][min_idx]}
            for pl in players]
    attach_fantrax_adp(shim)
    for pl, sh in zip(players, shim):
        pl["adp"] = sh["adp"]

    return {
        "stats":   [{"key": k, "label": l, "abbr": a} for k, l, a in DRAFT_LAB_STATS],
        "weights": weights,
        "weights_2526": weights_2526,
        "players": players,
    }


# ────────────────────────────────────────────────────────────────────
# Per-club detail
# ────────────────────────────────────────────────────────────────────

def get_club_detail(conn, roster_id: int, team_map: dict, standings_map: dict) -> dict:
    tm = team_map.get(roster_id, {})
    owner_row = q1(conn, """
        SELECT u.display_name, u.team_name, r.wins, r.losses,
               ROUND(COALESCE(r.fpts,0) + COALESCE(r.fpts_decimal,0) / 100.0, 2)         AS pts_for,
               ROUND(COALESCE(r.fpts_against,0) + COALESCE(r.fpts_against_decimal,0) / 100.0, 2) AS pts_against,
               r.total_moves, r.metadata AS metadata_json
        FROM rosters r
        LEFT JOIN league_users u ON u.league_id = r.league_id AND u.user_id = r.owner_id
        WHERE r.league_id = ? AND r.roster_id = ?
    """, (LEAGUE_ID, roster_id))

    weekly = q(conn, """
        SELECT week, points,
               (SELECT points FROM v_matchup_legs b
                WHERE b.league_id = ml.league_id AND b.season = ml.season
                  AND b.week = ml.week AND b.matchup_id = ml.matchup_id
                  AND b.roster_id != ml.roster_id) AS opp_points,
               (SELECT roster_id FROM v_matchup_legs b
                WHERE b.league_id = ml.league_id AND b.season = ml.season
                  AND b.week = ml.week AND b.matchup_id = ml.matchup_id
                  AND b.roster_id != ml.roster_id) AS opp_roster_id
        FROM v_matchup_legs ml
        WHERE league_id = ? AND season = ? AND roster_id = ?
        ORDER BY week
    """, (LEAGUE_ID, SEASON, roster_id))

    top_players = q(conn, """
        WITH rp AS (
            SELECT json_each.value AS player_id
            FROM rosters, json_each(players)
            WHERE league_id = ? AND roster_id = ?
        )
        SELECT p.player_id, p.full_name, p.team_abbr, p.position_primary,
               COALESCE(ps.total_pts, 0) AS season_pts
        FROM rp
        JOIN players p ON p.player_id = rp.player_id
        LEFT JOIN (
            SELECT player_id, SUM(stat_value) AS total_pts
            FROM player_stats WHERE season = ? AND stat_key = 'pts_std'
            GROUP BY player_id
        ) ps ON ps.player_id = p.player_id
        ORDER BY season_pts DESC
        LIMIT 10
    """, (LEAGUE_ID, roster_id, SEASON))

    meta = {}
    try:
        meta = json.loads(owner_row.get("metadata_json") or "{}")
    except Exception:
        pass
    rec = meta.get("record") or ""
    form = list(rec[-5:]) if rec else []

    # Position from standings_map
    position = standings_map.get(roster_id, {}).get("position", roster_id)

    return {
        "roster_id":   roster_id,
        "owner":       owner_row,
        "team_map":    tm,
        "weekly":      [dict(r) for r in weekly],
        "top_players": [dict(r) for r in top_players],
        "form":        form,
        "record_str":  rec,
        "position":    position,
    }


# ────────────────────────────────────────────────────────────────────
# Stats / awards
# ────────────────────────────────────────────────────────────────────

def compute_stats(conn, team_map: dict, standings: list[dict]) -> dict:
    std_map = {t["roster_id"]: t for t in standings}

    def team_label(rid):
        """Return (display_name, display_name) — team names no longer used publicly."""
        std = std_map.get(rid, {})
        dn  = std.get("display_name", f"roster {rid}")
        return dn, dn

    # Season high
    high = q1(conn, """
        SELECT roster_id, week, points FROM v_matchup_legs
        WHERE league_id=? AND season=? AND points IS NOT NULL
        ORDER BY points DESC LIMIT 1
    """, (LEAGUE_ID, SEASON))
    # Pre-season / first GW: nothing scored yet, so these stay None and the
    # templates skip the card rather than rendering an empty award.
    if high:
        high = dict(high)
        high["team_name"], high["display_name"] = team_label(high["roster_id"])
    else:
        high = None

    # Season low (nonzero)
    low = q1(conn, """
        SELECT roster_id, week, points FROM v_matchup_legs
        WHERE league_id=? AND season=? AND points IS NOT NULL AND points > 0
        ORDER BY points ASC LIMIT 1
    """, (LEAGUE_ID, SEASON))
    if low:
        low = dict(low)
        low["team_name"], low["display_name"] = team_label(low["roster_id"])
    else:
        low = None

    # Biggest blowout + closest game (with both teams scored)
    margins = q(conn, """
        SELECT a.week, a.matchup_id, a.roster_id AS r_a, a.points AS p_a,
               b.roster_id AS r_b, b.points AS p_b,
               ABS(a.points - b.points) AS diff
        FROM v_matchup_legs a
        JOIN v_matchup_legs b
          ON b.league_id=a.league_id AND b.season=a.season
         AND b.week=a.week AND b.matchup_id=a.matchup_id AND b.roster_id > a.roster_id
        WHERE a.league_id=? AND a.season=?
          AND a.points IS NOT NULL AND b.points IS NOT NULL
          AND a.points > 0 AND b.points > 0
        ORDER BY diff DESC
    """, (LEAGUE_ID, SEASON))
    margin_rows = [dict(m) for m in margins]

    def make_margin(r):
        winner = r["r_a"] if r["p_a"] >= r["p_b"] else r["r_b"]
        loser  = r["r_b"] if winner == r["r_a"] else r["r_a"]
        wn, _  = team_label(winner)
        ln, _  = team_label(loser)
        return {
            "week": r["week"],
            "diff": r["diff"],
            "winner_name": wn,
            "loser_name": ln,
        }

    biggest_blowout = make_margin(margin_rows[0]) if margin_rows else None
    closest_game    = make_margin(margin_rows[-1]) if margin_rows else None

    # ── Funny stats ────────────────────────────────────────────────
    # Pyrrhic Victory — highest-scoring team that STILL lost the week.
    pyr = q1(conn, """
        SELECT a.roster_id, a.week, a.points,
               b.roster_id AS opp_roster, b.points AS opp_points
        FROM v_matchup_legs a
        JOIN v_matchup_legs b
          ON b.league_id=a.league_id AND b.season=a.season
         AND b.week=a.week AND b.matchup_id=a.matchup_id AND b.roster_id != a.roster_id
        WHERE a.league_id=? AND a.season=?
          AND a.points IS NOT NULL AND b.points IS NOT NULL
          AND a.points < b.points         -- they lost
        ORDER BY a.points DESC LIMIT 1
    """, (LEAGUE_ID, SEASON))
    pyrrhic = None
    if pyr:
        loser_name,  _ = team_label(pyr["roster_id"])
        winner_name, _ = team_label(pyr["opp_roster"])
        pyrrhic = {
            "display_name": loser_name,
            "opp_name":     winner_name,
            "week":         pyr["week"],
            "points":       pyr["points"],
            "opp_points":   pyr["opp_points"],
        }

    # The Gift — lowest-scoring team that WON the week.
    gift = q1(conn, """
        SELECT a.roster_id, a.week, a.points,
               b.roster_id AS opp_roster, b.points AS opp_points
        FROM v_matchup_legs a
        JOIN v_matchup_legs b
          ON b.league_id=a.league_id AND b.season=a.season
         AND b.week=a.week AND b.matchup_id=a.matchup_id AND b.roster_id != a.roster_id
        WHERE a.league_id=? AND a.season=?
          AND a.points IS NOT NULL AND b.points IS NOT NULL
          AND a.points > b.points         -- they won
          AND a.points > 0                 -- but scored something
        ORDER BY a.points ASC LIMIT 1
    """, (LEAGUE_ID, SEASON))
    the_gift = None
    if gift:
        winner_name, _ = team_label(gift["roster_id"])
        loser_name,  _ = team_label(gift["opp_roster"])
        the_gift = {
            "display_name": winner_name,
            "opp_name":     loser_name,
            "week":         gift["week"],
            "points":       gift["points"],
            "opp_points":   gift["opp_points"],
        }

    # Streaks from record string
    longest_win_streak  = {"length": 0, "team_name": "", "display_name": ""}
    longest_loss_streak = {"length": 0, "team_name": "", "display_name": ""}
    for t in standings:
        rec = t.get("record_str") or ""
        cur_w = best_w = cur_l = best_l = 0
        for ch in rec:
            if ch == "W":
                cur_w += 1; cur_l = 0
                best_w = max(best_w, cur_w)
            elif ch == "L":
                cur_l += 1; cur_w = 0
                best_l = max(best_l, cur_l)
            else:
                cur_w = cur_l = 0
        if best_w > longest_win_streak["length"]:
            longest_win_streak = {"length": best_w, "team_name": t["team_name"], "display_name": t["display_name"]}
        if best_l > longest_loss_streak["length"]:
            longest_loss_streak = {"length": best_l, "team_name": t["team_name"], "display_name": t["display_name"]}

    # Best position single-week performance (started)
    def best_at_pos(pos):
        row = q1(conn, """
            SELECT ps.player_id, p.full_name, p.team_abbr, p.position_primary,
                   ps.week, SUM(ps.stat_value) AS pts,
                   ml.roster_id
            FROM player_stats ps
            JOIN players p ON p.player_id = ps.player_id
            JOIN v_matchup_legs ml ON ml.league_id=? AND ml.season=ps.season AND ml.week=ps.week
            WHERE ps.season=? AND ps.stat_key='pts_std'
              AND p.position_primary = ?
              AND EXISTS (
                  SELECT 1 FROM json_each(ml.starters)
                  WHERE json_each.value = ps.player_id
              )
            GROUP BY ps.player_id, ps.week, ml.roster_id
            ORDER BY pts DESC LIMIT 1
        """, (LEAGUE_ID, SEASON, pos))
        if row:
            row["display_name"], _ = team_label(row["roster_id"])
        return row

    best_fwd = best_at_pos("F")
    best_mid = best_at_pos("M")
    best_def = best_at_pos("D")
    best_gk  = best_at_pos("GK")

    # Top 10 single-week player performances overall
    top_perfs_rows = q(conn, """
        SELECT ps.player_id, p.full_name, p.team_abbr, p.position_primary,
               ps.week, SUM(ps.stat_value) AS pts, ml.roster_id
        FROM player_stats ps
        JOIN players p ON p.player_id = ps.player_id
        JOIN v_matchup_legs ml ON ml.league_id=? AND ml.season=ps.season AND ml.week=ps.week
        WHERE ps.season=? AND ps.stat_key='pts_std'
          AND EXISTS (
              SELECT 1 FROM json_each(ml.starters)
              WHERE json_each.value = ps.player_id
          )
        GROUP BY ps.player_id, ps.week, ml.roster_id
        ORDER BY pts DESC
        LIMIT 10
    """, (LEAGUE_ID, SEASON))
    top_perfs = []
    for r in top_perfs_rows:
        d = dict(r)
        d["fantasy_team"], _ = team_label(d["roster_id"])
        top_perfs.append(d)

    # PF leaderboard with high/low/per-gw
    pf_rows = q(conn, """
        SELECT roster_id,
               SUM(points)  AS total,
               AVG(points)  AS per_gw,
               MAX(points)  AS high,
               MIN(points)  AS low,
               COUNT(*)     AS weeks
        FROM v_matchup_legs
        WHERE league_id=? AND season=? AND points IS NOT NULL AND points > 0
        GROUP BY roster_id
        ORDER BY total DESC
    """, (LEAGUE_ID, SEASON))
    pf_leaderboard = []
    for r in pf_rows:
        d = dict(r)
        d["team_name"], d["display_name"] = team_label(d["roster_id"])
        pf_leaderboard.append(d)

    return {
        "season_high": high,
        "season_low":  low,
        "biggest_blowout": biggest_blowout,
        "closest_game":    closest_game,
        "pyrrhic_victory": pyrrhic,
        "the_gift":        the_gift,
        "longest_win_streak":  longest_win_streak,
        "longest_loss_streak": longest_loss_streak,
        "best_fwd": best_fwd,
        "best_mid": best_mid,
        "best_def": best_def,
        "best_gk":  best_gk,
        "top_perfs": top_perfs,
        "pf_leaderboard": pf_leaderboard,
    }


# ────────────────────────────────────────────────────────────────────
# Weekly awards — computed for a single GW (for GW detail + email)
# ────────────────────────────────────────────────────────────────────

def compute_weekly_awards(conn, week: int, team_map: dict, standings: list[dict]) -> dict:
    std_map = {t["roster_id"]: t for t in standings}

    def team_label(rid):
        """Return (display_name, display_name) — team names no longer used publicly."""
        std = std_map.get(rid, {})
        dn  = std.get("display_name", f"roster {rid}")
        return dn, dn

    # Weekly team high / low
    rows = q(conn, """
        SELECT roster_id, points FROM v_matchup_legs
        WHERE league_id=? AND season=? AND week=? AND points IS NOT NULL
        ORDER BY points DESC
    """, (LEAGUE_ID, SEASON, week))

    high = low = None
    if rows:
        h = dict(rows[0])
        h["team_name"], h["display_name"] = team_label(h["roster_id"])
        high = h
        l = dict(rows[-1])
        l["team_name"], l["display_name"] = team_label(l["roster_id"])
        low = l

    # Biggest blowout + closest game within THIS week
    margin_rows = q(conn, """
        SELECT a.matchup_id, a.roster_id AS r_a, a.points AS p_a,
               b.roster_id AS r_b, b.points AS p_b,
               ABS(a.points - b.points) AS diff
        FROM v_matchup_legs a
        JOIN v_matchup_legs b
          ON b.league_id=a.league_id AND b.season=a.season
         AND b.week=a.week AND b.matchup_id=a.matchup_id AND b.roster_id > a.roster_id
        WHERE a.league_id=? AND a.season=? AND a.week=?
          AND a.points IS NOT NULL AND b.points IS NOT NULL
        ORDER BY diff DESC
    """, (LEAGUE_ID, SEASON, week))
    margin_rows = [dict(m) for m in margin_rows]

    def make_margin(r):
        winner = r["r_a"] if r["p_a"] >= r["p_b"] else r["r_b"]
        loser  = r["r_b"] if winner == r["r_a"] else r["r_a"]
        wn, _  = team_label(winner)
        ln, _  = team_label(loser)
        return {
            "diff":        r["diff"],
            "winner_name": wn,
            "loser_name":  ln,
            "winner_pts":  max(r["p_a"], r["p_b"]),
            "loser_pts":   min(r["p_a"], r["p_b"]),
        }

    biggest_blowout = make_margin(margin_rows[0])  if margin_rows else None
    closest_game    = make_margin(margin_rows[-1]) if margin_rows else None

    # Best at each position — from starters only
    def best_at(pos):
        row = q1(conn, """
            SELECT ps.player_id, p.full_name, p.team_abbr, p.position_primary,
                   ps.stat_value AS pts, ml.roster_id
            FROM player_stats ps
            JOIN players p ON p.player_id = ps.player_id
            JOIN v_matchup_legs ml ON ml.league_id=? AND ml.season=ps.season AND ml.week=ps.week
            WHERE ps.season=? AND ps.week=? AND ps.stat_key='pts_std'
              AND p.position_primary = ?
              AND EXISTS (
                  SELECT 1 FROM json_each(ml.starters)
                  WHERE json_each.value = ps.player_id
              )
            ORDER BY pts DESC LIMIT 1
        """, (LEAGUE_ID, SEASON, week, pos))
        if row:
            row["display_name"], _ = team_label(row["roster_id"])
        return row

    return {
        "week":            week,
        "high_score":      high,
        "low_score":       low,
        "biggest_blowout": biggest_blowout,
        "closest_game":    closest_game,
        "best_fwd":        best_at("F"),
        "best_mid":        best_at("M"),
        "best_def":        best_at("D"),
        "best_gk":         best_at("GK"),
    }


# ────────────────────────────────────────────────────────────────────
# EPL fixtures
# ────────────────────────────────────────────────────────────────────

def get_epl_fixtures(conn, week: int) -> list[dict]:
    rows = q(conn, """
        SELECT home_name, away_name, date, status
        FROM fixtures WHERE season = ? AND week = ?
        ORDER BY date
    """, (SEASON, week))
    return [dict(r) for r in rows]


# ────────────────────────────────────────────────────────────────────
# Manager stats — careers, and draft alpha
# ────────────────────────────────────────────────────────────────────

# Alpha damping: a single monster pick shouldn't carry a manager's whole
# number, so residuals are clipped at ±ALPHA_CAP standard deviations before
# they're summed. At 1σ the top pick's share of a leading manager's alpha
# drops from ~28% to ~13% without reordering anyone. Reported per gameweek
# (÷38) because raw season totals run to ±700 and mean nothing at a glance.
ALPHA_CAP = 1.0
ALPHA_GWS = 38


def _season_league_points(conn, season: str, league_id: str) -> dict:
    """Every player's season total under that season's own scoring rules."""
    weights = get_league_scoring(conn, league_id)
    pts: dict[str, float] = {}
    for r in q(conn, """
        SELECT player_id, stat_key, SUM(stat_value) AS v
        FROM player_stats
        WHERE season = ? AND stat_key LIKE 'pos_%' AND stat_value IS NOT NULL
        GROUP BY player_id, stat_key
    """, (season,)):
        m = weights.get(r["stat_key"], 0) or 0
        if m:
            pts[r["player_id"]] = pts.get(r["player_id"], 0.0) + r["v"] * m
    return pts


def compute_draft_alpha(conn, season: str, league_id: str) -> list[dict] | None:
    """
    Points above what the draft slot implied. Expected points are fitted
    against log(pick_no) across all 204 picks — the market's own view — and
    each pick's residual is what the manager got over that line.

    This is a RETROSPECTIVE measure. It does not persist year to year
    (2024/25 -> 2025/26 correlation is -0.13, and within a single draft the
    odd/even-round split-half is negative), so treat it as an award, not a
    skill rating.
    """
    import math

    pts = _season_league_points(conn, season, league_id)
    if not pts:
        return None

    picks = []
    for r in q(conn, """
        SELECT d.pick_no, d.player_id, u.display_name, p.full_name,
               p.position_primary, p.team_abbr
        FROM draft_picks d
        JOIN rosters rs      ON rs.league_id = d.league_id AND rs.roster_id = d.roster_id
        JOIN league_users u  ON u.league_id = rs.league_id AND u.user_id = rs.owner_id
        LEFT JOIN players p  ON p.player_id = d.player_id
        WHERE d.league_id = ?
        ORDER BY d.pick_no
    """, (league_id,)):
        picks.append({
            "pick_no": r["pick_no"], "player_id": r["player_id"],
            "manager": r["display_name"],
            "full_name": r["full_name"] or r["player_id"],
            "position": r["position_primary"] or "",
            "club": r["team_abbr"] or "",
            "pts": pts.get(r["player_id"], 0.0),
        })
    if len(picks) < 24:
        return None

    xs = [math.log(p["pick_no"]) for p in picks]
    ys = [p["pts"] for p in picks]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom if denom else 0.0
    intercept = my - slope * mx
    for p in picks:
        p["expected"] = intercept + slope * math.log(p["pick_no"])
        p["resid"] = p["pts"] - p["expected"]

    resids = [p["resid"] for p in picks]
    mean_r = sum(resids) / len(resids)
    sd = math.sqrt(sum((x - mean_r) ** 2 for x in resids) / len(resids)) or 1.0
    cap = ALPHA_CAP * sd
    for p in picks:
        p["adj"] = max(-cap, min(cap, p["resid"]))
    return picks


def get_manager_stats(conn, histories: list[dict], team_map: dict) -> dict:
    """Career records + per-season draft alpha, keyed by Sleeper display name."""
    seasons = []           # newest first: {season, label, league_id}
    for h in histories:
        seasons.append({"season": h["season"], "label": h["label"],
                        "league_id": h["league_id"], "standings": h["standings"]})

    mgrs: dict[str, dict] = {}

    def slot(name):
        return mgrs.setdefault(name, {
            "display_name": name, "seasons": [], "alpha": {},
            "picks": [], "career": {"wins": 0, "losses": 0, "pts_for": 0.0,
                                     "pts_against": 0.0, "titles": 0,
                                     "best_finish": None, "seasons_played": 0},
            "current": None,
        })

    # ── Completed seasons: records and finishing position ──
    for sn in seasons:
        for i, t in enumerate(sn["standings"], start=1):
            m = slot(t["display_name"])
            m["seasons"].append({
                "season": sn["season"], "label": sn["label"], "position": i,
                "wins": t["wins"], "losses": t["losses"],
                "pts_for": t["pts_for"], "pts_against": t["pts_against"],
            })
            cr = m["career"]
            cr["wins"] += t["wins"]; cr["losses"] += t["losses"]
            cr["pts_for"] += t["pts_for"]; cr["pts_against"] += t["pts_against"]
            cr["seasons_played"] += 1
            if i == 1:
                cr["titles"] += 1
            if cr["best_finish"] is None or i < cr["best_finish"]:
                cr["best_finish"] = i

    # ── Draft alpha, per completed season ──
    for sn in seasons:
        picks = compute_draft_alpha(conn, sn["season"], sn["league_id"])
        if not picks:
            continue
        by_mgr: dict[str, list] = {}
        for p in picks:
            by_mgr.setdefault(p["manager"], []).append(p)
        for name, ps in by_mgr.items():
            m = slot(name)
            m["alpha"][sn["season"]] = round(sum(x["adj"] for x in ps) / ALPHA_GWS, 1)
            for x in ps:
                x["season_label"] = sn["label"]
            m["picks"].extend(ps)

    # ── This season: draft slot and opening pick ──
    for r in q(conn, """
        SELECT d.roster_id, u.display_name, MIN(d.pick_no) AS first_pick,
               MIN(d.draft_slot) AS slot
        FROM draft_picks d
        JOIN rosters rs     ON rs.league_id = d.league_id AND rs.roster_id = d.roster_id
        JOIN league_users u ON u.league_id = rs.league_id AND u.user_id = rs.owner_id
        WHERE d.league_id = ? AND d.season = ?
        GROUP BY d.roster_id, u.display_name
    """, (LEAGUE_ID, SEASON)):
        first = q1(conn, """
            SELECT p.full_name FROM draft_picks d
            LEFT JOIN players p ON p.player_id = d.player_id
            WHERE d.league_id = ? AND d.roster_id = ? ORDER BY d.pick_no LIMIT 1
        """, (LEAGUE_ID, r["roster_id"]))
        m = slot(r["display_name"])
        m["current"] = {
            "roster_id": r["roster_id"], "slot": r["slot"],
            "first_pick_no": r["first_pick"],
            "first_pick": first["full_name"] if first else None,
        }

    out = []
    for m in mgrs.values():
        cr = m["career"]
        games = cr["wins"] + cr["losses"]
        cr["win_pct"] = round(cr["wins"] / games, 3) if games else None
        cr["pts_for"] = round(cr["pts_for"], 1)
        cr["pts_against"] = round(cr["pts_against"], 1)
        cr["ppg"] = round(cr["pts_for"] / games, 1) if games else None
        vals = list(m["alpha"].values())
        m["alpha_career"] = round(sum(vals) / len(vals), 1) if vals else None
        # Headline picks use the RAW residual — damping ties several picks at
        # the cap, which would hide the actual story (Igor Thiago, #71).
        if m["picks"]:
            m["best_pick"] = max(m["picks"], key=lambda x: x["resid"])
            m["worst_pick"] = min(m["picks"], key=lambda x: x["resid"])
        else:
            m["best_pick"] = m["worst_pick"] = None
        m["is_active"] = m["current"] is not None
        m["seasons"].sort(key=lambda x: x["season"], reverse=True)
        m.pop("picks", None)
        out.append(m)

    out.sort(key=lambda m: (not m["is_active"], -(m["career"]["win_pct"] or 0)))

    # League-wide leaderboards for the alpha section
    alpha_rows = []
    for sn in seasons:
        picks = compute_draft_alpha(conn, sn["season"], sn["league_id"])
        if not picks:
            continue
        for p in sorted(picks, key=lambda x: -x["resid"])[:5]:
            alpha_rows.append({**p, "season_label": sn["label"]})

    return {
        "managers": out,
        "seasons": [{"season": s["season"], "label": s["label"]} for s in seasons],
        "top_hits": sorted(alpha_rows, key=lambda x: -x["resid"])[:10],
    }


# ────────────────────────────────────────────────────────────────────
# Draft board (real picks, from the Sleeper draft)
# ────────────────────────────────────────────────────────────────────

def get_draft_board(conn) -> dict | None:
    """This season's actual draft: the board grid, each manager's haul, and
    value-vs-ADP. Returns None until the draft has been ingested."""
    picks = q(conn, """
        SELECT d.pick_no, d.round, d.draft_slot, d.roster_id,
               d.player_id, d.metadata,
               p.full_name, p.position_primary, p.team_abbr,
               u.display_name
        FROM draft_picks d
        LEFT JOIN players p       ON p.player_id = d.player_id
        LEFT JOIN rosters r       ON r.league_id = d.league_id AND r.roster_id = d.roster_id
        LEFT JOIN league_users u  ON u.league_id = d.league_id AND u.user_id = r.owner_id
        WHERE d.league_id = ? AND d.season = ?
        ORDER BY d.pick_no
    """, (LEAGUE_ID, SEASON))
    if not picks:
        return None

    rows = []
    for r in picks:
        try:
            meta = json.loads(r["metadata"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        name = r["full_name"] or " ".join(
            x for x in (meta.get("first_name"), meta.get("last_name")) if x
        ) or r["player_id"]
        rows.append({
            "pick_no":   r["pick_no"],
            "round":     r["round"],
            "slot":      r["draft_slot"],
            "roster_id": r["roster_id"],
            "manager":   r["display_name"] or f"Roster {r['roster_id']}",
            "player_id": r["player_id"],
            "full_name": name,
            "position_primary": r["position_primary"] or meta.get("position") or "",
            "club":      r["team_abbr"] or (meta.get("team") or "").upper(),
        })

    # ADP join reuses the Players-page matcher; ordering by pick number means
    # earlier picks claim ambiguous name matches first.
    shim = [{"full_name": r["full_name"], "position_primary": r["position_primary"],
             "pts": -r["pick_no"]} for r in rows]
    attach_fantrax_adp(shim)
    for r, sh in zip(rows, shim):
        r["adp"] = sh["adp"]
        # Positive = drafted later than ADP (value); negative = a reach.
        r["adp_delta"] = round(sh["adp"] - r["pick_no"], 1) if sh["adp"] else None

    # Board grid: one row per round, columns ordered by draft slot. Snake
    # order means odd rounds run 1→12 and even rounds 12→1; laying the board
    # out by slot keeps each manager in a single column.
    slots = sorted({r["slot"] for r in rows if r["slot"]})
    slot_manager = {}
    for r in rows:
        slot_manager.setdefault(r["slot"], r["manager"])
    by_slot_round = {(r["slot"], r["round"]): r for r in rows}
    max_round = max(r["round"] for r in rows)
    board = [
        {"round": rd, "cells": [by_slot_round.get((sl, rd)) for sl in slots]}
        for rd in range(1, max_round + 1)
    ]

    # Per-manager haul, in pick order
    by_manager: dict[int, dict] = {}
    for r in rows:
        m = by_manager.setdefault(r["roster_id"], {
            "roster_id": r["roster_id"], "manager": r["manager"],
            "slot": r["slot"], "picks": [], "pos_counts": {},
        })
        m["picks"].append(r)
        pos = r["position_primary"] or "?"
        m["pos_counts"][pos] = m["pos_counts"].get(pos, 0) + 1
    managers = sorted(by_manager.values(), key=lambda m: m["slot"] or 99)

    # Best value / biggest reach, only over players Fantrax actually ranked
    scored = [r for r in rows if r["adp_delta"] is not None]
    best_value = max(scored, key=lambda r: r["adp_delta"]) if scored else None
    biggest_reach = min(scored, key=lambda r: r["adp_delta"]) if scored else None

    return {
        "picks":    rows,
        "board":    board,
        "columns":  [{"slot": sl, "manager": slot_manager.get(sl, "")} for sl in slots],
        "managers": managers,
        "rounds":   max_round,
        "total":    len(rows),
        "first_round": [r for r in rows if r["round"] == 1],
        "best_value": best_value,
        "biggest_reach": biggest_reach,
    }


# ────────────────────────────────────────────────────────────────────
# Draft enrichment
# ────────────────────────────────────────────────────────────────────

def enrich_draft(draft: dict, standings: list[dict]) -> dict:
    if not draft:
        return {}
    std_map = {t["roster_id"]: t["position"] for t in standings}
    managers = list(draft.get("managers") or [])
    for m in managers:
        m["current_pos"] = std_map.get(m["roster_id"])
    # Sort by preseason_rank for the table
    managers.sort(key=lambda x: x.get("preseason_rank", 99))

    # Metrics
    if managers:
        strongest         = max(managers, key=lambda m: m.get("draft_strength", 0))
        weakest           = min(managers, key=lambda m: m.get("draft_strength", 1))
        avg_strength      = sum(m.get("draft_strength", 0) for m in managers) / len(managers)
        # Most concentrated = highest count after the dash
        def conc_count(s):
            try:
                return int((s or "0-0").split("-")[1])
            except (ValueError, IndexError):
                return 0
        most_concentrated = max(managers, key=lambda m: conc_count(m.get("concentration", "")))
    else:
        strongest = weakest = most_concentrated = {}
        avg_strength = 0

    return {
        "season":        draft.get("season", ""),
        "ranking_date":  draft.get("ranking_date", ""),
        "managers":      managers,
        "metrics": {
            "strongest":         strongest,
            "weakest":           weakest,
            "most_concentrated": most_concentrated,
            "avg_strength":      avg_strength,
        },
    }


# ────────────────────────────────────────────────────────────────────
# Featured matches enrichment — pairs yaml entries with the actual
# matchup pairing for the given GW (validates they face each other).
# ────────────────────────────────────────────────────────────────────

def enrich_featured_matches(conn, fm: dict, team_map: dict, standings: list[dict]) -> dict:
    if not fm:
        return {}
    gw    = fm.get("gw", 0)
    std   = {t["roster_id"]: t for t in standings}

    # Pull pairings for the week
    pair_rows = q(conn, """
        SELECT a.roster_id AS r_a, b.roster_id AS r_b, a.matchup_id
        FROM v_matchup_legs a
        JOIN v_matchup_legs b
          ON b.league_id=a.league_id AND b.season=a.season
         AND b.week=a.week AND b.matchup_id=a.matchup_id AND b.roster_id > a.roster_id
        WHERE a.league_id=? AND a.season=? AND a.week=?
    """, (LEAGUE_ID, SEASON, gw))
    pairs = {frozenset([r["r_a"], r["r_b"]]): r["matchup_id"] for r in pair_rows}

    out = []
    for m in fm.get("matches", []):
        home_id = m.get("home_roster_id")
        away_id = m.get("away_roster_id")
        key = frozenset([home_id, away_id])
        matchup_id = pairs.get(key)

        def side(rid):
            tm = team_map.get(rid, {})
            s  = std.get(rid, {})
            return {
                "roster_id":    rid,
                "display_name": s.get("display_name", ""),
                "team_name":    tm.get("team_name", f"Team {rid}"),
                "record":       f"{s.get('wins',0)}–{s.get('losses',0)}",
                "pts_for":      s.get("pts_for", 0),
                "position":     s.get("position", 0),
                "form":         s.get("form", []),
            }

        out.append({
            "matchup_id":  matchup_id,
            "actually_paired": matchup_id is not None,
            "headline":    m.get("headline", ""),
            "body":        m.get("body", ""),
            "home":        side(home_id),
            "away":        side(away_id),
        })
    return {"gw": gw, "matches": out}


# ────────────────────────────────────────────────────────────────────
# Weekly placement history (for the line chart)
# ────────────────────────────────────────────────────────────────────

def compute_weekly_placements(conn, team_map: dict, total_weeks: int = 38) -> dict:
    """Return {roster_id: [{week, pos}, ...]} for every completed week."""
    rows = q(conn, """
        SELECT a.week, a.roster_id,
               a.points  AS pts,
               b.points  AS opp_pts
        FROM v_matchup_legs a
        JOIN v_matchup_legs b
          ON b.league_id=a.league_id AND b.season=a.season
         AND b.week=a.week AND b.matchup_id=a.matchup_id AND b.roster_id != a.roster_id
        WHERE a.league_id=? AND a.season=? AND a.points IS NOT NULL AND b.points IS NOT NULL
        ORDER BY a.week, a.roster_id
    """, (LEAGUE_ID, SEASON))

    # Build per-week results
    per_week = {}  # week -> list of {roster_id, pts, opp_pts, win}
    for r in rows:
        w = r["week"]
        per_week.setdefault(w, []).append({
            "roster_id": r["roster_id"],
            "pts":       r["pts"],
            "opp_pts":   r["opp_pts"],
            "win":       r["pts"] > r["opp_pts"],
        })

    roster_ids = sorted(team_map.keys())
    cum = {rid: {"w": 0, "l": 0, "pf": 0.0} for rid in roster_ids}
    placements = {rid: [] for rid in roster_ids}

    completed_weeks = sorted(per_week.keys())
    max_played = max(completed_weeks) if completed_weeks else 0

    for week in range(1, max_played + 1):
        # Update cumulative tallies from that week
        for entry in per_week.get(week, []):
            rid = entry["roster_id"]
            if rid not in cum:
                cum[rid] = {"w": 0, "l": 0, "pf": 0.0}
            if entry["win"]:
                cum[rid]["w"] += 1
            else:
                cum[rid]["l"] += 1
            cum[rid]["pf"] += entry["pts"] or 0

        # Sort rosters by W desc, PF desc — that's the standing
        ordered = sorted(roster_ids,
                         key=lambda r: (-cum[r]["w"], -cum[r]["pf"]))
        for pos, rid in enumerate(ordered, 1):
            placements[rid].append({"week": week, "pos": pos})

    return {
        "placements":  placements,
        "max_played":  max_played,
        "total_weeks": total_weeks,
    }


# ────────────────────────────────────────────────────────────────────
# Player detail (one page per rostered player)
# ────────────────────────────────────────────────────────────────────

# Preferred ordering for stat keys in the pivot table (left-to-right).
# Any keys not in this list get sorted alphabetically and appended afterward.
# `pts_std` is always rendered last as the "Total" column.
STAT_KEY_ORDER = [
    "gp",     # games played (flag)
    "gs",     # games started
    "min",    # minutes
    "g",      # goals
    "at",     # assists
    "sh",     # shots
    "sog",    # shots on goal
    "kp",     # key passes
    "cr",     # crosses
    "tkl",    # tackles
    "int",    # interceptions
    "bs",     # blocks
    "clr",    # clearances
    "cs",     # clean sheet
    "sv",     # saves (GK)
    "gc",     # goals conceded
    "pkm",    # penalty missed
    "pks",    # penalty saved
    "og",     # own goal
    "yc",     # yellow card
    "rc",     # red card
]

STAT_KEY_LABELS = {
    "gp":  "GP",   "gs":  "GS",   "min": "MIN",
    "g":   "G",    "at":  "A",    "sh":  "SH",   "sog": "SOG",
    "kp":  "KP",   "cr":  "CR",   "tkl": "TKL",  "int": "INT",
    "bs":  "BLK",  "clr": "CLR",  "cs":  "CS",   "sv":  "SV",
    "gc":  "GC",   "pkm": "PKm",  "pks": "PKs",  "og":  "OG",
    "yc":  "YC",   "rc":  "RC",
}


_SCORING_CACHE = {}

def get_scoring_settings(conn) -> dict:
    if "settings" not in _SCORING_CACHE:
        row = q1(conn, "SELECT scoring_settings FROM league WHERE league_id=?", (LEAGUE_ID,))
        _SCORING_CACHE["settings"] = json.loads(row["scoring_settings"]) if row and row["scoring_settings"] else {}
    return _SCORING_CACHE["settings"]


def get_player_detail(conn, player_id: str) -> dict | None:
    p = q1(conn, """
        SELECT player_id, full_name, first_name, last_name, team_abbr,
               position_primary, height, birth_country, injury_status,
               injury_notes
        FROM players WHERE player_id = ?
    """, (player_id,))
    if not p:
        return None
    scoring = get_scoring_settings(conn)
    pos_lower = (p.get("position_primary") or "").lower()  # 'd', 'f', 'm', 'gk'

    # Pull every (week, stat_key, value) row for this player this season.
    rows = q(conn, """
        SELECT week, stat_key, stat_value
        FROM player_stats
        WHERE player_id=? AND season=? AND stat_value IS NOT NULL
        ORDER BY week
    """, (player_id, SEASON))

    # Build: { week: { stat_key: value, ... } }
    # Skip pos_* keys — Sleeper exposes a position-prefixed copy of every stat
    # (e.g. pos_m_g == g) that would otherwise double-count in the pivot.
    per_week: dict[int, dict[str, float]] = {}
    seen_keys: set[str] = set()
    for r in rows:
        k = r["stat_key"]
        if k.startswith("pos_"):
            continue
        w = r["week"]
        v = r["stat_value"]
        per_week.setdefault(w, {})[k] = v
        if k != "pts_std":   # pts_std handled separately as the Total column
            seen_keys.add(k)

    # Filter to keys that actually have a nonzero value at least once
    meaningful_keys = {k for k in seen_keys
                       if any((per_week[w].get(k) or 0) != 0 for w in per_week)}

    # Ordered column list: STAT_KEY_ORDER first, then alphabetical leftovers
    preferred  = [k for k in STAT_KEY_ORDER if k in meaningful_keys]
    remaining  = sorted(meaningful_keys - set(preferred))
    stat_keys  = preferred + remaining

    # Build the rendered rows, one per week played, most recent first
    weeks_sorted = sorted(per_week.keys())
    pivot_rows = []
    col_totals = {k: 0.0 for k in stat_keys}
    pts_total  = 0.0
    for w in weeks_sorted:
        cells = []
        for k in stat_keys:
            v = per_week[w].get(k)
            cells.append(v)
            if v is not None:
                col_totals[k] += v
        pts = per_week[w].get("pts_std")
        pts_total += (pts or 0)
        pivot_rows.append({
            "week":  w,
            "cells": cells,
            "pts":   pts,
        })

    # Totals row (season totals for each column)
    totals_cells = [col_totals[k] for k in stat_keys]

    # Pts contribution per stat — totals × position-specific scoring multiplier
    contrib_cells = []
    pct_cells     = []
    for k in stat_keys:
        mult = scoring.get(f"pos_{pos_lower}_{k}", 0) if pos_lower else 0
        contrib = (col_totals[k] or 0) * mult
        contrib_cells.append(contrib if mult else None)
        if pts_total and mult:
            pct_cells.append((contrib / pts_total) * 100)
        else:
            pct_cells.append(None)

    # Stat headers with labels
    headers = [{"key": k, "label": STAT_KEY_LABELS.get(k, k.upper())} for k in stat_keys]

    # Owner (if rostered)
    owner = q1(conn, """
        SELECT r.roster_id, u.display_name
        FROM rosters r, json_each(r.players) j
        LEFT JOIN league_users u ON u.user_id=r.owner_id AND u.league_id=r.league_id
        WHERE r.league_id=? AND j.value=?
        LIMIT 1
    """, (LEAGUE_ID, player_id))

    return {
        "player":         p,
        "owner":          owner,
        "headers":        headers,        # [{key, label}, ...]
        "rows":           pivot_rows,     # [{week, cells, pts}]
        "totals_cells":   totals_cells,   # per-column raw stat totals (e.g. 15 goals)
        "contrib_cells":  contrib_cells,  # per-column pts contributed (e.g. 135.0 from goals)
        "pct_cells":      pct_cells,      # per-column % of total pts
        "pts_total":      pts_total,
        "weeks":          len([r for r in pivot_rows if (r["pts"] or 0) > 0]),
    }


def get_all_rostered_player_ids(conn) -> list[str]:
    rows = q(conn, """
        SELECT DISTINCT j.value AS player_id
        FROM rosters r, json_each(r.players) j
        WHERE r.league_id=?
    """, (LEAGUE_ID,))
    return [r["player_id"] for r in rows]


# ────────────────────────────────────────────────────────────────────
# Featured team enrichment
# ────────────────────────────────────────────────────────────────────

def enrich_featured(featured: dict, standings: list[dict], team_map: dict) -> dict:
    if not featured:
        return {}
    rid = featured.get("roster_id")
    std = next((t for t in standings if t["roster_id"] == rid), None)
    tm  = team_map.get(rid, {})
    out = dict(featured)
    if std:
        out["team_name"] = std["team_name"]
        out["display_name"] = std["display_name"]
    out["pl_club"] = tm.get("pl_club", "")
    return out


# ────────────────────────────────────────────────────────────────────
# Jinja2 environment
# ────────────────────────────────────────────────────────────────────

def make_env(relative_depth: int = 0) -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    prefix = "../" * relative_depth if relative_depth else ""

    def url_for(kind: str, name: str) -> str:
        if kind == "static":
            return f"{prefix}static/{name}"
        if kind == "page":
            pages = {
                "index":     f"{prefix}index.html",
                "table":     f"{prefix}table.html",
                "gameweeks": f"{prefix}gameweeks.html",
                "clubs":     f"{prefix}clubs.html",
                "players":   f"{prefix}players.html",
                "fixtures":  f"{prefix}fixtures.html",
                "stats":     f"{prefix}stats.html",
                "draft":     f"{prefix}draft.html",
                "draftlab":  f"{prefix}draftlab.html",
                "managers":  f"{prefix}managers.html",
                "history":   f"{prefix}history.html",
                "subscribe": f"{prefix}subscribe.html",
            }
            return pages.get(name, f"{prefix}{name}.html")
        return name

    def url_club(roster_id: int) -> str:
        return f"{prefix}clubs/{roster_id}.html"

    def url_gw(week: int) -> str:
        return f"{prefix}gameweek/{week}.html"

    def url_player(player_id) -> str:
        return f"{prefix}players/{player_id}.html"

    env.globals["url_for"]    = url_for
    env.globals["url_club"]   = url_club
    env.globals["url_gw"]     = url_gw
    env.globals["url_player"] = url_player
    # Season labels so no template has to hardcode "2025/26" again.
    env.globals["SEASON_LABEL"]     = season_label(SEASON)
    env.globals["LAB_SEASON_LABEL"] = season_label(LAB_SEASON)
    return env


def render(env: Environment, template_name: str, dest: Path, **ctx):
    t = env.get_template(template_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(t.render(**ctx), encoding="utf-8")
    print(f"  wrote {dest.relative_to(HERE)}")


# ────────────────────────────────────────────────────────────────────
# Build
# ────────────────────────────────────────────────────────────────────

def build(open_after: bool = False):
    conn          = open_db()
    team_map      = load_team_mapping()
    current_week  = get_current_week(conn)
    standings     = get_standings(conn, team_map)
    standings_map = {t["roster_id"]: t for t in standings}
    weeks_summary = get_all_weeks_summary(conn)
    last_gw       = get_gw_detail(conn, current_week, team_map, standings_map) if current_week >= 1 else None
    histories     = load_histories()
    preseason     = current_week < 1
    season_start  = get_season_start(conn)
    draft_summary = get_draft_summary(conn)
    upcoming_week = current_week + 1
    upcoming      = get_upcoming_matchups(conn, upcoming_week, team_map, standings)
    epl_fixtures  = get_epl_fixtures(conn, upcoming_week)
    featured      = enrich_featured(load_featured(), standings, team_map)
    featured_mm   = enrich_featured_matches(conn, load_featured_matches(), team_map, standings)
    draft         = enrich_draft(load_draft(), standings)
    stats_data    = compute_stats(conn, team_map, standings)
    placements    = compute_weekly_placements(conn, team_map)

    # Copy static
    dest_static = DIST_DIR / "static"
    if dest_static.exists():
        shutil.rmtree(dest_static)
    shutil.copytree(STATIC_DIR, dest_static)
    print("  copied static/")

    # Remove obsolete pages from previous builds (e.g. londoner.html)
    for legacy in ("londoner.html",):
        legacy_path = DIST_DIR / legacy
        if legacy_path.exists():
            legacy_path.unlink()
            print(f"  removed {legacy_path.relative_to(HERE)}")

    env0 = make_env(0)

    # Top-level pages
    render(env0, "index.html", DIST_DIR / "index.html",
           active_nav="home",
           standings=standings,
           current_week=current_week,
           last_gw=last_gw,
           featured=featured,
           season_high=stats_data["season_high"],
           preseason=preseason,
           season_start=season_start,
           draft_summary=draft_summary,
           last_champion=(histories[0] if histories else None),
           team_map=team_map)

    render(env0, "table.html", DIST_DIR / "table.html",
           active_nav="table",
           standings=standings,
           current_week=current_week,
           placements=placements,
           team_map=team_map)

    render(env0, "clubs.html", DIST_DIR / "clubs.html",
           active_nav="clubs",
           standings=standings,
           team_map=team_map)

    render(env0, "gameweeks.html", DIST_DIR / "gameweeks.html",
           active_nav="gameweeks",
           weeks=weeks_summary,
           current_week=current_week,
           season_start=season_start,
           team_map=team_map)

    # Season selector: every season we hold stats for, newest first. The
    # in-progress season shows up on its own once the first GW lands.
    player_seasons = get_stat_seasons(conn)
    all_players = []
    for ps in player_seasons:
        rows_ = get_all_active_players(conn, season=ps["season"])
        for pl in rows_:
            pl["season"] = ps["season"]
        all_players.extend(rows_)
    render(env0, "players.html", DIST_DIR / "players.html",
           active_nav="players",
           players=all_players,
           player_seasons=player_seasons,
           season_labels={ps["season"]: ps["label"] for ps in player_seasons},
           team_map=team_map)

    render(env0, "fixtures.html", DIST_DIR / "fixtures.html",
           active_nav="fixtures",
           current_week=current_week,
           upcoming_matchups=upcoming,
           featured_matches=featured_mm,
           epl_fixtures=epl_fixtures,
           team_map=team_map)

    render(env0, "stats.html", DIST_DIR / "stats.html",
           active_nav="stats",
           current_week=current_week,
           stats=stats_data,
           team_map=team_map)

    render(env0, "draft.html", DIST_DIR / "draft.html",
           active_nav="draft",
           draft=draft,
           board=get_draft_board(conn),
           team_map=team_map)

    render(env0, "draftlab.html", DIST_DIR / "draftlab.html",
           active_nav="draftlab",
           lab=get_draft_lab_data(conn),
           team_map=team_map)

    render(env0, "managers.html", DIST_DIR / "managers.html",
           active_nav="managers",
           mgr=get_manager_stats(conn, histories, team_map),
           team_map=team_map)

    render(env0, "history.html", DIST_DIR / "history.html",
           active_nav="history",
           histories=histories,
           team_map=team_map)

    render(env0, "subscribe.html", DIST_DIR / "subscribe.html",
           active_nav="subscribe",
           subscribe_endpoint=SUBSCRIBE_ENDPOINT,
           managers=standings,
           prefill_email=None)

    # Per-GW pages
    env1 = make_env(1)
    max_week = max((w["week"] for w in weeks_summary), default=1)
    # Include upcoming week if pairings are stored
    has_upcoming = bool(q1(conn,
        "SELECT 1 FROM v_matchup_legs WHERE league_id=? AND season=? AND week=? LIMIT 1",
        (LEAGUE_ID, SEASON, upcoming_week)))
    range_end = upcoming_week if has_upcoming else max_week
    gw_written = set()
    for w in range(1, range_end + 1):
        gw     = get_gw_detail(conn, w, team_map, standings_map)
        weekly = compute_weekly_awards(conn, w, team_map, standings)
        render(env1, "gameweek_detail.html", DIST_DIR / "gameweek" / f"{w}.html",
               active_nav="gameweeks",
               gw=gw, current_week=current_week, team_map=team_map,
               stats=stats_data, weekly=weekly)
        gw_written.add(f"{w}.html")

    # Gameweek pages from a previous season are orphaned once the season
    # rolls over — the Gameweeks index only lists the current season, and
    # past results live on the History page.
    gw_dir = DIST_DIR / "gameweek"
    if gw_dir.exists():
        stale_gw = [f for f in gw_dir.glob("*.html") if f.name not in gw_written]
        for f in stale_gw:
            f.unlink()
        if stale_gw:
            print(f"  removed {len(stale_gw)} stale gameweek pages")

    # Per-club pages
    opp_owner_map = {t["roster_id"]: t["display_name"] for t in standings}
    for roster_id in team_map:
        club = get_club_detail(conn, roster_id, team_map, standings_map)
        render(env1, "club_detail.html", DIST_DIR / "clubs" / f"{roster_id}.html",
               active_nav="clubs",
               club=club, team_map=team_map,
               opp_owner_map=opp_owner_map)

    # Per-player pages (rostered only)
    rostered_ids = get_all_rostered_player_ids(conn)
    print(f"  generating {len(rostered_ids)} player pages…")
    written = set()
    for pid in rostered_ids:
        detail = get_player_detail(conn, pid)
        if not detail:
            continue
        render(env1, "player_detail.html", DIST_DIR / "players" / f"{pid}.html",
               active_nav="players",
               detail=detail, team_map=team_map)
        written.add(f"{pid}.html")

    # Drop player pages left over from a previous season's rosters — they
    # carry stale ownership and never get refreshed otherwise.
    players_dir = DIST_DIR / "players"
    if players_dir.exists():
        stale = [f for f in players_dir.glob("*.html") if f.name not in written]
        for f in stale:
            f.unlink()
        if stale:
            print(f"  removed {len(stale)} stale player pages")

    conn.close()
    print(f"\nBuild complete → {DIST_DIR}/")

    if open_after:
        subprocess.run(["open", str(DIST_DIR / "index.html")])


# ────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="open index.html after build")
    args = ap.parse_args()

    try:
        import yaml  # noqa: F401
    except ImportError:
        print("Installing deps…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                               "jinja2", "pyyaml", "certifi"])
        import yaml  # noqa: F401

    build(open_after=args.open)
