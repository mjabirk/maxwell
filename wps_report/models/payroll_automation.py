# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
import logging

from odoo import api, fields, models, Command, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class PayrollAutomation(models.Model):
    _name = 'payroll.automation'
    _description = 'Payroll Automation'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True

    name = fields.Char(string='Name', required=True, tracking=True)

    employee_department_ids = fields.Many2many(
        'hr.department',
        string='Employee Departments',
        help='Select the employee departments for which payroll automation applies.',
        tracking=True
    )

    schedule_date = fields.Date(
        string='Scheduled Date',
        required=True,
        tracking=True,
        help='The date on which payroll automation should be scheduled.'
    )

    last_created_date = fields.Datetime(
        string='Last Created Date',
        default=fields.Datetime.now,
        tracking=True,
        help='Date and time when the payroll automation was last created or triggered.'
    )

    active = fields.Boolean(
        string='Active',
        default=True,
        help='Activate or deactivate payroll automation.'
    )

    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
        help='Company associated with this payroll automation',
        tracking=True
    )

    bank_list = fields.Selection([
        ('hsb', 'HSBC Bank Middle East'),
        ('mar', 'Masraf Al Rayyan Bank'),
        ('qnb', 'Qatar National Bank'),
        ('cbq', 'Commercial Bank of Qatar')],
        string='WPS Bank', required=True, default='qnb')

    email = fields.Char('Email send to')

    structure_id = fields.Many2one(
        'hr.payroll.structure',
        string='Structure',
        tracking=True
    )

    batch_name = fields.Char('Batch Name', default='%B %Y Pay Run')

    _name_unique = models.Constraint(
        'unique(name, company_id)',
        'The payroll automation name must be unique within a company!'
    )

    def action_trigger_payroll(self):
        automations = self.search([('active', '=', True)])
        today = fields.Date.today()
        for automation in automations:
            automation = automation.with_company(automation.company_id)
            if not automation.schedule_date:
                automation.schedule_date = today

            if automation.schedule_date == today:
                # Calculate payroll dates for the previous month
                first_day_of_current_month = today.replace(day=1)
                date_end = first_day_of_current_month - timedelta(days=1)
                date_start = date_end.replace(day=1)

                company_code = ''.join([word[0].upper() for word in automation.company_id.name.split(' ') if word])[:2]
                batch_pattern = automation.batch_name or '%B %Y Pay Run'
                try:
                    formatted_name = date_start.strftime(batch_pattern)
                except Exception:
                    formatted_name = batch_pattern
                run_name = f"{formatted_name} {company_code}".strip()

                # Find eligible active employees for this company and department
                domain = [
                    ('company_id', '=', automation.company_id.id),
                    ('active', '=', True),
                ]
                if automation.employee_department_ids:
                    domain.append(('department_id', 'in', automation.employee_department_ids.ids))
                employees = self.env['hr.employee'].search(domain)

                if not employees:
                    _logger.warning("Payroll Automation: No active employees found for company %s", automation.company_id.name)
                    automation.schedule_date = automation.schedule_date + relativedelta(months=1)
                    automation.last_created_date = fields.Datetime.now()
                    continue

                # In Odoo 20, contracts are managed via hr.version
                contracts_by_emp = employees._get_contracts(date_start, date_end, use_latest_version=True)
                versions = self.env['hr.version']
                for emp_versions in contracts_by_emp.values():
                    versions |= emp_versions

                if not versions:
                    _logger.warning("Payroll Automation: No active contract versions found for company %s in period %s to %s",
                                   automation.company_id.name, date_start, date_end)
                    automation.schedule_date = automation.schedule_date + relativedelta(months=1)
                    automation.last_created_date = fields.Datetime.now()
                    continue

                # Generate work entries for the period
                versions.generate_work_entries(date_start, date_end)

                # Determine payroll structure for the pay run (structure_id is required in Odoo 20)
                structure = automation.structure_id
                if not structure:
                    version_structs = versions.mapped('structure_type_id.default_struct_id')
                    if version_structs:
                        structure = version_structs[0]
                    else:
                        if automation.company_id.country_id:
                            structure = self.env['hr.payroll.structure'].search([
                                ('country_id', '=', automation.company_id.country_id.id),
                                ('use_worked_day_lines', '=', True),
                            ], limit=1)
                        if not structure:
                            structure = self.env['hr.payroll.structure'].search([], limit=1)

                if not structure:
                    _logger.warning("Payroll Automation: No payroll structure found for company %s", automation.company_id.name)
                    automation.schedule_date = automation.schedule_date + relativedelta(months=1)
                    automation.last_created_date = fields.Datetime.now()
                    continue

                # Create Pay Run
                payslip_run = self.env['hr.payslip.run'].create({
                    'name': run_name,
                    'date_start': date_start,
                    'date_end': date_end,
                    'company_id': automation.company_id.id,
                    'structure_id': structure.id,
                    'version_ids': [Command.set(versions.ids)],
                })

                # Create Payslips for each version
                Payslip = self.env['hr.payslip']
                payslips_vals = []
                for version in versions:
                    struct_id = automation.structure_id.id if automation.structure_id else (
                        version.structure_id.id if hasattr(version, 'structure_id') and version.structure_id else (
                            version.structure_type_id.default_struct_id.id if version.structure_type_id and version.structure_type_id.default_struct_id else structure.id
                        )
                    )
                    values = {
                        'name': f"Salary Slip - {version.employee_id.name} - {date_start.strftime('%B %Y')}",
                        'employee_id': version.employee_id.id,
                        'payslip_run_id': payslip_run.id,
                        'date_from': payslip_run.date_start,
                        'date_to': payslip_run.date_end,
                        'version_id': version.id,
                        'company_id': automation.company_id.id,
                        'struct_id': struct_id,
                    }
                    payslips_vals.append(values)

                payslips = Payslip.with_context(tracking_disable=True).create(payslips_vals)
                payslips._compute_name()
                payslips.compute_sheet()

                # Validate Pay Run
                payslip_run.action_validate()
                if hasattr(payslip_run, 'move_id') and payslip_run.move_id:
                    payslip_run.move_id.action_post()

                # Generate WPS transfer sheet and email if configured
                if automation.email:
                    try:
                        sheet_wiz = self.env['salary.transfer.sheet'].create({
                            'bank_list': automation.bank_list or 'qnb',
                        })
                        file_name, file_content = sheet_wiz.generate_transfer_sheet(given_payroll_reg=payslip_run)
                        if file_name and file_content:
                            attachment = self.env['ir.attachment'].create({
                                'name': file_name,
                                'type': 'binary',
                                'datas': file_content,
                                'res_model': 'mail.mail',
                                'res_id': 0,
                                'mimetype': 'text/csv' if file_name.endswith('.csv') else 'application/zip',
                            })
                            mail = self.env['mail.mail'].create({
                                'subject': f'{automation.company_id.name} WPS File of {date_start.strftime("%Y%m")}',
                                'email_to': automation.email,
                                'body_html': '<p>Please find attached the WPS SIF file.</p>',
                                'attachment_ids': [Command.link(attachment.id)],
                            })
                            mail.send()
                    except Exception as e:
                        _logger.error("Failed to generate or send WPS SIF email for automation %s: %s", automation.name, e)

                automation.schedule_date = automation.schedule_date + relativedelta(months=1)
                automation.last_created_date = fields.Datetime.now()

        return True
