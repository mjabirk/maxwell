# -*- coding: utf-8 -*-
import logging
import pytz
from datetime import datetime, time
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class OvertimeBonusType(models.Model):
    _name = 'overtime.bonus.type'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'analytic.mixin']
    _description = "Overtime Bonus Types"

    name = fields.Char(string='Name')
    rate = fields.Float(string="Rate")
    type = fields.Selection([
        ('manual', 'Manual'),
        ('wage', 'Wage'),
        ('fixed', 'Fixed'),
        ('deduction', 'Deduction'),
    ], string='Incentive Type', required=True)


class OvertimeBonus(models.Model):
    _name = 'overtime.bonus'
    _description = "Overtime Bonus"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'analytic.mixin']
    _order = "date_from desc, id desc"

    name = fields.Char(string='Description', tracking=True, required=True)
    date_from = fields.Datetime(string='Date', tracking=True, required=True)
    company_id = fields.Many2one('res.company', string='Company', default=lambda self: self.env.company, required=True)
    type = fields.Selection([
        ('overtime', 'Overtime'),
        ('bonus', 'Bonus'),
        ('productivity', 'Productivity'),
        ('deduction', 'Deduction'),
    ], 'Type Category', default='overtime', readonly=False)
    company_currency_id = fields.Many2one('res.currency', string="Report Company Currency",
                                          related='company_id.currency_id', readonly=True, tracking=True)
    duration = fields.Float(string="Duration", tracking=True)
    amount = fields.Float(string="Quantity / Amount", tracking=True)
    employee_id = fields.Many2one('hr.employee', string='Employee', tracking=True, required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected')
    ], string='State', required=True, default='draft', readonly=True, tracking=True)
    department_id = fields.Many2one('hr.department', string="Department", related='employee_id.department_id',
                                    store=True)
    job_id = fields.Many2one('hr.job', string="Job", related='employee_id.job_id', store=True)
    ot_amount = fields.Float(string="OT Amount", compute='_compute_amount', store=True)
    type_id = fields.Many2one('overtime.bonus.type', string='Type', required=True)
    notes = fields.Text(string="Notes", readonly=True)

    creation_type = fields.Selection([
        ('manual', 'Manual'),
        ('auto', 'Automatic')
    ], string='Creation Type', default='manual', required=True, tracking=True)

    @api.onchange('type_id')
    def _onchange_type_id(self):
        if self.type_id:
            self.type = self.type_id.type == 'fixed' and 'productivity' or (
                self.type_id.type == 'wage' and 'overtime' or (
                    self.type_id.type == 'manual' and 'bonus' or 'deduction'
                )
            )

    @api.depends('employee_id', 'amount', 'duration', 'type')
    def _compute_amount(self):
        DAYS_PER_MONTH = 365.0 / 12
        WORKING_TIME = 8
        for rec in self:
            employee_sudo = rec.sudo().employee_id
            version_sudo = (employee_sudo.version_id or employee_sudo.current_version_id) if employee_sudo else False

            if rec.type == 'overtime' and version_sudo:
                rec.ot_amount = version_sudo.wage / DAYS_PER_MONTH / WORKING_TIME * rec.duration
            elif rec.type == 'bonus':
                rec.ot_amount = rec.amount
            elif rec.type == 'productivity':
                rec.ot_amount = rec.amount * rec.type_id.rate
            elif rec.type == 'deduction':
                rec.ot_amount = -1.0 * rec.amount
            else:
                rec.ot_amount = 0.0

    @api.constrains('date_from', 'employee_id', 'creation_type')
    def _check_ot_bonus(self):
        if self.env.context.get('skip_daily_check'):
            return

        for ot_bonus in self.filtered(lambda c: c.state != 'rejected' and c.creation_type == 'manual'):
            if not ot_bonus.date_from:
                continue
            tz_name = self._context.get('tz') or self.env.user.tz or (ot_bonus.employee_id.resource_calendar_id.tz if ot_bonus.employee_id.resource_calendar_id else None) or 'UTC'
            context_tz = pytz.timezone(tz_name)
            tz_date_from = pytz.utc.localize(ot_bonus.date_from).astimezone(context_tz)

            domain = [
                ('id', '!=', ot_bonus.id),
                ('employee_id', '=', ot_bonus.employee_id.id),
                ('creation_type', '=', 'manual'),
                ('state', '!=', 'rejected'),
                ('date_from', '>=', tz_date_from.replace(hour=0, minute=0, second=0)),
                ('date_from', '<=', tz_date_from.replace(hour=23, minute=59, second=59)),
            ]
            if self.search_count(domain):
                raise ValidationError(
                    _(
                        'An employee is limited to only one instance of manual overtime per day.\n\nEmployee: %(employee_name)s',
                        employee_name=ot_bonus.employee_id.name
                    )
                )

    @api.onchange('type')
    def check_val(self):
        for res in self:
            res.amount = res.duration = 0

    def unlink(self):
        for rec in self:
            if rec.state not in ['draft']:
                raise UserError(_('Only draft records can be deleted.'))
        return super(OvertimeBonus, self).unlink()

    def button_approve(self):
        notification = {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('There are no records to approve.'),
                'type': 'warning',
                'sticky': False
            },
        }
        filtered_recs = self.filtered(lambda s: s.state in ['draft'])
        if not filtered_recs:
            return notification
        for record in filtered_recs:
            record.write({'state': 'approved'})

    def button_reject(self):
        if self.state == 'draft':
            self.write({'state': 'rejected'})

    def set_to_draft(self):
        self.write({'state': 'draft'})

    def action_open_wizard(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("payroll_extra.overtime_bonus_form")
        action['res_id'] = self.id
        return action


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    overtime_line_ids = fields.One2many('overtime.bonus', 'employee_id', string='Overtime')


class HrPayslipRun(models.Model):
    _inherit = 'hr.payslip.run'

    def _generate_payslips(self):
        for run in self:
            valid_versions = run.version_ids - run.slip_ids.version_id
            if run.structure_id:
                valid_versions = valid_versions.filtered(
                    lambda c: c.structure_type_id.id == run.structure_id.type_id.id
                )
            employee_ids = valid_versions.employee_id.ids

            if employee_ids and run.date_start and run.date_end:
                date_start_dt = datetime.combine(fields.Date.to_date(run.date_start), time.min)
                date_end_dt = datetime.combine(fields.Date.to_date(run.date_end), time.max)

                draft_ot = self.env['overtime.bonus'].search([
                    ('employee_id', 'in', employee_ids),
                    ('date_from', '>=', date_start_dt),
                    ('date_from', '<=', date_end_dt),
                    ('state', '=', 'draft'),
                ], limit=1)

                if draft_ot:
                    raise UserError(
                        _("Please review and either approve or reject the draft overtime entries prior to generating the payslip.")
                    )

        return super()._generate_payslips()