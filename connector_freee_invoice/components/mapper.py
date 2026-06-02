# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _

from odoo.addons.component.core import Component
from odoo.addons.connector.components.mapper import mapping, only_create
from odoo.addons.connector.exception import MappingError


class FreeeAccountMoveExportMapper(Component):
    """Convert an Odoo customer invoice into a freee deal payload.

    The output dict matches the body the freee ``POST /api/1/deals``
    endpoint expects. The mapper is intentionally split into small
    methods so downstream addons can override individual pieces without
    rewriting the whole transformation.
    """

    _name = "freee.account.move.export.mapper"
    # freee.export.mapper (connector_freee_base) supplies _collection,
    # _usage="export.mapper" and the master-data resolution helpers
    # (_map_account_item, _pick_freee_tax, _map_section, _map_item,
    # _map_partner). This mapper keeps only deal-payload shaping.
    _inherit = "freee.export.mapper"
    _apply_on = "freee.account.move"

    @mapping
    def company_id(self, record):
        return {"company_id": record.backend_id.external_company_id}

    @mapping
    def issue_date(self, record):
        invoice = record.odoo_id
        if not invoice.invoice_date:
            raise MappingError(
                _("Invoice %s has no invoice_date; cannot export to freee.")
                % invoice.display_name
            )
        # Refuse to export / update transactions inside a closed (locked)
        # period — freee rejects edits there and the binding would loop
        # in error. Surfaced here so the operator sees a clear message
        # before the request even leaves Odoo.
        record.backend_id._check_lock_date(invoice.invoice_date)
        return {"issue_date": invoice.invoice_date.isoformat()}

    @mapping
    def due_date(self, record):
        # freee deal ``due_date`` = the settlement due date (when the money
        # is due). Maps from Odoo's invoice due date. It is optional on
        # freee, so
        # omit the key entirely when unset — freee rejects ``false``/
        # ``null`` against the date-typed field (same reason the
        # optional detail keys are conditionally included).
        invoice = record.odoo_id
        if not invoice.invoice_date_due:
            return {}
        return {"due_date": invoice.invoice_date_due.isoformat()}

    @mapping
    def deal_type(self, record):
        # Both customer invoices and refunds map to freee ``type="income"``.
        # A refund (out_refund) is exported as a *negative-amount* income
        # deal — freee's red-slip / negative-line entry. The freee Deals
        # API accepts negative ``details[].amount`` on an income deal:
        # verified against the official OpenAPI schema (v2020_06_15 —
        # ``amount`` minimum is a negative int64 and the field doc states a
        # negative value is registered as a deduction / negative line). The
        # per-line negative sign is applied in ``_build_details``. Keeping
        # refunds as income leaves them on the sales side of freee's P&L
        # statement instead of misclassifying them as an expense (the old
        # expense-flip needed a manual sales-returns reclass on freee). This
        # module only ever exports out_invoice / out_refund, so the type is
        # always "income".
        return {"type": "income"}

    @mapping
    def reference(self, record):
        invoice = record.odoo_id
        return {"ref_number": invoice.name or ""}

    @mapping
    def partner(self, record):
        # Resolution precedence (freee_partner_id → freee_partner_code)
        # lives in freee.export.mapper.
        return self._map_partner(record.odoo_id.partner_id)

    @mapping
    def details(self, record):
        return {"details": self._build_details(record.odoo_id)}

    @only_create
    @mapping
    def payments(self, record):
        """Settle the deal through the journal's mapped freee walletable.

        If the invoice's ``account.journal`` has a freee walletable
        mapped (set via the picker wizard in connector_freee_base), the
        deal is exported as **settled**: one ``payments[]`` entry for
        the full tax-inclusive total, paid from that walletable on the
        issue date. With no walletable mapped — the default — the
        ``payments`` key is omitted entirely and freee keeps the deal
        **unsettled**.

        Refunds (``out_refund``) settle the same way when the journal
        has a walletable, but with a **negative** ``payments[].amount``
        (money flowing back out of the walletable) — freee accepts a
        negative amount
        on a deal's inline ``payments`` (verified against the OpenAPI
        schema ``v2020_06_15``: ``dealCreateParams.payments[].amount``
        allows negative int64, unlike the standalone ``paymentParams``).
        This mirrors real-world POS behaviour (e.g. a cash refund is
        settled). ``total`` is taken from the built details, which are
        already negative for a refund, so the sign falls out naturally.
        The connector does not track real settlement timing, so it books
        the payment on the invoice date — a documented simplification.

        Note: ``dealUpdateParams`` has no ``payments`` field, so a PUT
        (manual *Sync to freee* on an already-exported deal) cannot
        change settlement — only the initial POST carries it. That is
        why this mapping is decorated with ``@only_create``: the
        exporter maps updates with ``values(for_create=False)`` and the
        ``payments`` key must not leak into the PUT payload.
        """
        invoice = record.odoo_id
        journal = invoice.journal_id
        if not journal.freee_walletable_id:
            return {}
        total = sum(detail["amount"] for detail in self._build_details(invoice))
        return {
            "payments": [
                {
                    "amount": total,
                    "from_walletable_id": journal.freee_walletable_id,
                    "from_walletable_type": journal.freee_walletable_type,
                    "date": invoice.invoice_date.isoformat(),
                }
            ]
        }

    # ------------------------------------------------------------------ #
    # Internals — split out so subclasses can override piecewise.        #
    # ------------------------------------------------------------------ #
    def _build_details(self, invoice):
        # freee Accounting is a JPY-only ledger. A non-JPY invoice would
        # be silently posted with its foreign-currency figures treated as
        # yen, so refuse it loudly instead — there is no correct
        # conversion the connector can make on its own.
        if invoice.currency_id.name != "JPY":
            raise MappingError(
                _(
                    "Invoice %(invoice)s is in %(currency)s; the freee "
                    "connector only exports JPY (¥) invoices. Re-issue it "
                    "in JPY or exclude it from freee export."
                )
                % {
                    "invoice": invoice.display_name,
                    "currency": invoice.currency_id.name or _("(none)"),
                }
            )
        is_refund = invoice.move_type == "out_refund"
        sign = -1 if is_refund else 1
        # Optional sales-returns-account override (Air Regi-style): when the
        # invoice's journal has a refund account_item mapped, every refund
        # detail line is booked to it instead of the line's own account, so
        # the return lands in a dedicated contra-revenue account rather than
        # netting against the original sales account. Left unset → refund
        # lines keep their own (negative) account mapping, as before.
        refund_account_item = (
            invoice.journal_id.freee_refund_account_item_id or 0 if is_refund else 0
        )
        export_lines = [
            line
            for line in invoice.invoice_line_ids
            if line.display_type not in ("line_section", "line_note")
        ]
        # ``details[].amount`` (tax-inclusive) and ``details[].vat`` are
        # integer yen. We do NOT derive vat from ``price_total -
        # price_subtotal``: that display value rounds half-up and ignores the
        # company/partner consumption-tax rounding method
        # (account_tax_rounding_method: HALF-UP / UP / DOWN). ``_vat_by_line``
        # instead follows freee's per-document rule (round once per rate, put
        # the residual on the largest-amount line). All values are cast to
        # ``int`` so they JSON-serialise as bare integers (freee rejects a
        # float such as ``1100.0``).
        vat_by_line = self._vat_by_line(invoice, export_lines)
        details = []
        for line in export_lines:
            tax = self._pick_freee_tax(line)
            vat = vat_by_line[line.id]
            if self._line_taxes_price_included(line):
                # Tax-included: the entered tax-inclusive price is
                # authoritative (the rounding-method module skips DOWN here
                # too), so take Odoo's inclusive total as-is.
                amount = self._int_yen(line.price_total, "amount", line)
            else:
                # Tax-excluded: inclusive = net + the method-rounded vat.
                amount = self._int_yen(line.price_subtotal, "amount", line) + vat
            detail = {
                "account_item_id": refund_account_item or self._map_account_item(line),
                "tax_code": tax.freee_tax_code,
                "amount": sign * amount,
                "vat": sign * vat,
            }
            if line.name:
                detail["description"] = line.name
            # Optional fields: only include when set, otherwise freee
            # rejects ``false`` against integer-typed fields like
            # ``section_id`` with a generic validation error.
            section_id = self._map_section(line)
            if section_id:
                detail["section_id"] = section_id
            item_id = self._map_item(line)
            if item_id:
                detail["item_id"] = item_id
            details.append(detail)
        return details

    @staticmethod
    def _int_yen(value, label, line):
        """Return ``value`` as integer yen for the freee Deals API.

        freee types ``details[].amount`` / ``vat`` (and ``payments[].amount``,
        derived from these) as integer. An Odoo monetary float such as
        ``1100.0`` serialises with a decimal point and freee rejects it, so
        cast to ``int``. JPY uses ``decimal_places=0`` so the value is whole
        and the cast is exact; a non-whole value (mis-configured currency
        precision) raises a clear error instead of being silently truncated.
        """
        rounded = round(value)
        if abs(value - rounded) > 1e-6:
            raise MappingError(
                _(
                    "Invoice line '%(line)s' on %(invoice)s produced a "
                    "non-integer %(label)s (%(value)s); freee accepts only "
                    "whole yen. Check the invoice currency precision — JPY "
                    "must use 0 decimal places."
                )
                % {
                    "line": line.name or line.display_name,
                    "invoice": line.move_id.display_name,
                    "label": label,
                    "value": value,
                }
            )
        return int(rounded)

    def _line_taxes_price_included(self, line):
        """True when the line's freee-mapped tax(es) are tax-included.

        ``account_tax_rounding_method`` skips its DOWN/UP rounding for
        price-included taxes (the entered tax-inclusive price is
        authoritative), and so do we.
        """
        taxes = line.tax_ids.filtered(lambda t: t.freee_tax_code)
        return bool(taxes) and all(t.price_include for t in taxes)

    def _vat_by_line(self, invoice, lines):
        """Return ``{line.id: int vat}`` following freee's per-document
        consumption-tax rounding.

        freee rounds tax **once per tax rate for the whole document** and
        then apportions it to the detail lines: each line gets
        ``round(net * rate)`` with the configured method, and any residual
        between that per-line sum and the document-level rounded total lands
        on the line with the **largest amount** in the rate group (ties: the
        topmost line). This is freee's documented behaviour — e.g. with
        round-down, lines 426 / 2100 / 1885 / 220 @10% give 42 / 211 / 188 /
        22 (the +1 residual goes to the largest line, 2100), summing to the
        document total 463.

        The method (HALF-UP / UP / DOWN, with optional per-partner override)
        comes from ``account_tax_rounding_method`` and is applied through
        ``currency.round`` with the ``tax_rounding_method`` context. Odoo's
        own per-line vs round-globally setting is not used: the deal always
        reflects freee's per-document rule. Tax-included lines keep their
        entered inclusive tax (the rounding module skips them too).
        """
        currency = invoice.currency_id
        method = self.env["account.tax"]._get_tax_rounding_method(
            invoice.company_id, invoice.partner_id
        )
        if not method:
            # No method configured: fall back to Odoo's per-line display tax.
            return {
                line.id: int(round(line.price_total - line.price_subtotal))
                for line in lines
            }
        rounded = currency.with_context(tax_rounding_method=method)

        result = {}
        groups = {}  # tax.id -> list[line]
        taxes_by_id = {}
        for line in lines:
            if self._line_taxes_price_included(line):
                # Entered tax-inclusive price drives the tax (whole yen).
                result[line.id] = int(round(line.price_total - line.price_subtotal))
                continue
            tax = self._pick_freee_tax(line)
            taxes_by_id[tax.id] = tax
            groups.setdefault(tax.id, []).append(line)

        for tax_id, group_lines in groups.items():
            tax = taxes_by_id[tax_id]
            rate = tax.amount / 100.0 if tax.amount_type == "percent" else 0.0
            for line in group_lines:
                result[line.id] = int(rounded.round(line.price_subtotal * rate))
            # Document-level rounded total for this rate; the residual against
            # the per-line sum is absorbed by the largest-amount line.
            group_tax = int(
                rounded.round(sum(line.price_subtotal for line in group_lines) * rate)
            )
            diff = group_tax - sum(result[line.id] for line in group_lines)
            if diff:
                adj = max(group_lines, key=lambda line: line.price_subtotal)
                result[adj.id] += diff
        return result
