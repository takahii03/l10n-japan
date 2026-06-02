# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

"""Deal CRUD parsing asserted against a *real* freee deal payload.

``connector_freee_base/tests/fixtures/deal.json`` is a verbatim
``GET /api/1/deals/{id}`` response captured live from a freee
development company. freee returns the same ``{"deal": {...}}`` envelope
for create/update, so it doubles as the create/write fixture. No live
write is ever made — only read-only capture was used to build it.
"""

from contextlib import contextmanager
from datetime import timedelta
from unittest import mock

from odoo import fields

from odoo.addons.connector_freee_base.tests.common import (
    load_fixture,
    make_response,
)

from .common import FreeeInvoiceTestCommon

ADAPTER_REQUESTS = "odoo.addons.connector_freee_base.components.adapter.requests"

REAL_DEAL = load_fixture("deal")  # {"deal": {...}}
REAL_DEAL_ID = REAL_DEAL["deal"]["id"]  # 3520381214


class TestRealDealPayload(FreeeInvoiceTestCommon):
    def setUp(self):
        super().setUp()
        self.backend._write_secret("encrypted_access_token", "AT-test")
        self.backend.sudo().write(
            {
                "state": "authorized",
                "token_expires_at": fields.Datetime.now() + timedelta(hours=1),
            }
        )

    def _adapter(self):
        with self.backend.work_on("freee.account.move") as work:
            return work.component(
                usage="backend.adapter", model_name="freee.account.move"
            )

    @contextmanager
    def _freee(self):
        with mock.patch(f"{ADAPTER_REQUESTS}.request") as req:
            req.return_value = make_response(200, json_body=REAL_DEAL)
            yield req

    def test_create_parses_real_deal_envelope(self):
        adapter = self._adapter()
        with self._freee() as req:
            external_id, deal = adapter.create({"company_id": 12195334})
        self.assertEqual(external_id, REAL_DEAL_ID)
        self.assertEqual(deal["ref_number"], "INV/2026/00008")
        # Nested line items survive the round-trip unchanged.
        self.assertEqual(len(deal["details"]), 4)
        self.assertEqual(
            deal["details"][0]["description"], "[E-COM06] Corner Desk Right Sit"
        )
        self.assertEqual(deal["details"][0]["tax_code"], 129)
        # The real call is POST /api/1/deals.
        self.assertEqual(req.call_args.args[0], "POST")
        self.assertTrue(req.call_args.args[1].endswith("/api/1/deals"))

    def test_read_parses_real_deal(self):
        adapter = self._adapter()
        with self._freee() as req:
            deal = adapter.read(REAL_DEAL_ID)
        self.assertEqual(deal["id"], REAL_DEAL_ID)
        self.assertEqual(deal["type"], "income")
        self.assertEqual(req.call_args.args[0], "GET")
        self.assertTrue(req.call_args.args[1].endswith(f"/api/1/deals/{REAL_DEAL_ID}"))

    def test_write_targets_real_deal_and_parses(self):
        adapter = self._adapter()
        with self._freee() as req:
            deal = adapter.write(REAL_DEAL_ID, {"company_id": 12195334})
        self.assertEqual(deal["id"], REAL_DEAL_ID)
        self.assertEqual(req.call_args.args[0], "PUT")
        self.assertTrue(req.call_args.args[1].endswith(f"/api/1/deals/{REAL_DEAL_ID}"))

    def test_full_export_binds_real_external_id(self):
        # End-to-end: a posted invoice exported through the real adapter
        # binds to the deal id freee actually returned.
        invoice = self._make_invoice()
        binding = self._make_binding(invoice)
        with self._freee():
            binding.export_invoice()
        self.assertEqual(binding.external_id, str(REAL_DEAL_ID))
        self.assertEqual(binding.sync_state, "done")
