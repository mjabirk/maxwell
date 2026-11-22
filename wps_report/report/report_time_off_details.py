# -*- coding: utf-8 -*-
from datetime import datetime, time
import pytz

from odoo import api, fields, models
from odoo.tools.misc import format_date


class ReportTimeOffDetails(models.AbstractModel):
    _name = 'report.wps_report.report_time_off_details'
    _description = 'Time Off Details Report'

    @api.model
    def _get_report_values(self, docids, data=None):
        form = data.get('form', {}) if data else {}
        date_from = form.get('date_from')
        date_to = form.get('date_to')
        holiday_type = form.get('holiday_type', 'approved')
        emp_ids = form.get('emp_ids') or docids or []

        employees = self.env['hr.employee'].browse(emp_ids)
        leave_data = {}

        # Convert date range using the user's timezone to match UTC-stored datetime fields
        user_tz = pytz.timezone(self.env.user.tz or 'UTC')
        date_from_dt = False
        date_to_dt = False

        if date_from:
            date_from_obj = fields.Date.to_date(date_from)
            date_from_dt = user_tz.localize(datetime.combine(date_from_obj, time.min)).astimezone(pytz.UTC).replace(tzinfo=None)
        if date_to:
            date_to_obj = fields.Date.to_date(date_to)
            date_to_dt = user_tz.localize(datetime.combine(date_to_obj, time.max)).astimezone(pytz.UTC).replace(tzinfo=None)

        for emp in employees:
            domain = [('employee_id', '=', emp.id)]
            if date_from_dt:
                domain.append(('date_from', '>=', date_from_dt))
            if date_to_dt:
                domain.append(('date_to', '<=', date_to_dt))
            if holiday_type == 'approved':
                domain.append(('state', '=', 'validate'))

            leave_data[emp.id] = self.env['hr.leave'].search(domain, order='date_from asc')

        return {
            'doc_ids': emp_ids,
            'doc_model': 'hr.employee',
            'docs': employees,
            'leave_data': leave_data,
            'holiday_type': holiday_type,
            'date_from': format_date(self.env, fields.Date.to_date(date_from)) if date_from else '',
            'date_to': format_date(self.env, fields.Date.to_date(date_to)) if date_to else '',
        }
