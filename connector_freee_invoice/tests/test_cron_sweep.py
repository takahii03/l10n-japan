# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

"""The invoice export cron is self-contained in
``_cron_export_pending_invoices`` and dispatches each authorized
backend to its own ``_export_pending_invoices``. Each freee feature
module owns its own cron; these tests pin the gate semantics for the
invoice cron specifically.
"""

from unittest import mock

from odoo.addons.queue_job.tests.common import trap_jobs

from .common import FreeeInvoiceTestCommon


class TestInvoiceCronSweep(FreeeInvoiceTestCommon):
    def test_sweep_runs_authorized_backend(self):
        """An authorized backend is dispatched to
        ``_export_pending_invoices`` exactly once per cron tick.
        """
        self.backend.sudo().write({"state": "authorized"})
        with mock.patch.object(
            type(self.backend), "_export_pending_invoices"
        ) as enqueue:
            self.env["freee.backend"]._cron_export_pending_invoices()
        enqueue.assert_called_once()

    def test_sweep_skips_unauthorized_backend(self):
        """A backend still in 'draft' is excluded by the search filter."""
        self.backend.sudo().write({"state": "draft"})
        with mock.patch.object(
            type(self.backend), "_export_pending_invoices"
        ) as enqueue:
            self.env["freee.backend"]._cron_export_pending_invoices()
        enqueue.assert_not_called()

    # ------------------------------------------------------------------ #
    # export_from_date boundary and no-duplicate gates                    #
    # ------------------------------------------------------------------ #
    def _posted_invoice(self, invoice_date):
        invoice = self._make_invoice()
        invoice.invoice_date = invoice_date
        invoice.action_post()
        return invoice

    def _exported_odoo_ids(self, trap):
        return {
            job.recordset.odoo_id
            for job in trap.enqueued_jobs
            if job.method_name == "export_invoice"
        }

    def test_export_from_date_boundary(self):
        """The sweep enqueues invoices whose ``invoice_date`` is on or after
        ``export_from_date`` (``>=``) and excludes earlier ones, so a fresh
        authorize does not retroactively flood freee with history."""
        self.backend.sudo().write(
            {"state": "authorized", "export_from_date": "2026-04-10"}
        )
        on_date = self._posted_invoice("2026-04-10")
        before = self._posted_invoice("2026-04-09")
        with trap_jobs() as trap:
            self.backend._export_pending_invoices()
        exported = self._exported_odoo_ids(trap)
        self.assertIn(on_date, exported)
        self.assertNotIn(before, exported)

    def test_already_exported_invoice_not_re_enqueued(self):
        """An invoice already bound with a freee deal id (external_id) is
        excluded from the candidate set, so the cron never creates a second
        deal for it."""
        self.backend.sudo().write({"state": "authorized"})
        invoice = self._posted_invoice("2026-04-10")
        binding = self._make_binding(invoice)
        binding.external_id = "already-1"
        binding.sudo().write({"sync_state": "done"})
        with trap_jobs() as trap:
            self.backend._export_pending_invoices()
        self.assertNotIn(invoice, self._exported_odoo_ids(trap))
