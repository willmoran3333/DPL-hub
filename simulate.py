#!/usr/bin/env python3
"""
DPL season simulator — correlated Monte Carlo over the real 38-week schedule.

    python3 simulate.py --sims 5000
    python3 simulate.py --projections      # just show the projection inputs

Title is decided by best H2H record (no playoffs), points-for breaking ties.

Weeks that have already been scored are NOT re-simulated: their real W/L and
points-for are banked into every simulated season, and only the remaining
fixtures are drawn. A manager at 10-2 carries those wins into their title
probability. Early in a season this changes little; by the spring it is most
of the signal.

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
# Re-measured from 2025/26 stats re-scored under 2026/27 rules, players with
# 900+ minutes. The old midfield and forward means (7.5, 8.6) were low against
# the 7.98 and 9.66 the rebalanced scoring actually produces, which is most of
# why the model ran about 8% light on team totals.
# The raw weekly CV of a player who featured measures about 0.95, but that
# number already contains cameos and rotation, which the model handles
# separately through p_play. Using it as-is double-counts and put team-level
# weekly SD at 27 against a real 22.5. These are that measurement scaled to
# reproduce the observed team-level spread, which is the quantity that matters.
POS_CV = {"GK": 0.80, "D": 0.79, "M": 0.74, "F": 0.77}
POS_MEAN90 = {"GK": 6.4, "D": 6.7, "M": 8.0, "F": 9.7}
CLUB_SHOCK = 0.16      # sd of the shared per-club, per-week log shock
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

# ...but that drift is not equally available to everyone. A manager with half
# the league's waiver budget left cannot buy the same improvement as one
# carrying 50% more, and budget moves in trades: skoeiboy took $50 off
# Flabiano9 in GW1, leaving 146 against a 96 base and Flabiano9 on 46.
# Reversion is therefore tilted by how much budget a roster has left relative
# to the league. At 0.10 a +52% budget edge is worth about +5% squad strength
# by GW38 — roughly enough to buy your way from this roster's position to
# league average, and symmetric, so a depleted budget is marked down the same.
# The tilt ramps with the season: budget buys nothing until it is spent.
BUDGET_TILT = 0.10

# How wrong the projection itself can be, as a per-season multiplier on a
# roster's strength. This is NOT weekly noise — it is drawn once per simulated
# season and held all year, because it stands for "we do not actually know how
# good this squad is."
#
# Measured, not guessed. Across the eleven managers who played both 2024/25 and
# 2025/26, the correlation between their mean weekly score in one season and
# the next is r = +0.16 — r-squared of 0.02. willmoran went 107.6 to 81.2,
# skoeiboy 66.8 to 80.3, erozier 78.1 to 91.9 and won it. Of the 7.0-point
# spread in 2025/26 team strength, 1.1 was predictable from the year before and
# 6.9 was not. On a league mean near 81 that residual is about 8.5%.
#
# Tuned together with POS_CV and CLUB_SHOCK against three targets measured off
# real seasons — league mean weekly score 81-88, within-manager weekly SD
# 22.2-22.7, between-manager season spread 6.7-10.3. The settled trio lands at
# 81.2 / 22.5 / 9.4. They interact, so re-tune them in one pass, not singly.
STRENGTH_UNCERTAINTY = 0.10

# Week-to-week spread of a team's score on a log scale — the noise the update
# above has to see through. Real seasons put the within-manager weekly SD at
# 22.2 and 22.7 on a mean near 81, so roughly 0.27 in log terms.
WEEKLY_LOG_SD = 0.27

# The update is winsorised at this many weekly SDs. GW1 of 2026/27 contains a
# 35.65 — 3.2 SDs below the league mean, which is a lineup nobody set rather
# than a squad three sigma worse than the field. Taken at face value it cut
# that roster's title chance to 0.4%. Real team strength never sits two weekly
# SDs off its projection (the observed season spread is a quarter of that), so
# the cap only ever bites on a blow-up, and it bites less as weeks accumulate
# and the mean stops being one bad Saturday.
ROBUST_CLIP = 2.0

# How much of a roster's projected gap to the league average we actually
# believe. The projection is built from last season's per-90 rates and an FPL
# price anchor; it knows the squad, but it cannot see waivers, trades, lineup
# calls or a manager paying attention in March. Left at 1.0 the model asserts a
# 17.7 points-per-week gap between best and worst as SETTLED fact while letting
# a manager's season level wobble only 4.9 — roughly three and a half sigma of
# certainty about the ordering, which is why the bottom two rounded to zero
# after a single gameweek.
#
# Held at 0.70: the projection is backtested and decent
# (R^2 = 0.48 on points per 90), so most of the gap it reports is real. What it
# cannot see is in-season management, and STRENGTH_UNCERTAINTY carries that.
# Between them the model now reproduces the spread of real seasons while
# admitting it does not know which team is which in August — which is the
# honest position after one gameweek, when GW1 rank correlates with final rank
# at only +0.22, and the reigning champion started 8th of 12.
PROJECTION_SHRINK = 0.70

# This season's own projections, which is where the rate should mostly come
# from. Backtested on 2025/26, where we hold all 38 weeks of Sleeper's
# projections alongside what actually happened:
#
#   Sleeper projection -> points per 90 played   R^2 = 0.48
#   last season's per-90 -> same                 R^2 = 0.21
#   both together                                R^2 = 0.64 vs 0.635 for the
#                                                projection alone
#
# Prior-season rate adds +0.004 once the projection is in, and a regression of
# actual on both splits 93/7. So the projection carries the rate and last
# season is a garnish — which is what the year-over-year correlation of team
# strength (r = +0.16) should have told us anyway.
PROJ_WEIGHT = 0.93
# Sleeper projects every player at 95 minutes and runs hot on the rate: by
# position the ratio of actual per-90 to projection is GK 1.03, D 0.92, F 0.86,
# M 0.81. This is the pooled fit, rate90 = A + B * projection.
#
# Only the DISPERSION is portable, not the level. Scoring settings are
# re-tuned between seasons — 2026/27 moved 24 keys — and that shifts the whole
# projection scale: the same Sleeper projections score 8.45 on average under
# 2025/26 rules and 4.77 under 2026/27's, 44% lower. A fixed intercept and
# slope fitted on one season therefore collapses on the next, which is exactly
# what happened: a starting XI came out at 41 points a week against a league
# that actually scores about 88.
#
# So the level is anchored to POS_MEAN90, the league's own measured per-90
# means, and the projection supplies only relative standing within a position.
# The fit's slope of 0.746 against a mean ratio of 0.866 says projections are
# over-dispersed by about 0.86; that correction IS scale-free and is kept.
PROJ_DISPERSION = 0.86
WAIVER_BUDGET_BASE = 96   # league setting; overridden from the DB when present


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

    # This season's projections, latest week Sleeper has published. Covers
    # about two thirds of rostered players — the rest fall back to the price
    # anchor and last season's rate below.
    proj_week = conn.execute(
        "SELECT MAX(week) FROM player_projections WHERE season=?", (SEASON,)).fetchone()[0]
    sleeper_proj: dict[str, float] = {}
    if proj_week:
        for pid, key, v in conn.execute("""
            SELECT player_id, stat_key, stat_value FROM player_projections
            WHERE season=? AND week=? AND stat_key LIKE 'pos_%'
              AND stat_value IS NOT NULL""", (SEASON, proj_week)):
            m = scoring.get(key, 0) or 0
            if m:
                sleeper_proj[pid] = sleeper_proj.get(pid, 0.0) + v * m

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

    # Price -> projection, per position, fitted on rostered players who have a
    # projection. Used to impute one for those who do not, so every player sits
    # on one scale before being converted to a per-90 rate.
    proj_from_price = {}
    for pos in POS_CV:
        xs, ys = [], []
        for q in prelim:
            if q["pos"] == pos and q["fpl"] and q["player_id"] in sleeper_proj:
                xs.append(np.log(q["fpl"]["cost"])); ys.append(sleeper_proj[q["player_id"]])
        if len(xs) >= 8:
            bb, aa = np.polyfit(xs, ys, 1)
            proj_from_price[pos] = (aa, bb, float(np.mean(ys)))
        else:
            proj_from_price[pos] = (None, None, float(np.mean(ys)) if ys else 6.0)

    # Positional mean of the projections actually in play, which is what the
    # level gets anchored against.
    proj_mean = {}
    for pos in POS_CV:
        vals = [sleeper_proj[q["player_id"]] for q in prelim
                if q["pos"] == pos and q["player_id"] in sleeper_proj]
        proj_mean[pos] = float(np.mean(vals)) if vals else None

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

        # Where this season's projection exists it takes the rate over almost
        # entirely; what survives of `rate` is the 7% of last-season-and-price
        # the backtest says is still worth carrying.
        sp = sleeper_proj.get(p["player_id"])
        imputed = False
        if sp is None:
            aa, bb, mean_p = proj_from_price[pos]
            sp = (aa + bb * np.log(cost)) if aa is not None else mean_p
            # Keep an imputed projection inside the range Sleeper actually
            # publishes for the position; extrapolating off a price alone is
            # how a fringe forward ends up projected above Haaland.
            sp = float(np.clip(sp, 0.35 * mean_p, 1.6 * mean_p))
            imputed = True
        pm = proj_mean.get(pos)
        if pm:
            # Relative standing within the position, dispersion-corrected, then
            # put back on the league's own per-90 scale.
            rel = 1.0 + PROJ_DISPERSION * (sp / pm - 1.0)
            rate = (PROJ_WEIGHT * POS_MEAN90[pos] * rel) + (1 - PROJ_WEIGHT) * rate
        rate = max(rate, 0.5)
        p["imputed"] = imputed

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
                    "has_proj": p["player_id"] in sleeper_proj,
                    "imputed": p.get("imputed", False),
                    "cv": POS_CV[pos]})
    return out


# Set to a list to have simulate() record (week, roster_id, scores) so the
# model's own skill/luck split can be measured against real seasons.
DIAG_SCORES = None


def load_budgets(conn) -> dict:
    """Remaining waiver budget per roster, as a share of the league average.

    Sleeper stores spend as `waiver_budget_used` in rosters.settings, and it
    goes NEGATIVE when a manager receives budget in a trade. Returns
    roster_id -> edge, where 0.0 is an average purse and +0.5 is half as much
    again. Empty dict if the league has no budget settings, which leaves the
    reversion untilted.
    """
    row = conn.execute("SELECT settings FROM league WHERE league_id=?",
                       (LEAGUE_ID,)).fetchone()
    base = WAIVER_BUDGET_BASE
    if row and row[0]:
        try:
            base = json.loads(row[0]).get("waiver_budget", base) or base
        except json.JSONDecodeError:
            pass
    left = {}
    for rid, settings in conn.execute(
            "SELECT roster_id, settings FROM rosters WHERE league_id=?", (LEAGUE_ID,)):
        try:
            used = (json.loads(settings or "{}") or {}).get("waiver_budget_used", 0) or 0
        except json.JSONDecodeError:
            used = 0
        left[rid] = max(base - used, 0.0)
    if not left:
        return {}
    mean = sum(left.values()) / len(left)
    if mean <= 0:
        return {}
    return {rid: v / mean - 1.0 for rid, v in left.items()}


def load_schedule(conn, as_of: int | None = None):
    """Split the season's fixtures into results already banked and games left.

    as_of caps which weeks count as played, so the season can be replayed as it
    stood at the end of any given week. That is what makes the title-probability
    history reconstructable after the fact instead of only accumulating from now
    on.

    A leg counts as played when its points is not NULL — the same test
    make_history.py uses. The effective score is COALESCE(custom_points,
    points) so commissioner adjustments propagate, matching build_site.py.
    A pair where both sides scored exactly 0 is treated as unplayed, as
    build_site.get_h2h_matrix does; a genuine 0-0 is not a thing here, and
    such rows are stale placeholders.

    Returns (pending, seed, weeks, weeks_played):
      pending[wk]  list of (roster_a, roster_b) still to be simulated
      seed[rid]    {"w","l","t","pf"} banked from completed matchups
      weeks        every week in the schedule, in order
      weeks_played count of weeks with at least one completed matchup
    """
    pending = defaultdict(list)
    seed: dict[int, dict] = {}
    weeks, done_weeks = set(), set()

    def rec(rid):
        return seed.setdefault(rid, {"w": 0.0, "l": 0.0, "t": 0.0, "pf": 0.0})

    for wk, ra, rb, pa, pb in conn.execute("""
        SELECT a.week, a.roster_id, b.roster_id,
               COALESCE(a.custom_points, a.points) AS pa,
               COALESCE(b.custom_points, b.points) AS pb
        FROM matchup_legs a
        JOIN matchup_legs b ON b.league_id=a.league_id AND b.season=a.season
         AND b.week=a.week AND b.matchup_id=a.matchup_id AND b.roster_id > a.roster_id
        WHERE a.league_id=? AND a.season=? ORDER BY a.week""", (LEAGUE_ID, SEASON)):
        weeks.add(wk)
        if as_of is not None and wk > as_of:
            pending[wk].append((ra, rb))
            continue
        if pa is None or pb is None or (pa == 0 and pb == 0):
            pending[wk].append((ra, rb))
            continue
        done_weeks.add(wk)
        a_, b_ = rec(ra), rec(rb)
        a_["pf"] += pa; b_["pf"] += pb
        if pa > pb:
            a_["w"] += 1; b_["l"] += 1
        elif pb > pa:
            b_["w"] += 1; a_["l"] += 1
        else:                      # ties count half a win each, as in simulate()
            a_["t"] += 1; b_["t"] += 1

    return pending, seed, sorted(weeks), len(done_weeks)


def draw_season_strength(rng, n_sims, rosters, ridx, believed, banked, weeks_played):
    """Per-season strength multiplier, conditioned on what each roster has scored.

    The projection is a guess and STRENGTH_UNCERTAINTY says how wrong it might
    be. Points already on the board are evidence about that, so the multiplier
    is drawn from the posterior rather than the prior — otherwise a manager can
    post 130 a week all autumn and the model still rates their squad exactly
    where it did in August, which is what made a hot start move nothing.

    Everything is measured in RELATIVE terms: a roster's share of the league's
    scoring against the share its projection implied. That keeps a global level
    error — the model runs a little light — out of the per-team update.

    Normal-normal conjugate update on log strength. The prior carries precision
    1/sigma^2; each played week adds 1/tau^2 where tau is the weekly spread on
    a log scale, so one week barely moves the estimate and ten weeks dominate
    it. That is the right shape: a single gameweek is mostly noise.
    """
    n = len(rosters)
    prior_mu = -STRENGTH_UNCERTAINTY**2 / 2
    mu = np.full(n, prior_mu)
    sd = np.full(n, STRENGTH_UNCERTAINTY)

    played = [r for r in rosters if banked.get(r, {}).get("pf", 0) > 0]
    if weeks_played and played:
        games = {r: (banked[r]["w"] + banked[r]["l"] + banked[r]["t"]) for r in played}
        rate = {r: banked[r]["pf"] / games[r] for r in played if games[r]}
        if rate:
            obs_mean = float(np.mean(list(rate.values())))
            proj_mean = float(np.mean([believed[r] for r in rate]))
            if obs_mean > 0 and proj_mean > 0:
                prior_prec = 1.0 / STRENGTH_UNCERTAINTY**2
                for r, obs in rate.items():
                    if obs <= 0 or believed[r] <= 0:
                        continue
                    # how far this roster beat its projected share of the league
                    resid = np.log(obs / obs_mean) - np.log(believed[r] / proj_mean)
                    lim = ROBUST_CLIP * WEEKLY_LOG_SD
                    resid = float(np.clip(resid, -lim, lim))
                    k = games[r]
                    like_prec = k / WEEKLY_LOG_SD**2
                    post_prec = prior_prec + like_prec
                    i = ridx[r]
                    mu[i] = (prior_mu * prior_prec + resid * like_prec) / post_prec
                    sd[i] = np.sqrt(1.0 / post_prec)
    return np.exp(rng.normal(mu, sd, size=(n_sims, n)))


def simulate(conn, proj, n_sims: int, seed: int = 7, as_of: int | None = None):
    rng = np.random.default_rng(seed)
    rosters = sorted({p["roster_id"] for p in proj})
    mgr_of = {p["roster_id"]: p["manager"] for p in proj}
    by_roster = {rid: [p for p in proj if p["roster_id"] == rid] for rid in rosters}

    clubs = sorted({p["club"] or "?" for p in proj})
    club_ix = {c: i for i, c in enumerate(clubs)}

    pending, banked, weeks, weeks_played = load_schedule(conn, as_of)
    budget_edge = load_budgets(conn)

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
    # What we actually believe about each roster, as opposed to what the point
    # estimate says. mu is built from the unshrunk player expectations, so the
    # reversion factor below maps raw -> believed.
    believed = {rid: league_mean + (v - league_mean) * PROJECTION_SHRINK
                for rid, v in strength.items()}

    ridx = {rid: i for i, rid in enumerate(rosters)}
    wins = np.zeros((len(rosters), n_sims))
    pf = np.zeros((len(rosters), n_sims))

    # Every simulated season starts from what has actually happened.
    for rid, r in banked.items():
        if rid in ridx:
            wins[ridx[rid]] = r["w"] + 0.5 * r["t"]
            pf[ridx[rid]] = r["pf"]

    # One draw per roster per season, held for the whole year: how good this
    # squad actually turns out to be, versus what we projected — updated by
    # whatever the roster has actually scored so far.
    season_strength = draw_season_strength(
        rng, n_sims, rosters, ridx, believed, banked, weeks_played)

    n_weeks = max(weeks) if weeks else 1
    for wk in weeks:
        fixtures = pending.get(wk)
        if not fixtures:
            continue                  # week already in the books
        # only these rosters need a score drawn this week
        need = sorted({r for pair in fixtures for r in pair})

        # fraction of the initial gap to average that has closed by this week
        ramp = (wk - 1) / max(n_weeks - 1, 1)
        closed = REVERSION_BY_SEASON_END * ramp
        revert = {}
        for rid in need:
            s0 = strength[rid]
            target = league_mean + (believed[rid] - league_mean) * (1 - closed)
            # Budget tilt, ramped the same way: an unspent purse is only worth
            # something once there has been a season in which to spend it.
            target *= 1.0 + BUDGET_TILT * ramp * budget_edge.get(rid, 0.0)
            revert[rid] = (target / s0) if s0 > 0 else 1.0

            season_mult = season_strength[:, [ridx[r] for r in need]]

        shock = rng.normal(0.0, CLUB_SHOCK, size=(n_sims, len(clubs)))
        shock = np.exp(shock - CLUB_SHOCK**2 / 2)     # mean-preserving

        scores = {}
        for slot_i, rid in enumerate(need):
            pack = packs[rid]
            mine = season_mult[:, slot_i][:, None]   # this season's true level
            realised, expected, avail = {}, {}, {}
            for pos, g in pack.items():
                if g["n"] == 0:
                    realised[pos] = np.zeros((n_sims, 0))
                    expected[pos] = np.zeros((n_sims, 0))
                    continue
                a = rng.random((n_sims, g["n"])) < g["pplay"]
                mu = g["exp"][None, :] * shock[:, g["club"]] * revert[rid] * mine
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

        if DIAG_SCORES is not None:
            for rid, sc in scores.items():
                DIAG_SCORES.append((wk, rid, sc))

        for a, b in fixtures:
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

    res = [{
        "roster_id": rid, "manager": mgr_of[rid],
        "title_pct": float(titles[i]),
        "exp_wins": float(wins[i].mean()),
        "exp_pf": float(pf[i].mean()),
        "top3_pct": float(finish[i, :3].sum()),
        "last_pct": float(finish[i, -1]),
        # Results already in the books. Named banked_*, not actual_*:
        # build_site.load_power_rankings writes its own actual_wins from the
        # live standings and would otherwise overwrite these.
        "banked_wins": banked.get(rid, {}).get("w", 0.0),
        "banked_losses": banked.get(rid, {}).get("l", 0.0),
        "banked_ties": banked.get(rid, {}).get("t", 0.0),
        "banked_pf": round(banked.get(rid, {}).get("pf", 0.0), 2),
    } for i, rid in enumerate(rosters)]
    return res, weeks_played, len(weeks)


def build_history(conn, proj, n_sims: int, seed: int, rebuild: bool = False):
    """Extend the title-probability series with any week it does not yet hold.

    Week 0 is the pre-season projection; week N conditions on results through
    N. Snapshots already in the file are kept exactly as they were written —
    they were published, and the change column has to measure this week's move
    against the number managers actually saw last week, not against a figure
    revised afterwards.

    That does mean a point carries the rosters and price anchor of the week it
    was taken, so a move is not purely the effect of results. Recomputing the
    whole series instead (--rebuild-history) buys that purity at the cost of
    silently editing published history; the trade is not worth it in-season.
    Use it only when the series is corrupt or the model itself has changed.
    """
    _, _, weeks, weeks_played = load_schedule(conn)
    out = HERE / "power_rankings_history.json"

    existing = {}
    if out.exists() and not rebuild:
        try:
            for snap in json.loads(out.read_text()).get("snapshots", []):
                existing[snap["week"]] = snap
        except (json.JSONDecodeError, OSError):
            existing = {}

    snaps = []
    for wk in range(0, weeks_played + 1):
        if wk in existing:
            snaps.append(existing[wk])
            top = max(existing[wk]["title_pct"].items(), key=lambda kv: kv[1])
            print(f"  week {wk:>2}: kept as published, leader roster {top[0]} "
                  f"{100*top[1]:.1f}%")
            continue
        res, _, _ = simulate(conn, proj, n_sims, seed, as_of=wk)
        snaps.append({
            "week": wk,
            "title_pct": {str(r["roster_id"]): round(r["title_pct"], 5) for r in res},
            "exp_wins": {str(r["roster_id"]): round(r["exp_wins"], 2) for r in res},
        })
        top = max(res, key=lambda r: r["title_pct"])
        print(f"  week {wk:>2}: {len(res)} managers, leader {top['manager']} "
              f"{100*top['title_pct']:.1f}%")

    fresh = sum(1 for snap in snaps if snap["week"] not in existing)
    out.write_text(json.dumps({
        "season": SEASON, "sims": n_sims,
        "weeks_total": len(weeks), "weeks_played": weeks_played,
        "snapshots": snaps,
    }, indent=2) + "\n")
    print(f"wrote {out.name}: {len(snaps)} snapshots "
          f"({fresh} new, {len(snaps)-fresh} kept; week 0 = pre-season)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--projections", action="store_true", help="show inputs and exit")
    ap.add_argument("--refresh-fpl", action="store_true", help="re-pull FPL bootstrap")
    ap.add_argument("--write", action="store_true",
                    help="write power_rankings.json for the site build")
    ap.add_argument("--as-of", type=int, default=None, metavar="WEEK",
                    help="replay the season as it stood after WEEK (0 = pre-season)")
    ap.add_argument("--history", action="store_true",
                    help="extend power_rankings_history.json with any week it does "
                         "not yet hold; snapshots already written are kept as published")
    ap.add_argument("--rebuild-history", action="store_true",
                    help="with --history, recompute every week from scratch, "
                         "overwriting snapshots that were already published")
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

    if a.history:
        build_history(conn, proj, a.sims, a.seed, a.rebuild_history)
        return

    res, weeks_played, n_weeks = simulate(conn, proj, a.sims, a.seed, a.as_of)
    res.sort(key=lambda r: -r["title_pct"])
    print(f"\nDPL {SEASON}/{int(SEASON)+1} — {a.sims:,} simulated seasons")
    print(f"Title = best H2H record over {n_weeks} weeks, points-for breaks ties")
    if weeks_played:
        print(f"Conditioned on {weeks_played} completed week"
              f"{'' if weeks_played == 1 else 's'}; "
              f"{n_weeks - weeks_played} simulated per season\n")
    else:
        print("No completed weeks yet — the whole season is simulated\n")
    print(f"{'manager':22}{'now':>7}{'title%':>8}{'top3%':>8}{'wooden%':>9}"
          f"{'E[wins]':>9}{'E[PF]':>9}")
    print("-" * 72)
    for r in res:
        now = f"{r['banked_wins']:.0f}-{r['banked_losses']:.0f}" + (
            f"-{r['banked_ties']:.0f}" if r["banked_ties"] else "")
        print(f"{r['manager']:22}{now:>7}{100*r['title_pct']:>7.1f}%"
              f"{100*r['top3_pct']:>7.1f}%"
              f"{100*r['last_pct']:>8.1f}%{r['exp_wins']:>9.1f}{r['exp_pf']:>9.0f}")
    spread = res[0]["title_pct"] - res[-1]["title_pct"]
    print(f"\nbest-to-worst title spread: {100*spread:.1f} points")

    if a.write:
        out = HERE / "power_rankings.json"
        out.write_text(json.dumps({
            "season": SEASON,
            "sims": a.sims,
            "weeks_total": n_weeks,
            "weeks_played": weeks_played,
            "weeks_simulated": n_weeks - weeks_played,
            "params": {
                "avail_persist": AVAIL_PERSIST,
                "price_avail_weight": PRICE_AVAIL_WEIGHT,
                "reversion_by_season_end": REVERSION_BY_SEASON_END,
                "budget_tilt": BUDGET_TILT,
                "strength_uncertainty": STRENGTH_UNCERTAINTY,
                "projection_shrink": PROJECTION_SHRINK,
                "proj_weight": PROJ_WEIGHT,
                "proj_dispersion": PROJ_DISPERSION,
                "club_shock": CLUB_SHOCK,
                "shrink_minutes": SHRINK_MINUTES,
            },
            "managers": res,
        }, indent=2) + "\n")
        print(f"wrote {out.name}")


if __name__ == "__main__":
    main()
