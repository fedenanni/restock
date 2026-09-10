"""Browser session management.

Core idea: we never automate the login.
Instead we keep a persistent Chrome profile on disk. On each run we open Tesco;
if the saved session is still valid we go straight to work, otherwise we pause
and let the human log in manually (email/OTP challenge and all), then continue.

The tool therefore never sees, stores, or types a credential.

Cookie/consent banners are deliberately *not* auto-dismissed: the human is
present for the first login, and their choice persists in the profile, so
later runs never see the banner. Consent is theirs to give, not ours to click.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import config


class LoginTimeout(RuntimeError):
    """Raised when the human did not finish signing in within the timeout."""


class NavigationFailed(RuntimeError):
    """Raised when a page could not be loaded even after a retry."""


class EdgeBlocked(RuntimeError):
    """Raised when Tesco's Akamai edge serves an Access Denied page.

    This is recoverable and worth explaining, because the cause is almost never
    the IP: the profile's Akamai sensor cookie has been marked as a bot, and
    deleting `.browser-profile/` clears it.
    """


@dataclass(frozen=True)
class Site:
    """A site we hold a logged-in session for.

    `auth_url` must be a page that genuinely proves authentication. For Tesco
    that means the orders page: `favourites` and `basket` render fine for
    signed-out visitors, so they cannot tell us anything.

    `login_markers` are URL fragments that mean "you are being asked to sign
    in". URLs are used rather than markup because a redirect target is far more
    stable than a login form's DOM.
    """

    name: str
    profile_dir: Path
    auth_url: str
    login_markers: tuple[str, ...]


TESCO = Site(
    name="Tesco",
    profile_dir=config.USER_DATA_DIR,
    auth_url=config.ORDERS_URL,
    login_markers=("secure.tesco.com", "/account/", "/login"),
)

AMAZON = Site(
    name="Amazon",
    profile_dir=config.AMAZON_USER_DATA_DIR,
    auth_url=config.ALEXA_LIST_URL,
    # /ap/signin is the sign-in form; /ap/cvf and /ap/mfa are the OTP and
    # two-step challenges, which the human has to complete by hand.
    login_markers=("/ap/signin", "/ap/cvf", "/ap/mfa", "/ap/challenge"),
)


@contextmanager
def session(start_url: str | None = None, site: Site = TESCO) -> Iterator[Page]:
    """Yield a logged-in Tesco groceries page, parked at `start_url`.

    Opens the persistent-profile browser, gates on a real authentication check
    (see `_ensure_logged_in`), then navigates to `start_url` — default, the
    orders page — before yielding.
    """
    target = start_url or site.auth_url
    with sync_playwright() as p:
        context = _launch(p, site)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            config.vprint(f"Browser profile: {site.profile_dir}")
            _ensure_logged_in(page, site)
            if not page.url.startswith(target):
                goto(page, target)
            yield page
        finally:
            context.close()


def _launch(p, site: Site) -> BrowserContext:
    """Launch the persistent context, preferring real Chrome over Chromium.

    Tesco's bot detection is markedly friendlier to a real Chrome build, so we
    try the configured channel first and only fall back to bundled Chromium if
    that channel isn't installed.
    """
    kwargs = dict(
        user_data_dir=str(site.profile_dir),
        headless=config.HEADLESS,
        # A real window size; default headless-ish viewports are a bot tell.
        viewport={"width": 1440, "height": 900},
        args=config.STEALTH_ARGS,
        ignore_default_args=config.SUPPRESSED_ARGS,
    )
    try:
        return p.chromium.launch_persistent_context(
            channel=config.BROWSER_CHANNEL, **kwargs
        )
    except Exception as exc:  # channel not installed on this machine
        if config.BROWSER_CHANNEL == "chromium":
            raise
        print(
            f"[restock] Chrome channel '{config.BROWSER_CHANNEL}' unavailable "
            f"({type(exc).__name__}); falling back to bundled Chromium."
        )
        return p.chromium.launch_persistent_context(**kwargs)


def _ensure_logged_in(page: Page, site: Site) -> None:
    """Block until we have an authenticated session for `site`.

    We always test against `site.auth_url`, never the caller's target — see
    `Site` for why that distinction matters.

    If the saved session is valid we return immediately. Otherwise we leave the
    browser open and poll until the human finishes logging in (up to
    `config.LOGIN_TIMEOUT_S`) — no terminal interaction needed.
    """
    goto(page, site.auth_url)
    if _looks_signed_in(page, site):
        print(f"[restock] Existing {site.name} session is valid — continuing.")
        return

    print(
        f"\n[restock] Not signed in to {site.name}. A browser window is open — please\n"
        f"          sign in there (waiting up to {config.LOGIN_TIMEOUT_S}s). Accept or\n"
        "          reject cookies as you prefer; the choice is saved for later runs.\n"
    )
    # Watch passively. We must NOT navigate while the human is filling in the
    # form: an earlier version re-loaded the protected page to confirm, which
    # wiped a half-typed login and sent Amazon back to the email step.
    #
    # So: require the URL to sit off the login flow for several consecutive
    # polls (sign-in redirects pass through intermediate URLs, but only
    # briefly), and only once settled do a single confirming navigation.
    settled_polls_needed = 3
    settled = 0
    deadline_ms = config.LOGIN_TIMEOUT_S * 1000
    waited = 0
    while waited < deadline_ms:
        settled = settled + 1 if _looks_signed_in(page, site) else 0
        if settled >= settled_polls_needed and _confirm_signed_in(page, site):
            print(f"[restock] {site.name} login detected — continuing.")
            return
        page.wait_for_timeout(2000)
        waited += 2000
    raise LoginTimeout(
        f"Timed out after {config.LOGIN_TIMEOUT_S}s waiting for {site.name} login "
        f"(still at {page.url}). Re-run and finish signing in, or raise "
        f"RESTOCK_LOGIN_TIMEOUT."
    )


def goto(page: Page, url: str, attempts: int = 2) -> None:
    """Navigate to `url` at human pace, refusing to continue past an edge block.

    Every navigation in this project should go through here rather than calling
    `page.goto` directly: the pause keeps us under Akamai's rate heuristics, and
    the block check stops us mistaking a 403 error page for a real one. That
    mistake is not hypothetical — an earlier version read "Access Denied" at the
    orders URL, saw a URL that was not the login page, and reported a valid
    session.

    A stalled load is retried once. Tesco's product pages sometimes hang past
    the timeout for no lasting reason, and one such page used to abort an
    entire fill run.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        page.wait_for_timeout(config.PAGE_PAUSE_MS)
        config.vprint(f"Opening {url}" + (f" (attempt {attempt})" if attempt > 1 else ""))
        try:
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS
            )
        except PlaywrightTimeoutError as exc:
            last = exc
            config.vprint(f"navigation stalled: {url}")
            continue
        status = response.status if response else None
        if _is_blocked(page, status):
            raise EdgeBlocked(
                f"Tesco's edge denied access to {url} (HTTP {status}).\n"
                "          This usually means the browser profile's bot-sensor cookie has\n"
                "          been flagged. Delete the .browser-profile* directory for that\n"
            "          site and sign in\n"
                "          again; if it recurs, raise RESTOCK_PAGE_PAUSE_MS and take fewer\n"
                "          pages per run."
            )
        return
    raise NavigationFailed(f"could not load {url} after {attempts} attempts: {last}")


def _is_blocked(page: Page, status: int | None = None) -> bool:
    """True if this looks like Akamai's Access Denied interstitial."""
    if status == 403:
        return True
    try:
        return page.title().strip() == "Access Denied"
    except Exception:
        return False


def _looks_signed_in(page: Page, site: Site) -> bool:
    """A single, cheap observation that we might be signed in."""
    return not _on_login_page(page, site) and not _is_blocked(page)


def _confirm_signed_in(page: Page, site: Site) -> bool:
    """Re-check by actually loading the page that requires authentication.

    One observation is not enough. Sign-in flows pass through intermediate
    URLs — OpenID hand-offs, `about:blank` between redirects — that match
    neither the login markers nor the block check, so a single poll can catch
    a moment that merely *looks* signed in and then bounce straight back to
    the login form. Amazon's OAuth hand-off does exactly this.
    """
    try:
        goto(page, site.auth_url)
    except EdgeBlocked:
        return False
    return _looks_signed_in(page, site)


def _on_login_page(page: Page, site: Site) -> bool:
    """True if we've been bounced to `site`'s sign-in flow."""
    return any(marker in page.url for marker in site.login_markers)
