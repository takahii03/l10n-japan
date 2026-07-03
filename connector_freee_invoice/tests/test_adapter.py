# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from unittest import mock

from odoo.addons.connector_freee_base.components.adapter import FreeeAdapter

from .common import FreeeInvoiceTestCommon


class TestDealsAdapter(FreeeInvoiceTestCommon):
    def _adapter(self):
        with self.backend.work_on("freee.account.move") as work:
            return work.component(
                usage="backend.adapter", model_name="freee.account.move"
            )

    def test_create_returns_external_id(self):
        adapter = self._adapter()
        with mock.patch.object(FreeeAdapter, "post") as post:
            post.return_value = {"deal": {"id": 12345, "ref_number": "INV/0001"}}
            external_id, deal = adapter.create({"company_id": 1})
        self.assertEqual(external_id, 12345)
        self.assertEqual(deal["ref_number"], "INV/0001")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "/api/1/deals")

    def test_write_targets_specific_deal(self):
        adapter = self._adapter()
        with mock.patch.object(FreeeAdapter, "put") as put:
            put.return_value = {"deal": {"id": 7}}
            adapter.write(7, {"company_id": 1})
        self.assertEqual(put.call_args.args[0], "/api/1/deals/7")
