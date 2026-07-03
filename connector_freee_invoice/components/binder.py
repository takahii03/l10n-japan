# Copyright 2026 Takahiro SUNAGA <t.sunaga@takahii.co>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo.addons.component.core import Component


class FreeeAccountMoveBinder(Component):
    """Default binder for the freee.account.move binding."""

    _name = "freee.account.move.binder"
    _inherit = "freee.binder"
    _apply_on = "freee.account.move"
