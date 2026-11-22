# -*- coding: utf-8 -*-
from datetime import date
from odoo import fields, models, _
from odoo.exceptions import UserError


class TimeOffDetailsWizard(models.TransientModel):
    _name = 'time.off.details.wizard'
    _description = 'Time Off Details Wizard'

    date_from = fields.Date(
        string='Date From',
        required=True,
        default=lambda self: date(fields.Date.context_today(self).year, 1, 1),
    )
    date_to = fields.Date(
        string='Date To',
        required=True,
        default=fields.Date.context_today,
    )
    holiday_type = fields.Selection([
        ('approved', 'Approved'),
        ('all', 'All'),
    ], string='Status', required=True, default='approved')

    def print_report(self):
        self.ensure_one()
        active_ids = self.env.context.get('active_ids') or (
            [self.env.context.get('active_id')] if self.env.context.get('active_id') else []
        )
        if not active_ids:
            raise UserError(_("Please select at least one employee to print the report."))

        employees = self.env['hr.employee'].browse(active_ids)
        datas = {
            'ids': active_ids,
            'model': 'hr.employee',
            'form': {
                'date_from': fields.Date.to_string(self.date_from) if self.date_from else False,
                'date_to': fields.Date.to_string(self.date_to) if self.date_to else False,
                'holiday_type': self.holiday_type,
                'emp_ids': active_ids,
            },
        }
        return self.env.ref('wps_report.action_report_time_off_details').report_action(employees, data=datas)
