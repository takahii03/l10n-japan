### Prerequisites — set up master data in `connector_freee_base`

Before exporting invoices, finish the master-data setup documented
in `connector_freee_base` (Authorize the backend, pick the freee
company, then map at minimum every income `account.account` and
every sales `account.tax` you use to their freee counterparts).
`res.partner`, `product.template` and `account.analytic.account`
mappings are optional and only consumed when the corresponding
field is set on the invoice line.

### Consumption-tax rounding

The freee Deals API stores `details[].amount` (tax-inclusive) and
`details[].vat` as integer yen, per detail line. The connector reproduces
**freee's own per-document rounding rule** rather than copying Odoo's
display value `price_total − price_subtotal` (which always rounds half-up
and would ignore a round-down setting):

1. Each line's tax is `round(net × rate)` using the method configured by
   `account_tax_rounding_method` (**HALF-UP / UP / DOWN**, with an optional
   per-partner override).
2. The tax for each rate is also rounded **once for the whole document**
   (`round(Σ net × rate)`), and the residual between that total and the
   per-line sum is placed on the **largest-amount line** of the rate group
   (ties: the topmost line). `details[].amount = net + vat`.

This matches freee's documented behaviour
(`support.freee.co.jp/hc/ja/articles/23500770784153`). Example with
round-down, lines 426 / 2,100 / 1,885 / 220 @10%:

| net | per-line tax | sent `vat` |
|----:|-------------:|-----------:|
| 426 | 42.6 → 42 | 42 |
| 2,100 | 210 | **211** (absorbs the +1 residual — largest line) |
| 1,885 | 188.5 → 188 | 188 |
| 220 | 22 | 22 |

The document total is `floor(4,631 × 10%) = 463`, and the four `vat`
values sum to 463.

- Odoo's own *Round Per Line* / *Round Globally* company setting is **not**
  used — the deal always follows freee's per-document rule. With *Round
  Globally* on the Odoo side the two totals match; with *Round Per Line*
  they can differ by a yen or two.
- All figures are sent as integers; a non-whole yen value (e.g. a
  mis-configured currency precision) raises a clear error rather than being
  silently truncated.
- Tax-included taxes keep their entered tax-inclusive price (the rounding
  method is skipped for them, mirroring `account_tax_rounding_method`).

### Withholding tax — not supported

This connector does **not** sync withholding tax. Two reasons line up:

1. **freee Deals API**: the deal payload has no withholding field
   (neither on `details[]` rows nor at the top level — confirmed
   against the freee SDK schema). freee's own UI lets you attach
   withholding tax to a deal under its transaction-entry withholding
   tab, but that path is not exposed via the API.
2. **Odoo standard flow**: withholding tax is normally booked at
   payment-registration time (the customer pays the net amount and
   the withheld portion goes to a prepaid withholding-income-tax
   receivable account), not as an invoice line. This connector also
   does not sync `account.payment` to freee (the Odoo side owns
   commercial-transaction detail, the freee side owns
   financial-accounting settlement — see DESCRIPTION).

For professional-service invoices that need withholding tax:

- Issue the full-amount invoice in Odoo and let the connector
  export it to freee as usual (the gross amount).
- Register the withholding tax directly on freee — open the exported
  deal on freee and use freee's own withholding-tax UI to record the
  withholding amount + tax code.

If freee later exposes withholding on the Deals API, this
connector will grow a dedicated mapping; until then it is a known
limitation tracked in ROADMAP.

### Payment date (`payments[].date`) semantics

When the invoice's `account.journal` has a freee walletable (account)
mapped, the deal is exported **settled** with `payments[].date =
invoice.invoice_date`. This matches the freee Deals API payments
semantics (the day money actually moved) under an "issue = settle"
journal — typical of cash sales or an instant-debit account.

When the issue date and the cash-in date differ (e.g. bank-transfer
sales), leave the journal **without** a walletable mapping. The deal is
then exported **unsettled** and freee-side settlement is registered
separately in freee itself. Per the connector's accounting boundary
(see DESCRIPTION), Odoo's `account.payment` and reconciliation are
**not** pushed; route immediate vs. deferred-settlement invoices
through different journals to keep `payments[].date` honest.

### What gets sent to freee

Each Odoo customer invoice becomes one freee `deal`. Below is
exactly which Odoo source produces which field of the freee
`POST /api/1/deals` payload — useful when you want to know
"where do I set this?":

| freee `deal` field                  | Odoo source                                                                              | Where to set it in Odoo (menu path → form → field/tab)                                                                                            |
|-------------------------------------|------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------|
| `company_id`                        | `freee.backend.external_company_id`                                                      | Connectors → freee → Backends → form → click **Fetch Companies** → pick the freee company (one-time per backend)                                   |
| `issue_date`                        | `account.move.invoice_date`                                                              | Accounting / Invoicing → Customers → Invoices → form → *Invoice Date* field                                                                        |
| `due_date`                          | `account.move.invoice_date_due` — **optional**; omitted from the payload when unset (freee then has no due date on the deal) | Accounting / Invoicing → Customers → Invoices → form → *Due Date* field (set directly, or derived from the invoice *Payment Terms*)              |
| `type` (always `income`)            | `account.move.move_type` — both `out_invoice` and `out_refund` map to `income`; a refund (*Credit Note*) is sent as `income` with **negative** `details[].amount`/`vat` (a "red-slip" / negative-line entry), so it nets against sales instead of booking as an expense | Determined by the document type — *Invoice* vs *Credit Note* (no field to set)                                                                     |
| `ref_number`                        | `account.move.name`                                                                      | Accounting / Invoicing → Customers → Invoices → form → invoice *Number* (auto-assigned on post; *Reference* field while still in draft sequence)   |
| `partner_id` *or* `partner_code`    | First of: `res.partner.freee_partner_id` (→ `partner_id`) → `res.partner.freee_partner_code` (→ `partner_code`) — exactly one key is sent, never both. **Required**: an invoice whose partner has neither value mapped is rejected with a `MappingError` instead of being exported partner-less. | Contacts → form → **freee Mapping** group → *Pick from freee* (sets the freee id + code together) |
| `details[].account_item_id`         | `account.move.line.account_id.freee_account_item_id` if set, else `account.move.journal_id.freee_account_item_id` (per-journal fallback for accounts left intentionally unmapped) | **Chart of Accounts** → *freee Mapping* tab (per-account, primary), or Accounting → Configuration → **Journals** → *freee Mapping* tab (per-journal fallback). See *Splitting sales by journal* in `connector_freee_base` USAGE |
| `details[].tax_code`                | First `tax.freee_tax_code` among `account.move.line.tax_ids` with a value set            | Accounting → Configuration → Taxes → form → **freee Mapping** tab → *Pick from freee* (loads `/api/1/taxes/companies/{company_id}`)               |
| `details[].amount`                  | Tax-inclusive integer yen = `net + vat` (`net` = `account.move.line.price_subtotal`; `vat` as below). Signed: negative on `out_refund`. For tax-included lines, Odoo's inclusive `price_total` is used as-is. | Auto from the invoice line |
| `details[].vat`                     | Consumption tax via freee's per-document rule: `round(net × rate)` per line with `account_tax_rounding_method` (HALF-UP / UP / DOWN), plus the document-level residual on the largest line — **not** `price_total − price_subtotal` (always half-up). See *Consumption-tax rounding*. | Accounting → Settings → *Tax Rounding Method* (and optional per-partner override on the Contact) |
| `details[].description`             | `account.move.line.name`                                                                 | Invoice form → *Invoice Lines* table → *Label* column on the line                                                                                  |
| `details[].section_id` (section)    | First analytic account in `account.move.line.analytic_distribution` with `freee_section_id` set | Accounting / Invoicing → Configuration → Analytic Accounts → form → **freee Mapping** tab → *Pick from freee* (loads `/api/1/sections`); then per invoice line, see *Setting the department (section) per invoice line* below |
| `details[].item_id` (item)          | `account.move.line.product_id.product_tmpl_id.freee_item_id`                              | Products → product form → **freee Mapping** group → *Pick from freee* (loads `/api/1/items`). Optional — lines with no product, or whose product is unmapped, omit `item_id` |
| `payments[]` (settlement)           | Emitted **only** when `account.move.journal_id.freee_walletable_id` is set: one entry of `{amount=tax-inclusive total, from_walletable_id, from_walletable_type, date=invoice_date}`. For a refund (`out_refund`) the `amount` is **negative** (money back out — freee accepts it on a deal's inline `payments`). Omitted entirely (deal stays unsettled) when the journal has no walletable mapped. Only sent on the initial POST (freee's update API has no `payments`). | Accounting → Configuration → Journals → **freee Mapping** tab → settlement account → *Pick walletable from freee* (loads `/api/1/walletables`). See *Settling via a walletable* in `connector_freee_base` USAGE |
| `details[].account_item_id` (refund override) | When the invoice's `account.journal` has a **sales-returns account** mapped, every line of a credit note (`out_refund`) uses it instead of the line's own account. Optional; leave empty to keep each refund line on its own (negative) account mapping. | Accounting → Configuration → Journals → **freee Mapping** tab → *Pick from freee* (refund account) |

Anything not in the table is not sent. Odoo's `payment_state` is
**not** pushed; whether the deal is settled is driven solely by the
journal's walletable mapping above (no mapping ⇒ unsettled, the default).

**Required vs optional mappings.** The export will fail loudly with
a `MappingError` if any of these are missing:

- a **JPY** invoice currency — freee is a JPY-only ledger, so a
  non-JPY invoice is rejected (not exported); see `readme/RUNBOOK.md` §8
- `account.move.invoice_date` (no fallback — set it on the
  invoice before posting)
- `freee_account_item_id` on **either** the line's `account.account`
  (the primary, per-account mapping) **or** the invoice's
  `account.journal` (the per-journal fallback for accounts left
  intentionally unmapped — the per-journal/walletable split) — only if
  neither is set does the export fail
- `freee_tax_code` on at least one of the line's `account.tax`
  records

These are silently omitted from the payload when missing — the
deal still exports cleanly:

- `partner_id` / `partner_code` — omitted entirely if the partner
  has neither `freee_partner_id` nor `freee_partner_code` mapped
- `details[].description` — omitted if the line has no label
- `details[].section_id` — omitted if the line has no analytic
  distribution, or the picked analytic account has no
  `freee_section_id` set
- `details[].item_id` — omitted if the line has no product, or the
  product's `freee_item_id` is unmapped

### Invoice-specific steps

1. Post a customer invoice. The invoice waits for the next run of
   the *freee: Export pending invoices (PM)* scheduled action
   (22:00 server time by default — adjust under *Settings →
   Technical → Scheduled Actions*); on that run a
   `freee.account.move` binding is created, a queue job is delayed,
   and the invoice is POSTed to freee as a `deal`. Deals are
   exported **unsettled** unless the invoice's journal has a freee
   walletable (account) mapped, in which case they are exported
   **settled** through that walletable (see the `payments[]` row above
   and *Settling via a walletable* in `connector_freee_base` USAGE).
   Odoo's own `payment_state` is not synced.

   Only invoices that match the extraction conditions are picked
   up: `state = posted`, `move_type` is `out_invoice` or
   `out_refund`, the invoice belongs to the freee backend's
   company, and the invoice has not already been exported (no
   `freee.account.move` binding with an `external_id`). Drafts,
   cancelled invoices, vendor bills, other-company invoices and
   already-exported invoices are skipped. See *Extraction
   conditions* in the module description for details.

2. Editing an exported invoice: edits to a posted invoice do **not**
   automatically re-push the matching freee deal. The connector
   treats posted invoices as finalized vouchers and refuses to
   silently rewrite the freee side. To push the change, open the
   invoice and click *Sync to freee* on the *freee* notebook page —
   the connector then issues a single `PUT /api/1/deals/{id}`.

3. Pushing changes immediately: open the invoice → *freee*
   notebook page → click **Sync to freee**. Every authorized
   freee backend matching the invoice's company gets an export
   job enqueued straight away. Use this after posting if you need
   the deal in freee before the next cron tick, or to retry a
   binding stuck in *Error*.

4. Cancelling an invoice: pressing *Cancel* on a posted invoice
   does **not** delete the freee deal immediately. Instead, every
   exported binding is flipped to *To Delete* and the next cron
   run dispatches the throttled `DELETE /api/1/deals/{id}` call. This grace window lets you undo a mistaken cancel by
   re-posting the invoice before the cron fires — the binding is
   reverted to *Pending* and an update is pushed instead. After
   the cron has actually deleted the deal, re-posting the
   invoice re-creates it from scratch.

### Setting the department (section) per invoice line

freee's `details[].section_id` is driven by Odoo's analytic
distribution on the invoice line. Standard Odoo does **not**
automatically attach a department to the user, so it has to be
set on the line — either manually by the person who creates the
invoice, or by an `account.analytic.distribution.model` rule.

**Manual entry workflow (per invoice line):**

1. **One-time admin setup** — in `connector_freee_base`, open
   the Odoo analytic account that represents your freee
   department (Accounting / Invoicing → Configuration →
   Analytic Accounts). On the *freee Mapping* tab, click
   *Pick from freee*, fetch `/api/1/sections`, and pick the
   matching freee section. The chosen `freee_section_id` is now
   stored on the analytic account.
2. **Enable the column on the invoice line list.** Open a
   customer invoice. Above the *Invoice Lines* table, click the
   *⋮* options icon (top-right of the line list, the column
   chooser) and turn on **Analytic Distribution**. Without this
   the column is hidden by default.
3. **Pick the department on each line.** In the *Analytic
   Distribution* cell, type the name of the analytic account
   you mapped in step 1, or click the field and pick from the
   dropdown. Set the percentage (typically `100%` to a single
   account; you can also split across multiple analytic
   accounts and the mapper picks the first one with a
   `freee_section_id`).
4. **Save / post the invoice as usual.** On the next cron
   run (or via *Sync to freee*), the deal payload's
   `details[].section_id` is filled with the analytic account's
   `freee_section_id`. Lines without an analytic account, or
   whose analytic account has no `freee_section_id` mapped,
   simply omit `section_id` from the payload — that is fine,
   freee accepts deals with no section.

**Defaulting it instead of typing every time:**

If most of your invoices for a given partner / product / income
account always belong to the same department, set up an
`account.analytic.distribution.model` rule (Accounting /
Invoicing → Configuration → Analytic Distribution Models) keyed
on partner, product, account prefix, etc. Matching invoice lines
will pre-fill the analytic distribution automatically — no need
to repeat step 3 each time. Note that *salesperson / user* is
**not** a built-in matching criterion in Odoo; if you need
"every invoice raised by sales rep A → department X" you'll
need a small custom override on `account.move.line` to read
`self.env.user` and resolve to an analytic account.

Sync state is shown on each invoice form (`freee` notebook page) and
in the dedicated *Connectors → freee → Invoice Bindings* list.

### Inspecting what was sent to freee (debug)

Each export/delete runs as a queue job; its **Result**
(*Settings → Technical → Queue Job*) shows a non-sensitive summary
(method, endpoint, freee deal id, detail count, status) — the request
payload and freee's response are deliberately kept out of that
unencrypted column.

To see the **exact data sent** for troubleshooting: enable developer
mode, open the freee Backend, and tick **Debug: log request payload**
(visible only in developer mode, freee-admin only). Subsequent jobs'
*Result* then also include the full request payload and freee's
response. This re-exposes partner / amounts / line text in the job
result, so treat it as a temporary action — reproduce the issue, then
untick it. Leave it off in production.
