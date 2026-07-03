# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

"""Multi-company isolation.

Each ``freee.backend`` drives exactly one freee company for exactly one
Odoo company. The nightly sweep
(``connector_freee_invoice.models.freee_backend._export_pending_invoices``)
filters ``account.move`` by ``company_id = backend.company_id``. These
tests prove that boundary actually holds — a company-B backend must
never sweep, bind, or enqueue a job for a company-A invoice — and that
the ``unique(name, company_id)`` constraint still allows the same
backend name to be reused in another company.
"""

from psycopg2 import IntegrityError

from odoo.tools import mute_logger

from odoo.addons.queue_job.tests.common import trap_jobs

from .common import FreeeInvoiceTestCommon


class TestMultiCompanyIsolation(FreeeInvoiceTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_b = cls.env["res.company"].create({"name": "freee Co B"})
        # A second backend bound to the other company. It does not need
        # to be authorized: the tests call _export_pending_invoices()
        # directly (as the other suites do), which keys off company_id.
        cls.backend_b = cls.env["freee.backend"].create(
            {
                "name": cls.backend.name,  # same name, different company
                "company_id": cls.company_b.id,
                "client_id": "cid-b",
                "client_secret": "cse-b",
                "external_company_id": 9999,
            }
        )

    def test_sweep_does_not_cross_company_boundary(self):
        """A company-B backend must not sweep a company-A invoice."""
        invoice = self._make_invoice()  # company A (self.env.company)
        invoice.action_post()

        # Company-B backend sweeps: it must see nothing of company A.
        with trap_jobs() as trap:
            self.backend_b._export_pending_invoices()
        self.assertFalse(
            trap.enqueued_jobs,
            "company B backend enqueued a job for a company A invoice",
        )
        self.assertFalse(
            self.env["freee.account.move"].search(
                [("backend_id", "=", self.backend_b.id)]
            ),
            "company B backend created a binding for a company A invoice",
        )

        # The matching company-A backend does pick it up.
        with trap_jobs() as trap:
            self.backend._export_pending_invoices()
        self.assertTrue(trap.enqueued_jobs, "company A backend swept nothing")
        bindings = self.env["freee.account.move"].search(
            [("backend_id", "=", self.backend.id)]
        )
        # The test invoice is picked up...
        self.assertIn(
            invoice,
            bindings.odoo_id,
            "company A backend did not pick up its own invoice",
        )
        # ...and the isolation holds: every bound move belongs to company A,
        # never company B. (Asserted this way rather than equality so the test
        # is robust to any other company-A invoices present in the database,
        # e.g. demo data on the OCA CI runner.)
        self.assertTrue(
            all(m.company_id == self.backend.company_id for m in bindings.odoo_id),
            "company A backend bound an invoice from another company",
        )

    def test_same_backend_name_allowed_across_companies(self):
        """unique(name, company_id) is per-company: setUpClass already
        created two backends sharing a name in different companies, so
        the constraint must have accepted them."""
        self.assertEqual(self.backend.name, self.backend_b.name)
        self.assertNotEqual(self.backend.company_id, self.backend_b.company_id)

    def test_duplicate_backend_name_same_company_rejected(self):
        """...but a duplicate name *within* one company is still
        rejected, so the constraint is scoped, not disabled."""
        with (
            self.assertRaises(IntegrityError),
            mute_logger("odoo.sql_db"),
            self.cr.savepoint(),
        ):
            self.env["freee.backend"].create(
                {
                    "name": self.backend.name,
                    "company_id": self.backend.company_id.id,
                }
            )
