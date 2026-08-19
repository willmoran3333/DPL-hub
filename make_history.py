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

    standings = rows(conn, """
        SELECT r.roster_id, u.display_name, u.team_name,
               r.wins, r.losses, r.ties,
               ROUND(COALESCE(r.fpts,0) + COALESCE(r.fpts_decimal,0)/100.0, 2)                 AS pts_for,
               ROUND(COALESCE(r.fpts_against,0) + COALESCE(r.fpts_against_decimal,0)/100.0, 2) AS pts_against
        FROM rosters r
        LEFT JOIN league_users u
               ON u.league_id = r.league_id AND u.user_id = r.owner_id
        WHERE r.league_id = ?
        ORDER BY r.wins DESC, pts_for DESC
    """, (league_id,))
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
