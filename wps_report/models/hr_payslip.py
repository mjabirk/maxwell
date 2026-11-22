# -*- coding: utf-8 -*-
from odoo import models, api, _


class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    def action_generate_notes(self):
        for slip in self:
            note_content = ''
            for line in slip.worked_days_line_ids:
                if line.name in ('Annual', 'Out of Contract', 'Unpaid', 'Sick Time Off'):
                    # Added a strip() to ensure clean formatting
                    note_content += f' {line.number_of_days} days {line.name}.'
                if line.name in ('Resignation',):
                    note_content += ' Employee has resigned.'
                if line.name in ('Termination',):
                    note_content += ' End of the contract.'

            if note_content:
                note_content = f'"{note_content.strip()}"'
                slip.note = note_content
        return True

    @api.model_create_multi
    def create(self, vals_list):
        payslips = super().create(vals_list)
        # Trigger after creating the payslip
        payslips.action_generate_notes()
        return payslips

    def compute_sheet(self):
        res = super().compute_sheet()
        # Trigger after clicking 'Compute Sheet'
        self.action_generate_notes()
        return res
    def _get_localdict(self, work_entries=None):
        localdict = super()._get_localdict(work_entries=work_entries)
        localdict['contract'] = self.version_id
        return localdict
