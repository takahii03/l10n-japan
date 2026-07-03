# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

"""Operational-hardening tests.

Covers the invoice-side behaviours kept in the MVP:

* ``unlink`` guard on a binding still pointing at a live freee
  deal
* cron sweep keeps going past a per-backend failure
"""

from unittest import mock

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import FreeeInvoiceTestCommon


@tagged("post_install", "-at_install")
class TestInvoiceHardening(FreeeInvoiceTestCommon):
    # ------------------------------------------------------------------ #
    # binding unlink guard
    # ------------------------------------------------------------------ #
    def test_unlink_blocked_when_external_id_set(self):
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        binding.sudo().write({"external_id": "12345", "sync_state": "done"})
        with self.assertRaises(UserError):
            binding.unlink()

    def test_unlink_allowed_when_external_id_cleared(self):
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        self.assertFalse(binding.external_id)
        # Pure pending binding (never exported) is safe to drop.
        binding.unlink()
        self.assertFalse(binding.exists())

    # ------------------------------------------------------------------ #
    # cron sweep continues past a failing backend
    # ------------------------------------------------------------------ #
    @mute_logger("odoo.addons.connector_freee_invoice.models.freee_backend")
    def test_cron_continues_past_failing_backend(self):
        good = self.backend
        good.sudo().state = "authorized"
        # A second authorized backend must live in a *different* company —
        # ``_check_single_authorized_per_company`` allows only one
        # authorized backend per company. The cron sweeps all authorized
        # backends regardless of company, which is what we exercise here.
        other_company = self.env["res.company"].create({"name": "Other Co."})
        bad = self.env["freee.backend"].create(
            {
                "name": "Bad Backend",
                "company_id": other_company.id,
                "client_id": "c2",
                "client_secret": "s2",
                "external_company_id": 5555,
                "state": "authorized",
            }
        )
        called_for = []
        backend_cls = type(self.env["freee.backend"])
        original = backend_cls._export_pending_invoices

        def fake_export(self):
            called_for.append(self.id)
            if self.id == bad.id:
                raise RuntimeError("simulated failure")
            return original(self)

        with mock.patch.object(
            backend_cls, "_export_pending_invoices", new=fake_export
        ):
            self.env["freee.backend"]._cron_export_pending_invoices()

        self.assertIn(good.id, called_for)
        self.assertIn(bad.id, called_for)
