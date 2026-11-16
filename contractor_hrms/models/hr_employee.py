# -*- coding: utf-8 -*-
from datetime import timedelta
from dateutil.relativedelta import relativedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class HrEmployee(models.Model):
    _inherit = ['hr.employee', 'contractor.no.delete.mixin', 'contractor.reason.mixin']
    _name = 'hr.employee'

    workforce_category = fields.Selection([
        ('permanent', 'Permanent / In-House Staff'),
        ('temporary', 'Temporary / Daily Wage Worker'),
        ('manpower_supply', 'Manpower Supply Agency Worker'),
    ], string='Workforce Classification', default='permanent', required=True, tracking=True,
       help='Governing category for remuneration, site attendance rates, tool issuance, and leave/EOSB entitlement.')

    pending_reclassification_category = fields.Selection([
        ('permanent', 'Permanent / In-House Staff'),
        ('temporary', 'Temporary / Daily Wage Worker'),
        ('manpower_supply', 'Manpower Supply Agency Worker'),
    ], string='Target Category for Reclassification', tracking=True,
       help='Select target category before clicking Reclassify Workforce Category.')

    # Manpower Supply Agency Linkage
    agency_partner_id = fields.Many2one(
        'res.partner',
        string='Manpower Supply Agency',
        domain="[('is_company', '=', True)]",
        tracking=True,
    )
    agency_agreement_id = fields.Many2one(
        'contractor.agency.agreement',
        string='Supply Agreement SLA',
        tracking=True,
    )

    # Remuneration & Wage Basis
    monthly_basic_salary = fields.Monetary(
        string='Monthly Basic Salary',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
        help='Basic salary utilized for EOSB calculations and leave salary accruals.',
    )
    monthly_total_salary = fields.Monetary(
        string='Monthly Gross Salary',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
        help='Gross compensation (Basic + Fixed Allowances) for permanent employees.',
    )
    daily_wage = fields.Monetary(
        string='Agreed Daily Wage',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
        help='Daily wage rate for temporary workers based on standard daily hours.',
    )
    agency_billing_rate = fields.Monetary(
        string='Agency Contract Rate',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
        help='Agreed hourly or daily billing rate payable to the manpower supplier.',
    )

    # Dual Effective Rates
    system_calculated_hourly_rate = fields.Monetary(
        string='System Calculated Hourly Rate',
        currency_field='currency_id',
        compute='_compute_hourly_rates',
        store=True,
        help='Derived baseline hourly rate based on company standard hours scale.',
    )
    manual_override_hourly_rate = fields.Monetary(
        string='Manual Override Hourly Rate',
        currency_field='currency_id',
        tracking=True,
        help='Approved management override rate for specialized skills or site premiums.',
    )
    manual_override_approved = fields.Boolean(
        string='Override Approved by Manager',
        default=False,
        tracking=True,
        copy=False,
    )
    manual_override_reason = fields.Text(
        string='Override Justification',
        copy=False,
    )
    manual_override_approver_id = fields.Many2one(
        'res.users',
        string='Override Authorized By',
        readonly=True,
        copy=False,
    )
    manual_override_date = fields.Datetime(
        string='Override Approval Date',
        readonly=True,
        copy=False,
    )
    effective_hourly_rate = fields.Monetary(
        string='Effective Hourly Labor Rate',
        currency_field='currency_id',
        compute='_compute_hourly_rates',
        store=True,
        help='Final rate utilized for project labor costing (Override if approved, else System rate).',
    )

    # Tool Custody Register Linkage
    assigned_tool_ids = fields.One2many(
        'contractor.tool.asset',
        'current_employee_id',
        string='Assigned Tools in Custody',
    )
    assigned_tool_count = fields.Integer(
        string='Tools Currently in Custody',
        compute='_compute_assigned_tools',
    )
    pending_tool_deduction_amount = fields.Monetary(
        string='Outstanding Tool Deductions',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
        help='Accumulated receivables for lost or damaged tools pending payroll recovery.',
    )

    # Air Ticket Entitlement
    ticket_frequency = fields.Selection([
        ('annual', 'Annual (Every 1 Year)'),
        ('biennial', 'Biennial (Every 2 Years)'),
        ('none', 'Not Entitled'),
    ], string='Air Ticket Frequency', default='annual', tracking=True)

    ticket_route_class = fields.Char(
        string='Ticket Route / Entitlement',
        default='Economy Class / Home Country Return',
        tracking=True,
    )
    ticket_allowance_amount = fields.Monetary(
        string='Ticket Allowance Value',
        currency_field='currency_id',
        default=1500.0,
        tracking=True,
    )
    last_ticket_date = fields.Date(
        string='Last Ticket Disbursed Date',
        tracking=True,
    )
    next_ticket_due_date = fields.Date(
        string='Next Ticket Due Date',
        compute='_compute_ticket_due',
        store=True,
    )
    ticket_due_flag = fields.Boolean(
        string='Air Ticket Due',
        compute='_compute_ticket_due',
        store=True,
    )

    @api.constrains('workforce_category', 'agency_partner_id', 'agency_agreement_id')
    def _check_manpower_agency(self):
        for emp in self:
            if emp.workforce_category == 'manpower_supply':
                if not emp.agency_partner_id:
                    raise ValidationError(_("Manpower Supply Agency is mandatory for workers classified under 'Manpower Supply Agency Worker'."))
                if not emp.agency_agreement_id:
                    raise ValidationError(_("Supply Agreement SLA is mandatory for Manpower Supply Agency workers."))

    @api.depends(
        'workforce_category',
        'monthly_total_salary',
        'daily_wage',
        'agency_billing_rate',
        'agency_agreement_id.agreed_rate',
        'agency_agreement_id.rate_type',
        'manual_override_hourly_rate',
        'manual_override_approved',
        'company_id.standard_monthly_hours',
        'company_id.standard_daily_hours',
    )
    def _compute_hourly_rates(self):
        for emp in self:
            company = emp.company_id or self.env.company
            std_monthly = company.standard_monthly_hours or 208.0
            std_daily = company.standard_daily_hours or 8.0

            sys_rate = 0.0
            if emp.workforce_category == 'permanent':
                sys_rate = (emp.monthly_total_salary or 0.0) / std_monthly if std_monthly > 0 else 0.0
            elif emp.workforce_category == 'temporary':
                sys_rate = (emp.daily_wage or 0.0) / std_daily if std_daily > 0 else 0.0
            elif emp.workforce_category == 'manpower_supply':
                agreement = emp.agency_agreement_id
                rate = agreement.agreed_rate if agreement else emp.agency_billing_rate
                if agreement and agreement.rate_type == 'hourly':
                    sys_rate = rate
                else:
                    sys_rate = rate / std_daily if std_daily > 0 else 0.0

            emp.system_calculated_hourly_rate = sys_rate

            if emp.manual_override_approved and emp.manual_override_hourly_rate > 0.0:
                emp.effective_hourly_rate = emp.manual_override_hourly_rate
            else:
                emp.effective_hourly_rate = sys_rate

    @api.depends('assigned_tool_ids.status')
    def _compute_assigned_tools(self):
        for emp in self:
            active_tools = emp.assigned_tool_ids.filtered(lambda t: t.status == 'assigned')
            emp.assigned_tool_count = len(active_tools)

    @api.depends('ticket_frequency', 'last_ticket_date', 'create_date')
    def _compute_ticket_due(self):
        today = fields.Date.context_today(self)
        for emp in self:
            if emp.ticket_frequency == 'none':
                emp.next_ticket_due_date = False
                emp.ticket_due_flag = False
                continue

            ref_date = emp.last_ticket_date or (emp.create_date.date() if emp.create_date else today)
            years = 1 if emp.ticket_frequency == 'annual' else 2
            next_date = ref_date + relativedelta(years=years)
            emp.next_ticket_due_date = next_date
            emp.ticket_due_flag = next_date <= today

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'workforce_category' in vals and 'worker_category' not in vals:
                vals['worker_category'] = vals['workforce_category']
            elif 'worker_category' in vals and 'workforce_category' not in vals:
                vals['workforce_category'] = vals['worker_category']
        records = super().create(vals_list)
        for emp in records:
            # Create baseline dated rate history
            emp._record_dated_rate_history(
                reason=_("Initial employee recruitment rate scale."),
                effective_date=fields.Date.context_today(emp),
            )
        return records

    def write(self, vals):
        if 'workforce_category' in vals and 'worker_category' not in vals:
            vals['worker_category'] = vals['workforce_category']
        elif 'worker_category' in vals and 'workforce_category' not in vals:
            vals['workforce_category'] = vals['worker_category']

        if 'active' in vals and not vals['active'] and not self.env.su and not self.env.context.get('skip_tool_clearance_check'):
            for emp in self:
                if emp.assigned_tool_count > 0:
                    raise UserError(_(
                        "Tool Custody Clearance Hard-Stop:\n\n"
                        "Cannot archive employee '%s' while %d tool(s) remain assigned in custody.\n"
                        "Return all tools or process a Manager-approved exit settlement override first."
                    ) % (emp.name, emp.assigned_tool_count))

        if ('workforce_category' in vals or 'worker_category' in vals) and not self.env.su and not self.env.context.get('allow_reclassification'):
            if not self.env.user.has_group('contractor_core.group_contractor_manager'):
                raise UserError(_(
                    "Workforce Category Locked:\n\n"
                    "Workforce classification is fixed upon employee creation. "
                    "Only a Contractor Manager may reclassify via 'Reclassify Workforce Category'."
                ))

        res = super().write(vals)
        rate_altering_keys = {
            'monthly_total_salary',
            'monthly_basic_salary',
            'daily_wage',
            'agency_billing_rate',
            'manual_override_hourly_rate',
            'manual_override_approved',
            'workforce_category',
        }
        if set(vals.keys()) & rate_altering_keys:
            for emp in self:
                emp._record_dated_rate_history(
                    reason=vals.get('manual_override_reason') or _("Wage rate or workforce category modification."),
                    effective_date=fields.Date.context_today(emp),
                )
        return res

    def _record_dated_rate_history(self, reason, effective_date=None):
        """Creates or updates effective-dated rate history without same-day overlap errors."""
        self.ensure_one()
        eff_date = effective_date or fields.Date.context_today(self)
        RateHistory = self.env['contractor.dated.rate.history'].sudo()

        # Close out strictly earlier open-ended rate history records
        prev_rates = RateHistory.search([
            ('employee_id', '=', self.id),
            ('effective_to', '=', False),
            ('effective_date', '<', eff_date),
        ])
        if prev_rates:
            prev_rates.write({'effective_to': eff_date - timedelta(days=1)})

        rate_vals = {
            'system_hourly_rate': self.system_calculated_hourly_rate or 0.0,
            'override_hourly_rate': self.manual_override_hourly_rate if self.manual_override_approved else 0.0,
            'reason': reason,
            'approved_by_id': self.env.user.id,
        }
        # Update in-place if a rate history already starts on eff_date (avoids same-day overlap constraint)
        same_day_rate = RateHistory.search([
            ('employee_id', '=', self.id),
            ('effective_date', '=', eff_date),
            ('effective_to', '=', False),
        ], limit=1)
        if same_day_rate:
            same_day_rate.write(rate_vals)
        else:
            RateHistory.create({
                'employee_id': self.id,
                'effective_date': eff_date,
                'classification': 'helper',
                **rate_vals,
            })

    def action_reclassify_worker(self):
        """Manager workflow to reclassify worker category with effective-dated rate history."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can reclassify workforce categories."))

        return {
            'name': _('Reclassify Worker Category'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': 'reclassify',
                'default_callback_method': '_callback_reclassify_worker',
            },
        }

    def _callback_reclassify_worker(self, reason=None):
        clean_reason = (reason or getattr(self, 'last_action_reason', '')).strip()
        if len(clean_reason) < 10:
            raise UserError(_("Documented reclassification justification (minimum 10 characters) is required."))

        if self.pending_reclassification_category and self.pending_reclassification_category != self.workforce_category:
            new_cat = self.pending_reclassification_category
            self.with_context(allow_reclassification=True).write({
                'workforce_category': new_cat,
                'worker_category': new_cat,
                'pending_reclassification_category': False,
            })

        self._record_dated_rate_history(
            reason=_("Workforce Reclassification: %s") % clean_reason,
            effective_date=fields.Date.context_today(self),
        )
        self.message_post(
            body=Markup(_("<strong>Worker Reclassified:</strong> Category: %s | Authorized by %s.<br/>Justification: %s")) % (
                self.workforce_category, self.env.user.name, clean_reason
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_approve_rate_override(self):
        """Authorizes manual hourly rate override."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize rate overrides."))

        if self.manual_override_hourly_rate <= 0:
            raise UserError(_("Manual override hourly rate must be strictly greater than zero."))

        if hasattr(self, 'action_open_reason_wizard'):
            return self.action_open_reason_wizard(
                action_type='override',
                callback_method='_callback_rate_override',
            )

        self.write({
            'manual_override_approved': True,
            'manual_override_approver_id': self.env.user.id,
            'manual_override_date': fields.Datetime.now(),
            'manual_override_reason': _("Authorized by Contractor Manager %s.") % self.env.user.name,
        })
        self._record_dated_rate_history(
            reason=_("Manager Rate Override: %s") % self.manual_override_reason,
            effective_date=fields.Date.context_today(self),
        )

    def _callback_rate_override(self, reason=None):
        self.write({
            'manual_override_approved': True,
            'manual_override_reason': reason or getattr(self, 'last_action_reason', _('Authorized by Manager.')),
            'manual_override_approver_id': self.env.user.id,
            'manual_override_date': fields.Datetime.now(),
        })
        self._record_dated_rate_history(
            reason=_("Rate Override Approved: %s") % self.manual_override_reason,
            effective_date=fields.Date.context_today(self),
        )
        self.message_post(
            body=Markup(_("<strong>Hourly Rate Override Approved:</strong> Rate set to %s %.2f by %s.<br/>Reason: %s")) % (
                self.currency_id.symbol or '', self.manual_override_hourly_rate, self.env.user.name, self.manual_override_reason
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    @api.model
    def _cron_check_ticket_eligibility(self):
        """Daily cron checking employees eligible for air ticket allowance."""
        today = fields.Date.context_today(self)
        due_employees = self.search([
            ('workforce_category', '=', 'permanent'),
            ('ticket_frequency', 'in', ['annual', 'biennial']),
            ('next_ticket_due_date', '<=', today),
        ])

        group_hr = self.env.ref('contractor_core.group_contractor_hr_officer', raise_if_not_found=False)
        hr_users = (group_hr.all_user_ids | group_hr.user_ids).filtered(lambda u: u.active) if group_hr else self.env['res.users']
        act_type_id = self.env.ref('mail.mail_activity_data_todo').id
        model_id = self.env['ir.model']._get_id('hr.employee')
        for emp in due_employees:
            emp.ticket_due_flag = True
            summary = f"Air Ticket Allowance Due: {emp.name}"
            for user in hr_users:
                existing = self.env['mail.activity'].search([
                    ('res_model_id', '=', model_id),
                    ('res_id', '=', emp.id),
                    ('user_id', '=', user.id),
                    ('summary', '=', summary),
                ], limit=1)
                if not existing:
                    self.env['mail.activity'].create({
                        'activity_type_id': act_type_id,
                        'res_model_id': model_id,
                        'res_id': emp.id,
                        'user_id': user.id,
                        'summary': summary,
                        'note': _("Employee %s is due for air ticket disbursement on %s.") % (emp.name, emp.next_ticket_due_date),
                        'date_deadline': today,
                    })

