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



class ProjectProject360Ux(models.Model):
    _inherit = 'project.project'

    total_approved_budget = fields.Monetary(
        string='Total Approved Budget',
        currency_field='currency_id',
        compute='_compute_project_360_kpis',
    )
    committed_plus_actual_cost = fields.Monetary(
        string='Committed + Actual Cost',
        currency_field='currency_id',
        compute='_compute_project_360_kpis',
    )
    remaining_project_budget = fields.Monetary(
        string='Remaining Project Budget',
        currency_field='currency_id',
        compute='_compute_project_360_kpis',
    )
    earned_poc_revenue = fields.Monetary(
        string='Earned PoC Revenue',
        currency_field='currency_id',
        compute='_compute_project_360_kpis',
    )
    total_billed_to_date = fields.Monetary(
        string='Total Billed to Date',
        currency_field='currency_id',
        compute='_compute_project_360_kpis',
    )
    retention_held_amount = fields.Monetary(
        string='Retention Held',
        currency_field='currency_id',
        compute='_compute_project_360_kpis',
    )
    open_site_issues_count = fields.Integer(
        string='Open Site Issues',
        compute='_compute_project_360_kpis',
    )

    def _compute_project_360_kpis(self):
        Budget = self.env.get('contractor.project.budget')
        BudgetLine = self.env.get('contractor.project.budget.line')
        Wip = self.env.get('contractor.project.wip.schedule')
        AccountMove = self.env.get('account.move')
        Retention = self.env.get('contractor.retention.register')
        SiteIssue = self.env.get('contractor.site.issue')

        for proj in self:
            approved_budget = 0.0
            committed_actual = 0.0
            remaining_budget = 0.0

            if Budget is not None:
                b_dom = [('project_id', '=', proj.id)]
                if 'is_active' in Budget._fields:
                    b_dom.append(('is_active', '=', True))
                budget = Budget.sudo().search(b_dom, limit=1, order='id desc')
                if not budget and 'is_active' in Budget._fields:
                    budget = Budget.sudo().search([('project_id', '=', proj.id)], limit=1, order='id desc')
                if budget:
                    if hasattr(budget.line_ids, '_compute_line_consumption'):
                        budget.line_ids.sudo()._compute_line_consumption()
                    if BudgetLine is not None and 'total_approved_budget' in BudgetLine._fields:
                        approved_budget = sum(budget.line_ids.mapped('total_approved_budget'))
                        committed = sum(budget.line_ids.mapped('committed_amount'))
                        actual = sum(budget.line_ids.mapped('actual_cost_amount'))
                        committed_actual = committed + actual
                        remaining_budget = sum(budget.line_ids.mapped('remaining_budget'))
                    else:
                        approved_budget = sum(budget.line_ids.mapped('planned_amount'))
                        remaining_budget = approved_budget

            earned_rev = 0.0
            if Wip is not None:
                latest_wip = Wip.sudo().search([
                    ('project_id', '=', proj.id),
                ], order='period_date desc, id desc', limit=1)
                if latest_wip:
                    earned_rev = latest_wip.earned_revenue_to_date or 0.0

            billed = 0.0
            if AccountMove is not None and 'project_id' in AccountMove._fields:
                invs = AccountMove.sudo().search([
                    ('project_id', '=', proj.id),
                    ('move_type', '=', 'out_invoice'),
                    ('state', '=', 'posted'),
                ])
                billed = sum(invs.mapped('amount_untaxed'))

            retention_held = 0.0
            if Retention is not None:
                rets = Retention.sudo().search([
                    ('project_id', '=', proj.id),
                    ('state', '!=', 'released'),
                ])
                retention_held = sum(rets.mapped('retention_amount'))

            issues_cnt = 0
            if SiteIssue is not None:
                issues_cnt = SiteIssue.sudo().search_count([
                    ('project_id', '=', proj.id),
                    ('state', 'in', ['open', 'escalated']),
                ])

            proj.total_approved_budget = approved_budget
            proj.committed_plus_actual_cost = committed_actual
            proj.remaining_project_budget = remaining_budget
            proj.earned_poc_revenue = earned_rev
            proj.total_billed_to_date = billed
            proj.retention_held_amount = retention_held
            proj.open_site_issues_count = issues_cnt

    def action_view_site_issues(self):
        self.ensure_one()
        if 'contractor.site.issue' not in self.env:
            return {'type': 'ir.actions.act_window_close'}
        return {
            'name': _('Open Site Issues & Obstructions'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.site.issue',
            'view_mode': 'list,form',
            'domain': [('project_id', '=', self.id)],
            'context': {'default_project_id': self.id},
        }

    def action_view_project_retentions(self):
        self.ensure_one()
        if 'contractor.retention.register' not in self.env:
            return {'type': 'ir.actions.act_window_close'}
        return {
            'name': _('Project Retention Ledger'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.retention.register',
            'view_mode': 'list,form',
            'domain': [('project_id', '=', self.id)],
            'context': {'default_project_id': self.id},
        }
