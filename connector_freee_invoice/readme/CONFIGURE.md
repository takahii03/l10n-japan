Authentication, the freee company binding and the master-data mappings
are configured in `connector_freee_base` (see that module's
*Configuration*). This module adds the invoice **export sweep**, which
has two deployment steps an operator must complete. The full
operations guide is `readme/RUNBOOK.md`; the essentials:

1. **Cap the freee queue_job channel — HARD prerequisite, not
   optional.** Every export / update / delete job runs on the
   `root.freee` channel. ETA staggering only controls *when* a job
   becomes eligible; without a capacity cap the workers run several in
   parallel and the connector hits freee's ~3,600 req/h/company limit
   (HTTP 429) during the nightly sweep. In `odoo.conf`:

   ```ini
   [queue_job]
   channels = root:1,root.freee:1
   ```

   Verify after deployment under *Settings → Technical → Queue Job →
   Channels*: a `root.freee` channel with capacity `1` must exist.
   See `readme/RUNBOOK.md` §3.

2. **Agree and document the sync SLA.** The export cron *freee: Export
   pending invoices (PM)* runs once daily at 22:00 server time by
   default, so worst-case lag is ~24 h. It is `noupdate="1"` — the
   operator owns the cadence. For same-day export of a specific
   invoice use the **Sync to freee** button on the invoice; for a
   tighter blanket SLA an admin may shorten the cron interval under
   *Settings → Technical → Scheduled Actions* (still rate-limited).
   Decide the SLA with the customer and record it. See
   `readme/RUNBOOK.md` §2.

Before relying on exported figures for a real tax filing, we strongly
recommend the §1 reconciliation in `readme/RUNBOOK.md` (human check of
exported consumption-tax totals against a real freee company). It is a
recommendation, not a gate — Alpha, AGPL, used at your own risk.
