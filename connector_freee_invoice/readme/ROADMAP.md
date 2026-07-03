Both `connector_freee_base` and `connector_freee_invoice` are
**Alpha** — we're shipping them to gather early-adopter reactions.
Known limitations and deliberate deferrals, so adopters decide
knowingly:

- **Not yet reconciled against a real freee company.** No automated
  test exercises a real freee company — every test mocks the HTTP
  layer. A real-freee **human reconciliation of exported
  consumption-tax totals** is strongly recommended before relying on
  it for a tax filing (used at your own risk under AGPL). See
  `readme/RUNBOOK.md` §1.

- **Consumption-tax rounding follows the Odoo line totals.** The
  connector forwards `account.move.line.price_total` and the
  `price_total - price_subtotal` delta to `details[].amount` and
  `details[].vat` verbatim. Whatever rounding rule Odoo's invoice
  uses (`account_tax_rounding_method`) is what lands on freee — no
  freee-side alignment is required.

- **Withholding tax is not synced.** The freee Deals API
  does not expose withholding fields on the payload, and Odoo's
  standard withholding-tax flow is at payment time, not on the invoice
  line. The connector therefore exports the full (gross) deal —
  register withholding tax directly in freee's transaction-entry
  withholding UI on the exported deal until/unless freee adds
  first-class API support.

- **Credit notes (`out_refund`)** are exported as a freee *income*
  deal with negative amounts (a "red-slip" / negative-line entry), so
  they net against sales rather than booking as an expense. Have an
  accountant review and accept this representation before relying on
  it.

- **Encryption posture is modest by design.** The Fernet key lives in
  `odoo.conf` (read via `tools.config`), so it is kept out of the
  database and absent from any `pg_dump` — this defends DB dumps and
  casual reads, not a compromised live host that can read `odoo.conf`
  or process memory. An external-KMS / dedicated secrets-manager
  option is out of scope for now. See
  `connector_freee_base/readme/SECURITY.md`.

- **Master-data mapping is consumed but not auto-resolved.** The
  picker-wizard ids (`freee_partner_id`, `freee_item_id`, …) *are*
  used by the exporter, with documented precedence (USAGE). A future
  enhancement could auto-match Odoo records to freee masters instead
  of requiring the one-time manual pick; not yet planned.
