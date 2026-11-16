# -*- coding: utf-8 -*-
from datetime import timedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


# =========================================================================
# 1. Track 1 — Permanent Monthly Payroll Run
# =========================================================================
class ContractorPayrollRun(models.Model):
    _name = 'contractor.payroll.run'
    _description = 'Permanent Staff Monthly Payroll Run'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'date_end desc, id desc'

    name = fields.Char(
        string='Payroll Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Payroll'),
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        required=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    date_start = fields.Date(
        string='Period Start Date',
        required=True,
        tracking=True,
    )
    date_end = fields.Date(
        string='Period End Date',
        required=True,
        tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Draft Payroll'),
        ('hr_prepared', 'Prepared by HR Officer'),
        ('accountant_approved', 'Approved by Senior Accountant'),
        ('released', 'Released & Disbursed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    prepared_by = fields.Many2one(
        'res.users',
        string='Prepared By',
        readonly=True,
        copy=False,
    )
    preparation_date = fields.Datetime(
        string='Preparation Date',
        readonly=True,
        copy=False,
    )
    accountant_id = fields.Many2one(
        'res.users',
        string='Audited By Senior Accountant',
        readonly=True,
        copy=False,
    )
    accountant_date = fields.Datetime(
        string='Accountant Approval Date',
        readonly=True,
        copy=False,
    )
    manager_id = fields.Many2one(
        'res.users',
        string='Payment Released By Manager',
        readonly=True,
        copy=False,
    )
    release_date = fields.Datetime(
        string='Release Date',
        readonly=True,
        copy=False,
    )
    account_move_id = fields.Many2one(
        'account.move',
        string='Payroll Accounting Journal Entry',
        readonly=True,
        copy=False,
    )

    line_ids = fields.One2many(
        'contractor.payroll.run.line',
        'payroll_id',
        string='Permanent Employee Salary Lines',
        copy=True,
    )
    total_basic = fields.Monetary(
        string='Total Basic Salaries',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    total_gross = fields.Monetary(
        string='Total Monthly Gross',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    total_unpaid_deductions = fields.Monetary(
        string='Total Unpaid Leave Deductions',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    total_ot_pay = fields.Monetary(
        string='Total Approved Overtime Pay',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    total_tool_deductions = fields.Monetary(
        string='Total Tool Deductions',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    total_net_payable = fields.Monetary(
        string='Grand Total Net Payroll Payable',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )

    @api.depends(
        'line_ids.basic_salary',
        'line_ids.monthly_gross',
        'line_ids.unpaid_leave_deduction',
        'line_ids.ot_earnings',
        'line_ids.tool_deductions',
        'line_ids.net_payable',
    )
    def _compute_totals(self):
        for run in self:
            run.total_basic = sum(run.line_ids.mapped('basic_salary'))
            run.total_gross = sum(run.line_ids.mapped('monthly_gross'))
            run.total_unpaid_deductions = sum(run.line_ids.mapped('unpaid_leave_deduction'))
            run.total_ot_pay = sum(run.line_ids.mapped('ot_earnings'))
            run.total_tool_deductions = sum(run.line_ids.mapped('tool_deductions'))
            run.total_net_payable = sum(run.line_ids.mapped('net_payable'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Payroll')) == _('New Payroll'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.payroll.run') or _('New Payroll')
        return super().create(vals_list)

    def action_calculate_payroll(self):
        """Pulls all active permanent employees, computes unpaid leave deductions and site-approved OT."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Payroll calculations can only be recomputed in Draft state."))
        if self.date_start > self.date_end:
            raise UserError(_("Period Start Date cannot be after Period End Date."))

        permanent_staff = self.env['hr.employee'].search([
            ('workforce_category', '=', 'permanent'),
            ('company_id', '=', self.company_id.id),
            ('active', '=', True),
        ])

        days_in_period = (self.date_end - self.date_start).days + 1
        lines = []

        for emp in permanent_staff:
            gross = emp.monthly_total_salary or 0.0
            basic = emp.monthly_basic_salary or 0.0

            # 1. Unpaid Leave calculation from hr.leave
            unpaid_days = 0.0
            leaves = self.env['hr.leave'].search([
                ('employee_id', '=', emp.id),
                ('state', '=', 'validate'),
                ('date_from', '<=', self.date_end),
                ('date_to', '>=', self.date_start),
            ])
            for lv in leaves:
                if lv.holiday_status_id.unpaid or 'unpaid' in lv.holiday_status_id.name.lower():
                    unpaid_days += lv.number_of_days

            unpaid_deduction = (gross / days_in_period * unpaid_days) if days_in_period > 0 else 0.0

            # 2. Approved Overtime from site attendance lines
            atn_lines = self.env['contractor.site.attendance.line'].search([
                ('employee_id', '=', emp.id),
                ('attendance_date', '>=', self.date_start),
                ('attendance_date', '<=', self.date_end),
                ('attendance_status', 'in', ['present', 'unscheduled_addition']),
                ('ot_approved', '=', True),
            ])
            ot_hours = sum(atn_lines.mapped('overtime_hours'))
            ot_pay = sum(atn_lines.mapped('ot_cost'))

            # 3. Tool deductions
            tool_ded = emp.pending_tool_deduction_amount or 0.0

            # 4. Employee advance / loan installment deductions
            other_ded = 0.0
            if 'contractor.employee.advance' in self.env:
                open_advances = self.env['contractor.employee.advance'].search([
                    ('employee_id', '=', emp.id),
                    ('state', '=', 'disbursed'),
                    ('outstanding_balance', '>', 0.0),
                ])
                for adv in open_advances:
                    inst = adv.monthly_installment or adv.outstanding_balance
                    other_ded += min(inst, adv.outstanding_balance)

            net = gross - unpaid_deduction + ot_pay - tool_ded - other_ded

            lines.append((0, 0, {
                'employee_id': emp.id,
                'basic_salary': basic,
                'monthly_gross': gross,
                'days_in_month': days_in_period,
                'unpaid_leave_days': unpaid_days,
                'unpaid_leave_deduction': unpaid_deduction,
                'approved_ot_hours': ot_hours,
                'ot_earnings': ot_pay,
                'tool_deductions': tool_ded,
                'other_deductions': other_ded,
                'net_payable': max(net, 0.0),
            }))

        self.line_ids.unlink()
        self.write({
            'line_ids': lines,
        })
        self._compute_totals()

    def action_hr_prepare(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_hr_officer')):
            raise AccessError(_("Access Denied: Only HR Officers or Managers can submit payroll runs."))
        if not self.line_ids:
            raise UserError(_("Please calculate payroll lines before submitting."))
        self.write({
            'state': 'hr_prepared',
            'prepared_by': self.env.user.id,
            'preparation_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Payroll Prepared by HR:</strong> Submitted for Senior Accountant verification.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_accountant_approve(self):
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_senior_accountant') or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise UserError(_("Access Denied: Only a Senior Accountant or Accountant can approve payroll calculations."))
        self.write({
            'state': 'accountant_approved',
            'accountant_id': self.env.user.id,
            'accountant_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Payroll Audited & Approved:</strong> Certified by Senior Accountant %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_release_payroll(self):
        """Authorizes payment release, posts accounting entry, and clears employee tool deduction balances."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize payroll disbursement."))

        if self.state != 'accountant_approved':
            raise UserError(_("Payroll can only be released after Senior Accountant approval."))
        if self.account_move_id:
            raise UserError(_("A payroll journal entry (%s) has already been posted.") % self.account_move_id.name)
        if 'contractor.period.close' in self.env:
            self.env['contractor.period.close']._check_period_not_locked(self.date_end, self.company_id.id)

        # Create accounting entry
        misc_journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not misc_journal:
            raise UserError(_("No General Journal found for company '%s'.") % self.company_id.name)

        salary_expense_acc = self.company_id.default_expense_account_id or self.env['account.account'].search([
            ('internal_group', '=', 'expense'),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1)
        payable_acc = self.company_id.default_payroll_payable_account_id or self.env['account.account'].search([
            ('account_type', 'in', ['liability_current', 'payable']),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1) or salary_expense_acc

        move_lines = [
            (0, 0, {
                'name': f"Payroll Run {self.name} - Salaries & Overtime",
                'account_id': salary_expense_acc.id,
                'debit': self.total_net_payable,
                'credit': 0.0,
            }),
            (0, 0, {
                'name': f"Payroll Run {self.name} - Net Payroll Payable",
                'account_id': payable_acc.id,
                'debit': 0.0,
                'credit': self.total_net_payable,
            }),
        ]

        move = self.env['account.move'].create({
            'journal_id': misc_journal.id,
            'date': self.date_end,
            'ref': f"Payroll {self.name}",
            'move_type': 'entry',
            'line_ids': move_lines,
        })
        move.action_post()

        # Clear recovered tool deduction & employee advance installments on employees
        for line in self.line_ids:
            if line.tool_deductions > 0:
                line.employee_id.pending_tool_deduction_amount = max(line.employee_id.pending_tool_deduction_amount - line.tool_deductions, 0.0)
            if line.other_deductions > 0 and 'contractor.employee.advance' in self.env:
                remaining_to_apply = line.other_deductions
                advances = self.env['contractor.employee.advance'].sudo().search([
                    ('employee_id', '=', line.employee_id.id),
                    ('state', '=', 'disbursed'),
                    ('outstanding_balance', '>', 0.0),
                ], order='request_date asc, id asc')
                for adv in advances:
                    if remaining_to_apply <= 0.0:
                        break
                    applied = min(remaining_to_apply, adv.outstanding_balance)
                    new_recovered = (adv.recovered_amount or 0.0) + applied
                    adv_vals = {'recovered_amount': new_recovered}
                    if adv.advance_amount - new_recovered <= 0.005:
                        adv_vals['state'] = 'recovered'
                    adv.write(adv_vals)
                    remaining_to_apply -= applied

        self.write({
            'state': 'released',
            'manager_id': self.env.user.id,
            'release_date': fields.Datetime.now(),
            'account_move_id': move.id,
        })

        self.message_post(
            body=Markup(_("<strong>Payroll Released & Disbursed:</strong> Approved by %s. Journal Entry <a href='#' data-oe-model='account.move' data-oe-id='%s'>%s</a> created.")) % (
                self.env.user.name, move.id, move.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    @api.constrains('company_id', 'date_start', 'date_end', 'state')
    def _check_payroll_period_overlap(self):
        for rec in self:
            if rec.state == 'cancelled' or not rec.date_start or not rec.date_end:
                continue
            if rec.date_start > rec.date_end:
                raise ValidationError(_("Payroll Period Start Date cannot be after Period End Date."))
            overlap = self.sudo().search([
                ('id', '!=', rec.id),
                ('company_id', '=', rec.company_id.id),
                ('state', '!=', 'cancelled'),
                ('date_start', '<=', rec.date_end),
                ('date_end', '>=', rec.date_start),
            ], limit=1)
            if overlap:
                raise ValidationError(_(
                    "Payroll period (%s to %s) overlaps with existing payroll run '%s' (%s to %s) for company '%s'."
                ) % (rec.date_start, rec.date_end, overlap.name, overlap.date_start, overlap.date_end, rec.company_id.name))

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        if any(rec.state == 'released' for rec in self) and not set(vals.keys()).issubset(allowed_meta_fields):
            raise UserError(_("Released Payroll Runs are locked and cannot be modified."))
        return super().write(vals)


class ContractorPayrollRunLine(models.Model):
    _name = 'contractor.payroll.run.line'
    _description = 'Permanent Staff Monthly Payroll Line'
    _order = 'employee_id, id'

    payroll_id = fields.Many2one(
        'contractor.payroll.run',
        string='Payroll Reference',
        required=True,
        ondelete='cascade',
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Permanent Employee',
        required=True,
    )
    basic_salary = fields.Monetary(
        string='Basic Salary',
        currency_field='currency_id',
    )
    monthly_gross = fields.Monetary(
        string='Monthly Gross',
        currency_field='currency_id',
    )
    days_in_month = fields.Integer(
        string='Period Days',
        default=30,
    )
    unpaid_leave_days = fields.Float(
        string='Unpaid Days',
        default=0.0,
    )
    unpaid_leave_deduction = fields.Monetary(
        string='Unpaid Deduction',
        currency_field='currency_id',
    )
    approved_ot_hours = fields.Float(
        string='Approved OT Hours',
        default=0.0,
    )
    ot_earnings = fields.Monetary(
        string='Overtime Pay',
        currency_field='currency_id',
    )
    tool_deductions = fields.Monetary(
        string='Tool Deductions',
        currency_field='currency_id',
    )
    other_deductions = fields.Monetary(
        string='Other Deductions',
        currency_field='currency_id',
        default=0.0,
    )
    net_payable = fields.Monetary(
        string='Net Payable',
        currency_field='currency_id',
        required=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='payroll_id.currency_id',
        readonly=True,
    )

    def write(self, vals):
        if any(line.payroll_id.state == 'released' for line in self):
            raise UserError(_("Lines of a released Payroll Run cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if any(line.payroll_id.state == 'released' for line in self):
            raise UserError(_("Lines of a released Payroll Run cannot be deleted."))
        return super().unlink()


# =========================================================================
# 2. Track 2 — Temporary Worker Daily Wage Settlement
# =========================================================================
class ContractorTempWageSettlement(models.Model):
    _name = 'contractor.temp.wage.settlement'
    _description = 'Temporary Worker Daily Wage Settlement'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'date_end desc, id desc'

    name = fields.Char(
        string='Settlement Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New TWS'),
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        required=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project Site',
        required=True,
        tracking=True,
    )
    date_start = fields.Date(
        string='Period Start Date',
        required=True,
        tracking=True,
    )
    date_end = fields.Date(
        string='Period End Date',
        required=True,
        tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Draft Settlement'),
        ('hr_prepared', 'Prepared by HR / Site Engineer'),
        ('accountant_approved', 'Approved by Senior Accountant'),
        ('released', 'Paid & Settled'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    prepared_by = fields.Many2one(
        'res.users',
        string='Prepared By',
        readonly=True,
        copy=False,
    )
    accountant_id = fields.Many2one(
        'res.users',
        string='Accountant Approver',
        readonly=True,
        copy=False,
    )
    manager_id = fields.Many2one(
        'res.users',
        string='Manager Approver',
        readonly=True,
        copy=False,
    )
    account_move_id = fields.Many2one(
        'account.move',
        string='Settlement Journal Entry',
        readonly=True,
        copy=False,
    )

    line_ids = fields.One2many(
        'contractor.temp.wage.settlement.line',
        'settlement_id',
        string='Temporary Worker Wage Lines',
        copy=True,
    )
    total_workers = fields.Integer(
        string='Operatives Settled',
        compute='_compute_totals',
        store=True,
    )
    total_days_worked = fields.Float(
        string='Total Shifts Worked',
        compute='_compute_totals',
        store=True,
    )
    total_regular_hours = fields.Float(
        string='Total Regular Hours',
        compute='_compute_totals',
        store=True,
    )
    total_ot_hours = fields.Float(
        string='Total OT Hours',
        compute='_compute_totals',
        store=True,
    )
    total_wage_payable = fields.Monetary(
        string='Total Daily Wages Payable',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )

    @api.depends('line_ids.total_payable', 'line_ids.regular_hours', 'line_ids.ot_hours', 'line_ids.days_worked')
    def _compute_totals(self):
        for rec in self:
            rec.total_workers = len(rec.line_ids)
            rec.total_days_worked = sum(rec.line_ids.mapped('days_worked'))
            rec.total_regular_hours = sum(rec.line_ids.mapped('regular_hours'))
            rec.total_ot_hours = sum(rec.line_ids.mapped('ot_hours'))
            rec.total_wage_payable = sum(rec.line_ids.mapped('total_payable'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New TWS')) == _('New TWS'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.temp.wage.settlement') or _('New TWS')
        return super().create(vals_list)

    def action_compute_settlement(self):
        """Pulls approved attendance lines for temporary workers on this site and computes exact wage pay."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Temporary wage lines can only be recomputed in Draft state."))
        temp_lines = self.env['contractor.site.attendance.line'].search([
            ('project_id', '=', self.project_id.id),
            ('attendance_date', '>=', self.date_start),
            ('attendance_date', '<=', self.date_end),
            ('worker_category', '=', 'temporary'),
            ('attendance_status', 'in', ['present', 'unscheduled_addition']),
            ('attendance_id.state', '=', 'pm_approved'),
        ])

        # Group by employee
        grouped = {}
        for line in temp_lines:
            grouped.setdefault(line.employee_id, []).append(line)

        lines = []
        for emp, atn_records in grouped.items():
            days = len(atn_records)
            reg_hrs = sum(atn_records.mapped('regular_hours'))
            ot_hrs = sum(atn_records.mapped('overtime_hours'))
            reg_pay = sum(atn_records.mapped('regular_cost'))
            ot_pay = sum(atn_records.mapped('ot_cost'))
            tot_pay = reg_pay + ot_pay

            lines.append((0, 0, {
                'employee_id': emp.id,
                'project_id': self.project_id.id,
                'days_worked': days,
                'regular_hours': reg_hrs,
                'ot_hours': ot_hrs,
                'daily_wage': emp.daily_wage,
                'effective_hourly_rate': emp.effective_hourly_rate,
                'regular_pay': reg_pay,
                'ot_pay': ot_pay,
                'total_payable': tot_pay,
            }))

        self.line_ids.unlink()
        self.write({
            'line_ids': lines,
        })
        self._compute_totals()

    def action_hr_prepare(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_hr_officer')):
            raise AccessError(_("Access Denied: Only HR Officers or Managers can prepare temporary wage settlements."))
        if not self.line_ids:
            raise UserError(_("Please compute wage lines before proceeding."))
        self.write({
            'state': 'hr_prepared',
            'prepared_by': self.env.user.id,
        })

    def action_accountant_approve(self):
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_senior_accountant') or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise UserError(_("Access Denied: Only Accountants can verify wage settlements."))
        self.write({
            'state': 'accountant_approved',
            'accountant_id': self.env.user.id,
        })

    def action_release_settlement(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can disburse wage settlements."))

        if self.state != 'accountant_approved':
            raise UserError(_("Temporary wage settlements can only be disbursed after Accountant approval."))
        if self.account_move_id:
            raise UserError(_("A settlement journal entry (%s) has already been posted.") % self.account_move_id.name)
        if 'contractor.period.close' in self.env:
            self.env['contractor.period.close']._check_period_not_locked(self.date_end, self.company_id.id)

        misc_journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)

        wage_exp = self.company_id.default_expense_account_id or self.env['account.account'].search([
            ('internal_group', '=', 'expense'),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1)
        cash_or_payable = self.company_id.default_payroll_payable_account_id or self.env['account.account'].search([
            ('account_type', 'in', ['liability_current', 'payable']),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1) or wage_exp

        move = self.env['account.move'].create({
            'journal_id': misc_journal.id if misc_journal else False,
            'date': self.date_end,
            'ref': f"TWS {self.name} - {self.project_id.name}",
            'move_type': 'entry',
            'line_ids': [
                (0, 0, {
                    'name': f"Temporary Wages: {self.name} ({self.project_id.name})",
                    'account_id': wage_exp.id if wage_exp else False,
                    'debit': self.total_wage_payable,
                    'credit': 0.0,
                    'analytic_distribution': {str(self.project_id.account_id.id): 100.0} if self.project_id.account_id else False,
                }),
                (0, 0, {
                    'name': f"Temporary Wages Payable: {self.name}",
                    'account_id': cash_or_payable.id if cash_or_payable else False,
                    'debit': 0.0,
                    'credit': self.total_wage_payable,
                }),
            ],
        })
        move.action_post()

        # Check for released workers: enforce tool clearance & deactivate employee + ERP user
        for line in self.line_ids:
            if line.is_released_worker:
                if line.employee_id.assigned_tool_count > 0:
                    raise UserError(_(
                        "Tool Custody Clearance Hard-Stop:\n\n"
                        "Cannot release temporary worker '%s' because %d tool(s) remain assigned in field custody.\n"
                        "All tools must be returned or assessed for deduction before worker release."
                    ) % (line.employee_id.name, line.employee_id.assigned_tool_count))
                if line.employee_id.user_id:
                    line.employee_id.user_id.sudo().write({'active': False})
                line.employee_id.active = False

        self.write({
            'state': 'released',
            'manager_id': self.env.user.id,
            'account_move_id': move.id,
        })

        self.message_post(
            body=Markup(_("<strong>Temporary Wages Disbursed:</strong> Paid %s %.2f. Journal Entry %s created.")) % (
                self.currency_id.symbol or '', self.total_wage_payable, move.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        if any(rec.state == 'released' for rec in self) and not set(vals.keys()).issubset(allowed_meta_fields):
            raise UserError(_("Released Temporary Wage Settlements are locked and cannot be modified."))
        return super().write(vals)


class ContractorTempWageSettlementLine(models.Model):
    _name = 'contractor.temp.wage.settlement.line'
    _description = 'Temporary Worker Wage Settlement Line'
    _order = 'employee_id, id'

    settlement_id = fields.Many2one(
        'contractor.temp.wage.settlement',
        string='Settlement Reference',
        required=True,
        ondelete='cascade',
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Temporary Operative',
        required=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        related='settlement_id.project_id',
        store=True,
        readonly=True,
    )
    days_worked = fields.Integer(
        string='Shifts Logged',
    )
    regular_hours = fields.Float(
        string='Regular Hours',
    )
    ot_hours = fields.Float(
        string='OT Hours',
    )
    daily_wage = fields.Monetary(
        string='Daily Wage',
        currency_field='currency_id',
    )
    effective_hourly_rate = fields.Monetary(
        string='Hourly Rate',
        currency_field='currency_id',
    )
    regular_pay = fields.Monetary(
        string='Regular Pay',
        currency_field='currency_id',
    )
    ot_pay = fields.Monetary(
        string='OT Pay',
        currency_field='currency_id',
    )
    total_payable = fields.Monetary(
        string='Total Wage Payable',
        currency_field='currency_id',
        required=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='settlement_id.currency_id',
        readonly=True,
    )
    is_released_worker = fields.Boolean(
        string='Worker Released on Last Day',
        default=False,
        help='Tick if this temporary worker was discharged from the project at the end of this settlement cycle.',
    )
    release_reason = fields.Selection([
        ('project_completed', 'Project Stage / Milestone Completed'),
        ('no_longer_required', 'Manpower No Longer Required'),
        ('performance', 'Performance / Disciplinary'),
        ('other', 'Other Site Requirement'),
    ], string='Release Reason')
    release_date = fields.Date(
        string='Release Date',
    )

    def write(self, vals):
        if any(line.settlement_id.state == 'released' for line in self):
            raise UserError(_("Lines of a released Temporary Wage Settlement cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if any(line.settlement_id.state == 'released' for line in self):
            raise UserError(_("Lines of a released Temporary Wage Settlement cannot be deleted."))
        return super().unlink()

    def action_release_worker(self):
        """Action on the line to discharge worker and settle final wage up to last working day."""
        self.ensure_one()
        self.write({
            'is_released_worker': True,
            'release_date': self.settlement_id.date_end,
            'release_reason': 'project_completed',
        })


# =========================================================================
# 3. Track 3 — Manpower Supply Agency Reconciliation
# =========================================================================
class ContractorAgencyReconciliation(models.Model):
    _name = 'contractor.agency.reconciliation'
    _description = 'Manpower Agency Invoice Reconciliation & Audit'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'date_end desc, id desc'

    name = fields.Char(
        string='Reconciliation Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New MAR'),
        index=True,
    )
    agency_id = fields.Many2one(
        'res.partner',
        string='Manpower Supply Agency',
        required=True,
        domain="[('is_company', '=', True)]",
        tracking=True,
    )
    agreement_id = fields.Many2one(
        'contractor.agency.agreement',
        string='Service SLA Agreement',
        domain="[('agency_id', '=', agency_id)]",
        tracking=True,
    )
    vendor_bill_id = fields.Many2one(
        'account.move',
        string='Agency Vendor Bill',
        domain="[('partner_id', '=', agency_id), ('move_type', '=', 'in_invoice')]",
        tracking=True,
    )
    date_start = fields.Date(
        string='Billing Period Start Date',
        required=True,
        tracking=True,
    )
    date_end = fields.Date(
        string='Billing Period End Date',
        required=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        required=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    state = fields.Selection([
        ('draft', 'Draft Reconciliation'),
        ('matched', 'Verified & Matched'),
        ('discrepancy_flagged', 'Discrepancy Flagged (Payment Blocked)'),
        ('resolved', 'Resolved with Agency'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    line_ids = fields.One2many(
        'contractor.agency.reconciliation.line',
        'reconciliation_id',
        string='Operative Hours Comparison',
        copy=True,
    )
    erp_total_hours = fields.Float(
        string='Total ERP Approved Hours',
        compute='_compute_reconciliation_totals',
        store=True,
    )
    erp_total_provisional_cost = fields.Monetary(
        string='Total ERP Provisional Cost',
        currency_field='currency_id',
        compute='_compute_reconciliation_totals',
        store=True,
    )
    agency_invoiced_hours = fields.Float(
        string='Agency Invoiced Total Hours',
        tracking=True,
    )
    agency_invoiced_amount = fields.Monetary(
        string='Agency Invoiced Total Amount',
        currency_field='currency_id',
        tracking=True,
    )
    variance_hours = fields.Float(
        string='Variance Hours (Agency - ERP)',
        compute='_compute_reconciliation_totals',
        store=True,
    )
    variance_amount = fields.Monetary(
        string='Variance Amount',
        currency_field='currency_id',
        compute='_compute_reconciliation_totals',
        store=True,
    )
    has_variance = fields.Boolean(
        string='Discrepancy Detected',
        compute='_compute_reconciliation_totals',
        store=True,
    )
    resolution_notes = fields.Text(
        string='Discrepancy Resolution & Credit Notes',
        tracking=True,
    )
    resolved_by = fields.Many2one(
        'res.users',
        string='Resolved By',
        readonly=True,
        copy=False,
    )
    resolution_date = fields.Datetime(
        string='Resolution Date',
        readonly=True,
        copy=False,
    )

    @api.depends('line_ids.erp_hours', 'line_ids.erp_cost', 'agency_invoiced_hours', 'agency_invoiced_amount')
    def _compute_reconciliation_totals(self):
        for rec in self:
            tot_h = sum(rec.line_ids.mapped('erp_hours'))
            tot_c = sum(rec.line_ids.mapped('erp_cost'))
            rec.erp_total_hours = tot_h
            rec.erp_total_provisional_cost = tot_c
            v_hrs = (rec.agency_invoiced_hours or 0.0) - tot_h
            v_amt = (rec.agency_invoiced_amount or 0.0) - tot_c
            rec.variance_hours = v_hrs
            rec.variance_amount = v_amt
            rec.has_variance = (abs(v_hrs) > 0.5 or abs(v_amt) > 1.0)

    @api.onchange('vendor_bill_id')
    def _onchange_vendor_bill(self):
        if self.vendor_bill_id:
            self.agency_invoiced_amount = self.vendor_bill_id.amount_total

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New MAR')) == _('New MAR'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.agency.reconciliation') or _('New MAR')
        return super().create(vals_list)

    def action_pull_and_reconcile(self):
        """Pulls ERP site hours for workers of this agency and verifies against invoiced billing."""
        self.ensure_one()
        atn_lines = self.env['contractor.site.attendance.line'].search([
            ('employee_id.agency_partner_id', '=', self.agency_id.id),
            ('attendance_date', '>=', self.date_start),
            ('attendance_date', '<=', self.date_end),
            ('attendance_status', 'in', ['present', 'unscheduled_addition']),
            ('attendance_id.state', '=', 'pm_approved'),
        ])

        # Group by employee
        grouped = {}
        for l in atn_lines:
            grouped.setdefault(l.employee_id, []).append(l)

        lines = []
        for emp, records in grouped.items():
            tot_reg = sum(r.regular_hours for r in records)
            tot_ot = sum(r.overtime_hours for r in records)
            tot_cost = sum(r.calculated_labour_cost for r in records)

            lines.append((0, 0, {
                'employee_id': emp.id,
                'erp_regular_hours': tot_reg,
                'erp_ot_hours': tot_ot,
                'erp_hours': tot_reg + tot_ot,
                'erp_cost': tot_cost,
                'agency_invoiced_hours': tot_reg + tot_ot,
                'agency_invoiced_amount': tot_cost,
            }))

        self.line_ids.unlink()
        self.write({
            'line_ids': lines,
        })
        self._compute_reconciliation_totals()

        if self.has_variance:
            self.state = 'discrepancy_flagged'
            self.message_post(
                body=Markup(_("<div class='alert alert-danger'><strong>Discrepancy Flagged:</strong> Agency invoiced amounts/hours mismatch ERP site records by %s %.2f. Vendor bill approval is blocked until resolved.</div>")) % (
                    self.currency_id.symbol or '', self.variance_amount
                ),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
        else:
            self.state = 'matched'
            self.message_post(
                body=Markup(_("<strong>Reconciliation Verified:</strong> Agency hours and amounts match ERP site records precisely.")),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )

    def action_resolve_discrepancy(self):
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_manager') or self.env.user.has_group('contractor_core.group_contractor_senior_accountant')):
            raise UserError(_("Access Denied: Only a Contractor Manager or Senior Accountant can resolve agency billing discrepancies."))

        clean_notes = (self.resolution_notes or '').strip()
        if len(clean_notes) < 15:
            raise UserError(_("Please document the resolution agreement with the agency (credit note issued, hours revised) in 'Resolution Notes' (minimum 15 characters)."))

        self.write({
            'state': 'resolved',
            'resolved_by': self.env.user.id,
            'resolution_date': fields.Datetime.now(),
        })

        self.message_post(
            body=Markup(_("<strong>Discrepancy Formally Resolved:</strong> Authorized by %s.<br/>Resolution Details: %s")) % (
                self.env.user.name, self.resolution_notes
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )


class ContractorAgencyReconciliationLine(models.Model):
    _name = 'contractor.agency.reconciliation.line'
    _description = 'Agency Operative Reconciliation Line'
    _order = 'employee_id, id'

    reconciliation_id = fields.Many2one(
        'contractor.agency.reconciliation',
        string='Reconciliation Reference',
        required=True,
        ondelete='cascade',
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Agency Operative',
        required=True,
    )
    erp_regular_hours = fields.Float(
        string='ERP Regular Hours',
    )
    erp_ot_hours = fields.Float(
        string='ERP OT Hours',
    )
    erp_hours = fields.Float(
        string='Total ERP Hours',
    )
    erp_cost = fields.Monetary(
        string='ERP Provisional Cost',
        currency_field='currency_id',
    )
    agency_invoiced_hours = fields.Float(
        string='Invoiced Hours',
    )
    agency_invoiced_amount = fields.Monetary(
        string='Invoiced Amount',
        currency_field='currency_id',
    )
    variance_hours = fields.Float(
        string='Hours Variance',
        compute='_compute_line_variance',
    )
    variance_amount = fields.Monetary(
        string='Amount Variance',
        currency_field='currency_id',
        compute='_compute_line_variance',
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='reconciliation_id.currency_id',
        readonly=True,
    )

    @api.depends('erp_hours', 'agency_invoiced_hours', 'erp_cost', 'agency_invoiced_amount')
    def _compute_line_variance(self):
        for line in self:
            line.variance_hours = (line.agency_invoiced_hours or 0.0) - (line.erp_hours or 0.0)
            line.variance_amount = (line.agency_invoiced_amount or 0.0) - (line.erp_cost or 0.0)


# =========================================================================
# 4. Account Move Hard-Stop for Unresolved Agency Invoices
# =========================================================================
class AccountMoveAgencyReconciliation(models.Model):
    _inherit = 'account.move'

    def action_post(self):
        for move in self:
            if move.is_invoice(include_receipts=True) and move.move_type == 'in_invoice':
                # Check if there is an agency reconciliation linked or if partner is an agency
                recons = self.env['contractor.agency.reconciliation'].search([
                    ('vendor_bill_id', '=', move.id),
                    ('state', '=', 'discrepancy_flagged'),
                ])
                if recons:
                    rec = recons[0]
                    raise UserError(_(
                        "Manpower Agency Invoice Hard-Stop:\n\n"
                        "Vendor Bill '%s' cannot be posted or released for payment.\n\n"
                        "An unresolved discrepancy exists in Manpower Agency Reconciliation '%s'.\n"
                        "ERP Site Hours: %.2f hrs (Provisional Cost: %s %.2f)\n"
                        "Agency Invoiced: %.2f hrs (Amount: %s %.2f)\n\n"
                        "Posting is blocked until the discrepancy is formally resolved with documented credit notes."
                    ) % (
                        move.name or 'Draft Bill', rec.name,
                        rec.erp_total_hours, move.currency_id.symbol or '', rec.erp_total_provisional_cost,
                        rec.agency_invoiced_hours, move.currency_id.symbol or '', rec.agency_invoiced_amount
                    ))
        return super().action_post()

