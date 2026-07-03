This module exports Odoo customer invoices (`account.move` of type
`out_invoice` / `out_refund`) to freee Accounting as **deals** through
the freee REST API, using the OCA *connector* framework.

It builds on top of `connector_freee_base` (which handles
authentication, the freee backend record and the low-level HTTP
client) and adds:

- A binding model `freee.account.move` that links each Odoo invoice
  to its freee deal id.
- Per-record components (Binder, Mapper, Adapter, Synchronizer) so
  every step of the export can be overridden independently.
- A scheduled action (`ir.cron`) — *freee: Export pending invoices
  (PM)* — that runs once a day (22:00 server time by default; tune
  it under *Settings → Technical → Scheduled Actions*), sweeps
  posted customer invoices and dispatches one `queue_job` per
  un-exported invoice (initial export, POST). **The cron handles
  initial create + delete only** — it does **not** issue
  `PUT /api/1/deals/{id}` updates.
- **Updates are manual.** Once an invoice has been exported, the
  connector does **not** automatically re-push changes that happen
  on the posted Odoo invoice. Editing a posted invoice is itself a
  policy decision (electronic-bookkeeping / inalterability), so we
  do not silently rewrite the matching freee deal. To push an edit,
  use the *Sync to freee* button on the invoice form — the
  connector then issues `PUT /api/1/deals/{id}` once. The cron sweep
  never enters the update branch on its own.
- Deferred delete: `account.move.button_cancel` only flips bound
  bindings to `sync_state='to_delete'`. The same cron then
  dispatches one throttled `delete_invoice` job per marked
  binding, which calls `DELETE /api/1/deals/{id}` and clears
  `external_id` (the binding row is kept for audit). Because the
  DELETE is deferred, re-posting the invoice before the next cron
  tick reverts the mark (back to *Synced*) and the freee deal
  stays as-is — press *Sync to freee* if you also need to push the
  edits made in the draft window.
- A *Sync to freee* button on the invoice form (under the *freee*
  notebook page) for ad-hoc creates and updates — useful right
  after posting, after editing on a posted move, or to retry a
  binding stuck in *Error* without waiting for the next cron tick.
- **Scope of the connector — explicit non-goal: payment
  reconciliation.** freee is the **financial-accounting** ledger and
  the authoritative source for AR settlement; Odoo is the
  **commercial-transaction detail** side. The connector exports
  invoice issuance only —
  Odoo `account.payment`, partial payments and reconciliation are
  **not** pushed to freee, and freee-side payments are not pulled
  back to Odoo. Mark settled deals on freee; Odoo's
  `payment_state` is informational only.
- Mapping fields on `account.account` and `account.tax` for freee
  account-item ids and tax codes (set up in `connector_freee_base`,
  alongside partner and item mappings).

The mapping rules, adapted to Odoo: customer invoices become
freee `type=income` deals. **Refunds** (`out_refund`) are also
exported as `type=income` deals, but carrying **negative**
`details[].amount` / `vat` — freee registers a negative amount as a
deduction / negative line (a "red-slip" entry), so the refund nets
against sales on the P&L statement rather than landing in expenses.
(Verified against the freee OpenAPI schema `v2020_06_15`:
`dealCreateParams` `details[].amount` allows negative int64 values —
the field doc states that a negative value is registered as a
deduction / negative line. No type-flip to `expense` and no manual
sales-returns reclass on freee are needed.)

A deal is exported **unsettled by default**.
Settlement is opt-in **per journal**: map a freee walletable (account)
onto the invoice's `account.journal` (picker wizard in
`connector_freee_base`) and invoices on that journal are instead
exported **settled** — one `payments[]` entry for the full
tax-inclusive total, paid from that walletable on the invoice date.
With no walletable mapped the deal stays unsettled, as before. Refunds
(`out_refund`) on a journal with a walletable settle the same way but
with a **negative** `payments[].amount` (money flowing back out —
freee accepts a negative amount on a deal's inline `payments`); with
no walletable they stay unsettled. Note that `payments` is only sent
on the initial POST — freee's deal-update API has no `payments` field,
so a later *Sync to freee* (PUT) cannot change settlement.

Optionally, a **sales-returns account** can be mapped per journal
(*Accounting → Configuration → Journals → freee Mapping* tab → *Pick from
freee*). When set, every line of an exported credit note on that journal
is booked to that freee account item (typically a sales-returns or
sales-discount account) instead of the line's own account — keeping
returns in a dedicated contra-revenue account. Left unset, refund lines
keep their own (negative) account
mapping.

## Update and delete flows

| Trigger | What happens | freee call |
|---|---|---|
| Initial post + cron run | Binding created (no `external_id`); `record.exporter` POSTs the deal and stores the freee id. | `POST /api/1/deals` |
| Edit on a posted invoice | **No automatic push.** The binding stays *Synced* on Odoo's side and the freee deal is **not** updated unless the operator presses *Sync to freee* explicitly. | (none) |
| *Sync to freee* button | `action_freee_resync` enqueues an export for every authorized backend matching the move's company. The exporter routes to `POST` when there is no `external_id` yet, or to `PUT /api/1/deals/{id}` to push edits when one is bound. | `POST` or `PUT` |
| Cancel (`button_cancel`) | Bindings flipped to `sync_state='to_delete'`. Next cron run enqueues one throttled `delete_invoice` job per marked binding; `record.deleter` calls the API, then clears `external_id` while keeping the binding row. | (deferred) `DELETE /api/1/deals/{id}` |
| Re-post **before** the cron fires | `_post` reverts every `to_delete` binding back to `sync_state='done'` — the freee deal is **kept as-is**. Any edits made in the draft window are **not** auto-pushed; press *Sync to freee* if you want freee updated. | (none) |
| Re-post **after** the cron fires | The binding's `external_id` is already cleared, so the cron's initial-export path picks it up and creates a fresh deal. | `POST /api/1/deals` |

The exporter also runs a **search-before-create** step before any
`POST`. It first looks up freee for a deal matching the Odoo
invoice's `ref_number` + `issue_date`. If exactly one match exists
(typically an orphan from a prior crashed POST), the binding adopts
the existing freee deal id instead of creating a second one. More
than one match → the export refuses with an error so an operator
can disambiguate manually.

Why no automatic `PUT` on edit: posted Odoo invoices are finalized
vouchers (electronic-bookkeeping). Rewriting the matching freee deal
automatically every time someone tweaks a posted invoice would
silently bypass that audit boundary. The connector therefore
requires an explicit operator action (*Sync to freee*) before
freee is updated, and never sweeps "dirty" bindings on the cron.

## Extraction conditions

On each cron run, the backend selects the `account.move`
records to export using the following domain (see
`_export_pending_invoices` in
`models/freee_backend.py`):

| Condition | Meaning |
|---|---|
| `state = 'posted'` | Only confirmed invoices — drafts and cancelled moves are skipped. |
| `move_type in ('out_invoice', 'out_refund')` | Customer invoices and customer credit notes only; vendor bills are out of scope. |
| `company_id = backend.company_id` | Multi-company isolation — each freee backend exports only its own company's documents. |
| `id not in already_exported` | Skips moves that already have a `freee.account.move` binding with an `external_id`, so initial export happens exactly once. |
| `invoice_date >= backend.export_from_date` | Only invoices issued on or after the backend's *Export From* date. Stamped to today on first authorize so a fresh install does **not** retroactively flood freee with every historical invoice. Admin-editable for a back-dated catch-up. |
| `invoice_date > backend.lock_date` | Skips invoices whose issue date falls inside a closed (locked) freee period. freee rejects writes there, so blocking locally avoids an error loop. Leave the field empty to disable the check. |

The cron itself only processes freee backends in the `authorized`
state (i.e. OAuth completed). To narrow or widen the set of
exported invoices — for example, restrict to a fiscal year or
exclude internal-test partners — extend the domain in
`_export_pending_invoices`.

## Request throttling

The freee Accounting API limits each app to roughly **3,600
requests per hour per company** (~1 req/s steady-state) and
returns HTTP 429 when exceeded. To stay within that budget when a
cron run produces a large batch, every enqueued export job is
staggered with `with_delay(eta=index * _REQUEST_INTERVAL_SECONDS)`
in `_export_pending_invoices`. The first job runs
immediately, the second after `_REQUEST_INTERVAL_SECONDS`, the
third after `2 * _REQUEST_INTERVAL_SECONDS`, and so on.

The default interval is **2 seconds** (≈30 req/min — half the
freee budget, leaving headroom for retries, other API calls and
the import wizards). Tune the `_REQUEST_INTERVAL_SECONDS`
constant in `connector_freee_base/models/freee_backend.py` if you
need to go faster (single-tenant deployments) or slower (the same freee
company is shared with other integrations).

Note that staggering only controls *when each job becomes
eligible*; queue_job worker concurrency still applies. For best
results also cap the freee channel capacity in your `queue_job`
configuration (e.g. `root.freee = 1`) so the workers do not run
multiple export jobs in parallel and undo the staggering.
