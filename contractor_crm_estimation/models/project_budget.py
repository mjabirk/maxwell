# -*- coding: utf-8 -*-
from odoo import models, fields, api, _


class ContractorProjectBudget(models.Model):
    _name = 'contractor.project.budget'
    _description = 'Contractor Project Master Budget & Traceable VO Lines'
    _inherit = ['mail.thread', 'contractor.no.delete.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='Budget Title',
        required=True,
        tracking=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        required=True,
        index=True,
        ondelete='restrict',
        tracking=True,
    )
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Original Sales Contract',
        ondelete='set null',
        readonly=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='project_id.company_id',
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    contract_amount = fields.Monetary(
        string='Initial Contract Award',
        currency_field='currency_id',
        readonly=True,
    )
    baseline_cost = fields.Monetary(
        string='Initial Estimated Cost',
        currency_field='currency_id',
        readonly=True,
    )

    # Line Item Collections
    line_ids = fields.One2many(
        'contractor.project.budget.line',
        'budget_id',
        string='Baseline Budget Items',
    )
    vo_line_ids = fields.One2many(
        'contractor.project.budget.vo.line',
        'budget_id',
        string='Integrated Variation Orders (VO 1, VO 2...)',
        readonly=True,
    )

    # Consolidated Financial Tracking
    total_baseline_budget = fields.Monetary(
        string='Consolidated Baseline Budget',
        currency_field='currency_id',
        compute='_compute_budget_totals',
        store=True,
    )
    total_vo_budget = fields.Monetary(
        string='Total Approved VO Direct Cost',
        currency_field='currency_id',
        compute='_compute_budget_totals',
        store=True,
    )
    total_revised_budget = fields.Monetary(
        string='Total Revised Cost Budget',
        currency_field='currency_id',
        compute='_compute_budget_totals',
        store=True,
        help='Total allowable project expenditure (Baseline Cost + Cumulative VO Direct Cost).',
    )
    total_revised_contract_value = fields.Monetary(
        string='Total Revised Contract Value',
        currency_field='currency_id',
        compute='_compute_budget_totals',
        store=True,
        help='Total client contract value (Baseline Contract + Cumulative VO Billable Value).',
    )

    @api.depends(
        'line_ids.planned_amount',
        'vo_line_ids.planned_cost',
        'vo_line_ids.billed_amount',
        'contract_amount',
    )
    def _compute_budget_totals(self):
        for budget in self:
            base = sum(budget.line_ids.mapped('planned_amount'))
            vo_cost = sum(budget.vo_line_ids.mapped('planned_cost'))
            vo_billed = sum(budget.vo_line_ids.mapped('billed_amount'))

            budget.total_baseline_budget = base
            budget.total_vo_budget = vo_cost
            budget.total_revised_budget = base + vo_cost
            budget.total_revised_contract_value = (budget.contract_amount or 0.0) + vo_billed


class ContractorProjectBudgetLine(models.Model):
    _name = 'contractor.project.budget.line'
    _description = 'Contractor Baseline Budget Item'
    _order = 'block_type, id'

    budget_id = fields.Many2one(
        'contractor.project.budget',
        string='Master Budget',
        required=True,
        ondelete='cascade',
        index=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='budget_id.currency_id',
        readonly=True,
    )
    block_type = fields.Selection([
        ('labour', 'Labor Cost (Block 1)'),
        ('material', 'Material Cost (Block 2)'),
        ('legal', 'Legal & Permits (Block 3)'),
        ('misc', 'Miscellaneous & Indirects (Block 4)'),
    ], string='Cost Block', required=True, default='material')

    name = fields.Char(
        string='Item Description',
        required=True,
    )
    product_id = fields.Many2one(
        'product.product',
        string='Product Item',
    )
    quantity = fields.Float(
        string='Allocated Quantity',
        default=1.0,
    )
    unit_cost = fields.Monetary(
        string='Budget Unit Cost',
        currency_field='currency_id',
    )
    planned_amount = fields.Monetary(
        string='Planned Cost Amount',
        currency_field='currency_id',
        required=True,
    )
    notes = fields.Text(string='Budget Notes / Basis')


class ContractorProjectBudgetVoLine(models.Model):
    _name = 'contractor.project.budget.vo.line'
    _description = 'Contractor Traceable Variation Order Budget Line'
    _order = 'id'

    budget_id = fields.Many2one(
        'contractor.project.budget',
        string='Master Budget',
        required=True,
        ondelete='cascade',
        index=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='budget_id.currency_id',
        readonly=True,
    )
    vo_id = fields.Many2one(
        'contractor.variation.order',
        string='Source Variation Order',
        required=True,
        ondelete='restrict',
        index=True,
    )
    vo_sequence = fields.Char(
        string='VO Tag',
        required=True,
        default='VO 1',
        help='Traceable index e.g. VO 1, VO 2, maintaining permanent segregation from baseline items.',
    )
    name = fields.Char(
        string='VO Scope Summary',
        required=True,
    )
    planned_cost = fields.Monetary(
        string='VO Direct Cost Budget',
        currency_field='currency_id',
        required=True,
    )
    labour_cost = fields.Monetary(
        string='VO Labour Cost',
        currency_field='currency_id',
        default=0.0,
    )
    material_cost = fields.Monetary(
        string='VO Material Cost',
        currency_field='currency_id',
        default=0.0,
    )
    legal_cost = fields.Monetary(
        string='VO Legal Cost',
        currency_field='currency_id',
        default=0.0,
    )
    misc_cost = fields.Monetary(
        string='VO Misc Cost',
        currency_field='currency_id',
        default=0.0,
    )
    billed_amount = fields.Monetary(
        string='VO Contract Billing Value',
        currency_field='currency_id',
        required=True,
    )
    schedule_impact_days = fields.Integer(
        string='Schedule Extension (Days)',
        default=0,
    )
    integration_date = fields.Datetime(
        string='Integrated Date',
        default=fields.Datetime.now,
        readonly=True,
    )

