# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ContractorNegotiationWizard(models.TransientModel):
    _name = 'contractor.negotiation.wizard'
    _description = 'Contractor Client Negotiation & Revision Wizard'

    order_id = fields.Many2one(
        'sale.order',
        string='Quotation',
        required=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='order_id.currency_id',
    )
    current_quotation_value = fields.Monetary(
        string='Current Proposed Value',
        related='order_id.proposed_quotation_value',
    )
    negotiation_notes = fields.Text(
        string='Client Counter-Proposal & Scope Adjustments',
        required=True,
        help='Summarize the client feedback, requested discounts, scope deductions, or trade-offs.',
    )
    client_message_ids = fields.Many2many(
        'ir.attachment',
        'contractor_negotiation_attachment_rel',
        'wizard_id',
        'attachment_id',
        string='Client Correspondence / Markups',
        required=True,
        help='Mandatory client email, formal negotiation letter, or marked-up BOQ attachment.',
    )
    update_margin = fields.Boolean(
        string='Adjust Commercial Margin Now',
        default=False,
    )
    revised_margin_type = fields.Selection([
        ('percentage', 'Percentage Margin (%)'),
        ('fixed', 'Fixed Monetary Margin'),
    ], string='Revised Margin Mode', default='percentage')

    revised_margin_percent = fields.Float(
        string='New Margin (%)',
        default=15.0,
    )
    revised_margin_amount = fields.Monetary(
        string='New Margin Amount',
        currency_field='currency_id',
    )

    def action_confirm_negotiation(self):
        """Archives current estimate, updates margin, logs client correspondence,
        and routes back to 'submitted' for Contractor Manager re-approval.
        """
        self.ensure_one()
        notes = (self.negotiation_notes or '').strip()
        if len(notes) < 5:
            raise UserError(_("Please provide detailed negotiation notes (minimum 5 characters)."))

        if not self.client_message_ids:
            raise UserError(_("At least one client communication document or marked-up file must be attached."))

        order = self.order_id

        if order._is_post_conversion_locked():
            raise UserError(_(
                "Sales Order %s is already confirmed or linked to an active Project / Master Budget. "
                "Client negotiation revisions are not permitted post-conversion; please create a Variation Order (VO)."
            ) % order.display_name)

        # Update margins if requested in the wizard
        if self.update_margin:
            margin_vals = {'margin_type': self.revised_margin_type}
            if self.revised_margin_type == 'percentage':
                margin_vals['margin_percent'] = self.revised_margin_percent
            else:
                margin_vals['margin_amount'] = self.revised_margin_amount
            wizard_order = order.with_context(from_negotiation_wizard=True)
            wizard_order.with_context(skip_estimation_sync=True, from_negotiation_wizard=True).write(margin_vals)
            wizard_order._compute_margin_values()
            wizard_order._sync_order_lines_from_estimation()

        # Archive new version snapshot (e.g. v2, v3...)
        snapshot_reason = _("Client Negotiation: %s") % notes
        order._create_version_snapshot(
            reason=snapshot_reason,
            editor_user=self.env.user,
            move_attachments=True,
        )

        # Route back to submitted for Manager re-approval
        order.write({
            'contractor_quote_state': 'submitted',
            'submitted_date': fields.Datetime.now(),
        })
        if order.lead_id:
            order.lead_id.contractor_state = 'negotiation'

        # Bind new negotiation correspondence attachments to the active quotation
        self.client_message_ids.sudo().write({
            'res_model': 'sale.order',
            'res_id': order.id,
        })

        # Post chatter audit note
        attachment_names = ", ".join(self.client_message_ids.mapped('name'))
        order.message_post(
            body=Markup(_(
                "<p><strong>Client Negotiation Logged:</strong></p>"
                "<blockquote>%s</blockquote>"
                "<p><strong>Attachments:</strong> %s<br/>"
                "<strong>Status:</strong> Re-routed to Contractor Managers for Review.</p>"
            )) % (notes, attachment_names),
            attachment_ids=self.client_message_ids.ids,
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )

        return {'type': 'ir.actions.act_window_close'}

