# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ContractorStockAvailabilityWizardUx(models.TransientModel):
    _inherit = 'contractor.stock.availability.wizard'

    product_id = fields.Many2one(
        'product.product',
        string='Material / SKU',
        required=False,
    )
    product_ids = fields.Many2many(
        'product.product',
        'contractor_stock_avail_wiz_product_rel',
        'wizard_id',
        'product_id',
        string='Materials to Inspect',
    )
    receiving_project_id = fields.Many2one(
        'project.project',
        string='Receiving Project Site',
    )
    requisition_id = fields.Many2one(
        'contractor.material.requisition',
        string='Source Material Requisition',
    )

    def _build_multi_product_lines(self, products, company):
        return self._build_availability_lines_for_products(products, company)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        prod_ids = []
        if ctx.get('default_product_id'):
            prod_ids.append(ctx['default_product_id'])
        if ctx.get('default_product_ids'):
            for cmd in ctx['default_product_ids']:
                if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[0] == 6:
                    prod_ids.extend(cmd[2])
                elif isinstance(cmd, int):
                    prod_ids.append(cmd)
        if ctx.get('default_requisition_id'):
            mr = self.env['contractor.material.requisition'].browse(ctx['default_requisition_id']).exists()
            if mr:
                prod_ids.extend(mr.line_ids.mapped('product_id').ids)
                if not res.get('receiving_project_id') and mr.project_id:
                    res['receiving_project_id'] = mr.project_id.id

        unique_prod_ids = list(dict.fromkeys(p for p in prod_ids if p))
        if unique_prod_ids and 'line_ids' in fields_list:
            products = self.env['product.product'].browse(unique_prod_ids).exists()
            company = self.env.company
            if res.get('receiving_project_id'):
                proj = self.env['project.project'].browse(res['receiving_project_id']).exists()
                if proj and proj.company_id:
                    company = proj.company_id
            res['line_ids'] = self._build_multi_product_lines(products, company)
            if not res.get('product_id') and products:
                res['product_id'] = products[0].id
        return res

    @api.depends('product_id', 'product_ids', 'company_id')
    def _compute_availability_lines(self):
        for wiz in self:
            products = wiz.product_ids
            if wiz.product_id:
                products = products | wiz.product_id
            if not products:
                wiz.line_ids = [(5, 0, 0)]
                continue
            company = wiz.company_id or self.env.company
            wiz.line_ids = [(5, 0, 0)] + wiz._build_multi_product_lines(products, company)

    @api.onchange('product_id', 'product_ids')
    def _onchange_products_consolidated(self):
        self._compute_availability_lines()

    def _get_receiving_location(self, project):
        if project:
            loc = self.env['stock.location'].search([
                ('usage', '=', 'internal'),
                ('project_id', '=', project.id),
            ], limit=1)
            if loc:
                return loc
        company = (project.company_id if project else False) or self.company_id or self.env.company
        return self.env['stock.location'].search([
            ('usage', '=', 'internal'),
            ('company_id', 'in', [company.id, False]),
        ], limit=1)

    def action_create_stock_transfer_note(self):
        """Creates or opens a pre-filled Stock Transfer Note from available surplus stock."""
        self.ensure_one()
        surplus_lines = self.line_ids.filtered(
            lambda l: l.net_available_qty > 0
            and (not self.receiving_project_id or l.project_id != self.receiving_project_id)
        )
        if not surplus_lines:
            raise UserError(_("No positive net-available surplus stock found across other locations to transfer."))

        # Prefer lines that have an explicit Donor Project so STN satisfies mandatory donor_project_id
        project_surplus = surplus_lines.filtered(lambda l: bool(l.project_id))
        chosen = (project_surplus or surplus_lines)[0]
        matching_donor_lines = surplus_lines.filtered(lambda l: l.location_id == chosen.location_id)

        receiving_loc = self._get_receiving_location(self.receiving_project_id)

        stn_lines = []
        seen_prods = set()
        for l in matching_donor_lines:
            if not l.product_id or l.product_id.id in seen_prods:
                continue
            seen_prods.add(l.product_id.id)
            req_qty = l.net_available_qty
            if self.requisition_id:
                mr_qty = sum(self.requisition_id.line_ids.filtered(lambda ml: ml.product_id == l.product_id).mapped('qty'))
                if mr_qty > 0:
                    req_qty = min(mr_qty, l.net_available_qty)
            stn_lines.append((0, 0, {
                'product_id': l.product_id.id,
                'qty': req_qty,
            }))

        reason_txt = _("Inter-project stock utilization from Consolidated Stock Check%s.") % (
            f" ({self.requisition_id.name})" if self.requisition_id else ""
        )

        if chosen.project_id and self.receiving_project_id and receiving_loc:
            stn = self.env['contractor.stock.transfer.note'].create({
                'donor_project_id': chosen.project_id.id,
                'donor_location_id': chosen.location_id.id,
                'receiving_project_id': self.receiving_project_id.id,
                'receiving_location_id': receiving_loc.id,
                'reason': reason_txt,
                'line_ids': stn_lines,
            })
            return {
                'name': _('Stock Transfer Note'),
                'type': 'ir.actions.act_window',
                'res_model': 'contractor.stock.transfer.note',
                'res_id': stn.id,
                'view_mode': 'form',
                'target': 'current',
            }

        return {
            'name': _('New Stock Transfer Note'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.stock.transfer.note',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_donor_project_id': chosen.project_id.id if chosen.project_id else False,
                'default_donor_location_id': chosen.location_id.id,
                'default_receiving_project_id': self.receiving_project_id.id if self.receiving_project_id else False,
                'default_receiving_location_id': receiving_loc.id if receiving_loc else False,
                'default_reason': reason_txt,
                'default_line_ids': stn_lines,
            },
        }


class ContractorStockAvailabilityLineUx(models.TransientModel):
    _inherit = 'contractor.stock.availability.line'

    def action_create_stn_from_line(self):
        """Creates or pre-fills a Stock Transfer Note directly from this availability line."""
        self.ensure_one()
        if self.net_available_qty <= 0:
            raise UserError(_("Selected location has zero net available quantity to transfer."))

        wiz = self.wizard_id
        receiving_loc = wiz._get_receiving_location(wiz.receiving_project_id)

        qty_to_transfer = self.net_available_qty
        if wiz.requisition_id and self.product_id:
            mr_qty = sum(wiz.requisition_id.line_ids.filtered(lambda ml: ml.product_id == self.product_id).mapped('qty'))
            if mr_qty > 0:
                qty_to_transfer = min(mr_qty, self.net_available_qty)

        stn_lines = [(0, 0, {
            'product_id': self.product_id.id,
            'qty': qty_to_transfer,
        })] if self.product_id else []
        reason_txt = _("Inter-project stock transfer from %s.") % self.location_id.display_name

        if self.project_id and wiz.receiving_project_id and receiving_loc and self.project_id != wiz.receiving_project_id:
            stn = self.env['contractor.stock.transfer.note'].create({
                'donor_project_id': self.project_id.id,
                'donor_location_id': self.location_id.id,
                'receiving_project_id': wiz.receiving_project_id.id,
                'receiving_location_id': receiving_loc.id,
                'reason': reason_txt,
                'line_ids': stn_lines,
            })
            return {
                'name': _('Stock Transfer Note'),
                'type': 'ir.actions.act_window',
                'res_model': 'contractor.stock.transfer.note',
                'res_id': stn.id,
                'view_mode': 'form',
                'target': 'current',
            }

        return {
            'name': _('New Stock Transfer Note'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.stock.transfer.note',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_donor_project_id': self.project_id.id if self.project_id else False,
                'default_donor_location_id': self.location_id.id,
                'default_receiving_project_id': wiz.receiving_project_id.id if wiz.receiving_project_id else False,
                'default_receiving_location_id': receiving_loc.id if receiving_loc else False,
                'default_reason': reason_txt,
                'default_line_ids': stn_lines,
            },
        }


class ContractorProjectBudgetLineUx(models.Model):
    _inherit = 'contractor.project.budget.line'

    def action_check_consolidated_stock(self):
        self.ensure_one()
        return {
            'name': _('Consolidated Multi-Project Stock Check'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.stock.availability.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_product_id': self.product_id.id if self.product_id else False,
                'default_receiving_project_id': self.budget_id.project_id.id if self.budget_id.project_id else False,
            },
        }


class ContractorMaterialRequisitionUx(models.Model):
    _inherit = 'contractor.material.requisition'

    budget_remaining_before = fields.Monetary(
        string='Remaining Material Budget (Before MR)',
        currency_field='currency_id',
        compute='_compute_live_budget_bar',
    )
    order_total = fields.Monetary(
        string='Requisition Total',
        currency_field='currency_id',
        compute='_compute_live_budget_bar',
    )
    budget_traffic_status = fields.Selection([
        ('within_budget', 'Within Budget'),
        ('exceeds_budget', 'Exceeds Budget - Override Required'),
        ('no_budget', 'No Active Project Budget'),
    ], string='Budget Health', compute='_compute_live_budget_bar')

    @api.depends('project_id', 'total_estimated_amount', 'state', 'purchase_order_ids.state')
    def _compute_live_budget_bar(self):
        for mr in self:
            mr.order_total = mr.total_estimated_amount or 0.0
            if not mr.project_id:
                mr.budget_remaining_before = 0.0
                mr.budget_traffic_status = 'no_budget'
                continue

            budget = self.env['contractor.project.budget'].search([
                ('project_id', '=', mr.project_id.id),
                ('is_active', '=', True),
            ], limit=1)
            if not budget:
                mr.budget_remaining_before = 0.0
                mr.budget_traffic_status = 'no_budget'
                continue

            budget.line_ids.sudo()._compute_line_consumption()
            mat_lines = budget.line_ids.filtered(lambda l: l.block_type == 'material')
            rem = sum((mat_lines or budget.line_ids).mapped('remaining_budget'))
            if mr.state in ('approved', 'cs_in_progress', 'po_created') and not any(
                po.state in ('purchase', 'done') for po in mr.purchase_order_ids
            ):
                rem += mr.order_total
            mr.budget_remaining_before = rem
            if mr.order_total <= rem or budget.override_approved:
                mr.budget_traffic_status = 'within_budget'
            else:
                mr.budget_traffic_status = 'exceeds_budget'

    def action_check_consolidated_stock(self):
        self.ensure_one()
        prod_ids = self.line_ids.mapped('product_id').ids
        return {
            'name': _('Consolidated Multi-Project Stock Check'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.stock.availability.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_product_id': prod_ids[0] if prod_ids else False,
                'default_product_ids': [(6, 0, prod_ids)],
                'default_receiving_project_id': self.project_id.id if self.project_id else False,
                'default_requisition_id': self.id,
            },
        }


class PurchaseOrderBudgetUx(models.Model):
    _inherit = 'purchase.order'

    budget_remaining_before = fields.Monetary(
        string='Remaining Material Budget (Before PO)',
        currency_field='currency_id',
        compute='_compute_live_budget_bar',
    )
    order_total = fields.Monetary(
        string='Order Total',
        currency_field='currency_id',
        compute='_compute_live_budget_bar',
    )
    budget_traffic_status = fields.Selection([
        ('within_budget', 'Within Budget'),
        ('exceeds_budget', 'Exceeds Budget - Override Required'),
        ('no_budget', 'No Active Project Budget'),
    ], string='Budget Health', compute='_compute_live_budget_bar')

    @api.depends('project_id', 'amount_total', 'amount_untaxed', 'state', 'order_line.price_subtotal', 'order_line.qty_invoiced')
    def _compute_live_budget_bar(self):
        for po in self:
            po.order_total = po.amount_total or 0.0
            if not po.project_id:
                po.budget_remaining_before = 0.0
                po.budget_traffic_status = 'no_budget'
                continue

            budget = self.env['contractor.project.budget'].search([
                ('project_id', '=', po.project_id.id),
                ('is_active', '=', True),
            ], limit=1)
            if not budget:
                po.budget_remaining_before = 0.0
                po.budget_traffic_status = 'no_budget'
                continue

            budget.line_ids.sudo()._compute_line_consumption()
            mat_lines = budget.line_ids.filtered(lambda l: l.block_type == 'material')
            rem = sum((mat_lines or budget.line_ids).mapped('remaining_budget'))
            if po.state in ('purchase', 'done'):
                unbilled = sum(
                    max(l.product_qty - l.qty_invoiced, 0.0) * l.price_unit
                    for l in po.order_line
                )
                rem += unbilled
            po.budget_remaining_before = rem
            if po.order_total <= rem or budget.override_approved:
                po.budget_traffic_status = 'within_budget'
            else:
                po.budget_traffic_status = 'exceeds_budget'
