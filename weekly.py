#!/usr/bin/env python3
"""
DPL Weekly Refresh — one command does everything.

Run this whenever new gameweek results land. It will:

  1. Pull fresh data from Sleeper into dpl.db        (ingest.py)
  2. Show you the current Featured Team + Featured Matches and
     ask if you want to update the write-ups.
  3. Rebuild the entire site                          (build_site.py)
  4. Generate the email digest control panel          (email_digest.py)
  5. Open the site preview + email control panel in your browser.

Usage:
    python3 weekly.py             # interactive — prompts for write-up edits
    python3 weekly.py --no-edit   # skip all write-up prompts (data + build only)
    python3 weekly.py --no-open   # do everything but don't auto-open pages
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

HERE                  = Path(__file__).resolve().parent
INGEST                = HERE / "ingest.py"
BUILD                 = HERE / "build_site.py"
EMAIL_GEN             = HERE / "email_digest.py"
FEATURED_TEAM_PATH    = HERE / "featured_team.yml"
FEATURED_MATCHES_PATH = HERE / "featured_matches.yml"
SUBSCRIBERS_PATH      = HERE / "subscribers.json"
DIST_INDEX            = HERE / "dist" / "index.html"
EMAIL_PANEL           = HERE / "email_digest.html"


# ────────────────────────────────────────────────────────────────────
# Pretty CLI helpers
# ────────────────────────────────────────────────────────────────────

def banner(text: str):
    line = "─" * 64
    print(f"\n\033[1;35m{line}\n  {text}\n{line}\033[0m")


def step(n: int, total: int, text: str):
    print(f"\n\033[1;36m[{n}/{total}]\033[0m \033[1m{text}\033[0m")


def info(text: str):
    print(f"  \033[2m{text}\033[0m")


def ok(text: str):
    print(f"  \033[32m✓\033[0m {text}")


def warn(text: str):
    print(f"  \033[33m!\033[0m {text}")


def prompt_yes(msg: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    ans = input(f"  \033[1m?\033[0m {msg}{suffix}: ").strip().lower()
    if not ans:
        return default
    return ans.startswith("y")


def run_python(script: Path, *args):
    cmd = [sys.executable, str(script), *args]
    info(f"running {script.name} {' '.join(args)}")
    res = subprocess.run(cmd, cwd=HERE)
    if res.returncode != 0:
        warn(f"{script.name} exited with {res.returncode}")
        sys.exit(res.returncode)


def open_in_editor(path: Path):
    """Open a file in the user's preferred editor, fall back to macOS `open`."""
    editor = os.environ.get("EDITOR")
    if editor:
        info(f"opening in {editor}")
        subprocess.run([editor, str(path)])
    else:
        info("opening in your default app (set $EDITOR to override)")
        subprocess.run(["open", str(path)])
    input("  \033[1m↪\033[0m  Save your edits, then press Enter to continue...")


# ────────────────────────────────────────────────────────────────────
# YAML preview helpers
# ────────────────────────────────────────────────────────────────────

def show_featured_team():
    if not FEATURED_TEAM_PATH.exists():
        warn(f"{FEATURED_TEAM_PATH.name} not found")
        return
    data = yaml.safe_load(FEATURED_TEAM_PATH.read_text()) or {}
    print()
    print(f"  \033[2mCurrent featured team:\033[0m")
    print(f"    GW:        {data.get('gw', '—')}")
    print(f"    Roster:    #{data.get('roster_id', '—')}")
    print(f"    Headline:  {data.get('headline', '—')}")
    print(f"    Subhead:   {data.get('subheadline', '—')}")
    body = (data.get("body") or "").strip().replace("\n", " ")
    print(f"    Body:      {body[:120]}{'…' if len(body) > 120 else ''}")


def show_featured_matches():
    if not FEATURED_MATCHES_PATH.exists():
        warn(f"{FEATURED_MATCHES_PATH.name} not found")
        return
    data = yaml.safe_load(FEATURED_MATCHES_PATH.read_text()) or {}
    print()
    print(f"  \033[2mCurrent featured matches (GW{data.get('gw', '—')}):\033[0m")
    for i, m in enumerate(data.get("matches", []), 1):
        print(f"    [{i}] roster {m.get('home_roster_id', '?')} vs roster {m.get('away_roster_id', '?')}")
        print(f"        \"{m.get('headline', '')}\"")


def show_subscribers():
    if not SUBSCRIBERS_PATH.exists():
        info("(no subscribers.json yet — none added)")
        return
    subs = json.loads(SUBSCRIBERS_PATH.read_text())
    print(f"  \033[2m{len(subs)} subscriber(s):\033[0m")
    for s in subs:
        print(f"    • {s.get('name','—')} <{s['email']}>")


# ────────────────────────────────────────────────────────────────────
# Main flow
# ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-edit", action="store_true", help="skip the write-up edit prompts")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the site / email panel")
    args = ap.parse_args()

    banner("DPL Weekly Refresh")

    # ── 1. Pull fresh data ───────────────────────────────────────
    step(1, 5, "Pull fresh data from Sleeper")
    run_python(INGEST)
    ok("dpl.db refreshed")

    # ── 2. Featured team write-up ────────────────────────────────
    step(2, 5, "Featured Team write-up")
    show_featured_team()
    if args.no_edit:
        info("skipping (--no-edit)")
    elif prompt_yes("Edit featured_team.yml?"):
        open_in_editor(FEATURED_TEAM_PATH)
        ok("featured_team.yml saved")
    else:
        info("keeping current write-up")

    # ── 3. Featured matchups for next GW ─────────────────────────
    step(3, 5, "Featured Matchups for next GW")
    show_featured_matches()
    if args.no_edit:
        info("skipping (--no-edit)")
    elif prompt_yes("Edit featured_matches.yml?"):
        open_in_editor(FEATURED_MATCHES_PATH)
        ok("featured_matches.yml saved")
    else:
        info("keeping current matchups")

    # ── 4. Rebuild the site ──────────────────────────────────────
    step(4, 5, "Rebuild the site")
    run_python(BUILD)
    ok(f"dist/ regenerated")

    # ── 5. Email digest ──────────────────────────────────────────
    step(5, 5, "Email digest")
    show_subscribers()
    info("regenerating email_digest.html…")
    run_python(EMAIL_GEN)
    ok("email_digest.html ready")

    # ── Open everything ──────────────────────────────────────────
    if not args.no_open:
        banner("Opening site + email control panel")
        if DIST_INDEX.exists():
            subprocess.run(["open", str(DIST_INDEX)])
        if EMAIL_PANEL.exists():
            subprocess.run(["open", str(EMAIL_PANEL)])

    banner("Done.")
    print(
        "  Site:           dist/index.html\n"
        "  Email panel:    email_digest.html\n"
        "\n"
        "  Add subscriber:    python3 email_digest.py --add 'Name <email@x.com>'\n"
        "  Remove subscriber: python3 email_digest.py --remove 'email@x.com'\n"
        "  List subscribers:  python3 email_digest.py --list\n"
    )


if __name__ == "__main__":
    main()
