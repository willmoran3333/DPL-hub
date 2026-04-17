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
HISTORY_2024_PATH   = HERE / "history_2024.json"
FEATURED_PATH       = HERE / "featured_team.yml"
FEATURED_MATCHES_PATH = HERE / "featured_matches.yml"
DRAFT_PATH          = HERE / "draft_data.yml"

LEAGUE_ID     = "1244790289042776064"
SEASON        = "2025"
YOU_ROSTER_ID = 1  # willmoran

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
    row = q1(conn, "SELECT week FROM sport_state WHERE sport = 'clubsoccer:epl'")
    return (row["week"] if row else 1) - 1  # last completed week


def get_standings(conn, team_map: dict) -> list[dict]:
    rows = q(conn, """
        SELECT r.roster_id, u.display_name, u.team_name,
               r.wins, r.losses, r.ties,
               ROUND(COALESCE(r.fpts,0) + COALESCE(r.fpts_decimal,0), 2)         AS pts_for,
               ROUND(COALESCE(r.fpts_against,0) + COALESCE(r.fpts_against_decimal,0), 2) AS pts_against,
               r.total_moves, r.waiver_budget_used,
               r.metadata AS metadata_json
        FROM rosters r
        LEFT JOIN league_users u ON u.league_id = r.league_id AND u.user_id = r.owner_id
        WHERE r.league_id = ?
        ORDER BY r.wins DESC, pts_for DESC
    """, (LEAGUE_ID,))

    standings = []
    for pos, row in enumerate(rows, 1):
        d = dict(row)
        rid = d["roster_id"]
        tm  = team_map.get(rid, {})
        d["team_name"]   = d["team_name"] or tm.get("team_name", f"Team {rid}")
        d["pl_club"]     = tm.get("pl_club", "")
        d["position"]    = pos
        d["is_you"]      = rid == YOU_ROSTER_ID

        try:
            meta = json.loads(d.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        rec = meta.get("record") or ""
        d["form"] = list(rec[-5:]) if rec else []
        d["record_str"] = rec

        standings.append(d)
    return standings


# ────────────────────────────────────────────────────────────────────
# Matchups
# ────────────────────────────────────────────────────────────────────

def get_week_matchups(conn, week: int, team_map: dict, standings_map: dict | None = None) -> list[dict]:
    rows = q(conn, """
        SELECT a.roster_id AS roster_a, a.points AS pts_a,
               b.roster_id AS roster_b, b.points AS pts_b,
               a.matchup_id, a.starters AS starters_a, b.starters AS starters_b
        FROM matchup_legs a
        JOIN matchup_legs b
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
        FROM matchup_legs a
        JOIN matchup_legs b
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
        FROM matchup_legs
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
              SELECT json_each.value FROM matchup_legs ml, json_each(ml.starters)
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

def get_rostered_players(conn) -> list[dict]:
    """Legacy shim kept for backward compatibility; now returns all active players."""
    return get_all_active_players(conn)


def get_all_active_players(conn, current_week: int | None = None) -> list[dict]:
    """
    Return every player who has played at least one minute or scored any
    points this season, rostered or not. Each row includes rich aggregate
    stats + a `last5` list for the form sparkline.
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
    """, (SEASON, LEAGUE_ID))
    players = [dict(r) for r in rows]

    # Pull last-5-weeks pts_std for each player (for the sparkline)
    # Determine the window of weeks we care about
    last_weeks = q(conn, """
        SELECT DISTINCT week FROM player_stats
        WHERE season=? AND stat_key='pts_std'
        ORDER BY week DESC LIMIT 5
    """, (SEASON,))
    window_weeks = sorted([r["week"] for r in last_weeks])

    # Pull all pts_std rows in the window
    pts_rows = q(conn, f"""
        SELECT player_id, week, stat_value AS pts
        FROM player_stats
        WHERE season = ? AND stat_key = 'pts_std'
          AND week IN ({','.join('?' * len(window_weeks)) or '0'})
    """, (SEASON, *window_weeks)) if window_weeks else []
    recent_map: dict[str, dict[int, float]] = {}
    for r in pts_rows:
        recent_map.setdefault(r["player_id"], {})[r["week"]] = r["pts"] or 0

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

    return players


# ────────────────────────────────────────────────────────────────────
# Per-club detail
# ────────────────────────────────────────────────────────────────────

def get_club_detail(conn, roster_id: int, team_map: dict, standings_map: dict) -> dict:
    tm = team_map.get(roster_id, {})
    owner_row = q1(conn, """
        SELECT u.display_name, u.team_name, r.wins, r.losses,
               ROUND(COALESCE(r.fpts,0) + COALESCE(r.fpts_decimal,0), 2)         AS pts_for,
               ROUND(COALESCE(r.fpts_against,0) + COALESCE(r.fpts_against_decimal,0), 2) AS pts_against,
               r.total_moves, r.metadata AS metadata_json
        FROM rosters r
        LEFT JOIN league_users u ON u.league_id = r.league_id AND u.user_id = r.owner_id
        WHERE r.league_id = ? AND r.roster_id = ?
    """, (LEAGUE_ID, roster_id))

    weekly = q(conn, """
        SELECT week, points,
               (SELECT points FROM matchup_legs b
                WHERE b.league_id = ml.league_id AND b.season = ml.season
                  AND b.week = ml.week AND b.matchup_id = ml.matchup_id
                  AND b.roster_id != ml.roster_id) AS opp_points,
               (SELECT roster_id FROM matchup_legs b
                WHERE b.league_id = ml.league_id AND b.season = ml.season
                  AND b.week = ml.week AND b.matchup_id = ml.matchup_id
                  AND b.roster_id != ml.roster_id) AS opp_roster_id
        FROM matchup_legs ml
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
        tm = team_map.get(rid, {})
        std = std_map.get(rid, {})
        return tm.get("team_name", f"Team {rid}"), std.get("display_name", "")

    # Season high
    high = q1(conn, """
        SELECT roster_id, week, points FROM matchup_legs
        WHERE league_id=? AND season=? AND points IS NOT NULL
        ORDER BY points DESC LIMIT 1
    """, (LEAGUE_ID, SEASON))
    high["team_name"], high["display_name"] = team_label(high["roster_id"])

    # Season low (nonzero)
    low = q1(conn, """
        SELECT roster_id, week, points FROM matchup_legs
        WHERE league_id=? AND season=? AND points IS NOT NULL AND points > 0
        ORDER BY points ASC LIMIT 1
    """, (LEAGUE_ID, SEASON))
    low["team_name"], low["display_name"] = team_label(low["roster_id"])

    # Biggest blowout + closest game (with both teams scored)
    margins = q(conn, """
        SELECT a.week, a.matchup_id, a.roster_id AS r_a, a.points AS p_a,
               b.roster_id AS r_b, b.points AS p_b,
               ABS(a.points - b.points) AS diff
        FROM matchup_legs a
        JOIN matchup_legs b
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
        FROM matchup_legs a
        JOIN matchup_legs b
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
            "team_name":  loser_name,
            "opp_name":   winner_name,
            "week":       pyr["week"],
            "points":     pyr["points"],
            "opp_points": pyr["opp_points"],
        }

    # The Gift — lowest-scoring team that WON the week.
    gift = q1(conn, """
        SELECT a.roster_id, a.week, a.points,
               b.roster_id AS opp_roster, b.points AS opp_points
        FROM matchup_legs a
        JOIN matchup_legs b
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
            "team_name":  winner_name,
            "opp_name":   loser_name,
            "week":       gift["week"],
            "points":     gift["points"],
            "opp_points": gift["opp_points"],
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
            JOIN matchup_legs ml ON ml.league_id=? AND ml.season=ps.season AND ml.week=ps.week
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
            row["team_name"], _ = team_label(row["roster_id"])
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
        JOIN matchup_legs ml ON ml.league_id=? AND ml.season=ps.season AND ml.week=ps.week
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
        FROM matchup_legs
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
        tm  = team_map.get(rid, {})
        std = std_map.get(rid, {})
        return tm.get("team_name", f"Team {rid}"), std.get("display_name", "")

    # Weekly team high / low
    rows = q(conn, """
        SELECT roster_id, points FROM matchup_legs
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
        FROM matchup_legs a
        JOIN matchup_legs b
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
            JOIN matchup_legs ml ON ml.league_id=? AND ml.season=ps.season AND ml.week=ps.week
            WHERE ps.season=? AND ps.week=? AND ps.stat_key='pts_std'
              AND p.position_primary = ?
              AND EXISTS (
                  SELECT 1 FROM json_each(ml.starters)
                  WHERE json_each.value = ps.player_id
              )
            ORDER BY pts DESC LIMIT 1
        """, (LEAGUE_ID, SEASON, week, pos))
        if row:
            row["team_name"], _ = team_label(row["roster_id"])
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
        FROM fixtures WHERE week = ?
        ORDER BY date
    """, (week,))
    return [dict(r) for r in rows]


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
        FROM matchup_legs a
        JOIN matchup_legs b
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
        FROM matchup_legs a
        JOIN matchup_legs b
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
    hist_2024     = load_history_2024()
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
           team_map=team_map)

    render(env0, "players.html", DIST_DIR / "players.html",
           active_nav="players",
           players=get_rostered_players(conn),
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
           team_map=team_map)

    render(env0, "history.html", DIST_DIR / "history.html",
           active_nav="history",
           hist=hist_2024,
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
        "SELECT 1 FROM matchup_legs WHERE league_id=? AND season=? AND week=? LIMIT 1",
        (LEAGUE_ID, SEASON, upcoming_week)))
    range_end = upcoming_week if has_upcoming else max_week
    for w in range(1, range_end + 1):
        gw     = get_gw_detail(conn, w, team_map, standings_map)
        weekly = compute_weekly_awards(conn, w, team_map, standings)
        render(env1, "gameweek_detail.html", DIST_DIR / "gameweek" / f"{w}.html",
               active_nav="gameweeks",
               gw=gw, current_week=current_week, team_map=team_map,
               stats=stats_data, weekly=weekly)

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
    for pid in rostered_ids:
        detail = get_player_detail(conn, pid)
        if not detail:
            continue
        render(env1, "player_detail.html", DIST_DIR / "players" / f"{pid}.html",
               active_nav="players",
               detail=detail, team_map=team_map)

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
