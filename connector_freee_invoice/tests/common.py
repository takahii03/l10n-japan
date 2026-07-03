# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64

from odoo import tools
from odoo.tests import tagged

from odoo.addons.component.tests.common import TransactionComponentCase

# A deterministic, valid Fernet key for the test suite. The connector
# reads its credential-encryption key from odoo.conf only, so the tests
# must seed one.
_TEST_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()


# account-dependent: the chart of accounts / taxes are only available
# post_install, so every subclass must run there (OCA convention).
@tagged("post_install", "-at_install")
class FreeeInvoiceTestCommon(TransactionComponentCase):
    """Shared fixtures for freee invoice connector tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        original_key = tools.config.get("connector_freee_base_encryption_key")
        tools.config["connector_freee_base_encryption_key"] = _TEST_ENCRYPTION_KEY

        def _restore_key():
            if original_key is None:
                tools.config.pop("connector_freee_base_encryption_key", None)
            else:
                tools.config["connector_freee_base_encryption_key"] = original_key

        cls.addClassCleanup(_restore_key)
        cls.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "https://example.test"
        )
        cls.backend = cls.env["freee.backend"].create(
            {
                "name": "Test Backend",
                "client_id": "cid",
                "client_secret": "cse",
                "external_company_id": 4242,
            }
        )

        company = cls.env.company
        # freee is a JPY-only ledger and the mapper now refuses non-JPY
        # invoices, so every fixture invoice is built in JPY regardless
        # of the test DB's company currency. JPY's 0 decimal places also
        # matches the connector's integer-yen assumption.
        cls.jpy = cls.env.ref("base.JPY")
        cls.jpy.active = True
        # The mapper now refuses to export an invoice whose partner has
        # neither freee_partner_id nor freee_partner_code mapped, so the
        # default test partner carries a freee_partner_id. Tests that
        # specifically exercise the unmapped path clear it inline.
        cls.partner = cls.env["res.partner"].create(
            {
                "name": "Acme Co.",
                "ref": "ACME-001",
                "freee_partner_id": 7777,
            }
        )
        tax_group = cls.env["account.tax.group"].search(
            [("company_id", "=", company.id)], limit=1
        ) or cls.env["account.tax.group"].create(
            {"name": "JP Test Tax Group", "company_id": company.id}
        )
        cls.tax_10 = cls.env["account.tax"].create(
            {
                "name": "JP 10%",
                "amount": 10.0,
                "type_tax_use": "sale",
                "freee_tax_code": 21,
                "tax_group_id": tax_group.id,
            }
        )
        # 8% reduced rate — used by the mixed-rate mapper tests so the
        # per-tax-rate group reconciliation in mapper._build_details is
        # exercised with more than one rate group.
        cls.tax_8r = cls.env["account.tax"].create(
            {
                "name": "JP 8% (軽減)",
                "amount": 8.0,
                "type_tax_use": "sale",
                "freee_tax_code": 2,
                "tax_group_id": tax_group.id,
            }
        )
        cls.income_account = cls.env["account.account"].search(
            [("account_type", "=", "income"), ("company_ids", "in", company.id)],
            limit=1,
        )
        cls.income_account.freee_account_item_id = 9001

    def _make_invoice(self, amount=1000.0):
        invoice = self.env["account.move"].create(
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
                            "quantity": 1,
                            "price_unit": amount,
                            "account_id": self.income_account.id,
                            "tax_ids": [(6, 0, [self.tax_10.id])],
                        },
                    )
                ],
            }
        )
        return invoice

    def _make_invoice_multi(self, line_amounts):
        """Create a customer invoice with one line per ``line_amounts``
        entry, all using ``self.tax_10``.
        """
        line_ids = [
            (
                0,
                0,
                {
                    "name": f"Line {i + 1}",
                    "quantity": 1,
                    "price_unit": amount,
                    "account_id": self.income_account.id,
                    "tax_ids": [(6, 0, [self.tax_10.id])],
                },
            )
            for i, amount in enumerate(line_amounts)
        ]
        return self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner.id,
                "invoice_date": "2026-04-01",
                "currency_id": self.jpy.id,
                "invoice_line_ids": line_ids,
            }
        )

    def _make_invoice_lines(self, specs, move_type="out_invoice"):
        """Create an invoice with one line per ``(amount, tax)`` spec.

        ``specs`` is a list of ``(price_unit, account.tax record)``
        tuples so a single invoice can mix several tax rates (e.g.
        10% + 8% reduced rate).
        """
        line_ids = [
            (
                0,
                0,
                {
                    "name": f"Line {i + 1}",
                    "quantity": 1,
                    "price_unit": amount,
                    "account_id": self.income_account.id,
                    "tax_ids": [(6, 0, [tax.id])],
                },
            )
            for i, (amount, tax) in enumerate(specs)
        ]
        return self.env["account.move"].create(
            {
                "move_type": move_type,
                "partner_id": self.partner.id,
                "invoice_date": "2026-04-01",
                "currency_id": self.jpy.id,
                "invoice_line_ids": line_ids,
            }
        )

    def _make_binding(self, invoice):
        return self.env["freee.account.move"].create(
            {"backend_id": self.backend.id, "odoo_id": invoice.id}
        )
