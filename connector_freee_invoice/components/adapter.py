# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging

from odoo.addons.component.core import Component

_logger = logging.getLogger(__name__)


# pylint: disable=W8106
class FreeeAccountMoveAdapter(Component):
    """CRUD operations on freee ``/api/1/deals``.

    Single-inherits ``freee.adapter`` (the low-level HTTP client in
    ``connector_freee_base``) so the model-specific surface — create /
    write / read / delete around the deals endpoint — can call
    ``self.post`` / ``self.put`` / ``self.get`` / ``self.delete``
    directly.
    """

    _name = "freee.account.move.adapter"
    _inherit = "freee.adapter"
    _apply_on = "freee.account.move"

    _endpoint = "/api/1/deals"

    def create(self, payload):
        """POST a new deal and return ``(external_id, deal_dict)``."""
        # Log a summary, not the payload: it carries partner / amounts /
        # per-line text into the (unencrypted, broadly-readable) log.
        _logger.info(
            "freee POST %s (%d detail line(s))",
            self._endpoint,
            len(payload.get("details") or []),
        )
        response = self.post(self._endpoint, payload=payload)
        deal = (response or {}).get("deal") or {}
        external_id = deal.get("id")
        return external_id, deal

    def write(self, external_id, payload):
        """PUT updates to an existing deal and return the deal dict."""
        # Summary only — see create().
        _logger.info(
            "freee PUT %s/%s (%d detail line(s))",
            self._endpoint,
            external_id,
            len(payload.get("details") or []),
        )
        response = self.put(f"{self._endpoint}/{external_id}", payload=payload)
        return (response or {}).get("deal") or {}

    def read(self, external_id):
        """GET an existing deal and return its decoded dict."""
        response = self.get(f"{self._endpoint}/{external_id}")
        return (response or {}).get("deal") or {}

    def delete(self, external_id, company_id):
        """DELETE a deal; ``company_id`` is required by the freee API."""
        # freee's DELETE /api/1/deals/{id} requires the freee company id
        # as a query parameter — there is no body to carry it on.
        _logger.info(
            "freee DELETE %s/%s company_id=%s",
            self._endpoint,
            external_id,
            company_id,
        )
        return super().delete(
            f"{self._endpoint}/{external_id}",
            params={"company_id": company_id},
        )
