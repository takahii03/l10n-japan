# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
from unittest import mock

from .common import FreeeInvoiceTestCommon


class TestSynchronizer(FreeeInvoiceTestCommon):
    def _patch_http(self, post=None, put=None):
        # Patch the low-level freee.adapter so no real HTTP is made.
        from odoo.addons.connector_freee_base.components.adapter import (
            FreeeAdapter,
        )

        post_patch = mock.patch.object(
            FreeeAdapter, "post", autospec=True, return_value=post
        )
        put_patch = mock.patch.object(
            FreeeAdapter, "put", autospec=True, return_value=put
        )
        return post_patch, put_patch

    def test_first_export_creates_and_binds(self):
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)

        post_patch, put_patch = self._patch_http(post={"deal": {"id": 99001}})
        with post_patch as post, put_patch as put:
            binding.export_invoice()

        post.assert_called_once()
        put.assert_not_called()
        self.assertEqual(binding.external_id, "99001")
        self.assertEqual(binding.sync_state, "done")

    def test_second_export_updates_existing(self):
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        binding.external_id = "55"

        post_patch, put_patch = self._patch_http(put={"deal": {"id": 55}})
        with post_patch as post, put_patch as put:
            binding.export_invoice()

        post.assert_not_called()
        put.assert_called_once()
        self.assertEqual(binding.external_id, "55")
        self.assertEqual(binding.sync_state, "done")

    def test_put_payload_never_carries_payments(self):
        """``dealUpdateParams`` has no ``payments`` field, so the update
        (PUT) payload must not include it even when the journal has a
        settlement walletable mapped — the ``payments`` mapping is
        ``@only_create`` and applies to the initial POST only."""
        invoice = self._make_invoice()
        invoice.journal_id.write(
            {
                "freee_walletable_id": 31337,
                "freee_walletable_type": "bank_account",
            }
        )
        binding = self._make_binding(invoice)

        post_patch, put_patch = self._patch_http(post={"deal": {"id": 99001}})
        with post_patch as post, put_patch:
            binding.export_invoice()
        # Initial POST carries the settlement...
        post_payload = post.call_args.kwargs["payload"]
        self.assertIn("payments", post_payload)

        post_patch, put_patch = self._patch_http(put={"deal": {"id": 99001}})
        with post_patch, put_patch as put:
            binding.export_invoice()
        # ...but the subsequent PUT must not.
        put_payload = put.call_args.kwargs["payload"]
        self.assertNotIn("payments", put_payload)

    def test_create_without_deal_id_flags_error(self):
        """freee answering 2xx without a deal id must not be recorded as a
        successful export: without an ``external_id`` the next cron sweep
        would POST (and book) the same invoice again. The exporter raises
        instead and leaves the binding un-bound."""
        from odoo.addons.connector_freee_base.components.adapter import (
            FreeeAPIError,
        )

        invoice = self._make_invoice()
        binding = self._make_binding(invoice)

        post_patch, put_patch = self._patch_http(post={"deal": {}})
        with post_patch as post, put_patch:
            with self.assertRaises(FreeeAPIError):
                binding.export_invoice()

        post.assert_called_once()
        self.assertFalse(binding.external_id)
        self.assertNotEqual(binding.sync_state, "done")

    def test_export_failure_reraises_for_queue_job(self):
        """On API failure the exporter re-raises so queue_job records the
        job as failed.

        It also flips the binding to ``sync_state='error'`` via
        :func:`_persist_error_state`, on a *separate* committed cursor so
        the state survives queue_job's rollback and the cron's
        admin-notification sweep can count it. That cross-transaction
        write targets the committed binding row; under ``TransactionCase``
        the test binding is never committed, so the separate cursor can't
        see it and the flip can't be asserted here. The error→notification
        linkage is covered by
        ``test_runbook_fixes.test_failure_raises_dedupes_and_clears``
        (which sets the error state directly).
        """
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)

        post_patch, put_patch = self._patch_http()
        with post_patch as post, put_patch:
            post.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                binding.export_invoice()

    def test_job_result_excludes_sensitive_payload(self):
        """queue.job.result is an unencrypted, broadly-readable
        column. The exporter must store only a non-sensitive summary —
        never the deal request payload or freee's response body (which
        carry partner, amounts and per-line descriptions)."""
        invoice = self._make_invoice(amount=12345.0)
        invoice.invoice_line_ids[0].name = "Secret line description"
        binding = self._make_binding(invoice)

        post_patch, put_patch = self._patch_http(
            post={"deal": {"id": 99001, "details": [{"amount": 13579}]}}
        )
        with post_patch, put_patch:
            result = binding.export_invoice()

        data = json.loads(result)
        # Summary fields present and useful for tracing.
        self.assertEqual(data["external_id"], 99001)
        self.assertEqual(data["method"], "POST")
        self.assertEqual(data["detail_count"], 1)
        self.assertEqual(data["status"], "ok")
        # Raw request/response are gone, and no figure or line text leaks.
        self.assertNotIn("request", data)
        self.assertNotIn("response", data)
        self.assertNotIn("Secret line description", result)
        self.assertNotIn("12345", result)
        self.assertNotIn("13579", result)
        self.assertNotIn("ACME-001", result)

    def test_debug_toggle_includes_full_payload(self):
        """With the admin/dev-mode debug toggle ON, the Job Queue result
        carries the exact request payload + freee response so an
        operator can see what was sent. (Off by default — see
        test_job_result_excludes_sensitive_payload above; this is the
        explicit opt-in escape hatch.)"""
        self.backend.sudo().write({"debug_log_request_payload": True})
        invoice = self._make_invoice(amount=12345.0)
        invoice.invoice_line_ids[0].name = "Secret line description"
        binding = self._make_binding(invoice)

        post_patch, put_patch = self._patch_http(
            post={"deal": {"id": 99001, "echo": "freee-said-this"}}
        )
        with post_patch, put_patch:
            result = binding.export_invoice()

        data = json.loads(result)
        # Summary still there...
        self.assertEqual(data["external_id"], 99001)
        self.assertEqual(data["status"], "ok")
        # ...plus the exact data exchanged with freee.
        self.assertIn("request", data)
        self.assertIn("response", data)
        detail = data["request"]["details"][0]
        self.assertEqual(detail["description"], "Secret line description")
        self.assertEqual(detail["amount"], 12345 + detail["vat"])
        # freee's echoed response body is surfaced too.
        self.assertEqual(data["response"], {"id": 99001, "echo": "freee-said-this"})

    def test_deleter_skips_when_state_changed_after_enqueue(self):
        """Cancel→re-post race: cron enqueues a delete job while
        sync_state='to_delete', user re-posts before the worker
        picks up the job (state is now 'pending'). The deleter must
        early-return so the freee deal is not deleted out from under
        a live invoice.
        """
        from odoo.addons.connector_freee_base.components.adapter import (
            FreeeAdapter,
        )

        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        binding.external_id = "999"
        # The race we are simulating: previous state was 'to_delete',
        # user re-posted before the queue worker fired, state is now
        # 'pending'.
        binding.sudo().write({"sync_state": "pending"})

        with mock.patch.object(FreeeAdapter, "delete") as delete:
            binding.delete_invoice()

        delete.assert_not_called()
        # Binding survives intact: external_id and pending state kept.
        self.assertEqual(binding.external_id, "999")
        self.assertEqual(binding.sync_state, "pending")

    def test_deleter_calls_freee_when_state_is_to_delete(self):
        """Happy path of the same code path as the race-guard test —
        when the binding *is* legitimately to_delete, the DELETE
        actually fires and external_id is cleared."""
        from odoo.addons.connector_freee_base.components.adapter import (
            FreeeAdapter,
        )

        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        binding.external_id = "777"
        binding.sudo().write({"sync_state": "to_delete"})

        with mock.patch.object(FreeeAdapter, "delete") as delete:
            delete.return_value = None
            binding.delete_invoice()

        delete.assert_called_once()
        self.assertFalse(binding.external_id)
        self.assertEqual(binding.sync_state, "done")

    def test_deleter_skips_when_no_external_id(self):
        """A to_delete binding that was never exported (no external_id) has
        nothing to delete on freee — the deleter returns early without
        calling the API."""
        from odoo.addons.connector_freee_base.components.adapter import (
            FreeeAdapter,
        )

        invoice = self._make_invoice()
        binding = self._make_binding(invoice)  # external_id empty
        binding.sudo().write({"sync_state": "to_delete"})

        with mock.patch.object(FreeeAdapter, "delete") as delete:
            binding.delete_invoice()

        delete.assert_not_called()

    def test_deleter_keeps_to_delete_on_retryable(self):
        """A transient failure (429/5xx/network) during DELETE re-raises so
        queue_job retries, and the binding stays ``to_delete`` — flipping it
        to ``error`` would make the retry hit the state guard and silently
        skip the delete forever."""
        from odoo.addons.connector_freee_base.components.adapter import (
            FreeeAdapter,
        )
        from odoo.addons.queue_job.exception import RetryableJobError

        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        binding.external_id = "888"
        binding.sudo().write({"sync_state": "to_delete"})

        with mock.patch.object(
            FreeeAdapter, "delete", side_effect=RetryableJobError("rate limited")
        ):
            with self.assertRaises(RetryableJobError):
                binding.delete_invoice()

        self.assertEqual(binding.sync_state, "to_delete")
        self.assertEqual(binding.external_id, "888")
