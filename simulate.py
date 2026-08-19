#!/usr/bin/env python3
"""
DPL season simulator — correlated Monte Carlo over the real 38-week schedule.

    python3 simulate.py --sims 5000
    python3 simulate.py --projections      # just show the projection inputs

Title is decided by best H2H record (no playoffs), points-for breaking ties.

Modelling, in short:
  * Expected points per appearance blends this league's own 2025/26 per-90
    rates (re-scored under the live 2026/27 rules) with an FPL price anchor,
    weighted by how much history each player actually has.
  * Availability is drawn per player per week. Managers see availability
    before setting a lineup — which is what makes bench depth worth anything.
  * Performance is a gamma draw around the player's mean, with a shared
    per-club, per-week shock so teammates boom and blank together. Without
    that, a five-man City stack would look as safe as five separate clubs.
  * Lineups are chosen on EXPECTED points among available players, never on
    realised ones — no hindsight.
"""
from __future__ import annotations

import argparse, json, re, sqlite3, sys, unicodedata
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "dpl.db"
FPL_CACHE = HERE / "data" / "fpl_bootstrap.json"
FPL_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"

LEAGUE_ID = "1385458928208343040"
SEASON = "2026"
PRIOR_SEASON = "2025"
PRIOR_LEAGUE = "1244790289042776064"

# Roster: F, M, M, M, D, D, D, GK, FM_FLEX, MD_FLEX, FMD_FLEX  (+6 bench)
BASE_SLOTS = {"GK": 1, "F": 1, "M": 3, "D": 3}
# (extra_F, extra_M, extra_D) fillings of the three flex slots that are legal:
# a forward can take FM or FMD, a defender MD or FMD, a midfielder anything.
FLEX_COMBOS = [(0,1,2),(0,2,1),(0,3,0),(1,0,2),(1,1,1),(1,2,0),(2,0,1),(2,1,0)]

# Weekly spread when a player features, measured from 2025/26 under 2026 rules.
POS_CV = {"GK": 1.03, "D": 0.88, "M": 0.84, "F": 0.85}
POS_MEAN90 = {"GK": 6.5, "D": 6.7, "M": 7.5, "F": 8.6}
CLUB_SHOCK = 0.30      # sd of the shared per-club, per-week log shock
SHRINK_MINUTES = 2000  # minutes at which a player's own rate gets half weight.
                       # Raised from 900 to lean harder on the FPL price anchor:
                       # price embeds this-season expectation (transfers, role
                       # changes, fitness) that last season's rate cannot see.

# Availability persists far less year-to-year than scoring rate does: a player
# who missed half of last season is not permanently 65% likely to feature.
# Taking last season's minutes share at face value made the sim rank managers
# on whose picks happened to stay fit last year, which is close to noise.
# Regress it hard toward the rostered-player baseline.
AVAIL_PERSIST = 0.45

# Price also carries information about MINUTES, not just quality — clubs and
# the market price expected starters up. Blend a price-implied availability in
# alongside the (regressed) minutes history.
PRICE_AVAIL_WEIGHT = 0.35

# Waivers and trades: nobody sits on the roster they drafted for 38 weeks.
# Teams drift toward the league mean as the season runs. This is the fraction
# of a roster's initial gap to average that has closed by the final week.
REVERSION_BY_SEASON_END = 0.35


# ── name matching ────────────────────────────────────────────────────
_TRANSLIT = str.maketrans({"ø":"o","Ø":"O","đ":"d","Đ":"D","ł":"l","Ł":"L",
                           "æ":"ae","Æ":"Ae","ß":"ss","þ":"th","Þ":"Th","ð":"d","Ð":"D"})

def toks(s: str) -> frozenset:
    s = unicodedata.normalize("NFKD", (s or "").translate(_TRANSLIT))
    s = s.encode("ascii", "ignore").decode()
    return frozenset(t for t in re.split(r"[^a-z]+", s.lower()) if t)


def load_fpl(refresh: bool = False) -> dict:
    """FPL bootstrap, cached on disk. --refresh-fpl re-pulls it."""
    if FPL_CACHE.exists() and not refresh:
        return json.loads(FPL_CACHE.read_text())
    import ssl
    from urllib.request import Request, urlopen
    try:                                  # same cert handling as ingest.py
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = Request(FPL_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30, context=ctx) as r:
        data = json.loads(r.read().decode())
    FPL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FPL_CACHE.write_text(json.dumps(data))
    return data


def build_projections(conn) -> list[dict]:
    """One row per rostered player: expected points per start, and availability."""
    fpl = load_fpl()
    team_of = {t["id"]: t["short_name"] for t in fpl["teams"]}
    pos_of = {1: "GK", 2: "D", 3: "M", 4: "F"}
    elements = []
    for e in fpl["elements"]:
        full = f"{e['first_name']} {e['second_name']}"
        elements.append({
            "tokens": toks(full) | toks(e["web_name"]),
            "pos": pos_of[e["element_type"]], "club": team_of[e["team"]],
            "cost": e["now_cost"] / 10.0, "minutes": e["minutes"],
            "status": e["status"],
            "chance": e.get("chance_of_playing_next_round"),
            "taken": False,
        })

    scoring = json.loads(conn.execute(
        "SELECT scoring_settings FROM league WHERE league_id=?", (LEAGUE_ID,)
    ).fetchone()[0] or "{}")

    # Prior-season points and minutes, re-scored under THIS season's rules
    pts, mins = defaultdict(float), defaultdict(float)
    for pid, key, v in conn.execute("""
        SELECT player_id, stat_key, SUM(stat_value) FROM player_stats
        WHERE season=? AND stat_key LIKE 'pos_%' AND stat_value IS NOT NULL
        GROUP BY player_id, stat_key""", (PRIOR_SEASON,)):
        m = scoring.get(key, 0) or 0
        if m:
            pts[pid] += v * m
    for pid, v in conn.execute("""
        SELECT player_id, SUM(stat_value) FROM player_stats
        WHERE season=? AND stat_key='min' GROUP BY player_id""", (PRIOR_SEASON,)):
        mins[pid] = v or 0.0

    rows = list(conn.execute("""
        SELECT r.roster_id, u.display_name, j.value AS player_id,
               p.full_name, p.position_primary, p.team_abbr
        FROM rosters r, json_each(r.players) j
        JOIN players p ON p.player_id = j.value
        LEFT JOIN league_users u ON u.league_id=r.league_id AND u.user_id=r.owner_id
        WHERE r.league_id = ?""", (LEAGUE_ID,)))

    # Price anchor: regress prior per-90 on log(cost) within position
    def match_fpl(name, club, pos):
        st = toks(name); best, bs = None, -1
        for e in elements:
            if e["taken"]:
                continue
            ov = st & e["tokens"]
            if not ov:
                continue
            s = len(ov) + (3 if (st <= e["tokens"] or e["tokens"] <= st) else 0) \
                + (4 if e["club"] == club else 0) + (2 if e["pos"] == pos else 0)
            if s > bs:
                best, bs = e, s
        if best:
            best["taken"] = True
        return best

    prelim = []
    for rid, mgr, pid, name, pos, club in rows:
        if pos not in POS_CV:
            continue
        e = match_fpl(name, club, pos)
        m90 = mins.get(pid, 0.0)
        own_rate = (pts.get(pid, 0.0) / m90 * 90) if m90 >= 90 else None
        prelim.append({"roster_id": rid, "manager": mgr, "player_id": pid,
                       "name": name, "pos": pos, "club": club, "fpl": e,
                       "minutes_prior": m90, "own_rate": own_rate})

    # Price -> rate model, fitted per position on players with real history
    price_model = {}
    for pos in POS_CV:
        xs, ys = [], []
        for p in prelim:
            if p["pos"] == pos and p["own_rate"] is not None and p["fpl"] \
               and p["minutes_prior"] >= 600:
                xs.append(np.log(p["fpl"]["cost"])); ys.append(p["own_rate"])
        if len(xs) >= 8:
            b, a = np.polyfit(xs, ys, 1)
            price_model[pos] = (a, b)
        else:
            price_model[pos] = (POS_MEAN90[pos], 0.0)

    # Cost percentile within position, over the whole FPL pool
    pos_cost_rank: dict[str, dict[float, float]] = {}
    for pos in POS_CV:
        costs = sorted(e["cost"] for e in elements if e["pos"] == pos)
        if costs:
            pos_cost_rank[pos] = {
                round(c, 1): i / max(len(costs) - 1, 1)
                for i, c in enumerate(costs)
            }

    # Baseline availability = what a typical rostered player manages
    shares = [min(p["minutes_prior"] / (38 * 90), 1.0) for p in prelim if p["minutes_prior"]]
    avail_base = float(np.clip(0.15 + 0.9 * np.mean(shares), 0.35, 0.85)) if shares else 0.62

    out = []
    for p in prelim:
        pos = p["pos"]; a, b = price_model[pos]
        cost = p["fpl"]["cost"] if p["fpl"] else 4.5
        anchor = a + b * np.log(cost)
        anchor = float(np.clip(anchor, 0.35 * POS_MEAN90[pos], 2.2 * POS_MEAN90[pos]))

        # Weight own history by how much of it there is
        if p["own_rate"] is None:
            rate = anchor
            w = 0.0
        else:
            w = p["minutes_prior"] / (p["minutes_prior"] + SHRINK_MINUTES)
            rate = w * p["own_rate"] + (1 - w) * anchor
        rate = max(rate, 0.5)

        # Availability: last season's minutes share, regressed toward the
        # baseline, then nudged by current FPL status.
        share = min(p["minutes_prior"] / (38 * 90), 1.0) if p["minutes_prior"] else None
        if share is None:
            p_play = avail_base
        else:
            own = float(np.clip(0.15 + 0.9 * share, 0.12, 0.95))
            p_play = AVAIL_PERSIST * own + (1 - AVAIL_PERSIST) * avail_base
        # Price-implied availability: rank within position, mapped to a band.
        if p["fpl"] and pos_cost_rank.get(pos):
            ranks = pos_cost_rank[pos]
            pct = ranks.get(round(cost, 1), 0.5)
            p_price = 0.55 + 0.35 * pct
            p_play = (1 - PRICE_AVAIL_WEIGHT) * p_play + PRICE_AVAIL_WEIGHT * p_price

        if p["fpl"]:
            st = p["fpl"]["status"]
            if st in ("i", "s", "u"):            # injured / suspended / unavailable
                p_play *= 0.35
            elif st == "d":                       # doubtful
                p_play *= 0.75
            ch = p["fpl"]["chance"]
            if ch is not None:
                p_play *= max(float(ch) / 100.0, 0.1)

        out.append({**{k: p[k] for k in
                       ("roster_id","manager","player_id","name","pos","club")},
                    "cost": cost, "rate90": rate, "own_weight": round(w, 2),
                    "p_play": p_play, "exp_week": rate * p_play,
                    "cv": POS_CV[pos]})
    return out


def simulate(conn, proj, n_sims: int, seed: int = 7):
    rng = np.random.default_rng(seed)
    rosters = sorted({p["roster_id"] for p in proj})
    mgr_of = {p["roster_id"]: p["manager"] for p in proj}
    by_roster = {rid: [p for p in proj if p["roster_id"] == rid] for rid in rosters}

    clubs = sorted({p["club"] or "?" for p in proj})
    club_ix = {c: i for i, c in enumerate(clubs)}

    schedule = defaultdict(list)
    for wk, a, b in conn.execute("""
        SELECT a.week, a.roster_id, b.roster_id FROM matchup_legs a
        JOIN matchup_legs b ON b.league_id=a.league_id AND b.season=a.season
         AND b.week=a.week AND b.matchup_id=a.matchup_id AND b.roster_id > a.roster_id
        WHERE a.league_id=? AND a.season=? ORDER BY a.week""", (LEAGUE_ID, SEASON)):
        schedule[wk].append((a, b))
    weeks = sorted(schedule)

    # Per-roster arrays, grouped by position
    packs = {}
    for rid in rosters:
        pack = {}
        for pos in ("GK", "F", "M", "D"):
            grp = [p for p in by_roster[rid] if p["pos"] == pos]
            pack[pos] = {
                "exp":   np.array([p["rate90"] for p in grp]),
                "cv":    np.array([p["cv"] for p in grp]),
                "pplay": np.array([p["p_play"] for p in grp]),
                "club":  np.array([club_ix[p["club"] or "?"] for p in grp]),
                "n": len(grp),
            }
        packs[rid] = pack

    # Mean reversion: a roster's expected level drifts toward the league
    # average across the season, standing in for waivers and trades. Strength
    # is measured on the best legal XI by expected points, not the whole squad.
    def xi_strength(rid):
        grp = defaultdict(list)
        for p in by_roster[rid]:
            grp[p["pos"]].append(p["exp_week"])
        for k in grp:
            grp[k].sort(reverse=True)
        total = sum(sum(grp[pos][:n]) for pos, n in BASE_SLOTS.items())
        used = {pos: min(n, len(grp[pos])) for pos, n in BASE_SLOTS.items()}
        best = 0.0
        for fa, mb, dc in FLEX_COMBOS:
            got = 0.0; ok = True
            for pos, extra in (("F", fa), ("M", mb), ("D", dc)):
                lo, hi = used[pos], used[pos] + extra
                if hi > len(grp[pos]):
                    ok = False; break
                got += sum(grp[pos][lo:hi])
            if ok:
                best = max(best, got)
        return total + best

    strength = {rid: xi_strength(rid) for rid in rosters}
    league_mean = sum(strength.values()) / len(strength)

    wins = np.zeros((len(rosters), n_sims))
    pf = np.zeros((len(rosters), n_sims))
    ridx = {rid: i for i, rid in enumerate(rosters)}

    n_weeks = max(weeks) if weeks else 1
    for wk in weeks:
        # fraction of the initial gap to average that has closed by this week
        closed = REVERSION_BY_SEASON_END * (wk - 1) / max(n_weeks - 1, 1)
        revert = {}
        for rid in rosters:
            s0 = strength[rid]
            target = league_mean + (s0 - league_mean) * (1 - closed)
            revert[rid] = (target / s0) if s0 > 0 else 1.0

        shock = rng.normal(0.0, CLUB_SHOCK, size=(n_sims, len(clubs)))
        shock = np.exp(shock - CLUB_SHOCK**2 / 2)     # mean-preserving

        scores = {}
        for rid in rosters:
            pack = packs[rid]
            realised, expected, avail = {}, {}, {}
            for pos, g in pack.items():
                if g["n"] == 0:
                    realised[pos] = np.zeros((n_sims, 0))
                    expected[pos] = np.zeros((n_sims, 0))
                    continue
                a = rng.random((n_sims, g["n"])) < g["pplay"]
                mu = g["exp"][None, :] * shock[:, g["club"]] * revert[rid]
                k = 1.0 / g["cv"][None, :] ** 2
                draw = rng.gamma(shape=np.broadcast_to(k, mu.shape),
                                 scale=mu / k)
                realised[pos] = np.where(a, draw, 0.0)
                # what the manager sees when picking: expected, if available
                expected[pos] = np.where(a, g["exp"][None, :], -1.0)
                avail[pos] = a

            # order each group by expected points, carry realised alongside
            cum_e, cum_r = {}, {}
            for pos in ("GK", "F", "M", "D"):
                e, r = expected[pos], realised[pos]
                if e.shape[1] == 0:
                    cum_e[pos] = np.zeros((n_sims, 1)); cum_r[pos] = np.zeros((n_sims, 1))
                    continue
                order = np.argsort(-e, axis=1)
                es = np.take_along_axis(e, order, axis=1)
                rs = np.take_along_axis(r, order, axis=1)
                cum_e[pos] = np.concatenate([np.zeros((n_sims,1)), np.cumsum(es,axis=1)],axis=1)
                cum_r[pos] = np.concatenate([np.zeros((n_sims,1)), np.cumsum(rs,axis=1)],axis=1)

            def take(pos, n):
                c_e, c_r = cum_e[pos], cum_r[pos]
                n = min(n, c_e.shape[1] - 1)
                return c_e[:, n], c_r[:, n], n

            base_e = np.zeros(n_sims); base_r = np.zeros(n_sims); used = {}
            for pos, n in BASE_SLOTS.items():
                e, r, k = take(pos, n)
                base_e += e; base_r += r; used[pos] = k

            # pick the flex filling that maximises EXPECTED points
            best_e = np.full(n_sims, -np.inf); best_r = np.zeros(n_sims)
            for (fa, mb, dc) in FLEX_COMBOS:
                ok = True
                tot_e = base_e.copy(); tot_r = base_r.copy()
                for pos, extra in (("F", fa), ("M", mb), ("D", dc)):
                    top = used[pos] + extra
                    if top > cum_e[pos].shape[1] - 1:
                        ok = False; break
                    tot_e += cum_e[pos][:, top] - cum_e[pos][:, used[pos]]
                    tot_r += cum_r[pos][:, top] - cum_r[pos][:, used[pos]]
                if not ok:
                    continue
                better = tot_e > best_e
                best_e = np.where(better, tot_e, best_e)
                best_r = np.where(better, tot_r, best_r)
            scores[rid] = np.where(np.isfinite(best_e), best_r, base_r)

        for a, b in schedule[wk]:
            sa, sb = scores[a], scores[b]
            wins[ridx[a]] += (sa > sb); wins[ridx[b]] += (sb > sa)
            wins[ridx[a]] += 0.5 * (sa == sb); wins[ridx[b]] += 0.5 * (sa == sb)
            pf[ridx[a]] += sa; pf[ridx[b]] += sb

    # Champion = best record, points-for breaks ties
    key = wins + pf * 1e-6
    champ = np.argmax(key, axis=0)
    titles = np.bincount(champ, minlength=len(rosters)) / n_sims
    order = np.argsort(-key, axis=0)
    finish = np.zeros((len(rosters), len(rosters)))
    for pos in range(len(rosters)):
        finish[:, pos] = np.bincount(order[pos], minlength=len(rosters)) / n_sims

    return [{
        "roster_id": rid, "manager": mgr_of[rid],
        "title_pct": float(titles[i]),
        "exp_wins": float(wins[i].mean()),
        "exp_pf": float(pf[i].mean()),
        "top3_pct": float(finish[i, :3].sum()),
        "last_pct": float(finish[i, -1]),
    } for i, rid in enumerate(rosters)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--projections", action="store_true", help="show inputs and exit")
    ap.add_argument("--refresh-fpl", action="store_true", help="re-pull FPL bootstrap")
    ap.add_argument("--write", action="store_true",
                    help="write power_rankings.json for the site build")
    a = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    if a.refresh_fpl:
        load_fpl(refresh=True)
    proj = build_projections(conn)

    if a.projections:
        print(f"{'player':26}{'pos':4}{'club':5}{'cost':>6}{'pts/90':>8}"
              f"{'p(play)':>9}{'exp/wk':>8}{'own w':>7}")
        for p in sorted(proj, key=lambda x: -x["exp_week"])[:30]:
            print(f"{p['name'][:25]:26}{p['pos']:4}{p['club'] or '?':5}"
                  f"{p['cost']:>6.1f}{p['rate90']:>8.1f}{p['p_play']:>9.2f}"
                  f"{p['exp_week']:>8.1f}{p['own_weight']:>7.2f}")
        return

    res = simulate(conn, proj, a.sims, a.seed)
    res.sort(key=lambda r: -r["title_pct"])
    print(f"\nDPL {SEASON}/{int(SEASON)+1} — {a.sims:,} simulated seasons")
    print("Title = best H2H record over 38 weeks, points-for breaks ties\n")
    print(f"{'manager':22}{'title%':>8}{'top3%':>8}{'wooden%':>9}{'E[wins]':>9}{'E[PF]':>9}")
    print("-" * 65)
    for r in res:
        print(f"{r['manager']:22}{100*r['title_pct']:>7.1f}%{100*r['top3_pct']:>7.1f}%"
              f"{100*r['last_pct']:>8.1f}%{r['exp_wins']:>9.1f}{r['exp_pf']:>9.0f}")
    spread = res[0]["title_pct"] - res[-1]["title_pct"]
    print(f"\nbest-to-worst title spread: {100*spread:.1f} points")

    if a.write:
        out = HERE / "power_rankings.json"
        out.write_text(json.dumps({
            "season": SEASON,
            "sims": a.sims,
            "params": {
                "avail_persist": AVAIL_PERSIST,
                "price_avail_weight": PRICE_AVAIL_WEIGHT,
                "reversion_by_season_end": REVERSION_BY_SEASON_END,
                "club_shock": CLUB_SHOCK,
                "shrink_minutes": SHRINK_MINUTES,
            },
            "managers": res,
        }, indent=2) + "\n")
        print(f"wrote {out.name}")


if __name__ == "__main__":
    main()
