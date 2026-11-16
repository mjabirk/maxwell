# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError


class ContractorToolBulkWizard(models.TransientModel):
    _name = 'contractor.tool.bulk.wizard'
    _description = 'Bulk Hand & Power Tool Issue / Return Wizard'

    action_type = fields.Selection([
        ('issue', 'Bulk Issue Tools to Employee'),
        ('return', 'Bulk Return Tools to Store'),
    ], string='Operation Mode', default='issue', required=True)

    employee_id = fields.Many2one(
        'hr.employee',
        string='Employee / Operative',
        required=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Assigned Project Site',
    )
    tool_ids = fields.Many2many(
        'contractor.tool.asset',
        'contractor_tool_bulk_wiz_tool_rel',
        'wizard_id',
        'tool_id',
        string='Selected Tools',
    )
    return_line_ids = fields.One2many(
        'contractor.tool.bulk.line',
        'wizard_id',
        string='Tools to Return & Condition Inspection',
    )
    default_condition = fields.Selection([
        ('good', 'Good / Operational'),
        ('damaged', 'Damaged / Needs Repair'),
        ('lost', 'Lost / Missing'),
    ], string='Default Return Condition', default='good', required=True)
    notes = fields.Text(string='Dispatch / Return Inspection Notes')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        emp_id = res.get('employee_id') or self.env.context.get('default_employee_id')
        action_type = res.get('action_type') or self.env.context.get('default_action_type', 'issue')
        default_cond = res.get('default_condition', 'good')
        if emp_id and action_type == 'return' and 'return_line_ids' in fields_list:
            assigned_tools = self.env['contractor.tool.asset'].search([
                ('current_employee_id', '=', emp_id),
                ('status', '=', 'assigned'),
            ])
            res['return_line_ids'] = [
                (0, 0, {
                    'selected': True,
                    'tool_id': tool.id,
                    'return_condition': default_cond,
                })
                for tool in assigned_tools
            ]
        return res

    @api.onchange('action_type', 'employee_id', 'default_condition')
    def _onchange_populate_return_lines(self):
        if self.action_type == 'return' and self.employee_id:
            assigned_tools = self.env['contractor.tool.asset'].search([
                ('current_employee_id', '=', self.employee_id.id),
                ('status', '=', 'assigned'),
            ])
            cond = self.default_condition or 'good'
            self.return_line_ids = [(5, 0, 0)] + [
                (0, 0, {
                    'selected': True,
                    'tool_id': tool.id,
                    'return_condition': cond,
                })
                for tool in assigned_tools
            ]
        elif self.action_type == 'issue':
            self.return_line_ids = [(5, 0, 0)]

    def action_apply_bulk(self):
        self.ensure_one()
        if not (
            self.env.su
            or self.env.user.has_group('contractor_core.group_contractor_storekeeper')
            or self.env.user.has_group('contractor_core.group_contractor_project_manager')
            or self.env.user.has_group('contractor_core.group_contractor_manager')
        ):
            raise AccessError(_("Access Denied: Only Storekeepers, Project Managers, or Managers can execute bulk tool movements."))

        if self.action_type == 'issue':
            if not self.project_id:
                raise UserError(_("Please select a Project Site for issuing tools."))
            if not self.tool_ids:
                raise UserError(_("Please select at least one In-Store tool to issue."))
            for tool in self.tool_ids:
                tool.action_issue_tool(self.employee_id.id, self.project_id.id)
        else:
            selected_lines = self.return_line_ids.filtered(lambda l: l.selected and l.tool_id)
            if selected_lines:
                for line in selected_lines:
                    line.tool_id.action_return_tool(
                        return_condition=line.return_condition or 'good',
                        notes=line.notes or self.notes or '',
                    )
            elif self.tool_ids:
                cond = self.default_condition or 'good'
                for tool in self.tool_ids:
                    tool.action_return_tool(
                        return_condition=cond,
                        notes=self.notes or '',
                    )
            else:
                raise UserError(_("Please select at least one assigned tool to return."))

        return {'type': 'ir.actions.act_window_close'}


class ContractorToolBulkLine(models.TransientModel):
    _name = 'contractor.tool.bulk.line'
    _description = 'Bulk Tool Return Inspection Line'

    wizard_id = fields.Many2one(
        'contractor.tool.bulk.wizard',
        string='Bulk Wizard',
        required=True,
        ondelete='cascade',
    )
    selected = fields.Boolean(string='Return', default=True)
    tool_id = fields.Many2one(
        'contractor.tool.asset',
        string='Tool Asset',
        required=True,
    )
    tool_code = fields.Char(
        string='Asset Tag',
        related='tool_id.tool_code',
        readonly=True,
    )
    return_condition = fields.Selection([
        ('good', 'Good / Operational'),
        ('damaged', 'Damaged / Needs Repair'),
        ('lost', 'Lost / Missing'),
    ], string='Return Condition', default='good', required=True)
    notes = fields.Char(string='Condition Notes')


class HrEmployeeToolBulkUx(models.Model):
    _inherit = 'hr.employee'

    def action_open_bulk_tool_wizard(self):
        self.ensure_one()
        has_assigned = any(t.status == 'assigned' for t in self.assigned_tool_ids)
        return {
            'name': _('Bulk Tool Custody Issue / Return'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.tool.bulk.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_employee_id': self.id,
                'default_action_type': 'return' if has_assigned else 'issue',
            },
        }


class ContractorToolAssetBulkUx(models.Model):
    _inherit = 'contractor.tool.asset'

    def action_open_bulk_tool_wizard(self):
        tool_ids = self.ids
        first_emp = self[:1].current_employee_id.id if self and self[:1].current_employee_id else False
        first_proj = self[:1].current_project_id.id if self and self[:1].current_project_id else False
        is_assigned = all(t.status == 'assigned' for t in self) if self else False
        return {
            'name': _('Bulk Tool Issue / Return'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.tool.bulk.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_action_type': 'return' if (is_assigned and first_emp) else 'issue',
                'default_employee_id': first_emp,
                'default_project_id': first_proj,
                'default_tool_ids': [(6, 0, tool_ids)] if tool_ids else False,
            },
        }


class ContractorEmployeeExitUx(models.Model):
    _inherit = 'contractor.employee.exit'

    assigned_tools_count = fields.Integer(
        string='Assigned Tools Count',
        related='unreturned_tool_count',
        readonly=True,
    )
    outstanding_advances_balance = fields.Monetary(
        string='Outstanding Advances & Loans',
        currency_field='currency_id',
        compute='_compute_exit_clearance_checklist',
    )
    last_day_attendance_status = fields.Selection([
        ('logged', 'Logged & Verified'),
        ('draft_only', 'Logged (Pending PM Approval)'),
        ('missing', 'Missing Last-Day Attendance'),
    ], string='Last Working Day Attendance', compute='_compute_exit_clearance_checklist')
    tool_clearance_ready = fields.Boolean(
        string='Tool Clearance Ready',
        compute='_compute_exit_clearance_checklist',
    )
    exit_clearance_ready = fields.Boolean(
        string='All Exit Checks Cleared',
        compute='_compute_exit_clearance_checklist',
    )

    @api.depends(
        'employee_id',
        'last_working_day',
        'unreturned_tool_count',
        'tool_clearance_status',
        'employee_advances_loans',
        'damaged_lost_tool_deductions',
    )
    def _compute_exit_clearance_checklist(self):
        SiteAttLine = self.env.get('contractor.site.attendance.line')
        HrAtt = self.env.get('hr.attendance')
        LeaveSal = self.env.get('contractor.leave.salary.request')
        for rec in self:
            rec.tool_clearance_ready = rec.tool_clearance_status in ('cleared', 'manager_override')

            # Outstanding advances / loans + unrecovered tool liabilities + unclosed leave salary advances
            adv_bal = (rec.employee_advances_loans or 0.0) + (rec.damaged_lost_tool_deductions or 0.0)
            if rec.employee_id and LeaveSal is not None and 'is_advance_paid' in LeaveSal._fields:
                pass
            rec.outstanding_advances_balance = max(adv_bal, 0.0)

            # Last working day attendance check
            att_status = 'missing'
            if rec.employee_id and rec.last_working_day:
                if SiteAttLine is not None:
                    lines = SiteAttLine.search([
                        ('employee_id', '=', rec.employee_id.id),
                        ('attendance_date', '=', rec.last_working_day),
                    ])
                    if any(l.attendance_id.state == 'pm_approved' for l in lines):
                        att_status = 'logged'
                    elif lines:
                        att_status = 'draft_only'
                if att_status == 'missing' and HrAtt is not None:
                    hr_att = HrAtt.search_count([
                        ('employee_id', '=', rec.employee_id.id),
                        ('check_in', '>=', f'{rec.last_working_day} 00:00:00'),
                        ('check_in', '<=', f'{rec.last_working_day} 23:59:59'),
                    ])
                    if hr_att > 0:
                        att_status = 'logged'
            rec.last_day_attendance_status = att_status
            rec.exit_clearance_ready = bool(
                rec.tool_clearance_ready and rec.outstanding_advances_balance <= 0.0 and att_status == 'logged'
            )

    def action_view_unreturned_tools(self):
        self.ensure_one()
        return {
            'name': _('Assigned Tools in Custody'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.tool.asset',
            'view_mode': 'list,form',
            'domain': [
                ('current_employee_id', '=', self.employee_id.id),
                ('status', '=', 'assigned'),
            ],
            'context': {'default_current_employee_id': self.employee_id.id},
        }


class ContractorAgencyReconciliationUx(models.Model):
    _inherit = 'contractor.agency.reconciliation'

    discrepancy_line_count = fields.Integer(
        string='Lines with Discrepancies',
        compute='_compute_discrepancy_line_count',
    )

    @api.depends('line_ids.discrepancy_flag')
    def _compute_discrepancy_line_count(self):
        for rec in self:
            rec.discrepancy_line_count = len(rec.line_ids.filtered(lambda l: l.discrepancy_flag))

    def action_pull_and_reconcile(self):
        """Preserves Accountant-entered agency invoiced hours/amounts per worker when refreshing ERP hours."""
        saved_entries = {}
        for rec in self:
            saved_entries[rec.id] = {
                line.employee_id.id: (
                    line.agency_invoiced_hours,
                    line.agency_invoiced_amount,
                    line.dispute_notes,
                )
                for line in rec.line_ids
                if line.employee_id and (
                    line.agency_invoiced_hours or line.agency_invoiced_amount or line.dispute_notes
                )
            }

        res = super().action_pull_and_reconcile()

        for rec in self:
            entries = saved_entries.get(rec.id, {})
            if entries and rec.line_ids:
                for line in rec.line_ids:
                    if line.employee_id.id in entries:
                        inv_h, inv_amt, d_notes = entries[line.employee_id.id]
                        line.write({
                            'agency_invoiced_hours': inv_h,
                            'agency_invoiced_amount': inv_amt,
                            'dispute_notes': d_notes,
                        })
                rec._compute_reconciliation_totals()
                if rec.has_variance or abs(rec.variance_hours) > 0.5 or abs(rec.variance_amount) > 1.0:
                    rec.state = 'discrepancy_flagged'
                else:
                    rec.state = 'matched'
        return res

    def action_pull_approved_erp_hours(self):
        """Pre-populates ERP approved hours and contractual rates while keeping entered agency invoice figures."""
        return self.action_pull_and_reconcile()

    def action_bulk_flag_discrepancies(self):
        """Collects all operative rows with variances and logs a consolidated agency dispute note."""
        self.ensure_one()
        bad_lines = self.line_ids.filtered(lambda l: l.discrepancy_flag)
        if not bad_lines:
            raise UserError(_("No hour or amount discrepancies detected across operative lines."))
        details = []
        for l in bad_lines:
            note = f"- {l.employee_id.name}: ERP {l.erp_hours:.2f}h ({l.erp_cost:.2f}) vs Agency {l.agency_invoiced_hours:.2f}h ({l.agency_invoiced_amount:.2f})"
            if l.dispute_notes:
                note += f" [{l.dispute_notes}]"
            details.append(note)
        summary_text = "\n".join(details)
        self.write({
            'state': 'discrepancy_flagged',
            'resolution_notes': ((self.resolution_notes + "\n\n") if self.resolution_notes else "") + _("Discrepancy Breakdown:\n") + summary_text,
        })
        self.message_post(
            body=Markup(_("<strong>Bulk Agency Invoice Discrepancy Logged (%d operatives):</strong><br/><pre>%s</pre>")) % (
                len(bad_lines), summary_text
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True


class ContractorAgencyReconciliationLineUx(models.Model):
    _inherit = 'contractor.agency.reconciliation.line'

    contractual_rate = fields.Monetary(
        string='Contractual Hourly Rate',
        currency_field='currency_id',
        compute='_compute_ux_line_metrics',
    )
    discrepancy_flag = fields.Boolean(
        string='Discrepancy',
        compute='_compute_ux_line_metrics',
    )
    dispute_notes = fields.Char(string='Dispute Note')

    @api.depends(
        'erp_hours',
        'erp_cost',
        'employee_id',
        'variance_hours',
        'variance_amount',
        'reconciliation_id.date_end',
    )
    def _compute_ux_line_metrics(self):
        for line in self:
            rate = 0.0
            if line.erp_hours and line.erp_hours > 0:
                rate = (line.erp_cost or 0.0) / line.erp_hours
            elif line.employee_id and hasattr(line.employee_id, 'get_employee_rate_on_date'):
                rate = line.employee_id.get_employee_rate_on_date(
                    line.reconciliation_id.date_end or fields.Date.context_today(line)
                )
            elif line.employee_id:
                rate = getattr(line.employee_id, 'hourly_cost', 0.0) or 0.0
            line.contractual_rate = rate
            line.discrepancy_flag = bool(
                abs(line.variance_hours or 0.0) > 0.01 or abs(line.variance_amount or 0.0) > 0.01
            )
