#!/usr/bin/env python3
"""
DPL Weekly Email Digest — everything lives in one HTML file.

Workflow (runs entirely in your browser, no auto-send):

  1. python3 email_digest.py               # generates email_digest.html
  2. Open email_digest.html
  3. See the email preview on the left, subscriber checkboxes on the right
  4. Un-check anyone you don't want to email this week
  5. Click "Copy email for Gmail"            — puts formatted HTML on clipboard
  6. Click "Open Gmail compose (BCC everyone)" — opens a new Gmail compose tab
     with every checked recipient pre-filled into BCC and the subject pre-filled
  7. Paste into the compose window (Cmd-V), review once more, hit Send

Subscriber management — subscribers.json is a plain JSON array:
  [ {"name": "Will", "email": "will@example.com"} ]

Add / remove with the CLI (or just edit the file directly):
  python3 email_digest.py --add "Brennan <brendog@example.com>"
  python3 email_digest.py --remove "brendog@example.com"
  python3 email_digest.py --list
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs

HERE             = Path(__file__).resolve().parent
OUTPUT_FULL      = HERE / "email_digest.html"
OUTPUT_BODY      = HERE / "email_body.html"
SUBSCRIBERS_PATH = HERE / "subscribers.json"
SITE_URL         = "https://dallaspremierleague.org"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ────────────────────────────────────────────────────────────────────
# Subscribers
# ────────────────────────────────────────────────────────────────────

def load_subscribers() -> list[dict]:
    if SUBSCRIBERS_PATH.exists():
        return json.loads(SUBSCRIBERS_PATH.read_text())
    return []


def save_subscribers(subs: list[dict]):
    SUBSCRIBERS_PATH.write_text(json.dumps(subs, indent=2))


def parse_address(s: str) -> tuple[str, str]:
    m = re.match(r"^(.+?)\s*<\s*([^>]+)\s*>\s*$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    return s.strip(), s.strip().lower()


def add_subscriber(s: str):
    name, email = parse_address(s)
    if not EMAIL_RE.match(email):
        print(f"Invalid email: {email}"); sys.exit(1)
    subs = load_subscribers()
    if any(x["email"].lower() == email for x in subs):
        print(f"Already subscribed: {email}"); return
    subs.append({"name": name, "email": email})
    save_subscribers(subs)
    print(f"Added: {name} <{email}>")


def remove_subscriber(email: str):
    email = email.strip().lower()
    subs = load_subscribers()
    new  = [x for x in subs if x["email"].lower() != email]
    if len(new) == len(subs):
        print(f"Not found: {email}"); return
    save_subscribers(new)
    print(f"Removed: {email}")


def list_subscribers():
    subs = load_subscribers()
    if not subs: print("(no subscribers yet — use --add or edit subscribers.json)"); return
    print(f"{len(subs)} subscriber(s):")
    for s in subs:
        print(f"  {s.get('name','—')} <{s['email']}>")


# ────────────────────────────────────────────────────────────────────
# Email body styles (Gmail-safe, table-based scoreboard layout)
# ────────────────────────────────────────────────────────────────────

EMAIL_CSS = """
<style>
  body { margin:0; padding:0; background:#F8F5EE; font-family: 'Helvetica Neue', Arial, sans-serif; color:#1F0F2A; }
  .wrap { max-width:640px; margin:0 auto; background:#FFFFFF; }
  .header { background:#3D1452; color:#FFFFFF; padding:28px 32px; border-bottom:4px solid #B89968; }
  .header__brand { font-size:14px; font-weight:800; letter-spacing:0.08em; color:#FFFFFF; margin-bottom:4px; }
  .header__sub { font-size:11px; font-weight:600; letter-spacing:0.18em; text-transform:uppercase; color:#B89968; }
  .header__title { font-size:18px; font-weight:700; margin-top:10px; letter-spacing:0.02em; line-height:1.2; color:rgba(255,255,255,0.92); }
  .section { padding:28px 32px; border-top:1px solid #E8E0D5; }
  .section h2 { font-size:11px; font-weight:700; letter-spacing:0.14em; text-transform:uppercase; color:#5A4A65; margin:0 0 16px 0; }

  .stat-row { display:table; width:100%; border-collapse:collapse; margin-bottom:18px; }
  .stat-cell { display:table-cell; width:50%; padding:16px 18px; background:#F1ECE2; border:2px solid #FFFFFF; vertical-align:top; }
  .stat-cell__val { font-size:24px; font-weight:800; color:#3D1452; letter-spacing:-0.03em; line-height:1; font-variant-numeric: tabular-nums; }
  .stat-cell__lab { font-size:10px; font-weight:700; letter-spacing:0.14em; text-transform:uppercase; color:#8E7E99; margin-top:6px; }
  .stat-cell__sub { font-size:12px; color:#5A4A65; margin-top:4px; }

  .top-perf { background:#3D1452; padding:18px 22px; color:#FFFFFF; margin-bottom:20px; }
  .top-perf__kicker { font-size:10px; font-weight:700; letter-spacing:0.14em; text-transform:uppercase; color:#B89968; margin-bottom:6px; }
  .top-perf__name { font-size:17px; font-weight:800; }
  .top-perf__meta { font-size:11px; color:rgba(255,255,255,0.6); margin-top:2px; }
  .top-perf__pts { color:#B89968; font-weight:800; margin-left:8px; font-variant-numeric: tabular-nums; }

  .match { width:100%; border-collapse:collapse; border-bottom:1px solid #E8E0D5; }
  .match td { padding:12px 8px; vertical-align:middle; }
  .match .team-a { text-align:left;  width:40%; }
  .match .team-b { text-align:right; width:40%; }
  .match .score  { text-align:center; width:20%; font-weight:800; font-size:18px; color:#1F0F2A; font-variant-numeric: tabular-nums; white-space:nowrap; }
  .match .score em { color:#C9BDB0; font-style:normal; margin:0 3px; font-weight:300; }
  .match .name { font-weight:700; font-size:14px; color:#1F0F2A; line-height:1.25; }
  .match .name.winner { color:#3D1452; }
  .match .sub { font-size:11px; color:#8E7E99; margin-top:2px; }

  .table { width:100%; border-collapse:collapse; font-size:13px; }
  .table th { background:#3D1452; color:#FFFFFF; padding:8px 10px; font-size:10px; font-weight:700; letter-spacing:0.12em; text-transform:uppercase; text-align:right; }
  .table th:nth-child(2) { text-align:left; }
  .table td { padding:9px 10px; border-bottom:1px solid #E8E0D5; text-align:right; font-variant-numeric: tabular-nums; }
  .table td:nth-child(2) { text-align:left; font-weight:700; color:#1F0F2A; }

  .awards { display:table; width:100%; border-collapse:separate; border-spacing:6px 6px; margin-bottom:4px; }
  .awards-row { display:table-row; }
  .award { display:table-cell; width:50%; padding:14px 16px; background:#F1ECE2; vertical-align:top; }
  .award__title { font-size:10px; font-weight:700; letter-spacing:0.14em; text-transform:uppercase; color:#8B6E3F; margin-bottom:4px; }
  .award__val { font-size:22px; font-weight:800; letter-spacing:-0.03em; color:#1F0F2A; line-height:1.1; font-variant-numeric: tabular-nums; }
  .award__meta { margin-top:4px; font-size:12px; color:#5A4A65; line-height:1.4; }
  .award__meta strong { color:#1F0F2A; }
  .award__meta em { font-style:italic; color:#9C2A3D; }

  .footer { padding:24px 32px; text-align:center; font-size:11px; color:#8E7E99; background:#F8F5EE; }
  .footer a { color:#3D1452; text-decoration:none; }

  a { color:#3D1452; text-decoration:none; }
</style>
"""


def fmt(n, digits=1):
    if n is None: return "—"
    return f"{n:.{digits}f}"


def render_email_body(data: dict) -> str:
    last_gw          = data["last_gw"]
    upcoming         = data["upcoming"]
    standings        = data["standings"]
    weekly           = data["weekly_awards"]
    featured_matches = data["featured_matches"]
    current_wk       = data["current_week"]
    next_wk          = current_wk + 1
    site_url         = data["site_url"]

    parts = [f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>DPL — GW{current_wk} Digest</title>{EMAIL_CSS}</head>
<body>
<div class="wrap">

  <div class="header">
    <div class="header__brand">DPL</div>
    <div class="header__sub">Dallas Premier League</div>
    <div class="header__title">Gameweek {current_wk} Digest</div>
  </div>
"""]

    # ── Top Performer (single overall player) ──
    if last_gw and last_gw.get("top_scorer"):
        ts = last_gw["top_scorer"]
        parts.append(f"""
  <div class="section">
    <div class="top-perf">
      <div class="top-perf__kicker">Top Performer · GW{current_wk}</div>
      <div class="top-perf__name">{ts['full_name']} <span class="top-perf__pts">{fmt(ts['pts'], 1)} pts</span></div>
      <div class="top-perf__meta">{ts['team_abbr']} · {ts['position_primary']}</div>
    </div>
  </div>
""")

    # ── Weekly Awards (this GW only) ──
    if weekly and weekly.get("high_score"):
        hi = weekly["high_score"]
        lo = weekly["low_score"]
        bb = weekly.get("biggest_blowout")
        cg = weekly.get("closest_game")

        parts.append(f"""
  <div class="section">
    <h2>Gameweek {current_wk} Awards</h2>
    <div class="awards">
      <div class="awards-row">
        <div class="award">
          <div class="award__title">Highest Score</div>
          <div class="award__val">{fmt(hi['points'], 2)}</div>
          <div class="award__meta"><strong>{hi['display_name']}</strong></div>
        </div>
        <div class="award">
          <div class="award__title">Lowest Score</div>
          <div class="award__val">{fmt(lo['points'], 2)}</div>
          <div class="award__meta"><strong>{lo['display_name']}</strong></div>
        </div>
      </div>
""")

        if bb and cg:
            parts.append(f"""
      <div class="awards-row">
        <div class="award">
          <div class="award__title">Biggest Blowout</div>
          <div class="award__val">{fmt(bb['diff'], 2)}</div>
          <div class="award__meta"><strong>{bb['winner_name']}</strong> def. {bb['loser_name']}</div>
        </div>
        <div class="award">
          <div class="award__title">Closest Game</div>
          <div class="award__val">{fmt(cg['diff'], 2)}</div>
          <div class="award__meta"><strong>{cg['winner_name']}</strong> over {cg['loser_name']}</div>
        </div>
      </div>
""")
        parts.append("    </div>\n")

        # Best by position this week
        positions = [
            ("Best FWD", weekly.get("best_fwd")),
            ("Best MID", weekly.get("best_mid")),
            ("Best DEF", weekly.get("best_def")),
            ("Best GK",  weekly.get("best_gk")),
        ]
        parts.append('    <div class="awards" style="margin-top:6px;">\n')
        # Render in two rows of two
        pairs = [positions[i:i+2] for i in range(0, len(positions), 2)]
        for pair in pairs:
            parts.append('      <div class="awards-row">\n')
            for label, p in pair:
                if p:
                    parts.append(f"""        <div class="award">
          <div class="award__title">{label}</div>
          <div class="award__val">{fmt(p['pts'], 1)}</div>
          <div class="award__meta"><strong>{p['full_name']}</strong><br>
            <span style="color:#8E7E99; font-size:11px;">{p['team_abbr']} · started by {p['team_name']}</span>
          </div>
        </div>
""")
                else:
                    parts.append(f"""        <div class="award"><div class="award__title">{label}</div><div class="award__meta">—</div></div>\n""")
            parts.append('      </div>\n')
        parts.append("    </div>\n  </div>")

    # ── Results ──
    if last_gw and last_gw.get("matchups"):
        parts.append(f"""
  <div class="section">
    <h2>Gameweek {current_wk} Results</h2>
""")
        for m in last_gw["matchups"]:
            ta, tb = m["team_a"], m["team_b"]
            pts_a  = ta["points"] or 0
            pts_b  = tb["points"] or 0
            won_a  = m["winner_roster_id"] == ta["roster_id"]
            ta_cls = "name winner" if won_a else "name"
            tb_cls = "name winner" if not won_a else "name"
            parts.append(f"""
    <table class="match" role="presentation" cellpadding="0" cellspacing="0">
      <tr>
        <td class="team-a"><div class="{ta_cls}">{ta['owner']}</div></td>
        <td class="score">{fmt(pts_a, 1)}<em>–</em>{fmt(pts_b, 1)}</td>
        <td class="team-b"><div class="{tb_cls}">{tb['owner']}</div></td>
      </tr>
    </table>
""")
        parts.append("  </div>")

    # ── Look Ahead (with 2 featured-match write-ups) — ABOVE the table ──
    if upcoming:
        parts.append(f"""
  <div class="section">
    <h2>Look Ahead — GW{next_wk}</h2>
""")
        # Featured write-ups first
        if featured_matches and featured_matches.get("matches"):
            for fm in featured_matches["matches"]:
                parts.append(f"""
    <div style="border-left:4px solid #B89968; padding-left:14px; margin-bottom:18px;">
      <div style="font-size:10px; font-weight:700; letter-spacing:0.14em; text-transform:uppercase; color:#8B6E3F; margin-bottom:4px;">Match to Watch</div>
      <div style="font-size:15px; font-weight:800; color:#1F0F2A; letter-spacing:-0.01em; margin-bottom:4px;">{fm['headline']}</div>
      <table class="match" role="presentation" cellpadding="0" cellspacing="0" style="border-bottom:none;">
        <tr>
          <td class="team-a">
            <div class="name">{fm['home']['display_name']}</div>
            <div class="sub">{fm['home']['record']}</div>
          </td>
          <td class="score" style="font-size:12px; font-weight:600; color:#8E7E99; letter-spacing:0.14em;">VS</td>
          <td class="team-b">
            <div class="name">{fm['away']['display_name']}</div>
            <div class="sub">{fm['away']['record']}</div>
          </td>
        </tr>
      </table>
      <p style="margin:6px 0 0 0; font-size:13px; line-height:1.55; color:#5A4A65;">{fm['body']}</p>
    </div>
""")

        # Then all the other pairings, compact
        featured_pairs = set()
        if featured_matches and featured_matches.get("matches"):
            for fm in featured_matches["matches"]:
                featured_pairs.add(frozenset([fm["home"]["roster_id"], fm["away"]["roster_id"]]))

        remaining = [m for m in upcoming
                     if frozenset([m["team_a"]["roster_id"], m["team_b"]["roster_id"]]) not in featured_pairs]
        if remaining:
            parts.append("""
    <div style="margin-top:22px; padding-top:14px; border-top:1px solid #E8E0D5;">
      <h3 style="font-size:11px; font-weight:700; letter-spacing:0.14em; text-transform:uppercase; color:#5A4A65; margin:0 0 10px 0;">Rest of the Week</h3>
""")
            for m in remaining:
                ta, tb = m["team_a"], m["team_b"]
                parts.append(f"""
      <table class="match" role="presentation" cellpadding="0" cellspacing="0">
        <tr>
          <td class="team-a"><div class="name">{ta['owner']}</div><div class="sub">{ta['record']}</div></td>
          <td class="score" style="font-size:12px; font-weight:600; color:#8E7E99; letter-spacing:0.14em;">VS</td>
          <td class="team-b"><div class="name">{tb['owner']}</div><div class="sub">{tb['record']}</div></td>
        </tr>
      </table>
""")
            parts.append("    </div>")
        parts.append("  </div>")

    # ── Table (now below Look Ahead) ──
    parts.append("""
  <div class="section">
    <h2>League Table</h2>
    <table class="table" cellpadding="0" cellspacing="0">
      <thead><tr><th>#</th><th>Manager</th><th>W</th><th>L</th><th>PF</th></tr></thead>
      <tbody>
""")
    for t in standings:
        parts.append(f"        <tr><td>{t['position']}</td><td>{t['display_name']}</td><td>{t['wins']}</td><td>{t['losses']}</td><td>{fmt(t['pts_for'], 1)}</td></tr>\n")
    parts.append("      </tbody>\n    </table>\n  </div>")

    parts.append(f"""
  <div class="footer">
    Dallas Premier League · 2025/26 · <a href="{site_url}">View the site →</a>
  </div>

</div>
</body>
</html>
""")
    return "".join(parts)


# ────────────────────────────────────────────────────────────────────
# Wrapper — preview + subscribers + Copy + Gmail compose
# ────────────────────────────────────────────────────────────────────

WRAPPER_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>DPL Digest — GW{current_week} Control Panel</title>
<style>
  :root {{
    --c-primary: #3D1452;
    --c-gold:    #B89968;
    --c-ink:     #1F0F2A;
    --c-line:    #D9D2C5;
    --c-paper:   #FAF7F0;
    --c-soft:    #F1ECE2;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:'Helvetica Neue', Arial, sans-serif; background:#E8E0D5; color:var(--c-ink); }}

  .topbar {{
    background:var(--c-primary); color:#fff; padding:18px 24px;
    display:flex; align-items:center; gap:14px; border-bottom:3px solid var(--c-gold);
    position:sticky; top:0; z-index:10;
  }}
  .topbar__title {{ font-weight:800; font-size:17px; letter-spacing:0.02em; }}
  .topbar__title small {{ color:var(--c-gold); font-weight:600; font-size:11px; letter-spacing:0.16em; text-transform:uppercase; margin-left:10px; }}
  .topbar__spacer {{ flex:1; }}
  .topbar a.site-link {{ color:rgba(255,255,255,0.7); font-size:12px; letter-spacing:0.08em; text-transform:uppercase; font-weight:600; }}

  .layout {{
    display:grid; grid-template-columns: 1fr 360px; gap:24px; padding:24px;
    max-width:1400px; margin:0 auto; align-items:start;
  }}
  @media (max-width:1024px) {{ .layout {{ grid-template-columns:1fr; }} }}

  .preview-panel {{
    background:#fff; border:1px solid var(--c-line); padding:16px;
    display:flex; flex-direction:column; gap:12px;
  }}
  .preview-panel h2 {{
    margin:0; font-size:11px; font-weight:700; letter-spacing:0.16em; text-transform:uppercase; color:#8E7E99;
  }}
  .preview-panel iframe {{
    width:100%; height:1600px; max-width:640px; margin:0 auto;
    display:block; border:1px solid var(--c-line); background:#fff;
  }}

  .sidebar {{
    display:flex; flex-direction:column; gap:16px; position:sticky; top:88px;
  }}
  .card {{
    background:#fff; border:1px solid var(--c-line); padding:18px;
  }}
  .card h3 {{
    margin:0 0 12px 0; font-size:11px; font-weight:700; letter-spacing:0.16em; text-transform:uppercase; color:var(--c-primary);
  }}
  .card p {{ margin:0; font-size:13px; line-height:1.5; color:#5A4A65; }}
  .card p + p {{ margin-top:8px; }}

  .btn {{
    display:block; width:100%; font-family:inherit;
    font-size:12px; font-weight:700; letter-spacing:0.1em; text-transform:uppercase;
    padding:12px 16px; border:none; cursor:pointer; border-radius:3px; margin-bottom:8px;
    text-align:center;
  }}
  .btn:last-child {{ margin-bottom:0; }}
  .btn--primary {{ background:var(--c-primary); color:#fff; }}
  .btn--primary:hover {{ background:#5A2D70; }}
  .btn--gold {{ background:var(--c-gold); color:var(--c-primary); }}
  .btn--gold:hover {{ background:#D2B07F; }}
  .btn--ghost {{ background:transparent; color:var(--c-ink); border:1px solid var(--c-line); }}
  .btn--ghost:hover {{ border-color:var(--c-gold); color:var(--c-primary); }}

  .sub-list {{ list-style:none; margin:0; padding:0; max-height:360px; overflow-y:auto; }}
  .sub-item {{
    display:flex; gap:10px; align-items:center;
    padding:8px 4px; border-bottom:1px solid var(--c-line); font-size:13px;
  }}
  .sub-item:last-child {{ border-bottom:none; }}
  .sub-item input[type=checkbox] {{ accent-color:var(--c-primary); transform:scale(1.15); }}
  .sub-item__name {{ font-weight:700; color:var(--c-ink); }}
  .sub-item__email {{ color:#8E7E99; font-size:12px; }}
  .sub-item__info {{ display:flex; flex-direction:column; flex:1; min-width:0; }}
  .sub-item__info > div {{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}

  .sub-controls {{
    display:flex; gap:8px; margin-bottom:12px; font-size:11px;
  }}
  .sub-controls a {{
    cursor:pointer; color:var(--c-primary); font-weight:700;
    letter-spacing:0.08em; text-transform:uppercase;
  }}
  .sub-controls a:hover {{ color:var(--c-gold); }}
  .sub-controls span {{ color:var(--c-line); }}

  .counter {{
    font-size:12px; color:#8E7E99; font-weight:600; letter-spacing:0.04em;
    text-align:center; margin-top:10px;
  }}
  .counter strong {{ color:var(--c-primary); font-weight:800; }}

  .empty {{
    padding:32px 16px; text-align:center; color:#8E7E99; font-size:13px;
    border:1px dashed var(--c-line);
  }}
  .empty code {{ background:var(--c-soft); padding:2px 6px; border-radius:2px; font-size:12px; }}

  .toast {{
    position:fixed; bottom:28px; left:50%; transform:translateX(-50%);
    background:var(--c-ink); color:#fff; padding:12px 24px; border-radius:3px;
    font-size:13px; font-weight:600; letter-spacing:0.04em;
    box-shadow:0 4px 16px rgba(0,0,0,0.25); opacity:0; pointer-events:none;
    transition:opacity 0.2s; z-index:100;
  }}
  .toast.show {{ opacity:1; }}

  .step-list {{ list-style:none; padding:0; margin:0; font-size:13px; }}
  .step-list li {{ padding:8px 0; border-bottom:1px dotted var(--c-line); display:flex; gap:10px; }}
  .step-list li:last-child {{ border-bottom:none; }}
  .step-list .num {{
    flex-shrink:0; width:22px; height:22px; border-radius:50%;
    background:var(--c-gold); color:var(--c-primary);
    display:flex; align-items:center; justify-content:center;
    font-weight:800; font-size:12px;
  }}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar__title">DPL Digest Control Panel <small>GW{current_week}</small></div>
  <div class="topbar__spacer"></div>
  <a class="site-link" href="dist/index.html" target="_blank" rel="noopener">Open site →</a>
</div>

<div class="layout">

  <div class="preview-panel">
    <h2>Email preview</h2>
    <iframe id="email-frame" srcdoc='{iframe_srcdoc}'></iframe>
  </div>

  <aside class="sidebar">
    <div class="card">
      <h3>How to send</h3>
      <ol class="step-list">
        <li><span class="num">1</span><span>Review the email on the left.</span></li>
        <li><span class="num">2</span><span>Tick / un-tick recipients below.</span></li>
        <li><span class="num">3</span><span>Click <strong>Copy email for Gmail</strong>.</span></li>
        <li><span class="num">4</span><span>Click <strong>Open Gmail compose</strong>.</span></li>
        <li><span class="num">5</span><span>Paste (Cmd-V), review once more, send.</span></li>
      </ol>
    </div>

    <div class="card">
      <h3>Actions</h3>
      <button class="btn btn--gold"    onclick="copyEmail(this)">Copy email for Gmail</button>
      <button class="btn btn--primary" onclick="openCompose()">Open Gmail compose (BCC checked)</button>
      <button class="btn btn--ghost"   onclick="window.open('email_body.html', '_blank')">View body in new tab</button>
    </div>

    <div class="card">
      <h3>Recipients</h3>
      {subs_block}
      <div class="counter"><strong id="recip-count">{recipient_count}</strong> of {total} selected</div>
    </div>

    <div class="card">
      <h3>Manage subscribers</h3>
      <p style="margin:0 0 12px 0; font-size:12px; color:#5A4A65;">Type a name + email, then run the generated command in your terminal.</p>
      <input id="sub-name"  type="text"  placeholder="Name (e.g. Brennan)"
             style="width:100%; padding:8px 10px; border:1px solid #C9BDB0; font-size:13px; margin-bottom:6px;">
      <input id="sub-email" type="email" placeholder="email@example.com"
             style="width:100%; padding:8px 10px; border:1px solid #C9BDB0; font-size:13px; margin-bottom:10px;">
      <button class="btn btn--ghost" onclick="generateAddCmd()">Generate add command</button>
      <button class="btn btn--ghost" onclick="generateRemoveCmd()" style="margin-bottom:0;">Generate remove command</button>
      <pre id="sub-cmd" style="display:none; margin-top:10px; padding:10px 12px; background:#1F0F2A; color:#B89968; font-size:12px; font-family:'SF Mono', Menlo, monospace; white-space:pre-wrap; word-break:break-all; border-radius:3px;"></pre>
    </div>
  </aside>

</div>

<div class="toast" id="toast">Copied</div>

<script>
const SUBJECT = "DPL GW{current_week} Digest";

function updateCounter() {{
  const boxes = document.querySelectorAll('.sub-item input[type=checkbox]');
  const checked = Array.from(boxes).filter(b => b.checked).length;
  document.getElementById('recip-count').textContent = checked;
}}

function selectAll(val) {{
  document.querySelectorAll('.sub-item input[type=checkbox]').forEach(b => b.checked = val);
  updateCounter();
}}

async function copyEmail(btn) {{
  const frame = document.getElementById('email-frame');
  const doc = frame.contentDocument || frame.contentWindow.document;
  const html  = doc.documentElement.outerHTML;
  const plain = doc.body.innerText;

  try {{
    if (navigator.clipboard && window.ClipboardItem) {{
      const item = new ClipboardItem({{
        'text/html':  new Blob([html],  {{ type: 'text/html'  }}),
        'text/plain': new Blob([plain], {{ type: 'text/plain' }}),
      }});
      await navigator.clipboard.write([item]);
    }} else {{
      const sel = doc.getSelection();
      const range = doc.createRange();
      range.selectNodeContents(doc.body);
      sel.removeAllRanges(); sel.addRange(range);
      doc.execCommand('copy'); sel.removeAllRanges();
    }}
    showToast('Copied — now click "Open Gmail compose"');
    const original = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => {{ btn.textContent = original; }}, 2000);
  }} catch (e) {{
    showToast('Copy failed — select email manually with Cmd-A, Cmd-C');
    console.error(e);
  }}
}}

function openCompose() {{
  const boxes   = document.querySelectorAll('.sub-item input[type=checkbox]:checked');
  const emails  = Array.from(boxes).map(b => b.value);
  if (emails.length === 0) {{
    showToast('No recipients selected');
    return;
  }}
  const bcc = emails.join(',');
  const url = `https://mail.google.com/mail/?view=cm&fs=1&bcc=${{encodeURIComponent(bcc)}}&su=${{encodeURIComponent(SUBJECT)}}`;
  window.open(url, '_blank', 'noopener');
}}

function showToast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2400);
}}

document.querySelectorAll('.sub-item input[type=checkbox]').forEach(b => {{
  b.addEventListener('change', updateCounter);
}});
updateCounter();

function generateAddCmd() {{
  const n = document.getElementById('sub-name').value.trim();
  const e = document.getElementById('sub-email').value.trim();
  const out = document.getElementById('sub-cmd');
  if (!e) {{ showToast('Email is required'); return; }}
  const arg = n ? `"${{n}} <${{e}}>"` : `"${{e}}"`;
  out.textContent = `python3 email_digest.py --add ${{arg}}\\npython3 email_digest.py        # regenerate panel`;
  out.style.display = 'block';
  navigator.clipboard?.writeText(out.textContent).then(() => showToast('Command copied to clipboard'));
}}
function generateRemoveCmd() {{
  const e = document.getElementById('sub-email').value.trim();
  const out = document.getElementById('sub-cmd');
  if (!e) {{ showToast('Email is required'); return; }}
  out.textContent = `python3 email_digest.py --remove "${{e}}"\\npython3 email_digest.py        # regenerate panel`;
  out.style.display = 'block';
  navigator.clipboard?.writeText(out.textContent).then(() => showToast('Command copied to clipboard'));
}}
</script>
</body>
</html>
"""


def render_subs_block(subs: list[dict]) -> str:
    if not subs:
        return """<div class="empty">
No subscribers yet.<br>
Add with <code>python3 email_digest.py --add "Name &lt;email&gt;"</code><br>
or edit <code>subscribers.json</code> directly.
</div>"""
    items = []
    items.append('<div class="sub-controls">')
    items.append('<a onclick="selectAll(true)">Select all</a>')
    items.append('<span>·</span>')
    items.append('<a onclick="selectAll(false)">Clear</a>')
    items.append('</div>')
    items.append('<ul class="sub-list">')
    for s in subs:
        name  = (s.get("name") or s["email"]).replace("<", "&lt;")
        email = s["email"].replace("<", "&lt;")
        items.append(f"""
        <li class="sub-item">
          <input type="checkbox" value="{email}" checked>
          <div class="sub-item__info">
            <div class="sub-item__name">{name}</div>
            <div class="sub-item__email">{email}</div>
          </div>
        </li>""")
    items.append('</ul>')
    return "".join(items)


# ────────────────────────────────────────────────────────────────────

def list_available_weeks(conn) -> list[int]:
    """Weeks that have at least one scored matchup — candidates for a digest."""
    rows = conn.execute("""
        SELECT DISTINCT week FROM matchup_legs
        WHERE league_id=? AND season=? AND points IS NOT NULL
        ORDER BY week
    """, (bs.LEAGUE_ID, bs.SEASON)).fetchall()
    return [r["week"] if hasattr(r, "keys") else r[0] for r in rows]


def gather_data(override_week: int | None = None) -> dict:
    conn             = bs.open_db()
    team_map         = bs.load_team_mapping()
    auto_wk          = bs.get_current_week(conn)
    current_week     = override_week if override_week is not None else auto_wk
    standings        = bs.get_standings(conn, team_map)
    standings_map    = {t["roster_id"]: t for t in standings}
    last_gw          = bs.get_gw_detail(conn, current_week, team_map, standings_map) if current_week >= 1 else None
    upcoming         = bs.get_upcoming_matchups(conn, current_week + 1, team_map, standings)
    weekly_awards    = bs.compute_weekly_awards(conn, current_week, team_map, standings) if current_week >= 1 else None
    featured_matches = bs.enrich_featured_matches(conn, bs.load_featured_matches(), team_map, standings)
    conn.close()
    return {
        "current_week":     current_week,
        "last_gw":          last_gw,
        "upcoming":         upcoming,
        "standings":        standings,
        "weekly_awards":    weekly_awards,
        "featured_matches": featured_matches,
        "site_url":         SITE_URL,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open",   action="store_true", help="Open email_digest.html after generating")
    ap.add_argument("--week",   type=int, help="Generate the digest for a specific gameweek (default: last completed)")
    ap.add_argument("--weeks",  action="store_true", help="List available gameweeks and exit")
    ap.add_argument("--add",    help="Add a subscriber: 'Name <email@x>' or 'email@x'")
    ap.add_argument("--remove", help="Remove a subscriber by email")
    ap.add_argument("--list",   action="store_true", help="List current subscribers")
    args = ap.parse_args()

    if args.add:    add_subscriber(args.add); return
    if args.remove: remove_subscriber(args.remove); return
    if args.list:   list_subscribers(); return
    if args.weeks:
        conn = bs.open_db()
        weeks = list_available_weeks(conn)
        conn.close()
        if not weeks:
            print("(no scored gameweeks in the DB yet — run ingest.py first)")
        else:
            print(f"{len(weeks)} scored gameweek(s) in the DB: {', '.join('GW' + str(w) for w in weeks)}")
            print(f"Default digest week (last completed): GW{weeks[-1]}")
            print(f"To digest a specific one:  python3 email_digest.py --week {weeks[-1]}")
        return

    data = gather_data(override_week=args.week)
    body = render_email_body(data)
    OUTPUT_BODY.write_text(body, encoding="utf-8")

    subs       = load_subscribers()
    subs_html  = render_subs_block(subs)
    # Escape for srcdoc attribute (single-quoted in the template)
    srcdoc     = body.replace("&", "&amp;").replace("'", "&#39;")

    wrapper = WRAPPER_TEMPLATE.format(
        current_week    = data["current_week"],
        iframe_srcdoc   = srcdoc,
        subs_block      = subs_html,
        recipient_count = len(subs),
        total           = len(subs),
    )
    OUTPUT_FULL.write_text(wrapper, encoding="utf-8")

    print(f"Wrote {OUTPUT_BODY.name}")
    print(f"Wrote {OUTPUT_FULL.name}  ({len(subs)} subscriber(s))")
    print()
    print(f"Open {OUTPUT_FULL.name} in a browser to review + send.")

    if args.open:
        subprocess.run(["open", str(OUTPUT_FULL)])


if __name__ == "__main__":
    main()
