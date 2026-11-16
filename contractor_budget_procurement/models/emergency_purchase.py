# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class PurchaseOrderEmergency(models.Model):
    _inherit = 'purchase.order'

    is_emergency_purchase = fields.Boolean(
        string='Emergency Purchase',
        default=False,
        tracking=True,
        help='Bypasses standard Material Requisition & 3-Vendor RFQ procedures up to company emergency cap.',
    )
    emergency_reason_code = fields.Selection([
        ('site_safety', 'Immediate Site Safety Risk / Hazard'),
        ('job_stoppage', 'Critical Job Stoppage / Delay Risk'),
        ('equipment_breakdown', 'Essential Plant / Equipment Breakdown'),
        ('client_urgent', 'Client Urgent Operational Directive'),
        ('other', 'Other – Explanation Mandatory'),
    ], string='Emergency Classification', tracking=True)

    emergency_explanation = fields.Text(
        string='Emergency Justification & Impact',
        tracking=True,
        help='Detailed explanation of why standard procurement timeline could not be maintained.',
    )
    emergency_manager_approved = fields.Boolean(
        string='Emergency Authorized by Manager',
        default=False,
        readonly=True,
        tracking=True,
        copy=False,
    )
    emergency_approver_id = fields.Many2one(
        'res.users',
        string='Emergency Authorized By',
        readonly=True,
        copy=False,
    )
    emergency_approval_date = fields.Datetime(
        string='Emergency Approval Date',
        readonly=True,
        copy=False,
    )

    # Post-Event Review by Managing Director
    md_post_event_review_state = fields.Selection([
        ('pending', 'Pending MD Review'),
        ('reviewed', 'Reviewed & Accepted by MD'),
        ('exception_flagged', 'Exception Flagged by MD'),
    ], string='MD Post-Event Audit Status', default='pending', tracking=True, copy=False)

    md_review_notes = fields.Text(
        string='MD Audit Remarks',
        tracking=True,
        copy=False,
    )
    md_reviewed_by = fields.Many2one(
        'res.users',
        string='MD Reviewed By',
        readonly=True,
        copy=False,
    )
    md_review_date = fields.Datetime(
        string='MD Review Date',
        readonly=True,
        copy=False,
    )

    def action_approve_emergency_purchase(self):
        """Authorizes emergency PO bypass within allowable emergency cap."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize Emergency Purchases."))

        if not self.is_emergency_purchase:
            raise UserError(_("This purchase order is not marked as an Emergency Purchase."))

        if not self.emergency_reason_code:
            raise UserError(_("Emergency Classification is required."))

        if not self.emergency_explanation or len(self.emergency_explanation.strip()) < 15:
            raise UserError(_("A detailed Emergency Justification (minimum 15 characters) is mandatory."))

        company = self.company_id or self.env.company
        cap = company.emergency_po_cap or 5000.0
        if self.amount_total > cap:
            raise UserError(_(
                "Emergency Purchase Cap Exceeded Hard-Stop:\n\n"
                "Total purchase value (%s %.2f) exceeds the Company Emergency Cap (%s %.2f).\n\n"
                "Emergency bypass is prohibited for purchases exceeding this cap. "
                "This procurement must follow standard Material Requisition (MR) and Comparative Statement (3-Vendor RFQ) procedures."
            ) % (self.currency_id.symbol or '', self.amount_total, self.currency_id.symbol or '', cap))

        self.write({
            'emergency_manager_approved': True,
            'emergency_approver_id': self.env.user.id,
            'emergency_approval_date': fields.Datetime.now(),
        })

        self.message_post(
            body=Markup(_("<strong>Emergency Purchase Authorized:</strong> Cleared by Contractor Manager %s.<br/>Classification: %s")) % (
                self.env.user.name, dict(self._fields['emergency_reason_code'].selection).get(self.emergency_reason_code)
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_md_accept_emergency_review(self):
        """Managing Director accepts post-event review without exception."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Only the Managing Director can perform post-event emergency reviews."))

        self.write({
            'md_post_event_review_state': 'reviewed',
            'md_reviewed_by': self.env.user.id,
            'md_review_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>MD Post-Event Audit Completed:</strong> Emergency purchase accepted without exceptions by Managing Director %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_md_flag_emergency_exception(self):
        """Managing Director flags a procedural or cost exception on an emergency purchase."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Only the Managing Director can flag post-event exceptions."))

        return self.action_open_reason_wizard(
            action_type='exception',
            callback_method='_callback_md_exception',
        )

    def _callback_md_exception(self, reason=None):
        self.write({
            'md_post_event_review_state': 'exception_flagged',
            'md_review_notes': reason or self.last_action_reason,
            'md_reviewed_by': self.env.user.id,
            'md_review_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>MD Post-Event Exception Flagged:</strong> Flagged by Managing Director %s.<br/>Audit Findings: %s")) % (
                self.env.user.name, self.md_review_notes
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def _prepare_invoice(self):
        """Propagate emergency purchase status to vendor bill."""
        res = super()._prepare_invoice()
        if self.is_emergency_purchase:
            res.update({
                'is_emergency_purchase': True,
                'emergency_reason_code': self.emergency_reason_code,
                'emergency_explanation': self.emergency_explanation,
            })
        return res


class StockPickingEmergency(models.Model):
    _inherit = 'stock.picking'

    is_emergency_purchase = fields.Boolean(
        string='Emergency Purchase Receipt',
        default=False,
        readonly=True,
    )
    emergency_reason_code = fields.Selection([
        ('site_safety', 'Immediate Site Safety Risk / Hazard'),
        ('job_stoppage', 'Critical Job Stoppage / Delay Risk'),
        ('equipment_breakdown', 'Essential Plant / Equipment Breakdown'),
        ('client_urgent', 'Client Urgent Operational Directive'),
        ('other', 'Other – Explanation Mandatory'),
    ], string='Emergency Classification', readonly=True)

    emergency_explanation = fields.Text(
        string='Emergency Justification',
        readonly=True,
    )


class AccountMoveEmergency(models.Model):
    _inherit = 'account.move'

    is_emergency_purchase = fields.Boolean(
        string='Emergency Purchase Bill',
        default=False,
        readonly=True,
    )
    emergency_reason_code = fields.Selection([
        ('site_safety', 'Immediate Site Safety Risk / Hazard'),
        ('job_stoppage', 'Critical Job Stoppage / Delay Risk'),
        ('equipment_breakdown', 'Essential Plant / Equipment Breakdown'),
        ('client_urgent', 'Client Urgent Operational Directive'),
        ('other', 'Other – Explanation Mandatory'),
    ], string='Emergency Classification', readonly=True)

    emergency_explanation = fields.Text(
        string='Emergency Justification',
        readonly=True,
    )


class AccountPaymentEmergency(models.Model):
    _inherit = 'account.payment'

    is_emergency_purchase = fields.Boolean(
        string='Emergency Purchase Payment',
        default=False,
        readonly=True,
    )
    emergency_reason_code = fields.Selection([
        ('site_safety', 'Immediate Site Safety Risk / Hazard'),
        ('job_stoppage', 'Critical Job Stoppage / Delay Risk'),
        ('equipment_breakdown', 'Essential Plant / Equipment Breakdown'),
        ('client_urgent', 'Client Urgent Operational Directive'),
        ('other', 'Other – Explanation Mandatory'),
    ], string='Emergency Classification', readonly=True)

    emergency_explanation = fields.Text(
        string='Emergency Justification',
        readonly=True,
    )


class AccountPaymentRegisterEmergency(models.TransientModel):
    _inherit = 'account.payment.register'

    is_emergency_purchase = fields.Boolean(
        string='Emergency Purchase Payment',
        default=False,
        readonly=True,
    )
    emergency_reason_code = fields.Selection([
        ('site_safety', 'Immediate Site Safety Risk / Hazard'),
        ('job_stoppage', 'Critical Job Stoppage / Delay Risk'),
        ('equipment_breakdown', 'Essential Plant / Equipment Breakdown'),
        ('client_urgent', 'Client Urgent Operational Directive'),
        ('other', 'Other – Explanation Mandatory'),
    ], string='Emergency Classification', readonly=True)

    emergency_explanation = fields.Text(
        string='Emergency Justification',
        readonly=True,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if self._context.get('active_model') == 'account.move' and self._context.get('active_ids'):
            moves = self.env['account.move'].browse(self._context['active_ids'])
            emergency_moves = moves.filtered(lambda m: m.is_emergency_purchase)
            if emergency_moves:
                res['is_emergency_purchase'] = True
                res['emergency_reason_code'] = emergency_moves[0].emergency_reason_code
                res['emergency_explanation'] = emergency_moves[0].emergency_explanation
        return res

    def _create_payment_vals_from_wizard(self, batch_result):
        vals = super()._create_payment_vals_from_wizard(batch_result)
        if self.is_emergency_purchase:
            vals['is_emergency_purchase'] = True
            vals['emergency_reason_code'] = self.emergency_reason_code
            vals['emergency_explanation'] = self.emergency_explanation
        return vals

    def _create_payment_vals_from_batch(self, batch_result):
        vals = super()._create_payment_vals_from_batch(batch_result)
        if self.is_emergency_purchase:
            vals['is_emergency_purchase'] = True
            vals['emergency_reason_code'] = self.emergency_reason_code
            vals['emergency_explanation'] = self.emergency_explanation
        return vals


