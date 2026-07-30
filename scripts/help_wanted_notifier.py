#!/usr/bin/env python3
"""
Telegram notifier: fires when the `Help Wanted` label is ADDED to an issue.

How it works
------------
Instead of asking "which issues currently have this label?" (which cannot tell
you *when* the label appeared), this polls the repository's issue-event stream:

    GET /repos/{owner}/{repo}/issues/events

That stream contains one `labeled` event per label application, newest first,
with the exact timestamp and who did it. The script keeps a high-water mark of
the newest event id it has processed, so:

  * old issues are never announced -- on the first run it just records the
    current newest event id and stays quiet,
  * you are notified the moment the label lands, not when the issue was created,
  * if the label is removed and re-added, that counts as a new event.

Setup
-----
1. @BotFather -> BOT TOKEN.
2. Message the bot, open https://api.telegram.org/bot<TOKEN>/getUpdates,
   copy "chat":{"id": ...} -> CHAT ID.
3. GITHUB_TOKEN: any token with no scopes. Raises the limit to 5000 req/hour.

Run
---
    export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
    export TELEGRAM_CHAT_ID="987654321"
    export GITHUB_TOKEN="ghp_..."
    python3 help_wanted_notifier.py

Only stdlib is used.
"""

import datetime as dt
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.environ.get("REPO", "Expensify/App")
LABEL = os.environ.get("LABEL", "Help Wanted")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.environ.get("STATE_FILE", "last_event.json")
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() in ("1", "true", "yes")
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "0"))
# How many pages of 100 events to walk back before giving up on catching up.
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")


# --- GitHub ------------------------------------------------------------------

def fetch_events(page: int):
    """One page of repository issue events, newest first. None on failure."""
    query = urllib.parse.urlencode({"per_page": "100", "page": str(page)})
    url = f"https://api.github.com/repos/{REPO}/issues/events?{query}"

    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "help-wanted-notifier")
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            reset = (exc.headers or {}).get("x-ratelimit-reset", "?")
            print(f"Rate limited (resets at {reset}). Set GITHUB_TOKEN or poll slower.",
                  file=sys.stderr)
        else:
            print(f"GitHub HTTP {exc.code}: {exc.read().decode()[:200]}", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001 - transient errors must not kill the loop
        print(f"GitHub request failed: {exc}", file=sys.stderr)
        return None


def collect_new_labelings(last_event_id: int):
    """
    Walk back through the event stream until we reach last_event_id.
    Returns (matching_events_oldest_first, newest_event_id_seen).
    """
    wanted = LABEL.lower()
    matches = []
    newest_id = last_event_id

    for page in range(1, MAX_PAGES + 1):
        events = fetch_events(page)
        if events is None:
            # Request failed. Return what we have; the mark only advances for
            # events we actually processed, so nothing is silently skipped.
            return list(reversed(matches)), newest_id
        if not events:
            break

        reached_known = False
        for event in events:
            event_id = event.get("id", 0)
            newest_id = max(newest_id, event_id)

            if event_id <= last_event_id:
                reached_known = True
                break

            if event.get("event") != "labeled":
                continue
            if (event.get("label") or {}).get("name", "").lower() != wanted:
                continue

            issue = event.get("issue") or {}
            if "pull_request" in issue:
                continue  # it's a PR, not an issue
            matches.append(event)

        if reached_known:
            break
    else:
        print(f"Walked {MAX_PAGES} pages without catching up - some events may "
              "have been missed. Lower POLL_INTERVAL_SECONDS or raise MAX_PAGES.",
              file=sys.stderr)

    return list(reversed(matches)), newest_id


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
    req.add_header("User-Agent", "help-wanted-notifier")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            if not body.get("ok"):
                print(f"Telegram error: {body}", file=sys.stderr)
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram request failed: {exc}", file=sys.stderr)
        return False


def age(timestamp: str) -> str:
    try:
        moment = dt.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except (ValueError, TypeError):
        return "just now"
    seconds = int((dt.datetime.now(dt.timezone.utc) - moment).total_seconds())
    if seconds < 90:
        return f"{max(seconds, 0)}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"


def format_message(event: dict) -> str:
    issue = event["issue"]
    # Titles regularly contain <, > and & - escaping is required or Telegram
    # rejects the message with a parse error.
    title = html.escape(issue["title"])
    actor = html.escape((event.get("actor") or {}).get("login", "someone"))
    return (
        f"🙋 <b>{html.escape(LABEL)}</b> · {age(event.get('created_at', ''))}\n"
        f"<b>#{issue['number']}</b>: {title}\n"
        f"Labeled by: {actor}\n"
        f"{issue['html_url']}"
    )


# --- State -------------------------------------------------------------------

def load_last_event_id() -> int:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            return int(json.load(handle).get("last_event_id", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, AttributeError):
        return 0


def save_last_event_id(event_id: int) -> None:
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"last_event_id": event_id}, handle)
    os.replace(tmp, STATE_FILE)  # atomic: never leave a half-written state file


# --- Main --------------------------------------------------------------------

def check_once() -> None:
    last_id = load_last_event_id()
    first_run = last_id == 0

    events, newest_id = collect_new_labelings(last_id)

    if first_run:
        # Start from now. Nothing that happened before this moment is announced.
        if newest_id:
            save_last_event_id(newest_id)
            print(f"Starting from event {newest_id}. Existing issues ignored.")
        return

    for event in events:
        if send_telegram(format_message(event)):
            print(f"Notified #{event['issue']['number']}: {event['issue']['title'][:80]}")

    if newest_id > last_id:
        save_last_event_id(newest_id)


def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", file=sys.stderr)
        sys.exit(1)
    if not GITHUB_TOKEN:
        print("WARNING: no GITHUB_TOKEN - the 60 req/hour anonymous limit is too "
              "low for short poll intervals.", file=sys.stderr)

    if RUN_ONCE:
        check_once()
        return

    deadline = time.monotonic() + MAX_RUNTIME_SECONDS if MAX_RUNTIME_SECONDS else None
    print(f"Watching {REPO} for the '{LABEL}' label every {POLL_INTERVAL_SECONDS}s.")

    while True:
        check_once()
        if deadline and (deadline - time.monotonic()) <= POLL_INTERVAL_SECONDS:
            print("Runtime limit reached, exiting cleanly.")
            return
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
