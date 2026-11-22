# -*- coding: utf-8 -*-

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    leave_ids = fields.One2many('hr.leave', 'employee_id')
    x_leave_ids = fields.One2many('hr.leave', 'employee_id', string='Leaves (Legacy)')
    joining_date = fields.Date(string='Joining Date')
    wps_sponsor_id = fields.Many2one(
        'hr.wps.sponsor',
        string="WPS Sponsor",
        ondelete='restrict',  # Prevents deleting a sponsor if employees are linked
        tracking=True,
        help="Sponsor responsible for the employee's WPS salary transfer."
    )

    def _get_target_version_for_salary_certificate(self):
        """
        Determine the appropriate version for this employee:
        1. Context 'version_id' if matching this employee.
        2. Currently displayed version on the employee record (self.version_id).
        3. Current active version (self.current_version_id).
        4. Latest active version ordered by date_version desc, id desc.
        5. Latest archived version fallback.
        """
        self.ensure_one()
        context_version_id = self.env.context.get('version_id')
        if context_version_id:
            v = self.env['hr.version'].browse(context_version_id).exists()
            if v and v.employee_id.id == self.id:
                return v

        if self.version_id:
            return self.version_id

        if hasattr(self, 'current_version_id') and self.current_version_id:
            return self.current_version_id

        latest = self.env['hr.version'].search([
            ('employee_id', '=', self.id),
        ], order='date_version desc, id desc', limit=1)
        if latest:
            return latest

        archived = self.env['hr.version'].with_context(active_test=False).search([
            ('employee_id', '=', self.id),
        ], order='date_version desc, id desc', limit=1)
        if archived:
            return archived

        raise UserError(_("No contract or version found for employee '%s'.") % self.name)

    def action_print_salary_certificate(self):
        self.ensure_one()
        version = self._get_target_version_for_salary_certificate()
        return self.env.ref('wps_report.salary_certificate_reg').with_context(
            active_model='hr.employee',
            active_id=self.id,
            active_ids=[self.id],
            version_id=version.id,
        ).report_action(
            self,
            data={
                'version_id': version.id,
                'employee_id': self.id,
            }
        )

    def action_print_salary_certificate_bank(self):
        self.ensure_one()
        version = self._get_target_version_for_salary_certificate()
        return self.env.ref('wps_report.salary_certificate_reg_tb').with_context(
            active_model='hr.employee',
            active_id=self.id,
            active_ids=[self.id],
            version_id=version.id,
        ).report_action(
            self,
            data={
                'version_id': version.id,
                'employee_id': self.id,
            }
        )

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
