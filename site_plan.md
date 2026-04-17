# DPL Public Site — Build Plan

Goal: A public, weekly-updating HTML website for the DPL (Derby Premier League? Dallas Premier League?) — Will's 12-team Sleeper EPL fantasy league. Styled after [premierleague.com](https://www.premierleague.com) so each fantasy team looks like a real Premier League club.

---

## 1 · Architecture

### Recommended stack
| Layer | Choice | Why |
|---|---|---|
| Hosting | **Cloudflare Pages** (free tier) | Fast global CDN, generous free tier, good custom-domain support, no bandwidth cap |
| Build | **Python + Jinja2 → static HTML** | Reuses existing `ingest.py` pipeline; no framework bloat; full control over markup so it won't look AI-templated |
| Styling | **Hand-rolled CSS** (no Tailwind) with a proper design token system | The "doesn't look AI-made" requirement specifically argues against utility-class frameworks — they produce recognizably AI-ish markup |
| Interactivity | Vanilla JS + tiny `players.json` fetched client-side for sortable/searchable player table | Keep bundle near-zero |
| Scheduling | **GitHub Actions cron** (Tuesday 14:00 UTC = after Monday Night Football-equivalent) | Free, reliable, emails on failure |
| Source control | GitHub public or private repo | Cloudflare Pages auto-deploys on push |

### Alternative (if you'd rather): **Astro on Cloudflare Pages**
- Pros: component model, built-in asset optimization, can co-locate Markdown recaps
- Cons: introduces JS tooling; slightly more "templated" look unless we fight it

**My recommendation: go with Python + Jinja2**. The site is fundamentally a data-rendering site. Python already has the DB and the scraper. Adding a Node toolchain just to render HTML is friction we don't need.

### Weekly update pipeline
```
Tuesday 14:00 UTC (GitHub Actions cron)
  ├─ checkout repo
  ├─ python3 ingest.py                 (pulls latest Sleeper data into dpl.db)
  ├─ python3 build_site.py             (renders Jinja2 templates → /dist)
  ├─ commit updated dpl.db + /dist back to repo (or skip commit, just deploy)
  └─ Cloudflare Pages picks up the push → deploys
```
Also wire a `workflow_dispatch` trigger so you can hit "Run now" after a midweek game.

### File layout (proposed)
```
DPL Project/
├── dpl.db                          # SQLite (committed — small, ~15 MB)
├── schema.sql
├── ingest.py                       # existing
├── build_site.py                   # NEW — queries DB, renders templates
├── team_mapping.yml                # NEW — fantasy team → real PL club
├── requirements.txt
├── templates/                      # Jinja2
│   ├── _base.html
│   ├── _partials/                  # nav, crest, standings-row, player-row, etc.
│   ├── index.html                  # home / current standings
│   ├── gameweek.html               # every GW recap (one page per GW)
│   ├── teams/
│   │   └── team.html               # per-fantasy-team page
│   ├── players.html
│   ├── fixtures.html               # look-ahead
│   └── history.html                # last season
├── static/
│   ├── css/
│   │   └── main.css                # hand-written, token-based
│   ├── js/
│   │   └── players.js              # sortable/searchable table
│   ├── fonts/
│   │   └── (self-hosted)
│   ├── crests/                     # 12 fantasy-team crests
│   └── img/
└── .github/workflows/
    └── weekly.yml
```

---

## 2 · Information architecture (navigation)

Top nav (sticky, narrow strip, like premierleague.com):
```
DPL  |  Table  |  Gameweek  |  Fixtures  |  Clubs  |  Players  |  History  |  [The Londoner]
```

### Page inventory

**1 · Home (`/`)**
- Hero: current gameweek headline (e.g. "GW33 — Haalandcaust FC survive scare")
- Full league table (wins, losses, PF, PA, form streak)
- "Gameweek XX in 60 seconds" summary box
- Two featured matches card (see §4 below)
- Latest transactions ticker

**2 · Table (`/table`)**
- Full standings with sortable columns
- Form guide (last 5 GW W/L dots)
- PF/PA, avg points, total moves, waiver $ spent
- Relegation zone callout: **bottom team = The Londoner penalty**

**3 · Gameweek (`/gameweek/32`, `/gameweek/31`, …)**
- Index page listing all 33 gameweeks
- Per-GW page:
  - Scoreline-style recap (each team's total vs. league median)
  - Team of the Week (best XI across the league)
  - Top scorer, biggest bust, highest variance
  - Transaction summary for the week
  - Every team's lineup + score as a collapsible card

**4 · Fixtures (`/fixtures`)**
- "Look forward" for upcoming GW
- Two featured storylines (see §4 for what "match" means here)
- Upcoming real EPL fixtures that matter for rosters (players on bye / double GW / big matchups)

**5 · Clubs (`/clubs` and `/clubs/{slug}`)**
- All 12 fantasy teams as tiles with real PL crests
- Per-club page:
  - Owner name, team nickname, paired PL club
  - Current roster with position breakdown
  - Formation used per GW
  - Season stat leaders (top goalscorer on roster, etc.)
  - Transaction history
  - Season-long points-per-GW chart

**6 · Players (`/players`)**
- Filterable/sortable table of all ~1,650 EPL players OR just the ~420 rostered + commonly-available
- Filters: position, team, owner (rostered/free), form, min. minutes
- Per-player mini-modal: weekly stat breakdown, projection, owner history

**7 · History (`/history`)**
- 2024 season recap
- Final standings, season champion, season awards
- Top performers 2024

**8 · The Londoner (`/the-londoner`)**
- Dedicated page to the last-place penalty
- Current team in last place (with a countdown to their pub-day)
- Past Londoner victims (from 2024 data)
- Rules of the penalty, if there's lore

---

## 3 · Design direction — "doesn't look AI-made"

### Anti-patterns to avoid
- Purple-to-blue gradient hero backgrounds
- Generic shadcn/ui cards floating on neutral gray
- Utility-class soup (`bg-gray-100 rounded-xl shadow-md p-6`)
- Tailwind's default font stack
- Lucide/Heroicons default icons
- "Dashboard-y" charts in vibrant primary colors
- Rounded-full pill badges everywhere

### Going-for-real vibe — borrow directly from premierleague.com
- **Palette:**
  - Primary: `#37003C` (EPL deep purple)
  - Accent: `#FF2882` (EPL pink/magenta)
  - Highlight green: `#00FF87` (EPL lime, used sparingly for positive deltas)
  - Ink: `#240935` (deep text), light grays for body
  - White cards on soft off-white backgrounds, not pure white
- **Typography:**
  - Display: **PP Neue Machina** or **Geist** (condensed, confident) — or licensed **Premier Sans** alternative
  - Body: **Inter** at a specific weight (450, not 400 — small details like this break AI vibes)
  - Numerals: tabular, always
- **Layout:**
  - Tight baseline grid — 4px base unit, not 8px
  - Data tables are the centerpiece, not an afterthought. Heavy column dividers, zebra-striping optional, hover states with accent-color row bar
  - Team crests appear beside every team name, always same size, with proper optical spacing
  - Big, editorial headlines on gameweek pages
- **Micro-details that kill "AI-made" feel:**
  - Real EPL ball-graphic as a bullet marker in lists
  - Player tiles with a subtle diagonal clip-path (EPL brand shape)
  - "LIVE" dot with color pulse during gameweeks
  - Tabular numbers in every stat, no proportional digits
  - Team crest as a background watermark on per-team pages at 5% opacity
  - Actual scoreline-style boxes (two team rows, final score in the middle) for weekly recaps

### Team-to-club mapping (§5) is the single most impactful visual decision
If each fantasy team convincingly inhabits a real PL club (colors, crest, kit pattern), the site stops looking like a generic data viewer and starts looking like a broadcast graphic.

---

## 4 · The "featured matches" question — CORRECTED

**Correction from Will:** DPL uses real head-to-head matchups, not league-median. So there IS weekly opponent pairing, we just haven't found the endpoint yet.

**Status of investigation (as of dispatch handoff):**
- REST `/v1/league/{id}/matchups/{week}` returns HTTP 404 with body `null` and `application/json` content-type — distinct from a wrong path (which returns HTML redirect). The route exists server-side but has no data for EPL leagues.
- All standard alt paths (matchup, scoreboard, pairings, h2h, games, weeks, legs/32/matchups) return HTML 404.
- **GraphQL endpoint exists at `https://api.sleeper.app/graphql`** and accepts introspection. Almost certainly where EPL matchups live. See [sleeper_graphql_lead.md memory](memory/sleeper_graphql_lead.md) for details.
- Roster metadata has `record` (32-char W/L string per GW) and `streak` — confirms H2H exists, but no opponent info.

**Next step (for dispatch):** Full GraphQL introspection to find the matchup query. May need an auth bearer (log into sleeper.com in browser, capture from DevTools).

**"Featured matches" page content once H2H is available:** this becomes the natural H2H Fixtures page — two upcoming DPL matchups highlighted based on standings stakes.

---

## 5 · Team-to-PL-club mapping

To make each fantasy team *look like* a real PL club, we need to assign one of the 20 PL clubs to each of the 12 DPL teams. Your team name is already "Derby County" (which isn't in the PL — Championship) so existing team names can't drive this directly.

### Proposed approach
Create `team_mapping.yml`:
```yaml
# roster_id → real PL club
1:
  owner: willmoran
  team_name: Derby County          # keep the fantasy team nickname
  pl_club: Chelsea                 # their adopted PL identity
  color_primary: "#034694"
  color_secondary: "#DBA111"

2:
  owner: skoeiboy
  team_name: Fantasy Soccer is Dumb FC
  pl_club: West Ham
  color_primary: "#7A263A"
  color_secondary: "#1BB1E7"
# ... etc
```

**How to assign:** best call is probably to ask each league member which PL club they want. Failing that, we can derive from their most-drafted players' real clubs (e.g., if brendog2814's top 5 players are all Man City, assign Man City).

I'll pre-fill the YAML with *provisional* assignments based on each team's top-3 most-rostered real-club players, and you can edit before first deploy.

### Assets needed per team
- Crest (SVG preferred; can use free EPL crest SVGs with appropriate license note)
- Primary/secondary color
- Optional: kit pattern as CSS background (stripe for Newcastle, sash for Palace, etc.)

---

## 6 · Data pipeline additions needed

Before the site can build cleanly, we need to extend `ingest.py` a bit:

1. **Weekly team totals** — compute each roster's weekly points from starters + stats, store in a new `weekly_team_scores` table. This is what powers the W/L vs. median, gameweek recaps, form guide, charts. *(Current DB has raw player stats but not assembled team totals.)*
2. **Previous season ingest** — run the ingest against `previous_league_id=1121835436143435776` season 2024 to populate history. Probably want a `--league-id` and `--season` flag added to `ingest.py`.
3. **"Team of the Week" materialization** — cheap view or table: top-scoring player at each position across all rosters, per GW.
4. **Transactions → human-readable** — join adds/drops against player names for display.

---

## 7 · Open questions for you

Need answers before I can start building for real. None are blocking the plan itself.

1. **Hosting** — Cloudflare Pages OK? Do you have a custom domain you want to use, or start with a `.pages.dev` subdomain?
2. **GitHub repo** — public or private? (Cloudflare Pages auto-deploys from both.)
3. **Featured matches interpretation** — A, B, or C above, or mix?
4. **Team-to-PL-club mapping** — do you want to collect preferences from league members, or should I auto-assign provisionally and let you tweak?
5. **"The Londoner" lore** — is this Dallas's The Londoner pub? How long does last place have to be there (open to close = all day)? Is there a photo or history of past sufferers that should go on the page?
6. **Last year's data scope** — full rebuild of every GW from 2024, or just final standings + top performers?
7. **League name** — "DPL" stands for what? (Derby? Dallas? Dude?) Affects the logo direction.
8. **Design inspirations beyond premierleague.com** — any specific clubs' websites you like? FBref? Opta? The Athletic?

---

## 8 · Phased build order

**Phase 0 — Resolve open questions** *(I need your input)*

**Phase 1 — Data layer** *(solo, no UI)*
- Extend `ingest.py` with `--league-id` / `--season` flags
- Add `weekly_team_scores` materialization
- Pull 2024 season data
- Add views for: form guide, team of the week, transaction feed

**Phase 2 — Design system** *(pre-UI)*
- Set up `static/css/main.css` with tokens (colors, type, spacing)
- Build component library in isolation: nav, crest, standings row, player row, scoreline box, GW card
- Get typography and one page looking *right* before building everything

**Phase 3 — Core pages**
- Home, Table, Clubs index + per-club, Players
- GW index + one sample GW page
- Deploy preview to Cloudflare Pages from day 1 so we can review in the wild

**Phase 4 — Secondary pages**
- Fixtures (forward-look), History, The Londoner
- Transaction ticker, player modal

**Phase 5 — Weekly automation**
- GitHub Actions workflow
- Test with a manual run
- Wire up workflow_dispatch for manual rebuilds

**Phase 6 — Polish pass**
- Dedicated "doesn't look AI-made" review. Hand-tuning spacing, typographic rhythm, real motion on ingest-dependent numbers, loading states, 404 page, meta tags/OG cards for share previews

---

## 9 · Rough effort estimate (not a commitment)

Phase 1: small (~1 sitting). Phase 2: this is where we can spend real craft time, because it determines the feel. Phase 3 + 4: straightforward template work once the design system is solid. Phase 5: trivial. Phase 6: as much polish as you want to put in — this is the "doesn't look AI-made" budget and benefits from iterations, not a single pass.
