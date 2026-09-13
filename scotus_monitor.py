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
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://www.supremecourt.gov"
USER_AGENT = "SCOTUS-Publication-Monitor/1.0 (personal notification tool)"
JUSTICES = {
    "PC": "Per Curiam",
    "R": "Chief Justice Roberts",
    "T": "Justice Thomas",
    "A": "Justice Alito",
    "SS": "Justice Sotomayor",
    "EK": "Justice Kagan",
    "NG": "Justice Gorsuch",
    "BK": "Justice Kavanaugh",
    "AB": "Justice Barrett",
}
DOCKET_RE = re.compile(r"(?:\d{1,3}[A-Z]?[-–]\d+|\d{1,3}O\d+)", re.I)


@dataclass(frozen=True)
class Item:
    category: str
    title: str
    url: str
    page_url: str
    docket: str = ""
    justice: str = ""


class PDFLinkParser(HTMLParser):
    def __init__(self, page_url: str, category: str):
        super().__init__()
        self.page_url = page_url
        self.category = category
        self._href: str | None = None
        self._text: list[str] = []
        self._in_row = False
        self._in_cell = False
        self._cell_text: list[str] = []
        self._cells: list[str] = []
        self._row_links: list[tuple[str, str]] = []
        self.items: list[Item] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._in_row = True
            self._cells = []
            self._row_links = []
        elif tag in {"td", "th"} and self._in_row:
            self._in_cell = True
            self._cell_text = []
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href and re.search(r"\.pdf(?:$|[?#])", href, re.I):
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text.append(data)
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._in_cell:
            self._cells.append(" ".join(" ".join(self._cell_text).split()))
            self._in_cell = False
            self._cell_text = []
        elif tag == "a" and self._href is not None:
            link_text = " ".join(" ".join(self._text).split())
            if self._in_row:
                self._row_links.append((self._href, link_text))
            else:
                self.items.append(self._make_item(self._href, link_text, []))
            self._href = None
            self._text = []
        elif tag == "tr" and self._in_row:
            for href, link_text in self._row_links:
                self.items.append(self._make_item(href, link_text, self._cells))
            self._in_row = False
            self._cells = []
            self._row_links = []

    def _make_item(self, href: str, link_text: str, cells: list[str]) -> Item:
        url = urllib.parse.urljoin(self.page_url, href)
        docket = ""
        docket_index: int | None = None
        for index, cell in enumerate(cells):
            match = DOCKET_RE.search(cell)
            if match:
                docket = match.group(0).replace("–", "-")
                docket_index = index
                break

        justice = ""
        for cell in cells:
            code = cell.strip().upper().rstrip(".")
            if code in JUSTICES:
                justice = JUSTICES[code]
                break

        title = link_text
        if docket_index is not None and docket_index + 1 < len(cells):
            candidate = cells[docket_index + 1]
            if candidate and candidate.upper().rstrip(".") not in JUSTICES:
                title = candidate
        if not title or (docket and title == docket):
            title = docket or Path(urllib.parse.urlparse(url).path).name
        return Item(self.category, title, url, self.page_url, docket, justice)


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
    lines = [f"SUPREME COURT PUBLICATION ALERT\n{len(items)} new item{'s' if len(items) != 1 else ''}"]
    for item in items:
        lines.append(f"\n{item.category.upper()}\n{item.title}")
        if item.docket:
            lines.append(f"Docket: {item.docket}")
        if item.justice:
            lines.append(f"Opinion by: {item.justice}")
        lines.append(item.url)
    return "\n".join(lines)


def email_subject(items: list[Item]) -> str:
    if len(items) != 1:
        return f"SCOTUS Alert | {len(items)} New Publications"
    item = items[0]
    labels = {
        "Opinions": "New Opinion of the Court",
        "Orders": "New Order",
        "Opinions relating to orders": "New Opinion Relating to Orders",
    }
    suffix = f" | {item.docket}" if item.docket else ""
    return f"SCOTUS Alert | {labels.get(item.category, 'New Publication')}{suffix}"


def email_html(items: list[Item]) -> str:
    cards = []
    for item in items:
        metadata = []
        if item.docket:
            metadata.append(
                f'<tr><td style="padding:5px 14px 5px 0;color:#667085;">Docket</td>'
                f'<td style="padding:5px 0;font-weight:600;">{html.escape(item.docket)}</td></tr>'
            )
        if item.justice:
            metadata.append(
                f'<tr><td style="padding:5px 14px 5px 0;color:#667085;">Opinion by</td>'
                f'<td style="padding:5px 0;font-weight:600;">{html.escape(item.justice)}</td></tr>'
            )
        cards.append(f'''
          <div style="border:1px solid #d0d5dd;border-radius:8px;padding:22px;margin:0 0 18px;">
            <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#6941c6;">
              {html.escape(item.category)}
            </div>
            <h2 style="font-family:Georgia,serif;font-size:21px;line-height:1.35;margin:8px 0 12px;color:#101828;">
              {html.escape(item.title)}
            </h2>
            <table role="presentation" style="font-size:14px;color:#344054;margin-bottom:16px;">
              {''.join(metadata)}
            </table>
            <a href="{html.escape(item.url, quote=True)}"
               style="display:inline-block;background:#1d2939;color:#fff;text-decoration:none;padding:10px 16px;border-radius:5px;font-weight:700;">
              View official PDF
            </a>
          </div>''')
    return f'''<!doctype html>
<html><body style="margin:0;background:#f2f4f7;font-family:Arial,sans-serif;color:#101828;">
  <div style="max-width:680px;margin:0 auto;padding:28px 14px;">
    <div style="background:#101828;color:#fff;padding:24px 28px;border-radius:8px 8px 0 0;">
      <div style="font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#d0d5dd;">SCOTUS Wire</div>
      <div style="font-family:Georgia,serif;font-size:28px;margin-top:5px;">Supreme Court Publication Alert</div>
    </div>
    <div style="background:#fff;padding:28px;border-radius:0 0 8px 8px;">
      <p style="margin:0 0 22px;color:#475467;">The Supreme Court has published {len(items)} new item{'s' if len(items) != 1 else ''} on a monitored page.</p>
      {''.join(cards)}
      <p style="font-size:12px;color:#98a2b3;margin:22px 0 0;">Automated alert based on the official Supreme Court website.</p>
    </div>
  </div>
</body></html>'''


def send_email(items: list[Item]) -> None:
    required = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_TO"]
    if not all(os.getenv(key) for key in required):
        return
    sender = os.getenv("EMAIL_FROM", os.environ["SMTP_USERNAME"])
    msg = EmailMessage()
    msg["Subject"] = email_subject(items)
    msg["From"] = sender
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(alert_text(items))
    msg.add_alternative(email_html(items), subtype="html")
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
    updated_seen = old_seen | {item.url for item in all_items}
    # Avoid a repository commit on every scheduled check. Persist only the
    # initial baseline or a genuinely new PDF URL.
    if first_run or updated_seen != old_seen:
        save_state(args.state, updated_seen)
    if first_run and not args.notify_initial:
        print("Baseline created; alerts begin with the next new posting.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (urllib.error.URLError, smtplib.SMTPException, RuntimeError) as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
