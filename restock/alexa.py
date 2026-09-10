"""Reading the Alexa shopping list.

Amazon turned off List Skills and the List Management REST API on 2024-07-01,
so there is no supported programmatic route to these lists. What remains is
the page a *customer* sees, at `/alexaquantum/sp/alexaShoppingList`, which is
why this module reads a signed-in browser session like the Tesco side does.

Confirmed DOM model (see `pages/alexa-list.html`):

* Each row carries `<p class="... item-title">text</p>` and a sibling
  `<div class="item-meta">Sam Added 31 days ago</div>`. The `sc-*` classes
  around them are styled-components hashes and change on every Amazon deploy —
  match on `item-title` / `item-meta` only.
* Rows are **virtualised**: the container positions them absolutely and only
  renders what is on screen, so a long list has to be scrolled to be read in
  full. `fetch_list` scrolls until the set of items stops growing.
* The list has "Active items" and "Completed items" tabs. Only active items
  are a shopping list; completed ones are history.

The text is whatever the voice transcription produced, so it is messy by
nature — "a. a. batteries" for AA batteries. Nothing here tries to clean that
up; matching is a separate concern.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass

from . import browser, config

ITEM_TITLE_RE = re.compile(r'class="[^"]*\bitem-title\b[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
ITEM_META_RE = re.compile(r'class="[^"]*\bitem-meta\b[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")

# "Added 31 days ago", "Sam Added 2 weeks ago", "Added today".
AGE_RE = re.compile(
    r"(?:(\d+)\s*(minute|hour|day|week|month|year)s?\s*ago|(today|yesterday))", re.I
)
_AGE_IN_DAYS = {
    "minute": 0.0,
    "hour": 0.0,
    "day": 1.0,
    "week": 7.0,
    "month": 30.0,
    "year": 365.0,
}


@dataclass(frozen=True)
class ListItem:
    """One line of the Alexa shopping list, as spoken.

    `age_days` is None when the "Added ..." note could not be read. That is
    reported rather than treated as old: silently dropping something you did
    ask for is worse than including something you didn't.
    """

    text: str
    age_days: int | None = None


def parse_age_days(meta: str) -> int | None:
    """Turn an "Added ..." note into a whole number of days, or None."""
    match = AGE_RE.search(meta)
    if not match:
        return None
    if match.group(3):
        return 0 if match.group(3).lower() == "today" else 1
    count, unit = int(match.group(1)), match.group(2).lower()
    return int(count * _AGE_IN_DAYS[unit])


def parse_list(html: str) -> list[ListItem]:
    """Read the rendered list into data. Pure; unit-tested against fixtures.

    Each row's age comes from the `item-meta` note that follows its title, so
    titles and metas are paired by position rather than zipped — a row whose
    meta is missing must not steal the next row's.
    """
    titles = list(ITEM_TITLE_RE.finditer(html))
    metas = list(ITEM_META_RE.finditer(html))

    items: list[ListItem] = []
    seen: set[str] = set()
    for n, match in enumerate(titles):
        text = " ".join(html_mod.unescape(TAG_RE.sub("", match.group(1))).split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)

        # The row's own meta: after this title, before the next one.
        limit = titles[n + 1].start() if n + 1 < len(titles) else len(html)
        age = None
        for meta in metas:
            if match.end() <= meta.start() < limit:
                age = parse_age_days(
                    " ".join(html_mod.unescape(TAG_RE.sub("", meta.group(1))).split())
                )
                break
        items.append(ListItem(text, age))
    return items


def fetch_list(page, max_age_days: int | None = None) -> list[ListItem]:
    """Read the active Alexa shopping list from a signed-in Amazon session.

    Scrolls until no new items appear, because the row container is virtualised
    and a single read would silently return only the visible slice.

    `max_age_days` drops items added longer ago than that. Items whose age
    could not be read are kept, not dropped.
    """
    browser.goto(page, config.ALEXA_LIST_URL)
    page.wait_for_timeout(6000)

    found: dict[str, ListItem] = {}
    stalled = 0
    while stalled < 3:
        before = len(found)
        for item in parse_list(page.content()):
            found.setdefault(item.text.lower(), item)
        if len(found) == before:
            stalled += 1
        else:
            stalled = 0
            config.vprint(f"{len(found)} items so far")
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(1200)

    items = list(found.values())
    if max_age_days is None:
        return items
    return [
        i for i in items if i.age_days is None or i.age_days <= max_age_days
    ]
