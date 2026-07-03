# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

"""Invoice-specific freee.backend behaviour.

The nightly invoice export sweep (the cron entry, the per-backend
enqueue loop and the aggregate failure alert) is fully self-contained
here. Each freee feature module (invoice, future purchase / pos) owns
its own cron and sweep — they are siblings of each other on top of
``connector_freee_base``, never stacked through a base-level hook.
Master-data sync stays in ``connector_freee_base`` so its wizards
cover every feature module.
"""

import logging

from odoo import _, api, models

from odoo.addons.connector_freee_base.models.freee_backend import (
    _REQUEST_INTERVAL_SECONDS,
    FREEE_QUEUE_CHANNEL,
)
from odoo.addons.queue_job.job import identity_exact

_logger = logging.getLogger(__name__)


class FreeeBackend(models.Model):
    _inherit = "freee.backend"

    @api.model
    def _cron_export_pending_invoices(self):
        """Nightly sweep entry point for the invoice export cron.

        Loops over authorized backends and asks each to enqueue its
        pending invoice export / update / delete jobs.

        Each backend is wrapped in its own try/except so a single
        misconfigured / temporarily-broken backend cannot abort the
        cron run for the others — that used to mean the whole day's
        exports waited until tomorrow. A failing backend gets a
        deduplicated admin to-do via the standard activity mechanism.
        """
        for backend in self.search([("state", "=", "authorized")]):
            try:
                backend._export_pending_invoices()
            except Exception as err:  # noqa: BLE001 — sweep must continue
                _logger.exception(
                    "freee: cron sweep failed for backend %s; continuing with "
                    "the next one",
                    backend.name,
                )
                try:
                    backend._notify_freee_admins(
                        "connector_freee_invoice.mail_activity_freee_export_failures",
                        _("freee export sweep failed for backend '%s'") % backend.name,
                        _(
                            "The nightly cron raised %(type)s while sweeping "
                            "backend '%(backend)s'. The other backends were "
                            "processed; this one will be retried on the next "
                            "cron tick. Detail: %(err)s"
                        )
                        % {
                            "type": type(err).__name__,
                            "backend": backend.name,
                            "err": err,
                        },
                    )
                except Exception:  # noqa: BLE001 — alerting must never re-raise
                    _logger.exception(
                        "freee: failed to schedule sweep-failure activity "
                        "for backend %s",
                        backend.id,
                    )

    def _export_pending_invoices(self):
        """Sweep posted customer invoices that have not yet been exported
        to this backend, plus bindings flagged for deletion, and enqueue
        an export / delete job for each.

        The cron handles **create** (initial POST) and **delete** only.
        Updates (PUT) are restricted to the manual *Sync to freee*
        button — posted invoices are finalized vouchers and we deliberately
        do not let a background sweep rewrite them on freee.

        Filters:

        * ``invoice_date >= backend.export_from_date`` so a fresh
          authorize does not retroactively grab every historical
          invoice. ``export_from_date`` is stamped to today on first
          authorize and admin-editable for back-dated catch-up.
        * ``invoice_date > backend.lock_date`` so the connector never
          tries to write into a closed (locked) freee period — the
          exporter would just hit a freee-side rejection.
        """
        self.ensure_one()
        already_exported = self.env["freee.account.move"].search(
            [
                ("backend_id", "=", self.id),
                ("external_id", "!=", False),
            ]
        )
        domain = [
            ("state", "=", "posted"),
            ("move_type", "in", ("out_invoice", "out_refund")),
            ("company_id", "=", self.company_id.id),
            ("id", "not in", already_exported.mapped("odoo_id.id")),
        ]
        if self.export_from_date:
            domain.append(("invoice_date", ">=", self.export_from_date))
        if self.lock_date:
            domain.append(("invoice_date", ">", self.lock_date))
        candidates = self.env["account.move"].search(domain)
        # Delete path: bindings whose Odoo invoice was cancelled
        # (account.move.button_cancel marked them "to_delete"). The DELETE
        # is dispatched here, not synchronously on cancel, so users can
        # undo a mistaken cancel by re-posting before the next cron run.
        delete_bindings = self.env["freee.account.move"].search(
            [
                ("backend_id", "=", self.id),
                ("external_id", "!=", False),
                ("sync_state", "=", "to_delete"),
            ]
        )
        # ``identity_key=identity_exact`` so a nightly re-sweep does not
        # stack a second job on a binding whose job from a previous run
        # is still pending/enqueued. Without it the duplicate job would
        # find ``external_id`` already bound and silently turn into a
        # PUT — contradicting the "updates are manual only" policy.
        # (queue_job only dedups against waiting/pending/enqueued jobs,
        # so failed jobs never block the nightly retry.)
        eta_index = 0
        for move in candidates:
            binding = move._ensure_freee_binding(self)
            binding.with_delay(
                description=_("freee create: %s") % move.display_name,
                eta=eta_index * _REQUEST_INTERVAL_SECONDS,
                channel=FREEE_QUEUE_CHANNEL,
                identity_key=identity_exact,
            ).export_invoice()
            eta_index += 1
        for binding in delete_bindings:
            binding.with_delay(
                description=_("freee delete: %s") % binding.odoo_id.display_name,
                eta=eta_index * _REQUEST_INTERVAL_SECONDS,
                channel=FREEE_QUEUE_CHANNEL,
                identity_key=identity_exact,
            ).delete_invoice()
            eta_index += 1
        self._notify_export_failures()

    def _notify_export_failures(self):
        """Raise (or retract) one aggregate admin alert per backend.

        Per-binding ``sync_state='error'`` is invisible unless someone
        looks; a token expiry or freee outage during the nightly cron
        can otherwise go unnoticed until month-end. This turns the
        sweep's view of failed bindings into a single deduplicated
        to-do for freee admins (plus an Inbox/email push), cleared
        automatically once every binding recovers.
        """
        self.ensure_one()
        error_count = self.env["freee.account.move"].search_count(
            [
                ("backend_id", "=", self.id),
                ("sync_state", "=", "error"),
            ]
        )
        if error_count:
            self._notify_freee_admins(
                "connector_freee_invoice.mail_activity_freee_export_failures",
                _("freee export failures need attention"),
                _(
                    "%(n)s invoice(s) failed to export to freee backend "
                    "'%(backend)s'. Review them under Accounting → freee "
                    "(sync state = Error), fix the cause (often a stale "
                    "token or a freee-side validation error), then re-post "
                    "or use the 'Sync to freee' button. This alert clears "
                    "automatically once all bindings recover."
                )
                % {"n": error_count, "backend": self.name},
            )
        else:
            self._clear_freee_admin_activities(
                "connector_freee_invoice.mail_activity_freee_export_failures"
            )
