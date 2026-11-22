# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class HrPayrollStructure(models.Model):
    _inherit = 'hr.payroll.structure'

    use_odoo20_salary_rules = fields.Boolean(
        string='Create Rules as Odoo 20 Format',
        default=True,
        help="If enabled, loads Qatar WPS salary rules in Odoo 20 format when creating a new structure."
    )

    def _find_account_id(self, code_prefix):
        if not code_prefix:
            return False
        account = self.env['account.account'].search([
            ('code_store', '=like', f'{code_prefix}%'),
        ], limit=1)
        return account.id if account else False

    def _find_salary_partner_id(self):
        partner = self.env['res.partner'].search([
            ('name', 'ilike', 'SALARY'),
        ], limit=1)
        return partner.id if partner else False

    @api.model
    def _get_odoo20_salary_rules_vals(self, country_id=None):
        """Return salary rule definition dictionaries in Odoo 20 format based on WPS / Qatar requirements."""
        cid = country_id or (self.country_id.id if hasattr(self, 'country_id') and self.country_id else self.env.company.country_id.id)

        cat_basic = self.env.ref('hr_payroll.BASIC', raise_if_not_found=False)
        cat_alw = self.env.ref('hr_payroll.ALW', raise_if_not_found=False)
        cat_gross = self.env.ref('hr_payroll.GROSS', raise_if_not_found=False)
        cat_ded = self.env.ref('hr_payroll.DED', raise_if_not_found=False)
        cat_net = self.env.ref('hr_payroll.NET', raise_if_not_found=False)

        cat_basic_id = cat_basic.id if cat_basic else False
        cat_alw_id = cat_alw.id if cat_alw else False
        cat_gross_id = cat_gross.id if cat_gross else False
        cat_ded_id = cat_ded.id if cat_ded else False
        cat_net_id = cat_net.id if cat_net else False

        partner_salary_id = self._find_salary_partner_id()

        has_account_fields = 'account_debit' in self.env['hr.salary.rule']._fields

        def make_rule_vals(name, code, sequence, category_id, condition_select, condition_python,
                           amount_python_compute, appears_on_payslip='always',
                           credit_code=None, debit_code=None, partner_id=False):
            vals = {
                'name': name,
                'code': code,
                'sequence': sequence,
                'category_ids': [(6, 0, [category_id])] if category_id else [(5, 0, 0)],
                'condition_select': condition_select,
                'condition_python': condition_python or 'result = True',
                'amount_select': 'code',
                'amount_python_compute': amount_python_compute,
                'appears_on_payslip': appears_on_payslip,
                'quantity': '1.0',
                'active': True,
                'country_id': cid,
            }
            if partner_id:
                vals['partner_id'] = partner_id
            if has_account_fields:
                if credit_code:
                    vals['account_credit'] = self._find_account_id(credit_code)
                if debit_code:
                    vals['account_debit'] = self._find_account_id(debit_code)
            return vals

        rules = []

        # 1. CONTOT - Contract Total
        rules.append(make_rule_vals(
            name=_('Contract Total'),
            code='CONTOT',
            sequence=10,
            category_id=cat_gross_id,
            condition_select='none',
            condition_python=False,
            appears_on_payslip='never',
            amount_python_compute="""contract = localdict.get('contract') or version
paid_work_types = [
    "WORK100", "002.00", "PAIDLEAVE106", "LEAVE110", "Business",
    "Sick_Leave", "Leave_Reimbursement", "Termination", "Resignation"
]
Paid_Days = 0
for work_type in paid_work_types:
    work_entries = worked_days.get(work_type)
    if work_entries:
        for wd in work_entries:
            Paid_Days += wd.number_of_days

# --- 2. Get Total Days in the Month (Computed Once) ---
month = payslip.date_from.month
year = payslip.date_from.year
if month == 2:
    if (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0):
        tot_days = 29
    else:
        tot_days = 28
elif month in [1, 3, 5, 7, 8, 10, 12]:
    tot_days = 31
else:
    tot_days = 30

# If no worked days entries exist on payslip, default to full month
if not worked_days and tot_days > 0:
    Paid_Days = tot_days

# --- 3. Calculate Prorated Salary (Computed Once) ---
actual_salary = 0
proration_factor = 1.0

if tot_days > 0:
    # Calculate the proration factor
    proration_factor = Paid_Days / tot_days
    actual_salary += round(contract.wage * proration_factor)
    actual_salary += round(contract.accommodation_allowance * proration_factor)
    actual_salary += round(contract.food_allowance * proration_factor)
    actual_salary += round(contract.transportation_allowance * proration_factor)
    actual_salary += round(contract.other_allowance * proration_factor)

# Store in localdict for subsequent rules
localdict['tot_days'] = tot_days
localdict['Paid_Days'] = Paid_Days
localdict['proration_factor'] = proration_factor
localdict['actual_salary'] = actual_salary
localdict['contract'] = contract

result = contract.total_salary
result_name = 'Contract Total'"""
        ))

        # 2. BASIC - Basic Salary
        rules.append(make_rule_vals(
            name=_('Basic Salary - الراتب الاساسي'),
            code='BASIC',
            sequence=20,
            category_id=cat_basic_id,
            condition_select='none',
            condition_python=False,
            appears_on_payslip='always',
            credit_code='405',
            debit_code='701',
            partner_id=partner_salary_id,
            amount_python_compute="""contract = localdict.get('contract') or version
tot_days = localdict.get('tot_days', 30)
proration_factor = localdict.get('proration_factor', 1.0)
result = round((tot_days) and contract.wage * proration_factor or 0)"""
        ))

        # 3. ACCO - Accommodation
        rules.append(make_rule_vals(
            name=_('Accommodation - السكن'),
            code='ACCO',
            sequence=30,
            category_id=cat_alw_id,
            condition_select='none',
            condition_python=False,
            appears_on_payslip='always',
            credit_code='405',
            debit_code='705',
            partner_id=partner_salary_id,
            amount_python_compute="""contract = localdict.get('contract') or version
tot_days = localdict.get('tot_days', 30)
proration_factor = localdict.get('proration_factor', 1.0)
result = round((tot_days) and contract.accommodation_allowance * proration_factor or 0)"""
        ))

        # 4. FOOD - Food
        rules.append(make_rule_vals(
            name=_('Food - بدل طعام'),
            code='FOOD',
            sequence=35,
            category_id=cat_alw_id,
            condition_select='none',
            condition_python=False,
            appears_on_payslip='always',
            credit_code='405',
            debit_code='610',
            partner_id=partner_salary_id,
            amount_python_compute="""contract = localdict.get('contract') or version
tot_days = localdict.get('tot_days', 30)
proration_factor = localdict.get('proration_factor', 1.0)
result = round((tot_days) and contract.food_allowance * proration_factor or 0)"""
        ))

        # 5. OT - Overtime Hours
        rules.append(make_rule_vals(
            name=_('Overtime - ساعات العمل اضافية'),
            code='OT',
            sequence=40,
            category_id=cat_alw_id,
            condition_select='python',
            condition_python="""overtime_lines = employee.overtime_line_ids.search([
    ('employee_id', '=', employee.id),
    ('date_from', '>=', payslip.date_from),
    ('date_from', '<=', payslip.date_to),
    ('type', '=', 'overtime'),
    ('state', '=', 'approved'),
]) if 'overtime_line_ids' in employee._fields else []
tot_hours = 0
for ot in overtime_lines:
    tot_hours += ot.duration

result = bool(worked_days.get("OT") or (inputs.get("OT") and inputs["OT"].amount) or tot_hours)""",
            appears_on_payslip='non_zero',
            credit_code='405',
            debit_code='617',
            partner_id=partner_salary_id,
            amount_python_compute="""DAYS_PER_MONTH = 365.0 / 12
WORKING_TIME = 8
contract = localdict.get('contract') or version
rate = contract.wage / DAYS_PER_MONTH / WORKING_TIME
overtime_lines = employee.overtime_line_ids.search([
    ('employee_id', '=', employee.id),
    ('date_from', '>=', payslip.date_from),
    ('date_from', '<=', payslip.date_to),
    ('type', '=', 'overtime'),
    ('state', '=', 'approved'),
]) if 'overtime_line_ids' in employee._fields else []
tot_hours = 0
for ot in overtime_lines:
    tot_hours += ot.duration
ot_hours = 0
if worked_days.get("OT"):
    for wd in worked_days["OT"]:
        ot_hours += wd.number_of_hours
result = round(ot_hours * rate + (inputs.get("OT") and inputs["OT"].amount or 0) + tot_hours * rate)"""
        ))

        # 6. BONUS - Addition
        rules.append(make_rule_vals(
            name=_('Addition - إضافة'),
            code='BONUS',
            sequence=45,
            category_id=cat_alw_id,
            condition_select='none',
            condition_python=False,
            appears_on_payslip='non_zero',
            credit_code='405',
            debit_code='617',
            partner_id=partner_salary_id,
            amount_python_compute="""overtime_lines = employee.overtime_line_ids.search([
    ('employee_id', '=', employee.id),
    ('date_from', '>=', payslip.date_from),
    ('date_from', '<=', payslip.date_to),
    ('type', '=', 'bonus'),
    ('state', '=', 'approved'),
]) if 'overtime_line_ids' in employee._fields else []
tot_amount = 0
for ot in overtime_lines:
    tot_amount += ot.ot_amount
result = round(tot_amount)"""
        ))

        # 7. INC - Incentive
        rules.append(make_rule_vals(
            name=_('Incentive - حوافز'),
            code='INC',
            sequence=50,
            category_id=cat_alw_id,
            condition_select='none',
            condition_python=False,
            appears_on_payslip='non_zero',
            credit_code='405',
            debit_code='617',
            partner_id=partner_salary_id,
            amount_python_compute="""result = 0
if 'x_productivity_ids' in employee._fields:
    incentive_ids = employee.x_productivity_ids.search([
        ('x_employee_id', '=', employee.id),
        ('x_date', '>=', payslip.date_from),
        ('x_date', '<=', payslip.date_to),
    ])
    for incentive in incentive_ids:
        result += incentive.x_amount if 'x_amount' in incentive._fields else 0

if 'overtime_line_ids' in employee._fields:
    overtime_lines = employee.overtime_line_ids.search([
        ('employee_id', '=', employee.id),
        ('date_from', '>=', payslip.date_from),
        ('date_from', '<=', payslip.date_to),
        ('type', '=', 'productivity'),
        ('state', '=', 'approved'),
    ])
    for ot in overtime_lines:
        result += ot.ot_amount
result = round(result)"""
        ))

        # 8. TRANS - Transportation Allowances
        rules.append(make_rule_vals(
            name=_('Transportation Allowances - بدل انتقال'),
            code='TRANS',
            sequence=55,
            category_id=cat_alw_id,
            condition_select='none',
            condition_python=False,
            appears_on_payslip='always',
            credit_code='405',
            debit_code='701',
            partner_id=partner_salary_id,
            amount_python_compute="""contract = localdict.get('contract') or version
tot_days = localdict.get('tot_days', 30)
proration_factor = localdict.get('proration_factor', 1.0)
result = round((tot_days) and contract.transportation_allowance * proration_factor or 0)"""
        ))

        # 9. OTHER - Other Allowances
        rules.append(make_rule_vals(
            name=_('Other Allowances - بدلات أخرى'),
            code='OTHER',
            sequence=60,
            category_id=cat_alw_id,
            condition_select='none',
            condition_python=False,
            appears_on_payslip='always',
            credit_code='405',
            debit_code='701',
            partner_id=partner_salary_id,
            amount_python_compute="""contract = localdict.get('contract') or version
tot_days = localdict.get('tot_days', 30)
proration_factor = localdict.get('proration_factor', 1.0)
result = round((tot_days) and contract.other_allowance * proration_factor or 0)"""
        ))

        # 10. AIRTKT - Air Ticket
        rules.append(make_rule_vals(
            name=_('Air Ticket - ????? ?????'),
            code='AIRTKT',
            sequence=70,
            category_id=cat_alw_id,
            condition_select='python',
            condition_python="""result = bool(worked_days.get("Resignation") or worked_days.get("Termination") or worked_days.get("Annual"))""",
            appears_on_payslip='non_zero',
            credit_code='405',
            debit_code='407',
            partner_id=partner_salary_id,
            amount_python_compute="""contract = localdict.get('contract') or version
result = 0
if worked_days.get("Resignation") or worked_days.get("Termination"):
    result += round(contract.air_ticket)

leave_model = employee.leave_ids if 'leave_ids' in employee._fields else (employee.x_leave_ids if 'x_leave_ids' in employee._fields else payslip.env['hr.leave'])
leave_type_field = 'work_entry_type_id' if 'work_entry_type_id' in payslip.env['hr.leave']._fields else 'holiday_status_id'
leaves_available = leave_model.search([
    ('employee_id', '=', employee.id),
    ('state', '=', 'validate'),
    ('request_date_from', '<=', payslip.date_to),
    ('request_date_from', '>=', payslip.date_from),
    (f'{leave_type_field}.name', 'in', ['Eligible_Leave', 'Resignation', 'Termination']),
    ('air_ticket', '>', 0),
])
for leave in leaves_available:
    if leave.air_ticket:
        result = leave.air_ticket"""
        ))

        # 11. EOS - End of Service
        rules.append(make_rule_vals(
            name=_('End of Service - ????? ??????'),
            code='EOS',
            sequence=80,
            category_id=cat_alw_id,
            condition_select='python',
            condition_python="""leave_model = employee.leave_ids if 'leave_ids' in employee._fields else (employee.x_leave_ids if 'x_leave_ids' in employee._fields else payslip.env['hr.leave'])
leave_type_field = 'work_entry_type_id' if 'work_entry_type_id' in payslip.env['hr.leave']._fields else 'holiday_status_id'
has_wd = bool(worked_days.get("Termination") or worked_days.get("Resignation"))
is_first_month = False
already_calculated = False

if has_wd:
    term_leaves = leave_model.search([
        ('employee_id', '=', employee.id),
        ('state', '=', 'validate'),
        (f'{leave_type_field}.name', 'in', ['Termination', 'Resignation']),
        ('request_date_from', '<=', payslip.date_to),
        ('request_date_to', '>=', payslip.date_from),
    ])
    for l in term_leaves:
        if payslip.date_from <= l.request_date_from <= payslip.date_to:
            is_first_month = True
            prev_lines = payslip.env['hr.payslip.line'].search_count([
                ('slip_id', '!=', payslip.id),
                ('slip_id.employee_id', '=', employee.id),
                ('code', '=', 'EOS'),
                ('total', '!=', 0),
                ('slip_id.date_to', '>=', l.request_date_from),
            ])
            if prev_lines:
                already_calculated = True
            break

result = bool(has_wd and is_first_month and not already_calculated)""",
            appears_on_payslip='non_zero',
            credit_code='405',
            debit_code='408',
            partner_id=partner_salary_id,
            amount_python_compute="""contract = localdict.get('contract') or version
date_from = contract.date_start

leave_model = employee.leave_ids if 'leave_ids' in employee._fields else (employee.x_leave_ids if 'x_leave_ids' in employee._fields else payslip.env['hr.leave'])
leave_type_field = 'work_entry_type_id' if 'work_entry_type_id' in payslip.env['hr.leave']._fields else 'holiday_status_id'

term_leaves = leave_model.search([
    ('employee_id', '=', employee.id),
    ('state', '=', 'validate'),
    (f'{leave_type_field}.name', 'in', ['Termination', 'Resignation']),
    ('request_date_from', '<=', payslip.date_to),
    ('request_date_from', '>=', payslip.date_from),
])

if not term_leaves:
    result = 0
else:
    date_to = term_leaves[0].request_date_from
    delay = (date_to - date_from).days + 1

    unpaid_leaves = leave_model.search([
        ('employee_id', '=', employee.id),
        ('state', '=', 'validate'),
        ('request_date_from', '>=', date_from),
        ('request_date_to', '<=', payslip.date_to),
        (f'{leave_type_field}.name', 'in', ['Unpaid', 'Absent', 'Eligible_Leave']),
    ])
    unpaid_days = 0
    for unpaid_leave in unpaid_leaves:
        unpaid_days += unpaid_leave.number_of_days
    if delay <= unpaid_days:
        delay = 0
    else:
        delay = delay - unpaid_days 
    result = round((contract.eos_days * delay / 365) * (contract.wage) / (365 / 12)) or 0"""
        ))

        # 12. ELP - Eligible Leave Pay
        rules.append(make_rule_vals(
            name=_('Eligible Leave Pay'),
            code='ELP',
            sequence=85,
            category_id=cat_alw_id,
            condition_select='python',
            condition_python="""result = bool(worked_days.get("Annual"))""",
            appears_on_payslip='non_zero',
            credit_code='405',
            debit_code='406',
            partner_id=partner_salary_id,
            amount_python_compute="""contract = localdict.get('contract') or version
days = 0
if worked_days.get("Annual"):
    for wd in worked_days["Annual"]:
        days += wd.number_of_days
result = round((contract.wage + contract.accommodation_allowance) / (365 / 12) * days)"""
        ))

        # 13. LP - Leave Pay
        rules.append(make_rule_vals(
            name=_('Leave Pay - ??? ?????'),
            code='LP',
            sequence=90,
            category_id=cat_alw_id,
            condition_select='python',
            condition_python="""leave_model = employee.leave_ids if 'leave_ids' in employee._fields else (employee.x_leave_ids if 'x_leave_ids' in employee._fields else payslip.env['hr.leave'])
leave_type_field = 'work_entry_type_id' if 'work_entry_type_id' in payslip.env['hr.leave']._fields else 'holiday_status_id'
has_wd = bool(worked_days.get("Termination") or worked_days.get("Resignation"))
is_first_month = False
already_calculated = False

if has_wd:
    term_leaves = leave_model.search([
        ('employee_id', '=', employee.id),
        ('state', '=', 'validate'),
        (f'{leave_type_field}.name', 'in', ['Termination', 'Resignation']),
        ('request_date_from', '<=', payslip.date_to),
        ('request_date_to', '>=', payslip.date_from),
    ])
    for l in term_leaves:
        if payslip.date_from <= l.request_date_from <= payslip.date_to:
            is_first_month = True
            prev_lines = payslip.env['hr.payslip.line'].search_count([
                ('slip_id', '!=', payslip.id),
                ('slip_id.employee_id', '=', employee.id),
                ('code', '=', 'LP'),
                ('total', '!=', 0),
                ('slip_id.date_to', '>=', l.request_date_from),
            ])
            if prev_lines:
                already_calculated = True
            break

result = bool(has_wd and is_first_month and not already_calculated)""",
            appears_on_payslip='non_zero',
            credit_code='405',
            debit_code='406',
            partner_id=partner_salary_id,
            amount_python_compute="""contract = localdict.get('contract') or version
date_from = contract.date_start
total_taken_days = 0

leave_model = employee.leave_ids if 'leave_ids' in employee._fields else (employee.x_leave_ids if 'x_leave_ids' in employee._fields else payslip.env['hr.leave'])
leave_type_field = 'work_entry_type_id' if 'work_entry_type_id' in payslip.env['hr.leave']._fields else 'holiday_status_id'

term_leaves = leave_model.search([
    ('employee_id', '=', employee.id),
    ('state', '=', 'validate'),
    (f'{leave_type_field}.name', 'in', ['Termination', 'Resignation']),
    ('request_date_from', '<=', payslip.date_to),
    ('request_date_from', '>=', payslip.date_from),
])

if not term_leaves:
    result = 0
else:
    annual_leaves_taken = leave_model.search([
        ('employee_id', '=', employee.id),
        ('state', '=', 'validate'),
        ('request_date_from', '<', payslip.date_to),
        ('request_date_from', '>=', date_from),
        (f'{leave_type_field}.name', 'in', ['Eligible_Leave']),
    ], order='request_date_from desc')
    for annual_leave in annual_leaves_taken:
        total_taken_days += annual_leave.number_of_days

    date_to = term_leaves[0].request_date_from

    unpaid_leaves = leave_model.search([
        ('employee_id', '=', employee.id),
        ('state', '=', 'validate'),
        ('request_date_from', '>=', date_from),
        ('request_date_to', '<=', payslip.date_to),
        (f'{leave_type_field}.name', 'in', ['Unpaid', 'Absent', 'Eligible_Leave']),
    ])
    unpaid_days = 0
    for unpaid_leave in unpaid_leaves:
        unpaid_days += unpaid_leave.number_of_days

    delay = (date_to - date_from).days - unpaid_days + 1
    result = round((contract.leave_pay_days * delay / 365 - total_taken_days) * (contract.wage + contract.accommodation_allowance) / (365 / 12)) or 0

    if total_taken_days:
        result_name = 'Leave pay ({} days have already been utilized) - ??? ?????'.format(total_taken_days)
    else:
        result_name = 'Leave pay ({} days ) - ??? ?????'.format(delay)"""
        ))

        # 14. DED - Deduction Salary
        rules.append(make_rule_vals(
            name=_('Deduction Salary - الاستقطاعات'),
            code='DED',
            sequence=100,
            category_id=cat_ded_id,
            condition_select='python',
            condition_python="""ot_count = len(employee.overtime_line_ids.search([
    ('employee_id', '=', employee.id),
    ('date_from', '>=', payslip.date_from),
    ('date_from', '<=', payslip.date_to),
    ('type', '=', 'deduction'),
    ('state', '=', 'approved'),
    ('ot_amount', '<', 0),
])) if 'overtime_line_ids' in employee._fields else 0

result = bool(ot_count or inputs.get("DED"))""",
            appears_on_payslip='non_zero',
            credit_code='510',
            debit_code='405',
            partner_id=partner_salary_id,
            amount_python_compute="""result = 0
overtime_line_ids = employee.overtime_line_ids.search([
    ('employee_id', '=', employee.id),
    ('date_from', '>=', payslip.date_from),
    ('date_from', '<=', payslip.date_to),
    ('type', '=', 'deduction'),
    ('state', '=', 'approved'),
    ('ot_amount', '<', 0),
]) if 'overtime_line_ids' in employee._fields else []
reason = ''
for ot in overtime_line_ids:
    result += -1 * ot.ot_amount
    reason += f'{ot.name}: {ot.amount} {payslip.currency_id.symbol}. '
result = round(result)
result_name = reason
if inputs.get("DED"):
    result += inputs["DED"].amount
    result_name += inputs["DED"].name"""
        ))

        # 15. TOT_DED - Total Deduction
        rules.append(make_rule_vals(
            name=_('Total Deduction'),
            code='TOT_DED',
            sequence=110,
            category_id=cat_gross_id,
            condition_select='python',
            condition_python="""contract = localdict.get('contract') or version
actual_salary = localdict.get('actual_salary', contract.total_salary if contract else 0)
result = (actual_salary - contract.total_salary) < 0""",
            appears_on_payslip='non_zero',
            amount_python_compute="""contract = localdict.get('contract') or version
actual_salary = localdict.get('actual_salary', contract.total_salary if contract else 0)
result_name = ''
if worked_days.get("Unpaid") or worked_days.get("Annual"):
    result_name += "Absent Days"
if worked_days.get("Out of Contract"):
    if result_name:
        result_name += " and "
    result_name += "Out of Contract days"
if result_name:
    result_name += " deduction"
result = actual_salary - contract.total_salary"""
        ))

        # 16. NET - Net Salary
        rules.append(make_rule_vals(
            name=_('Net Salary - صافي الراتب'),
            code='NET',
            sequence=200,
            category_id=cat_net_id,
            condition_select='none',
            condition_python=False,
            appears_on_payslip='always',
            amount_python_compute="""result = categories.get("BASIC", 0) + categories.get("ALW", 0) - categories.get("DED", 0)
result_name = 'Net Salary - صافي الراتب'"""
        ))

        return rules

    @api.model
    def _get_odoo20_rule_commands(self, country_id=None):
        vals_list = self._get_odoo20_salary_rules_vals(country_id=country_id)
        return [(0, 0, vals) for vals in vals_list]

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'rule_ids' in fields_list and res.get('use_odoo20_salary_rules', True):
            if not self.env.context.get('use_standard_structure_rules'):
                res['rule_ids'] = self._get_odoo20_rule_commands()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('use_odoo20_salary_rules', True) and not vals.get('rule_ids'):
                vals['rule_ids'] = self._get_odoo20_rule_commands()
        return super().create(vals_list)

    @api.onchange('use_odoo20_salary_rules')
    def _onchange_use_odoo20_salary_rules(self):
        if self.use_odoo20_salary_rules and not self.rule_ids:
            self.rule_ids = self._get_odoo20_rule_commands()

    def action_generate_odoo20_salary_rules(self):
        """Load or add missing Odoo 20 format salary rules onto the current structure, and update existing rules."""
        self.ensure_one()
        rule_commands = self._get_odoo20_rule_commands(country_id=self.country_id.id if self.country_id else None)
        existing_rules_by_code = {r.code: r for r in self.rule_ids}
        to_add = []
        updated_count = 0
        for cmd in rule_commands:
            vals = cmd[2]
            code = vals.get('code')
            if code in existing_rules_by_code:
                rule = existing_rules_by_code[code]
                rule.write({
                    'condition_python': vals.get('condition_python'),
                    'amount_python_compute': vals.get('amount_python_compute'),
                    'condition_select': vals.get('condition_select'),
                    'appears_on_payslip': vals.get('appears_on_payslip'),
                })
                updated_count += 1
            else:
                to_add.append(cmd)

        if to_add:
            self.write({'rule_ids': to_add})

        msg = _("%(added)s rule(s) added, %(updated)s rule(s) updated in Odoo 20 format.") % {
            'added': len(to_add),
            'updated': updated_count,
        }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Salary Rules'),
                'message': msg,
                'type': 'success',
                'sticky': False,
            }
        }
