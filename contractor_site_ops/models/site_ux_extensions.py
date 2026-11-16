# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ContractorSiteConsumptionUx(models.Model):
    _inherit = 'contractor.site.consumption'

    def action_load_issued_site_materials(self):
        """Pre-populates consumption lines from unconsumed materials issued to this project site."""
        self.ensure_one()
        if not self.project_id:
            raise UserError(_("Please select a Project Site before loading issued materials."))
        if self.state != 'draft':
            raise UserError(_("Issued materials can only be bulk-loaded while the sheet is in Draft state."))

        existing_product_ids = set(self.line_ids.mapped('product_id').ids)
        candidate_product_ids = set()

        # 1. Products delivered on POs linked to this project
        if 'purchase.order.line' in self.env:
            po_lines = self.env['purchase.order.line'].search([
                ('order_id.project_id', '=', self.project_id.id),
                ('order_id.state', 'in', ['purchase', 'done']),
                ('product_id', '!=', False),
                ('qty_received', '>', 0),
            ])
            candidate_product_ids.update(po_lines.mapped('product_id').ids)

        # 2. Products transferred IN via completed Stock Transfer Notes
        if 'contractor.stock.transfer.note.line' in self.env:
            stn_lines = self.env['contractor.stock.transfer.note.line'].search([
                ('stn_id.receiving_project_id', '=', self.project_id.id),
                ('stn_id.state', '=', 'transferred'),
                ('product_id', '!=', False),
            ])
            candidate_product_ids.update(stn_lines.mapped('product_id').ids)

        # 3. Products physically stocked on project internal stock locations
        site_locs = self.env['stock.location'].search([
            ('usage', '=', 'internal'),
            ('project_id', '=', self.project_id.id),
        ])
        if site_locs:
            quants = self.env['stock.quant'].search([
                ('location_id', 'in', site_locs.ids),
                ('quantity', '>', 0),
            ])
            candidate_product_ids.update(quants.mapped('product_id').ids)

        new_line_vals = []
        ConsumptionLine = self.env['contractor.site.consumption.line']
        for prod_id in sorted(candidate_product_ids - existing_product_ids):
            temp_line = ConsumptionLine.new({
                'consumption_id': self.id,
                'product_id': prod_id,
                'consumed_today_qty': 0.0,
            })
            temp_line._compute_quantities()
            temp_line._compute_available_site_qty()
            if temp_line.available_site_qty > 0.0:
                new_line_vals.append((0, 0, {
                    'product_id': prod_id,
                    'consumed_today_qty': 0.0,
                }))

        if new_line_vals:
            self.write({'line_ids': new_line_vals})
        return True


class ContractorSiteConsumptionLineUx(models.Model):
    _inherit = 'contractor.site.consumption.line'

    available_site_qty = fields.Float(
        string='Available at Site',
        compute='_compute_available_site_qty',
        help='Unconsumed site balance available prior to this consumption entry.',
    )
    is_over_consumed = fields.Boolean(
        string='Over-Consumed',
        compute='_compute_available_site_qty',
    )

    @api.depends(
        'issued_to_site_qty',
        'previously_consumed_qty',
        'consumed_today_qty',
        'product_id',
        'consumption_id.project_id',
    )
    def _compute_available_site_qty(self):
        for line in self:
            issued = line.issued_to_site_qty or 0.0
            prev = line.previously_consumed_qty or 0.0
            avail = max(issued - prev, 0.0)
            if not issued and line.product_id and line.consumption_id.project_id:
                site_locs = self.env['stock.location'].search([
                    ('usage', '=', 'internal'),
                    ('project_id', '=', line.consumption_id.project_id.id),
                ])
                if site_locs:
                    quants = self.env['stock.quant'].search([
                        ('product_id', '=', line.product_id.id),
                        ('location_id', 'in', site_locs.ids),
                    ])
                    loc_avail = sum(quants.mapped('quantity')) - sum(quants.mapped('reserved_quantity'))
                    avail = max(loc_avail - prev, 0.0)
            line.available_site_qty = avail
            line.is_over_consumed = bool((line.consumed_today_qty or 0.0) > avail)
