# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class ContractorDatedRateHistory(models.Model):
    _name = 'contractor.dated.rate.history'
    _description = 'Contractor Dated Labor Rate History'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'effective_date desc, id desc'
    _rec_name = 'display_name'

    employee_id = fields.Many2one(
        'hr.employee',
        string='Employee',
        required=True,
        index=True,
        ondelete='restrict',
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='employee_id.company_id',
        store=True,
        index=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    effective_date = fields.Date(
        string='Effective From',
        required=True,
        index=True,
        default=fields.Date.context_today,
        tracking=True,
    )
    effective_to = fields.Date(
        string='Effective To',
        index=True,
        tracking=True,
        help='Leave empty if this rate is indefinitely active until a future superseded rate is created.',
    )
    classification = fields.Selection([
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
    ], string='Labor Classification / Trade', required=True, default='helper', index=True, tracking=True)

    system_hourly_rate = fields.Monetary(
        string='System Hourly Rate',
        currency_field='currency_id',
        required=True,
        tracking=True,
        help='Baseline derived hourly rate according to salary scale and standard monthly hours.',
    )
    override_hourly_rate = fields.Monetary(
        string='Override Hourly Rate',
        currency_field='currency_id',
        tracking=True,
        help='Approved management override rate for specialized site assignments or skill premiums.',
    )
    effective_hourly_rate = fields.Monetary(
        string='Effective Hourly Rate',
        currency_field='currency_id',
        compute='_compute_effective_hourly_rate',
        store=True,
        help='Final rate utilized for project labor costing on this date interval.',
    )
    reason = fields.Text(
        string='Revision Reason',
        required=True,
        help='Documented business justification for the rate adjustment or classification change.',
    )
    approved_by_id = fields.Many2one(
        'res.users',
        string='Approved By',
        default=lambda self: self.env.user,
        required=True,
        readonly=True,
    )

    @api.depends('system_hourly_rate', 'override_hourly_rate')
    def _compute_effective_hourly_rate(self):
        for rec in self:
            if rec.override_hourly_rate and rec.override_hourly_rate > 0.0:
                rec.effective_hourly_rate = rec.override_hourly_rate
            else:
                rec.effective_hourly_rate = rec.system_hourly_rate or 0.0

    @api.depends('employee_id', 'classification', 'effective_hourly_rate', 'effective_date')
    def _compute_display_name(self):
        for rec in self:
            emp_name = rec.employee_id.name or 'Employee'
            trade = dict(rec._fields['classification'].selection).get(rec.classification, rec.classification)
            rec.display_name = f"{emp_name} - {trade} ({rec.effective_hourly_rate} / hr from {rec.effective_date})"

    @api.constrains('effective_date', 'effective_to', 'employee_id')
    def _check_effective_dates(self):
        for rec in self:
            if rec.effective_to and rec.effective_to < rec.effective_date:
                raise ValidationError(_(
                    "Effective To date (%s) cannot be earlier than Effective From date (%s) for employee %s."
                ) % (rec.effective_to, rec.effective_date, rec.employee_id.name))

            # Check for overlapping records for the same employee
            overlap_domain = [
                ('id', '!=', rec.id),
                ('employee_id', '=', rec.employee_id.id),
            ]
            if rec.effective_to:
                overlap_domain += [
                    ('effective_date', '<=', rec.effective_to),
                    '|',
                    ('effective_to', '=', False),
                    ('effective_to', '>=', rec.effective_date),
                ]
            else:
                overlap_domain += [
                    '|',
                    ('effective_to', '=', False),
                    ('effective_to', '>=', rec.effective_date),
                ]

            conflicts = self.search(overlap_domain, limit=1)
            if conflicts:
                raise ValidationError(_(
                    "Rate history conflict for employee %s: Date range %s to %s overlaps with existing rate from %s to %s."
                ) % (
                    rec.employee_id.name,
                    rec.effective_date,
                    rec.effective_to or 'Indefinite',
                    conflicts.effective_date,
                    conflicts.effective_to or 'Indefinite',
                ))

    @api.model
    def get_rate_on_date(self, employee_id, target_date=None):
        """Returns the effective hourly labor rate for an employee on a given date.
        Guarantees that historical project costings are never retroactively recalculated.
        """
        if not employee_id:
            return 0.0

        target_date = fields.Date.to_date(target_date) if target_date else fields.Date.context_today(self)

        record = self.sudo().search([
            ('employee_id', '=', employee_id),
            ('effective_date', '<=', target_date),
            '|',
            ('effective_to', '=', False),
            ('effective_to', '>=', target_date),
        ], order='effective_date desc, id desc', limit=1)

        if record and record.effective_hourly_rate > 0.0:
            return record.effective_hourly_rate

        # Fallback to employee base rate or hourly cost
        employee = self.env['hr.employee'].sudo().browse(employee_id).exists()
        if not employee:
            return 0.0

        if getattr(employee, 'effective_hourly_rate', 0.0) > 0.0:
            return employee.effective_hourly_rate

        if getattr(employee, 'hourly_cost', 0.0) > 0.0:
            return employee.hourly_cost

        company = employee.company_id or self.env.company
        standard_monthly_hours = company.standard_monthly_hours or 208.0

        # Resolve Odoo 20 hr.version (contract)
        version = employee._get_version(target_date) if hasattr(employee, '_get_version') else (
            getattr(employee, 'version_id', False) or getattr(employee, 'current_version_id', False)
        )
        if version:
            if getattr(version, 'wage_type', 'monthly') == 'hourly' and getattr(version, 'hourly_wage', 0.0) > 0.0:
                return version.hourly_wage
            monthly_wage = getattr(version, 'total_salary', 0.0) or getattr(version, 'wage', 0.0)
            if monthly_wage > 0.0 and standard_monthly_hours > 0:
                return monthly_wage / standard_monthly_hours

        # Legacy contract_id fallback
        if hasattr(employee, 'contract_id') and employee.contract_id:
            contract = employee.contract_id
            if hasattr(contract, 'wage') and contract.wage and standard_monthly_hours > 0:
                return contract.wage / standard_monthly_hours

        return 0.0


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    rate_history_ids = fields.One2many(
        'contractor.dated.rate.history',
        'employee_id',
        string='Labor Rate Histories',
        readonly=True,
    )
    current_effective_rate = fields.Monetary(
        string='Current Effective Hourly Rate',
        compute='_compute_current_effective_rate',
        currency_field='currency_id',
        help='Effective hourly labor rate as of today according to dated rate history.',
    )

    def _compute_current_effective_rate(self):
        today = fields.Date.context_today(self)
        rate_model = self.env['contractor.dated.rate.history'].sudo()
        for emp in self:
            emp.current_effective_rate = rate_model.get_rate_on_date(emp.id, today)

    def get_employee_rate_on_date(self, target_date=None):
        """Helper on employee to fetch historical hourly rate on specific date."""
        self.ensure_one()
        return self.env['contractor.dated.rate.history'].sudo().get_rate_on_date(self.id, target_date)

