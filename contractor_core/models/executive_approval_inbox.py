# -*- coding: utf-8 -*-
from datetime import timedelta
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ContractorApprovalInbox(models.Model):
    _name = 'contractor.approval.inbox'
    _description = 'Unified Executive Approval & Action Inbox'
    _order = 'sla_status desc, submitted_datetime asc, id desc'

    name = fields.Char(string='Document Reference', required=True, readonly=True)
    document_type = fields.Selection([
        ('quotation', 'Quotation'),
        ('vo', 'Variation Order'),
        ('mr', 'Material Requisition'),
        ('cs', 'Comparative Statement'),
        ('po', 'Purchase Order'),
        ('payment', 'Payment Disbursement'),
        ('payroll', 'Payroll Run'),
        ('period_close', 'Period Close'),
    ], string='Document Type', required=True, readonly=True)
    res_model = fields.Char(string='Source Model', required=True, readonly=True)
    res_id = fields.Integer(string='Source Record ID', required=True, readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Customer / Vendor', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True, required=True)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id', readonly=True)
    amount = fields.Monetary(string='Value / Amount', currency_field='currency_id', readonly=True)
    state_label = fields.Char(string='Current Stage', readonly=True)
    submitted_datetime = fields.Datetime(string='Pending Since', readonly=True)
    sla_status = fields.Selection([
        ('on_track', 'On Track'),
        ('warning', 'Nearing SLA'),
        ('breached', 'SLA Breached'),
    ], string='SLA Status', default='on_track', readonly=True)

    def _compute_sla_flag(self, pending_dt, company, default_hours=48.0):
        if not pending_dt:
            return 'on_track'
        now = fields.Datetime.now()
        elapsed_hrs = (now - pending_dt).total_seconds() / 3600.0
        limit = getattr(company, 'approval_sla_hours', 0) or default_hours
        if elapsed_hrs >= limit:
            return 'breached'
        if elapsed_hrs >= (limit * 0.75):
            return 'warning'
        return 'on_track'

    def action_refresh_inbox(self):
        """Rebuilds executive approval inbox entries across all Contractor ERP workflow queues."""
        self.sudo().search([]).unlink()
        vals_list = []

        # 1. Quotations (sale.order)
        SaleOrder = self.env.get('sale.order')
        if SaleOrder is not None and 'contractor_quote_state' in SaleOrder._fields:
            quotes = SaleOrder.sudo().search([('contractor_quote_state', '=', 'submitted')])
            for q in quotes:
                pending_dt = getattr(q, 'submitted_date', False) or q.create_date
                proj = getattr(q, 'project_id', False)
                vals_list.append({
                    'name': q.name,
                    'document_type': 'quotation',
                    'res_model': 'sale.order',
                    'res_id': q.id,
                    'project_id': proj.id if proj else False,
                    'partner_id': q.partner_id.id if q.partner_id else False,
                    'company_id': q.company_id.id,
                    'amount': getattr(q, 'proposed_quotation_value', 0.0) or q.amount_total,
                    'state_label': _('Manager Quote Review'),
                    'submitted_datetime': pending_dt,
                    'sla_status': self._compute_sla_flag(pending_dt, q.company_id, getattr(q.company_id, 'approval_sla_hours', 48) or 48),
                })

        # 2. Variation Orders
        VO = self.env.get('contractor.variation.order')
        if VO is not None:
            vos = VO.sudo().search([('state', 'in', ['submitted', 'pending_md_approval'])])
            for v in vos:
                pending_dt = v.write_date or v.create_date
                vals_list.append({
                    'name': v.name,
                    'document_type': 'vo',
                    'res_model': 'contractor.variation.order',
                    'res_id': v.id,
                    'project_id': v.project_id.id if getattr(v, 'project_id', False) else False,
                    'partner_id': v.project_id.partner_id.id if getattr(v, 'project_id', False) and v.project_id.partner_id else False,
                    'company_id': v.company_id.id,
                    'amount': getattr(v, 'proposed_vo_value', 0.0) or getattr(v, 'total_estimated_cost', 0.0),
                    'state_label': _('MD VO Approval') if v.state == 'pending_md_approval' else _('Manager VO Review'),
                    'submitted_datetime': pending_dt,
                    'sla_status': self._compute_sla_flag(pending_dt, v.company_id, 48),
                })

        # 3. Material Requisitions
        MR = self.env.get('contractor.material.requisition')
        if MR is not None:
            mrs = MR.sudo().search([('state', '=', 'submitted')])
            for mr in mrs:
                pending_dt = getattr(mr, 'submitted_datetime', False) or mr.create_date
                vals_list.append({
                    'name': mr.name,
                    'document_type': 'mr',
                    'res_model': 'contractor.material.requisition',
                    'res_id': mr.id,
                    'project_id': mr.project_id.id if mr.project_id else False,
                    'partner_id': False,
                    'company_id': mr.company_id.id,
                    'amount': getattr(mr, 'total_estimated_amount', 0.0),
                    'state_label': _('Manager MR Approval'),
                    'submitted_datetime': pending_dt,
                    'sla_status': self._compute_sla_flag(pending_dt, mr.company_id, getattr(mr.company_id, 'mr_approval_sla_hours', 24) or 24),
                })

        # 4. Comparative Statements
        CS = self.env.get('contractor.comparative.statement')
        if CS is not None:
            css = CS.sudo().search([('state', 'in', ['submitted', 'pending_md_approval'])])
            for c in css:
                pending_dt = c.write_date or c.create_date
                vals_list.append({
                    'name': c.name,
                    'document_type': 'cs',
                    'res_model': 'contractor.comparative.statement',
                    'res_id': c.id,
                    'project_id': c.project_id.id if getattr(c, 'project_id', False) else False,
                    'partner_id': c.selected_vendor_id.id if getattr(c, 'selected_vendor_id', False) else False,
                    'company_id': c.company_id.id,
                    'amount': getattr(c, 'selected_total_amount', 0.0),
                    'state_label': _('MD Award Approval') if c.state == 'pending_md_approval' else _('Manager Award Review'),
                    'submitted_datetime': pending_dt,
                    'sla_status': self._compute_sla_flag(pending_dt, c.company_id, 24),
                })

        # 5. Purchase Orders (To Approve or MD Sign-off Pending)
        PO = self.env.get('purchase.order')
        if PO is not None:
            pos = PO.sudo().search([('state', 'in', ['draft', 'sent', 'to approve'])])
            for po in pos:
                needs_md = getattr(po, 'requires_md_approval', False) and not getattr(po, 'md_approved', False)
                if po.state == 'to approve' or needs_md:
                    pending_dt = po.write_date or po.create_date
                    vals_list.append({
                        'name': po.name,
                        'document_type': 'po',
                        'res_model': 'purchase.order',
                        'res_id': po.id,
                        'project_id': po.project_id.id if getattr(po, 'project_id', False) else False,
                        'partner_id': po.partner_id.id if po.partner_id else False,
                        'company_id': po.company_id.id,
                        'amount': po.amount_total,
                        'state_label': _('MD Threshold Approval') if needs_md else _('PO Confirmation Pending'),
                        'submitted_datetime': pending_dt,
                        'sla_status': self._compute_sla_flag(pending_dt, po.company_id, 24),
                    })

        # 6. Payment Maker-Checker Queue
        Payment = self.env.get('account.payment')
        if Payment is not None and 'payment_workflow_state' in Payment._fields:
            payments = Payment.sudo().search([('payment_workflow_state', 'in', ['prepared', 'approved'])])
            for pay in payments:
                pending_dt = getattr(pay, 'preparation_date', False) or pay.write_date or pay.create_date
                vals_list.append({
                    'name': pay.name or f"PAY/{pay.id}",
                    'document_type': 'payment',
                    'res_model': 'account.payment',
                    'res_id': pay.id,
                    'project_id': pay.project_id.id if getattr(pay, 'project_id', False) else False,
                    'partner_id': pay.partner_id.id if pay.partner_id else False,
                    'company_id': pay.company_id.id,
                    'amount': pay.amount,
                    'state_label': _('Step 2: Manager Approval') if pay.payment_workflow_state == 'prepared' else _('Step 3: Disbursement Release'),
                    'submitted_datetime': pending_dt,
                    'sla_status': self._compute_sla_flag(pending_dt, pay.company_id, 24),
                })

        # 7. Payroll Runs
        Payroll = self.env.get('contractor.payroll.run')
        if Payroll is not None:
            runs = Payroll.sudo().search([('state', 'in', ['hr_prepared', 'accountant_approved'])])
            for run in runs:
                pending_dt = run.write_date or run.create_date
                vals_list.append({
                    'name': run.name,
                    'document_type': 'payroll',
                    'res_model': 'contractor.payroll.run',
                    'res_id': run.id,
                    'project_id': False,
                    'partner_id': False,
                    'company_id': run.company_id.id,
                    'amount': getattr(run, 'total_net_payroll', 0.0) or getattr(run, 'total_gross_payroll', 0.0),
                    'state_label': _('Accountant Audit') if run.state == 'hr_prepared' else _('Payroll Release Pending'),
                    'submitted_datetime': pending_dt,
                    'sla_status': self._compute_sla_flag(pending_dt, run.company_id, 24),
                })

        # 8. Period Close
        PeriodClose = self.env.get('contractor.period.close')
        if PeriodClose is not None:
            closes = PeriodClose.sudo().search([('state', '=', 'in_review')])
            for pc in closes:
                pending_dt = pc.write_date or pc.create_date
                vals_list.append({
                    'name': pc.name,
                    'document_type': 'period_close',
                    'res_model': 'contractor.period.close',
                    'res_id': pc.id,
                    'project_id': False,
                    'partner_id': False,
                    'company_id': pc.company_id.id,
                    'amount': getattr(pc, 'completion_percentage', 0.0),
                    'state_label': _('Period Lock Authorization'),
                    'submitted_datetime': pending_dt,
                    'sla_status': self._compute_sla_flag(pending_dt, pc.company_id, 48),
                })

        if vals_list:
            self.sudo().create(vals_list)
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    @api.model
    def action_open_inbox(self):
        self.action_refresh_inbox()
        return self.env.ref('contractor_core.action_contractor_approval_inbox').sudo().read()[0]

    def _get_source_record(self):
        self.ensure_one()
        target = self.env.get(self.res_model)
        if target is None:
            raise UserError(_("Model %s is not installed.") % self.res_model)
        rec = target.browse(self.res_id).exists()
        if not rec:
            raise UserError(_("Source document %s (%s) no longer exists.") % (self.name, self.res_model))
        return rec

    def action_open_source_document(self):
        self.ensure_one()
        rec = self._get_source_record()
        return {
            'name': rec.display_name,
            'type': 'ir.actions.act_window',
            'res_model': self.res_model,
            'res_id': rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_quick_approve(self):
        """Routes directly to the source model's governance-compliant approval method."""
        self.ensure_one()
        rec = self._get_source_record()
        result = False
        if self.res_model == 'sale.order':
            result = rec.action_manager_approve()
        elif self.res_model == 'contractor.variation.order':
            if rec.state == 'pending_md_approval':
                result = rec.action_md_approve()
            else:
                result = rec.action_manager_approve()
        elif self.res_model == 'contractor.material.requisition':
            result = rec.action_manager_approve()
        elif self.res_model == 'contractor.comparative.statement':
            if rec.state == 'pending_md_approval':
                result = rec.action_md_approve()
            else:
                result = rec.action_manager_approve()
        elif self.res_model == 'purchase.order':
            if getattr(rec, 'requires_md_approval', False) and not getattr(rec, 'md_approved', False):
                result = rec.action_md_approve_po()
            elif rec.state == 'to approve':
                result = rec.button_approve()
            else:
                result = rec.button_confirm()
        elif self.res_model == 'account.payment':
            if rec.payment_workflow_state == 'prepared':
                result = rec.action_approve_payment()
            else:
                result = rec.action_release_payment()
        elif self.res_model == 'contractor.payroll.run':
            if rec.state == 'hr_prepared':
                result = rec.action_accountant_approve()
            else:
                result = rec.action_release_payroll()
        elif self.res_model == 'contractor.period.close':
            result = rec.action_lock_period()

        if isinstance(result, dict):
            return result
        self.action_refresh_inbox()
        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def action_quick_decline_or_return(self):
        """Opens the source model's mandatory reason wizard for decline/return/cancel."""
        self.ensure_one()
        rec = self._get_source_record()
        for method_name in ('action_decline', 'action_reject', 'action_return_for_correction', 'button_cancel'):
            if hasattr(rec, method_name):
                res = getattr(rec, method_name)()
                if isinstance(res, dict):
                    return res
        if hasattr(rec, 'action_open_reason_wizard'):
            return rec.action_open_reason_wizard(action_type='decline', target_state='declined')
        raise UserError(_("No interactive decline or return action available for %s.") % self.name)
