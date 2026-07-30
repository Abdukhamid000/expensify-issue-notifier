#!/usr/bin/env python3
"""
Telegram notifier for Expensify/App GitHub issues.

Watches several *label groups* at once (e.g. `Bug`+`Daily`, and `Help Wanted`)
and sends a Telegram message the moment an issue enters one of those groups --
including when the label is added to an issue that already existed.

Why this version is faster than the previous one
------------------------------------------------
1. Sorts by `updated` instead of `created`. When a label is added to an old
   issue, its `created_at` does not change, so a `created`-sorted page of 50
   would never show it. `updated_at` *does* change on labeling.
2. Uses the `since` parameter, so each poll only asks for issues touched since
   the last check -> tiny responses -> safe to poll every 30-60 seconds.
3. Tracks seen issues *per group*, so an issue already announced as Bug+Daily
   is announced again when `Help Wanted` is later added to it.
4. Optionally reports how old the labeling event is, so you can see whether the
   remaining lag is this script or something upstream.

Setup
-----
1. Create a bot with @BotFather -> BOT TOKEN.
2. Message the bot, open https://api.telegram.org/bot<TOKEN>/getUpdates,
   copy "chat":{"id": ...} -> CHAT ID.
3. A GitHub token is now effectively required: 60 req/hr unauthenticated is not
   enough for minute-level polling. A classic token with no scopes is fine.

Run
---
    export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
    export TELEGRAM_CHAT_ID="987654321"
    export GITHUB_TOKEN="ghp_..."
    export POLL_INTERVAL_SECONDS=60
    python3 telegram_issue_notifier.py

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

# --- Configuration -----------------------------------------------------------

REPO = os.environ.get("REPO", "Expensify/App")

# Each group is an AND-set of labels. An issue matching a group triggers one
# notification for that group. Override with the WATCH_GROUPS env var (JSON).
DEFAULT_WATCH_GROUPS = [
    {"name": "bug-daily", "labels": ["Bug", "Daily"], "emoji": "🐛"},
    {"name": "help-wanted", "labels": ["Help Wanted"], "emoji": "🙋"},
]

try:
    WATCH_GROUPS = json.loads(os.environ["WATCH_GROUPS"])
except KeyError:
    WATCH_GROUPS = DEFAULT_WATCH_GROUPS
except json.JSONDecodeError as exc:
    print(f"ERROR: WATCH_GROUPS is not valid JSON: {exc}", file=sys.stderr)
    sys.exit(1)

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
# Re-scan a little further back than the last check, so nothing falls through
# the gap between two polls (clock skew, slow indexing, a missed run).
LOOKBACK_BUFFER_SECONDS = int(os.environ.get("LOOKBACK_BUFFER_SECONDS", "180"))
STATE_FILE = os.environ.get("STATE_FILE", "seen_issues.json")
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() in ("1", "true", "yes")
# Stop the polling loop after this many seconds (0 = run forever). Used to keep
# a long-poll inside a single CI job that must finish before the next one starts.
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "0"))
# Ask GitHub when the label was actually added (1 extra request per new issue).
SHOW_LABEL_LAG = os.environ.get("SHOW_LABEL_LAG", "true").lower() in ("1", "true", "yes")
# On a brand new state file: record what already exists without notifying.
SEED_SILENTLY = os.environ.get("SEED_SILENTLY", "true").lower() in ("1", "true", "yes")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except (ValueError, TypeError):
        return None


# --- GitHub ------------------------------------------------------------------

def gh_get(path_and_query: str):
    """GET the GitHub API. Returns (parsed_json, headers) or (None, headers)."""
    url = f"https://api.github.com{path_and_query}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "telegram-issue-notifier")
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers or {})
        if exc.code in (403, 429):
            remaining = headers.get("x-ratelimit-remaining")
            reset = headers.get("x-ratelimit-reset")
            print(
                f"GitHub rate limited (remaining={remaining}, reset={reset}). "
                "Set GITHUB_TOKEN or raise POLL_INTERVAL_SECONDS.",
                file=sys.stderr,
            )
        else:
            print(f"GitHub HTTP {exc.code}: {exc.read().decode()[:200]}", file=sys.stderr)
        return None, headers
    except Exception as exc:  # noqa: BLE001 - keep the loop alive
        print(f"GitHub request failed: {exc}", file=sys.stderr)
        return None, {}


def fetch_group_issues(group: dict, since_iso: str | None) -> list[dict]:
    """Open issues carrying ALL labels of the group, most recently touched first."""
    query = {
        "state": "open",
        "labels": ",".join(group["labels"]),  # comma-separated == AND
        "sort": "updated",
        "direction": "desc",
        "per_page": "100",
    }
    if since_iso:
        query["since"] = since_iso

    data, _ = gh_get(f"/repos/{REPO}/issues?" + urllib.parse.urlencode(query))
    if not isinstance(data, list):
        return []
    # The issues endpoint also returns pull requests; drop them.
    return [item for item in data if "pull_request" not in item]


def labeled_at(issue_number: int, wanted_labels: list[str]) -> dt.datetime | None:
    """When was the last of the group's labels applied? None if unknown."""
    data, _ = gh_get(f"/repos/{REPO}/issues/{issue_number}/events?per_page=100")
    if not isinstance(data, list):
        return None

    wanted = {name.lower() for name in wanted_labels}
    newest = None
    for event in data:
        if event.get("event") != "labeled":
            continue
        name = (event.get("label") or {}).get("name", "").lower()
        if name not in wanted:
            continue
        moment = parse_iso(event.get("created_at", ""))
        if moment and (newest is None or moment > newest):
            newest = moment
    return newest


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
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram request failed: {exc}", file=sys.stderr)
        return False


def human_age(delta: dt.timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"


def format_message(issue: dict, group: dict, applied: dt.datetime | None) -> str:
    # Issue titles regularly contain <, > and & -- escaping is required or
    # Telegram rejects the message with a parse error.
    title = html.escape(issue["title"])
    author = html.escape(issue["user"]["login"])
    labels = html.escape(", ".join(lbl["name"] for lbl in issue.get("labels", [])))
    watched = html.escape(" + ".join(group["labels"]))

    lines = [
        f"{group.get('emoji', '🔔')} <b>{watched}</b>",
        f"<b>#{issue['number']}</b>: {title}",
        f"Labels: {labels}",
        f"By: {author}",
    ]
    if applied is not None:
        lines.append(f"Labeled: {human_age(utcnow() - applied)}")
    lines.append(issue["html_url"])
    return "\n".join(lines)


# --- State -------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 2, "seen": {}, "last_check": None}

    # Migrate the old format: a flat list of issue numbers.
    if isinstance(raw, list):
        first = WATCH_GROUPS[0]["name"] if WATCH_GROUPS else "default"
        return {"version": 2, "seen": {first: list(raw)}, "last_check": None}

    raw.setdefault("seen", {})
    raw.setdefault("last_check", None)
    return raw


def save_state(state: dict) -> None:
    serializable = {
        "version": 2,
        "seen": {name: sorted(numbers) for name, numbers in state["seen"].items()},
        "last_check": state.get("last_check"),
    }
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(serializable, handle, indent=0)
    os.replace(tmp, STATE_FILE)  # atomic: never leave a half-written state file


# --- Main loop ---------------------------------------------------------------

def check_once(state: dict) -> dict:
    started = utcnow()
    last_check = parse_iso(state.get("last_check") or "")
    since = None
    if last_check:
        since = iso(last_check - dt.timedelta(seconds=LOOKBACK_BUFFER_SECONDS))

    for group in WATCH_GROUPS:
        name = group["name"]
        seen = set(state["seen"].get(name, []))
        first_run = name not in state["seen"]

        # On the first run for a group, look at the whole current backlog
        # (no `since`) so the state file starts out complete.
        issues = fetch_group_issues(group, None if first_run else since)

        for issue in issues:
            number = issue["number"]
            if number in seen:
                continue
            seen.add(number)

            if first_run and SEED_SILENTLY:
                continue

            applied = labeled_at(number, group["labels"]) if SHOW_LABEL_LAG else None
            if send_telegram(format_message(issue, group, applied)):
                print(f"[{name}] notified #{number}: {issue['title'][:80]}")

        state["seen"][name] = sorted(seen)
        if first_run and SEED_SILENTLY:
            print(f"[{name}] seeded {len(seen)} existing issues without notifying.")

    state["last_check"] = iso(started)
    save_state(state)
    return state


def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        fail("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")
    if not WATCH_GROUPS:
        fail("WATCH_GROUPS is empty - nothing to watch.")
    if not GITHUB_TOKEN:
        print(
            "WARNING: no GITHUB_TOKEN. The unauthenticated limit is 60 requests/hour, "
            "which is too low for short poll intervals.",
            file=sys.stderr,
        )

    state = load_state()
    watching = "; ".join(" + ".join(g["labels"]) for g in WATCH_GROUPS)

    if RUN_ONCE:
        print(f"Checking {REPO} once for: {watching}")
        check_once(state)
        return

    deadline = time.monotonic() + MAX_RUNTIME_SECONDS if MAX_RUNTIME_SECONDS else None
    limit = f", stopping after {MAX_RUNTIME_SECONDS}s" if deadline else ""
    print(f"Watching {REPO} for: {watching} (every {POLL_INTERVAL_SECONDS}s{limit}). Ctrl+C to stop.")

    while True:
        state = check_once(state)
        if deadline is None:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        remaining = deadline - time.monotonic()
        # Only sleep if a full poll still fits before the deadline.
        if remaining <= POLL_INTERVAL_SECONDS:
            print("Runtime limit reached, exiting cleanly.")
            return
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
