# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

"""Edge-case branches that the feature-focused suites do not exercise:
transient-retry vs hard-error handling in the synchronizer, the
manual-resync guard, the per-document tax fallback and the cron
delete-dispatch / alert-failure paths. Pinned here so the defensive
branches stay covered.
"""

from unittest import mock

from odoo.exceptions import UserError

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from .common import FreeeInvoiceTestCommon


class TestSynchronizerEdges(FreeeInvoiceTestCommon):
    def _patch_adapter(self):
        from odoo.addons.connector_freee_base.components.adapter import FreeeAdapter

        return FreeeAdapter

    def test_exporter_retryable_does_not_flip_error(self):
        """A transient failure (429/5xx/network) on the initial POST
        re-raises so queue_job reschedules, and the binding is left as-is
        — flipping it to ``error`` would fire a false admin alert for a
        blip that self-heals on retry."""
        adapter = self._patch_adapter()
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)

        with mock.patch.object(
            adapter, "post", side_effect=RetryableJobError("rate limited")
        ):
            with self.assertRaises(RetryableJobError):
                binding.export_invoice()

        self.assertNotEqual(binding.sync_state, "error")

    def test_exporter_persist_failure_is_swallowed(self):
        """If recording the error state itself fails, the exporter logs and
        still re-raises the *original* API error (never masks it)."""
        adapter = self._patch_adapter()
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)

        with (
            mock.patch.object(adapter, "post", side_effect=RuntimeError("boom")),
            mock.patch(
                "odoo.addons.connector_freee_invoice.components.synchronizer."
                "_persist_error_state",
                side_effect=RuntimeError("cursor exploded"),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                binding.export_invoice()
        # The surfaced error is the original API failure, not the
        # bookkeeping failure.
        self.assertEqual(str(ctx.exception), "boom")

    def test_deleter_hard_error_marks_and_reraises(self):
        """A non-transient failure during DELETE re-raises (so queue_job
        records the job failed) after marking the binding via
        ``_persist_error_state``."""
        adapter = self._patch_adapter()
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        binding.external_id = "771"
        binding.sudo().write({"sync_state": "to_delete"})

        with mock.patch.object(
            adapter, "delete", side_effect=RuntimeError("server boom")
        ):
            with self.assertRaises(RuntimeError):
                binding.delete_invoice()

    def test_deleter_persist_failure_is_swallowed(self):
        """Same as above but the error-state write itself raises — the
        original DELETE error must still surface."""
        adapter = self._patch_adapter()
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        binding.external_id = "772"
        binding.sudo().write({"sync_state": "to_delete"})

        with (
            mock.patch.object(
                adapter, "delete", side_effect=RuntimeError("server boom")
            ),
            mock.patch(
                "odoo.addons.connector_freee_invoice.components.synchronizer."
                "_persist_error_state",
                side_effect=RuntimeError("cursor exploded"),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                binding.delete_invoice()
        self.assertEqual(str(ctx.exception), "server boom")


class TestManualResyncGuard(FreeeInvoiceTestCommon):
    def test_resync_without_authorized_backend_raises(self):
        """The manual *Sync to freee* button refuses when the company has
        no authorized freee backend, rather than silently doing nothing."""
        self.backend.sudo().write({"state": "draft"})
        invoice = self._make_invoice()
        invoice.action_post()
        with self.assertRaises(UserError):
            invoice.action_freee_resync()


class TestMapperRoundingFallback(FreeeInvoiceTestCommon):
    def test_no_rounding_method_falls_back_to_display_tax(self):
        """When ``account_tax_rounding_method`` resolves no method, the
        mapper falls back to Odoo's per-line display tax
        (``price_total - price_subtotal``) instead of the per-document
        reconciliation."""
        invoice = self._make_invoice(amount=1000.0)
        binding = self._make_binding(invoice)
        lines = invoice.invoice_line_ids.filtered(
            lambda line: line.display_type not in ("line_section", "line_note")
        )
        with self.backend.work_on(binding._name) as work:
            mapper = work.component(usage="export.mapper", model_name=binding._name)
            with mock.patch.object(
                type(self.env["account.tax"]),
                "_get_tax_rounding_method",
                return_value=None,
            ):
                vat_by_line = mapper._vat_by_line(invoice, lines)
        line = lines[0]
        self.assertEqual(
            vat_by_line[line.id],
            int(round(line.price_total - line.price_subtotal)),
        )


class TestRefundPickerFallback(FreeeInvoiceTestCommon):
    def test_action_select_without_refund_journal_delegates_to_base(self):
        """When the picker wizard is *not* bound to a refund journal, the
        invoice override falls through to the base ``action_select`` (which
        writes the regular ``freee_account_item_id`` target)."""
        self.backend.sudo().write({"state": "authorized"})
        account = self.env["account.account"].search(
            [("account_type", "=", "income")], limit=1
        )
        wizard = self.env["freee.account.item.fetch.wizard"].create(
            {"account_id": account.id}
        )
        line = self.env["freee.account.item.fetch.wizard.line"].create(
            {"wizard_id": wizard.id, "external_id": 6611, "name": "Sales"}
        )
        line.action_select()
        self.assertEqual(account.freee_account_item_id, 6611)


class TestCronSweepEdges(FreeeInvoiceTestCommon):
    def _posted_invoice(self, invoice_date):
        invoice = self._make_invoice()
        invoice.invoice_date = invoice_date
        invoice.action_post()
        return invoice

    def test_lock_date_excludes_invoices_on_or_before(self):
        """``lock_date`` keeps the sweep from writing into a closed freee
        period: only invoices strictly after the lock date are enqueued."""
        self.backend.sudo().write({"state": "authorized", "lock_date": "2026-04-10"})
        after = self._posted_invoice("2026-04-11")
        on_lock = self._posted_invoice("2026-04-10")
        with trap_jobs() as trap:
            self.backend._export_pending_invoices()
        exported = {
            job.recordset.odoo_id
            for job in trap.enqueued_jobs
            if job.method_name == "export_invoice"
        }
        self.assertIn(after, exported)
        self.assertNotIn(on_lock, exported)

    def test_sweep_dispatches_delete_jobs(self):
        """A binding flagged ``to_delete`` with a freee id is dispatched a
        throttled ``delete_invoice`` job by the sweep."""
        self.backend.sudo().write({"state": "authorized"})
        invoice = self._posted_invoice("2026-04-10")
        binding = self._make_binding(invoice)
        binding.external_id = "to-del-1"
        binding.sudo().write({"sync_state": "to_delete"})
        with trap_jobs() as trap:
            self.backend._export_pending_invoices()
        deleted = {
            job.recordset
            for job in trap.enqueued_jobs
            if job.method_name == "delete_invoice"
        }
        self.assertIn(binding, deleted)

    def test_sweep_alert_failure_is_swallowed(self):
        """If a backend's sweep raises *and* scheduling the failure activity
        also raises, the cron must not propagate — the other backends were
        already processed and this one retries next tick."""
        self.backend.sudo().write({"state": "authorized"})
        with (
            mock.patch.object(
                type(self.backend),
                "_export_pending_invoices",
                side_effect=RuntimeError("sweep boom"),
            ),
            mock.patch.object(
                type(self.backend),
                "_notify_freee_admins",
                side_effect=RuntimeError("alert boom"),
            ),
        ):
            # Must not raise.
            self.env["freee.backend"]._cron_export_pending_invoices()
