# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "Connector freee Invoice",
    "version": "18.0.1.0.0",
    "category": "Connector",
    "summary": "Export Odoo customer invoices to freee Accounting as deals.",
    "author": "Takahiro SUNAGA <t.sunaga@takahii.co>, Odoo Community Association (OCA)",
    "maintainers": ["takahii03"],
    "website": "https://github.com/OCA/l10n-japan",
    "license": "AGPL-3",
    "depends": [
        "connector_freee_base",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/freee_invoice_security.xml",
        "data/ir_cron.xml",
        "data/mail_activity_data.xml",
        "views/account_move_views.xml",
        "views/freee_account_move_views.xml",
        "views/account_journal_views.xml",
        "wizards/freee_account_item_fetch_wizard_views.xml",
    ],
    "development_status": "Alpha",
    "installable": True,
}
