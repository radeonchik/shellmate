"""Distribution news since the last full upgrade, shown before updating: on Arch some updates
need manual intervention, and the news page is where that's announced. Feeds come from the
distro backend (Arch + CachyOS today; others have none yet)."""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from shellmate import backends, rag, web

MANUAL = re.compile(r"manual intervention|requires? (?:user )?action|breaking change", re.I)


@dataclass
class Item:
    source: str
    title: str
    link: str
    date: datetime
    text: str
    manual: bool  # the update needs you to do something by hand
    affects: list[str]  # installed packages the item names


def last_upgrade() -> datetime | None:
    return backends.current().last_upgrade()


PKG = r"[a-z0-9][a-z0-9+._-]*[a-z0-9+]"


def _affected(title: str, installed: set[str]) -> list[str]:
    """Package names in Arch news titles are in backticks ("`varnish`"), before a version
    ("mkinitcpio >=42", "kea >= 1:3.0") or the first word — plain words like "linux" or
    "which" in "Arch Linux …, which is renamed" are not packages."""
    low = title.lower()
    cands = re.findall(rf"`({PKG})`", low) + re.findall(rf"({PKG})\s*[<>]?=", low)
    first = re.match(PKG, low)
    if first:
        cands.append(first.group(0))
    return [w for w in dict.fromkeys(cands) if w in installed and len(w) > 2]


def since(when: datetime | None, limit: int = 8) -> list[Item]:
    installed = backends.current().installed_names()
    items = []
    for source, url in backends.current().news_feeds():
        try:
            _, body = web.get(url, 10)
            root = ET.fromstring(body)
        except Exception:  # one feed down must not block the update
            continue
        for it in root.iter("item"):
            try:
                date = parsedate_to_datetime(it.findtext("pubDate") or "")
            except (TypeError, ValueError):
                continue
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if when and date <= when:
                continue
            title = (it.findtext("title") or "").strip()
            text = rag.html_to_text(it.findtext("description") or "")
            items.append(Item(source, title, it.findtext("link") or "", date, text,
                              bool(MANUAL.search(title) or MANUAL.search(text[:400])),
                              _affected(title, installed)))
    return sorted(items, key=lambda i: i.date, reverse=True)[:limit]
