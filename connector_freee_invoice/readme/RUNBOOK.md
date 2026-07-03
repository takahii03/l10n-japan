# freee Connector — Operations Runbook

Operational guide for deploying and running the freee invoice connector
(`connector_freee_base` + `connector_freee_invoice`). This covers the items a code
review flagged as deployment/process risks. Read it before the first production rollout.

> Status: both modules are **Alpha**, distributed under AGPL-3 **with no warranty —
> used at your own risk** (OCA's standard model; `development_status` + the ROADMAP
> "Known limitations" are the disclosure). The §1 reconciliation below is **strongly
> recommended**, not a gate or a sign-off — it is the adopter's call.

---

## 1. Recommended pre-flight — reconcile against a real freee company

Every automated test mocks the freee HTTP layer. That proves parsing and control flow,
**not** that real money figures land correctly in a real freee ledger. We **strongly
recommend** — though nothing requires it — that *before you rely on exported figures for
a real tax filing* you:

1. Run a **trial export against a real freee company** (ideally a freee sandbox /
   a dedicated test company, not the live books).
2. Export a representative set of invoices: at least one each of —
   - a multi-line invoice with mixed tax rates (10% + 8% reduced),
   - a rounding edge case (subtotals that force a per-tax residual),
   - a credit note (`out_refund`) — see §6,
   - an invoice with a mapped partner, item, and section.
3. **Reconcile each exported deal in the freee UI** against the Odoo invoice:
   tax-exclusive / tax-inclusive totals, per-line consumption tax, partner, account
   item, tax code, section. Totals should match to
   the yen (modulo the documented per-row vs. per-invoice 1-yen delta — see
   `readme/USAGE.md` *Why the freee deal total can differ from the Odoo invoice total*).
4. Optionally keep an internal record (who, date, freee company id, deal ids / invoices
   checked) with your deployment notes — for your own audit trail, not a sign-off
   anyone requires.

Step 3 is the one that matters: only a human reconciliation catches mappings that look
plausible on the Odoo side but post to the wrong freee account, partner or section.
Skipping it is a risk you accept, not a rule you break.

---

## 2. Sync SLA (latency)

Default cadence: the export cron `freee: Export pending invoices (PM)` runs **once daily
at 22:00 server time**. Worst-case lag between posting an invoice and it appearing in
freee is therefore **~24 h**.

This is a deliberate default, not a bug. The cron record is `noupdate="1"`, so the
operator owns the cadence:

- **Same-day for specific invoices:** use the **Sync to freee** button on the invoice
  (queues immediately, still throttled).
- **Tighter blanket SLA:** an admin may change the cron's interval (Settings → Technical
  → Scheduled Actions → _freee: Export pending invoices (PM)_) to e.g. hourly. Each run
  still respects freee's rate limit via job staggering and the capped channel (§5), so a
  shorter interval is safe but increases freee API volume.

**Decide and document the agreed SLA with the customer**, and tell the accounting team
which mechanism (cron cadence vs. manual button) meets it. The connector does not assume
a cadence on the customer's behalf.

---

## 3. queue_job channel cap — HARD deployment step (not optional)

Every freee export/update/delete job is dispatched on the queue*job channel
**`root.freee`** (constant `FREEE_QUEUE_CHANNEL`). Job ETAs are staggered, but
staggering only controls \_when a job becomes eligible* — without a channel **capacity
cap**, queue_job workers run several in parallel and the connector hits freee's ~3,600
req/h/app/company limit (HTTP 429) under load.

**You must cap the channel in the queue_job configuration.** In `odoo.conf`:

```ini
[queue_job]
channels = root:1,root.freee:1
```

(Adjust `root:` to your overall worker budget; `root.freee:1` is the part that matters
here.)

This is a **hard prerequisite**, not a tuning suggestion. If it is missed, production
_will_ hit 429s during the nightly sweep. Verify after deployment: Settings → Technical
→ Queue Job → Channels — a `root.freee` channel must exist with capacity `1`.

---

## 4. Tax-rounding behaviour (no freee-side alignment required)

The connector computes consumption tax per row using Odoo's
`account_tax_rounding_method` (HALF-UP / UP / DOWN, with optional per-partner override)
and sends `details[].vat` explicitly to freee. freee stores the value as sent — its own
company-level rounding setting only applies when `vat` is omitted, which this connector
never does. Practical consequences:

- **No alignment requirement between Odoo and freee.** The freee company admin and the
  Odoo Accounting admin do **not** need to coordinate the rounding setting — pick
  whichever rule matches the customer's accounting policy on the Odoo side, leave the
  freee side at whatever it is.
- **The tax-accounting method (tax-inclusive / tax-exclusive) is similarly
  irrelevant.** The Deals API specifies that
  `details[].amount` is *always* tax-inclusive; the connector always sends it that way
  with the consumption tax in `vat`. No setting, check or operator action is required.
- **Tax follows freee's per-document rule.** Each line is rounded with the configured
  method, the tax per rate is rounded once for the whole document, and the residual lands
  on the largest-amount line — matching freee's published behaviour. Odoo's own *Round
  Per Line* / *Round Globally* company setting is not used; with *Round Globally* the deal
  total matches Odoo's `amount_tax`, with *Round Per Line* it can differ by a yen or two.
- **Spot-check during the §1 pilot.** Reconcile a few exported deals' consumption-tax
  totals in
  the freee UI to confirm the expected behaviour for the customer's actual mix of tax
  codes and per-partner overrides.

---

## 5. Monitoring & failure ownership

Failures surface three ways; assign an owner for the first two:

1. **Aggregate alert (built-in):** when the nightly sweep finds any binding in
   `sync_state = error`, it raises **one** "freee: export failures" activity on the
   backend, assigned to every freee admin, summarising the count. It is deduplicated
   (won't pile up) and **clears automatically** once every binding recovers. A freee
   admin must watch their Activities. Owner: **\*\*\*\***\_**\*\*\*\***
2. **Per-binding:** Accounting → freee → bindings with `sync_state = Error` and the
   `Last Error` text.
3. **queue_job:** failed jobs in Settings → Technical → Queue Job.

Known blind spots to brief the owner on:

- The export cron is `noupdate="1"`. An admin can **silently disable it**; if they do,
  `to_delete` bindings (cancelled invoices) never propagate the freee DELETE and pending
  invoices never export, with no alert. Periodically confirm the scheduled action is
  active.
- A token expiry / freee outage during the nightly run shows up only via signals 1–3 —
  there is no email/Slack push. If the customer needs push alerting, wire an external
  monitor onto the "freee: export failures" activity or the queue_job failure state.

---

## 6. Accounting representation to be aware of — credit notes (`out_refund`)

A customer credit note (`out_refund`) is exported as a freee **income** deal with
**negative** line amounts (`details[].amount` / `vat`). freee registers a negative
amount as a deduction / negative line (a "red-slip" entry), so the refund nets against
sales on the P&L statement — it is *not* booked as an expense. This is an accounting
representation choice,
not a technical one.

We **recommend** an accountant reviews and accepts this representation before relying
on it. This is informational disclosure (the same content is in the ROADMAP "Known
limitations") — accepting or rejecting it is the adopter's decision, not a sign-off
this module enforces.

---

## 7. Security posture (state plainly to whoever decides to adopt)

See `connector_freee_base/readme/SECURITY.md`. Summary: stored freee secrets are
Fernet-encrypted, but the key lives in `ir.config_parameter` in the **same database**.
This defends DB dumps and casual reads, **not** a compromised live database. Losing the
key forces re-authorization of every backend. This is a documented Alpha limitation;
adopting the module means accepting it (AGPL, no warranty).

---

## 8. Currency — JPY only (hard constraint)

freee Accounting is a **JPY-only ledger** and the connector exports every amount as an
integer yen. The mapper now **refuses** any invoice whose currency is not JPY: the
export job fails with a `MappingError` ("…only exports JPY (¥) invoices…"), the binding
goes to `sync_state=error`, and the §5 aggregate alert fires. Nothing is silently posted
in the wrong currency.

Operational implications, brief the accounting/sales team:

- A foreign-currency customer invoice (export sales in USD/EUR/…) **will not reach
  freee**. It is not a transient error — it stays in `error` until the document is
  re-issued in JPY or deliberately excluded from freee export.
- If the business issues any non-JPY sales invoices, decide up front how those are
  recorded in freee (manual entry, a separate process) — the connector does not and
  cannot convert them.
- To exclude foreign-currency invoices from the nightly sweep entirely (so they never
  raise an alert), extend the domain in `_export_pending_invoices`
  (`connector_freee_invoice/models/freee_backend.py`) with
  `("currency_id.name", "=", "JPY")`.
- The §1 pilot must include at least one deliberately non-JPY invoice and confirm it is
  rejected (not exported) as expected.
