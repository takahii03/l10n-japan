# Invoice Connector — Security Notes

This module reuses the threat model documented in
`connector_freee_base/readme/SECURITY.md`. The notes below cover only
what the **invoice export** layer adds on top.

## Multi-company isolation

The `freee.account.move` binding is tied to exactly one
`freee.backend`, and the backend belongs to exactly one Odoo company.
A record rule (`freee_account_move_company_rule`) restricts every user
to bindings whose backend belongs to a company in their
`company_ids` — mirrors `freee_backend_company_rule` in the base
module, so the invoice and the binding cannot drift apart across the
company boundary.

## ACL on the binding

- `group_freee_user` (read-only) and `group_freee_admin` (full) are
  defined by `connector_freee_base`.
- `account.group_account_manager` gets `read / write / create` on
  `freee.account.move` — invoice-team operators can re-bind or
  re-export, but **deleting the binding is restricted to freee
  administrators** (`group_freee_admin`). The binding is an audit link
  between an Odoo invoice and a freee deal; removing it without first
  cleaning up the freee side leaves a "ghost deal" with no Odoo
  trace. Keep the unlink permission narrow.

## What this module logs and stores

The invoice exporter follows the same payload-redaction posture as the base adapter:
the queue-job *Result*, the binding's `sync_error` column and the
adapter log line contain a **non-sensitive operational summary**
(method, endpoint, freee deal id, detail count, status) — never the
deal request payload or freee's raw response body, which carry
partner / amounts / per-line text.

The backend's `debug_log_request_payload` toggle (freee-admin-only,
developer-mode-only, off by default) re-exposes the full
request/response in the job *Result* for troubleshooting. Use it
deliberately: turn it on, reproduce the issue, turn it off. It does
**not** change `sync_error` or the adapter log line.

## Failure notifications

Failed export / delete jobs raise a mail activity on the bound Odoo
invoice (see `data/mail_activity_data.xml`). The activity body
contains the same operational summary as above, so following operators
who can see the invoice but not freee credentials still get actionable
context without leaking secrets.
