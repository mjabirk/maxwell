# -*- coding: utf-8 -*-
from datetime import datetime, time
from markupsafe import Markup
import pytz
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorSiteAttendance(models.Model):
    _name = 'contractor.site.attendance'
    _description = 'Daily Site Attendance & Overtime Sheet'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.site.lock.mixin', 'contractor.no.delete.mixin']
    _order = 'attendance_date desc, id desc'

    name = fields.Char(
        string='Attendance Sheet Number',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Sheet'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project Site',
        required=True,
        index=True,
        tracking=True,
    )
    attendance_date = fields.Date(
        string='Work Date',
        required=True,
        default=fields.Date.context_today,
        index=True,
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
    site_engineer_id = fields.Many2one(
        'res.users',
        string='Recorded By (Site Engineer)',
        default=lambda self: self.env.user,
        tracking=True,
    )
    schedule_id = fields.Many2one(
        'contractor.site.schedule',
        string='Source Schedule',
        readonly=True,
        copy=False,
    )
    state = fields.Selection([
        ('draft', 'Draft Attendance'),
        ('submitted', 'Submitted to PM'),
        ('pm_approved', 'Approved by PM / Costed'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    approver_id = fields.Many2one(
        'res.users',
        string='Approved By',
        readonly=True,
        copy=False,
    )
    approval_date = fields.Datetime(
        string='Approval Date',
        readonly=True,
        copy=False,
    )
    cost_posting_id = fields.Many2one(
        'contractor.daily.cost.posting',
        string='Daily Project Cost Posting',
        readonly=True,
        copy=False,
    )

    line_ids = fields.One2many(
        'contractor.site.attendance.line',
        'attendance_id',
        string='Operative Attendance Records',
        copy=True,
    )

    total_workers = fields.Integer(
        string='Total Workforce Scheduled',
        compute='_compute_totals',
        store=True,
    )
    total_present = fields.Integer(
        string='Operatives Present',
        compute='_compute_totals',
        store=True,
    )
    total_no_shows = fields.Integer(
        string='No-Shows / Absent',
        compute='_compute_totals',
        store=True,
    )
    total_regular_hours = fields.Float(
        string='Total Regular Hours',
        compute='_compute_totals',
        store=True,
    )
    total_overtime_hours = fields.Float(
        string='Total Overtime Hours',
        compute='_compute_totals',
        store=True,
    )
    total_labour_cost = fields.Monetary(
        string='Total Calculated Daily Labour Cost',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )

    _check_uniq_project_attendance_date = models.Constraint(
        'UNIQUE(project_id, attendance_date)',
        'A site attendance record already exists for this Project on this work date!'
    )

    @api.depends(
        'line_ids.attendance_status',
        'line_ids.regular_hours',
        'line_ids.overtime_hours',
        'line_ids.calculated_labour_cost',
    )
    def _compute_totals(self):
        for atn in self:
            atn.total_workers = len(atn.line_ids)
            atn.total_present = len(atn.line_ids.filtered(lambda l: l.attendance_status in ('present', 'unscheduled_addition')))
            atn.total_no_shows = len(atn.line_ids.filtered(lambda l: l.attendance_status == 'no_show'))
            atn.total_regular_hours = sum(atn.line_ids.mapped('regular_hours'))
            atn.total_overtime_hours = sum(atn.line_ids.mapped('overtime_hours'))
            atn.total_labour_cost = sum(atn.line_ids.mapped('calculated_labour_cost'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Sheet')) == _('New Sheet'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.site.attendance') or _('New Sheet')
        return super().create(vals_list)

    def action_submit(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Please add operative attendance lines before submitting."))

        self.line_ids.filtered(lambda l: l.overtime_hours > 0).write({'se_ot_verified': True})
        total_ot = sum(self.line_ids.mapped('overtime_hours'))
        self.state = 'submitted'

        pm_user = self.project_id.user_id
        if pm_user:
            self.env['mail.activity'].create({
                'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                'res_model_id': self.env['ir.model']._get_id('contractor.site.attendance'),
                'res_id': self.id,
                'user_id': pm_user.id,
                'summary': f"Approve Site Attendance & OT: {self.name} for {self.project_id.name}",
                'note': _("Attendance for %s (%d operatives present, %.1fh OT, %s %.2f labour cost).") % (
                    self.attendance_date, self.total_present, total_ot, self.currency_id.symbol or '', self.total_labour_cost
                ),
                'date_deadline': fields.Date.context_today(self),
            })

        self.message_post(
            body=Markup(_(
                "<strong>Site Attendance Submitted:</strong> Site Engineer verified %.1f OT hours. Sent to Project Manager for approval."
            )) % total_ot,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_pm_approve(self):
        """Authorizes site attendance, approves overtime, and triggers daily project cost posting."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_project_manager'):
            raise UserError(_("Access Denied: Only a Contractor Project Manager can approve site attendance sheets."))

        # Mark OT approved on present lines with OT
        for line in self.line_ids:
            if line.overtime_hours > 0 and line.attendance_status in ('present', 'unscheduled_addition'):
                line.ot_approved = True

        self.write({
            'state': 'pm_approved',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })

        # Auto-trigger / sync Daily Cost Posting
        posting = self.env['contractor.daily.cost.posting'].get_or_create_daily_posting(
            project_id=self.project_id.id,
            cost_date=self.attendance_date,
        )
        self.cost_posting_id = posting.id

        self.message_post(
            body=Markup(_("<strong>Site Attendance Approved:</strong> Verified by Project Manager %s. Synced with Daily Cost Posting <a href='#' data-oe-model='contractor.daily.cost.posting' data-oe-id='%s'>%s</a>.")) % (
                self.env.user.name, posting.id, posting.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_decline(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_project_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Project Manager can decline site attendance sheets."))
        return {
            'name': _('Decline Attendance Sheet'),
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

    def action_mark_all_present(self):
        """One-tap bulk mark all non-leave operatives on this sheet as Present."""
        self.ensure_one()
        if self.state in ('pm_approved', 'declined'):
            raise UserError(_("Cannot modify an approved or declined attendance sheet."))
        for line in self.line_ids:
            if line.attendance_status != 'on_leave':
                line.write({
                    'attendance_status': 'present',
                    'regular_hours': line.regular_hours or line.scheduled_hours or 8.0,
                })
        return True

    def action_verify_all_ot(self):
        """One-tap bulk OT verification for all present operatives logging overtime."""
        self.ensure_one()
        if self.state in ('pm_approved', 'declined'):
            raise UserError(_("Cannot modify an approved or declined attendance sheet."))
        for line in self.line_ids:
            if line.attendance_status in ('present', 'unscheduled_addition') and line.overtime_hours > 0:
                line.se_ot_verified = True
        return True


class ContractorSiteAttendanceLine(models.Model):
    _name = 'contractor.site.attendance.line'
    _description = 'Site Attendance Operative Record'
    _order = 'employee_id, id'

    attendance_id = fields.Many2one(
        'contractor.site.attendance',
        string='Attendance Sheet',
        required=True,
        ondelete='cascade',
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        related='attendance_id.project_id',
        store=True,
        readonly=True,
    )
    attendance_date = fields.Date(
        string='Work Date',
        related='attendance_id.attendance_date',
        store=True,
        readonly=True,
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Operative',
        required=True,
        domain="[('active', '=', True)]",
    )
    worker_category = fields.Selection(
        related='employee_id.worker_category',
        string='Category',
        store=True,
        readonly=True,
    )
    job_title_id = fields.Many2one(
        'hr.job',
        related='employee_id.job_id',
        string='Designation / Trade',
        store=True,
        readonly=True,
    )
    scheduled_hours = fields.Float(
        string='Scheduled',
        default=8.0,
    )
    attendance_status = fields.Selection([
        ('present', 'Present on Site'),
        ('no_show', 'No-Show / Absent'),
        ('unscheduled_addition', 'Unscheduled Addition'),
        ('on_leave', 'Approved Leave'),
    ], string='Status', default='present', required=True)

    regular_hours = fields.Float(
        string='Regular Hours',
        default=8.0,
    )
    overtime_hours = fields.Float(
        string='Overtime Hours',
        default=0.0,
    )
    ot_reason = fields.Char(
        string='Overtime Task / Justification',
    )
    se_ot_verified = fields.Boolean(
        string='SE Verified OT',
        default=True,
    )
    ot_approved = fields.Boolean(
        string='OT Approved',
        default=False,
    )
    hourly_rate = fields.Monetary(
        string='Hourly Rate',
        currency_field='currency_id',
        compute='_compute_rates',
        store=True,
        readonly=False,
    )
    ot_multiplier = fields.Float(
        string='OT Multiplier',
        compute='_compute_rates',
        store=True,
        help='System-computed multiplier based on workday, Friday, or public holiday.',
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='attendance_id.currency_id',
        readonly=True,
    )
    regular_cost = fields.Monetary(
        string='Regular Cost',
        currency_field='currency_id',
        compute='_compute_costs',
        store=True,
    )
    ot_cost = fields.Monetary(
        string='OT Cost',
        currency_field='currency_id',
        compute='_compute_costs',
        store=True,
    )
    calculated_labour_cost = fields.Monetary(
        string='Total Daily Cost',
        currency_field='currency_id',
        compute='_compute_costs',
        store=True,
    )
    work_zone = fields.Char(
        string='Zone / Floor',
    )

    @api.onchange('attendance_status')
    def _onchange_status(self):
        if self.attendance_status in ('no_show', 'on_leave'):
            self.regular_hours = 0.0
            self.overtime_hours = 0.0
        elif self.attendance_status in ('present', 'unscheduled_addition') and not self.regular_hours:
            self.regular_hours = self.scheduled_hours or 8.0

    @api.depends('employee_id', 'attendance_date')
    def _compute_rates(self):
        for line in self:
            if not line.employee_id:
                line.hourly_rate = 0.0
                line.ot_multiplier = 1.25
                continue

            target_date = line.attendance_date or fields.Date.context_today(line)
            # 1. Fetch hourly rate from dated rate history or employee
            rate = 0.0
            if hasattr(line.employee_id, 'get_employee_rate_on_date'):
                rate = line.employee_id.get_employee_rate_on_date(target_date)
            elif hasattr(line.employee_id, 'hourly_cost'):
                rate = line.employee_id.hourly_cost or 0.0
            line.hourly_rate = rate

            # 2. Determine OT Multiplier
            company = line.attendance_id.company_id or self.env.company
            ot_workday = company.ot_workday_multiplier or 1.25
            ot_friday = company.ot_friday_multiplier or 1.50
            ot_holiday = company.ot_holiday_multiplier or 2.0

            # Check for holiday in calendar
            calendar = line.project_id.resource_calendar_id
            is_holiday = False
            if calendar:
                tz = pytz.timezone(calendar.tz or 'UTC')
                start_dt = tz.localize(datetime.combine(target_date, time.min))
                end_dt = tz.localize(datetime.combine(target_date, time.max))
                # Check leaves in calendar
                leaves = calendar._leave_intervals_batch(start_dt, end_dt)
                if leaves.get(False):
                    is_holiday = True

            if is_holiday:
                line.ot_multiplier = ot_holiday
            elif target_date.weekday() == 4:  # Friday
                line.ot_multiplier = ot_friday
            else:
                line.ot_multiplier = ot_workday

    @api.depends('regular_hours', 'overtime_hours', 'hourly_rate', 'ot_multiplier', 'ot_approved', 'attendance_status')
    def _compute_costs(self):
        for line in self:
            if line.attendance_status not in ('present', 'unscheduled_addition'):
                line.regular_cost = 0.0
                line.ot_cost = 0.0
                line.calculated_labour_cost = 0.0
                continue

            rate = line.hourly_rate or 0.0
            line.regular_cost = (line.regular_hours or 0.0) * rate
            mult = line.ot_multiplier or 1.25
            line.ot_cost = (line.overtime_hours or 0.0) * rate * mult
            line.calculated_labour_cost = line.regular_cost + line.ot_cost

    def action_toggle_attendance_status(self):
        """One-tap toggle between Present and No-Show / Absent from Kanban card."""
        for line in self:
            if line.attendance_id.state in ('pm_approved', 'declined'):
                raise UserError(_("Cannot modify attendance after sheet is approved or declined."))
            if line.attendance_status in ('present', 'unscheduled_addition'):
                line.write({
                    'attendance_status': 'no_show',
                    'regular_hours': 0.0,
                    'overtime_hours': 0.0,
                })
            else:
                line.write({
                    'attendance_status': 'present',
                    'regular_hours': line.scheduled_hours or 8.0,
                })
        return True

    def action_toggle_ot_verified(self):
        """One-tap toggle for Site Engineer OT verification on mobile Kanban card."""
        for line in self:
            if line.attendance_id.state in ('pm_approved', 'declined'):
                raise UserError(_("Cannot modify OT verification after sheet is approved or declined."))
            line.se_ot_verified = not line.se_ot_verified
        return True

