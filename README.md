# Dallas Premier League — Site & Pipeline

A static site + email digest for the **Dallas Premier League** (DPL), a 12-team Sleeper EPL fantasy league.

The site is generated from a local SQLite database (`dpl.db`) populated by `ingest.py` from the Sleeper public API + GraphQL endpoint. Templates render to `dist/`, which is what gets deployed to GitHub Pages.

---

## The weekly cycle (one command)

After a gameweek finishes, run:

```bash
python3 weekly.py
```

That walks through all five steps interactively:

1. **Pulls fresh Sleeper data** into `dpl.db` (idempotent — safe to run any time)
2. Shows the current **Featured Team** write-up and asks if you want to update `featured_team.yml`
3. Shows the current **Featured Matchups** and asks if you want to update `featured_matches.yml`
4. **Rebuilds the entire site** to `dist/`
5. **Generates the email digest** and opens it + the site preview in your browser

If you just want a "data only" refresh without prose updates:

```bash
python3 weekly.py --no-edit
```

The whole cycle takes ~30 seconds.

> Sleeper has uneven gameweek scheduling — sometimes 2 in a week, sometimes 0. There's no cron. Just run `weekly.py` whenever new results land.

---

## File Layout

```
DPL Project/
├── ingest.py                # Pulls Sleeper data → dpl.db (idempotent)
├── build_site.py            # Renders templates/* → dist/
├── email_digest.py          # Generates email_digest.html control panel
├── weekly.py                # ★ Single-command refresh — start here every week
├── schema.sql               # SQLite schema
├── dpl.db                   # SQLite database (~50 MB, committed)
│
├── team_mapping.yml         # 12 fantasy teams → managers + colours
├── featured_team.yml        # Featured-team write-up (home page lead)
├── featured_matches.yml     # 2 featured matchups (Fixtures + email)
├── draft_data.yml           # Pre-season power rankings (Draft tab)
├── history_2024.json        # Past-season recap data
├── subscribers.json         # Email subscriber list (gitignored — private)
│
├── templates/               # Jinja2 templates
├── static/                  # CSS + img + fonts
│   └── img/
│       └── dpl_logo.png     # ← swap to change the brand mark
│
├── dist/                    # Built site (deployed to GitHub Pages)
│
├── email_digest.html        # Generated email control panel (gitignored)
├── email_body.html          # Generated email body only (gitignored)
│
└── .github/workflows/
    └── pages.yml            # Builds + deploys dist/ to GitHub Pages on every push
```

---

## Configuration files (where to edit each thing)

| File | What to edit | Notes |
|---|---|---|
| `featured_team.yml` | The home page's lead spotlight | `roster_id`, `gw`, `headline`, `subheadline`, `body`, `highlight_stats`. `weekly.py` shows current value + offers to open it. |
| `featured_matches.yml` | The two featured upcoming matchups (used on Fixtures page **and** in the email "Look Ahead") | List of two entries. Each has `home_roster_id`, `away_roster_id`, `headline`, `body`. |
| `team_mapping.yml` | Manager names, team display names, colours | One block per `roster_id`. Keep `roster_id` matching the Sleeper roster IDs — those don't change mid-season. |
| `draft_data.yml` | Pre-season power rankings (Draft tab) | One entry per manager; updated once per season. |
| `history_2024.json` | Prior-season recap | Replace with fresh data each year (see "Adding a new season" below). |
| `static/img/dpl_logo.png` | Brand mark in the nav and email | Just overwrite the file, then rebuild. |

---

## Email digest

### Workflow

```bash
python3 email_digest.py        # generate the control panel
open email_digest.html         # opens it in your browser
```

The page shows:

- **Left:** the rendered email preview (exactly what your league will see)
- **Right top:** "How to send" steps + two action buttons
- **Right middle — Recipients:** every subscriber as a checkbox (all checked by default). Untick anyone you don't want this week.
- **Right bottom — Manage subscribers:** type a name + email, click **Generate add command**. The CLI command is shown + copied to clipboard so you can paste into a terminal.

The two key buttons:

- **Copy email for Gmail** — puts the formatted HTML on your clipboard (rich text, with formatting preserved)
- **Open Gmail compose (BCC checked)** — opens a new Gmail tab with every checked recipient pre-filled into BCC and `DPL GW{n} Digest` as the subject

Then paste with `Cmd-V` into the compose window, review one final time, send.

### Subscriber management (CLI)

```bash
python3 email_digest.py --add "Will <will@example.com>"
python3 email_digest.py --remove "will@example.com"
python3 email_digest.py --list
```

Or just edit `subscribers.json` directly — it's a plain JSON array.

### Sending for a specific gameweek (not just the latest)

```bash
python3 email_digest.py --week 28      # generate digest for GW28
python3 email_digest.py --weeks        # see all available gameweeks
```

---

## Deploying to GitHub Pages

The site is set up to publish from the `dist/` folder via a GitHub Action (`.github/workflows/pages.yml`).

### One-time setup

1. **Create a public GitHub repo** (e.g. `dallas-premier-league`).

2. **Initialize git locally + push:**
   ```bash
   cd "~/Desktop/DPL Project"
   git init
   git add .
   git commit -m "Initial commit — DPL site"
   git branch -M main
   git remote add origin https://github.com/<YOUR-USERNAME>/dallas-premier-league.git
   git push -u origin main
   ```

   The `.gitignore` automatically excludes:
   - `subscribers.json` (your private subscriber list)
   - `email_digest.html` / `email_body.html` (your private control panel)
   - `__pycache__/`, `dpl.db-shm`, `dpl.db-wal`, etc.

3. **Enable GitHub Pages on the repo:**
   - Go to repo → **Settings** → **Pages**
   - Under **Build and deployment → Source**, select **GitHub Actions** (not the legacy "Deploy from branch" option).
   - That's it — no branch selection needed; the workflow handles everything.

4. **Trigger the first build:**
   - Push any commit to `main`, or
   - Go to **Actions** tab → **Build + deploy to GitHub Pages** → **Run workflow**

5. After ~2 minutes your site is live at:
   ```
   https://<YOUR-USERNAME>.github.io/dallas-premier-league/
   ```

### Custom domain (optional)

- In repo **Settings → Pages → Custom domain**, enter `dpl.example.com`.
- Add a CNAME record on your DNS pointing `dpl` to `<YOUR-USERNAME>.github.io`.
- A `CNAME` file is auto-created in the repo root; the workflow copies it into `dist/` on each build.

### Weekly publish

Every time you want to update the public site:

```bash
python3 weekly.py            # local refresh + preview
git add -A && git commit -m "weekly: GW32 update"
git push
```

The push triggers `pages.yml`, which:

1. Runs `ingest.py` (refreshes data from Sleeper)
2. Runs `build_site.py` (rebuilds the site)
3. Adds `.nojekyll` so GitHub serves all files raw
4. Uploads `dist/` and deploys to Pages

> The Action ALSO runs ingest, so if you forget to commit a fresh `dpl.db` it'll still build with the latest data. But committing `dpl.db` along with your changes makes the build reproducible.

> The email digest is **never** deployed — `email_digest.html` and `subscribers.json` are gitignored. They live only on your laptop.

---

## Adding a new season (2026/27 etc.)

When the current season ends:

1. **Find the new league ID** — open Sleeper, navigate to the new DPL league page; the URL contains it: `sleeper.com/leagues/<NEW_ID>/...`.

2. **Save the current season's recap** to `history_2025.json` (mirroring `history_2024.json`).

3. **Update the configs:**
   - In `ingest.py` and `build_site.py`, change:
     ```python
     LEAGUE_ID = "<NEW_LEAGUE_ID>"
     SEASON    = "2026"
     ```
   - Update `team_mapping.yml` for any new/changed managers.
   - Replace `draft_data.yml` with the new pre-season power rankings.
   - Set `featured_team.yml` to the season opener's spotlight.

4. **Wipe + rebuild:**
   ```bash
   rm dpl.db
   python3 weekly.py
   ```

---

## Database

Run `sqlite3 dpl.db ".schema"` to see the schema. Highlights:

| Table | Purpose |
|---|---|
| `league` | League metadata, scoring settings |
| `league_users` | Sleeper users (managers) |
| `rosters` | Per-team record, points, players JSON |
| `players` | EPL player universe (~1,650 players) |
| `player_stats` | Per-week per-player stats |
| `matchup_legs` | H2H pairings + scores per gameweek |
| `fixtures` | Real EPL match schedule |
| `transactions` | Adds / drops / waivers |
| `v_standings` | Standings view |
| `v_matchup_results` | H2H results with both sides on one row |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ingest.py` SSL errors | `pip install certifi` |
| `ModuleNotFoundError` on build | `pip install -r requirements.txt` |
| GitHub Pages serves a 404 | Ensure `dist/.nojekyll` exists (the workflow creates it). Also confirm Settings → Pages source is **GitHub Actions**, not "Deploy from branch". |
| Pages deploy fails with permissions error | Settings → Actions → General → Workflow permissions → enable "Read and write permissions". |
| Email "Copy" button doesn't preserve formatting | Use Chrome / Edge — Safari's clipboard API has spotty `text/html` support. Alternative: `Cmd-A` inside the iframe, then `Cmd-C`. |

---

## Credits

- Data: [Sleeper API](https://docs.sleeper.com) + GraphQL endpoint
- Fonts: [Inter](https://rsms.me/inter/)
