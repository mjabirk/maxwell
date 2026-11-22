# -*- coding: utf-8 -*-
from datetime import timedelta
from dateutil.relativedelta import relativedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


# =========================================================================
# 0. Leave Type Policy Extension (Paid %, Unpaid EOSB Exclusion)
# =========================================================================
class HrLeaveTypeContractor(models.Model):
    _inherit = 'hr.work.entry.type'

    leave_pay_percentage = fields.Float(
        string='Leave Pay Policy (%)',
        default=100.0,
        help='Percentage of basic salary payable (100% for full-pay annual leave, partial % for extended sick leave, 0% for unpaid).',
    )
    exclude_from_eosb_accrual = fields.Boolean(
        string='Unpaid Leave (Excludes EOSB/Leave Accrual)',
        default=False,
        help='Tick if leave days under this type must be deducted from End-of-Service service duration.',
    )


# =========================================================================
# 1. Leave Management Extensions for Construction Permanent Staff
# =========================================================================
class HrLeaveContractor(models.Model):
    _inherit = ['hr.leave', 'contractor.no.delete.mixin', 'contractor.reason.mixin']
    _name = 'hr.leave'

    current_leave_balance = fields.Float(
        string='Current Available Annual Leave Balance',
        compute='_compute_leave_balance',
    )
    is_critical_project_period = fields.Boolean(
        string='Falls in Critical Project Milestone / Handover',
        default=False,
        tracking=True,
        help='Tick if this leave request overlaps with a concrete pour, client handover, or critical path milestone.',
    )
    pm_acknowledged_by_id = fields.Many2one(
        'res.users',
        string='PM Milestone Acknowledgement By',
        readonly=True,
        copy=False,
    )
    pm_acknowledgement_date = fields.Datetime(
        string='PM Acknowledgement Date',
        readonly=True,
        copy=False,
    )
    leave_salary_request_id = fields.Many2one(
        'contractor.leave.salary.request',
        string='Leave Salary Request',
        readonly=True,
        copy=False,
    )
    leave_salary_paid = fields.Boolean(
        string='Leave Salary Paid',
        default=False,
        readonly=True,
        copy=False,
    )

    @api.constrains('employee_id')
    def _check_permanent_category(self):
        for leave in self:
            if leave.employee_id and leave.employee_id.workforce_category != 'permanent':
                raise ValidationError(_(
                    "Leave Governance Hard-Stop:\n\n"
                    "Formal leave requests in ERP are strictly restricted to Permanent in-house staff.\n"
                    "Operative '%s' is categorized as '%s'.\n"
                    "Temporary workers are paid on daily site hours logged; Manpower supply personnel are administered through their supply agency."
                ) % (leave.employee_id.name, dict(self.env['hr.employee']._fields['workforce_category'].selection).get(leave.employee_id.workforce_category)))

    @api.depends('employee_id', 'work_entry_type_id')
    def _compute_leave_balance(self):
        for leave in self:
            if leave.employee_id:
                alloc_domain = [
                    ('employee_id', '=', leave.employee_id.id),
                    ('state', '=', 'validate'),
                ]
                taken_domain = [
                    ('employee_id', '=', leave.employee_id.id),
                    ('state', '=', 'validate'),
                ]
                if leave.work_entry_type_id:
                    alloc_domain.append(('work_entry_type_id', '=', leave.work_entry_type_id.id))
                    taken_domain.append(('work_entry_type_id', '=', leave.work_entry_type_id.id))
                allocations = self.env['hr.leave.allocation'].search(alloc_domain)
                taken_leaves = self.env['hr.leave'].search(taken_domain)
                allocated_days = sum(allocations.mapped('number_of_days'))
                taken_days = sum(taken_leaves.mapped('number_of_days'))
                leave.current_leave_balance = max(0.0, allocated_days - taken_days)
            else:
                leave.current_leave_balance = 0.0

    def action_pm_acknowledge(self):
        """Allows Project Manager to acknowledge critical milestone leave."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_project_manager'):
            raise UserError(_("Access Denied: Only a Contractor Project Manager can acknowledge milestone leave."))

        self.write({
            'pm_acknowledged_by_id': self.env.user.id,
            'pm_acknowledgement_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Milestone Leave Acknowledged:</strong> Project Manager %s confirmed critical project coverage.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def _action_validate(self, check_state=True):
        for leave in self:
            if leave.is_critical_project_period and not leave.pm_acknowledged_by_id:
                raise UserError(_(
                    "Project Milestone Hard-Stop:\n\n"
                    "This leave falls in a critical project period. Project Manager acknowledgement is strictly mandatory before approval."
                ))
        res = super()._action_validate(check_state=check_state)
        # Automatically mark draft site attendance lines overlapping approved leave dates as On Leave
        AttendanceLine = self.env['contractor.site.attendance.line'].sudo()
        for leave in self:
            if leave.employee_id and leave.request_date_from and leave.request_date_to:
                overlapping_lines = AttendanceLine.search([
                    ('employee_id', '=', leave.employee_id.id),
                    ('attendance_date', '>=', leave.request_date_from),
                    ('attendance_date', '<=', leave.request_date_to),
                    ('attendance_id.state', '=', 'draft'),
                ])
                if overlapping_lines:
                    overlapping_lines.write({
                        'attendance_status': 'on_leave',
                        'regular_hours': 0.0,
                        'overtime_hours': 0.0,
                    })
        return res


# =========================================================================
# 2. Leave Salary Calculator Model
# =========================================================================
class ContractorLeaveSalaryRequest(models.Model):
    _name = 'contractor.leave.salary.request'
    _description = 'Contractor Leave Salary Payout Request'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='Request Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New LSR'),
        index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Permanent Employee',
        required=True,
        domain="[('workforce_category', '=', 'permanent')]",
        tracking=True,
    )
    leave_id = fields.Many2one(
        'hr.leave',
        string='Approved Annual Leave',
        domain="[('employee_id', '=', employee_id), ('state', '=', 'validate'), ('leave_salary_paid', '=', False)]",
        required=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='employee_id.company_id',
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    leave_days = fields.Float(
        string='Leave Duration (Days)',
        compute='_compute_leave_salary_amount',
        store=True,
    )
    daily_salary_rate = fields.Monetary(
        string='Calculated Daily Basic Rate',
        currency_field='currency_id',
        compute='_compute_leave_salary_amount',
        store=True,
    )
    basic_hourly_rate = fields.Monetary(
        string='Basic Hourly Rate',
        currency_field='currency_id',
        compute='_compute_leave_salary_amount',
        store=True,
    )
    pay_percentage = fields.Float(
        string='Applicable Pay Policy (%)',
        compute='_compute_leave_salary_amount',
        store=True,
    )
    leave_salary_amount = fields.Monetary(
        string='Calculated Statutory Leave Salary',
        currency_field='currency_id',
        compute='_compute_leave_salary_amount',
        store=True,
        help='Calculated from Employee Basic Salary, Leave Policy %, and Annual Leave Days entitlement.',
    )
    approved_amount = fields.Monetary(
        string='Approved Payout Amount',
        currency_field='currency_id',
        required=True,
        tracking=True,
    )
    payout_method = fields.Selection([
        ('advance_release', 'Direct Advance Release before Travel'),
        ('with_payroll', 'Processed with Monthly Payroll Run'),
    ], string='Payout Method', default='advance_release', required=True, tracking=True)

    state = fields.Selection([
        ('draft', 'Draft Calculation'),
        ('hr_prepared', 'Prepared by HR'),
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
    accountant_id = fields.Many2one(
        'res.users',
        string='Senior Accountant Approver',
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
        string='Advance Payment Journal Entry',
        readonly=True,
        copy=False,
    )

    @api.depends('employee_id', 'leave_id', 'leave_id.work_entry_type_id.leave_pay_percentage')
    def _compute_leave_salary_amount(self):
        for req in self:
            if not req.employee_id or not req.leave_id:
                req.leave_days = 0.0
                req.basic_hourly_rate = 0.0
                req.pay_percentage = 100.0
                req.daily_salary_rate = 0.0
                req.leave_salary_amount = 0.0
                continue

            company = req.company_id or self.env.company
            std_monthly = company.standard_monthly_hours or 208.0
            std_daily = company.standard_daily_hours or 8.0
            emp = req.employee_id
            basic_monthly = emp.monthly_basic_salary or emp.monthly_total_salary or 0.0
            basic_hr = (basic_monthly / std_monthly) if std_monthly > 0 else (emp.system_calculated_hourly_rate or 0.0)
            daily_rate = basic_hr * std_daily

            days = req.leave_id.number_of_days or 0.0
            ltype = req.leave_id.work_entry_type_id
            is_unpaid = ltype.unpaid or ltype.exclude_from_eosb_accrual or ('unpaid' in (ltype.name or '').lower())
            if is_unpaid:
                pay_pct = 0.0
                calc_amt = 0.0
            else:
                pay_pct = ltype.leave_pay_percentage if ltype.leave_pay_percentage else 100.0
                entitlement = company.annual_leave_salary_days or 21.0
                calc_amt = daily_rate * min(days, entitlement) * (pay_pct / 100.0)

            req.leave_days = days
            req.basic_hourly_rate = basic_hr
            req.pay_percentage = pay_pct
            req.daily_salary_rate = daily_rate
            req.leave_salary_amount = calc_amt
            if not req.approved_amount:
                req.approved_amount = calc_amt

    @api.constrains('leave_id')
    def _check_duplicate_request(self):
        for req in self:
            if req.leave_id:
                existing = self.search([
                    ('leave_id', '=', req.leave_id.id),
                    ('id', '!=', req.id),
                    ('state', '!=', 'cancelled'),
                ])
                if existing:
                    raise ValidationError(_("A Leave Salary Request (%s) already exists for this approved leave record.") % existing[0].name)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New LSR')) == _('New LSR'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.leave.salary.request') or _('New LSR')
        return super().create(vals_list)

    def action_hr_prepare(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_hr_officer')):
            raise AccessError(_("Access Denied: Only HR Officers or Managers can prepare leave salary requests."))
        if self.leave_id.leave_salary_paid:
            raise UserError(_("Leave salary has already been paid for this leave."))
        self.write({
            'state': 'hr_prepared',
            'prepared_by': self.env.user.id,
        })

    def action_accountant_approve(self):
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_senior_accountant') or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise UserError(_("Access Denied: Only a Senior Accountant can verify leave salary requests."))
        self.write({
            'state': 'accountant_approved',
            'accountant_id': self.env.user.id,
        })

    def action_release_salary(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize leave salary release."))

        if self.state != 'accountant_approved':
            raise UserError(_("Leave salary can only be released after Senior Accountant approval."))
        if self.account_move_id:
            raise UserError(_("A leave salary journal entry (%s) has already been posted.") % self.account_move_id.name)

        move = False
        if self.payout_method == 'advance_release':
            misc_journal = self.env['account.journal'].search([
                ('type', '=', 'general'),
                ('company_id', '=', self.company_id.id),
            ], limit=1)
            expense_acc = self.company_id.default_expense_account_id or self.env['account.account'].search([
                ('internal_group', '=', 'expense'),
                ('company_ids', 'in', self.company_id.ids),
            ], limit=1)
            payable_acc = self.company_id.default_payroll_payable_account_id or self.env['account.account'].search([
                ('account_type', 'in', ['liability_current', 'liability_payable']),
                ('company_ids', 'in', self.company_id.ids),
            ], limit=1) or expense_acc

            move = self.env['account.move'].create({
                'journal_id': misc_journal.id if misc_journal else False,
                'date': fields.Date.context_today(self),
                'ref': f"Leave Salary {self.name} - {self.employee_id.name}",
                'move_type': 'entry',
                'line_ids': [
                    (0, 0, {
                        'name': f"Leave Salary Advance: {self.employee_id.name}",
                        'account_id': expense_acc.id if expense_acc else False,
                        'debit': self.approved_amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': f"Advance Payable: {self.employee_id.name}",
                        'account_id': payable_acc.id if payable_acc else False,
                        'debit': 0.0,
                        'credit': self.approved_amount,
                    }),
                ],
            })
            move.action_post()

        self.write({
            'state': 'released',
            'manager_id': self.env.user.id,
            'account_move_id': move.id if move else False,
        })

        # Lock leave record
        self.leave_id.write({
            'leave_salary_request_id': self.id,
            'leave_salary_paid': True,
        })

        self.message_post(
            body=Markup(_("<strong>Leave Salary Disbursed:</strong> Paid %s %.2f. Leave salary status locked against duplicates.")) % (
                self.currency_id.symbol or '', self.approved_amount
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
            raise UserError(_("Released Leave Salary Requests are locked and cannot be modified."))
        return super().write(vals)


# =========================================================================
# 3. Air Ticket Allowance Tracker & Request Model
# =========================================================================
class ContractorTicketAllowanceRequest(models.Model):
    _name = 'contractor.ticket.allowance.request'
    _description = 'Air Ticket Allowance Disbursement Request'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'request_date desc, id desc'

    name = fields.Char(
        string='Ticket Request Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New TAR'),
        index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Permanent Employee',
        required=True,
        domain="[('workforce_category', '=', 'permanent')]",
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='employee_id.company_id',
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    request_date = fields.Date(
        string='Request Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    disbursement_type = fields.Selection([
        ('cash_allowance', 'Cash Travel Allowance Payout'),
        ('ticket_booking', 'Direct Airline Ticket Booking'),
    ], string='Disbursement Method', default='cash_allowance', required=True, tracking=True)

    allowance_amount = fields.Monetary(
        string='Ticket Allowance Amount',
        currency_field='currency_id',
        required=True,
        tracking=True,
    )
    last_ticket_date = fields.Date(
        string='Last Ticket Date',
        related='employee_id.last_ticket_date',
        readonly=True,
    )
    next_ticket_due_date = fields.Date(
        string='Next Due Date',
        related='employee_id.next_ticket_due_date',
        readonly=True,
    )
    is_early_release = fields.Boolean(
        string='Early / Premature Cycle Release',
        compute='_compute_early_release',
        store=True,
    )
    manager_override_reason = fields.Text(
        string='Manager Early Payout Justification',
        help='Mandatory justification if disbursing ticket allowance prior to contractual entitlement cycle.',
    )
    state = fields.Selection([
        ('draft', 'Draft Request'),
        ('hr_prepared', 'Prepared by HR'),
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
    accountant_id = fields.Many2one(
        'res.users',
        string='Audited By Senior Accountant',
        readonly=True,
        copy=False,
    )
    manager_id = fields.Many2one(
        'res.users',
        string='Approved By Manager',
        readonly=True,
        copy=False,
    )
    account_move_id = fields.Many2one(
        'account.move',
        string='Accounting Journal Entry',
        readonly=True,
        copy=False,
    )

    @api.onchange('employee_id')
    def _onchange_employee(self):
        if self.employee_id:
            self.allowance_amount = self.employee_id.ticket_allowance_amount or 1500.0

    @api.depends('employee_id.next_ticket_due_date', 'request_date')
    def _compute_early_release(self):
        for req in self:
            if req.employee_id and req.employee_id.next_ticket_due_date and req.request_date:
                req.is_early_release = req.request_date < req.employee_id.next_ticket_due_date
            else:
                req.is_early_release = False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New TAR')) == _('New TAR'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.ticket.allowance.request') or _('New TAR')
        return super().create(vals_list)

    def action_hr_prepare(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_hr_officer')):
            raise AccessError(_("Access Denied: Only HR Officers or Managers can prepare ticket allowance requests."))
        if self.is_early_release:
            if not self.manager_override_reason or len(self.manager_override_reason.strip()) < 15:
                raise UserError(_(
                    "Early Cycle Ticket Release Hard-Stop:\n\n"
                    "Employee is not yet contractually due for ticket allowance (Due: %s).\n"
                    "A documented Manager Early Payout Justification (minimum 15 characters) is mandatory."
                ) % self.next_ticket_due_date)
        self.write({
            'state': 'hr_prepared',
            'prepared_by': self.env.user.id,
        })

    def action_accountant_approve(self):
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_senior_accountant') or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise UserError(_("Access Denied: Only a Senior Accountant can verify ticket allowances."))
        self.write({
            'state': 'accountant_approved',
            'accountant_id': self.env.user.id,
        })

    def action_release_ticket(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize ticket allowance release."))

        if self.state != 'accountant_approved':
            raise UserError(_("Ticket allowance can only be released after Senior Accountant approval."))
        if self.account_move_id:
            raise UserError(_("A ticket allowance journal entry (%s) has already been posted.") % self.account_move_id.name)

        misc_journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        expense_acc = self.company_id.default_expense_account_id or self.env['account.account'].search([
            ('internal_group', '=', 'expense'),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1)
        payable_acc = self.company_id.default_payroll_payable_account_id or self.env['account.account'].search([
            ('account_type', 'in', ['liability_current', 'liability_payable']),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1) or expense_acc

        move = self.env['account.move'].create({
            'journal_id': misc_journal.id if misc_journal else False,
            'date': self.request_date,
            'ref': f"Ticket Allowance {self.name} - {self.employee_id.name}",
            'move_type': 'entry',
            'line_ids': [
                (0, 0, {
                    'name': f"Air Ticket Allowance: {self.employee_id.name}",
                    'account_id': expense_acc.id if expense_acc else False,
                    'debit': self.allowance_amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': f"Ticket Allowance Payable: {self.employee_id.name}",
                    'account_id': payable_acc.id if payable_acc else False,
                    'debit': 0.0,
                    'credit': self.allowance_amount,
                }),
            ],
        })
        move.action_post()

        # Update last ticket date on employee
        self.employee_id.write({
            'last_ticket_date': self.request_date,
        })

        self.write({
            'state': 'released',
            'manager_id': self.env.user.id,
            'account_move_id': move.id,
        })

        self.message_post(
            body=Markup(_("<strong>Air Ticket Disbursed:</strong> Paid %s %.2f. Employee next due date recalculated.")) % (
                self.currency_id.symbol or '', self.allowance_amount
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
            raise UserError(_("Released Ticket Allowance Requests are locked and cannot be modified."))
        return super().write(vals)

