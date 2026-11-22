# -*- coding: utf-8 -*-
from odoo import api, models, _
from odoo.exceptions import UserError


def _resolve_version_for_employee(env, employee, context_version_id=None):
    """
    Determine the version to use for an employee:
    1. If context_version_id is given (or in env.context), check if it belongs to this employee.
    2. Check employee.version_id.
    3. Check employee.current_version_id.
    4. Search latest active version (order='date_version desc, id desc').
    5. Search latest archived version (active_test=False).
    """
    if not context_version_id:
        context_version_id = env.context.get('version_id')

    if context_version_id:
        v = env['hr.version'].browse(context_version_id).exists()
        if v and v.employee_id.id == employee.id:
            return v

    if employee.version_id:
        return employee.version_id

    if hasattr(employee, 'current_version_id') and employee.current_version_id:
        return employee.current_version_id

    latest = env['hr.version'].search([
        ('employee_id', '=', employee.id),
    ], order='date_version desc, id desc', limit=1)
    if latest:
        return latest

    archived = env['hr.version'].with_context(active_test=False).search([
        ('employee_id', '=', employee.id),
    ], order='date_version desc, id desc', limit=1)
    if archived:
        return archived

    raise UserError(_("No contract or version found for employee '%s'.") % employee.name)


def _get_salary_certificate_docs(env, docids, data=None):
    """
    Resolve the hr.version recordset to render the salary certificate.
    Handles:
    - Explicit version_id or version_ids passed in `data`.
    - Context version_id (e.g. from version timeline or action_open_version).
    - Printing from hr.employee records (docids are hr.employee IDs).
    - Printing directly from hr.version records (docids are hr.version IDs).
    """
    if not docids and data:
        if data.get('version_id'):
            return env['hr.version'].browse(data['version_id'])
        if data.get('version_ids'):
            return env['hr.version'].browse(data['version_ids'])

    # 1. Explicit version in data
    if data and data.get('version_id'):
        version = env['hr.version'].browse(data['version_id']).exists()
        if version:
            return version
    if data and data.get('version_ids'):
        versions = env['hr.version'].browse(data['version_ids']).exists()
        if versions:
            return versions

    # 2. Check context version_id
    context_version_id = env.context.get('version_id')
    if context_version_id:
        v = env['hr.version'].browse(context_version_id).exists()
        if v:
            # If docids is [v.id] or matches the employee of this version
            if (docids and v.id in docids) or (v.employee_id and v.employee_id.id in docids):
                return v

    # 3. Check active_model and docids
    active_model = env.context.get('active_model')

    if active_model == 'hr.employee':
        versions = env['hr.version']
        for emp in env['hr.employee'].browse(docids):
            versions |= _resolve_version_for_employee(env, emp, context_version_id)
        return versions

    # If active_model == 'hr.version' but the user opened the employee form from a contract list
    # (in which case docids are employee IDs, not version IDs):
    employees = env['hr.employee'].browse(docids).exists()
    if employees and len(employees) == len(docids):
        if active_model == 'hr.version':
            versions = env['hr.version'].browse(docids).exists()
            if len(versions) == len(docids) and not context_version_id and env.context.get('active_ids') == docids:
                return versions

        versions = env['hr.version']
        for emp in employees:
            versions |= _resolve_version_for_employee(env, emp, context_version_id)
        return versions

    return env['hr.version'].browse(docids)


class ReportSalaryCertificate(models.AbstractModel):
    _name = 'report.wps_report.salary_certificate_report'
    _description = 'Salary Certificate Report'

    @api.model
    def _get_report_values(self, docids, data=None):
        versions = _get_salary_certificate_docs(self.env, docids, data=data)
        return {
            'doc_ids': versions.ids,
            'doc_model': 'hr.version',
            'docs': versions,
        }


class ReportSalaryCertificateTB(models.AbstractModel):
    _name = 'report.wps_report.salary_certificate_report_tb'
    _description = 'Salary Certificate to Bank Report'

    @api.model
    def _get_report_values(self, docids, data=None):
        versions = _get_salary_certificate_docs(self.env, docids, data=data)
        return {
            'doc_ids': versions.ids,
            'doc_model': 'hr.version',
            'docs': versions,
        }
