# -*- coding: utf-8 -*-
from odoo import api, models, _
from odoo.exceptions import UserError


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    _COMMERCIAL_LINE_FIELDS = {
        'order_id',
        'product_id',
        'product_template_id',
        'name',
        'product_uom_qty',
        'product_uom_id',
        'product_uom',
        'price_unit',
        'discount',
        'tax_ids',
        'tax_id',
    }

    def _check_parent_order_lock(self, orders, action_label, lines=None):
        ctx = self.env.context
        if ctx.get('from_convert_to_project'):
            return
        for order in orders:
            if not order:
                continue
            if order._is_post_conversion_locked():
                if order._has_authorized_vo_context(lines=lines):
                    continue
                raise UserError(_(
                    "Cannot %s order lines on Sales Order %s because it is confirmed or linked to an active "
                    "Project / Master Budget. Use an authorized Variation Order (VO) instead."
                ) % (action_label, order.display_name))
            if order._is_post_approval_locked():
                if ctx.get('from_negotiation_wizard') or ctx.get('from_revision_action'):
                    continue
                if order._has_authorized_vo_context(lines=lines):
                    continue
                raise UserError(_(
                    "Cannot %s order lines on Quotation %s after approval/dispatch (%s). "
                    "Use 'Revise Quotation (New Version)' or 'Log Client Negotiation' with a mandatory reason."
                ) % (action_label, order.display_name, order.contractor_quote_state))

    @api.model_create_multi
    def create(self, vals_list):
        order_ids = {v['order_id'] for v in vals_list if v.get('order_id')}
        if order_ids:
            orders = self.env['sale.order'].browse(list(order_ids))
            self._check_parent_order_lock(orders, _("add"), lines=None)
        return super().create(vals_list)

    def write(self, vals):
        if self._COMMERCIAL_LINE_FIELDS & set(vals.keys()):
            target_orders = self.mapped('order_id')
            if vals.get('order_id'):
                target_orders |= self.env['sale.order'].browse(vals['order_id'])
            self._check_parent_order_lock(target_orders, _("modify"), lines=self)
        return super().write(vals)

    def unlink(self):
        self._check_parent_order_lock(self.mapped('order_id'), _("delete"), lines=self)
        return super().unlink()
