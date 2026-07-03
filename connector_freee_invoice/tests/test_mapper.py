# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo.exceptions import UserError
from odoo.tools import mute_logger

from odoo.addons.connector.exception import MappingError

from .common import FreeeInvoiceTestCommon


class TestMapper(FreeeInvoiceTestCommon):
    """Exercise the invoice mapper end-to-end.

    Covers which values land on the deal payload and how consumption tax is
    rounded: the connector follows freee's per-document rule (round once per
    tax rate with the configured method, residual on the largest-amount line)
    and sends integer-yen amounts, rather than copying Odoo's per-line display
    values verbatim.
    """

    def _map(self, binding):
        with self.backend.work_on(binding._name) as work:
            mapper = work.component(usage="export.mapper", model_name=binding._name)
            return mapper.map_record(binding).values(for_create=True)

    # ------------------------------------------------------------------ #
    # Payload shape                                                       #
    # ------------------------------------------------------------------ #
    def test_basic_payload_shape(self):
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        payload = self._map(binding)

        self.assertEqual(payload["company_id"], 4242)
        self.assertEqual(payload["issue_date"], "2026-04-01")
        self.assertEqual(payload["type"], "income")
        self.assertEqual(payload["ref_number"], invoice.name or "")
        # ``details_description_default`` was removed — it was redundant
        # alongside the per-line ``description``.
        self.assertNotIn("details_description_default", payload)
        self.assertEqual(len(payload["details"]), 1)
        detail = payload["details"][0]
        self.assertEqual(detail["account_item_id"], 9001)
        self.assertEqual(detail["tax_code"], 21)
        # 1000 + 10% tax → 1100. amount is always tax-inclusive and vat
        # carries the consumption tax, regardless of any
        # tax-exclusive/tax-inclusive setting.
        self.assertEqual(detail["amount"], 1100)
        self.assertEqual(detail["vat"], 100)
        # Must be real ints, not Odoo monetary floats (1100.0): freee's
        # integer validation rejects a JSON value with a decimal point.
        # assertEqual alone would not catch it (1100.0 == 1100).
        self.assertIsInstance(detail["amount"], int)
        self.assertIsInstance(detail["vat"], int)

    def test_amounts_and_payment_are_integers_not_floats(self):
        """amount / vat / payments[].amount must serialise as bare integers
        — Odoo monetary fields are floats and freee rejects ``1100.0``."""
        invoice = self._make_invoice()
        invoice.journal_id.write(
            {"freee_walletable_id": 4430264, "freee_walletable_type": "bank_account"}
        )
        payload = self._map(self._make_binding(invoice))
        for detail in payload["details"]:
            self.assertIsInstance(detail["amount"], int)
            self.assertIsInstance(detail["vat"], int)
        self.assertIsInstance(payload["payments"][0]["amount"], int)

    @mute_logger("odoo.tools.translate")
    def test_non_integer_yen_raises(self):
        """A fractional yen amount (e.g. mis-configured currency precision)
        is rejected with a clear MappingError, never silently truncated."""
        invoice = self._make_invoice()
        # Force a fractional line total by bumping the JPY precision.
        invoice.currency_id.rounding = 0.01
        line = invoice.invoice_line_ids[0]
        line.price_unit = 1000.5
        invoice.invalidate_recordset()
        with self.assertRaises(MappingError):
            self._map(self._make_binding(invoice))

    # ------------------------------------------------------------------ #
    # Consumption-tax rounding (account_tax_rounding_method)              #
    # ------------------------------------------------------------------ #
    def test_round_down_residual_on_largest_line(self):
        """Round-down (DOWN): tax is rounded per detail, and the residual to
        the document-level total lands on the largest-amount line — freee's
        documented rule. Reproduces the help-centre example: nets
        426/2100/1885/220 @10% -> 42 / 211 / 188 / 22 (the +1 residual is
        absorbed by the 2,100 line), summing to the document total 463."""
        self.env.company.write({"tax_rounding_method": "DOWN"})
        inv = self._make_invoice_lines(
            [
                (426, self.tax_10),
                (2100, self.tax_10),
                (1885, self.tax_10),
                (220, self.tax_10),
            ]
        )
        payload = self._map(self._make_binding(inv))
        vats = [d["vat"] for d in payload["details"]]
        amounts = [d["amount"] for d in payload["details"]]
        self.assertEqual(vats, [42, 211, 188, 22])
        self.assertEqual(sum(vats), 463)
        self.assertEqual(amounts, [468, 2311, 2073, 242])
        self.assertEqual(sum(amounts), 5094)

    def test_rounding_independent_of_company_global_setting(self):
        """The result follows freee's per-document rule regardless of Odoo's
        own round-per-line / round-globally company setting."""
        amounts_seen = []
        for calc in ("round_per_line", "round_globally"):
            self.env.company.write(
                {
                    "tax_rounding_method": "DOWN",
                    "tax_calculation_rounding_method": calc,
                }
            )
            inv = self._make_invoice_lines(
                [
                    (426, self.tax_10),
                    (2100, self.tax_10),
                    (1885, self.tax_10),
                    (220, self.tax_10),
                ]
            )
            payload = self._map(self._make_binding(inv))
            amounts_seen.append([d["vat"] for d in payload["details"]])
        self.assertEqual(amounts_seen[0], [42, 211, 188, 22])
        self.assertEqual(amounts_seen[0], amounts_seen[1])

    def test_round_half_up_residual_on_largest_line(self):
        """Round-half-up (HALF-UP): per-line rounds half up, and the residual
        between that sum and the document total lands on the largest line.
        Nets 135 / 246 @10%: per-line 14 / 25 (=39); document total
        round(38.1)=38; the -1 residual is absorbed by the 246 line -> 24."""
        self.env.company.write({"tax_rounding_method": "HALF-UP"})
        inv = self._make_invoice_lines([(135, self.tax_10), (246, self.tax_10)])
        payload = self._map(self._make_binding(inv))
        vats = [d["vat"] for d in payload["details"]]
        self.assertEqual(vats, [14, 24])
        self.assertEqual(sum(vats), 38)

    def test_round_up_residual_on_largest_line(self):
        """Round-up (UP): per-line ceils, and the negative residual to the
        document total is absorbed by the largest-amount line. Nets 105 / 215
        @10%: per-line ceil 11 / 22 (=33); document total ceil(32.0)=32; the
        -1 residual is absorbed by the 215 line -> 21."""
        self.env.company.write({"tax_rounding_method": "UP"})
        inv = self._make_invoice_lines([(105, self.tax_10), (215, self.tax_10)])
        payload = self._map(self._make_binding(inv))
        vats = [d["vat"] for d in payload["details"]]
        self.assertEqual(vats, [11, 21])
        self.assertEqual(sum(vats), 32)

    def test_no_adjustment_when_totals_already_match(self):
        """When the per-line sum already equals the document total, no line is
        adjusted (the residual is zero). Nets 1000 / 2000 @10% -> 100 / 200."""
        self.env.company.write({"tax_rounding_method": "DOWN"})
        inv = self._make_invoice_lines([(1000, self.tax_10), (2000, self.tax_10)])
        payload = self._map(self._make_binding(inv))
        self.assertEqual([d["vat"] for d in payload["details"]], [100, 200])

    def test_account_mapping_wins_over_journal_fallback(self):
        """Account-level mapping is the natural baseline and wins when
        set. The journal-level value is a fallback for accounts left
        unmapped — when both are set, the account-level mapping is the
        one that drives the freee deal payload."""
        invoice = self._make_invoice()
        invoice.journal_id.freee_account_item_id = 7777
        binding = self._make_binding(invoice)
        payload = self._map(binding)
        self.assertEqual(payload["details"][0]["account_item_id"], 9001)

    def test_journal_fallback_used_when_account_unmapped(self):
        """Split sales by journal/walletable: keep an Odoo sales account
        unmapped at the chart-of-accounts level so each journal can
        resolve a different freee account item via its own
        ``freee_account_item_id`` fallback (manual vs. EC-site
        sales)."""
        self.income_account.freee_account_item_id = 0
        invoice = self._make_invoice()
        invoice.journal_id.freee_account_item_id = 7777
        binding = self._make_binding(invoice)
        self.assertEqual(self._map(binding)["details"][0]["account_item_id"], 7777)

    def test_account_used_when_no_journal_fallback(self):
        """With no journal fallback, the per-account mapping is used
        (unchanged behaviour)."""
        invoice = self._make_invoice()
        self.assertFalse(invoice.journal_id.freee_account_item_id)
        binding = self._make_binding(invoice)
        self.assertEqual(self._map(binding)["details"][0]["account_item_id"], 9001)

    # ------------------------------------------------------------------ #
    # Settlement (payments[])                                             #
    # ------------------------------------------------------------------ #
    def test_unsettled_by_default_no_payments_key(self):
        invoice = self._make_invoice()
        self.assertFalse(invoice.journal_id.freee_walletable_id)
        payload = self._map(self._make_binding(invoice))
        self.assertNotIn("payments", payload)

    def test_journal_walletable_makes_deal_settled(self):
        invoice = self._make_invoice()  # 1000 + 10% = 1100
        invoice.journal_id.write(
            {
                "freee_walletable_id": 4430264,
                "freee_walletable_type": "bank_account",
                "freee_walletable_name": "三菱ＵＦＪ（法人）（API）",
            }
        )
        payload = self._map(self._make_binding(invoice))
        self.assertEqual(
            payload["payments"],
            [
                {
                    "amount": 1100,
                    "from_walletable_id": 4430264,
                    "from_walletable_type": "bank_account",
                    "date": "2026-04-01",
                }
            ],
        )

    def test_refund_settles_with_negative_payment(self):
        """A refund on a journal with a walletable settles with a NEGATIVE
        payments[].amount (money flowing back out of the walletable). freee
        accepts a negative amount on a deal's inline payments
        (dealCreateParams.payments[].amount, OpenAPI v2020_06_15)."""
        invoice = self._make_invoice()  # 1000 + 10% = 1100
        invoice.move_type = "out_refund"
        invoice.journal_id.write(
            {"freee_walletable_id": 4430264, "freee_walletable_type": "bank_account"}
        )
        payload = self._map(self._make_binding(invoice))
        self.assertEqual(
            payload["payments"],
            [
                {
                    "amount": -1100,
                    "from_walletable_id": 4430264,
                    "from_walletable_type": "bank_account",
                    "date": "2026-04-01",
                }
            ],
        )

    def test_update_payload_omits_payments(self):
        """``payments`` is ``@only_create``: mapping with
        ``for_create=False`` (the PUT / manual re-sync path) must omit it
        even when the journal has a walletable mapped, because freee's
        ``dealUpdateParams`` has no ``payments`` field."""
        invoice = self._make_invoice()
        invoice.journal_id.write(
            {"freee_walletable_id": 4430264, "freee_walletable_type": "bank_account"}
        )
        binding = self._make_binding(invoice)
        with self.backend.work_on(binding._name) as work:
            mapper = work.component(usage="export.mapper", model_name=binding._name)
            payload = mapper.map_record(binding).values(for_create=False)
        self.assertNotIn("payments", payload)
        # The rest of the deal body is still mapped on update.
        self.assertIn("details", payload)
        self.assertEqual(payload["type"], "income")

    def test_refund_unsettled_without_walletable(self):
        """No walletable on the journal → refund stays unsettled (no payments),
        same as a normal invoice."""
        invoice = self._make_invoice()
        invoice.move_type = "out_refund"
        self.assertFalse(invoice.journal_id.freee_walletable_id)
        payload = self._map(self._make_binding(invoice))
        self.assertNotIn("payments", payload)

    def test_refund_account_item_override(self):
        """When the invoice's journal maps a sales-returns account, every
        refund line is booked to it instead of the line's own account."""
        invoice = self._make_invoice()
        invoice.move_type = "out_refund"
        invoice.journal_id.freee_refund_account_item_id = 9999
        payload = self._map(self._make_binding(invoice))
        self.assertEqual(payload["details"][0]["account_item_id"], 9999)

    def test_refund_account_item_override_ignored_for_invoices(self):
        """The journal's sales-returns override only affects refunds — a
        normal invoice still uses its own account mapping."""
        invoice = self._make_invoice()  # out_invoice
        invoice.journal_id.freee_refund_account_item_id = 9999
        payload = self._map(self._make_binding(invoice))
        self.assertEqual(payload["details"][0]["account_item_id"], 9001)

    # ------------------------------------------------------------------ #
    # Dates                                                               #
    # ------------------------------------------------------------------ #
    def test_due_date_exported_when_set(self):
        invoice = self._make_invoice()
        invoice.invoice_date_due = "2026-05-31"
        payload = self._map(self._make_binding(invoice))
        self.assertEqual(payload["due_date"], "2026-05-31")

    def test_due_date_omitted_when_unset(self):
        invoice = self._make_invoice()
        invoice.invoice_date_due = False
        payload = self._map(self._make_binding(invoice))
        self.assertNotIn("due_date", payload)

    # ------------------------------------------------------------------ #
    # Refund (out_refund → income with negative amount/vat = red-slip)    #
    # ------------------------------------------------------------------ #
    def test_refund_uses_income_type_and_negative_amount(self):
        # A refund stays type="income" and carries negative amounts: freee
        # registers a negative details[].amount as a deduction / negative
        # line (red-slip), so the refund lands on the sales side of the P&L
        # rather than as an expense. See freee OpenAPI schema v2020_06_15
        # (dealCreateParams details[].amount allows negative int64).
        invoice = self._make_invoice()
        invoice.move_type = "out_refund"
        payload = self._map(self._make_binding(invoice))
        self.assertEqual(payload["type"], "income")
        self.assertEqual(payload["details"][0]["amount"], -1100)
        self.assertEqual(payload["details"][0]["vat"], -100)

    # ------------------------------------------------------------------ #
    # Hard rejections                                                     #
    # ------------------------------------------------------------------ #
    def test_non_jpy_invoice_is_rejected(self):
        """freee is JPY-only; a foreign-currency invoice must fail
        loudly rather than silently post its figures as yen."""
        usd = self.env.ref("base.USD")
        usd.active = True
        invoice = self._make_invoice()
        invoice.currency_id = usd
        with self.assertRaises(MappingError):
            self._map(self._make_binding(invoice))

    def test_missing_account_mapping_raises(self):
        self.income_account.freee_account_item_id = 0
        invoice = self._make_invoice()
        with self.assertRaises(MappingError):
            self._map(self._make_binding(invoice))

    def test_unmapped_partner_raises(self):
        """Partner with neither freee_partner_id nor freee_partner_code
        → MappingError. Avoids the silent "no partner on the deal" path
        that would pollute freee's sales-by-partner reports."""
        self.partner.write({"freee_partner_id": False, "freee_partner_code": False})
        invoice = self._make_invoice()
        with self.assertRaises(MappingError):
            self._map(self._make_binding(invoice))

    def test_multi_tax_row_raises(self):
        """A single invoice line with more than one tax carrying
        ``freee_tax_code`` cannot be expressed on freee — refuse to
        export instead of silently dropping the second tax."""
        invoice = self._make_invoice()
        invoice.invoice_line_ids[0].tax_ids = [(6, 0, [self.tax_10.id, self.tax_8r.id])]
        with self.assertRaises(MappingError):
            self._map(self._make_binding(invoice))

    def test_lock_date_blocks_export(self):
        """invoice_date <= backend.lock_date → UserError (sync_state
        ends up in 'error' on the binding when called from a job)."""
        invoice = self._make_invoice()  # invoice_date = 2026-04-01
        self.backend.sudo().lock_date = "2026-04-30"
        with self.assertRaises(UserError):
            self._map(self._make_binding(invoice))

    # ------------------------------------------------------------------ #
    # Section / item mapping                                              #
    # ------------------------------------------------------------------ #
    def test_section_mapping_picks_first_analytic(self):
        plan = self.env["account.analytic.plan"].create({"name": "freee Test Plan"})
        analytic = self.env["account.analytic.account"].create(
            {
                "name": "freee Test Section",
                "plan_id": plan.id,
                "freee_section_id": 555,
            }
        )
        invoice = self._make_invoice()
        invoice.invoice_line_ids[0].analytic_distribution = {
            str(analytic.id): 100.0,
        }
        payload = self._map(self._make_binding(invoice))
        self.assertEqual(payload["details"][0]["section_id"], 555)

    def test_section_mapping_skipped_when_unmapped(self):
        plan = self.env["account.analytic.plan"].create({"name": "freee Test Plan"})
        analytic = self.env["account.analytic.account"].create(
            {"name": "Unmapped", "plan_id": plan.id}
        )
        invoice = self._make_invoice()
        invoice.invoice_line_ids[0].analytic_distribution = {
            str(analytic.id): 100.0,
        }
        payload = self._map(self._make_binding(invoice))
        self.assertNotIn("section_id", payload["details"][0])

    # ------------------------------------------------------------------ #
    # Multi-line forwarding (no per-group rounding step in the mapper)   #
    # ------------------------------------------------------------------ #
    def test_multi_line_uses_document_level_rounding(self):
        """Multi-line tax follows freee's per-document rule, not a verbatim
        per-line copy of Odoo's display values. With the default HALF-UP
        method, nets 426/2100/1885/220 @10% give per-line 43/210/189/22
        (=464); the document total rounds to 463, so the -1 residual is
        absorbed by the largest line (2100 -> 209)."""
        invoice = self._make_invoice_multi([426, 2100, 1885, 220])
        details = self._map(self._make_binding(invoice))["details"]
        self.assertEqual(len(details), 4)
        self.assertEqual([d["vat"] for d in details], [43, 209, 189, 22])
        self.assertEqual([d["amount"] for d in details], [469, 2309, 2074, 242])
        self.assertEqual(sum(d["vat"] for d in details), 463)

    def test_mixed_rate_groups_carry_their_own_tax_code(self):
        invoice = self._make_invoice_lines(
            [
                (1000, self.tax_10),
                (1000, self.tax_8r),
            ]
        )
        details = self._map(self._make_binding(invoice))["details"]
        self.assertEqual([d["tax_code"] for d in details], [21, 2])

    # ------------------------------------------------------------------ #
    # Partner resolution precedence                                       #
    # ------------------------------------------------------------------ #
    def test_partner_prefers_freee_partner_id(self):
        self.partner.write({"freee_partner_id": 7777, "freee_partner_code": "C-1"})
        payload = self._map(self._make_binding(self._make_invoice()))
        self.assertEqual(payload["partner_id"], 7777)
        self.assertNotIn("partner_code", payload)

    def test_partner_falls_back_to_freee_partner_code(self):
        self.partner.write(
            {"freee_partner_id": False, "freee_partner_code": "FREEE-CODE"}
        )
        payload = self._map(self._make_binding(self._make_invoice()))
        self.assertEqual(payload["partner_code"], "FREEE-CODE")
        self.assertNotIn("partner_id", payload)

    # ------------------------------------------------------------------ #
    # Product → freee item mapping                                        #
    # ------------------------------------------------------------------ #
    def _make_invoice_with_product(self, product):
        return self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner.id,
                "invoice_date": "2026-04-01",
                "currency_id": self.jpy.id,
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "name": "Service A",
                            "product_id": product.id,
                            "quantity": 1,
                            "price_unit": 1000.0,
                            "account_id": self.income_account.id,
                            "tax_ids": [(6, 0, [self.tax_10.id])],
                        },
                    )
                ],
            }
        )

    def test_item_id_included_when_product_mapped(self):
        product = self.env["product.product"].create(
            {"name": "Mapped Product", "freee_item_id": 33012}
        )
        payload = self._map(
            self._make_binding(self._make_invoice_with_product(product))
        )
        self.assertEqual(payload["details"][0]["item_id"], 33012)

    def test_item_id_omitted_when_product_unmapped(self):
        product = self.env["product.product"].create({"name": "Unmapped Product"})
        payload = self._map(
            self._make_binding(self._make_invoice_with_product(product))
        )
        self.assertNotIn("item_id", payload["details"][0])

    def test_item_id_omitted_when_line_has_no_product(self):
        payload = self._map(self._make_binding(self._make_invoice()))
        self.assertNotIn("item_id", payload["details"][0])

    # ------------------------------------------------------------------ #
    # Tax-included vs tax-excluded amount/vat composition                 #
    # ------------------------------------------------------------------ #
    def test_tax_included_line_uses_inclusive_total_as_amount(self):
        """A tax-included line takes the entered inclusive price as the
        (authoritative) ``amount`` and books ``price_total - price_subtotal``
        as vat, skipping the per-document round-down (the inclusive price is
        already what the customer paid). 1,100 incl. 10% -> amount 1,100,
        vat 100."""
        tax_inc = self.env["account.tax"].create(
            {
                "name": "JP 10% (税込)",
                "amount": 10.0,
                "type_tax_use": "sale",
                "price_include_override": "tax_included",
                "freee_tax_code": 21,
                "tax_group_id": self.tax_10.tax_group_id.id,
            }
        )
        self.assertTrue(tax_inc.price_include)
        invoice = self._make_invoice_lines([(1100, tax_inc)])
        detail = self._map(self._make_binding(invoice))["details"][0]
        self.assertEqual(detail["amount"], 1100)
        self.assertEqual(detail["vat"], 100)

    def test_tax_excluded_amount_is_net_plus_rounded_vat(self):
        """A tax-excluded line sends ``amount = price_subtotal + rounded vat``
        (tax-inclusive total), not the bare net. Net 333 @10% -> vat 33,
        amount 366."""
        invoice = self._make_invoice_lines([(333, self.tax_10)])
        detail = self._map(self._make_binding(invoice))["details"][0]
        self.assertEqual(detail["vat"], 33)
        self.assertEqual(detail["amount"], 366)

    # ------------------------------------------------------------------ #
    # Section / note lines are never sent as deal details                 #
    # ------------------------------------------------------------------ #
    def test_section_and_note_lines_excluded_from_details(self):
        """``line_section`` / ``line_note`` display lines carry no amount and
        must be dropped from ``details`` — only the real product line is
        exported."""
        invoice = self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner.id,
                "invoice_date": "2026-04-01",
                "currency_id": self.jpy.id,
                "invoice_line_ids": [
                    (0, 0, {"display_type": "line_section", "name": "Group A"}),
                    (
                        0,
                        0,
                        {
                            "name": "Service",
                            "quantity": 1,
                            "price_unit": 1000.0,
                            "account_id": self.income_account.id,
                            "tax_ids": [(6, 0, [self.tax_10.id])],
                        },
                    ),
                    (0, 0, {"display_type": "line_note", "name": "Footnote"}),
                ],
            }
        )
        details = self._map(self._make_binding(invoice))["details"]
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["amount"], 1100)

    # ------------------------------------------------------------------ #
    # Hard rejections (continued)                                         #
    # ------------------------------------------------------------------ #
    def test_unmapped_tax_code_raises(self):
        """A line whose tax has no ``freee_tax_code`` cannot resolve a freee
        tax code → MappingError, rather than silently sending a deal with a
        missing/0 tax code."""
        tax_no_code = self.env["account.tax"].create(
            {
                "name": "JP 10% (unmapped)",
                "amount": 10.0,
                "type_tax_use": "sale",
                "tax_group_id": self.tax_10.tax_group_id.id,
            }
        )
        invoice = self._make_invoice_lines([(1000, tax_no_code)])
        with self.assertRaises(MappingError):
            self._map(self._make_binding(invoice))

    def test_missing_invoice_date_raises(self):
        """``issue_date`` is required by freee; an invoice with no
        ``invoice_date`` is rejected up front with MappingError."""
        invoice = self._make_invoice()
        invoice.invoice_date = False
        with self.assertRaises(MappingError):
            self._map(self._make_binding(invoice))

    def test_blank_ref_number_sent_as_empty_string(self):
        """``ref_number`` maps from the invoice ``name``; a blank name is
        sent as an empty string (not ``false``) so freee accepts it without
        an export error."""
        invoice = self._make_invoice()
        invoice.name = False
        payload = self._map(self._make_binding(invoice))
        self.assertEqual(payload["ref_number"], "")
