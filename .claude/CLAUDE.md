# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

`restock` is a Playwright-driven CLI that reads a Tesco account's past orders,
distils them into a hand-editable shopping list (`regulars.yaml`), and puts
that list in the basket. No web app, no server.

## Commands

Uses [`uv`](https://docs.astral.sh/uv/); Python ≥3.13.

```bash
uv sync
uv run restock login          # manual sign-in; session saved to .browser-profile/
uv run restock alexa-login    # manual Amazon sign-in; own profile
uv run restock alexa-list     # read the Alexa shopping list
uv run restock shop           # the weekly run: alexa -> match -> fill (--dry-run)
uv run restock scan -n 16     # build regulars.yaml from the last 16 orders
uv run restock fill           # add `always` products (--all, --dry-run, --limit N)
uv run restock update         # append newly frequent products, preserving edits
uv run restock variants       # candidate size choices, from cache, no network
uv run restock empty --yes    # clear the basket
uv run restock probe orders   # capture live HTML into pages/ when selectors drift
uv run pytest tests/          # fast, no browser, no network
uv run ty check               # clean — keep it that way
```

## Architecture

`cli.py` → `browser.session()` → `orders.py` / `basket.py` (fetch + parse)
→ `regulars.py` (file I/O).

- `config.py` — every tunable as a module constant with a `RESTOCK_*` env override.
- `browser.py` — yields a logged-in `Page` for a `Site` (TESCO or AMAZON), each
  with its own profile and auth check. Login is **never automated**.
- `orders.py` — order/receipt fetching *and* the pure parsers.
- `regulars.py` — reads and writes the shopping list.
- `basket.py` — adds products, prices options, reads the basket back, empties it.
- `alexa.py` — reads the Alexa shopping list from a signed-in Amazon session.
- `matcher.py` — maps dictated items onto regulars entries via `claude -p`.

Fetching and parsing are deliberately separate: `parse_order_ids`,
`parse_receipt` and `aggregate` are pure string→data, so markup drift is
diagnosed offline against fixtures instead of against a live session.

## The regulars file

Entries are either a single `id` or a `oneof` list of equivalent products.
`always: true` marks the weekly staples; `fill` adds only those unless `--all`.
Anything without `always` is the rest of the repertoire, intended as the target
for matching an incoming shopping list.

For `oneof`, comparison is on unit price at the *effective* (Clubcard) price:
Tesco quotes £/kg against the shelf price, so the discount is applied pro rata
before sizes are compared. No unit price → cheapest outright. Ties go to the
first listed.

`update` **appends and never rewrites.** The file carries comments, `always`
flags, `oneof` groups and hand-set quantities that no YAML round-trip would
survive, so `regulars.append` writes only to the end of the file. Its
"already known" check reads *every* id, including `oneof` options and
`skip: true` entries — otherwise a deliberately skipped product returns every
week. Stale products are reported, never deleted; a gap may be seasonal.

**A product id may appear in only one entry**, enforced in `load()`. Folding
sizes into a `oneof` while leaving the old standalone entries in place is an
easy edit to get wrong, and it would silently double-buy.

**`scan` must never generate `oneof` groups automatically.** A progression —
sizes someone moves through once and never returns to, 1 then 2 then 3 — is
mutually exclusive across orders in exactly the same way a genuine either/or
is, and buying the cheapest would put the wrong size in the basket. This is
not hypothetical: a real list contained such a run, and it looked identical in
the data to the salmon 2-pack/4-pack choice. `variants` reports candidates for a human to fold in; that is the
whole reason it only prints.

## Confirmed facts about the live site — do not re-guess these

Each was discovered by probing, and several contradict the obvious guess:

- **Only the orders page proves authentication.** `favourites` and `basket`
  render fine for signed-out visitors; only `/shop/en-GB/orders` redirects to
  the login flow. Auth checks must use it.
- **Canonical paths are `/shop/en-GB/…`, not `/groceries/en-GB/…`** for orders,
  favourites and products. The `/groceries/` variants are stale.
- **Past orders live at `/shop/en-GB/orders/recent`**, 10 per page.
  **`?page=N` is ignored** — it silently re-serves page 1. Paginate by clicking
  "Next Page" and verify new ids appeared.
- **Receipt line items**: `data-testid="product-title"` wrapping
  `<a href=".../products/<id>">Name</a>`, with quantity as
  `Quantity<!-- -->: <!-- -->N` (React splits the text node).
- **A placed-but-undelivered order is not a receipt.** It lives on the
  "Upcoming orders" tab at `/shop/en-GB/orders/<id>` — no `/receipt` suffix —
  and its markup is different: items are `_1bCRSG_titleLink` anchors inside
  the section that starts at `data-testid="items-no"` ("Items (31)"), with no
  `data-testid="product-title"`. **`parse_receipt` returns 0 items on it.**
  Only delivered orders have `/receipt`.
- **Never infer rendered state from a string being present in the HTML.** The
  orders page ships its i18n catalogue, so "You have no upcoming orders"
  appears in the source even when an order *is* shown. Check the rendered
  structure (or the screenshot), not the phrase.
- **Favourites is not the regulars list** — it is everything ever bought
  (9+ pages), so frequency has to come from receipts.
- Products are keyed by Tesco **product id**, never by name. Names get
  rewritten as packaging changes.
- **The basket is `/shop/en-GB/trolley`.** Both `/groceries/en-GB/basket` and
  `/shop/en-GB/basket` serve an error page.
- **Product pages carry many add buttons** (recommendation carousels). Always
  scope to `[data-auto="pdp-buy-box"]` or you will buy the wrong thing.
- **The add button's `aria-label` is not reactive** — it still reads "add 1"
  after the quantity input changes. Never read quantity from it. Filling the
  input then clicking does add the full amount (verified against the basket).
- **The buy box hydrates late.** Check which controls are present *before*
  touching it: read too early and an in-basket product looks fresh, sending a
  click to what is really the stepper's increment button.
- Basket lines: `data-testid="product-list-item"`, quantity input
  `id="quantity-controls-<product-id>"`. Empty via `#emptyBasketButton`.
- **Prices on a product page**: shelf price and unit price are within ~4k
  chars of the buy box; the Clubcard value bar is ~6.5k in, and the first
  *recommendation tile's* Clubcard price is ~400k in. `parse_price` windows to
  20k and rejects any "Clubcard price" that is not below the shelf price —
  that guard is what makes the scoping safe, so keep it.

## The matcher — treat model output as untrusted input

`matcher.py` is the only place a language model is used. Two rules that must
not be relaxed:

- **Every label the model returns is validated against the regulars file**
  (`_validate`). An unrecognised label becomes an unmatched item, never a
  basket addition. The model does not get to widen the shopping list.
- **Every item asked about gets exactly one Match**, whatever the model said —
  a skipped item reports "model gave no answer" rather than vanishing.

The prompt tells the model a near-miss is worse than an honest null, and that
matters: on a live run it correctly refused honey -> conserve, canned tuna ->
salmon, and pistachios -> cashews. Don't "improve" the prompt toward
helpfulness here; conservatism is the whole point.

Invoked as `claude -p --output-format json` with `stdin=DEVNULL` (the CLI
otherwise waits on stdin). The reply is nested in the envelope's `result`.
Each call carries Claude Code's own system prompt, so it costs ~$0.20.

## `shop` decides before it touches the basket

Read Alexa, match, *then* fill once. Filling the staples first and matching
afterwards would leave a half-built basket whenever matching fails, with no
clean state to return to. Keep that order.

## The basket is the authority, never our own clicks

After adding, the buy box swaps its add button for a stepper only once the
basket round-trips. An impatient check reported "clicked add but basket
controls did not appear" for a product that had in fact landed, while the
final verification simultaneously reported success — so `_fill_entries` now
treats anything present in the basket at the right quantity as landed, and
`add_option` waits up to 15s for the state to settle.

## Amazon / Alexa

There is **no API**: Amazon turned off List Skills and the List Management REST
API on 2024-07-01, and the Alexa+ APIs are not public. The customer-facing page
`/alexaquantum/sp/alexaShoppingList` is the only route, hence the browser.

- **One browser profile per site** (`.browser-profile-amazon/`). A bot flag
  attaches to the profile, so a shared one would let an Amazon block take out
  Tesco too.
- Alexa rows are **virtualised** — only on-screen rows are in the DOM. Read by
  scrolling until the item set stops growing, never from a single `content()`.
- Match on `item-title` / `item-meta` only. The surrounding `sc-*` classes are
  styled-components hashes and change on every Amazon deploy.
- List text is raw voice transcription ("a. a. batteries"). The parser leaves
  it alone; normalising is the matcher's job.

## Never navigate while a human is signing in

`_ensure_logged_in` watches the URL passively and requires it to stay off the
login flow for three consecutive polls before confirming with a single
navigation. An earlier version re-loaded the protected page on every poll to
confirm, which wiped the half-typed Amazon login and bounced it back to the
email step. Confirm once, at the end — never mid-flow.

## Akamai — the constraint that shapes the code

Tesco is behind Akamai. Rapid navigation triggers `403 Access Denied`, and the
block then attaches to the **browser profile's sensor cookie**, not the IP — so
every later request fails until `.browser-profile/` is deleted. A clean profile
from the same IP works immediately; that is how we diagnosed it.

- Every navigation must go through `browser.goto()`, never `page.goto()`
  directly. It applies `PAGE_PAUSE_MS` and raises `EdgeBlocked` on a 403.
- **Never treat a page as valid without checking for the block.** An early
  version read "Access Denied" at the orders URL, saw a non-login URL, and
  reported a healthy session. That is why `_is_blocked` exists.
- Don't add parallel fetches. Slower is the working state. (`goto` retries a
  stalled navigation once; that is about flaky page loads, not rate limits.)

## Deliberate safety guards — do not weaken

- The tool **never checks out** and never books a slot. It fills a basket; the
  human reviews and pays.
- `scan` **refuses to overwrite** an existing `regulars.yaml` without `--force`
  — that file holds the user's edits.
- `empty` **refuses to run** without `--yes`, and prints the basket first.
- `fill` defaults to `always` products only. Widening that default would put
  the user's whole repertoire in the basket unasked.
- `fill` **sets** quantities rather than incrementing, so it is idempotent, and
  leaves basket products that aren't on the list alone.
- `fill` **verifies against the basket** instead of trusting its own clicks,
  and one failing product is recorded and skipped, never fatal to the run.
- Login is never automated and no credential is ever stored or typed.
- Headless stays off by default.

## Sensitive local files (all git-ignored)

`.browser-profile/` holds a live logged-in session — never commit, read, or
exfiltrate it. `pages/` and `regulars.yaml` contain personal order history.
Never commit real captured HTML as a test fixture; write synthetic ones.

## Conventions

- User-facing progress is `print`ed with a `[restock]` prefix; no logging framework.
- Expected failures (`LoginTimeout`, `EdgeBlocked`) are caught in `cli.main`
  and reported as a message, not a traceback.
- Docstrings carry the *why*. Keep them and the README current with behaviour.
