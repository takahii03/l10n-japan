# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, fields, models


class AccountJournal(models.Model):
    _inherit = "account.journal"

    # Optional sales-returns account_item for refunds (Air Regi-style).
    # When set, every line of an exported credit note (``out_refund``) on
    # this journal is booked to this freee account item instead of the
    # line's own account, so returns land in a dedicated contra-revenue
    # account. Left unset → refund lines keep their own (negative) account
    # mapping. Lives on the journal (alongside the freee account-item split
    # and the settlement walletable) and is invoice-specific, so it stays
    # in this feature module — connector_freee_base knows nothing about
    # refunds.
    freee_refund_account_item_id = fields.Integer(
        string="freee Refund Account Item (sales returns)",
        copy=False,
        help="Optional. freee ``account_item`` id used for every line of an "
        "exported credit note (out_refund) on this journal. Typically a "
        "sales-returns or sales-discount account. Leave empty to keep each "
        "refund line on its own (negative) account-item mapping.",
    )
    freee_refund_account_item_name = fields.Char(
        string="freee Refund Account Item Name",
        copy=False,
        help="Display name of the matching freee account_item, captured "
        "when the id is set via the picker wizard. Display-only.",
    )

    def action_open_freee_refund_account_item_wizard(self):
        """Open the account-item picker for this journal's sales-returns
        account (used on exported credit notes)."""
        self.ensure_one()
        wizard = self.env["freee.account.item.fetch.wizard"].create(
            {"journal_refund_id": self.id}
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Map freee Refund Account Item"),
            "res_model": "freee.account.item.fetch.wizard",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_clear_freee_refund_account_item(self):
        """Unmap the sales-returns account → credit-note lines on this journal
        fall back to their own (negative) account mapping."""
        self.write(
            {
                "freee_refund_account_item_id": False,
                "freee_refund_account_item_name": False,
            }
        )
