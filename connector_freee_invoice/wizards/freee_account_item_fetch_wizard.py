# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

"""Invoice-specific extension of the base account-item picker wizard.

The shared ``freee.account.item.fetch.wizard`` (connector_freee_base) maps a
freee account item onto an ``account.account`` or ``account.journal`` (writing
``freee_account_item_id``). The invoice module adds one more target — a
journal's **sales-returns** account (``freee_refund_account_item_id``, used on
exported credit notes) — without teaching the base wizard about refunds: we
only add a target field and route ``action_select`` to it when set.
"""

from odoo import fields, models


class FreeeAccountItemFetchWizard(models.TransientModel):
    _inherit = "freee.account.item.fetch.wizard"

    # When set, the picked account_item is written onto this journal's
    # ``freee_refund_account_item_id`` (distinct from the regular
    # ``journal_id`` target, which writes the sales-split account item).
    journal_refund_id = fields.Many2one(
        "account.journal",
        readonly=True,
        ondelete="cascade",
    )


class FreeeAccountItemFetchWizardLine(models.TransientModel):
    _inherit = "freee.account.item.fetch.wizard.line"

    def action_select(self):
        self.ensure_one()
        journal_refund = self.wizard_id.journal_refund_id
        if journal_refund:
            # No sudo(): writing account.journal already requires the native
            # accounting group on top of group_freee_admin, the same
            # dual-permission gate the base wizard relies on.
            journal_refund.write(
                {
                    "freee_refund_account_item_id": self.external_id,
                    "freee_refund_account_item_name": self.name,
                }
            )
            return {"type": "ir.actions.act_window_close"}
        return super().action_select()
