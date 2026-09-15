#!/usr/bin/env python3
"""
Title race — per-gameweek title probability for every manager, as a series.

    python3 title_race.py                  # current season, writes title_race_2026.json
    python3 title_race.py --season 2025    # a completed season, for the History tab
    python3 title_race.py --sims 800       # more sims, slower, smoother lines

This is a different model from simulate.py and deliberately so. simulate.py
answers "who wins from here" for the Rankings page. This answers "how has the
title race looked all season", which needs a value for every week, including
weeks before any results existed.

How a player's weekly score is built
------------------------------------
* LEVEL comes from FPL, not from Sleeper's projections and not from price.
  Each player's FPL points are converted to DPL points by HIS OWN measured
  ratio -- avg DPL per week / avg FPL per week over the prior season. That
  ratio runs from about 1.0 to 2.9 and is the whole point of the exercise:
  FPL pays nothing for tackles, interceptions or key passes, so a Tielemans
  banks 2.85 DPL points per FPL point and a Konsa 1.01. A flat positional
  conversion would erase exactly the players this league rewards.
* SPREAD comes from the empirical band table. Players are sorted into deciles
  within position by scoring rate to date, and each decile has its own
  distribution of weekly outcomes measured off the prior season -- seven bands
  from major bust to major boom. A gamma cannot produce the bottom of that
  distribution at all: 10-12% of defender and keeper weeks score zero or less,
  and a gamma is strictly positive.
* The decile is RE-RANKED EVERY WEEK, so a player climbing into a higher tier
  gets that tier's shape from then on.

The shrink ramp
---------------
Early in a season the projection has nothing behind it, and taken at face value
it produces nonsense -- unshrunk, the 2025/26 replay had the eventual LAST
placed manager at 86% after two gameweeks. So every player's level is pulled
fully to his positional mean to start with, held there through SHRINK_HOLD, and
released linearly after that until the projection is trusted in full by the last
week. Banked results are never shrunk, which is why this costs nothing at the
sharp end: in the replay the eventual champion still crossed 50% at GW16 and hit
100% by GW36, while nobody exceeded 37% before GW10.

Lineups are best ball -- the highest-scoring legal XI of the week. That is a
choice about what the number means (how good is this roster, not how well is it
being managed) and it does widen the spread; see README.
"""
from __future__ import annotations
import argparse, json, re, sqlite3, sys, unicodedata, difflib
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import simulate as S

SHRINK_HOLD = 10      # weeks the full shrink is held before it starts decaying
PRIOR_FPL_W = 4.0     # gameweeks of prior-season FPL average carried early
BAND_EDGES  = [0, 5, 20, 40, 60, 80, 95, 100]     # the seven bands, in percentiles

# Latin letters NFKD will not decompose; without these, "Đorđe" becomes "ore".
_PRE = str.maketrans({'Đ':'D','đ':'d','Ø':'O','ø':'o','Ł':'L','ł':'l','ß':'ss',
                      'Æ':'AE','æ':'ae','Ð':'D','ð':'d'})
def _flat(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.translate(_PRE))
    return re.sub(r"[^a-z ]", "", s.encode("ascii", "ignore").decode().lower())
def _toks(s: str) -> set: return set(_flat(s).split())

POSMAP = {"GK": "GKP", "D": "DEF", "M": "MID", "F": "FWD"}


def dpl_scores(conn, season, scoring):
    """DPL points and minutes, per player per gameweek, under the live rules."""
    pts, mins = {}, {}
    for p, w, k, v in conn.execute(
        """SELECT player_id, week, stat_key, stat_value FROM player_stats
           WHERE season=? AND stat_key LIKE 'pos_%' AND stat_value IS NOT NULL""", (season,)):
        m = scoring.get(k, 0) or 0
        if m:
            pts[(p, w)] = pts.get((p, w), 0.0) + v * m
    for p, w, v in conn.execute(
        "SELECT player_id, week, stat_value FROM player_stats WHERE season=? AND stat_key='min'", (season,)):
        mins[(p, w)] = v or 0.0
    return pts, mins


def fpl_map(conn, season, meta):
    """Sleeper player_id -> FPL element for a season, by greedy best-first match."""
    cand = [(el, _toks(nm), _flat(nm), po) for el, nm, po in conn.execute(
        "SELECT DISTINCT element, name, position FROM fpl_prices WHERE season=?", (season,))]
    scored = []
    for pid, (nm, po) in meta.items():
        t, f = _toks(nm), _flat(nm)
        for el, ft, ffl, fpo in cand:
            ov = len(t & ft)
            if not ov:
                continue
            s = (ov * 3 + (2 if (t <= ft or ft <= t) else 0)
                 + (1.5 if POSMAP.get(po) == fpo else 0)
                 + 1.5 * difflib.SequenceMatcher(None, f, ffl).ratio())
            scored.append((s, pid, el))
    scored.sort(reverse=True)
    took_p, took_e, out = set(), set(), {}
    for s, pid, el in scored:
        if s < 4.5 or pid in took_p or el in took_e:
            continue
        took_p.add(pid); took_e.add(el); out[pid] = el
    return out


def band_pools(meta, pts, mins):
    """Per position, per decile, the pool of weekly multiples to sample from.

    Multiples are of the player's mean over ALL 38 gameweeks, not just the ones
    he played, so blanks sit inside the distribution and the level can be an
    expected-points-per-gameweek figure.
    """
    pools, ntier = {}, {}
    for pos in ("F", "M", "D", "GK"):
        pl = []
        for p, (nm, po) in meta.items():
            if po != pos:
                continue
            if sum(1 for w in range(1, 39) if mins.get((p, w), 0) > 0) < 10:
                continue
            allv = np.array([pts.get((p, w), 0.0) for w in range(1, 39)])
            if allv.mean() <= 0:
                continue
            pl.append((allv.sum(), allv, allv.mean()))
        pl.sort(key=lambda r: -r[0])
        n = len(pl)
        nt = 4 if pos == "GK" else 10          # 21 keepers will not carry deciles
        ntier[pos] = nt
        pools[pos] = [
            np.concatenate([x[1] / x[2] for x in
                            (pl[int(round(t * n / nt)):int(round((t + 1) * n / nt))] or pl[-1:])])
            for t in range(nt)]
    return pools, ntier


def run_week(w0, *, base_raw, pos, rid, rosters, ridx, rpos, posmask, pools, ntier,
             dpl_wk, team_real, fixtures, n_sims, rng, total_weeks):
    """Title probability as it stood at the end of gameweek w0."""
    P = len(base_raw)
    base = base_raw.copy()
    # full shrink held through SHRINK_HOLD, then released linearly
    extra = 1.0 if w0 <= SHRINK_HOLD else max(0.0, (total_weeks - w0) / (total_weeks - SHRINK_HOLD))
    keep = 1.0 - extra
    for q in ("F", "M", "D", "GK"):
        ix = posmask[q]
        if ix.size:
            mu = base[ix].mean()
            base[ix] = mu + (base[ix] - mu) * keep

    R = len(rosters)
    wins = np.zeros((n_sims, R)); pf = np.zeros((n_sims, R))
    for wk in range(1, w0 + 1):
        for a, b in fixtures.get(wk, []):
            if team_real[wk, a] > team_real[wk, b]: wins[:, a] += 1
            else:                                   wins[:, b] += 1
            pf[:, a] += team_real[wk, a]; pf[:, b] += team_real[wk, b]

    cum = np.tile(dpl_wk[1:w0 + 1].sum(axis=0), (n_sims, 1)).astype(float)
    ngw = max(float(w0), 1.0)
    for wk in range(w0 + 1, total_weeks + 1):
        tier = np.zeros((n_sims, P), dtype=int)
        for q, ix in posmask.items():
            if not ix.size: continue
            rate = cum[:, ix] / ngw
            o = np.argsort(-rate, axis=1, kind="stable")
            rk = np.empty_like(o)
            np.put_along_axis(rk, o, np.arange(ix.size)[None, :].repeat(n_sims, 0), axis=1)
            tier[:, ix] = np.minimum((rk * ntier[q]) // ix.size, ntier[q] - 1)
        mult = np.zeros((n_sims, P))
        for q, ix in posmask.items():
            if not ix.size: continue
            sub = np.zeros((n_sims, ix.size)); ts = tier[:, ix]
            for t in range(ntier[q]):
                sel = ts == t
                k = int(sel.sum())
                if k: sub[sel] = rng.choice(pools[q][t], size=k)
            mult[:, ix] = sub
        score = base[None, :] * mult
        cum += score; ngw += 1

        team = np.zeros((n_sims, R))
        for r in rosters:
            cs = {}
            for q in ("GK", "D", "M", "F"):
                ix = rpos[(r, q)]
                if not ix.size:
                    cs[q] = np.zeros((n_sims, 1)); continue
                s_ = -np.sort(-score[:, ix], axis=1)        # best ball
                cs[q] = np.concatenate([np.zeros((n_sims, 1)), np.cumsum(s_, axis=1)], axis=1)
            take = lambda q, n: cs[q][:, min(n, cs[q].shape[1] - 1)]
            best = None
            for eF, eM, eD in S.FLEX_COMBOS:
                tot = take("GK", 1) + take("F", 1 + eF) + take("M", 3 + eM) + take("D", 3 + eD)
                best = tot if best is None else np.maximum(best, tot)
            team[:, ridx[r]] = best
        for a, b in fixtures.get(wk, []):
            aw = team[:, a] > team[:, b]
            wins[:, a] += aw; wins[:, b] += ~aw
            pf[:, a] += team[:, a]; pf[:, b] += team[:, b]

    key = wins + pf * 1e-6
    return np.bincount(np.argmax(key, axis=1), minlength=R) / n_sims


def build(conn, season, n_sims, seed=7):
    prior = str(int(season) - 1)
    scoring = json.loads(conn.execute(
        "SELECT scoring_settings FROM league WHERE league_id=?", (S.LEAGUE_ID,)).fetchone()[0])
    meta = {r[0]: (r[1], r[2]) for r in
            conn.execute("SELECT player_id, full_name, position_primary FROM players")}
    league_id = conn.execute(
        "SELECT league_id FROM league WHERE season=?", (season,)).fetchone()
    if not league_id:
        raise SystemExit(f"no league row for season {season}")
    league_id = league_id[0]

    p_pts, p_min = dpl_scores(conn, prior, scoring)
    t_pts, t_min = dpl_scores(conn, season, scoring)
    pools, ntier = band_pools(meta, p_pts, p_min)
    map_p = fpl_map(conn, prior, meta)

    # conversion ratio: a player's own DPL-per-FPL, measured on the prior season
    p_fpl, p_fmin = {}, {}
    for el, gw, pts_, mn in conn.execute(
        "SELECT element, gw, total_points, minutes FROM fpl_prices WHERE season=?", (prior,)):
        p_fpl[(el, gw)] = pts_ or 0; p_fmin[(el, gw)] = mn or 0
    ratios = {}
    for pid, el in map_p.items():
        app = [w for w in range(1, 39) if p_min.get((pid, w), 0) > 0 and p_fmin.get((el, w), 0) > 0]
        if len(app) < 10: continue
        d = float(np.mean([p_pts.get((pid, w), 0.0) for w in app]))
        f = float(np.mean([p_fpl.get((el, w), 0) for w in app]))
        if f > 0.5 and d > 0: ratios[pid] = d / f
    posmed = {}
    for po in ("F", "M", "D", "GK"):
        v = [r for p, r in ratios.items() if meta[p][1] == po]
        posmed[po] = float(np.median(v)) if v else 1.9

    rows = list(conn.execute(
        "SELECT r.roster_id, j.value FROM rosters r, json_each(r.players) j WHERE r.league_id=?",
        (league_id,)))
    players = [{"pid": p, "rid": r, "pos": meta[p][1]}
               for r, p in rows if p in meta and meta[p][1] in ("F", "M", "D", "GK")]
    P = len(players)
    pidl = [x["pid"] for x in players]
    pos = np.array([x["pos"] for x in players]); rid = np.array([x["rid"] for x in players])
    ratio = np.array([ratios.get(p, posmed[meta[p][1]]) for p in pidl])

    map_t = fpl_map(conn, season, meta)
    t_fpl = {}
    for el, gw, pts_, mn in conn.execute(
        "SELECT element, gw, total_points, minutes FROM fpl_prices WHERE season=?", (season,)):
        t_fpl[(el, gw)] = pts_ or 0
    prior_fpl = np.array([
        float(np.mean([p_fpl.get((map_p[p], w), 0) for w in range(1, 39)])) if p in map_p else 0.0
        for p in pidl])

    total_weeks = conn.execute(
        "SELECT MAX(week) FROM v_matchup_results WHERE season=?", (season,)).fetchone()[0] or 38
    fpl_wk = np.zeros((total_weeks + 1, P)); dpl_wk = np.zeros((total_weeks + 1, P))
    for i, p in enumerate(pidl):
        el = map_t.get(p)
        for w in range(1, total_weeks + 1):
            if el is not None: fpl_wk[w, i] = t_fpl.get((el, w), 0)
            dpl_wk[w, i] = t_pts.get((p, w), 0.0)

    rosters = sorted(set(rid.tolist())); R = len(rosters)
    ridx = {r: i for i, r in enumerate(rosters)}
    fixtures, played = {}, set()
    for wk, a, b, pa in conn.execute(
        """SELECT week, roster_id_a, roster_id_b, points_a FROM v_matchup_results
           WHERE season=? ORDER BY week""", (season,)):
        if a in ridx and b in ridx:
            fixtures.setdefault(wk, []).append((ridx[a], ridx[b]))
            if pa is not None: played.add(wk)
    team_real = np.zeros((total_weeks + 1, R))
    for wk, r, p_ in conn.execute(
        """SELECT week, roster_id, COALESCE(custom_points, points) FROM matchup_legs
           WHERE season=? AND points IS NOT NULL""", (season,)):
        if r in ridx and wk <= total_weeks: team_real[wk, ridx[r]] = p_
    weeks_done = max(played) if played else 0

    posmask = {q: np.where(pos == q)[0] for q in ("GK", "D", "M", "F")}
    rpos = {(r, q): np.where((rid == r) & (pos == q))[0] for r in rosters for q in ("GK", "D", "M", "F")}
    names = {}
    for r, tn, dn in conn.execute(
        """SELECT r.roster_id, NULLIF(u.team_name,''), u.display_name FROM rosters r
           LEFT JOIN league_users u ON u.league_id=r.league_id AND u.user_id=r.owner_id
           WHERE r.league_id=?""", (league_id,)):
        if r in ridx: names[ridx[r]] = {"manager": dn, "team": tn or dn, "roster_id": r}

    rng = np.random.default_rng(seed)
    series = {}
    for w0 in range(0, weeks_done + 1):
        n = float(w0)
        fplavg = (fpl_wk[1:w0 + 1].sum(axis=0) + PRIOR_FPL_W * prior_fpl) / (n + PRIOR_FPL_W)
        base_raw = np.maximum(fplavg * ratio, 0.05)
        t = run_week(w0, base_raw=base_raw, pos=pos, rid=rid, rosters=rosters, ridx=ridx,
                     rpos=rpos, posmask=posmask, pools=pools, ntier=ntier, dpl_wk=dpl_wk,
                     team_real=team_real, fixtures=fixtures, n_sims=n_sims, rng=rng,
                     total_weeks=total_weeks)
        series[w0] = [round(float(x), 4) for x in t]
        print(f"  GW{w0:>2}: leader {names[int(np.argmax(t))]['manager']} {100*max(t):.0f}%", flush=True)

    wins = np.zeros(R)
    for wk in sorted(played):
        for a, b in fixtures.get(wk, []):
            if team_real[wk, a] > team_real[wk, b]: wins[a] += 1
            else:                                   wins[b] += 1
    return {"season": season, "sims": n_sims, "weeks_done": weeks_done,
            "total_weeks": total_weeks, "shrink_hold": SHRINK_HOLD,
            "managers": [{**names[i], "wins": int(wins[i])} for i in range(R)],
            "series": {str(k): v for k, v in series.items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default=S.SEASON)
    ap.add_argument("--sims", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    conn = sqlite3.connect(S.DB_PATH)
    print(f"Title race — season {a.season}, {a.sims} sims per gameweek")
    data = build(conn, a.season, a.sims, a.seed)
    out = Path(a.out) if a.out else HERE / f"title_race_{a.season}.json"
    out.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {out.name}: {data['weeks_done']+1} points x {len(data['managers'])} managers")


if __name__ == "__main__":
    main()
