# -*- coding: utf-8 -*-

from odoo import models, fields, api, _
import base64
import time
import io
import zipfile
import logging
from odoo.exceptions import UserError
from collections import defaultdict

_logger = logging.getLogger(__name__)


class SalaryTransferSheet(models.TransientModel):
    """
    Salary Transfer Sheet
    Generates a ZIP file containing one or more SIF (Salary Information File)
    reports, grouped by the WPS Sponsor specified on the employee.
    """

    _name = "salary.transfer.sheet"
    _description = "Salary Transfer Sheet"

    export_file = fields.Binary(
        string='File',
        readonly=True
    )
    bank_list = fields.Selection([
        ('hsb', 'HSBC Bank Middle East'),
        ('mar', 'Masraf Al Rayyan Bank'),
        ('qnb', 'Qatar National Bank'),
        ('cbq', 'Commercial Bank of Qatar')
    ], string='Bank', required=True, default='qnb')

    export_filename = fields.Char(
        string='File Name',
        help="Name of the export file generated for this transfer sheet",
        store=True,
        readonly=True
    )

    def _generate_sif_for_group(self, sponsor, sponsor_bank_ac, slips, bank_format):
        """
        Helper method to generate the SIF file content for a single group of payslips.
        Returns a tuple: (file_name, file_content_bytes)
        """
        data_lines = []

        # 1. Get Sponsor's Bank and Company Details
        bank_bic = sponsor_bank_ac.bank_bic or 'CONFIG_BIC'
        bank_acc_number = sponsor_bank_ac.account_number.replace(" ", "") or 'CONFIG_ACC'

        # Use sponsor's details from hr.wps.sponsor model
        employer_eid = sponsor.employer_eid or ''
        payer_eid = sponsor.payer_eid or ''
        payer_qid = sponsor.payer_qid or ''

        if not all([employer_eid, bank_bic != 'CONFIG_BIC', bank_acc_number != 'CONFIG_ACC']):
            _logger.warning(
                f"Skipping sponsor {sponsor.partner_id.name} due to incomplete WPS configuration (EID, QID, or Bank Details)."
            )
            return None, None

        # Fetch Salary Month from the first payslip in the group
        if slips and slips[0].date_from:
            salary_month_str = slips[0].date_from.strftime('%Y%m')
        else:
            salary_month_str = time.strftime('%Y%m')

        # 2. Generate Header Rows
        file_creation_date = time.strftime('%Y%m%d')
        file_creation_time = time.strftime('%H%M')

        header_cols_1 = [
            'Employer EID', 'File Creation Date', 'File Creation Time',
            'Payer EID', 'Payer QID', 'Payer Bank Short Name', 'Payer IBAN',
            'Salary Year and Month', 'Total Salaries', 'Total Records'
        ]
        header_data_1 = [
            employer_eid, file_creation_date, file_creation_time,
            payer_eid, payer_qid, bank_bic, bank_acc_number,
            salary_month_str, '__Total_Transfer__', '__Total_Records__'
        ]

        if bank_format in ('hsb', 'cbq'):
            header_cols_1.append('SIF Version')
            header_data_1.append('01')

        if bank_format == 'hsb':
            # Add HSBC specific additional header from hr.wps.sponsor model
            additional_header = sponsor.additional_header and time.strftime(sponsor.additional_header) or ''
            if additional_header:
                data_lines.append(additional_header)

        data_lines.append(','.join(header_cols_1))
        data_lines.append(','.join(header_data_1))

        # 3. Generate Data Column Header Row
        data_cols = [
            'Record Sequence', 'Employee QID', 'Employee Visa ID', 'Employee Name',
            'Employee Bank Short Name', 'Employee Account', 'Salary Frequency',
            'Number of Working days', 'Net Salary', 'Basic Salary', 'Extra hours',
            'Extra income', 'Deductions', 'Payment Type', 'Notes / Comments'
        ]
        if bank_format in ('hsb', 'cbq'):
            data_cols.extend([
                'Housing Allowance', 'Food Allowance', 'Transportation Allowance',
                'Overtime Allowance', 'Deduction Reason Code', 'Extra Field 1', 'Extra Field 2'
            ])

        data_lines.append(','.join(data_cols))

        # 4. Loop through Slips and Generate Data Rows
        total_rec = 0
        total_amount = 0
        for payslip in slips:
            if not payslip.employee_id.primary_bank_account_id:
                _logger.warning(
                    f"Skipping employee {payslip.employee_id.name} (in sponsor group {sponsor.partner_id.name}) due to missing bank account."
                )
                continue

            if not payslip.employee_id.identification_id:
                raise UserError(_(f"Please add QID/Identification No for employee {payslip.employee_id.name}."))

            total_rec += 1

            overtime_hours = 0
            total_working_days = 0
            unpaid_leaves = False

            for worked_days_line in payslip.worked_days_line_ids:
                if worked_days_line.code in (
                        'WORK100', 'Business', 'Leave_Reimbursement', 'Sick_Leave', 'Termination', 'Resignation'):
                    total_working_days += worked_days_line.number_of_days
                else:
                    unpaid_leaves = True

            basic = 0
            allowance = 0
            deduction = 0
            Housing_Allowance = 0
            Food_Allowance = 0
            Transportation_Allowance = 0
            Overtime_Allowance = 0
            Reason_Code = ''

            for line in payslip.line_ids:
                if line.code == 'BASIC':
                    basic += line.total
                elif line.code == 'ALW':
                    allowance += line.total
                elif line.code == 'DED':
                    deduction += line.total
                elif line.code == 'LOAN':
                    deduction += line.total

                if line.code == 'ACCO':
                    Housing_Allowance += line.total
                elif line.code == 'FOOD':
                    Food_Allowance += line.total
                elif line.code == 'TRANS':
                    Transportation_Allowance += line.total
                elif line.code == 'OT':
                    Overtime_Allowance += line.total

            net_salary = basic + allowance - deduction
            total_amount += round(net_salary)

            contract_wage = payslip.version_id.wage
            basic_diff = basic - contract_wage

            if deduction:
                Reason_Code = '04'
            if basic_diff < 0:
                Reason_Code = '01'
                deduction -= basic_diff
            else:
                allowance += basic_diff

            emp_id = payslip.employee_id.identification_id.strip()
            emp_qid = emp_id if len(emp_id) == 11 else ''
            emp_visa_id = emp_id if len(emp_id) != 11 else ''

            emp_bank_bic = payslip.employee_id.primary_bank_account_id.bank_bic or ''
            emp_bank_acc = payslip.employee_id.primary_bank_account_id.account_number.replace(" ", "") or ''

            data_line_items = [
                total_rec,
                emp_qid,
                emp_visa_id,
                payslip.employee_id.name,
                emp_bank_bic,
                emp_bank_acc,
                'M',
                int(total_working_days),
                round(net_salary),
                round(contract_wage),
                overtime_hours,
                round(allowance),
                round(deduction),
                'Normal Payment' if bank_format == 'hsb' else '',
                (payslip.note or '').replace(',', ';')
            ]

            if bank_format in ('hsb', 'cbq'):
                data_line_items.extend([
                    Housing_Allowance,
                    Food_Allowance,
                    Transportation_Allowance,
                    Overtime_Allowance,
                    Reason_Code,
                    '',
                    ''
                ])

            data_lines.append(','.join(map(str, data_line_items)))

        if total_rec == 0:
            _logger.info(f"No valid payslips found for sponsor {sponsor.partner_id.name}. No file generated.")
            return None, None

        # 5. Finalize file content
        data = '\n'.join(data_lines)
        data = data.replace('__Total_Records__', str(total_rec))
        data = data.replace('__Total_Transfer__', str(total_amount))

        file_name = f'SIF_{payer_eid}_{bank_bic}_{file_creation_date}_{file_creation_time}.csv'

        return file_name, data.encode('utf-8')

    def generate_transfer_sheet(self, given_payroll_reg=False):
        """
        Main method to generate SIF files.
        Collects payslips dynamically from context (runs or direct slips),
        groups them by sponsor, and generates files.
        """
        context = self.env.context
        active_model = context.get('active_model')
        active_ids = context.get('active_ids', [])

        slips = self.env['hr.payslip']

        # Determine which payslips to process based on context/arguments
        if given_payroll_reg:
            slips = given_payroll_reg.mapped('slip_ids')
        elif active_model == 'hr.payslip.run' and active_ids:
            runs = self.env['hr.payslip.run'].browse(active_ids)
            slips = runs.mapped('slip_ids')
        elif active_model == 'hr.payslip' and active_ids:
            slips = self.env['hr.payslip'].browse(active_ids)
        elif context.get('active_id'):
            # Fallback if triggered from a single form view without explicit active_model
            run = self.env['hr.payslip.run'].browse(context.get('active_id'))
            slips = run.mapped('slip_ids')
        if not slips:
            raise UserError(_("No payslips found to process."))

        # -- DATE VALIDATION --
        date_ranges = set((slip.date_from, slip.date_to) for slip in slips)
        if len(date_ranges) > 1:
            raise UserError(
                _("All selected payslips or pay runs must have the exact same start and end dates. Please adjust your selection."))

        # Group payslips by sponsor
        grouped_slips = defaultdict(list)
        for slip in slips:
            sponsor = slip.employee_id.wps_sponsor_id

            if not sponsor:
                # Fallback check if company has a sponsor mapped (optional customization)
                if hasattr(slip.company_id.partner_id, 'wps_sponsor_id') and slip.company_id.partner_id.wps_sponsor_id:
                    sponsor = slip.company_id.partner_id.wps_sponsor_id
                else:
                    raise UserError(_("WPS Sponsor is missing for employee %s.") % slip.employee_id.name)

            grouped_slips[sponsor].append(slip)

        generated_files = []

        for sponsor, sponsor_slips in grouped_slips.items():
            # Extract the bank_account_id directly from the hr.wps.sponsor record
            sponsor_bank_ac = sponsor.bank_account_id
            if not sponsor_bank_ac:
                _logger.warning(
                    f"WPS SIF generation skipped for sponsor {sponsor.partner_id.name} (ID: {sponsor.id}) "
                    f"due to missing bank account on their WPS Sponsor record."
                )
                continue

            file_name, file_content = self._generate_sif_for_group(
                sponsor, sponsor_bank_ac, sponsor_slips, self.bank_list
            )
            if file_name and file_content:
                generated_files.append((file_name, file_content))

        if not generated_files:
            raise UserError(
                _("No valid employee payslips or sponsor configurations found. No SIF files were generated."))

        final_filename = ""
        final_b64_content = None

        if len(generated_files) == 1:
            # --- SINGLE FILE CASE ---
            file_name, file_content = generated_files[0]
            final_filename = file_name
            final_b64_content = base64.b64encode(file_content)

        else:
            # --- MULTIPLE FILES CASE (ZIP Logic) ---
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'a', zipfile.ZIP_DEFLATED, False) as zip_file:
                for file_name, file_content in generated_files:
                    zip_file.writestr(file_name, file_content)

            final_b64_content = base64.b64encode(zip_buffer.getvalue())

            # Formulate the zip filename dynamically
            run_names = list(set(slips.mapped('payslip_run_id.name')))
            name_prefix = run_names[0].replace(' ', '_') if len(run_names) == 1 and run_names[0] else 'Multiple_Runs'
            final_filename = f"WPS_SIF_Files_{name_prefix}_{time.strftime('%Y%m%d')}.zip"

        if given_payroll_reg:
            return (final_filename, final_b64_content)
        else:
            self.export_file = final_b64_content.decode('utf-8')
            self.export_filename = final_filename

            return {
                "view_mode": "form",
                "res_model": "salary.transfer.sheet",
                "res_id": self.id,
                "type": "ir.actions.act_window",
                "target": "new",
                "context": self.env.context,
                "nodestroy": True,
            }