"""Reading past orders and distilling them into a list of regular products.

The shape of the data, confirmed by probing the live site (see `pages/` after
running `restock probe`):

* `/shop/en-GB/orders/recent` is the "Past orders" tab — a paginated list, 10
  orders per page, navigated with `?page=N`. Each order tile links to
  `/shop/en-GB/orders/<order-id>/receipt` via `data-testid="view-items"`.
* A receipt lists every line item. Each carries `data-testid="product-title"`
  wrapping an `<a href=".../products/<product-id>">Name</a>`, followed by a
  price and a `Quantity: N` fragment (React splits that text with comment
  markers, hence the tolerant regex).

Parsing is deliberately split from fetching: the `parse_*` functions are pure
string->data and are unit-tested against saved fixtures, so selector drift can
be diagnosed without a browser or a live session.
"""

from __future__ import annotations

import html as html_mod
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

RECEIPT_HREF_RE = re.compile(r'href="/shop/en-GB/orders/([0-9][0-9-]*)/receipt"')

# One product line on a receipt: the title block, then (within the same tile)
# the product link and a quantity. We scan title-block by title-block rather
# than with one big regex so that a markup change breaks one field, not all.
PRODUCT_TITLE_RE = re.compile(r'data-testid="product-title"')
PRODUCT_LINK_RE = re.compile(
    r'href="[^"]*?/products/(\d{6,})"[^>]*>(.*?)</a>', re.DOTALL
)
# React interleaves comment markers into text nodes: `Quantity<!-- -->: <!-- -->1`
QUANTITY_RE = re.compile(r"Quantity(?:<!--\s*-->)?\s*:?\s*(?:<!--\s*-->)?\s*(\d+)")

# How far past a title block to look for that item's quantity. Generous enough
# to clear the price markup, tight enough not to reach the next product.
_ITEM_WINDOW = 1600

TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Item:
    """A single line on an order receipt."""

    product_id: str
    name: str
    quantity: int


@dataclass
class Regular:
    """A product bought often enough to be worth pre-filling."""

    product_id: str
    name: str
    quantity: int
    order_count: int
    total_orders: int
    other_names: list[str] = field(default_factory=list)

    @property
    def frequency(self) -> float:
        return self.order_count / self.total_orders if self.total_orders else 0.0


def _text(fragment: str) -> str:
    """Strip tags/entities from an HTML fragment and collapse whitespace."""
    return " ".join(html_mod.unescape(TAG_RE.sub("", fragment)).split())


def parse_order_ids(html: str) -> list[str]:
    """Return the order ids linked from a "Past orders" page, in page order."""
    seen: dict[str, None] = {}
    for match in RECEIPT_HREF_RE.finditer(html):
        seen.setdefault(match.group(1), None)
    return list(seen)


def parse_receipt(html: str) -> list[Item]:
    """Return the line items on an order receipt page.

    Items are keyed by Tesco product id, which is stable across renames and
    repackaging — the reason we never match products by name.
    """
    items: list[Item] = []
    seen: set[str] = set()
    for title in PRODUCT_TITLE_RE.finditer(html):
        window = html[title.end() : title.end() + _ITEM_WINDOW]
        link = PRODUCT_LINK_RE.search(window)
        if not link:
            continue
        product_id = link.group(1)
        if product_id in seen:
            # The receipt repeats a product's id in image/price sub-blocks;
            # only the first title block per product is the real line item.
            continue
        seen.add(product_id)
        quantity = QUANTITY_RE.search(window[link.end() :])
        items.append(
            Item(
                product_id=product_id,
                name=_text(link.group(2)),
                quantity=int(quantity.group(1)) if quantity else 1,
            )
        )
    return items


def aggregate(orders: list[list[Item]], min_orders: int = 2) -> list[Regular]:
    """Distil per-order line items into a list of regulars.

    A product is "regular" if it appears in at least `min_orders` of the orders
    considered. The suggested quantity is the *modal* quantity across those
    orders, not the mean: groceries come in whole units, and the number you
    reach for most weeks is a better default than an average dragged upward by
    the occasional bulk buy.
    """
    order_count: Counter[str] = Counter()
    quantities: defaultdict[str, Counter[int]] = defaultdict(Counter)
    names: defaultdict[str, Counter[str]] = defaultdict(Counter)

    for items in orders:
        for item in items:
            order_count[item.product_id] += 1
            quantities[item.product_id][item.quantity] += 1
            names[item.product_id][item.name] += 1

    total = len(orders)
    regulars = [
        Regular(
            product_id=pid,
            # Most frequently seen spelling wins; Tesco renames products over time.
            name=names[pid].most_common(1)[0][0],
            quantity=quantities[pid].most_common(1)[0][0],
            order_count=count,
            total_orders=total,
            other_names=[n for n, _ in names[pid].most_common()[1:]],
        )
        for pid, count in order_count.items()
        if count >= min_orders
    ]
    # Most-bought first, so the top of the file is the part worth trusting.
    regulars.sort(key=lambda r: (-r.order_count, r.name.lower()))
    return regulars


# --- Finding size variants -------------------------------------------------

# Tokens that describe *how much*, not *what*: stripping them makes
# "Tesco Strawberries 400G" and "Tesco Strawberries 600G" the same thing.
SIZE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:g|kg|ml|l|cl|litre|litres|pint|pints)\b"
    r"|\b\d+\s*(?:x|pack|packs|pk|rolls?|sheets?|each|pieces?|ct)\b"
    r"|\bx\s*\d+\b|\(\s*\d+[a-z]*\s*\)|\b\d+\b",
    re.I,
)
# Words that mark a variant of the same product rather than a different one.
QUALIFIER_RE = re.compile(
    r"\b(maxi|mini|large|small|big|twin|jumbo|value|supersized|long|half|side|"
    r"giant|family|single|multipack|loose|punnet|whole)\b",
    re.I,
)
NOISE_RE = re.compile(r"\b(pack|each|rolls?|sheets?|approx|approximately)\b", re.I)


@dataclass
class VariantGroup:
    """Products that look like the same thing in different sizes."""

    label: str
    members: list[tuple[Item, int]]   # (an example item, orders containing it)
    orders_with_any: int
    orders_with_two: int


def product_stem(name: str) -> str:
    """Reduce a product name to what it *is*, dropping how much of it.

    "Tesco 2 Boneless Salmon Fillets 260G" and
    "Tesco Boneless Salmon Fillets 4 Pack 520g" both reduce to
    "tesco boneless salmon fillets".
    """
    stem = SIZE_RE.sub(" ", name.lower())
    stem = QUALIFIER_RE.sub(" ", stem)
    stem = NOISE_RE.sub(" ", stem)
    stem = re.sub(r"[^a-z& ]", " ", stem)
    return " ".join(stem.split())


def variant_groups(
    receipts: dict[str, list[Item]], min_orders: int = 3
) -> list[VariantGroup]:
    """Group past purchases into candidate size choices.

    `orders_with_two` is the tell worth reading: if two members never appear in
    the same order, you were choosing between them. It cannot, however,
    distinguish that from a progression you moved through over time — sizes
    1 -> 2 -> 3, bought in that order and never revisited, also never co-occur.
    That call stays with the human, which is why nothing here writes to the
    regulars file.
    """
    stems: defaultdict[str, dict[str, Item]] = defaultdict(dict)
    counts: Counter[str] = Counter()
    per_order: dict[str, set[str]] = {}

    for order_id, items in receipts.items():
        per_order[order_id] = {i.product_id for i in items}
        for item in items:
            stems[product_stem(item.name)][item.product_id] = item
            counts[item.product_id] += 1

    groups: list[VariantGroup] = []
    for stem, members in stems.items():
        if len(members) < 2:
            continue
        ids = set(members)
        with_any = sum(1 for pids in per_order.values() if pids & ids)
        if with_any < min_orders:
            continue
        with_two = sum(1 for pids in per_order.values() if len(pids & ids) >= 2)
        groups.append(
            VariantGroup(
                label=stem,
                members=sorted(
                    ((item, counts[pid]) for pid, item in members.items()),
                    key=lambda pair: -pair[1],
                ),
                orders_with_any=with_any,
                orders_with_two=with_two,
            )
        )
    groups.sort(key=lambda g: (-g.orders_with_any, g.orders_with_two))
    return groups


# --- Fetching (needs a live, logged-in page) ------------------------------


def fetch_order_ids(page, max_orders: int) -> list[str]:
    """Collect up to `max_orders` past-order ids, newest first.

    The "Past orders" tab shows 10 orders per page. Its pagination is
    client-side — a `?page=N` query string is *ignored*, silently re-serving
    page 1 — so we advance by clicking the "Next Page" control, and verify we
    actually moved by checking that new order ids appeared.
    """
    from . import browser, config

    browser.goto(page, f"{config.ORDERS_URL}/recent")
    page.wait_for_timeout(3000)

    ids: list[str] = []
    page_no = 1
    while len(ids) < max_orders:
        found = parse_order_ids(page.content())
        new = [i for i in found if i not in ids]
        if not new:
            config.vprint(f"page {page_no} added no new orders; stopping")
            break
        ids.extend(new)
        config.vprint(f"page {page_no}: {len(new)} orders (total {len(ids)})")
        if len(ids) >= max_orders:
            break
        if not _click_next_page(page):
            config.vprint("no further pages of orders")
            break
        page_no += 1
    return ids[:max_orders]


def _click_next_page(page) -> bool:
    """Advance the past-orders list by one page. False if there isn't one."""
    from . import config

    try:
        nxt = page.get_by_label("Next Page")
        if not nxt.is_visible() or nxt.is_disabled():
            return False
        nxt.click()
        # Client-side render: no navigation event to wait on, so settle instead.
        page.wait_for_timeout(config.PAGE_PAUSE_MS)
        return True
    except Exception as exc:
        config.vprint(f"next-page click failed: {type(exc).__name__}: {exc}")
        return False


def fetch_receipt(page, order_id: str, use_cache: bool = True) -> list[Item]:
    """Fetch and parse one order's receipt, caching the result.

    A delivered order's receipt is immutable, so a cache hit is as good as a
    fetch — and every hit is one fewer request against Tesco's edge.
    """
    from . import browser, config

    cache = config.RECEIPT_CACHE_DIR / f"{order_id}.json"
    if use_cache and cache.exists():
        config.vprint(f"cache hit: {order_id}")
        return [Item(**row) for row in json.loads(cache.read_text(encoding="utf-8"))]

    browser.goto(page, f"{config.BASE_URL}/shop/en-GB/orders/{order_id}/receipt")
    page.wait_for_timeout(3000)
    items = parse_receipt(page.content())

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps([asdict(i) for i in items], indent=1), encoding="utf-8"
    )
    return items


def cached_receipts() -> dict[str, list[Item]]:
    """Every receipt already on disk, keyed by order id. No network."""
    from . import config

    out: dict[str, list[Item]] = {}
    if not config.RECEIPT_CACHE_DIR.exists():
        return out
    for path in sorted(config.RECEIPT_CACHE_DIR.glob("*.json")):
        rows = json.loads(path.read_text(encoding="utf-8"))
        out[path.stem] = [Item(**row) for row in rows]
    return out
