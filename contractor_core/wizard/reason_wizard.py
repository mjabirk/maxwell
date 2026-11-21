# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ContractorReasonWizard(models.TransientModel):
    _name = 'contractor.reason.wizard'
    _description = 'Contractor Mandatory Action Reason Wizard'

    res_model = fields.Char(
        string='Model Name',
        required=True,
        readonly=True,
    )
    res_id = fields.Integer(
        string='Record ID',
        required=True,
        readonly=True,
    )
    action_type = fields.Selection([
        ('decline', 'Decline / Reject'),
        ('return', 'Return for Correction'),
        ('cancel', 'Cancel'),
        ('hold', 'Place on Hold'),
        ('withdraw', 'Withdraw Document'),
        ('override', 'Management Governance Override'),
        ('escalate', 'Manual SLA Escalation'),
        ('exception', 'Flag Exception / Discrepancy'),
        ('reclassify', 'Workforce Reclassification'),
        ('dispose', 'Asset Disposal / Write-Off'),
        ('reopen', 'Reopen Locked Period / Record'),
        ('other', 'Other Governance Action'),
    ], string='Action Type', required=True, default='decline')

    target_state = fields.Char(
        string='Target State',
        readonly=True,
        help='Optional state to transition the record to upon reason confirmation.',
    )
    callback_method = fields.Char(
        string='Callback Method Name',
        readonly=True,
        help='Optional method to call on the target model after recording reason.',
    )
    reason = fields.Text(
        string='Mandatory Justification / Reason',
        required=True,
        help='Document the comprehensive reason for declining, returning, cancelling, or overriding this record.',
    )

    @api.model
    def default_get(self, fields_list):
        """Supports both default_* keys and legacy active_model/active_id/target_state/callback_method context keys."""
        res = super().default_get(fields_list)
        ctx = self.env.context
        if not res.get('res_model') and ctx.get('active_model'):
            res['res_model'] = ctx['active_model']
        if not res.get('res_id') and ctx.get('active_id'):
            res['res_id'] = ctx['active_id']
        if not res.get('target_state') and ctx.get('target_state'):
            res['target_state'] = ctx['target_state']
        if not res.get('callback_method') and ctx.get('callback_method'):
            res['callback_method'] = ctx['callback_method']
        if ctx.get('action_type') and not ctx.get('default_action_type'):
            valid_types = {k for k, _ in self._fields['action_type'].selection}
            if ctx['action_type'] in valid_types:
                res['action_type'] = ctx['action_type']
        return res

    def action_confirm(self):
        """Validates the documented reason and executes the state transition or callback."""
        self.ensure_one()
        clean_reason = (self.reason or '').strip()
        if not clean_reason or len(clean_reason) < 5:
            raise UserError(_(
                "A comprehensive explanation (minimum 5 characters) is strictly mandatory "
                "to proceed with this action under Contractor Platform Governance."
            ))

        if not self.res_model or not self.res_id:
            raise UserError(_("Invalid document target reference in Reason Wizard."))

        target_model = self.env.get(self.res_model)
        if target_model is None:
            raise UserError(_("Model %s does not exist.") % self.res_model)

        record = target_model.browse(self.res_id).exists()
        if not record:
            raise UserError(_("Target record for model %s with ID %s not found.") % (self.res_model, self.res_id))

        # 1. Update governance audit fields if present on the target model
        update_vals = {}
        if 'last_action_reason' in record._fields:
            update_vals['last_action_reason'] = clean_reason
        if 'last_action_by_id' in record._fields:
            update_vals['last_action_by_id'] = self.env.user.id
        if 'last_action_date' in record._fields:
            update_vals['last_action_date'] = fields.Datetime.now()
        if self.target_state:
            if 'contractor_quote_state' in record._fields:
                if self.target_state in ('cancel', 'cancelled'):
                    update_vals['contractor_quote_state'] = 'cancelled'
                    if 'state' in record._fields:
                        update_vals['state'] = 'cancel'
                elif self.target_state == 'draft':
                    update_vals['contractor_quote_state'] = 'draft'
                    if 'state' in record._fields:
                        update_vals['state'] = 'draft'
                elif self.target_state in ('declined', 'lost'):
                    update_vals['contractor_quote_state'] = self.target_state
                    if 'state' in record._fields:
                        update_vals['state'] = 'cancel'
                else:
                    update_vals['contractor_quote_state'] = self.target_state
            elif 'contractor_state' in record._fields:
                update_vals['contractor_state'] = self.target_state
            elif 'state' in record._fields:
                update_vals['state'] = self.target_state

        if update_vals:
            record.with_context(bypass_state_lock=True).write(update_vals)

        # 2. Invoke optional callback method on the record if specified
        if self.callback_method and hasattr(record, self.callback_method):
            method = getattr(record.with_context(bypass_state_lock=True), self.callback_method)
            try:
                method(reason=clean_reason)
            except TypeError:
                # If callback signature does not accept keyword 'reason'
                method()

        # 3. Post formatted audit trail into record chatter if mail.thread is supported
        if hasattr(record, 'message_post'):
            action_label = dict(self._fields['action_type'].selection).get(self.action_type, self.action_type)
            record.message_post(
                body=Markup(_(
                    "<p><strong>Governance Action Executed:</strong> %s</p>"
                    "<p><strong>Executed By:</strong> %s</p>"
                    "<p><strong>Documented Reason:</strong></p>"
                    "<blockquote>%s</blockquote>"
                )) % (action_label, self.env.user.name, clean_reason),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

        return {'type': 'ir.actions.act_window_close'}

