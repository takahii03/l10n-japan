# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

"""The per-journal sales-returns (refund) account mapping: the open / clear
buttons on the journal and the picker write-back. The mapper consumption of
``freee_refund_account_item_id`` is covered by ``test_mapper``.
"""

from .common import FreeeInvoiceTestCommon


class TestJournalRefundAccount(FreeeInvoiceTestCommon):
    def setUp(self):
        super().setUp()
        # The picker wizard's ``backend_id`` defaults to the first authorized
        # backend (required), so authorize the test backend.
        self.backend.sudo().write({"state": "authorized"})

    def _journal(self):
        return self.env["account.journal"].search(
            [("type", "=", "sale")], limit=1
        ) or self.env["account.journal"].create(
            {"name": "Sales", "type": "sale", "code": "FRSJ"}
        )

    def test_open_refund_wizard_binds_journal(self):
        journal = self._journal()
        action = journal.action_open_freee_refund_account_item_wizard()
        self.assertEqual(action["type"], "ir.actions.act_window")
        self.assertEqual(action["res_model"], "freee.account.item.fetch.wizard")
        wizard = self.env["freee.account.item.fetch.wizard"].browse(action["res_id"])
        self.assertEqual(wizard.journal_refund_id, journal)

    def test_picker_writes_refund_account_onto_journal(self):
        journal = self._journal()
        wizard = self.env["freee.account.item.fetch.wizard"].create(
            {"journal_refund_id": journal.id}
        )
        line = self.env["freee.account.item.fetch.wizard.line"].create(
            {"wizard_id": wizard.id, "external_id": 5555, "name": "Sales returns"}
        )
        line.action_select()
        self.assertEqual(journal.freee_refund_account_item_id, 5555)
        self.assertEqual(journal.freee_refund_account_item_name, "Sales returns")

    def test_clear_refund_account(self):
        journal = self._journal()
        journal.write(
            {
                "freee_refund_account_item_id": 5555,
                "freee_refund_account_item_name": "Sales returns",
            }
        )
        journal.action_clear_freee_refund_account_item()
        self.assertFalse(journal.freee_refund_account_item_id)
        self.assertFalse(journal.freee_refund_account_item_name)
