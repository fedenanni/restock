"""Command-line entry point.

Two commands exist so far, both about getting a real session and seeing the
real markup:

    restock login    — open the browser, let you sign in, confirm the session
    restock probe    — dump live page HTML + screenshots for selector discovery

`scan` (build the regulars list) and `fill` (put it in the basket) are written
against the output of `probe`, not guessed at.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from datetime import date

from . import alexa, basket, browser, config, matcher, orders, regulars
from .browser import EdgeBlocked, LoginTimeout

# Pages worth capturing for selector discovery, as (slug, url).
PROBE_TARGETS = {
    "orders": config.ORDERS_URL,
    "usuals": config.USUALS_URL,
    "basket": config.BASKET_URL,
}


def cmd_login(_args: argparse.Namespace) -> int:
    """Establish (or verify) a saved Tesco session."""
    with browser.session(config.ORDERS_URL) as page:
        print(f"[restock] Logged in. Landed on: {page.url}")
        print(f"[restock] Session saved in {config.USER_DATA_DIR}")
        print("[restock] Holding the window open 10s so you can eyeball it.")
        page.wait_for_timeout(10_000)
    return 0


def cmd_alexa_login(_args: argparse.Namespace) -> int:
    """Establish (or verify) a saved Amazon session for the Alexa list.

    Kept separate from `login` because it uses its own browser profile: a
    bot-detection flag attaches to the profile, so an Amazon problem must not
    be able to lock us out of Tesco.
    """
    with browser.session(config.ALEXA_LIST_URL, site=browser.AMAZON) as page:
        print(f"[restock] Signed in to Amazon. Landed on: {page.url}")
        print(f"[restock] Session saved in {config.AMAZON_USER_DATA_DIR}")
        print("[restock] Holding the window open 10s so you can eyeball it.")
        page.wait_for_timeout(10_000)
    return 0


def cmd_alexa_list(_args: argparse.Namespace) -> int:
    """Print the active Alexa shopping list. Reads only; changes nothing."""
    with browser.session(config.ALEXA_LIST_URL, site=browser.AMAZON) as page:
        items = alexa.fetch_list(page)
    if not items:
        print("[restock] Alexa shopping list is empty.")
        return 0
    print(f"[restock] {len(items)} item(s) on the Alexa shopping list:")
    for item in items:
        print(f"[restock]   {item.text}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Save the HTML and a screenshot of each requested page.

    Output lands in `pages/` (git-ignored — it contains your order history).
    """
    slugs = args.pages or list(PROBE_TARGETS)
    unknown = [s for s in slugs if s not in PROBE_TARGETS]
    if unknown:
        print(f"[restock] Unknown page(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"[restock] Choose from: {', '.join(PROBE_TARGETS)}", file=sys.stderr)
        return 2

    config.PAGES_DIR.mkdir(parents=True, exist_ok=True)
    with browser.session(PROBE_TARGETS[slugs[0]]) as page:
        for slug in slugs:
            url = PROBE_TARGETS[slug]
            print(f"[restock] Probing {slug}: {url}")
            browser.goto(page, url)
            # These pages hydrate client-side; give the network a moment to
            # settle, but don't fail the run if it never fully idles.
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                config.vprint(f"{slug}: network never idled; capturing anyway")
            html_path = config.PAGES_DIR / f"{slug}.html"
            png_path = config.PAGES_DIR / f"{slug}.png"
            html_path.write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(png_path), full_page=True)
            print(f"[restock]   -> {html_path.name}, {png_path.name}")
    print(f"[restock] Done. Captures in {config.PAGES_DIR}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Build the regulars list from recent order receipts."""
    out = args.output or config.REGULARS_FILE
    if out.exists() and not args.force:
        print(
            f"[restock] {out} already exists — refusing to overwrite your edits.\n"
            "          Pass --force to regenerate, or --output to write elsewhere.",
            file=sys.stderr,
        )
        return 1

    with browser.session(f"{config.ORDERS_URL}/recent") as page:
        order_ids = orders.fetch_order_ids(page, args.orders)
        if not order_ids:
            print("[restock] Found no past orders to scan.", file=sys.stderr)
            return 1
        print(f"[restock] Scanning {len(order_ids)} orders...")
        baskets: list[list[orders.Item]] = []
        for n, order_id in enumerate(order_ids, start=1):
            items = orders.fetch_receipt(page, order_id)
            print(f"[restock]   {n}/{len(order_ids)}  {order_id}: {len(items)} items")
            baskets.append(items)

    found = orders.aggregate(baskets, min_orders=args.min_orders)
    regulars.write(
        out,
        found,
        date=date.today().isoformat(),
        n_orders=len(baskets),
        always_at=args.always_at,
    )
    n_always = sum(1 for r in found if r.frequency >= args.always_at)
    distinct = len({i.product_id for b in baskets for i in b})
    print(
        f"\n[restock] {distinct} distinct products across {len(baskets)} orders; "
        f"{len(found)} appear in >= {args.min_orders}."
    )
    print(
        f"[restock] {n_always} marked `always: true` "
        f"(in >= {args.always_at:.0%} of orders); `fill` adds those by default."
    )
    print(f"[restock] Wrote {out} — review and edit it before running `fill`.")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Extend an existing regulars file with newly frequent products.

    Appends; never rewrites. The file holds hand edits, `always` flags and
    `oneof` groups, so anything already in it is left exactly as it is.
    """
    target = args.file or config.REGULARS_FILE
    if not target.exists():
        print(
            f"[restock] No regulars file at {target}. Run `restock scan` first.",
            file=sys.stderr,
        )
        return 1
    try:
        known = regulars.all_product_ids(target)
    except ValueError as exc:
        print(f"[restock] {exc}", file=sys.stderr)
        return 1

    with browser.session(f"{config.ORDERS_URL}/recent") as page:
        order_ids = orders.fetch_order_ids(page, args.orders)
        if not order_ids:
            print("[restock] Found no past orders to scan.", file=sys.stderr)
            return 1
        print(f"[restock] Scanning {len(order_ids)} orders...")
        baskets: list[list[orders.Item]] = []
        for n, order_id in enumerate(order_ids, start=1):
            items = orders.fetch_receipt(page, order_id)
            print(f"[restock]   {n}/{len(order_ids)}  {order_id}: {len(items)} items")
            baskets.append(items)

    found = orders.aggregate(baskets, min_orders=args.min_orders)
    fresh = [r for r in found if r.product_id not in known]
    seen_now = {item.product_id for basket in baskets for item in basket}

    print(
        f"\n[restock] {len(known)} products already on the list; "
        f"{len(fresh)} newly frequent."
    )
    if fresh:
        for r in fresh:
            print(f"[restock]   + {r.order_count}/{r.total_orders}  {r.name}")

    # Report, never edit: a product missing from recent orders might be
    # seasonal, or the shop might have stopped stocking it. Deleting it is a
    # judgement call, so it stays the human's.
    stale = sorted(known - seen_now)
    if stale:
        print(
            f"\n[restock] {len(stale)} product(s) on your list weren't bought in "
            f"these {len(baskets)} orders:"
        )
        for product_id in stale:
            print(f"[restock]   ? {product_id}")
        print("[restock] Left in place — remove them yourself if they're gone for good.")

    if not fresh:
        print("\n[restock] Nothing new to add.")
        return 0
    if args.dry_run:
        print(f"\n[restock] Dry run — {target} not modified.")
        return 0

    regulars.append(
        target,
        fresh,
        date=date.today().isoformat(),
        n_orders=len(baskets),
        always_at=args.always_at,
    )
    print(f"\n[restock] Appended {len(fresh)} product(s) to {target}.")
    print("[restock] Everything already in the file was left untouched.")
    return 0


def cmd_fill(args: argparse.Namespace) -> int:
    """Put the regulars list into the basket."""
    source = args.file or config.REGULARS_FILE
    if not source.exists():
        print(
            f"[restock] No regulars file at {source}. Run `restock scan` first.",
            file=sys.stderr,
        )
        return 1
    try:
        entries = regulars.load(source, always_only=not args.all)
    except ValueError as exc:
        print(f"[restock] {exc}", file=sys.stderr)
        return 1
    if not entries:
        which = "products" if args.all else "products marked `always: true`"
        print(
            f"[restock] {source} has no {which} to add."
            + ("" if args.all else " Use --all to add everything on the list."),
            file=sys.stderr,
        )
        return 1
    if args.limit:
        entries = entries[: args.limit]

    scope = "all products" if args.all else "`always` products"
    print(f"[restock] {len(entries)} {scope} from {source.name}")
    if args.dry_run:
        # Deliberately touches nothing: no session, no page loads, no basket.
        for entry in entries:
            if entry.is_choice:
                options = ", ".join(o.name for o in entry.options)
                print(f"[restock]   would add {entry.quantity} x best value of: {options}")
            else:
                print(
                    f"[restock]   would add {entry.quantity} x {entry.label} "
                    f"({entry.product_id})"
                )
        print("[restock] Dry run — nothing was added.")
        return 0

    return _fill_entries(entries)


def _fill_entries(entries: list[regulars.Entry]) -> int:
    """Add `entries` to the basket, then verify against the basket itself."""
    results: list[basket.AddResult] = []
    with browser.session(config.BASKET_URL) as page:
        for n, entry in enumerate(entries, start=1):
            try:
                result = basket.add_entry(page, entry)
            except EdgeBlocked:
                # Being blocked is not a per-product problem; stop and report.
                print(f"[restock]   {n}/{len(entries)} blocked — stopping early.")
                break
            except Exception as exc:
                # One unhealthy product must never end the run. Record and move on.
                result = basket.AddResult(entry, False, f"{type(exc).__name__}: {exc}")
            results.append(result)
            mark = "ok " if result.ok else "FAIL"
            print(f"[restock]   {n}/{len(entries)} {mark} {entry.label}: {result.detail}")

        # Verify against the basket itself rather than trusting the clicks.
        # This runs even if products failed above — a partial basket is
        # exactly when you most want to know what is actually in it.
        print("\n[restock] Verifying basket contents...")
        lines = {line.product_id: line.quantity for line in basket.read_basket(page)}

    # Verify against the option actually chosen, not the entry: for a `oneof`
    # entry only one of its ids should be in the basket.
    wanted: dict[str, tuple[regulars.Entry, int]] = {}
    for result in results:
        if result.chosen:
            wanted[result.chosen.product_id] = (result.entry, result.entry.quantity)
    missing = [e for pid, (e, _) in wanted.items() if pid not in lines]
    wrong = [
        (e, lines[pid])
        for pid, (e, q) in wanted.items()
        if pid in lines and lines[pid] != q
    ]
    # The basket is the authority, not our own clicks. A product whose click
    # looked like it failed but which is in the basket at the right quantity
    # did land — reporting it as a failure would send you hunting for nothing.
    landed = {pid for pid, (_, q) in wanted.items() if lines.get(pid) == q}
    failed = [
        r
        for r in results
        if not r.ok and not (r.chosen and r.chosen.product_id in landed)
    ]
    recovered = [
        r
        for r in results
        if not r.ok and r.chosen and r.chosen.product_id in landed
    ]
    if failed:
        print(f"[restock] {len(failed)} product(s) could not be added:")
        for r in failed:
            print(f"[restock]   {r.entry.label}: {r.detail}")
    for r in recovered:
        print(
            f"[restock]   (reported a problem adding {r.entry.label}, "
            "but it is in the basket correctly)"
        )
    matched = len(set(wanted) & set(lines))
    print(f"[restock] Basket holds {matched} of {len(wanted)} requested products.")
    for entry in missing:
        print(f"[restock]   MISSING  {entry.label}")
    for entry, actual in wrong:
        print(
            f"[restock]   QTY      {entry.label}: wanted {entry.quantity}, "
            f"basket has {actual}"
        )
    extra = set(lines) - set(wanted)
    if extra:
        print(f"[restock]   {len(extra)} other product(s) already in the basket, left alone.")
    if missing or wrong:
        print("\n[restock] Basket does not match the list — review it before checking out.")
        return 1
    print("\n[restock] Basket matches the list. Add your extras and check out yourself.")
    return 0


def cmd_shop(args: argparse.Namespace) -> int:
    """The weekly run: read the Alexa list, match it, then fill the basket once.

    Everything is decided before the basket is touched. Doing it the other way
    round — fill the staples, then match — leaves a half-filled basket behind
    if the matching step fails, with no clean state to go back to.
    """
    source = args.file or config.REGULARS_FILE
    if not source.exists():
        print(
            f"[restock] No regulars file at {source}. Run `restock scan` first.",
            file=sys.stderr,
        )
        return 1
    try:
        everything = regulars.load(source)
        staples = [e for e in everything if e.always]
    except ValueError as exc:
        print(f"[restock] {exc}", file=sys.stderr)
        return 1

    # 1. Read the dictated list.
    max_age = args.since or None   # `--since 0` means no age limit
    with browser.session(config.ALEXA_LIST_URL, site=browser.AMAZON) as page:
        dictated = alexa.fetch_list(page, max_age_days=max_age)
    if max_age is not None:
        print(f"[restock] {len(dictated)} Alexa item(s) from the last {max_age} days.")
    else:
        print(f"[restock] {len(dictated)} Alexa item(s).")

    # 2. Match them, before touching anything.
    matches: list[matcher.Match] = []
    if dictated:
        print(f"[restock] Matching against {len(everything)} regulars via {matcher.MODEL}...")
        try:
            matches = matcher.match_items(dictated, everything)
        except matcher.MatcherError as exc:
            print(f"[restock] {exc}", file=sys.stderr)
            return 1
        for match in matches:
            age = "" if match.item.age_days is None else f" ({match.item.age_days}d)"
            if match.matched and match.entry is not None:
                print(f"[restock]   {match.item.text}{age} -> {match.entry.label}")
            else:
                print(f"[restock]   {match.item.text}{age} -> no match ({match.reason})")

    # 3. Work out the single basket to build.
    wanted: dict[str, regulars.Entry] = {e.label: e for e in staples}
    for match in matches:
        if match.entry is not None:
            wanted.setdefault(match.entry.label, match.entry)
    entries = list(wanted.values())
    unmatched = [m for m in matches if not m.matched]

    print(
        f"\n[restock] Basket to build: {len(staples)} always + "
        f"{len(entries) - len(staples)} from the Alexa list = {len(entries)}."
    )
    if unmatched:
        print(f"[restock] {len(unmatched)} Alexa item(s) matched nothing — add these yourself:")
        for match in unmatched:
            print(f"[restock]   ! {match.item.text}")

    if args.dry_run:
        print("\n[restock] Would add:")
        for entry in entries:
            how = "best value of " + ", ".join(o.name for o in entry.options) if entry.is_choice else entry.label
            print(f"[restock]   {entry.quantity} x {how}")
        print("\n[restock] Dry run — the basket was not touched.")
        return 0

    return _fill_entries(entries)


def cmd_empty(args: argparse.Namespace) -> int:
    """Remove everything from the basket."""
    with browser.session(config.BASKET_URL) as page:
        contents = basket.read_basket(page)
        if not contents:
            print("[restock] Basket is already empty.")
            return 0
        total = sum(line.quantity for line in contents)
        print(f"[restock] Basket holds {len(contents)} products ({total} items):")
        for line in contents:
            print(f"[restock]   {line.quantity} x {line.name or line.product_id}")
        if not args.yes:
            # Flush first: stdout and stderr are not interleaved, so without
            # this the refusal appears above the listing it refers to.
            sys.stdout.flush()
            print(
                "\n[restock] Refusing to empty without confirmation. "
                "Re-run with --yes to clear it.",
                file=sys.stderr,
            )
            return 1
        before, after = basket.empty_basket(page)
    if after:
        print(f"\n[restock] Emptied {before - after} of {before}; {after} remain.")
        return 1
    print(f"\n[restock] Basket emptied ({before} products removed).")
    return 0


def cmd_variants(args: argparse.Namespace) -> int:
    """Report products that look like a choice between sizes of one thing.

    Reads only the cached receipts written by `scan` — no network. Nothing is
    changed: the output is a suggestion for you to fold into `regulars.yaml`
    by hand, because the data cannot tell an either/or from a progression.
    """
    receipts = orders.cached_receipts()
    if not receipts:
        print(
            "[restock] No cached receipts. Run `restock scan` first.", file=sys.stderr
        )
        return 1

    groups = orders.variant_groups(receipts, min_orders=args.min_orders)
    if not groups:
        print(f"[restock] No variant groups found across {len(receipts)} orders.")
        return 0

    print(f"[restock] {len(groups)} possible size choices across {len(receipts)} orders.")
    print(
        "[restock] `exclusive` means the variants never appeared in the same order,\n"
        "[restock] which is what an either/or looks like. Check each one: a\n"
        "[restock] progression (sizes moved through once) looks the same.\n"
    )
    for group in groups:
        together = (
            "exclusive" if not group.orders_with_two
            else f"together in {group.orders_with_two}"
        )
        print(f"  {group.label}  —  in {group.orders_with_any}/{len(receipts)} orders, {together}")
        for item, count in group.members:
            print(f'      {count:>2}x  - id: "{item.product_id}"')
            print(f'            name: "{item.name}"')
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="restock",
        description="Automate a weekly Tesco basket from a reviewable list.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print extra diagnostics"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="sign in to Tesco and save the session").set_defaults(
        func=cmd_login
    )
    sub.add_parser(
        "alexa-login", help="sign in to Amazon to read your Alexa shopping list"
    ).set_defaults(func=cmd_alexa_login)
    sub.add_parser(
        "alexa-list", help="print your Alexa shopping list"
    ).set_defaults(func=cmd_alexa_list)
    p_probe = sub.add_parser("probe", help="dump live page HTML for development")
    p_probe.add_argument(
        "pages",
        nargs="*",
        help=f"pages to capture ({', '.join(PROBE_TARGETS)}); default: all",
    )
    p_probe.set_defaults(func=cmd_probe)

    p_scan = sub.add_parser(
        "scan", help="build the regulars list from your recent orders"
    )
    p_scan.add_argument(
        "-n", "--orders", type=int, default=12,
        help="how many recent orders to scan (default: 12)",
    )
    p_scan.add_argument(
        "-m", "--min-orders", type=int, default=3,
        help="a product is regular if it appears in at least this many (default: 3)",
    )
    p_scan.add_argument(
        "--always-at", type=float, default=0.75, metavar="FRACTION",
        help="mark products in at least this share of orders as `always` (default: 0.75)",
    )
    p_scan.add_argument(
        "-o", "--output", type=Path, default=None,
        help=f"where to write the list (default: {config.REGULARS_FILE.name})",
    )
    p_scan.add_argument(
        # No short form on purpose. `-f` is `--file` on update/fill/shop, and
        # this one discards a hand-edited list — it should have to be typed out.
        "--force", action="store_true",
        help="overwrite an existing regulars file, discarding your edits",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_update = sub.add_parser(
        "update", help="add newly frequent products to an existing regulars file"
    )
    p_update.add_argument(
        "-n", "--orders", type=int, default=12,
        help="how many recent orders to look at (default: 12)",
    )
    p_update.add_argument(
        "-m", "--min-orders", type=int, default=3,
        help="a product is regular if it appears in at least this many (default: 3)",
    )
    p_update.add_argument(
        "--always-at", type=float, default=0.75, metavar="FRACTION",
        help="mark new products in at least this share of orders as `always`",
    )
    p_update.add_argument(
        "-f", "--file", type=Path, default=None,
        help=f"regulars file to extend (default: {config.REGULARS_FILE.name})",
    )
    p_update.add_argument(
        "--dry-run", action="store_true", help="report what would be added only"
    )
    p_update.set_defaults(func=cmd_update)

    p_fill = sub.add_parser("fill", help="put the regulars list into your basket")
    p_fill.add_argument(
        "-f", "--file", type=Path, default=None,
        help=f"regulars file to read (default: {config.REGULARS_FILE.name})",
    )
    p_fill.add_argument(
        "-a", "--all", action="store_true",
        help="add every product on the list, not just those marked `always: true`",
    )
    p_fill.add_argument(
        "--dry-run", action="store_true",
        help="list what would be added, touching nothing",
    )
    p_fill.add_argument(
        "--limit", type=int, default=None,
        help="only add the first N products (useful for testing)",
    )
    p_fill.set_defaults(func=cmd_fill)

    p_empty = sub.add_parser("empty", help="remove everything from your basket")
    p_empty.add_argument(
        "-y", "--yes", action="store_true",
        help="required: confirm you really want the basket cleared",
    )
    p_empty.set_defaults(func=cmd_empty)

    p_shop = sub.add_parser(
        "shop",
        help="the weekly run: read the Alexa list, match it, fill the basket",
    )
    p_shop.add_argument(
        "--since", type=int, default=14, metavar="DAYS",
        help="ignore Alexa items older than this many days (default: 14; 0 for all)",
    )
    p_shop.add_argument(
        "-f", "--file", type=Path, default=None,
        help=f"regulars file to use (default: {config.REGULARS_FILE.name})",
    )
    p_shop.add_argument(
        "--dry-run", action="store_true",
        help="show the matches and the basket it would build, touching nothing",
    )
    p_shop.set_defaults(func=cmd_shop)

    p_variants = sub.add_parser(
        "variants", help="report products that look like a choice between sizes"
    )
    p_variants.add_argument(
        "-m", "--min-orders", type=int, default=3,
        help="only report groups appearing in at least this many orders (default: 3)",
    )
    p_variants.set_defaults(func=cmd_variants)

    args = parser.parse_args(argv)
    if args.verbose:
        config.VERBOSE = True
    try:
        return args.func(args)
    except (LoginTimeout, EdgeBlocked) as exc:
        # Expected, explainable outcomes — report them, don't dump a traceback.
        print(f"[restock] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
