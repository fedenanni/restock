"""Unit tests for the pure parsing/aggregation layer.

These use small synthetic fixtures modelled on the live markup rather than
captured pages: real receipts contain personal order data and must never be
committed. When Tesco's markup drifts, update the fixtures here to match a
fresh `restock probe` capture — that keeps the failure diagnosable offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from restock import regulars
from restock.orders import Item, Regular, aggregate, parse_order_ids, parse_receipt


def receipt_item(product_id: str, name: str, qty: int) -> str:
    """Reproduce one receipt line, including React's comment-split text nodes."""
    return (
        f'<div data-testid="product-title">'
        f'<a href="https://www.tesco.com/shop/en-GB/products/{product_id}">{name}</a>'
        f'</div><h4 data-testid="receipt-total-price">£6.25</h4>'
        f"<div>Quantity<!-- -->: <!-- -->{qty}</div>"
    )


class TestParseOrderIds:
    def test_extracts_ids_in_page_order_without_duplicates(self) -> None:
        html = (
            '<a href="/shop/en-GB/orders/1111-2222-33/receipt">View</a>'
            '<a href="/shop/en-GB/orders/1111-2222-33/receipt">View</a>'
            '<a href="/shop/en-GB/orders/4444-5555-66/receipt">View</a>'
        )
        assert parse_order_ids(html) == ["1111-2222-33", "4444-5555-66"]

    def test_no_orders_is_empty_not_an_error(self) -> None:
        assert parse_order_ids("<p>You have no upcoming orders</p>") == []


class TestParseReceipt:
    def test_reads_id_name_and_quantity(self) -> None:
        html = receipt_item("317297975", "Crosta &amp; Mollica Pizza 403g", 2)
        assert parse_receipt(html) == [
            Item("317297975", "Crosta & Mollica Pizza 403g", 2)
        ]

    def test_defaults_to_one_when_quantity_is_absent(self) -> None:
        html = (
            '<div data-testid="product-title">'
            '<a href="/shop/en-GB/products/123456789">Thing</a></div>'
        )
        assert parse_receipt(html)[0].quantity == 1

    def test_repeated_product_id_counts_once(self) -> None:
        # Receipts echo a product's id in image and price sub-blocks; only the
        # first title block is the real line item.
        html = receipt_item("111111111", "Milk", 1) * 2
        assert len(parse_receipt(html)) == 1

    def test_multiple_items_keep_their_own_quantities(self) -> None:
        html = receipt_item("111111111", "Milk", 1) + receipt_item(
            "222222222", "Coffee", 3
        )
        assert [(i.product_id, i.quantity) for i in parse_receipt(html)] == [
            ("111111111", 1),
            ("222222222", 3),
        ]


class TestAggregate:
    def orders(self) -> list[list[Item]]:
        milk = Item("1", "Milk", 1)
        return [
            [milk, Item("2", "Coffee", 3)],
            [milk, Item("2", "Coffee", 1)],
            [milk, Item("3", "Treat", 1)],
        ]

    def test_min_orders_threshold_excludes_one_offs(self) -> None:
        found = aggregate(self.orders(), min_orders=2)
        assert [r.product_id for r in found] == ["1", "2"]

    def test_quantity_is_the_mode_not_the_mean(self) -> None:
        # Coffee was bought 3 once and 1 once; a mean would invent 2.
        items = [
            [Item("2", "Coffee", 1)],
            [Item("2", "Coffee", 1)],
            [Item("2", "Coffee", 9)],
        ]
        assert aggregate(items, min_orders=1)[0].quantity == 1

    def test_sorted_most_frequent_first(self) -> None:
        found = aggregate(self.orders(), min_orders=1)
        assert [r.order_count for r in found] == sorted(
            [r.order_count for r in found], reverse=True
        )

    def test_records_evidence_against_the_number_scanned(self) -> None:
        found = aggregate(self.orders(), min_orders=3)
        assert (found[0].order_count, found[0].total_orders) == (3, 3)

    def test_renamed_product_keeps_the_commonest_name(self) -> None:
        items = [
            [Item("1", "Tesco Milk 2 Pints", 1)],
            [Item("1", "Tesco Milk 1.13L", 1)],
            [Item("1", "Tesco Milk 1.13L", 1)],
        ]
        found = aggregate(items, min_orders=1)[0]
        assert found.name == "Tesco Milk 1.13L"
        assert found.other_names == ["Tesco Milk 2 Pints"]


class TestRegularsFile:
    def sample(self) -> list[Regular]:
        return [
            # Names full of YAML metacharacters — the case that broke the
            # first implementation, which emitted a `...` document-end marker.
            Regular("1", 'Tesco Milk 1.13L, 2 Pints: "the good one" & more', 2, 9, 10),
            Regular("2", "Tesco Courgettes", 1, 3, 10),
        ]

    def test_round_trips_through_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "regulars.yaml"
        regulars.write(
            path, self.sample(), date="2026-09-06", n_orders=10, always_at=0.75
        )
        loaded = regulars.load(path)
        assert [(e.product_id, e.quantity) for e in loaded] == [("1", 2), ("2", 1)]
        assert loaded[0].label == 'Tesco Milk 1.13L, 2 Pints: "the good one" & more'

    def test_written_file_has_no_document_end_marker(self, tmp_path: Path) -> None:
        path = tmp_path / "regulars.yaml"
        regulars.write(
            path, self.sample(), date="2026-09-06", n_orders=10, always_at=0.75
        )
        assert "\n...\n" not in path.read_text(encoding="utf-8")

    def test_skip_true_is_excluded(self, tmp_path: Path) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(
            'products:\n  - id: "1"\n    qty: 1\n'
            '  - id: "2"\n    qty: 1\n    skip: true\n',
            encoding="utf-8",
        )
        assert [e.product_id for e in regulars.load(path)] == ["1"]

    def test_ids_stay_strings_even_when_unquoted(self, tmp_path: Path) -> None:
        # A hand-edit that drops the quotes must not turn the id into an int.
        path = tmp_path / "r.yaml"
        path.write_text("products:\n  - id: 304368923\n    qty: 1\n", encoding="utf-8")
        assert regulars.load(path)[0].product_id == "304368923"

    @pytest.mark.parametrize(
        "body,expected",
        [
            ("nope: []\n", "expected a top-level 'products:' list"),
            ("products: {}\n", "must be a list"),
            ("products:\n  - name: no id\n", "has no 'id'"),
            ('products:\n  - id: "1"\n    qty: 0\n', "whole number of 1 or more"),
            ('products:\n  - id: "1"\n    qty: two\n', "whole number of 1 or more"),
        ],
    )
    def test_hand_edit_mistakes_get_pointed_errors(
        self, tmp_path: Path, body: str, expected: str
    ) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError, match=expected):
            regulars.load(path)


def basket_line(product_id: str, name: str, qty: int) -> str:
    """Reproduce one basket line's markup."""
    return (
        f'<li data-testid="product-list-item">'
        f'<a href="https://www.tesco.com/shop/en-GB/products/{product_id}" '
        f'aria-hidden="true" data-testid="imageContainer_{product_id}"></a>'
        f'<a href="https://www.tesco.com/shop/en-GB/products/{product_id}">{name}</a>'
        f'<input data-auto="ddsweb-quantity-controls-input" '
        f'id="quantity-controls-{product_id}" maxlength="2" type="number" '
        f'value="{qty}"></li>'
    )


class TestParseBasket:
    def test_reads_id_quantity_and_name(self) -> None:
        from restock.basket import BasketLine, parse_basket

        html = basket_line("304368923", "Tesco Chicken 400G", 3)
        assert parse_basket(html) == [
            BasketLine("304368923", 3, "Tesco Chicken 400G")
        ]

    def test_reads_several_lines(self) -> None:
        from restock.basket import parse_basket

        html = basket_line("1", "Milk", 1) + basket_line("2", "Coffee", 2)
        assert [(b.product_id, b.quantity) for b in parse_basket(html)] == [
            ("1", 1),
            ("2", 2),
        ]

    def test_empty_basket_is_empty_list(self) -> None:
        from restock.basket import parse_basket

        assert parse_basket('<div data-testid="empty-basket-heading">empty</div>') == []

    def test_a_product_is_not_double_counted(self) -> None:
        from restock.basket import parse_basket

        assert len(parse_basket(basket_line("1", "Milk", 2) * 2)) == 1

    def test_html_entities_in_names_are_decoded(self) -> None:
        from restock.basket import parse_basket

        line = basket_line("1", "Tesco Ripe &amp; Ready Avocado", 1)
        assert parse_basket(line)[0].name == "Tesco Ripe & Ready Avocado"


class TestAlwaysFlag:
    def test_written_flag_follows_the_threshold(self, tmp_path: Path) -> None:
        # 9/10 is above a 0.75 threshold; 3/10 is not.
        found = [
            Regular("1", "Weekly staple", 1, 9, 10),
            Regular("2", "Occasional thing", 1, 3, 10),
        ]
        path = tmp_path / "r.yaml"
        regulars.write(path, found, date="2026-09-06", n_orders=10, always_at=0.75)
        flags = {e.product_id: e.always for e in regulars.load(path)}
        assert flags == {"1": True, "2": False}

    def test_always_only_filters(self, tmp_path: Path) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(
            'products:\n'
            '  - id: "1"\n    qty: 1\n    always: true\n'
            '  - id: "2"\n    qty: 1\n',
            encoding="utf-8",
        )
        assert [e.product_id for e in regulars.load(path)] == ["1", "2"]
        assert [
            e.product_id for e in regulars.load(path, always_only=True)
        ] == ["1"]

    def test_skip_beats_always(self, tmp_path: Path) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(
            'products:\n  - id: "1"\n    qty: 1\n    always: true\n    skip: true\n',
            encoding="utf-8",
        )
        assert regulars.load(path, always_only=True) == []


class TestOneOfEntries:
    def yaml_for(self, extra: str = "") -> str:
        return (
            'products:\n'
            '  - name: "Salmon fillets"\n'
            '    qty: 1\n'
            '    always: true\n'
            f'{extra}'
            '    oneof:\n'
            '      - id: "296920881"\n'
            '        name: "Tesco 2 Boneless Salmon Fillets 260G"\n'
            '      - id: "309747513"\n'
            '        name: "Tesco Boneless Salmon Fillets 4 Pack 520g"\n'
        )

    def test_parses_as_a_choice(self, tmp_path: Path) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(self.yaml_for(), encoding="utf-8")
        entry = regulars.load(path)[0]
        assert entry.is_choice
        assert entry.label == "Salmon fillets"
        assert [o.product_id for o in entry.options] == ["296920881", "309747513"]
        assert entry.always is True

    def test_asking_a_choice_for_one_id_is_an_error(self, tmp_path: Path) -> None:
        # Guards against code that assumes every entry has a single product.
        path = tmp_path / "r.yaml"
        path.write_text(self.yaml_for(), encoding="utf-8")
        with pytest.raises(ValueError, match="is a choice of 2"):
            _ = regulars.load(path)[0].product_id

    @pytest.mark.parametrize(
        "body,expected",
        [
            ('products:\n  - id: "1"\n    oneof:\n      - id: "2"\n', "both 'id' and 'oneof'"),
            ("products:\n  - name: x\n    oneof: []\n", "empty 'oneof'"),
            ("products:\n  - name: x\n    oneof:\n      - name: no id\n", "no 'id'"),
        ],
    )
    def test_malformed_choices_get_pointed_errors(
        self, tmp_path: Path, body: str, expected: str
    ) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError, match=expected):
            regulars.load(path)


class TestProductStem:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("Tesco Strawberries 400G", "Tesco Strawberries 600G"),
            (
                "Tesco 2 Boneless Salmon Fillets 260G",
                "Tesco Boneless Salmon Fillets 4 Pack 520g",
            ),
            ("Tropicana Smooth Orange Juice 1.5L", "Tropicana Smooth Orange Juice 900Ml"),
            ("Galbani Italian Mozzarella Cheese 125g", "Galbani Maxi Italian Mozzarella Cheese 250g"),
            ("Corona Extra 4X330ml", "Corona Extra 12X330ml"),
        ],
    )
    def test_sizes_of_one_product_share_a_stem(self, a: str, b: str) -> None:
        from restock.orders import product_stem

        assert product_stem(a) == product_stem(b)

    @pytest.mark.parametrize(
        "a,b",
        [
            ("Tesco Strawberries 400G", "Tesco Blueberries 250G"),
            ("Tropicana Smooth Orange Juice 900Ml", "Innocent Smooth Orange Juice 900ml"),
            ("Tesco Paneer Cheese 200G", "Tesco Halloumi 225G"),
        ],
    )
    def test_different_products_do_not_share_a_stem(self, a: str, b: str) -> None:
        from restock.orders import product_stem

        assert product_stem(a) != product_stem(b)


class TestVariantGroups:
    def test_exclusive_choice_is_reported(self) -> None:
        from restock.orders import variant_groups

        small = Item("1", "Tesco Strawberries 400G", 1)
        large = Item("2", "Tesco Strawberries 600G", 1)
        receipts = {"a": [small], "b": [large], "c": [small]}
        groups = variant_groups(receipts, min_orders=3)
        assert len(groups) == 1
        assert groups[0].orders_with_any == 3
        assert groups[0].orders_with_two == 0

    def test_products_bought_together_are_flagged_as_such(self) -> None:
        from restock.orders import variant_groups

        a = Item("1", "Tesco Strawberries 400G", 1)
        b = Item("2", "Tesco Strawberries 600G", 1)
        groups = variant_groups({"x": [a, b], "y": [a, b], "z": [a, b]}, min_orders=3)
        assert groups[0].orders_with_two == 3

    def test_single_variant_is_not_a_group(self) -> None:
        from restock.orders import variant_groups

        only = Item("1", "Tesco Strawberries 400G", 1)
        assert variant_groups({"a": [only], "b": [only], "c": [only]}) == []


class TestPriceComparison:
    def price(self, shelf: float, clubcard: float | None, unit: float | None):
        from restock.basket import Price

        return Price(price=shelf, clubcard=clubcard, unit_price=unit, unit="kg")

    def test_effective_price_prefers_clubcard(self) -> None:
        assert self.price(4.00, 3.50, 10.00).effective == 3.50

    def test_effective_price_without_clubcard_is_the_shelf_price(self) -> None:
        assert self.price(4.00, None, 10.00).effective == 4.00

    def test_unit_price_is_discounted_pro_rata(self) -> None:
        # Tesco shows £/kg against the shelf price, so a Clubcard discount has
        # to be applied to compare sizes honestly.
        assert self.price(4.00, 3.50, 10.00).effective_unit == 8.75

    def test_missing_unit_price_is_none_not_zero(self) -> None:
        # Zero would win every comparison — the caller must see None instead.
        assert self.price(4.00, None, None).effective_unit is None

    def test_clubcard_makes_the_larger_pack_better_value(self) -> None:
        small = self.price(4.00, None, 10.00)
        large = self.price(7.00, 5.00, 11.00)   # 11.00 * 5/7 = 7.86/kg
        assert large.effective_unit is not None
        assert small.effective_unit is not None
        assert large.effective_unit < small.effective_unit


class TestAlexaList:
    def row(self, text: str, meta: str = "Added 3 days ago") -> str:
        """Reproduce one list row, styled-components hashes included."""
        return (
            '<div class="inner"><div class="item-body">'
            '<div class="sc-gFqAkR PDePJ item-header" role="heading">'
            '<div class="sc-ikkxIA ccqiRJ item-name">'
            f'<p class="sc-dAbbOL kfLsxb item-title">{text}</p></div>'
            f'<div class="item-meta">{meta}</div></div></div></div>'
        )

    def test_reads_items_in_order(self) -> None:
        from restock.alexa import ListItem, parse_list

        html = self.row("honey") + self.row("miso paste")
        assert parse_list(html) == [
            ListItem("honey", 3),
            ListItem("miso paste", 3),
        ]

    def test_empty_list_is_empty(self) -> None:
        from restock.alexa import parse_list

        assert parse_list("<div>Active items (0)</div>") == []

    def test_entities_and_whitespace_are_cleaned(self) -> None:
        from restock.alexa import parse_list

        html = self.row("salt &amp;   pepper\n")
        assert parse_list(html)[0].text == "salt & pepper"

    def test_virtualised_rows_repeated_across_scrolls_count_once(self) -> None:
        # The container re-renders rows as it scrolls, so the same item can
        # appear more than once in a captured page.
        from restock.alexa import parse_list

        assert len(parse_list(self.row("honey") * 3)) == 1

    def test_transcription_artefacts_are_left_alone(self) -> None:
        # "a. a. batteries" is what Alexa heard; cleaning it up is the
        # matcher's job, not the parser's.
        from restock.alexa import parse_list

        assert parse_list(self.row("a. a. batteries"))[0].text == "a. a. batteries"


class TestUpdateAppends:
    HAND_EDITED = '''# My own comment at the top.
products:
  # 14/16 orders
  - id: "304368923"
    name: "Chicken"
    qty: 2
    always: true
  - name: "Salmon fillets"
    qty: 1
    always: true
    oneof:
      - id: "296920881"
        name: "2 pack"
      - id: "309747513"
        name: "4 pack"
  - id: "999999999"
    name: "Something I never want"
    skip: true
'''

    def test_all_ids_includes_oneof_options_and_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(self.HAND_EDITED, encoding="utf-8")
        assert regulars.all_product_ids(path) == {
            "304368923",
            "296920881",
            "309747513",
            "999999999",
        }

    def test_append_preserves_the_existing_file_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        # The whole reason `update` appends: comments, `always`, `oneof` and a
        # hand-set qty must survive, and no serialiser round-trip would keep them.
        path = tmp_path / "r.yaml"
        path.write_text(self.HAND_EDITED, encoding="utf-8")
        regulars.append(
            path,
            [Regular("111111111", "New thing", 1, 4, 12)],
            date="2026-09-07",
            n_orders=12,
            always_at=0.75,
        )
        after = path.read_text(encoding="utf-8")
        assert after.startswith(self.HAND_EDITED)
        assert "My own comment at the top." in after

    def test_appended_product_parses_alongside_the_old_ones(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(self.HAND_EDITED, encoding="utf-8")
        regulars.append(
            path,
            [Regular("111111111", "New thing", 1, 4, 12)],
            date="2026-09-07",
            n_orders=12,
            always_at=0.75,
        )
        loaded = regulars.load(path)
        # Skipped entry stays dropped; the hand-set qty and oneof survive.
        assert [e.label for e in loaded] == ["Chicken", "Salmon fillets", "New thing"]
        assert loaded[0].quantity == 2
        assert loaded[1].is_choice
        assert loaded[2].product_id == "111111111"

    def test_append_marks_frequent_new_products_as_always(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(self.HAND_EDITED, encoding="utf-8")
        regulars.append(
            path,
            [Regular("111111111", "Staple", 1, 11, 12)],   # 92%
            date="2026-09-07",
            n_orders=12,
            always_at=0.75,
        )
        assert regulars.load(path, always_only=True)[-1].label == "Staple"

    def test_append_of_nothing_leaves_the_file_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(self.HAND_EDITED, encoding="utf-8")
        regulars.append(
            path, [], date="2026-09-07", n_orders=12, always_at=0.75
        )
        assert path.read_text(encoding="utf-8") == self.HAND_EDITED

    def test_append_to_a_file_without_a_trailing_newline(
        self, tmp_path: Path
    ) -> None:
        # A list item only continues the sequence from the start of a line.
        path = tmp_path / "r.yaml"
        path.write_text('products:\n  - id: "1"\n    qty: 1', encoding="utf-8")
        regulars.append(
            path,
            [Regular("2", "New", 1, 4, 12)],
            date="2026-09-07",
            n_orders=12,
            always_at=0.75,
        )
        assert [e.product_id for e in regulars.load(path)] == ["1", "2"]


class TestDuplicateGuard:
    def test_id_in_both_a_group_and_a_standalone_entry_is_rejected(
        self, tmp_path: Path
    ) -> None:
        # The exact mistake made when folding strawberries into a `oneof`
        # without deleting the old standalone entries.
        path = tmp_path / "r.yaml"
        path.write_text(
            'products:\n'
            '  - name: "Strawberries"\n'
            '    qty: 1\n'
            '    oneof:\n'
            '      - id: "287529333"\n'
            '      - id: "287529575"\n'
            '  - id: "287529575"\n'
            '    name: "Tesco Strawberries 600G"\n'
            '    qty: 1\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="appears in two entries"):
            regulars.load(path)

    def test_the_message_names_both_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(
            'products:\n'
            '  - id: "1"\n    name: "First"\n    qty: 1\n'
            '  - id: "1"\n    name: "Second"\n    qty: 1\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="'First' and 'Second'"):
            regulars.load(path)

    def test_a_skipped_duplicate_is_not_a_conflict(self, tmp_path: Path) -> None:
        # A skipped entry never reaches the basket, so it cannot double-buy.
        path = tmp_path / "r.yaml"
        path.write_text(
            'products:\n'
            '  - id: "1"\n    name: "Live"\n    qty: 1\n'
            '  - id: "1"\n    name: "Retired"\n    qty: 1\n    skip: true\n',
            encoding="utf-8",
        )
        assert [e.label for e in regulars.load(path)] == ["Live"]


class TestMatcherValidation:
    """The model's reply is data, not instruction — every label is checked."""

    def entries(self) -> list[regulars.Entry]:
        from restock.regulars import Entry, Option

        return [
            Entry("Strawberries", (Option("1", "Tesco Strawberries 400G"),
                                   Option("2", "Tesco Strawberries 600G")), 1, True),
            Entry("Tesco Honey Nut Corn Flakes 500G",
                  (Option("3", "Tesco Honey Nut Corn Flakes 500G"),), 1, False),
        ]

    def items(self, *texts: str):
        from restock.alexa import ListItem

        return [ListItem(t) for t in texts]

    def validate(self, reply: str, *texts: str):
        from restock.matcher import _validate

        return _validate(reply, self.items(*texts), self.entries())

    def test_a_good_match_is_accepted(self) -> None:
        reply = '[{"item": "strawberries", "match": "Strawberries", "reason": "same"}]'
        matches = self.validate(reply, "strawberries")
        assert matches[0].matched
        assert matches[0].entry is not None
        assert matches[0].entry.label == "Strawberries"

    def test_an_invented_label_is_rejected(self) -> None:
        # The failure that matters: a plausible product that is not on the list.
        reply = '[{"item": "milk", "match": "Tesco Whole Milk 2 Pints", "reason": "milk"}]'
        matches = self.validate(reply, "milk")
        assert not matches[0].matched
        assert "unknown label" in matches[0].reason

    def test_an_explicit_null_is_honoured(self) -> None:
        reply = '[{"item": "saffron", "match": null, "reason": "not on the list"}]'
        assert not self.validate(reply, "saffron")[0].matched

    def test_every_item_gets_a_match_even_if_the_model_skips_it(self) -> None:
        reply = '[{"item": "strawberries", "match": "Strawberries", "reason": "ok"}]'
        matches = self.validate(reply, "strawberries", "saffron")
        assert [m.item.text for m in matches] == ["strawberries", "saffron"]
        assert matches[1].reason == "model gave no answer"

    def test_an_item_we_never_sent_is_ignored(self) -> None:
        reply = (
            '[{"item": "caviar", "match": "Strawberries", "reason": "invented"},'
            ' {"item": "saffron", "match": null, "reason": "none"}]'
        )
        matches = self.validate(reply, "saffron")
        assert [m.item.text for m in matches] == ["saffron"]

    def test_label_matching_ignores_case_and_padding(self) -> None:
        reply = '[{"item": "strawberries", "match": "  strawberries ", "reason": "x"}]'
        assert self.validate(reply, "strawberries")[0].matched

    def test_code_fences_are_tolerated(self) -> None:
        reply = (
            'Here you go:\n```json\n'
            '[{"item": "strawberries", "match": "Strawberries", "reason": "ok"}]\n```'
        )
        assert self.validate(reply, "strawberries")[0].matched

    @pytest.mark.parametrize("reply", ["not json at all", "[", '{"a": 1}'])
    def test_unparseable_replies_raise_rather_than_guess(self, reply: str) -> None:
        from restock.matcher import MatcherError

        with pytest.raises(MatcherError):
            self.validate(reply, "strawberries")

    def test_prompt_lists_every_label_and_option(self) -> None:
        from restock.matcher import build_prompt

        prompt = build_prompt(self.items("strawberries"), self.entries())
        assert 'LABEL: "Strawberries"' in prompt
        assert "Tesco Strawberries 600G" in prompt
        assert "- strawberries" in prompt


class TestAlexaItemAge:
    @pytest.mark.parametrize(
        "meta,expected",
        [
            ("Added 31 days ago", 31),
            ("Sam Added 54 days ago", 54),
            ("Added 1 day ago", 1),
            ("Added 2 weeks ago", 14),
            ("Added 3 months ago", 90),
            ("Added today", 0),
            ("Added yesterday", 1),
            ("Added 5 hours ago", 0),
            ("no date here", None),
        ],
    )
    def test_age_parsing(self, meta: str, expected: int | None) -> None:
        from restock.alexa import parse_age_days

        assert parse_age_days(meta) == expected

    def test_age_is_paired_with_the_right_row(self) -> None:
        from restock.alexa import parse_list

        html = (
            '<p class="item-title">honey</p><div class="item-meta">Added 3 days ago</div>'
            '<p class="item-title">saffron</p><div class="item-meta">Added 108 days ago</div>'
        )
        assert [(i.text, i.age_days) for i in parse_list(html)] == [
            ("honey", 3),
            ("saffron", 108),
        ]

    def test_a_row_without_a_meta_does_not_steal_the_next_rows(self) -> None:
        from restock.alexa import parse_list

        html = (
            '<p class="item-title">honey</p>'
            '<p class="item-title">saffron</p><div class="item-meta">Added 108 days ago</div>'
        )
        assert [(i.text, i.age_days) for i in parse_list(html)] == [
            ("honey", None),
            ("saffron", 108),
        ]
