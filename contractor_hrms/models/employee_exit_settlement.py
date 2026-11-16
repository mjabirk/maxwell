# -*- coding: utf-8 -*-
from datetime import timedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorEmployeeExit(models.Model):
    _name = 'contractor.employee.exit'
    _description = 'Contractor Employee Exit & Final EOSB Settlement'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'last_working_day desc, id desc'

    name = fields.Char(
        string='Settlement Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Exit'),
        index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Departing Employee',
        required=True,
        tracking=True,
    )
    workforce_category = fields.Selection(
        related='employee_id.workforce_category',
        string='Workforce Classification',
        store=True,
        readonly=True,
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
    join_date = fields.Date(
        string='Date of Joining',
        compute='_compute_employee_details',
        store=True,
        readonly=False,
    )
    last_working_day = fields.Date(
        string='Last Working Day',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    exit_type = fields.Selection([
        ('resignation', 'Resignation by Employee'),
        ('termination', 'Termination by Employer'),
        ('contract_end', 'Contract Expiry / Non-Renewal'),
    ], string='Exit Separation Type', default='resignation', required=True, tracking=True)

    reason = fields.Text(
        string='Documented Reason for Departure',
        required=True,
        tracking=True,
    )
    notice_status = fields.Selection([
        ('served', 'Full Contractual Notice Served'),
        ('waived', 'Notice Period Waived by Employer'),
        ('short_notice', 'Short Notice / Early Release (Deduction Applicable)'),
    ], string='Notice Period Status', default='served', required=True, tracking=True)

    years_of_service = fields.Float(
        string='Length of Service (Years)',
        compute='_compute_service_and_eosb',
        store=True,
    )
    basic_salary = fields.Monetary(
        string='Last Drawn Basic Salary',
        currency_field='currency_id',
        compute='_compute_employee_details',
        store=True,
        readonly=False,
    )
    monthly_gross = fields.Monetary(
        string='Last Drawn Monthly Gross',
        currency_field='currency_id',
        compute='_compute_employee_details',
        store=True,
        readonly=False,
    )

    # Final Settlement Payables & Deductions
    eosb_amount = fields.Monetary(
        string='End of Service Benefits (EOSB)',
        currency_field='currency_id',
        compute='_compute_service_and_eosb',
        store=True,
        help='Statutory end of service gratuity based on length of service, exit type, and basic salary.',
    )
    unpaid_salary_amount = fields.Monetary(
        string='Unpaid Salary for Last Month',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    unused_leave_salary = fields.Monetary(
        string='Encashment of Unused Leave',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    pending_ticket_allowance = fields.Monetary(
        string='Repatriation Ticket Allowance',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    employee_advances_loans = fields.Monetary(
        string='Advances / Loan Recovery Deductions',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    damaged_lost_tool_deductions = fields.Monetary(
        string='Unreturned / Damaged Tool Deductions',
        currency_field='currency_id',
        compute='_compute_tool_deductions',
        store=True,
        readonly=False,
        tracking=True,
    )
    net_settlement_amount = fields.Monetary(
        string='Final Net Settlement Payout',
        currency_field='currency_id',
        compute='_compute_net_settlement',
        store=True,
    )

    # Tool Clearance Hard-Stop Fields
    unreturned_tool_count = fields.Integer(
        string='Outstanding Tools in Custody',
        compute='_compute_tool_clearance',
        store=True,
    )
    tool_clearance_status = fields.Selection([
        ('cleared', 'All Tools Returned & Cleared'),
        ('unreturned', 'Outstanding Tools in Custody'),
        ('manager_override', 'Manager Tool Exception Authorized'),
    ], string='Tool Custody Clearance', compute='_compute_tool_clearance', store=True)

    tool_exception_reason = fields.Text(
        string='Manager Tool Clearance Exception Justification',
        copy=False,
    )
    tool_exception_manager_id = fields.Many2one(
        'res.users',
        string='Tool Exception Authorized By',
        readonly=True,
        copy=False,
    )

    state = fields.Selection([
        ('draft', 'Draft Clearance'),
        ('hr_prepared', 'Prepared by HR Officer'),
        ('accountant_approved', 'Approved by Senior Accountant'),
        ('released', 'Settled & System Access Revoked'),
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
        string='Final Settlement Journal Entry',
        readonly=True,
        copy=False,
    )

    @api.depends('employee_id')
    def _compute_employee_details(self):
        for exit_rec in self:
            if exit_rec.employee_id:
                exit_rec.join_date = exit_rec.employee_id.create_date.date() if exit_rec.employee_id.create_date else False
                exit_rec.basic_salary = exit_rec.employee_id.monthly_basic_salary or 0.0
                exit_rec.monthly_gross = exit_rec.employee_id.monthly_total_salary or 0.0
            else:
                exit_rec.join_date = False
                exit_rec.basic_salary = 0.0
                exit_rec.monthly_gross = 0.0

    @api.depends('employee_id.pending_tool_deduction_amount')
    def _compute_tool_deductions(self):
        for exit_rec in self:
            if exit_rec.employee_id:
                exit_rec.damaged_lost_tool_deductions = exit_rec.employee_id.pending_tool_deduction_amount or 0.0
            else:
                exit_rec.damaged_lost_tool_deductions = 0.0

    @api.depends('employee_id.assigned_tool_ids.status', 'tool_exception_manager_id')
    def _compute_tool_clearance(self):
        for exit_rec in self:
            if not exit_rec.employee_id:
                exit_rec.unreturned_tool_count = 0
                exit_rec.tool_clearance_status = 'cleared'
                continue

            active_tools = exit_rec.employee_id.assigned_tool_ids.filtered(lambda t: t.status == 'assigned')
            count = len(active_tools)
            exit_rec.unreturned_tool_count = count

            if exit_rec.tool_exception_manager_id:
                exit_rec.tool_clearance_status = 'manager_override'
            elif count == 0:
                exit_rec.tool_clearance_status = 'cleared'
            else:
                exit_rec.tool_clearance_status = 'unreturned'

    @api.depends('join_date', 'last_working_day', 'basic_salary', 'exit_type', 'workforce_category', 'employee_id')
    def _compute_service_and_eosb(self):
        for exit_rec in self:
            if not exit_rec.join_date or not exit_rec.last_working_day or exit_rec.join_date > exit_rec.last_working_day:
                exit_rec.years_of_service = 0.0
                exit_rec.eosb_amount = 0.0
                continue

            gross_days = (exit_rec.last_working_day - exit_rec.join_date).days
            unpaid_days = 0.0
            if exit_rec.employee_id:
                unpaid_leaves = self.env['hr.leave'].sudo().search([
                    ('employee_id', '=', exit_rec.employee_id.id),
                    ('state', '=', 'validate'),
                ]).filtered(lambda l: l.holiday_status_id.exclude_from_eosb_accrual or l.holiday_status_id.unpaid)
                unpaid_days = sum(unpaid_leaves.mapped('number_of_days'))
            days = max(gross_days - unpaid_days, 0.0)
            years = days / 365.25
            exit_rec.years_of_service = round(years, 2)

            # Manpower supply and temporary workers do not accrue permanent EOSB
            if exit_rec.workforce_category != 'permanent' or years < 1.0:
                exit_rec.eosb_amount = 0.0
                continue

            # Configurable Statutory Construction EOSB Calculation
            company = exit_rec.company_id or self.env.company
            first_5_rate = company.eosb_first_5yr_days or 21.0
            after_5_rate = company.eosb_after_5yr_days or 30.0
            daily_basic = (exit_rec.basic_salary or 0.0) / 30.0
            first_5_years = min(years, 5.0)
            beyond_5_years = max(years - 5.0, 0.0)

            full_eosb = (first_5_years * first_5_rate * daily_basic) + (beyond_5_years * after_5_rate * daily_basic)

            if exit_rec.exit_type == 'resignation':
                if years < 1.0:
                    entitled_eosb = 0.0
                elif 1.0 <= years < 3.0:
                    entitled_eosb = full_eosb * (1.0 / 3.0)
                elif 3.0 <= years < 5.0:
                    entitled_eosb = full_eosb * (2.0 / 3.0)
                else:
                    entitled_eosb = full_eosb
            else:
                # Termination or Contract End
                entitled_eosb = full_eosb

            exit_rec.eosb_amount = entitled_eosb

    @api.depends(
        'eosb_amount',
        'unpaid_salary_amount',
        'unused_leave_salary',
        'pending_ticket_allowance',
        'employee_advances_loans',
        'damaged_lost_tool_deductions',
        'workforce_category',
    )
    def _compute_net_settlement(self):
        for exit_rec in self:
            if exit_rec.workforce_category == 'manpower_supply':
                exit_rec.net_settlement_amount = 0.0
                continue

            earnings = (
                (exit_rec.eosb_amount or 0.0) +
                (exit_rec.unpaid_salary_amount or 0.0) +
                (exit_rec.unused_leave_salary or 0.0) +
                (exit_rec.pending_ticket_allowance or 0.0)
            )
            deductions = (
                (exit_rec.employee_advances_loans or 0.0) +
                (exit_rec.damaged_lost_tool_deductions or 0.0)
            )
            exit_rec.net_settlement_amount = max(earnings - deductions, 0.0)

    @api.constrains('tool_clearance_status', 'state')
    def _check_tool_clearance_gate(self):
        for exit_rec in self:
            if exit_rec.state in ('accountant_approved', 'released'):
                if exit_rec.tool_clearance_status == 'unreturned':
                    raise ValidationError(_(
                        "Tool Clearance Governance Hard-Stop:\n\n"
                        "Cannot approve or release final exit settlement.\n"
                        "Operative '%s' has %d unreturned tool(s) in field custody.\n\n"
                        "All tools must be returned to stores or an approved Management Tool Exception must be authorized."
                    ) % (exit_rec.employee_id.name, exit_rec.unreturned_tool_count))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Exit')) == _('New Exit'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.employee.exit') or _('New Exit')
        records = super().create(vals_list)
        records.action_pull_pending_entitlements_and_deductions()
        return records

    @api.onchange('employee_id', 'last_working_day')
    def _onchange_pull_exit_balances(self):
        if self.employee_id:
            vals = self._get_auto_settlement_balances()
            if not self.unused_leave_salary:
                self.unused_leave_salary = vals['unused_leave_salary']
            if not self.pending_ticket_allowance:
                self.pending_ticket_allowance = vals['pending_ticket_allowance']
            if not self.employee_advances_loans:
                self.employee_advances_loans = vals['employee_advances_loans']

    def _get_auto_settlement_balances(self):
        self.ensure_one()
        emp = self.employee_id
        if not emp:
            return {'unused_leave_salary': 0.0, 'pending_ticket_allowance': 0.0, 'employee_advances_loans': 0.0}

        # 1. Unused Annual Leave Encashment
        allocs = self.env['hr.leave.allocation'].sudo().search([
            ('employee_id', '=', emp.id),
            ('state', '=', 'validate'),
        ])
        taken = self.env['hr.leave'].sudo().search([
            ('employee_id', '=', emp.id),
            ('state', '=', 'validate'),
        ]).filtered(lambda l: not l.holiday_status_id.unpaid and not l.holiday_status_id.exclude_from_eosb_accrual)
        remaining_days = max(0.0, sum(allocs.mapped('number_of_days')) - sum(taken.mapped('number_of_days')))
        daily_basic = (emp.monthly_basic_salary or emp.monthly_total_salary or 0.0) / 30.0
        unused_leave_val = round(remaining_days * daily_basic, 2)

        # 2. Pending Air Ticket Allowance Requests
        pending_tickets = self.env['contractor.ticket.allowance.request'].sudo().search([
            ('employee_id', '=', emp.id),
            ('state', 'in', ['draft', 'hr_prepared', 'accountant_approved']),
        ])
        ticket_val = sum(pending_tickets.mapped('allowance_amount'))
        if not ticket_val and emp.ticket_due_flag and emp.ticket_allowance_amount:
            ticket_val = emp.ticket_allowance_amount

        # 3. Open Employee Advances / Loans (from contractor_finance when installed)
        advance_val = 0.0
        if 'contractor.employee.advance' in self.env:
            open_advances = self.env['contractor.employee.advance'].sudo().search([
                ('employee_id', '=', emp.id),
                ('state', 'in', ['approved', 'disbursed']),
            ])
            advance_val = sum(open_advances.mapped('outstanding_balance'))

        return {
            'unused_leave_salary': unused_leave_val,
            'pending_ticket_allowance': ticket_val,
            'employee_advances_loans': advance_val,
        }

    def action_pull_pending_entitlements_and_deductions(self):
        """Auto-populates unused leave encashment, pending tickets, and open employee advances/loans."""
        for exit_rec in self:
            if not exit_rec.employee_id:
                continue
            vals = exit_rec._get_auto_settlement_balances()
            updates = {}
            if not exit_rec.unused_leave_salary and vals['unused_leave_salary']:
                updates['unused_leave_salary'] = vals['unused_leave_salary']
            if not exit_rec.pending_ticket_allowance and vals['pending_ticket_allowance']:
                updates['pending_ticket_allowance'] = vals['pending_ticket_allowance']
            if not exit_rec.employee_advances_loans and vals['employee_advances_loans']:
                updates['employee_advances_loans'] = vals['employee_advances_loans']
            if updates:
                exit_rec.write(updates)
        return True

    def action_hr_prepare(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_hr_officer')):
            raise AccessError(_("Access Denied: Only HR Officers or Managers can prepare exit settlements."))
        self.write({
            'state': 'hr_prepared',
            'prepared_by': self.env.user.id,
        })
        self.message_post(
            body=Markup(_("<strong>Exit Settlement Prepared by HR:</strong> Submitted for Senior Accountant audit.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_accountant_approve(self):
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_senior_accountant') or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise UserError(_("Access Denied: Only a Senior Accountant can audit exit settlements."))

        self._compute_tool_clearance()
        if self.tool_clearance_status == 'unreturned':
            raise UserError(_(
                "Tool Clearance Hard-Stop:\n\n"
                "Audit approval blocked. Operative '%s' has %d unreturned tool(s) in custody."
            ) % (self.employee_id.name, self.unreturned_tool_count))

        self.write({
            'state': 'accountant_approved',
            'accountant_id': self.env.user.id,
        })
        self.message_post(
            body=Markup(_("<strong>Exit Settlement Audited & Approved:</strong> Certified by Senior Accountant %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_authorize_tool_exception(self):
        """Allows Contractor Manager to authorize a tool clearance exception."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize tool clearance exceptions."))

        return {
            'name': _('Authorize Tool Clearance Exception'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': 'override',
                'default_callback_method': '_callback_tool_exception',
            },
        }

    def _callback_tool_exception(self, reason=None):
        clean_reason = (reason or getattr(self, 'last_action_reason', '')).strip()
        if len(clean_reason) < 15:
            raise UserError(_("Documented justification (minimum 15 characters) is required to waive tool clearance."))

        self.write({
            'tool_exception_manager_id': self.env.user.id,
            'tool_exception_reason': clean_reason,
            'tool_clearance_status': 'manager_override',
        })
        self.message_post(
            body=Markup(_("<strong>Tool Clearance Exception Granted:</strong> Waived by %s.<br/>Reason: %s")) % (
                self.env.user.name, clean_reason
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_release_settlement(self):
        """Authorizes final payout and automatically revokes ERP user access and archives employee record."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize final settlement payout."))

        if self.state != 'accountant_approved':
            raise UserError(_("Final exit settlement can only be released after Senior Accountant audit approval."))
        if self.account_move_id:
            raise UserError(_("An exit settlement journal entry (%s) has already been posted.") % self.account_move_id.name)

        self._compute_tool_clearance()
        if self.tool_clearance_status == 'unreturned':
            raise UserError(_(
                "Tool Clearance Hard-Stop:\n\n"
                "Settlement release blocked. Operative '%s' has %d unreturned tool(s) in custody."
            ) % (self.employee_id.name, self.unreturned_tool_count))

        # 1. Post Accounting Entry if payout > 0
        move = False
        if self.net_settlement_amount > 0:
            misc_journal = self.env['account.journal'].search([
                ('type', '=', 'general'),
                ('company_id', '=', self.company_id.id),
            ], limit=1)
            expense_acc = self.company_id.default_expense_account_id or self.env['account.account'].search([
                ('internal_group', '=', 'expense'),
                ('company_ids', 'in', self.company_id.ids),
            ], limit=1)
            payable_acc = self.company_id.default_payroll_payable_account_id or self.env['account.account'].search([
                ('account_type', 'in', ['liability_current', 'payable']),
                ('company_ids', 'in', self.company_id.ids),
            ], limit=1) or expense_acc

            move = self.env['account.move'].create({
                'journal_id': misc_journal.id if misc_journal else False,
                'date': self.last_working_day,
                'ref': f"Exit Settlement {self.name} - {self.employee_id.name}",
                'move_type': 'entry',
                'line_ids': [
                    (0, 0, {
                        'name': f"Final EOSB Settlement: {self.employee_id.name}",
                        'account_id': expense_acc.id if expense_acc else False,
                        'debit': self.net_settlement_amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': f"Settlement Payable: {self.employee_id.name}",
                        'account_id': payable_acc.id if payable_acc else False,
                        'debit': 0.0,
                        'credit': self.net_settlement_amount,
                    }),
                ],
            })
            move.action_post()

        # 2. Mark recovered employee advances as deducted at exit
        if 'contractor.employee.advance' in self.env and self.employee_advances_loans > 0:
            open_advances = self.env['contractor.employee.advance'].sudo().search([
                ('employee_id', '=', self.employee_id.id),
                ('state', 'in', ['approved', 'disbursed']),
            ])
            for adv in open_advances:
                adv.write({
                    'recovered_amount': adv.advance_amount,
                    'state': 'deducted_at_exit',
                })

        # 3. Automatically revoke ERP user access
        if self.employee_id.user_id:
            self.employee_id.user_id.sudo().write({
                'active': False,
            })

        # 4. Archive employee record
        self.employee_id.with_context(skip_tool_clearance_check=True).write({
            'active': False,
        })

        self.write({
            'state': 'released',
            'manager_id': self.env.user.id,
            'account_move_id': move.id if move else False,
        })

        self.message_post(
            body=Markup(_(
                "<strong>Final Settlement Released & Disbursed:</strong> Paid %s %.2f.<br/>"
                "<strong>System Access Revoked:</strong> User login deactivated and employee record archived."
            )) % (self.currency_id.symbol or '', self.net_settlement_amount),
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
            raise UserError(_("Released Employee Exit Settlements are locked and cannot be modified."))
        return super().write(vals)

