# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging

from odoo import _, api

from odoo.addons.component.core import Component
from odoo.addons.connector_freee_base.components.adapter import FreeeAPIError
from odoo.addons.connector_freee_base.components.job_result import (
    debug_payload,
    format_job_result,
)
from odoo.addons.queue_job.exception import RetryableJobError

_logger = logging.getLogger(__name__)


def _persist_error_state(binding, error_message):
    """Record ``sync_state='error'`` on a separate transaction.

    queue_job rolls the synchronizer's transaction back when the job
    raises, so a plain ``binding.write({"sync_state": "error"})`` inside
    an ``except`` block would be reverted and the binding would stay at
    ``"pending"`` — indistinguishable from "never picked up". Open a
    fresh cursor here so the error status survives that rollback, shows
    up in the Bindings list, and is what the cron's admin-notification
    sweep counts.
    """
    binding.ensure_one()
    with binding.env.registry.cursor() as cr:
        env = api.Environment(cr, binding.env.uid, binding.env.context)
        rec = env[binding._name].browse(binding.id).sudo()
        rec.write(
            {
                "sync_state": "error",
                "sync_error": (error_message or "")[:1000],
            }
        )


class FreeeAccountMoveExporter(Component):
    """Export an Odoo customer invoice as a freee deal.

    The flow is:

    1. Resolve the existing freee deal id (if any) via the binder.
    2. Build the payload via the mapper.
    3. POST a new deal *or* PUT the existing one through the adapter.
    4. Persist the freee id on the binding via the binder.

    The exporter is invoked from ``freee.account.move.export_invoice``
    (running as a queue job). Failures are left to queue_job: the job
    is marked failed and the traceback is recorded on ``queue.job``,
    so there is no in-band error bookkeeping here.

    The return value is shown in the *Result* field of the queue job:
    a non-sensitive operational summary by default, plus the full
    request/response when the backend's ``debug_log_request_payload``
    toggle is on (see ``connector_freee_base.components.job_result``).
    """

    _name = "freee.account.move.exporter"
    _inherit = "freee.exporter"
    _apply_on = "freee.account.move"

    def run(self, binding):
        """Map the bound invoice and create/update the matching freee deal."""
        binding.ensure_one()
        external_id = self.binder.to_external(binding)
        try:
            payload = self.mapper.map_record(binding).values(for_create=not external_id)
            if external_id:
                response = self.backend_adapter.write(external_id, payload)
                method = "PUT"
                endpoint = f"/api/1/deals/{external_id}"
                new_external_id = external_id
            else:
                new_external_id, response = self.backend_adapter.create(payload)
                method = "POST"
                endpoint = "/api/1/deals"
                if not new_external_id:
                    # freee answered 2xx but the response carried no deal
                    # id. Writing ``sync_state='done'`` without binding an
                    # ``external_id`` would make the next cron sweep POST
                    # the same invoice again and duplicate the deal on
                    # freee. Fail loudly instead: the binding flips to
                    # 'error' (below) and the operator must check freee
                    # for the possibly-created deal before re-exporting.
                    raise FreeeAPIError(
                        _(
                            "freee accepted the deal POST for %(invoice)s "
                            "but returned no deal id; the binding was not "
                            "updated. Check on freee whether the deal was "
                            "actually created before re-exporting — a "
                            "blind retry could book it twice."
                        )
                        % {"invoice": binding.odoo_id.display_name}
                    )
        except RetryableJobError:
            # Transient (rate-limit, 5xx, network) — queue_job will
            # reschedule; leave sync_state alone so the next run picks
            # it up exactly like a fresh pending binding. Crucially, do
            # NOT flip it to 'error': that would fire a false admin
            # failure alert for a blip that self-heals on retry.
            raise
        except Exception as err:  # noqa: BLE001 — re-raised after marking
            # Mark the binding error so it survives queue_job's rollback
            # and the cron sweep notifies freee admins. queue_job still
            # records the failure on the queue.job and (for retryable
            # errors) reschedules.
            try:
                _persist_error_state(binding, str(err))
            except Exception:  # noqa: BLE001 — never mask the original
                _logger.exception(
                    "freee: failed to persist error state for binding %s",
                    binding.id,
                )
            raise

        # ``new_external_id`` is guaranteed here: the PUT path reuses the
        # existing id and the POST path raised above when freee returned
        # none.
        self.binder.bind(str(new_external_id), binding)
        binding.sudo().write({"sync_state": "done", "sync_error": False})
        _logger.info(
            "Exported invoice %s as freee deal %s",
            binding.odoo_id.display_name,
            new_external_id,
        )
        # Summary by default: partner / amounts / per-line text are
        # kept out of the unencrypted, broadly-readable result column.
        # The full request + response are added back only when the
        # backend's debug toggle is on (admin, developer mode).
        return format_job_result(
            {
                "invoice": binding.odoo_id.display_name,
                "external_id": new_external_id,
                "method": method,
                "endpoint": endpoint,
                "detail_count": len(payload.get("details") or []),
                "status": "ok",
                **debug_payload(binding, payload, response),
            }
        )


class FreeeAccountMoveDeleter(Component):
    """Delete a previously-exported freee deal.

    Triggered from ``freee.account.move.delete_invoice`` (queue job).
    On success the freee deal id is cleared on the binding while the
    binding row itself is preserved as an audit trail; subsequent
    re-export of the same Odoo invoice will create a fresh deal.
    """

    _name = "freee.account.move.deleter"
    _inherit = "freee.deleter"
    _apply_on = "freee.account.move"

    def run(self, binding):
        """Delete the bound freee deal and clear the binding's external id."""
        binding.ensure_one()
        external_id = binding.external_id
        if not external_id:
            # Nothing on freee yet — nothing to delete.
            return format_job_result(
                {
                    "invoice": binding.odoo_id.display_name,
                    "method": "DELETE",
                    "skipped": "no external_id on binding",
                }
            )
        # Defend against the cancel→re-post race: button_cancel marks
        # the binding to_delete and the cron enqueues this job, but if
        # the user re-posts before the worker picks it up, _post flips
        # the binding back to done. Without this check the deal would
        # be deleted on freee even though the Odoo invoice is live again.
        if binding.sync_state != "to_delete":
            return format_job_result(
                {
                    "invoice": binding.odoo_id.display_name,
                    "external_id": external_id,
                    "method": "DELETE",
                    "skipped": (
                        "binding sync_state changed to "
                        f"{binding.sync_state!r} after the job was queued"
                    ),
                }
            )
        company_id = binding.backend_id.external_company_id
        try:
            response = self.backend_adapter.delete(external_id, company_id)
        except RetryableJobError:
            # Transient (rate-limit, 5xx, network) — queue_job will
            # reschedule. Leave sync_state at 'to_delete': flipping it to
            # 'error' here would make the retry hit the sync_state !=
            # 'to_delete' guard above and silently skip the delete, so
            # the freee deal would never be removed.
            raise
        except Exception as err:  # noqa: BLE001 — re-raised after marking
            try:
                _persist_error_state(binding, str(err))
            except Exception:  # noqa: BLE001 — never mask the original
                _logger.exception(
                    "freee: failed to persist error state for binding %s",
                    binding.id,
                )
            raise

        binding.sudo().write(
            {
                "external_id": False,
                "sync_state": "done",
                "sync_error": False,
            }
        )
        _logger.info(
            "Deleted freee deal %s for invoice %s",
            external_id,
            binding.odoo_id.display_name,
        )
        # Summary by default; full request/response only with the
        # backend debug toggle on.
        return format_job_result(
            {
                "invoice": binding.odoo_id.display_name,
                "external_id": external_id,
                "method": "DELETE",
                "endpoint": f"/api/1/deals/{external_id}",
                "status": "ok",
                **debug_payload(binding, {"company_id": company_id}, response),
            }
        )
