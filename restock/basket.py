"""Putting the regulars list into the Tesco basket, and clearing it again.

Confirmed DOM model (see `pages/` after `restock probe`):

* The basket is at `/shop/en-GB/trolley` — Tesco calls it the *trolley*.
  Both `/groceries/en-GB/basket` and `/shop/en-GB/basket` serve an error page.
* On a product page, the main buy controls sit inside `[data-auto="pdp-buy-box"]`.
  Scoping to it matters: the page also carries recommendation tiles with their
  own add buttons, and clicking one of those would buy the wrong thing.
* That box has an `aria-label="Quantity"` input plus either
  - a submit button (`aria-label="add N <name>"`) when the product is not yet
    in the basket, or
  - a stepper including `aria-label="remove 1 <name> from trolley"` when it is.
  The add button's aria-label is *not* reactive — it keeps saying "add 1" after
  the quantity input changes — so it must never be trusted to report quantity.
  Filling the input and then clicking does add the full amount; verified
  against the basket.
* On the basket page each line is `data-testid="product-list-item"`, and its
  quantity input is `id="quantity-controls-<product-id>"` — which is how we
  read the basket back as data.
* `#emptyBasketButton` (`data-auto="full-trolley--empty-button"`) clears it.

Nothing here checks out or books a delivery slot; both are separate steps the
human takes.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass

from . import browser, config
from .regulars import Entry, Option

# `id="quantity-controls-<product-id>" ... value="<qty>"` on the basket page.
BASKET_LINE_RE = re.compile(
    r'id="quantity-controls-(\d+)"[^>]*?value="(\d+)"', re.DOTALL
)
BASKET_NAME_RE = re.compile(
    r'href="[^"]*?/products/(\d+)"[^>]*?>([^<]{2,200})</a>'
)

BUY_BOX = '[data-auto="pdp-buy-box"]'

# Prices, read from within the buy box only — the page is full of prices for
# recommendation tiles, and comparing against one of those would be nonsense.
PRICE_RE = re.compile(r'product-tile-price__text[^>]*>\s*£([\d.]+)')
UNIT_PRICE_RE = re.compile(
    r'product-tile-unit-price__subtext[^>]*>\s*£([\d.]+)\s*/\s*([a-zA-Z]+)'
)
CLUBCARD_RE = re.compile(r'£([\d.]+)\s*Clubcard Price')


@dataclass(frozen=True)
class BasketLine:
    """One line of the basket as Tesco currently holds it."""

    product_id: str
    quantity: int
    name: str = ""


@dataclass(frozen=True)
class Price:
    """What a product costs right now, as shown on its own page."""

    price: float
    clubcard: float | None
    unit_price: float | None
    unit: str

    @property
    def effective(self) -> float:
        """What you actually pay, Clubcard included."""
        return self.clubcard if self.clubcard is not None else self.price

    @property
    def effective_unit(self) -> float | None:
        """Price per kg/litre/etc at the price you actually pay.

        Tesco shows the unit price against the *shelf* price, so a Clubcard
        discount has to be applied pro rata to compare sizes honestly.
        """
        if self.unit_price is None or not self.price:
            return None
        return self.unit_price * (self.effective / self.price)

    def describe(self) -> str:
        bits = [f"£{self.effective:.2f}"]
        if self.clubcard is not None:
            bits.append(f"(Clubcard, was £{self.price:.2f})")
        if self.effective_unit is not None:
            bits.append(f"£{self.effective_unit:.2f}/{self.unit}")
        return " ".join(bits)


@dataclass
class AddResult:
    """What happened when we tried to put one entry in the basket."""

    entry: Entry
    ok: bool
    detail: str
    chosen: Option | None = None

    @property
    def product_id(self) -> str | None:
        return self.chosen.product_id if self.chosen else None


def parse_basket(html: str) -> list[BasketLine]:
    """Read the basket page into data. Pure; unit-tested against fixtures."""
    # Unescape: these names are printed back to the user, and raw markup
    # would show "Ripe &amp; Ready" instead of "Ripe & Ready".
    names = {
        pid: html_mod.unescape(name).strip()
        for pid, name in BASKET_NAME_RE.findall(html)
    }
    lines: list[BasketLine] = []
    seen: set[str] = set()
    for product_id, quantity in BASKET_LINE_RE.findall(html):
        if product_id in seen:
            continue
        seen.add(product_id)
        lines.append(
            BasketLine(product_id, int(quantity), names.get(product_id, ""))
        )
    return lines


def read_basket(page) -> list[BasketLine]:
    """Navigate to the basket and read its current contents."""
    browser.goto(page, config.BASKET_URL)
    page.wait_for_timeout(4000)
    return parse_basket(page.content())


def add_entry(page, entry: Entry) -> AddResult:
    """Put `entry` in the basket, choosing between its options if need be."""
    if not entry.is_choice:
        return add_option(page, entry, entry.options[0])

    priced: list[tuple[Option, Price]] = []
    for option in entry.options:
        price = read_price(page, option)
        if price is None:
            config.vprint(f"no price for {option.name}; skipping as a candidate")
            continue
        config.vprint(f"  {option.name}: {price.describe()}")
        priced.append((option, price))

    if not priced:
        return AddResult(entry, False, "could not price any of the options")

    # Best value per unit, falling back to outright cheapest when Tesco gives
    # no unit price (loose produce, "each" items).
    if all(p.effective_unit is not None for _, p in priced):
        option, price = min(priced, key=lambda op: op[1].effective_unit or 0.0)
        basis = f"£{price.effective_unit:.2f}/{price.unit}"
    else:
        option, price = min(priced, key=lambda op: op[1].effective)
        basis = f"£{price.effective:.2f}"

    result = add_option(page, entry, option)
    if result.ok:
        result.detail = f"chose {option.name} at {basis} — {result.detail}"
    return result


def parse_price(html: str) -> Price | None:
    """Read the buy box's price, unit price and Clubcard price. Pure.

    Scoped to the buy box: product pages carry prices for recommendation
    tiles too, and picking one of those up would make a size comparison
    meaningless.
    """
    start = html.find('data-auto="pdp-buy-box"')
    if start == -1:
        return None
    # The shelf price and unit price sit right at the top of the box; the
    # Clubcard value bar is ~6.5k further in, while the first recommendation
    # tile's own Clubcard price is ~400k away. This window comfortably
    # separates them, and the sanity check below catches it if that changes.
    head = html[start : start + 4000]
    box = html[start : start + 20_000]

    price = PRICE_RE.search(head)
    if not price:
        return None
    shelf = float(price.group(1))
    unit = UNIT_PRICE_RE.search(head)

    clubcard: float | None = None
    match = CLUBCARD_RE.search(box)
    if match:
        candidate = float(match.group(1))
        # A "Clubcard price" that isn't cheaper is not this product's — we have
        # picked up someone else's markup. Ignoring it degrades to the shelf
        # price, which is safe; trusting it would corrupt a size comparison.
        if candidate < shelf:
            clubcard = candidate
        else:
            config.vprint(
                f"ignoring implausible Clubcard price £{candidate:.2f} vs shelf £{shelf:.2f}"
            )

    return Price(
        price=shelf,
        clubcard=clubcard,
        unit_price=float(unit.group(1)) if unit else None,
        unit=unit.group(2).lower() if unit else "each",
    )


def read_price(page, option: Option) -> Price | None:
    """Read an option's current price from its own product page."""
    url = f"{config.BASE_URL}/shop/en-GB/products/{option.product_id}"
    try:
        browser.goto(page, url)
    except browser.NavigationFailed:
        return None
    page.wait_for_timeout(2000)
    return parse_price(page.content())


def add_option(page, entry: Entry, option: Option) -> AddResult:
    """Put one specific product in the basket at `entry`'s quantity.

    Idempotent by design: if the product is already in the basket the buy box
    shows a stepper rather than an add button, and we *set* the quantity to the
    requested figure instead of adding to it. Re-running `fill` therefore
    converges on the list rather than doubling it.
    """
    url = f"{config.BASE_URL}/shop/en-GB/products/{option.product_id}"
    try:
        browser.goto(page, url)
    except browser.NavigationFailed as exc:
        return AddResult(entry, False, str(exc), option)
    page.wait_for_timeout(2500)

    # A discontinued id can redirect elsewhere; buying whatever we landed on
    # would be worse than skipping it.
    if option.product_id not in page.url:
        return AddResult(entry, False, f"page redirected to {page.url}", option)

    box = page.locator(BUY_BOX).first
    try:
        if not box.is_visible(timeout=8000):
            raise TimeoutError
    except Exception:
        return AddResult(entry, False, "no buy box — product may be unavailable", option)

    # Decide which controls we are looking at BEFORE touching anything. The
    # buy box hydrates a moment after load: check too early and a product that
    # is already in the basket still looks like a fresh one, which would send
    # us to click what is actually the stepper's increment button.
    already_in_basket = _wait_for_buy_box_state(box)
    if already_in_basket is None:
        return AddResult(entry, False, "buy box never settled into a known state", option)

    try:
        quantity_input = box.get_by_label("Quantity").first
        quantity_input.fill(str(entry.quantity))
        page.wait_for_timeout(600)

        if already_in_basket:
            # The stepper writes through on change; commit and let it settle.
            quantity_input.press("Enter")
            page.wait_for_timeout(2500)
            return AddResult(entry, True, f"already in basket, set to {entry.quantity}", option)

        box.locator("button[type=submit]").first.click()
        # Be patient here. The buy box swaps the add button for the stepper
        # only after the basket round-trips, and an impatient check reports a
        # failure for a product that did land — verified against the basket.
        if _wait_for_buy_box_state(box, timeout_ms=15_000) is not True:
            return AddResult(
                entry, False, "clicked add but basket controls did not appear", option
            )
        return AddResult(entry, True, f"added {entry.quantity}", option)
    except Exception as exc:
        return AddResult(entry, False, f"{type(exc).__name__}: {exc}", option)


def _wait_for_buy_box_state(box, timeout_ms: int = 10_000) -> bool | None:
    """Wait for the buy box to hydrate; True if in basket, False if not.

    Returns None if neither control appeared in time, so the caller can skip
    the product rather than guess at which button it is about to press.
    """
    waited = 0
    while waited < timeout_ms:
        if _in_basket(box, timeout_ms=500):
            return True
        if _has_add_button(box):
            return False
        box.page.wait_for_timeout(500)
        waited += 500
    return None


def _has_add_button(box) -> bool:
    """True if the buy box is offering a fresh add (product not in basket)."""
    try:
        return box.locator('button[aria-label^="add "]').first.is_visible(timeout=500)
    except Exception:
        return False


def _in_basket(box, timeout_ms: int = 4000) -> bool:
    """True if the buy box is showing in-basket stepper controls."""
    try:
        return box.locator('button[aria-label*="from trolley"]').first.is_visible(
            timeout=timeout_ms
        )
    except Exception:
        return False


def empty_basket(page) -> tuple[int, int]:
    """Empty the basket. Returns (lines before, lines after).

    Uses Tesco's own "Empty Basket" control rather than removing lines one at a
    time. A confirmation dialog may appear; we accept it, since emptying is the
    entire point of the command.
    """
    before = read_basket(page)
    if not before:
        return 0, 0

    button = page.locator("#emptyBasketButton").first
    if not button.is_visible(timeout=8000):
        raise RuntimeError("Could not find the Empty Basket button.")
    button.click()
    page.wait_for_timeout(2000)
    _confirm_empty(page)
    page.wait_for_timeout(4000)

    after = read_basket(page)
    return len(before), len(after)


def _confirm_empty(page) -> None:
    """Accept the "are you sure?" dialog, if Tesco shows one."""
    for name in ("Empty Basket", "Empty basket", "Yes, empty basket", "Confirm"):
        try:
            button = page.get_by_role("button", name=name, exact=False).last
            if button.is_visible(timeout=2000):
                config.vprint(f"confirming with '{name}'")
                button.click()
                return
        except Exception:
            continue
    config.vprint("no confirmation dialog appeared")
