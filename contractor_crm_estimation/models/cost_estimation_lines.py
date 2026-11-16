# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


# =========================================================================
# Block 1: Labour Cost Estimation Line
# =========================================================================
class ContractorCostLabourLine(models.Model):
    _name = 'contractor.cost.labour.line'
    _description = 'Contractor Labor Cost Estimation Line (Block 1)'

    order_id = fields.Many2one(
        'sale.order',
        string='Quotation Reference',
        ondelete='cascade',
        index=True,
    )
    variation_order_id = fields.Many2one(
        'contractor.variation.order',
        string='Variation Order Reference',
        ondelete='cascade',
        index=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        compute='_compute_currency_id',
    )
    worker_category = fields.Selection([
        ('permanent', 'Permanent / In-House Workforce'),
        ('temporary', 'Temporary / Subcontracted Labor'),
    ], string='Worker Category', default='permanent', required=True)

    employee_id = fields.Many2one(
        'hr.employee',
        string='Specific Employee (Optional)',
        help='Optional: select a specific employee to auto-pull their effective hourly cost rate from HR/salary history.',
    )

    trade = fields.Selection([
        ('foreman', 'Foreman / Supervisor'),
        ('carpenter', 'Carpenter'),
        ('mason', 'Mason'),
        ('electrician', 'Electrician'),
        ('plumber', 'Plumber'),
        ('welder', 'Welder / Fabricator'),
        ('steel_fixer', 'Steel Fixer'),
        ('operator', 'Heavy Equipment Operator'),
        ('driver', 'Site Driver / Logistics'),
        ('surveyor', 'Site Surveyor'),
        ('safety', 'HSE / Safety Officer'),
        ('qa_qc', 'QA/QC Inspector'),
        ('helper', 'General Labor / Helper'),
        ('engineer', 'Site Engineer'),
    ], string='Trade / Classification', required=True, default='helper')

    role_designation = fields.Char(
        string='Role / Crew Designation',
        help='Specific assignment description e.g. Night shift formwork crew, Rebar bending lead.',
    )
    calculation_mode = fields.Selection([
        ('hours_rate', 'Hours × Hourly Rate'),
        ('direct_amount', 'Direct Lump Sum / Day Rate'),
    ], string='Calculation Mode', default='hours_rate', required=True)

    estimated_hours = fields.Float(
        string='Estimated Hours',
        default=0.0,
    )
    hourly_cost_rate = fields.Monetary(
        string='Hourly Cost Rate',
        currency_field='currency_id',
        default=0.0,
    )
    direct_amount = fields.Monetary(
        string='Direct Lump Sum',
        currency_field='currency_id',
        default=0.0,
    )
    subtotal_cost = fields.Monetary(
        string='Subtotal Cost',
        currency_field='currency_id',
        compute='_compute_subtotal_cost',
        store=True,
    )

    def _compute_currency_id(self):
        for line in self:
            parent = line.order_id or line.variation_order_id
            line.currency_id = parent.currency_id if parent else self.env.company.currency_id

    @api.depends('calculation_mode', 'estimated_hours', 'hourly_cost_rate', 'direct_amount')
    def _compute_subtotal_cost(self):
        for line in self:
            if line.calculation_mode == 'hours_rate':
                line.subtotal_cost = (line.estimated_hours or 0.0) * (line.hourly_cost_rate or 0.0)
            else:
                line.subtotal_cost = line.direct_amount or 0.0

    @api.onchange('employee_id', 'trade', 'worker_category')
    def _onchange_auto_rate_from_employee_or_trade(self):
        """Auto-populates applicable hourly cost rate from employee salary/rate history or trade baseline."""
        RateHistory = self.env['contractor.dated.rate.history'].sudo()
        today = fields.Date.context_today(self)
        if self.employee_id:
            emp = self.employee_id
            emp_cat = getattr(emp, 'workforce_category', False) or getattr(emp, 'worker_category', False)
            if emp_cat in ('permanent', 'temporary'):
                self.worker_category = emp_cat
            elif emp_cat == 'manpower_supply':
                self.worker_category = 'temporary'
            if not self.role_designation and emp.job_title:
                self.role_designation = emp.job_title
            rate_val = RateHistory.get_rate_on_date(emp.id, today)
            if not rate_val:
                rate_val = (
                    getattr(emp, 'effective_hourly_rate', 0.0)
                    or getattr(emp, 'current_effective_rate', 0.0)
                    or getattr(emp, 'system_calculated_hourly_rate', 0.0)
                    or 0.0
                )
            if rate_val > 0.0:
                self.hourly_cost_rate = rate_val
            latest_hist = RateHistory.search([
                ('employee_id', '=', emp.id),
                ('effective_date', '<=', today),
            ], order='effective_date desc, id desc', limit=1)
            if latest_hist and latest_hist.classification:
                self.trade = latest_hist.classification
        elif self.trade and not self.hourly_cost_rate:
            trade_hist = RateHistory.search([
                ('classification', '=', self.trade),
                ('effective_date', '<=', today),
            ], order='effective_date desc, id desc', limit=1)
            if trade_hist and trade_hist.effective_hourly_rate > 0.0:
                self.hourly_cost_rate = trade_hist.effective_hourly_rate


# =========================================================================
# Block 2: Material Cost Estimation Line
# =========================================================================
class ContractorCostMaterialLine(models.Model):
    _name = 'contractor.cost.material.line'
    _description = 'Contractor Material Cost Estimation Line (Block 2)'

    order_id = fields.Many2one(
        'sale.order',
        string='Quotation Reference',
        ondelete='cascade',
        index=True,
    )
    variation_order_id = fields.Many2one(
        'contractor.variation.order',
        string='Variation Order Reference',
        ondelete='cascade',
        index=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        compute='_compute_currency_id',
    )
    is_new_sku = fields.Boolean(
        string='Non-Catalog / New SKU Item',
        default=False,
        help='Check if estimating a non-standard or bespoke site item not in standard inventory catalog.',
    )
    product_id = fields.Many2one(
        'product.product',
        string='Catalog Product Item',
        help='Standard inventory product item. Pulls standard cost automatically.',
    )
    item_description = fields.Text(
        string='Item Description & Specs',
        required=True,
        help='Complete material grade, dimensions, brand, technical specifications.',
    )
    quantity = fields.Float(
        string='Estimated Quantity',
        default=1.0,
        required=True,
    )
    uom_id = fields.Many2one(
        'uom.uom',
        string='Unit of Measure',
        help='Unit of measure for estimating quantity (kg, m2, m3, pcs, ton).',
    )
    historical_avg_cost = fields.Monetary(
        string='Historical / Catalog Cost',
        currency_field='currency_id',
        compute='_compute_historical_avg_cost',
        store=True,
        help='Standard cost pulled automatically from inventory item master.',
    )
    manual_unit_cost = fields.Monetary(
        string='Manual Unit Cost',
        currency_field='currency_id',
        help='Vendor quote or market rate for non-catalog item.',
    )
    cost_basis_reason = fields.Text(
        string='Cost Basis Justification',
        help='Mandatory justification explaining the vendor quotation or market research basis for non-catalog items.',
    )
    cost_basis = fields.Text(
        related='cost_basis_reason',
        readonly=False,
        string='Cost Basis',
    )
    missing_cost_basis = fields.Boolean(
        string='Missing Cost Basis',
        compute='_compute_missing_cost_basis',
    )
    effective_unit_cost = fields.Monetary(
        string='Effective Unit Cost',
        currency_field='currency_id',
        compute='_compute_effective_unit_cost',
        store=True,
    )
    requires_manager_cost_review = fields.Boolean(
        string='Requires Manager Cost Review',
        compute='_compute_requires_manager_cost_review',
        store=True,
    )
    subtotal_cost = fields.Monetary(
        string='Subtotal Cost',
        currency_field='currency_id',
        compute='_compute_subtotal_cost',
        store=True,
    )

    @api.depends('is_new_sku', 'cost_basis_reason')
    def _compute_missing_cost_basis(self):
        for line in self:
            line.missing_cost_basis = bool(line.is_new_sku and not (line.cost_basis_reason or '').strip())

    def _compute_currency_id(self):
        for line in self:
            parent = line.order_id or line.variation_order_id
            line.currency_id = parent.currency_id if parent else self.env.company.currency_id

    @api.depends('is_new_sku', 'order_id.company_id.new_sku_requires_manager_review', 'variation_order_id.company_id.new_sku_requires_manager_review')
    def _compute_requires_manager_cost_review(self):
        for line in self:
            parent = line.order_id or line.variation_order_id
            company = parent.company_id if parent else self.env.company
            line.requires_manager_cost_review = bool(line.is_new_sku and company.new_sku_requires_manager_review)

    def _get_weighted_historical_po_price(self, product, company):
        if not product or 'purchase.order.line' not in self.env:
            return 0.0
        po_lines = self.env['purchase.order.line'].sudo().search([
            ('product_id', '=', product.id),
            ('order_id.state', 'in', ['purchase', 'done']),
            ('order_id.company_id', '=', company.id),
            ('product_qty', '>', 0),
        ], order='id desc', limit=20)
        if not po_lines:
            return 0.0
        total_qty = sum(po_lines.mapped('product_qty'))
        total_sub = sum(po_lines.mapped('price_subtotal'))
        return (total_sub / total_qty) if total_qty > 0 else 0.0

    @api.depends('product_id', 'product_id.standard_price')
    def _compute_historical_avg_cost(self):
        for line in self:
            if line.product_id:
                parent = line.order_id or line.variation_order_id
                company = parent.company_id if parent else self.env.company
                po_avg = line._get_weighted_historical_po_price(line.product_id, company)
                line.historical_avg_cost = po_avg if po_avg > 0.0 else (line.product_id.standard_price or 0.0)
                if not line.item_description:
                    line.item_description = line.product_id.display_name
                if not line.uom_id:
                    line.uom_id = line.product_id.uom_id
            else:
                line.historical_avg_cost = 0.0

    @api.depends('is_new_sku', 'manual_unit_cost', 'historical_avg_cost')
    def _compute_effective_unit_cost(self):
        for line in self:
            if line.is_new_sku:
                line.effective_unit_cost = line.manual_unit_cost or 0.0
            else:
                line.effective_unit_cost = line.historical_avg_cost or 0.0

    @api.depends('quantity', 'effective_unit_cost')
    def _compute_subtotal_cost(self):
        for line in self:
            line.subtotal_cost = (line.quantity or 0.0) * (line.effective_unit_cost or 0.0)

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id:
            parent = self.order_id or self.variation_order_id
            company = parent.company_id if parent else self.env.company
            po_avg = self._get_weighted_historical_po_price(self.product_id, company)
            self.historical_avg_cost = po_avg if po_avg > 0.0 else self.product_id.standard_price
            if not self.item_description:
                self.item_description = self.product_id.display_name
            if not self.uom_id:
                self.uom_id = self.product_id.uom_id

    @api.constrains('is_new_sku', 'product_id', 'manual_unit_cost', 'cost_basis_reason')
    def _check_material_cost_basis(self):
        for line in self:
            if line.is_new_sku:
                if not line.manual_unit_cost or line.manual_unit_cost <= 0.0:
                    raise ValidationError(_(
                        "Material Block 2: Non-catalog item '%s' must have a positive manual unit cost."
                    ) % (line.item_description or 'New SKU'))
                reason = (line.cost_basis_reason or '').strip()
                if len(reason) < 5:
                    raise ValidationError(_(
                        "Material Block 2: A documented cost basis justification (min 5 characters) "
                        "is mandatory for non-catalog item '%s'."
                    ) % (line.item_description or 'New SKU'))
            else:
                if not line.product_id:
                    raise ValidationError(_(
                        "Material Block 2: Either select a catalog product item or check 'Non-Catalog / New SKU Item'."
                    ))


# =========================================================================
# Block 3: Legal & Regulatory Permits Line
# =========================================================================
class ContractorCostLegalLine(models.Model):
    _name = 'contractor.cost.legal.line'
    _description = 'Contractor Legal, Statutory & Permits Cost Line (Block 3)'

    order_id = fields.Many2one(
        'sale.order',
        string='Quotation Reference',
        ondelete='cascade',
        index=True,
    )
    variation_order_id = fields.Many2one(
        'contractor.variation.order',
        string='Variation Order Reference',
        ondelete='cascade',
        index=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        compute='_compute_currency_id',
    )
    fee_category = fields.Selection([
        ('municipality', 'Municipality / Building Permit'),
        ('civil_defense', 'Civil Defense / Fire Safety Approval'),
        ('gate_pass', 'Security & Site Gate Passes'),
        ('environmental', 'Environmental & Waste License'),
        ('road_occupancy', 'Road Occupancy / Traffic Police Permit'),
        ('utility', 'Utility Connection Fees (Water/Power)'),
        ('other', 'Other Statutory / Regulatory Fee'),
    ], string='Permit / License Type', required=True, default='municipality')

    description = fields.Char(
        string='Permit / Pass Description',
        required=True,
        help='Permit reference, agency requirements, and specific site authorization details.',
    )
    issuing_authority = fields.Char(
        string='Issuing Authority / Agency',
        help='Governmental department, municipal agency, or developer security office.',
    )
    estimated_cost = fields.Monetary(
        string='Estimated Fee / Cost',
        currency_field='currency_id',
        required=True,
        default=0.0,
    )

    def _compute_currency_id(self):
        for line in self:
            parent = line.order_id or line.variation_order_id
            line.currency_id = parent.currency_id if parent else self.env.company.currency_id


# =========================================================================
# Block 4: Miscellaneous & Site Expenses Line
# =========================================================================
class ContractorCostMiscLine(models.Model):
    _name = 'contractor.cost.misc.line'
    _description = 'Contractor Miscellaneous & Site Indirects Cost Line (Block 4)'

    order_id = fields.Many2one(
        'sale.order',
        string='Quotation Reference',
        ondelete='cascade',
        index=True,
    )
    variation_order_id = fields.Many2one(
        'contractor.variation.order',
        string='Variation Order Reference',
        ondelete='cascade',
        index=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        compute='_compute_currency_id',
    )
    misc_category = fields.Selection([
        ('transport', 'Logistics, Transport & Mobilization'),
        ('safety_ppe', 'PPE & Site Safety Consumables'),
        ('welfare', 'Worker Welfare & Site Refreshments'),
        ('site_office', 'Site Office & Temporary Power'),
        ('testing', 'Third-Party Lab Testing & Surveying'),
        ('other', 'General Site Expenses & Unforeseen Indirects'),
    ], string='Expense Category', required=True, default='transport')

    description = fields.Char(
        string='Expense Description',
        required=True,
        help='Detailed description of the indirect operational site expenditure.',
    )
    estimated_cost = fields.Monetary(
        string='Estimated Expense Cost',
        currency_field='currency_id',
        required=True,
        default=0.0,
    )

    def _compute_currency_id(self):
        for line in self:
            parent = line.order_id or line.variation_order_id
            line.currency_id = parent.currency_id if parent else self.env.company.currency_id

