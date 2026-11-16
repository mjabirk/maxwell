# -*- coding: utf-8 -*-
from datetime import timedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class ContractorSiteSchedule(models.Model):
    _name = 'contractor.site.schedule'
    _description = 'Next-Day Site Staff Schedule'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.site.lock.mixin', 'contractor.no.delete.mixin']
    _order = 'schedule_date desc, id desc'

    name = fields.Char(
        string='Schedule Number',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Schedule'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project Site',
        required=True,
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
    schedule_date = fields.Date(
        string='Deployment Date',
        required=True,
        default=lambda self: fields.Date.context_today(self) + timedelta(days=1),
        tracking=True,
        index=True,
        help='Target operational date for scheduled workforce on site (typically next working day).',
    )
    site_engineer_id = fields.Many2one(
        'res.users',
        string='Site Engineer / Scheduler',
        default=lambda self: self.env.user,
        tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Draft Schedule'),
        ('confirmed', 'Confirmed & Attendance Generated'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    line_ids = fields.One2many(
        'contractor.site.schedule.line',
        'schedule_id',
        string='Scheduled Operatives',
        copy=True,
    )
    total_workers = fields.Integer(
        string='Total Operatives',
        compute='_compute_totals',
        store=True,
    )
    total_planned_hours = fields.Float(
        string='Total Planned Shift Hours',
        compute='_compute_totals',
        store=True,
    )
    attendance_id = fields.Many2one(
        'contractor.site.attendance',
        string='Pre-Populated Attendance Sheet',
        readonly=True,
        copy=False,
    )

    _check_uniq_project_schedule_date = models.Constraint(
        'UNIQUE(project_id, schedule_date)',
        'A site staff schedule already exists for this Project on this deployment date!'
    )

    @api.depends('line_ids.planned_shift_hours')
    def _compute_totals(self):
        for sch in self:
            sch.total_workers = len(sch.line_ids)
            sch.total_planned_hours = sum(sch.line_ids.mapped('planned_shift_hours'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Schedule')) == _('New Schedule'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.site.schedule') or _('New Schedule')
        return super().create(vals_list)

    def action_confirm_schedule(self):
        """Confirms next-day schedule and auto-populates site attendance sheet for the engineer."""
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Please add at least one operative to the deployment schedule."))

        # Check or create attendance sheet for project and date
        attendance = self.env['contractor.site.attendance'].search([
            ('project_id', '=', self.project_id.id),
            ('attendance_date', '=', self.schedule_date),
        ], limit=1)

        attendance_lines = []
        for line in self.line_ids:
            attendance_lines.append((0, 0, {
                'employee_id': line.employee_id.id,
                'scheduled_hours': line.planned_shift_hours,
                'regular_hours': line.planned_shift_hours,
                'attendance_status': 'present',
                'work_zone': line.notes or '',
            }))

        if not attendance:
            attendance = self.env['contractor.site.attendance'].create({
                'project_id': self.project_id.id,
                'attendance_date': self.schedule_date,
                'schedule_id': self.id,
                'line_ids': attendance_lines,
            })
        else:
            if attendance.state == 'draft':
                # Replace or add lines
                attendance.line_ids.unlink()
                attendance.write({
                    'schedule_id': self.id,
                    'line_ids': attendance_lines,
                })

        self.write({
            'state': 'confirmed',
            'attendance_id': attendance.id,
        })

        self.message_post(
            body=Markup(_("<strong>Schedule Confirmed:</strong> Pre-populated Site Attendance Sheet <a href='#' data-oe-model='contractor.site.attendance' data-oe-id='%s'>%s</a> for %d operatives.")) % (
                attendance.id, attendance.name, len(self.line_ids)
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

        return {
            'name': _('Site Attendance Sheet'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.site.attendance',
            'res_id': attendance.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_cancel(self):
        self.ensure_one()
        self.state = 'cancelled'


class ContractorSiteScheduleLine(models.Model):
    _name = 'contractor.site.schedule.line'
    _description = 'Site Schedule Operative Line'
    _order = 'employee_id, id'

    schedule_id = fields.Many2one(
        'contractor.site.schedule',
        string='Schedule Reference',
        required=True,
        ondelete='cascade',
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        related='schedule_id.project_id',
        store=True,
        readonly=True,
    )
    schedule_date = fields.Date(
        string='Deployment Date',
        related='schedule_id.schedule_date',
        store=True,
        readonly=True,
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Operative / Employee',
        required=True,
        domain="[('active', '=', True)]",
    )
    worker_category = fields.Selection(
        related='employee_id.worker_category',
        string='Worker Category',
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
    planned_shift_hours = fields.Float(
        string='Planned Shift Hours',
        required=True,
        default=8.0,
    )
    notes = fields.Char(
        string='Assigned Zone / Task',
    )

    @api.constrains('employee_id', 'schedule_date')
    def _check_double_booking(self):
        """Hard-Stop: Strictly prevents the same operative from being scheduled on multiple projects on the same date."""
        for line in self:
            if not line.employee_id or not line.schedule_date:
                continue

            conflicting = self.search([
                ('employee_id', '=', line.employee_id.id),
                ('schedule_date', '=', line.schedule_date),
                ('id', '!=', line.id),
                ('schedule_id.state', '!=', 'cancelled'),
            ])

            for conflict in conflicting:
                if conflict.project_id.id != line.project_id.id:
                    job_name = line.job_title_id.name or _('Operative')
                    raise ValidationError(_(
                        "Site Staff Double-Booking Hard-Stop:\n\n"
                        "Operative '%s' (%s) is already scheduled on Project '%s' for deployment date '%s'.\n\n"
                        "An employee cannot be scheduled across multiple project sites on the same working day."
                    ) % (line.employee_id.name, job_name, conflict.project_id.name, line.schedule_date))

