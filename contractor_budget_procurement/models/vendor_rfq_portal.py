# -*- coding: utf-8 -*-
import uuid
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ContractorRfqVendorInvite(models.Model):
    _name = 'contractor.rfq.vendor.invite'
    _description = 'Vendor RFQ Portal Invitation'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='RFQ Invite Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New RFQ Invite'),
        index=True,
    )
    requisition_id = fields.Many2one(
        'contractor.material.requisition',
        string='Material Requisition (MR)',
        required=True,
        ondelete='cascade',
        index=True,
        tracking=True,
    )
    comparative_statement_id = fields.Many2one(
        'contractor.comparative.statement',
        string='Comparative Statement',
        index=True,
        tracking=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        related='requisition_id.project_id',
        store=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='requisition_id.company_id',
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    vendor_id = fields.Many2one(
        'res.partner',
        string='Vendor / Supplier',
        required=True,
        domain="[('is_company', '=', True)]",
        index=True,
        tracking=True,
    )
    vendor_email = fields.Char(
        string='Vendor Email',
        tracking=True,
    )
    access_token = fields.Char(
        string='Access Token',
        default=lambda self: str(uuid.uuid4()),
        copy=False,
        readonly=True,
        required=True,
        index=True,
    )
    portal_url = fields.Char(
        string='Portal Quote Link',
        compute='_compute_portal_url',
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('sent', 'Sent to Vendor'),
        ('submitted', 'Quotation Received'),
        ('approved', 'Awarded / Synced to CS'),
        ('expired', 'Closed / Expired'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    deadline_date = fields.Date(
        string='Submission Deadline',
        tracking=True,
    )
    delivery_days = fields.Integer(
        string='Quoted Delivery Lead Time (Days)',
        default=7,
        tracking=True,
    )
    payment_terms = fields.Char(
        string='Offered Payment Terms',
        default='30 Days Net',
        tracking=True,
    )
    vendor_reference = fields.Char(
        string='Vendor Quote Ref No.',
        tracking=True,
    )
    vendor_notes = fields.Text(
        string='Vendor Commercial Remarks',
    )
    submitted_datetime = fields.Datetime(
        string='Submitted At',
        readonly=True,
        copy=False,
    )
    quotation_attachment_ids = fields.Many2many(
        'ir.attachment',
        'contractor_rfq_invite_att_rel',
        'invite_id',
        'attachment_id',
        string='Vendor Quotation Attachments',
    )
    line_ids = fields.One2many(
        'contractor.rfq.vendor.invite.line',
        'invite_id',
        string='Quoted Items',
        copy=True,
    )
    total_quoted_amount = fields.Monetary(
        string='Total Quoted Amount',
        currency_field='currency_id',
        compute='_compute_total_quoted_amount',
        store=True,
    )

    @api.depends('access_token')
    def _compute_portal_url(self):
        base_url = (
            self.env['ir.config_parameter'].sudo().get_str('web.base.url') or ''
        ).rstrip('/')
        for rec in self:
            rec_id = rec.id if isinstance(rec.id, int) else False
            if rec_id and rec.access_token:
                rec.portal_url = f"{base_url}/rfq/quote/{rec_id}/{rec.access_token}"
            else:
                rec.portal_url = False

    @api.depends('line_ids.subtotal')
    def _compute_total_quoted_amount(self):
        for rec in self:
            rec.total_quoted_amount = sum(rec.line_ids.mapped('subtotal'))

    @api.onchange('vendor_id')
    def _onchange_vendor_id(self):
        for rec in self:
            if rec.vendor_id and rec.vendor_id.email:
                rec.vendor_email = rec.vendor_id.email

    @api.onchange('requisition_id')
    def _onchange_requisition_id(self):
        for rec in self:
            if rec.requisition_id and not rec.deadline_date:
                rec.deadline_date = rec.requisition_id.required_date
            if rec.requisition_id and not rec.line_ids and rec.requisition_id.line_ids:
                rec.line_ids = [
                    (0, 0, {
                        'mr_line_id': mr_line.id,
                        'product_id': mr_line.product_id.id,
                        'description': mr_line.description or mr_line.product_id.display_name,
                        'qty': mr_line.qty,
                    })
                    for mr_line in rec.requisition_id.line_ids
                ]

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        req_id = res.get('requisition_id') or self.env.context.get('default_requisition_id')
        if req_id and not res.get('deadline_date'):
            mr = self.env['contractor.material.requisition'].browse(req_id).exists()
            if mr and mr.required_date:
                res['deadline_date'] = mr.required_date
        return res

    @api.model_create_multi
    def create(self, vals_list):
        partner_model = self.env['res.partner']
        mr_model = self.env['contractor.material.requisition']
        for vals in vals_list:
            if vals.get('name', _('New RFQ Invite')) == _('New RFQ Invite'):
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('contractor.rfq.vendor.invite')
                    or _('New RFQ Invite')
                )
            if vals.get('vendor_id') and not vals.get('vendor_email'):
                vendor = partner_model.browse(vals['vendor_id']).exists()
                if vendor and vendor.email:
                    vals['vendor_email'] = vendor.email
            if vals.get('requisition_id') and not vals.get('deadline_date'):
                mr = mr_model.browse(vals['requisition_id']).exists()
                if mr and mr.required_date:
                    vals['deadline_date'] = mr.required_date
        return super().create(vals_list)

    def _populate_lines_from_mr(self):
        """Copy item lines from the parent Material Requisition when invite lines are empty."""
        line_model = self.env['contractor.rfq.vendor.invite.line']
        for rec in self:
            if not rec.line_ids and rec.requisition_id.line_ids:
                vals_list = [
                    {
                        'invite_id': rec.id,
                        'mr_line_id': mr_line.id,
                        'product_id': mr_line.product_id.id,
                        'description': mr_line.description or mr_line.product_id.display_name,
                        'qty': mr_line.qty,
                    }
                    for mr_line in rec.requisition_id.line_ids
                ]
                line_model.create(vals_list)

    def _upsert_lines_to_comparative_statement(self, cs, attach_to_chatter=True):
        """Sync this invite's quoted lines (and attachments) into the given draft Comparative Statement."""
        self.ensure_one()
        cs_line_model = self.env['contractor.comparative.statement.line']
        matched_line_ids = set()
        for inv_line in self.line_ids:
            existing = cs.line_ids.filtered(
                lambda l: (
                    l.vendor_id.id == self.vendor_id.id
                    and l.product_id.id == inv_line.product_id.id
                    and l.id not in matched_line_ids
                )
            )[:1]
            vals = {
                'statement_id': cs.id,
                'vendor_id': self.vendor_id.id,
                'product_id': inv_line.product_id.id,
                'description': inv_line.description or inv_line.product_id.display_name,
                'qty': inv_line.qty,
                'quoted_price': inv_line.quoted_price,
                'delivery_days': self.delivery_days or 7,
                'payment_terms': self.payment_terms or '30 Days Net',
                'compliance_notes': inv_line.vendor_line_note or False,
            }
            if existing:
                existing.write(vals)
                matched_line_ids.add(existing.id)
            else:
                created_line = cs_line_model.create(vals)
                matched_line_ids.add(created_line.id)

        if attach_to_chatter:
            copied_att_ids = self._copy_attachments_to_record(cs)
            self._post_cs_sync_message(cs, copied_att_ids)
        return True

    def _copy_attachments_to_record(self, target_record):
        """Duplicate quotation attachments onto target_record without duplicating existing checksums."""
        self.ensure_one()
        if not self.quotation_attachment_ids:
            return []
        att_model = self.env['ir.attachment'].sudo()
        existing_atts = att_model.search([
            ('res_model', '=', target_record._name),
            ('res_id', '=', target_record.id),
        ])
        existing_keys = {(a.name, a.checksum) for a in existing_atts}
        new_ids = []
        for att in self.quotation_attachment_ids.sudo():
            key = (att.name, att.checksum)
            if key in existing_keys:
                match = existing_atts.filtered(lambda a: (a.name, a.checksum) == key)[:1]
                if match:
                    new_ids.append(match.id)
                continue
            copied = att.copy({
                'res_model': target_record._name,
                'res_id': target_record.id,
            })
            new_ids.append(copied.id)
        return new_ids

    def _post_cs_sync_message(self, cs, attachment_ids=None):
        """Log quotation sync into the Comparative Statement chatter with attached PDFs."""
        self.ensure_one()
        currency_sym = self.currency_id.symbol or ''
        body = Markup(_(
            "<strong>Vendor RFQ Portal Quote Synced (%s):</strong><br/>"
            "Vendor: %s | Quote Ref: %s | Delivery: %s days | Payment Terms: %s | Total: %s %.2f"
        )) % (
            self.name,
            self.vendor_id.name,
            self.vendor_reference or '-',
            self.delivery_days or 0,
            self.payment_terms or '30 Days Net',
            currency_sym,
            self.total_quoted_amount,
        )
        cs.message_post(
            body=body,
            attachment_ids=attachment_ids or [],
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_send_rfq_invite(self):
        """Populate items, transition to sent, email vendor_email (when configured), and log link in chatter."""
        skip_email = self.env.context.get('skip_rfq_email')
        mail_model = self.env['mail.mail'].sudo()
        for rec in self:
            rec._populate_lines_from_mr()
            if not rec.line_ids:
                raise UserError(_("Cannot send RFQ invitation '%s' because Material Requisition %s has no item lines.") % (
                    rec.name, rec.requisition_id.name
                ))
            rec.write({'state': 'sent'})
            portal_link = rec.portal_url
            company_name = rec.company_id.name or self.env.company.name
            deadline_str = str(rec.deadline_date or _('As soon as possible'))

            if rec.vendor_email and not skip_email:
                email_body = Markup(_(
                    "<p>Dear %s,</p>"
                    "<p><strong>%s</strong> invites you to submit your quotation for Material Requisition <strong>%s</strong> (%s).</p>"
                    "<p><strong>RFQ Reference:</strong> %s<br/>"
                    "<strong>Submission Deadline:</strong> %s<br/>"
                    "<strong>Items Requested:</strong> %d line(s)</p>"
                    "<p>Please submit your unit prices, commercial terms, and signed PDF quotation directly (no login required) using the link below:</p>"
                    "<p><a href='%s' style='display:inline-block;padding:10px 18px;background-color:#0f2a4a;color:#ffffff;text-decoration:none;border-radius:4px;'>Submit Quotation Online</a></p>"
                    "<p>Or copy this direct URL:<br/>%s</p>"
                )) % (
                    rec.vendor_id.name,
                    company_name,
                    rec.requisition_id.name,
                    rec.project_id.name or '',
                    rec.name,
                    deadline_str,
                    len(rec.line_ids),
                    portal_link,
                    portal_link,
                )
                mail_rec = mail_model.create({
                    'subject': _("Request for Quotation %s - %s (%s)") % (rec.name, company_name, rec.requisition_id.name),
                    'email_to': rec.vendor_email,
                    'email_from': self.env.company.email or self.env.user.email_formatted or 'noreply@localhost',
                    'body_html': str(email_body),
                    'model': rec._name,
                    'res_id': rec.id,
                })
                mail_rec.send(auto_commit=False, raise_exception=False)

            rec.message_post(
                body=Markup(_(
                    "<strong>RFQ Invitation Ready for Vendor (%s):</strong><br/>"
                    "Shareable Zero-Login Portal Link (Email / WhatsApp): <a href='%s'>%s</a><br/>"
                    "Deadline: %s | Email Dispatched To: %s"
                )) % (
                    rec.vendor_id.name,
                    portal_link,
                    portal_link,
                    deadline_str,
                    rec.vendor_email or _('Not set (copy link manually)'),
                ),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
        return True

    def action_sync_to_comparative_statement(self):
        """Find or create a draft Comparative Statement, sync lines and attachments, mark approved, and open CS form."""
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("No quoted lines found on invitation %s.") % self.name)
        if self.total_quoted_amount <= 0:
            raise UserError(_(
                "Total quoted amount on %s must be greater than zero before syncing to a Comparative Statement."
            ) % self.name)

        cs = self.comparative_statement_id
        if cs and cs.state != 'draft':
            cs = False
        if not cs:
            cs = self.env['contractor.comparative.statement'].search([
                ('requisition_id', '=', self.requisition_id.id),
                ('state', '=', 'draft'),
            ], order='id desc', limit=1)
        if not cs:
            cs = self.env['contractor.comparative.statement'].create({
                'requisition_id': self.requisition_id.id,
            })
            if self.requisition_id.state == 'approved':
                self.requisition_id.with_context(bypass_state_lock=True).write({'state': 'cs_in_progress'})

        self._upsert_lines_to_comparative_statement(cs, attach_to_chatter=True)
        self.write({
            'comparative_statement_id': cs.id,
            'state': 'approved',
        })
        self.message_post(
            body=Markup(_(
                "<strong>Awarded / Synced to Comparative Statement %s:</strong> "
                "Quoted prices and attachments transferred successfully."
            )) % cs.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return {
            'type': 'ir.actions.act_window',
            'name': _('Comparative Statement (3-Vendor Matrix)'),
            'res_model': 'contractor.comparative.statement',
            'res_id': cs.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_reopen_for_vendor(self):
        """Reopen an already-submitted or expired link so the vendor can submit an updated quote."""
        for rec in self:
            rec.write({'state': 'sent'})
            rec.message_post(
                body=Markup(_(
                    "<strong>Portal Link Reopened for Vendor:</strong> "
                    "%s may now submit a revised quotation at <a href='%s'>%s</a>."
                )) % (rec.vendor_id.name, rec.portal_url, rec.portal_url),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
        return True


class ContractorRfqVendorInviteLine(models.Model):
    _name = 'contractor.rfq.vendor.invite.line'
    _description = 'Vendor RFQ Invitation Item Line'
    _order = 'id'

    invite_id = fields.Many2one(
        'contractor.rfq.vendor.invite',
        string='RFQ Invitation',
        required=True,
        ondelete='cascade',
    )
    mr_line_id = fields.Many2one(
        'contractor.material.requisition.line',
        string='MR Line Reference',
    )
    product_id = fields.Many2one(
        'product.product',
        string='Product / Material',
        required=True,
    )
    description = fields.Char(
        string='Description / Specs',
    )
    qty = fields.Float(
        string='Requested Qty',
        required=True,
        default=1.0,
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='UoM',
        related='product_id.uom_id',
        readonly=True,
    )
    quoted_price = fields.Monetary(
        string='Quoted Unit Price',
        currency_field='currency_id',
        default=0.0,
    )
    subtotal = fields.Monetary(
        string='Subtotal',
        currency_field='currency_id',
        compute='_compute_subtotal',
        store=True,
    )
    vendor_line_note = fields.Char(
        string='Brand / Spec / Lead Time Note',
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='invite_id.currency_id',
        readonly=True,
    )

    @api.onchange('product_id')
    def _onchange_product_id(self):
        for line in self:
            if line.product_id and not line.description:
                line.description = line.product_id.display_name

    @api.depends('qty', 'quoted_price')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = (line.qty or 0.0) * (line.quoted_price or 0.0)


class ContractorRfqSendWizard(models.TransientModel):
    _name = 'contractor.rfq.send.wizard'
    _description = 'Send Vendor RFQ Portal Invitations Wizard'

    requisition_id = fields.Many2one(
        'contractor.material.requisition',
        string='Material Requisition',
        required=True,
    )
    comparative_statement_id = fields.Many2one(
        'contractor.comparative.statement',
        string='Comparative Statement',
    )
    vendor_ids = fields.Many2many(
        'res.partner',
        string='Vendors to Invite',
        required=True,
        domain="[('is_company', '=', True)]",
    )
    deadline_date = fields.Date(
        string='Submission Deadline',
        required=True,
    )
    send_email = fields.Boolean(
        string='Send Email Notifications',
        default=True,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        if not res.get('requisition_id') and res.get('comparative_statement_id'):
            cs = self.env['contractor.comparative.statement'].browse(res['comparative_statement_id']).exists()
            if cs and cs.requisition_id:
                res['requisition_id'] = cs.requisition_id.id
        req_id = res.get('requisition_id') or ctx.get('default_requisition_id')
        if req_id and not res.get('deadline_date'):
            mr = self.env['contractor.material.requisition'].browse(req_id).exists()
            if mr and mr.required_date:
                res['deadline_date'] = mr.required_date
        if not res.get('deadline_date'):
            res['deadline_date'] = fields.Date.context_today(self)
        return res

    def action_send_invitations(self):
        self.ensure_one()
        if not self.vendor_ids:
            raise UserError(_("Please select at least one vendor to invite."))
        if not self.requisition_id.line_ids:
            raise UserError(_("Material Requisition %s has no item lines to quote.") % self.requisition_id.name)
        if self.requisition_id.state not in ('approved', 'cs_in_progress'):
            raise UserError(_(
                "Vendor RFQ Portal invitations can only be sent for Approved or CS-In-Progress Material Requisitions."
            ))

        invite_model = self.env['contractor.rfq.vendor.invite']
        created_invites = invite_model
        all_target_invites = invite_model

        for vendor in self.vendor_ids:
            existing = invite_model.search([
                ('requisition_id', '=', self.requisition_id.id),
                ('vendor_id', '=', vendor.id),
                ('state', '!=', 'expired'),
            ], limit=1)
            if existing:
                all_target_invites |= existing
                continue

            invite_vals = {
                'requisition_id': self.requisition_id.id,
                'comparative_statement_id': self.comparative_statement_id.id if self.comparative_statement_id else False,
                'vendor_id': vendor.id,
                'vendor_email': vendor.email or False,
                'deadline_date': self.deadline_date or self.requisition_id.required_date,
            }
            invite = invite_model.create(invite_vals)
            invite.with_context(skip_rfq_email=not self.send_email).action_send_rfq_invite()
            created_invites |= invite
            all_target_invites |= invite

        return {
            'name': _('Vendor RFQ Invitations for %s') % self.requisition_id.name,
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.rfq.vendor.invite',
            'view_mode': 'list,form',
            'domain': [('id', 'in', all_target_invites.ids)],
            'context': {
                'default_requisition_id': self.requisition_id.id,
                'default_comparative_statement_id': self.comparative_statement_id.id if self.comparative_statement_id else False,
            },
            'target': 'current',
        }


class ContractorMaterialRequisitionRfqPortal(models.Model):
    _inherit = 'contractor.material.requisition'

    rfq_invite_ids = fields.One2many(
        'contractor.rfq.vendor.invite',
        'requisition_id',
        string='Vendor RFQ Invitations',
    )
    rfq_invite_count = fields.Integer(
        string='Vendor Quotes Count',
        compute='_compute_rfq_invite_count',
    )

    @api.depends('rfq_invite_ids')
    def _compute_rfq_invite_count(self):
        for mr in self:
            mr.rfq_invite_count = len(mr.rfq_invite_ids)

    def action_open_send_rfq_wizard(self):
        self.ensure_one()
        if self.state not in ('approved', 'cs_in_progress'):
            raise UserError(_("You can only invite vendors after the Material Requisition is approved."))
        return {
            'name': _('Send RFQ to Vendors (Portal)'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.rfq.send.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_requisition_id': self.id,
                'default_deadline_date': self.required_date,
            },
        }

    def action_view_rfq_invites(self):
        self.ensure_one()
        return {
            'name': _('Vendor RFQ Invitations for %s') % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.rfq.vendor.invite',
            'view_mode': 'list,form',
            'domain': [('requisition_id', '=', self.id)],
            'context': {'default_requisition_id': self.id},
        }


class ContractorComparativeStatementRfqPortal(models.Model):
    _inherit = 'contractor.comparative.statement'

    rfq_invite_ids = fields.One2many(
        'contractor.rfq.vendor.invite',
        'comparative_statement_id',
        string='Vendor RFQ Invitations',
    )

    def action_open_send_rfq_wizard(self):
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Vendor RFQ invitations can only be dispatched while the Comparative Statement is in Draft."))
        return {
            'name': _('Send RFQ to Vendors (Portal)'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.rfq.send.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_requisition_id': self.requisition_id.id,
                'default_comparative_statement_id': self.id,
                'default_deadline_date': self.requisition_id.required_date,
            },
        }
