# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class FreeeAccountMove(models.Model):
    """Binding between an Odoo ``account.move`` and a freee ``deal``.

    One row per (backend, invoice) pair. ``external_id`` holds the freee
    deal id once the invoice has been exported successfully.
    """

    _name = "freee.account.move"
    _inherit = "external.binding"
    _inherits = {"account.move": "odoo_id"}
    _description = "freee Binding for Account Move"

    backend_id = fields.Many2one(
        comodel_name="freee.backend",
        string="freee Backend",
        required=True,
        ondelete="restrict",
        index=True,
    )
    # ondelete="restrict" — the legacy ``cascade`` silently dropped
    # the binding when somebody force-deleted the Odoo invoice, leaving
    # the freee deal orphaned on freee's side with no Odoo audit trail
    # of its existence. Restricting forces the normal cancel→to_delete
    # path (``account.move.button_cancel`` → cron-dispatched DELETE) to
    # run first; only after the deleter clears ``external_id`` does
    # ``unlink`` of the binding (and indirectly the move) become legal.
    odoo_id = fields.Many2one(
        comodel_name="account.move",
        string="Invoice",
        required=True,
        index=True,
        ondelete="restrict",
    )
    external_id = fields.Char(
        string="freee Deal ID",
        help="Identifier of the deal on freee. Empty until the first "
        "successful export.",
        copy=False,
        index=True,
    )
    sync_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("done", "Synced"),
            ("error", "Error"),
            ("to_delete", "To Delete"),
        ],
        default="pending",
        required=True,
        readonly=True,
        copy=False,
        help="Lifecycle state of the freee deal:\n"
        "- Pending: needs an export (initial create or re-push after edit).\n"
        "- Synced: in sync with freee.\n"
        "- Error: last export/delete failed; see Last Error. freee admins "
        "are notified (To-Do + Inbox/email) by the next cron sweep.\n"
        "- To Delete: invoice was cancelled, the next cron run will "
        "DELETE the deal on freee.",
    )
    # ``sync_error`` is the verbatim freee error body, which can carry
    # partner names, amounts, and per-line text. Gate it behind the
    # freee admin group so a regular ``freee.user`` (who can read the
    # binding to see ``sync_state``) does not get to see the detail.
    sync_error = fields.Text(
        readonly=True,
        copy=False,
        groups="connector_freee_base.group_freee_admin",
        help="Last error returned by freee when exporting this invoice. "
        "Restricted to freee admins because freee echoes the offending "
        "payload (partner, amounts, line text) back in the error body.",
    )

    # PostgreSQL treats NULL as "distinct" in unique indexes, so the
    # second constraint allows any number of bindings with
    # ``external_id IS NULL`` (the "never exported yet" state).
    # ``freee_account_move_uniq`` keeps even those un-exported rows
    # one-per-(backend, invoice), so we never duplicate-bind the same
    # Odoo invoice within a backend. Together: at most one binding per
    # invoice per backend, and at most one freee deal id per backend.
    _sql_constraints = [
        (
            "freee_account_move_uniq",
            "unique(backend_id, odoo_id)",
            "An invoice can only be bound once per freee backend.",
        ),
        (
            "freee_account_move_external_uniq",
            "unique(backend_id, external_id)",
            "A freee deal id can only be bound once per backend.",
        ),
    ]

    def unlink(self):
        """Refuse to drop a binding that still points at a live freee
        deal.

        Without this, a freee admin could ``unlink`` the binding and
        leave the corresponding freee deal orphaned — no Odoo record
        of it exists, but the deal still shows up on freee's side and
        skews the journal-entry totals at year-end close. The supported
        delete path is
        ``button_cancel`` on the Odoo invoice → ``sync_state =
        to_delete`` → cron-dispatched DELETE → ``external_id`` cleared
        by the deleter → binding now safe to drop.
        """
        live = self.filtered(lambda b: b.external_id)
        if live:
            # Surfacing the freee deal id is intentional and safe here:
            # this UserError is only seen by callers who already hold
            # unlink permission on freee.account.move (freee admins only —
            # account managers get read/write/create but no unlink, see
            # ir.model.access.csv). Deal ids are not sensitive on their
            # own.
            names = ", ".join(
                f"{b.odoo_id.display_name} (deal {b.external_id})" for b in live[:5]
            )
            raise UserError(
                _(
                    "Cannot delete %(count)s freee binding(s) that still point "
                    "at a live freee deal: %(names)s%(suffix)s. Cancel the "
                    "Odoo invoice first so the connector deletes the deal on "
                    "freee, then the binding can be removed."
                )
                % {
                    "count": len(live),
                    "names": names,
                    "suffix": ", ..." if len(live) > 5 else "",
                }
            )
        return super().unlink()

    def export_invoice(self):
        """Export this binding's invoice to freee.

        Called by ``queue_job`` (via ``with_delay``) and intended to
        be the only public entry-point. The actual flow lives in the
        ``record.exporter`` component.
        """
        self.ensure_one()
        with self.backend_id.work_on(self._name) as work:
            exporter = work.component(usage="record.exporter")
            return exporter.run(self)

    def delete_invoice(self):
        """Delete this binding's freee deal.

        Called by ``queue_job`` (via ``with_delay``) when the Odoo
        invoice is cancelled. The binding row is kept (with
        ``external_id`` cleared) so that re-posting the invoice
        creates a fresh deal on freee.
        """
        self.ensure_one()
        with self.backend_id.work_on(self._name) as work:
            deleter = work.component(usage="record.deleter")
            return deleter.run(self)
