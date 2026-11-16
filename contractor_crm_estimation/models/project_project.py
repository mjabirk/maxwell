# -*- coding: utf-8 -*-
from odoo import models, fields, api, _


class ProjectProject(models.Model):
    _inherit = 'project.project'

    project_code = fields.Char(
        string='Project Code',
        index=True,
        copy=False,
        help='Unique alphanumeric project reference e.g. PRJ-2026-0001.',
    )
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Original Contract Quotation',
        ondelete='set null',
        copy=False,
    )
    budget_ids = fields.One2many(
        'contractor.project.budget',
        'project_id',
        string='Project Budgets',
    )
    variation_order_ids = fields.One2many(
        'contractor.variation.order',
        'project_id',
        string='Variation Orders',
    )
    baseline_contract_value = fields.Monetary(
        string='Baseline Contract Value',
        currency_field='currency_id',
        copy=False,
        help='Initial awarded contract amount before any scope variation orders.',
    )
    total_approved_vo_value = fields.Monetary(
        string='Approved Variation Orders Value',
        currency_field='currency_id',
        compute='_compute_vo_totals',
        store=True,
    )
    revised_contract_value = fields.Monetary(
        string='Revised Total Contract Value',
        currency_field='currency_id',
        compute='_compute_vo_totals',
        store=True,
        help='Current contractual value including baseline contract and all client-accepted VOs.',
    )
    total_schedule_extension_days = fields.Integer(
        string='Cumulative Schedule Extension (Days)',
        compute='_compute_vo_totals',
        store=True,
        help='Total contractual days added to project completion deadline from accepted VOs.',
    )

    @api.depends(
        'baseline_contract_value',
        'variation_order_ids.state',
        'variation_order_ids.total_vo_value',
        'variation_order_ids.timeline_impact_days',
    )
    def _compute_vo_totals(self):
        for project in self:
            integrated_vos = project.variation_order_ids.filtered(
                lambda v: v.state in ('client_accepted', 'integrated')
            )
            vo_val = sum(integrated_vos.mapped('total_vo_value'))
            ext_days = sum(integrated_vos.mapped('timeline_impact_days'))

            project.total_approved_vo_value = vo_val
            project.revised_contract_value = (project.baseline_contract_value or 0.0) + vo_val
            project.total_schedule_extension_days = ext_days

    def action_view_budgets(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Project Budgets'),
            'res_model': 'contractor.project.budget',
            'view_mode': 'list,form',
            'domain': [('project_id', '=', self.id)],
            'context': {'default_project_id': self.id},
        }

    def action_view_variation_orders(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Variation Orders'),
            'res_model': 'contractor.variation.order',
            'view_mode': 'list,form',
            'domain': [('project_id', '=', self.id)],
            'context': {'default_project_id': self.id},
        }

