#!/usr/bin/env python3
"""
Freeze a completed season into history_{season}.json for the History page.

    python3 make_history.py 2025 --notes "erozier ran away with it."

Re-runnable: overwrites the file. The History page reads whatever files are
listed in build_site.PAST_SEASONS.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "dpl.db"


def rows(conn, sql, args=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, args)]


def build(season: str, league_id: str, notes: str) -> dict:
    conn = sqlite3.connect(DB_PATH)

    # Standings are computed from the played matchups, not read off
    # rosters.wins/fpts. Sleeper stopped updating those after week 37 of
    # 2025/26, which left every manager a game short. Recomputing reproduces
    # 2024/25's stored totals exactly and gets 2025/26 right.
    #
    # NB: team_name comes from league_users, which reflects whatever a manager
    # calls their team *today* — rename after a season and a re-freeze will
    # overwrite the historical name. Check the file before overwriting one.
    standings = rows(conn, """
        WITH legs AS (
            SELECT a.roster_id,
                   COALESCE(a.custom_points, a.points) AS pf,
                   COALESCE(b.custom_points, b.points) AS pa
            FROM matchup_legs a
            JOIN matchup_legs b
              ON b.league_id = a.league_id AND b.season = a.season
             AND b.week = a.week AND b.matchup_id = a.matchup_id
             AND b.roster_id <> a.roster_id
            WHERE a.league_id = ?
              AND a.points IS NOT NULL AND b.points IS NOT NULL
        )
        SELECT r.roster_id, u.display_name, u.team_name,
               SUM(CASE WHEN l.pf > l.pa THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN l.pa > l.pf THEN 1 ELSE 0 END) AS losses,
               SUM(CASE WHEN l.pf = l.pa THEN 1 ELSE 0 END) AS ties,
               ROUND(SUM(l.pf), 2)                          AS pts_for,
               ROUND(SUM(l.pa), 2)                          AS pts_against
        FROM rosters r
        JOIN legs l ON l.roster_id = r.roster_id
        LEFT JOIN league_users u
               ON u.league_id = r.league_id AND u.user_id = r.owner_id
        WHERE r.league_id = ?
        GROUP BY r.roster_id, u.display_name, u.team_name
        ORDER BY wins DESC, pts_for DESC
    """, (league_id, league_id))
    for t in standings:
        t["team_name"] = (t["team_name"] or "").strip()

    name_of = {t["roster_id"]: t["display_name"] for t in standings}

    hi = rows(conn, """
        SELECT roster_id, week, points FROM v_matchup_legs
        WHERE league_id = ? AND points IS NOT NULL
        ORDER BY points DESC LIMIT 1
    """, (league_id,))
    lo = rows(conn, """
        SELECT roster_id, week, points FROM v_matchup_legs
        WHERE league_id = ? AND points IS NOT NULL AND points > 0
        ORDER BY points ASC LIMIT 1
    """, (league_id,))
    weeks = rows(conn, """
        SELECT COUNT(DISTINCT week) AS n FROM v_matchup_legs
        WHERE league_id = ? AND points IS NOT NULL
    """, (league_id,))[0]["n"]
    conn.close()

    def rec(t):
        return f"{t['wins']}–{t['losses']}" + (f"–{t['ties']}" if t["ties"] else "")

    champ, runner = standings[0], standings[1]
    most_wins = max(standings, key=lambda t: t["wins"])
    highest_pf = max(standings, key=lambda t: t["pts_for"])

    return {
        "season": season,
        "league_id": league_id,
        "standings": standings,
        "awards": {
            "champion":   {"display_name": champ["display_name"],  "team_name": champ["team_name"],
                           "record": rec(champ),  "pts": champ["pts_for"]},
            "runner_up":  {"display_name": runner["display_name"], "team_name": runner["team_name"],
                           "record": rec(runner), "pts": runner["pts_for"]},
            "high_score": {"pts": round(hi[0]["points"], 2), "display_name": name_of.get(hi[0]["roster_id"]),
                           "week": hi[0]["week"]} if hi else None,
            "low_score":  {"pts": round(lo[0]["points"], 2), "display_name": name_of.get(lo[0]["roster_id"]),
                           "week": lo[0]["week"]} if lo else None,
            "most_wins":  {"display_name": most_wins["display_name"], "wins": most_wins["wins"]},
            "highest_pf": {"display_name": highest_pf["display_name"], "pts": highest_pf["pts_for"]},
        },
        "weeks_played": weeks,
        "notes": notes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("season")
    ap.add_argument("--league", help="league_id (default: looked up from the DB)")
    ap.add_argument("--notes", default="")
    a = ap.parse_args()

    league_id = a.league
    if not league_id:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT league_id FROM league WHERE season = ?", (a.season,)).fetchone()
        conn.close()
        if not row:
            raise SystemExit(f"no league in dpl.db for season {a.season} — pass --league")
        league_id = row[0]

    data = build(a.season, league_id, a.notes)
    out = HERE / f"history_{a.season}.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out.name}: champion {data['awards']['champion']['display_name']} "
          f"({data['awards']['champion']['record']}), {data['weeks_played']} weeks")


if __name__ == "__main__":
    main()
