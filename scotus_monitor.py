#!/usr/bin/env python3
"""Monitor Supreme Court publication pages and send email/SMS alerts."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://www.supremecourt.gov"
DEFAULT_PAGES = {
    "Opinions": f"{BASE}/opinions/slipopinion/{str(datetime.now().year)[-2:]}",
    "Orders": f"{BASE}/orders/ordersofthecourt",
    "Opinions relating to orders": f"{BASE}/opinions/relatingtoorders/{str(datetime.now().year)[-2:]}",
}
USER_AGENT = "SCOTUS-Publication-Monitor/1.0 (personal notification tool)"


@dataclass(frozen=True)
class Item:
    category: str
    title: str
    url: str
    page_url: str


class PDFLinkParser(HTMLParser):
    def __init__(self, page_url: str, category: str):
        super().__init__()
        self.page_url = page_url
        self.category = category
        self._href: str | None = None
        self._text: list[str] = []
        self.items: list[Item] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href and re.search(r"\.pdf(?:$|[?#])", href, re.I):
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        url = urllib.parse.urljoin(self.page_url, self._href)
        title = " ".join(" ".join(self._text).split())
        if not title:
            title = Path(urllib.parse.urlparse(url).path).name
        self.items.append(Item(self.category, title, url, self.page_url))
        self._href = None
        self._text = []


def term_year() -> str:
    """Return the two-digit Supreme Court term start year (term begins in October)."""
    now = datetime.now()
    year = now.year if now.month >= 10 else now.year - 1
    return str(year)[-2:]


def monitored_pages() -> dict[str, str]:
    term = term_year()
    return {
        "Opinions": f"{BASE}/opinions/slipopinion/{term}",
        "Orders": f"{BASE}/orders/ordersofthecourt",
        "Opinions relating to orders": f"{BASE}/opinions/relatingtoorders/{term}",
    }


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_items(page_html: str, page_url: str, category: str) -> list[Item]:
    parser = PDFLinkParser(page_url, category)
    parser.feed(page_html)
    unique: dict[str, Item] = {}
    for item in parser.items:
        # Ignore generic site-wide PDFs that occasionally appear in navigation.
        path = urllib.parse.urlparse(item.url).path.lower()
        expected = {
            "Opinions": "/opinions/",
            "Orders": "/orders/",
            "Opinions relating to orders": "/opinions/",
        }[category]
        if expected in path:
            unique[item.url] = item
    return list(unique.values())


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"seen": [], "last_checked": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("seen"), list):
            raise ValueError("state.seen must be a list")
        return data
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read state file {path}: {exc}") from exc


def save_state(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "seen": sorted(seen),
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def alert_text(items: list[Item]) -> str:
    lines = [f"SCOTUS posted {len(items)} new item{'s' if len(items) != 1 else ''}:"]
    for item in items:
        lines.extend([f"\n[{item.category}] {item.title}", item.url])
    return "\n".join(lines)


def send_email(items: list[Item]) -> None:
    required = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_TO"]
    if not all(os.getenv(key) for key in required):
        return
    sender = os.getenv("EMAIL_FROM", os.environ["SMTP_USERNAME"])
    msg = EmailMessage()
    msg["Subject"] = f"SCOTUS alert: {len(items)} new posting{'s' if len(items) != 1 else ''}"
    msg["From"] = sender
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(alert_text(items))
    rows = "".join(
        f'<li><strong>{html.escape(i.category)}:</strong> '
        f'<a href="{html.escape(i.url, quote=True)}">{html.escape(i.title)}</a></li>'
        for i in items
    )
    msg.add_alternative(f"<p>New Supreme Court posting(s):</p><ul>{rows}</ul>", subtype="html")
    port = int(os.getenv("SMTP_PORT", "465"))
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, context=context) as server:
        server.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)


def send_sms(items: list[Item]) -> None:
    required = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "SMS_TO"]
    if not all(os.getenv(key) for key in required):
        return
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    # Keep one text readable. Email contains the complete batch.
    first = items[0]
    more = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    body = f"SCOTUS: New {first.category}: {first.title}{more} {first.url}"
    data = urllib.parse.urlencode({
        "From": os.environ["TWILIO_FROM"],
        "To": os.environ["SMS_TO"],
        "Body": body[:1500],
    }).encode()
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    request = urllib.request.Request(endpoint, data=data, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(request, timeout=30):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("state/seen.json"))
    parser.add_argument("--notify-initial", action="store_true", help="alert for existing items on first run")
    parser.add_argument("--dry-run", action="store_true", help="print findings without alerts or state changes")
    args = parser.parse_args()

    state = load_state(args.state)
    first_run = not args.state.exists()
    old_seen = set(state["seen"])
    all_items: list[Item] = []
    errors: list[str] = []
    for category, url in monitored_pages().items():
        try:
            all_items.extend(parse_items(fetch(url), url, category))
        except Exception as exc:  # preserve successful page results, but never silently ignore failure
            errors.append(f"{category}: {exc}")

    if errors:
        print("One or more pages could not be checked; state was not changed:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    if not all_items:
        print("No PDF links found; refusing to overwrite state.", file=sys.stderr)
        return 1

    new_items = [item for item in all_items if item.url not in old_seen]
    should_alert = bool(new_items) and (not first_run or args.notify_initial)
    print(alert_text(new_items) if new_items else "No new Supreme Court postings.")

    if args.dry_run:
        return 0
    if should_alert:
        # Only mark items seen after every configured channel succeeds.
        send_email(new_items)
        send_sms(new_items)
    save_state(args.state, old_seen | {item.url for item in all_items})
    if first_run and not args.notify_initial:
        print("Baseline created; alerts begin with the next new posting.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (urllib.error.URLError, smtplib.SMTPException, RuntimeError) as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
