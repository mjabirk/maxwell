# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorToolAsset(models.Model):
    _name = 'contractor.tool.asset'
    _description = 'Contractor Personal Hand & Power Tool Asset'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'tool_code desc, id desc'

    name = fields.Char(
        string='Tool Description / Model',
        required=True,
        tracking=True,
    )
    tool_code = fields.Char(
        string='Tool Serial / Asset Tag',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Tool'),
        index=True,
    )
    tool_type = fields.Selection([
        ('hand_tool', 'Hand Tool (Wrench, Hammer, Level)'),
        ('power_tool', 'Power Tool (Drill, Grinder, Breaker)'),
        ('measuring_instrument', 'Measuring / Surveying Instrument'),
        ('safety_equipment', 'PPE / Specialized Safety Gear'),
        ('heavy_tool', 'Light Plant & Compactor'),
    ], string='Tool Category', default='power_tool', required=True, tracking=True)

    serial_number = fields.Char(
        string='Manufacturer Serial Number',
        tracking=True,
    )
    purchase_date = fields.Date(
        string='Purchase Date',
        default=fields.Date.context_today,
    )
    purchase_value = fields.Monetary(
        string='Replacement / Purchase Value',
        currency_field='currency_id',
        required=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        required=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    condition = fields.Selection([
        ('good', 'Good / Operational'),
        ('damaged', 'Damaged / Needs Repair'),
        ('lost', 'Lost / Missing'),
    ], string='Physical Condition', default='good', required=True, tracking=True)

    status = fields.Selection([
        ('in_store', 'In Central / Site Store'),
        ('assigned', 'In Field Custody (Assigned)'),
        ('under_repair', 'Under Repair'),
        ('written_off', 'Scrapped / Written Off'),
    ], string='Custody Status', default='in_store', required=True, tracking=True, copy=False)

    current_employee_id = fields.Many2one(
        'hr.employee',
        string='Current Custodian Operative',
        tracking=True,
        copy=False,
    )
    current_project_id = fields.Many2one(
        'project.project',
        string='Assigned Project Site',
        tracking=True,
        copy=False,
    )

    assignment_history_ids = fields.One2many(
        'contractor.tool.assignment.line',
        'tool_id',
        string='Custody & Transfer History',
        copy=False,
    )

    # Damage / Loss Deduction Tracking
    deduction_amount = fields.Monetary(
        string='Assessed Liability Deduction',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    deduction_status = fields.Selection([
        ('none', 'No Deduction'),
        ('pending_recovery', 'Pending Payroll Recovery'),
        ('recovered', 'Fully Recovered from Wages'),
        ('waived', 'Management Waived'),
    ], string='Deduction Status', default='none', tracking=True, copy=False)

    deduction_reason = fields.Text(
        string='Deduction / Loss Justification',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('tool_code', _('New Tool')) == _('New Tool'):
                vals['tool_code'] = self.env['ir.sequence'].next_by_code('contractor.tool.asset') or _('New Tool')
        return super().create(vals_list)

    def action_issue_tool(self, employee_id, project_id):
        """Storekeeper issues tool to an operative on a specific project site."""
        self.ensure_one()
        if self.status != 'in_store':
            raise UserError(_("Tool '%s' is not in store (Current Status: %s).") % (self.name, self.status))

        emp = self.env['hr.employee'].browse(employee_id)
        proj = self.env['project.project'].browse(project_id)

        self.env['contractor.tool.assignment.line'].create({
            'tool_id': self.id,
            'employee_id': emp.id,
            'project_id': proj.id,
            'assigned_date': fields.Date.context_today(self),
            'issued_by_id': self.env.user.id,
        })

        self.write({
            'status': 'assigned',
            'current_employee_id': emp.id,
            'current_project_id': proj.id,
        })

        self.message_post(
            body=Markup(_("<strong>Tool Issued to Field Custody:</strong> Assigned to %s on Project %s.")) % (emp.name, proj.name),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_transfer_tool(self, new_employee_id, new_project_id):
        """Transfers tool between operatives, preserving the full immutable chain of custody."""
        self.ensure_one()
        if self.status != 'assigned':
            raise UserError(_("Tool must be in 'assigned' status to execute an inter-employee transfer."))

        new_emp = self.env['hr.employee'].browse(new_employee_id)
        new_proj = self.env['project.project'].browse(new_project_id)

        # Close out active assignment line
        active_lines = self.assignment_history_ids.filtered(lambda l: not l.returned_date)
        if active_lines:
            active_lines.write({
                'returned_date': fields.Date.context_today(self),
                'received_by_id': self.env.user.id,
                'notes': _("Transferred custody to %s on %s") % (new_emp.name, new_proj.name),
            })

        # Create new assignment line
        self.env['contractor.tool.assignment.line'].create({
            'tool_id': self.id,
            'employee_id': new_emp.id,
            'project_id': new_proj.id,
            'assigned_date': fields.Date.context_today(self),
            'issued_by_id': self.env.user.id,
        })

        old_emp_name = self.current_employee_id.name
        self.write({
            'current_employee_id': new_emp.id,
            'current_project_id': new_proj.id,
        })

        self.message_post(
            body=Markup(_("<strong>Custody Transferred:</strong> Transferred from %s to %s on Project %s.")) % (
                old_emp_name, new_emp.name, new_proj.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_return_tool(self, return_condition='good', notes=''):
        """Storekeeper acknowledges tool return, logs physical condition, and triggers liability if damaged/lost."""
        self.ensure_one()
        active_lines = self.assignment_history_ids.filtered(lambda l: not l.returned_date)
        prev_emp = self.current_employee_id

        if active_lines:
            active_lines.write({
                'returned_date': fields.Date.context_today(self),
                'received_by_id': self.env.user.id,
                'return_condition': return_condition,
                'notes': notes,
            })

        self.condition = return_condition

        if return_condition == 'good':
            self.write({
                'status': 'in_store',
                'current_employee_id': False,
                'current_project_id': False,
            })
            self.message_post(
                body=Markup(_("<strong>Tool Returned to Store:</strong> Received in good operational condition.")),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
        else:
            # Damaged or Lost
            new_status = 'under_repair' if return_condition == 'damaged' else 'written_off'
            self.write({
                'status': new_status,
                'current_employee_id': False,
                'current_project_id': False,
            })
            self.message_post(
                body=Markup(_(
                    "<div class='alert alert-warning'><strong>Tool Returned %s:</strong> Custodian was %s.<br/>"
                    "Assessed for repair/write-off and potential employee deduction. Notes: %s</div>"
                )) % (return_condition.upper(), prev_emp.name if prev_emp else 'Unknown', notes),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )

    def action_book_employee_deduction(self, amount, reason):
        """Books damage/loss liability on employee for payroll or exit settlement recovery."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize tool damage deductions."))

        # Identify custodian from last return line
        last_line = self.assignment_history_ids.sorted('id', reverse=True)
        if not last_line:
            raise UserError(_("No assignment history found for this tool."))

        custodian = last_line[0].employee_id
        if not custodian:
            raise UserError(_("No custodian employee found to apply deduction."))

        self.write({
            'deduction_amount': amount,
            'deduction_status': 'pending_recovery',
            'deduction_reason': reason,
        })
        custodian.pending_tool_deduction_amount += amount

        self.message_post(
            body=Markup(_("<strong>Employee Liability Booked:</strong> %s %.2f billed to %s for %s.<br/>Reason: %s")) % (
                self.currency_id.symbol or '', amount, custodian.name, self.condition, reason
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_open_issue_wizard(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_storekeeper') or self.env.user.has_group('contractor_core.group_contractor_project_manager')):
            raise AccessError(_("Access Denied: Only Storekeepers, Project Managers, or Managers can issue tools."))
        return {
            'name': _('Issue Tool to Operative'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.tool.action.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_tool_id': self.id,
                'default_action_type': 'issue',
            },
        }

    def action_open_transfer_wizard(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_storekeeper') or self.env.user.has_group('contractor_core.group_contractor_project_manager')):
            raise AccessError(_("Access Denied: Only Storekeepers, Project Managers, or Managers can transfer tools."))
        return {
            'name': _('Transfer Tool Custody'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.tool.action.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_tool_id': self.id,
                'default_action_type': 'transfer',
            },
        }

    def action_open_return_wizard(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_storekeeper') or self.env.user.has_group('contractor_core.group_contractor_project_manager')):
            raise AccessError(_("Access Denied: Only Storekeepers, Project Managers, or Managers can record tool returns."))
        return {
            'name': _('Return Tool to Store'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.tool.action.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_tool_id': self.id,
                'default_action_type': 'return',
            },
        }

    def action_open_deduct_wizard(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Manager can authorize tool damage deductions."))
        return {
            'name': _('Book Employee Damage / Loss Liability'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.tool.action.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_tool_id': self.id,
                'default_action_type': 'deduct',
                'default_amount': self.purchase_value,
            },
        }


class ContractorToolAssignmentLine(models.Model):
    _name = 'contractor.tool.assignment.line'
    _description = 'Tool Custody Assignment History Line'
    _order = 'assigned_date desc, id desc'

    tool_id = fields.Many2one(
        'contractor.tool.asset',
        string='Tool Asset',
        required=True,
        ondelete='cascade',
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Custodian Operative',
        required=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project Site',
        required=True,
    )
    assigned_date = fields.Date(
        string='Issued Date',
        default=fields.Date.context_today,
        required=True,
    )
    returned_date = fields.Date(
        string='Returned Date',
    )
    issued_by_id = fields.Many2one(
        'res.users',
        string='Issued By Storekeeper',
        default=lambda self: self.env.user,
    )
    received_by_id = fields.Many2one(
        'res.users',
        string='Received By Storekeeper',
    )
    return_condition = fields.Selection([
        ('good', 'Good Condition'),
        ('damaged', 'Damaged Condition'),
        ('lost', 'Lost / Not Returned'),
    ], string='Condition Upon Return')

    notes = fields.Char(
        string='Remarks / Transfer Notes',
    )


class ContractorToolActionWizard(models.TransientModel):
    _name = 'contractor.tool.action.wizard'
    _description = 'Tool Custody Action Wizard'

    tool_id = fields.Many2one('contractor.tool.asset', string='Tool Asset', required=True)
    action_type = fields.Selection([
        ('issue', 'Issue Tool'),
        ('transfer', 'Transfer Custody'),
        ('return', 'Return to Store'),
        ('deduct', 'Book Damage / Loss Deduction'),
    ], string='Action', required=True)
    employee_id = fields.Many2one('hr.employee', string='Recipient Operative')
    project_id = fields.Many2one('project.project', string='Project Site')
    return_condition = fields.Selection([
        ('good', 'Good / Operational'),
        ('damaged', 'Damaged / Needs Repair'),
        ('lost', 'Lost / Missing'),
    ], string='Return Physical Condition', default='good')
    notes = fields.Char(string='Remarks / Condition Notes')
    amount = fields.Float(string='Assessed Damage / Loss Liability')
    reason = fields.Text(string='Reason for Liability Deduction')

    def action_confirm(self):
        self.ensure_one()
        if self.action_type == 'issue':
            if not self.employee_id or not self.project_id:
                raise UserError(_("Recipient Operative and Project Site are mandatory to issue a tool."))
            self.tool_id.action_issue_tool(self.employee_id.id, self.project_id.id)
        elif self.action_type == 'transfer':
            if not self.employee_id or not self.project_id:
                raise UserError(_("Recipient Operative and Project Site are mandatory to transfer custody."))
            self.tool_id.action_transfer_tool(self.employee_id.id, self.project_id.id)
        elif self.action_type == 'return':
            self.tool_id.action_return_tool(return_condition=self.return_condition or 'good', notes=self.notes or '')
        elif self.action_type == 'deduct':
            if self.amount <= 0:
                raise UserError(_("Deduction amount must be greater than zero."))
            if not self.reason:
                raise UserError(_("Justification reason is required for deduction."))
            self.tool_id.action_book_employee_deduction(self.amount, self.reason)
        return {'type': 'ir.actions.act_window_close'}
