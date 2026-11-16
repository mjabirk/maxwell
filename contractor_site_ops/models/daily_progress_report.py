# -*- coding: utf-8 -*-
from datetime import datetime, time, timedelta
from markupsafe import Markup
import pytz
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class ContractorDailyProgressReport(models.Model):
    _name = 'contractor.daily.progress.report'
    _description = 'Contractor Daily Progress Report (DPR)'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.site.lock.mixin', 'contractor.no.delete.mixin']
    _order = 'report_date desc, id desc'

    name = fields.Char(
        string='DPR Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New DPR'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        required=True,
        index=True,
        tracking=True,
    )
    report_date = fields.Date(
        string='Reporting Date',
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
    site_engineer_id = fields.Many2one(
        'res.users',
        string='Site Engineer',
        default=lambda self: self.env.user,
        tracking=True,
    )
    pm_id = fields.Many2one(
        'res.users',
        string='Project Manager',
        related='project_id.user_id',
        store=True,
        readonly=True,
    )
    cost_head = fields.Selection([
        ('civil', 'Civil & Structural'),
        ('mep', 'MEP (Mechanical, Electrical, Plumbing)'),
        ('finishing', 'Architectural & Finishing'),
        ('external', 'External & Infrastructure'),
        ('general', 'General Site Works'),
    ], string='Primary Cost Head', default='civil', required=True, tracking=True)

    work_package = fields.Char(
        string='Work Package / Structural Zone',
        required=True,
        tracking=True,
    )
    weather_conditions = fields.Selection([
        ('clear_sunny', 'Clear / Sunny'),
        ('overcast', 'Overcast / Cloudy'),
        ('rainy', 'Rain / Wet Site'),
        ('stormy', 'High Winds / Sandstorm'),
        ('extreme_heat', 'Extreme Heat (Midday Suspension)'),
    ], string='Weather Conditions', default='clear_sunny', required=True, tracking=True)

    schedule_deviation = fields.Selection([
        ('ahead', 'Ahead of Schedule'),
        ('on_schedule', 'On Schedule'),
        ('minor_delay', 'Minor Deviation (<2 Days)'),
        ('critical_delay', 'Critical Delay (>2 Days)'),
    ], string='Schedule Variance Status', default='on_schedule', required=True, tracking=True)

    work_done_summary = fields.Text(
        string='Daily Output & Works Executed',
        required=True,
        tracking=True,
        help='Detailed description of tasks completed today with structural gridlines, quantities, and milestones.',
    )
    planned_work_tomorrow = fields.Text(
        string='Planned Works for Tomorrow',
        tracking=True,
    )

    # Missing Prior Day Continuity Governance
    missing_prior_dpr_override = fields.Boolean(
        string='Missing Prior DPR Override Approved',
        default=False,
        tracking=True,
        copy=False,
    )
    missing_prior_dpr_reason = fields.Text(
        string='Prior DPR Gap Justification',
        copy=False,
        tracking=True,
    )
    missing_prior_dpr_approver_id = fields.Many2one(
        'res.users',
        string='Gap Override Approved By',
        readonly=True,
        copy=False,
    )
    missing_prior_dpr_approval_date = fields.Datetime(
        string='Gap Override Approval Date',
        readonly=True,
        copy=False,
    )

    state = fields.Selection([
        ('draft', 'Draft DPR'),
        ('submitted', 'Submitted to PM'),
        ('pm_approved', 'Approved by PM'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    approval_date = fields.Datetime(
        string='PM Approval Datetime',
        readonly=True,
        copy=False,
    )
    submitted_datetime = fields.Datetime(
        string='Submitted Timestamp',
        readonly=True,
        copy=False,
        tracking=True,
    )
    submission_deadline = fields.Datetime(
        string='Next-Day Cutoff Deadline',
        compute='_compute_submission_timeliness',
        store=True,
    )
    is_late_submission = fields.Boolean(
        string='Submitted After Next-Day Cutoff',
        compute='_compute_submission_timeliness',
        store=True,
        help='True if submitted or logged past the company DPR next-day cutoff hour (e.g. 09:00 AM).',
    )

    issue_ids = fields.One2many(
        'contractor.site.issue',
        'dpr_id',
        string='Site Issues Logged Today',
    )
    issue_count = fields.Integer(
        string='Issues Count',
        compute='_compute_counts',
    )

    _check_uniq_project_report_date = models.Constraint(
        'UNIQUE(project_id, report_date)',
        'A Daily Progress Report (DPR) already exists for this Project on this date!'
    )

    @api.depends('issue_ids')
    def _compute_counts(self):
        for dpr in self:
            dpr.issue_count = len(dpr.issue_ids)

    @api.depends('report_date', 'submitted_datetime', 'create_date', 'company_id.dpr_cutoff_hour')
    def _compute_submission_timeliness(self):
        for dpr in self:
            if not dpr.report_date:
                dpr.submission_deadline = False
                dpr.is_late_submission = False
                continue
            cutoff_hour = (dpr.company_id or self.env.company).dpr_cutoff_hour or 9.0
            hour_int = int(cutoff_hour) % 24
            minute_int = min(59, max(0, int(round((cutoff_hour % 1) * 60))))
            next_day = dpr.report_date + timedelta(days=1)
            deadline = datetime.combine(next_day, time(hour=hour_int, minute=minute_int))
            dpr.submission_deadline = deadline
            check_dt = dpr.submitted_datetime or dpr.create_date
            dpr.is_late_submission = bool(check_dt and check_dt > deadline)

    def _get_previous_working_day(self, target_date, project):
        """Calculates the previous working day based on project calendar or standard working days."""
        calendar = project.resource_calendar_id
        tz = pytz.timezone(calendar.tz or 'UTC') if calendar else pytz.UTC

        curr = target_date - timedelta(days=1)
        for _i in range(14):  # Look back up to 14 days
            if calendar:
                start_dt = tz.localize(datetime.combine(curr, time.min))
                end_dt = tz.localize(datetime.combine(curr, time.max))
                intervals = calendar._work_intervals_batch(start_dt, end_dt)
                if intervals.get(False):
                    return curr
            else:
                # Default Gulf / standard construction week (e.g. Friday off)
                if curr.weekday() != 4:  # 4 is Friday
                    return curr
            curr -= timedelta(days=1)
        return target_date - timedelta(days=1)

    @api.constrains('project_id', 'report_date', 'missing_prior_dpr_override')
    def _check_prior_day_continuity(self):
        """Hard-Stop: Blocks creation if previous working day DPR is missing without PM override."""
        for dpr in self:
            if not dpr.project_id or not dpr.report_date:
                continue

            # Skip continuity check for first DPR of a brand new project
            has_any_earlier_dpr = self.search_count([
                ('project_id', '=', dpr.project_id.id),
                ('report_date', '<', dpr.report_date),
            ])
            if not has_any_earlier_dpr:
                continue

            prev_working_day = dpr._get_previous_working_day(dpr.report_date, dpr.project_id)
            prior_dpr = self.search([
                ('project_id', '=', dpr.project_id.id),
                ('report_date', '=', prev_working_day),
                ('state', 'in', ['submitted', 'pm_approved']),
            ], limit=1)

            if not prior_dpr and not dpr.missing_prior_dpr_override:
                raise ValidationError(_(
                    "DPR Continuity Governance Hard-Stop:\n\n"
                    "No submitted or approved DPR exists for Project '%s' on previous working day (%s).\n\n"
                    "Daily Progress Reports must maintain an unbroken daily operational chain.\n"
                    "Creation is blocked unless a 'Missing Prior DPR Override' is authorized by a Contractor Project Manager."
                ) % (dpr.project_id.name, prev_working_day))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New DPR')) == _('New DPR'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.daily.progress.report') or _('New DPR')
        return super().create(vals_list)

    def action_submit(self):
        self.ensure_one()
        clean_summary = (self.work_done_summary or '').strip()
        if len(clean_summary) < 15:
            raise UserError(_("Please provide a thorough work executed summary (minimum 15 characters)."))

        # Re-check prior day continuity on submit
        self._check_prior_day_continuity()

        now = fields.Datetime.now()
        self.write({
            'state': 'submitted',
            'submitted_datetime': self.submitted_datetime or now,
        })

        # Notify PM
        pm_user = self.pm_id or self.project_id.user_id
        if pm_user:
            self.env['mail.activity'].create({
                'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                'res_model_id': self.env['ir.model']._get_id('contractor.daily.progress.report'),
                'res_id': self.id,
                'user_id': pm_user.id,
                'summary': f"Review & Approve DPR {self.name} for {self.project_id.name}",
                'note': _("DPR submitted for date %s (%s). Work package: %s.") % (
                    self.report_date, self.cost_head, self.work_package
                ),
                'date_deadline': fields.Date.context_today(self),
            })

        self.message_post(
            body=Markup(_("<strong>DPR Submitted:</strong> Dispatched to Project Manager for verification.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_pm_approve(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_project_manager'):
            raise UserError(_("Access Denied: Only a Contractor Project Manager can approve DPRs."))

        self.write({
            'state': 'pm_approved',
            'approval_date': fields.Datetime.now(),
        })

        self.message_post(
            body=Markup(_("<strong>DPR Approved:</strong> Verified and signed off by Project Manager %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_override_missing_prior(self):
        """Allows Project Manager to grant an explicit override for a gap in DPR continuity."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_project_manager'):
            raise UserError(_("Access Denied: Only a Contractor Project Manager can authorize a prior-day DPR gap override."))

        return {
            'name': _('Authorize DPR Continuity Gap Override'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': 'override',
                'default_callback_method': '_callback_missing_prior_override',
            },
        }

    def _callback_missing_prior_override(self, reason=None):
        self.write({
            'missing_prior_dpr_override': True,
            'missing_prior_dpr_reason': reason or getattr(self, 'last_action_reason', _('Authorized by Project Manager.')),
            'missing_prior_dpr_approver_id': self.env.user.id,
            'missing_prior_dpr_approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Missing Prior Day DPR Override Granted:</strong> Authorized by %s.<br/>Reason: %s")) % (
                self.env.user.name, self.missing_prior_dpr_reason
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    # -------------------------------------------------------------------------
    # UX Cockpit: Unified Daily Site Snapshot (Attendance, Consumption, Work Logs)
    # -------------------------------------------------------------------------
    attendance_sheet_ids = fields.Many2many(
        'contractor.site.attendance',
        string='Daily Attendance Sheets',
        compute='_compute_cockpit_records',
    )
    attendance_line_ids = fields.Many2many(
        'contractor.site.attendance.line',
        string='Daily Operative Attendance',
        compute='_compute_cockpit_records',
    )
    attendance_count = fields.Integer(
        string='Attendance Count',
        compute='_compute_cockpit_records',
    )
    consumption_sheet_ids = fields.Many2many(
        'contractor.site.consumption',
        string='Daily Material Consumption',
        compute='_compute_cockpit_records',
    )
    consumption_count = fields.Integer(
        string='Consumption Count',
        compute='_compute_cockpit_records',
    )
    work_log_ids = fields.Many2many(
        'contractor.site.work.log',
        string='Daily Work Output Logs',
        compute='_compute_cockpit_records',
    )
    work_log_count = fields.Integer(
        string='Work Log Count',
        compute='_compute_cockpit_records',
    )

    @api.depends('project_id', 'report_date')
    def _compute_cockpit_records(self):
        Attendance = self.env['contractor.site.attendance']
        Consumption = self.env['contractor.site.consumption']
        WorkLog = self.env['contractor.site.work.log']
        for dpr in self:
            if not dpr.project_id or not dpr.report_date:
                dpr.attendance_sheet_ids = Attendance
                dpr.attendance_line_ids = self.env['contractor.site.attendance.line']
                dpr.attendance_count = 0
                dpr.consumption_sheet_ids = Consumption
                dpr.consumption_count = 0
                dpr.work_log_ids = WorkLog
                dpr.work_log_count = 0
                continue
            att_sheets = Attendance.search([
                ('project_id', '=', dpr.project_id.id),
                ('attendance_date', '=', dpr.report_date),
            ])
            cons_sheets = Consumption.search([
                ('project_id', '=', dpr.project_id.id),
                ('consumption_date', '=', dpr.report_date),
            ])
            work_logs = WorkLog.search([
                ('project_id', '=', dpr.project_id.id),
                ('work_date', '=', dpr.report_date),
            ])
            dpr.attendance_sheet_ids = att_sheets
            dpr.attendance_line_ids = att_sheets.mapped('line_ids')
            dpr.attendance_count = len(att_sheets)
            dpr.consumption_sheet_ids = cons_sheets
            dpr.consumption_count = len(cons_sheets)
            dpr.work_log_ids = work_logs
            dpr.work_log_count = len(work_logs)

    def action_open_daily_attendance(self):
        self.ensure_one()
        existing = self.env['contractor.site.attendance'].search([
            ('project_id', '=', self.project_id.id),
            ('attendance_date', '=', self.report_date),
        ], limit=1)
        action = {
            'name': _('Daily Site Attendance'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.site.attendance',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_project_id': self.project_id.id,
                'default_attendance_date': self.report_date,
            },
        }
        if existing:
            action['res_id'] = existing.id
        return action

    def action_open_daily_consumption(self):
        self.ensure_one()
        existing = self.env['contractor.site.consumption'].search([
            ('project_id', '=', self.project_id.id),
            ('consumption_date', '=', self.report_date),
        ], limit=1)
        action = {
            'name': _('Daily Material Consumption'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.site.consumption',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_project_id': self.project_id.id,
                'default_consumption_date': self.report_date,
            },
        }
        if existing:
            action['res_id'] = existing.id
        return action

    def action_open_daily_work_logs(self):
        self.ensure_one()
        return {
            'name': _('Daily Work Output Logs'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.site.work.log',
            'view_mode': 'list,form',
            'domain': [
                ('project_id', '=', self.project_id.id),
                ('work_date', '=', self.report_date),
            ],
            'context': {
                'default_project_id': self.project_id.id,
                'default_work_date': self.report_date,
            },
        }

    def action_open_daily_issues(self):
        self.ensure_one()
        return {
            'name': _('Site Issues & Photos'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.site.issue',
            'view_mode': 'list,form',
            'domain': [
                '|',
                ('dpr_id', '=', self.id),
                '&', ('project_id', '=', self.project_id.id), ('issue_date', '=', self.report_date),
            ],
            'context': {
                'default_project_id': self.project_id.id,
                'default_issue_date': self.report_date,
                'default_dpr_id': self.id,
            },
        }


