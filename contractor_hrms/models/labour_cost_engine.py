# -*- coding: utf-8 -*-
from datetime import datetime, time
from markupsafe import Markup
import pytz
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


# =========================================================================
# 1. Central Overtime Day Classification Engine
# =========================================================================
def get_calendar_day_type_and_multiplier(company, target_date, calendar=None):
    """Deterministically resolves day classification and overtime multiplier from calendar rules.
    Day type is strictly system-computed and never user-selectable.
    """
    if not target_date:
        return 'workday', 1.25

    # 1. Check for public holidays / leaves in calendar
    is_holiday = False
    if calendar:
        tz = pytz.timezone(calendar.tz or 'UTC')
        start_dt = tz.localize(datetime.combine(target_date, time.min))
        end_dt = tz.localize(datetime.combine(target_date, time.max))
        leaves = calendar._leave_intervals_batch(start_dt, end_dt)
        if leaves.get(False):
            is_holiday = True

    if is_holiday:
        return 'public_holiday', (company.ot_holiday_multiplier or 2.0)
    elif target_date.weekday() == 4:  # Friday in construction calendar
        return 'friday', (company.ot_friday_multiplier or 1.50)
    else:
        return 'workday', (company.ot_workday_multiplier or 1.25)


# =========================================================================
# 2. Site Attendance & Daily Cost Posting Extensions for Provisional Agency Cost
# =========================================================================
class ContractorSiteAttendanceLineHRMS(models.Model):
    _inherit = 'contractor.site.attendance.line'

    is_provisional_agency_cost = fields.Boolean(
        string='Provisional Agency Cost',
        compute='_compute_provisional_status',
        store=True,
        help='True if worker is supplied by an agency, meaning daily cost is provisional until agency invoice reconciliation.',
    )
    day_type = fields.Selection([
        ('workday', 'Standard Workday (1.25x OT)'),
        ('friday', 'Friday Work (1.50x OT)'),
        ('public_holiday', 'Public Holiday (2.00x OT)'),
    ], string='Calendar Day Type', compute='_compute_day_type', store=True, readonly=True)

    @api.depends('employee_id.workforce_category')
    def _compute_provisional_status(self):
        for line in self:
            line.is_provisional_agency_cost = line.employee_id.workforce_category == 'manpower_supply'

    @api.depends('attendance_date', 'project_id.resource_calendar_id')
    def _compute_day_type(self):
        for line in self:
            company = line.attendance_id.company_id or self.env.company
            calendar = line.project_id.resource_calendar_id
            day_type, _mult = get_calendar_day_type_and_multiplier(company, line.attendance_date, calendar)
            line.day_type = day_type


class ContractorDailyCostPostingLineHRMS(models.Model):
    _inherit = 'contractor.daily.cost.posting.line'

    is_provisional_agency_cost = fields.Boolean(
        string='Provisional Agency Cost',
        default=False,
        help='Indicates this labour accrual is provisional awaiting formal manpower agency reconciliation.',
    )


# =========================================================================
# 3. Documented Labour Cost Adjustment Model
# =========================================================================
class ContractorLabourCostAdjustment(models.Model):
    _name = 'contractor.labour.cost.adjustment'
    _description = 'Contractor Documented Labour Cost Adjustment'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'adjustment_date desc, id desc'

    name = fields.Char(
        string='Adjustment Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New LCA'),
        index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Operative',
        required=True,
        tracking=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Affected Project Site',
        required=True,
        index=True,
        tracking=True,
    )
    adjustment_date = fields.Date(
        string='Adjustment Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
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
    attendance_id = fields.Many2one(
        'contractor.site.attendance',
        string='Source Site Attendance Sheet',
        help='Original site attendance sheet being retroactively adjusted.',
    )
    cost_posting_id = fields.Many2one(
        'contractor.daily.cost.posting',
        string='Source Daily Cost Posting',
    )
    hours_adjustment = fields.Float(
        string='Hours Variance (+/-)',
        default=0.0,
        tracking=True,
        help='Adjustment in shift or overtime hours.',
    )
    rate_adjustment = fields.Monetary(
        string='Hourly Rate Variance (+/-)',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    amount_adjustment = fields.Monetary(
        string='Net Adjustment Cost (+/-)',
        currency_field='currency_id',
        required=True,
        tracking=True,
        help='Net monetary impact to adjust project labour cost.',
    )
    reason = fields.Text(
        string='Mandatory Technical & Audit Justification',
        required=True,
        tracking=True,
        help='Explain why original site entry required adjustment (timecard correction, missing OT approval, trade reclassification).',
    )
    state = fields.Selection([
        ('draft', 'Draft Adjustment'),
        ('submitted', 'Submitted to Manager'),
        ('approved', 'Approved & Budget Adjusted'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    requested_by = fields.Many2one(
        'res.users',
        string='Requested By',
        default=lambda self: self.env.user,
        readonly=True,
    )
    approver_id = fields.Many2one(
        'res.users',
        string='Approved By Manager',
        readonly=True,
        copy=False,
    )
    approval_date = fields.Datetime(
        string='Approval Date',
        readonly=True,
        copy=False,
    )
    account_move_id = fields.Many2one(
        'account.move',
        string='Adjustment Journal Entry',
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New LCA')) == _('New LCA'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.labour.cost.adjustment') or _('New LCA')
        return super().create(vals_list)

    def action_submit(self):
        self.ensure_one()
        clean_reason = (self.reason or '').strip()
        if len(clean_reason) < 15:
            raise UserError(_("Please provide a thorough technical justification for this cost adjustment (minimum 15 characters)."))

        self.state = 'submitted'
        group_mgr = self.env.ref('contractor_core.group_contractor_manager', raise_if_not_found=False)
        if group_mgr:
            for user in (group_mgr.all_user_ids | group_mgr.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('contractor.labour.cost.adjustment'),
                    'res_id': self.id,
                    'user_id': user.id,
                    'summary': f"Approve Labour Cost Adjustment: {self.name}",
                    'note': _("Adjustment of %s %.2f for %s on project %s.") % (
                        self.currency_id.symbol or '', self.amount_adjustment, self.employee_id.name, self.project_id.name
                    ),
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_("<strong>Adjustment Submitted:</strong> Dispatched to Contractor Manager for sign-off.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_approve(self):
        """Authorizes labor cost adjustment, updates budget actuals, and generates accounting entry."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize labour cost adjustments."))

        move = self._create_adjustment_journal_entry()

        self.write({
            'state': 'approved',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
            'account_move_id': move.id if move else False,
        })

        # Update Master Budget
        budget = self.env['contractor.project.budget'].search([
            ('project_id', '=', self.project_id.id),
            ('is_active', '=', True),
        ], limit=1)
        if budget:
            budget.line_ids._compute_line_consumption()

        self.message_post(
            body=Markup(_("<strong>Adjustment Approved:</strong> Signed off by Manager %s.<br/>Net Project Cost Impact: %s %.2f.")) % (
                self.env.user.name, self.currency_id.symbol or '', self.amount_adjustment
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def _create_adjustment_journal_entry(self):
        """Creates an adjustment journal entry to shift costs into or out of project analytic account."""
        if not self.project_id.account_id or abs(self.amount_adjustment) < 0.01:
            return False

        misc_journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not misc_journal:
            return False

        expense_acc = self.company_id.default_expense_account_id or self.env['account.account'].search([
            ('internal_group', '=', 'expense'),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1)
        wip_acc = self.company_id.default_cost_accrual_account_id or self.env['account.account'].search([
            ('account_type', 'in', ['liability_current', 'equity']),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1) or expense_acc

        amt = abs(self.amount_adjustment)
        is_positive = self.amount_adjustment > 0

        aml_fields = self.env['account.move.line']._fields
        am_fields = self.env['account.move']._fields
        expense_line = {
            'name': f"Labour Cost Adj: {self.name} - {self.employee_id.name}",
            'account_id': expense_acc.id,
            'debit': amt if is_positive else 0.0,
            'credit': 0.0 if is_positive else amt,
            'analytic_distribution': {str(self.project_id.account_id.id): 100.0},
        }
        if 'project_id' in aml_fields:
            expense_line['project_id'] = self.project_id.id
        if 'cost_head' in aml_fields:
            expense_line['cost_head'] = 'labour'

        lines = [
            (0, 0, expense_line),
            (0, 0, {
                'name': f"Labour Cost Accrual Adj: {self.name}",
                'account_id': wip_acc.id,
                'debit': 0.0 if is_positive else amt,
                'credit': amt if is_positive else 0.0,
            }),
        ]

        move_vals = {
            'journal_id': misc_journal.id,
            'date': self.adjustment_date,
            'ref': f"LCA {self.name} - {self.project_id.name}",
            'move_type': 'entry',
            'line_ids': lines,
        }
        if 'project_id' in am_fields:
            move_vals['project_id'] = self.project_id.id
        move = self.env['account.move'].create(move_vals)
        move.action_post()
        return move

    def action_decline(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Manager can decline labour cost adjustments."))
        return {
            'name': _('Decline Labour Cost Adjustment'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': 'decline',
                'default_target_state': 'declined',
            },
        }

