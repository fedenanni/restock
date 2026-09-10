"""Configuration for the Tesco basket automation.

Nothing here is secret. Credentials are never stored or typed by the tool —
you log in by hand in the browser window (see browser.py), and the session is
kept in a persistent Chrome profile on disk.
"""

from __future__ import annotations

import os
from pathlib import Path

# Root of the repo, used to anchor on-disk paths.
ROOT = Path(__file__).resolve().parent.parent

# Where the persistent Chrome profiles live. Logging in once (including any
# email/OTP challenge) leaves a session here that later runs reuse. Git-ignored.
#
# One profile per site, deliberately. A bot-detection flag attaches to the
# profile's sensor cookie rather than to the IP, so sharing a profile would let
# a block picked up on Amazon lock us out of Tesco as well.
USER_DATA_DIR = ROOT / ".browser-profile"
AMAZON_USER_DATA_DIR = ROOT / ".browser-profile-amazon"

# Parsed order receipts, cached as JSON keyed by order id. A delivered order
# never changes, so this cache is always valid — and it keeps repeated analysis
# off Tesco's edge entirely. Git-ignored: personal order data.
RECEIPT_CACHE_DIR = ROOT / ".cache" / "receipts"

# Where `probe` dumps live page HTML/screenshots for selector discovery.
# Contains personal order data — git-ignored.
PAGES_DIR = ROOT / "pages"

# The reviewable product list that sits between the two tools: `scan` writes
# it, you edit it, `fill` reads it.
REGULARS_FILE = Path(os.environ.get("RESTOCK_REGULARS", ROOT / "regulars.yaml"))

# --- Tesco URLs -----------------------------------------------------------

BASE_URL = "https://www.tesco.com"
GROCERIES_URL = f"{BASE_URL}/groceries/en-GB"
# Tesco redirects /groceries/en-GB/orders here; use the canonical path directly.
# This is also the only one of these pages that reliably 302s to the login flow
# when signed out, which is why browser.py uses it as the auth gate.
ORDERS_URL = f"{BASE_URL}/shop/en-GB/orders"
# Tesco's own "regularly bought" list — the primary source for `scan`.
# Canonical path taken from the site's own nav link, not guessed: the
# /groceries/en-GB/favourites variant renders for signed-out visitors too.
USUALS_URL = f"{BASE_URL}/shop/en-GB/favourites"
# The basket page. Tesco calls it the *trolley* internally; both
# /groceries/en-GB/basket and /shop/en-GB/basket serve an error page.
# Viewing it is read-only — checkout is a further, separate step we never take.
BASKET_URL = f"{BASE_URL}/shop/en-GB/trolley"

# --- Amazon / Alexa -------------------------------------------------------

# The Alexa shopping list, as a *customer* sees it. Amazon turned off the List
# Management REST API on 2024-07-01, so there is no supported programmatic
# route; this page is what remains, and reading it needs a signed-in session.
AMAZON_BASE_URL = os.environ.get("RESTOCK_AMAZON_BASE", "https://www.amazon.co.uk")
ALEXA_LIST_URL = f"{AMAZON_BASE_URL}/alexaquantum/sp/alexaShoppingList"


def product_url(product_id: str) -> str:
    """Canonical product page for a Tesco product id."""
    return f"{GROCERIES_URL}/products/{product_id}"


# --- Browser behaviour ----------------------------------------------------

# Use your real Chrome rather than Playwright's bundled Chromium. Tesco fronts
# its site with bot detection that treats stock Chromium far more suspiciously,
# so real Chrome means fewer challenges. Set to "chromium" to fall back.
BROWSER_CHANNEL = os.environ.get("RESTOCK_CHANNEL", "chrome")

# Headless is a deliberate non-default: the login is manual, and a headless
# browser is also the fastest way to get flagged as a bot.
HEADLESS = os.environ.get("RESTOCK_HEADLESS", "false").lower() == "true"

# How long to wait for the human to complete login before giving up.
LOGIN_TIMEOUT_S = int(os.environ.get("RESTOCK_LOGIN_TIMEOUT", "300"))

# Chrome flags that keep Akamai's bot sensor from flagging us on sight.
# `--disable-blink-features=AutomationControlled` clears `navigator.webdriver`;
# suppressing `--enable-automation` removes the "controlled by automation"
# infobar and its matching CDP hints. Neither hides *behaviour* — see PAGE_PAUSE_MS.
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
SUPPRESSED_ARGS = ["--enable-automation"]

# Pause between page navigations. Tesco's edge flags rapid-fire navigation and,
# once it does, it poisons the profile's `_abck` sensor cookie so that *every*
# later request 403s until the profile is reset. Human pace is not politeness
# here, it is the difference between working and locked out.
PAGE_PAUSE_MS = int(os.environ.get("RESTOCK_PAGE_PAUSE_MS", "4000"))

# Per-navigation timeout. Tesco's product pages are heavy and occasionally
# stall well past Playwright's 30s default; a single slow page must not be
# allowed to end a 46-product run.
NAV_TIMEOUT_MS = int(os.environ.get("RESTOCK_NAV_TIMEOUT_MS", "45000"))

VERBOSE = os.environ.get("RESTOCK_VERBOSE", "false").lower() == "true"


def vprint(msg: str) -> None:
    """Print `msg` only when verbose output is enabled."""
    if VERBOSE:
        print(f"[restock]   {msg}")
