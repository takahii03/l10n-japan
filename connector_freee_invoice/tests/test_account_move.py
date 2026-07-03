# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from psycopg2 import IntegrityError

from odoo.exceptions import UserError
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from .common import FreeeInvoiceTestCommon


class TestAccountMoveHooks(FreeeInvoiceTestCommon):
    """Cover the account.move hooks added by this module:

    - ``button_cancel`` flips bound bindings to ``sync_state='to_delete'``.
    - ``_post`` reverts ``to_delete`` bindings back to ``done`` so a
      re-post within the cron grace window keeps the freee deal as-is
      (no automatic update — connector policy is updates-are-manual).
    """

    def _bind_as_exported(self, invoice, external_id="42"):
        binding = self._make_binding(invoice)
        binding.external_id = external_id
        binding.sudo().write({"sync_state": "done"})
        return binding

    def test_edit_on_posted_invoice_does_not_mark_pending(self):
        """Editing a posted invoice no longer auto-dirties the binding
        — updates to freee are manual via *Sync to freee* only."""
        invoice = self._make_invoice()
        invoice.action_post()
        binding = self._bind_as_exported(invoice)
        invoice.write({"ref": "edited-after-post"})
        self.assertEqual(binding.sync_state, "done")

    def test_button_cancel_flags_to_delete(self):
        invoice = self._make_invoice()
        invoice.action_post()
        binding = self._bind_as_exported(invoice)
        invoice.button_draft()
        invoice.button_cancel()
        self.assertEqual(binding.sync_state, "to_delete")

    def test_repost_reverts_to_delete_back_to_done(self):
        """Cancel + re-post within the cron grace window keeps the
        freee deal alive — the binding goes ``done`` → ``to_delete`` →
        ``done`` (no automatic update push)."""
        invoice = self._make_invoice()
        invoice.action_post()
        binding = self._bind_as_exported(invoice)
        invoice.button_draft()
        invoice.button_cancel()
        self.assertEqual(binding.sync_state, "to_delete")
        # In Odoo 18 the path back to posted from cancel goes through
        # draft: button_draft (cancel → draft), then action_post.
        invoice.button_draft()
        invoice.action_post()
        self.assertEqual(binding.sync_state, "done")
        # external_id is preserved across the cancel→repost cycle so
        # the next manual sync would issue a PUT, not a POST.
        self.assertTrue(binding.external_id)

    # ------------------------------------------------------------------ #
    # Manual *Sync to freee* guards (posted customer invoices only)       #
    # ------------------------------------------------------------------ #
    def test_resync_rejects_draft_invoice(self):
        """A draft (un-posted) invoice cannot be pushed — only finalized
        vouchers go to freee."""
        invoice = self._make_invoice()  # draft
        with self.assertRaises(UserError):
            invoice.action_freee_resync()

    def test_resync_rejects_non_customer_move(self):
        """A vendor bill (in_invoice) is refused — this connector only
        exports customer invoices / refunds."""
        bill = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": self.partner.id,
                "invoice_date": "2026-04-01",
                "currency_id": self.jpy.id,
                "invoice_line_ids": [
                    (0, 0, {"name": "Cost", "quantity": 1, "price_unit": 1000.0})
                ],
            }
        )
        with self.assertRaises(UserError):
            bill.action_freee_resync()

    # ------------------------------------------------------------------ #
    # Binding integrity: ondelete=restrict and uniqueness                 #
    # ------------------------------------------------------------------ #
    @mute_logger("odoo.sql_db")
    def test_invoice_with_live_binding_cannot_be_deleted(self):
        """``freee.account.move.odoo_id`` is ondelete=restrict, so an
        invoice still referenced by a binding (with a live deal id) cannot
        be hard-deleted — the orphan-deal path is blocked at the DB."""
        invoice = self._make_invoice()
        self._bind_as_exported(invoice, external_id="555")
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                invoice.unlink()

    @mute_logger("odoo.sql_db")
    def test_duplicate_binding_per_invoice_rejected(self):
        """unique(backend_id, odoo_id): an invoice can be bound only once
        per backend."""
        invoice = self._make_invoice()
        self._make_binding(invoice)
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self._make_binding(invoice)

    @mute_logger("odoo.sql_db")
    def test_duplicate_external_id_per_backend_rejected(self):
        """unique(backend_id, external_id): a freee deal id can be bound
        only once per backend (NULLs stay distinct, so un-exported rows do
        not collide)."""
        first = self._make_binding(self._make_invoice())
        first.external_id = "dup-1"
        first.flush_recordset()
        second = self._make_binding(self._make_invoice())
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                second.external_id = "dup-1"
                second.flush_recordset()

    # ------------------------------------------------------------------ #
    # Binding ACL: freee.user sees sync_state but not sync_error          #
    # ------------------------------------------------------------------ #
    def test_freee_user_sees_sync_state_not_sync_error(self):
        """``freee.user`` may read a binding's ``sync_state`` but never
        ``sync_error`` (groups=group_freee_admin) — the error body can echo
        partner / amounts / line text."""
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        binding.sudo().write({"sync_state": "error", "sync_error": "leaky detail"})
        user = new_test_user(
            self.env,
            login="freee-binding-ro",
            groups="connector_freee_base.group_freee_user",
        )
        fields = binding.with_user(user).fields_get()
        self.assertIn("sync_state", fields)
        self.assertNotIn("sync_error", fields)
        self.assertEqual(
            binding.with_user(user).read(["sync_state"])[0]["sync_state"], "error"
        )
