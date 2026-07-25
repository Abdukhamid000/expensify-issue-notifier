#!/usr/bin/env python3
"""
Telegram notifier for new Expensify/App GitHub issues labeled `bug` + `daily`.

Polls the GitHub Issues API on an interval, remembers which issues it has
already announced (in a small JSON state file), and sends a Telegram message
for each newly-seen matching issue.

Setup
-----
1. Create a bot with @BotFather on Telegram -> get the BOT TOKEN.
2. Send any message to your bot, then open:
      https://api.telegram.org/bot<TOKEN>/getUpdates
   and copy the "chat":{"id": ...} value -> that's your CHAT ID.
3. (Optional but recommended) create a GitHub personal access token to raise
   the API rate limit from 60/hr to 5000/hr. A classic token with no scopes
   (public read) is enough for a public repo.

Run
---
    export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
    export TELEGRAM_CHAT_ID="987654321"
    export GITHUB_TOKEN="ghp_..."        # optional
    python3 telegram_issue_notifier.py

Only stdlib is used, so no `pip install` is needed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration (via environment variables) ------------------------------

REPO = os.environ.get("REPO", "Expensify/App")
LABELS = os.environ.get("LABELS", "bug,daily")  # ALL of these must be present
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))  # 5 min
STATE_FILE = os.environ.get("STATE_FILE", "seen_issues.json")
# When true, check once and exit (used by the GitHub Actions cron schedule,
# which is itself the scheduler). When false, run the internal polling loop.
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() in ("1", "true", "yes")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # optional


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# --- GitHub ------------------------------------------------------------------

def fetch_matching_issues() -> list[dict]:
    """Return open issues that have ALL of the configured labels."""
    query = {
        "state": "open",
        "labels": LABELS,          # GitHub treats comma-separated labels as AND
        "sort": "created",
        "direction": "desc",
        "per_page": "50",
    }
    url = f"https://api.github.com/repos/{REPO}/issues?" + urllib.parse.urlencode(query)

    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "telegram-issue-notifier")
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"GitHub HTTP {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return []
    except Exception as e:  # noqa: BLE001 - keep the loop alive on transient errors
        print(f"GitHub request failed: {e}", file=sys.stderr)
        return []

    # The issues endpoint also returns pull requests; filter those out.
    return [item for item in data if "pull_request" not in item]


# --- Telegram ----------------------------------------------------------------

def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode()

    req = urllib.request.Request(url, data=payload)
    req.add_header("User-Agent", "telegram-issue-notifier")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            if not body.get("ok"):
                print(f"Telegram error: {body}", file=sys.stderr)
                return False
            return True
    except Exception as e:  # noqa: BLE001
        print(f"Telegram request failed: {e}", file=sys.stderr)
        return False


def format_message(issue: dict) -> str:
    labels = ", ".join(lbl["name"] for lbl in issue.get("labels", []))
    title = issue["title"]
    return (
        f"🐛 <b>New bug+daily issue</b>\n"
        f"<b>#{issue['number']}</b>: {title}\n"
        f"Labels: {labels}\n"
        f"By: {issue['user']['login']}\n"
        f"{issue['html_url']}"
    )


# --- State -------------------------------------------------------------------

def load_seen() -> set[int]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[int]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


# --- Main loop ---------------------------------------------------------------

def check_once(seen: set[int]) -> set[int]:
    issues = fetch_matching_issues()
    new_seen = set(seen)

    first_run = len(seen) == 0
    for issue in issues:
        number = issue["number"]
        if number in seen:
            continue
        new_seen.add(number)
        if first_run:
            # On the very first run, record existing issues silently so we
            # don't spam a backlog. Comment this out if you want the backlog.
            continue
        if send_telegram(format_message(issue)):
            print(f"Notified: #{number} {issue['title']}")

    if new_seen != seen:
        save_seen(new_seen)
    return new_seen


def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        fail("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")

    seen = load_seen()

    if RUN_ONCE:
        print(f"Checking {REPO} once for new issues labeled '{LABELS}'.")
        check_once(seen)
        return

    print(f"Watching {REPO} for new issues labeled '{LABELS}' "
          f"every {POLL_INTERVAL_SECONDS}s. Ctrl+C to stop.")
    while True:
        seen = check_once(seen)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
