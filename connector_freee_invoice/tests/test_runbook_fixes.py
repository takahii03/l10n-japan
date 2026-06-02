# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

"""Regression tests for the review-driven fixes:

* every freee export job is dispatched on the capped
  ``root.freee`` queue_job channel (so the documented capacity cap
  actually binds to these jobs).
* the export sweep raises one aggregate admin activity when any
  binding is in error, and retracts it once they all recover.
"""

from datetime import timedelta

from odoo import fields

from odoo.addons.connector_freee_base.models.freee_backend import (
    FREEE_QUEUE_CHANNEL,
)
from odoo.addons.queue_job.tests.common import trap_jobs

from .common import FreeeInvoiceTestCommon

FAILURE_ACTIVITY = "connector_freee_invoice.mail_activity_freee_export_failures"


class TestRunbookFixes(FreeeInvoiceTestCommon):
    def setUp(self):
        super().setUp()
        self.backend.sudo().write(
            {
                "state": "authorized",
                "token_expires_at": fields.Datetime.now() + timedelta(hours=1),
            }
        )

    # ----------------------------------------------------------------- #
    # capped channel
    # ----------------------------------------------------------------- #
    def test_sweep_jobs_use_capped_channel(self):
        invoice = self._make_invoice()
        invoice.action_post()
        with trap_jobs() as trap:
            self.backend._export_pending_invoices()
        self.assertTrue(trap.enqueued_jobs, "sweep enqueued no jobs")
        for job in trap.enqueued_jobs:
            self.assertEqual(job.channel, FREEE_QUEUE_CHANNEL)

    def test_manual_resync_uses_capped_channel(self):
        invoice = self._make_invoice()
        invoice.action_post()
        with trap_jobs() as trap:
            invoice.action_freee_resync()
        self.assertTrue(trap.enqueued_jobs)
        for job in trap.enqueued_jobs:
            self.assertEqual(job.channel, FREEE_QUEUE_CHANNEL)

    # ----------------------------------------------------------------- #
    # aggregate failure alert
    # ----------------------------------------------------------------- #
    def _failure_activities(self):
        activity_type = self.env.ref(FAILURE_ACTIVITY)
        return self.env["mail.activity"].search(
            [
                ("res_model", "=", "freee.backend"),
                ("res_id", "=", self.backend.id),
                ("activity_type_id", "=", activity_type.id),
            ]
        )

    def test_failure_raises_dedupes_and_clears(self):
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        binding.sudo().write({"sync_state": "error", "sync_error": "boom"})

        # First sweep: aggregate alert raised for freee admins only.
        with trap_jobs():
            self.backend._export_pending_invoices()
        activities = self._failure_activities()
        self.assertTrue(activities)
        self.assertTrue(all(a.user_id and not a.user_id.share for a in activities))

        # Second sweep, still failing: deduplicated (no pile-up).
        before = len(activities)
        with trap_jobs():
            self.backend._export_pending_invoices()
        self.assertEqual(len(self._failure_activities()), before)

        # Recovered: alert retracted automatically.
        binding.sudo().write(
            {"sync_state": "done", "external_id": "999", "sync_error": False}
        )
        with trap_jobs():
            self.backend._export_pending_invoices()
        self.assertFalse(self._failure_activities())
