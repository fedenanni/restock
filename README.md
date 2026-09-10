# restock

Fills a weekly Tesco basket from a shopping list you control.

## Quick start

Once, to set up:

```bash
uv sync
uv run restock login          # sign in to Tesco by hand; the session is saved
uv run restock scan -n 24     # build regulars.yaml from your last 24 orders
```

Then open `regulars.yaml` and edit it — that file is the point of the whole
tool. Mark your weekly staples `always: true`, fix the quantities, delete what
you don't want bought automatically.

After that, the weekly run is one command:

```bash
uv run restock fill --dry-run   # show what it would add, touching nothing
uv run restock fill             # add your `always` products
```

It never books a slot and never checks out. You review the basket and pay.

### Optionally: your Alexa shopping list

If you dictate things to Alexa during the week, `restock` can read that list
and match each item against your regulars, so "milk" becomes the milk you
actually buy. This is entirely optional — **everything above works without an
Amazon account**, and so do `scan`, `update`, `variants` and `empty`.

```bash
uv run restock alexa-login      # once: sign in to Amazon
uv run restock shop --dry-run   # show the matches and the basket it would build
uv run restock shop             # build it
```

`shop` is `fill` plus the Alexa step: it fills the basket **once** with your
`always` products plus whatever matched. Everything is decided before the
basket is touched, because filling first and matching afterwards would leave a
half-built basket behind whenever matching failed.

## How it fits together

Three stages, with a file you control in the middle:

1. **`scan`** reads your past order receipts and works out what you buy
   regularly, writing `regulars.yaml`.
2. **You edit that file.** Product availability and packaging change
   constantly, so the list is somewhere you can see and correct what the
   automation is about to buy — rather than discovering it at the door.
3. **`fill`** puts that file in your basket. (`shop` is the same plus the
   optional Alexa step.)

## Setup

Uses [`uv`](https://docs.astral.sh/uv/); Python ≥3.13.

```bash
uv sync
uv run playwright install chromium   # only if you don't have Google Chrome
```

The tool prefers your real Chrome — Tesco's bot detection is markedly
friendlier to it — and falls back to Playwright's bundled Chromium.

Only the optional `shop` command needs anything further: the
[`claude` CLI](https://claude.com/claude-code) on your PATH, for matching. It
runs on your existing Claude Code login, with no API key. Nothing else in the
tool uses it.

## Commands

```bash
uv run restock login              # sign in to Tesco; session saved to .browser-profile/

uv run restock scan -n 24         # build regulars.yaml from your last 24 orders
uv run restock scan --force       # regenerate it, discarding your edits
uv run restock update             # append newly frequent products, keeping edits
uv run restock variants           # products that look like a choice between sizes

uv run restock fill --dry-run     # show what would be added, touching nothing
uv run restock fill               # add the `always` products
uv run restock fill --all         # add everything on the list
uv run restock fill --limit 5     # only the first 5, for testing

uv run restock empty --yes        # clear the basket again
uv run restock probe orders       # dump live page HTML when selectors drift
```

Needing an Amazon account, all optional:

```bash
uv run restock alexa-login        # sign in to Amazon; separate profile
uv run restock alexa-list         # print your Alexa shopping list
uv run restock shop --dry-run     # fill, plus the Alexa matching step
uv run restock shop               # the weekly run, with Alexa
uv run restock shop --since 0     # ...ignoring the 14-day age limit
```

`-v` / `--verbose` goes before the subcommand: `uv run restock -v shop`.

Note `--force` on `scan` has no short form, deliberately: `-f` is `--file` on
`update`, `fill` and `shop`, and `scan --force` throws away a hand-edited list.

## The list

Two things distinguish entries in `regulars.yaml`:

**`always: true`** means buy it every week. `shop` and `fill` add only these by
default; `fill --all` adds everything. `scan` sets the flag for anything
appearing in at least 75% of the orders it read (`--always-at` to change that).
Entries without it are the rest of your repertoire — what an incoming Alexa
item gets matched against.

**`oneof`** makes an entry a choice between the same product in different
sizes, and `fill` buys whichever is best value that week:

```yaml
  - name: "Salmon fillets"
    qty: 1
    always: true
    oneof:
      - id: "296920881"
        name: "Tesco 2 Boneless Salmon Fillets 260G"
      - id: "309747513"
        name: "Tesco Boneless Salmon Fillets 4 Pack 520g"
```

Comparison is on **price per kg/litre at the price you actually pay** — Tesco
shows the unit price against the shelf price, so a Clubcard discount is applied
pro rata before sizes are compared. Where Tesco gives no unit price (loose
produce, "each" items) it falls back to the outright cheapest. On a tie the
first option listed wins, so put your preferred size first.

`restock variants` reports which of your products look like such a choice,
from your cached order history with no network. It prints copy-pasteable YAML
but **never edits the file**, because the data cannot distinguish a genuine
either/or from a progression. Sizes you move through once and never go back
to — 1, then 2, then 3 — also never appear in the same order, and buying the
cheapest of those would put the wrong one in the basket.

A product id may appear in only one entry. If you fold sizes into a `oneof`
and forget to delete the old standalone entries, `fill` refuses to run and
names both — otherwise it would buy the same thing twice.

You can also set `skip: true` to keep an entry on the list without buying it.

## Keeping the list up to date

`scan` regenerates the list from scratch, so it is for starting over, and it
refuses to overwrite an existing `regulars.yaml` without `--force`.

To extend a list you have already edited, use `update`: it re-reads your recent
orders and **appends** anything newly frequent, leaving everything already in
the file byte-for-byte alone — comments, `always` flags, `oneof` groups and
hand-set quantities all survive. It also lists products that have not appeared
in recent orders, but never deletes them: a gap might be seasonal.

## Matching a dictated list to your regulars

`shop` sends your Alexa items and your regulars list to `claude-opus-5` via the
`claude` CLI.

**The model's answer is never trusted.** It may only choose labels from your
regulars file, and every label it returns is checked against that file before
anything reaches the basket — a model asked to match "canned tuna" can produce
a plausible product that is not on your list, and that must be caught here
rather than discovered at the door. Anything unmatched is reported for you to
add yourself, never guessed at.

The prompt tells it a near-miss is worse than an honest "no match", and it
holds that line: on a real run it declined to match honey to a conserve, canned
tuna to salmon, and pistachios to cashews.

`--since DAYS` (default 14) ignores older items, since the list accumulates;
`--since 0` takes everything. Items whose age can't be read are kept rather
than silently dropped — losing something you did ask for is worse than
including something stale, which your review of the basket catches.

Each `claude -p` call carries Claude Code's own system prompt, so a `shop` run
costs roughly $0.20. The same prompt through the Anthropic API would be a small
fraction of that, but needs an API key.

## Behaviour

`fill` is idempotent: a product already in the basket has its quantity *set* to
the figure in your list rather than added to it, so re-running converges on the
list instead of doubling it. It verifies the result by reading the basket back
rather than trusting that the clicks worked, and reports anything missing or at
the wrong quantity. Products already in your basket that aren't on the list are
left alone.

`empty` refuses to do anything without `--yes`, and prints the basket first.

No command ever books a delivery slot or checks out.

## The Alexa shopping list

This whole section is optional — skip it if you don't use Alexa; every other
command works without an Amazon account.

Amazon turned off List Skills and the List Management REST API on 1 July 2024,
so there is no supported API for reading Alexa lists. What still exists is the
page you see as a customer, so `restock` reads it from a signed-in browser
session exactly as it reads Tesco.

`alexa-login` uses a **separate** browser profile (`.browser-profile-amazon/`).
That isolation is deliberate: a bot-detection flag attaches to the profile, so
sharing one would let a block picked up on Amazon lock you out of Tesco.

Caveats worth knowing: the list is whatever the voice transcription produced,
so expect "a. a. batteries" for AA batteries; and rows are virtualised, so
reading the page scrolls until no new items appear.

## How login works

The tool never sees a password. `browser.py` launches Chrome against a
persistent profile; if there's no valid session it opens the window, waits for
you to sign in by hand, and carries on. Later runs reuse the saved session.
It is the only approach that survives SSO, MFA and email challenges.

It watches passively while you type and only confirms once the URL has settled
off the sign-in flow. An earlier version re-loaded the protected page on every
poll to check, which wiped a half-typed Amazon login and bounced it back to the
email step.

## Tesco's bot detection, and why the tool is slow

Tesco sits behind Akamai. Navigate quickly and it starts returning
`403 Access Denied` — and once triggered, it poisons the browser profile's
sensor cookie, so **every** later request fails until the profile is deleted.
We hit exactly this while building the tool.

Two defences, both in `config.py`:

- `STEALTH_ARGS` / `SUPPRESSED_ARGS` clear the obvious automation tells.
- `PAGE_PAUSE_MS` (default 4s) paces every navigation. This is the one that
  actually matters — the flags alone did not help.

All navigation goes through `browser.goto()`, which applies the pause and
raises `EdgeBlocked` on a 403 rather than letting an error page be mistaken for
real content. If you do get blocked: delete `.browser-profile/`, sign in again,
and raise `RESTOCK_PAGE_PAUSE_MS`.

This is why a full `scan` takes a few minutes. Parsed receipts are cached under
`.cache/receipts/`, since a delivered order never changes, so repeat analysis
costs no requests at all.

## Configuration

Every tunable in `config.py` is overridable by environment variable:
`RESTOCK_CHANNEL`, `RESTOCK_HEADLESS`, `RESTOCK_LOGIN_TIMEOUT`,
`RESTOCK_NAV_TIMEOUT_MS`, `RESTOCK_PAGE_PAUSE_MS`, `RESTOCK_REGULARS`,
`RESTOCK_AMAZON_BASE`, `RESTOCK_VERBOSE`.

Headless is off by default and should stay off: the login is manual, and a
headless browser is the fastest way to get flagged.

## Development

```bash
uv run pytest tests/   # unit tests — pure parsers, no browser, no network
uv run ty check
```

Parsing is kept separate from fetching throughout: `parse_receipt`,
`parse_basket`, `parse_price`, `parse_list` and the matcher's validation are
pure string→data and tested against synthetic fixtures, so markup drift can be
diagnosed offline instead of against a live session. Real captures from `probe`
land in `pages/` and are git-ignored — they contain your order history.

## Files that stay out of git

All git-ignored, and all worth keeping that way:

- `.browser-profile/`, `.browser-profile-amazon/` — live logged-in sessions
- `regulars.yaml` — your shopping habits
- `pages/` — captured order and basket pages
- `.cache/receipts/` — your parsed order history
