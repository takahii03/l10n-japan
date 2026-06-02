# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, fields, models
from odoo.exceptions import UserError

from odoo.addons.connector_freee_base.models.freee_backend import (
    _REQUEST_INTERVAL_SECONDS,
    FREEE_QUEUE_CHANNEL,
)


class AccountMove(models.Model):
    _inherit = "account.move"

    freee_bind_ids = fields.One2many(
        comodel_name="freee.account.move",
        inverse_name="odoo_id",
        string="freee Bindings",
        copy=False,
    )

    def _ensure_freee_binding(self, backend):
        """Return the binding for ``backend`` (creating one if missing)."""
        self.ensure_one()
        binding = self.freee_bind_ids.filtered(lambda b: b.backend_id == backend)
        if binding:
            return binding
        return self.env["freee.account.move"].create(
            {
                "backend_id": backend.id,
                "odoo_id": self.id,
            }
        )

    def button_cancel(self):
        result = super().button_cancel()
        for move in self:
            exported = move.freee_bind_ids.filtered(lambda b: b.external_id)
            if exported:
                # Mark only — the cron sweeps `to_delete` bindings
                # and dispatches the throttled delete jobs. This gives
                # users a window (until the next cron tick) to undo a
                # mistaken cancel by re-posting the invoice.
                exported.sudo().write({"sync_state": "to_delete"})
        return result

    def _post(self, soft=True):
        result = super()._post(soft=soft)
        # If the invoice was cancelled and re-posted before the cron
        # actually deleted the freee deal, drop the pending DELETE —
        # the existing deal stays as-is. The operator must press
        # *Sync to freee* explicitly to push any edits made in the
        # draft window: connector policy is "updates are manual only"
        # so we never silently rewrite a freee deal once it is on
        # freee's books (matches the electronic-bookkeeping stance on
        # finalized vouchers).
        for move in self:
            stale = move.freee_bind_ids.filtered(
                lambda b: b.sync_state == "to_delete" and b.external_id
            )
            if stale:
                stale.sudo().write({"sync_state": "done"})
        return result

    def action_freee_resync(self):
        """Manual button: enqueue an export job for every authorized
        freee backend matching this invoice's company.

        Creates a binding on the fly when one does not yet exist, so
        the button works equally for the initial export and for
        subsequent re-pushes after edits.
        """
        self.ensure_one()
        if self.move_type not in ("out_invoice", "out_refund"):
            raise UserError(
                _("Only customer invoices and refunds can be exported to freee.")
            )
        if self.state != "posted":
            raise UserError(_("Only posted invoices can be exported to freee."))
        backends = self.env["freee.backend"].search(
            [
                ("state", "=", "authorized"),
                ("company_id", "=", self.company_id.id),
            ]
        )
        if not backends:
            raise UserError(
                _("No authorized freee backend exists for company %s.")
                % self.company_id.display_name
            )
        for index, backend in enumerate(backends):
            binding = self._ensure_freee_binding(backend)
            action = "update" if binding.external_id else "create"
            binding.with_delay(
                description=_("freee %(action)s (manual): %(invoice)s")
                % {
                    "action": action,
                    "invoice": self.display_name,
                },
                eta=index * _REQUEST_INTERVAL_SECONDS,
                channel=FREEE_QUEUE_CHANNEL,
            ).export_invoice()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("freee sync"),
                "message": _("Queued %d export job(s).") % len(backends),
                "type": "success",
                "sticky": False,
            },
        }
