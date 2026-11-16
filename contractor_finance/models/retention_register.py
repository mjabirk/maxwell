# -*- coding: utf-8 -*-
from datetime import timedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorRetentionRegister(models.Model):
    _name = 'contractor.retention.register'
    _description = 'Contractor Client & Subcontractor Retention Register'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin', 'contractor.reason.mixin']
    _order = 'dlp_expiry_date asc, id desc'

    name = fields.Char(
        string='Retention Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Retention'),
        index=True,
    )
    retention_type = fields.Selection([
        ('client_receivable', 'Client Retention Receivable (AR)'),
        ('subcontractor_payable', 'Subcontractor Retention Payable (AP)'),
    ], string='Retention Classification', default='client_receivable', required=True, tracking=True)

    project_id = fields.Many2one(
        'project.project',
        string='Project Site',
        required=True,
        tracking=True,
        index=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Contracting Party (Client / Subcontractor)',
        required=True,
        tracking=True,
    )
    invoice_id = fields.Many2one(
        'account.move',
        string='Source Invoice / Bill',
        tracking=True,
    )
    contract_amount = fields.Monetary(
        string='Certified Billing Base Amount',
        currency_field='currency_id',
        tracking=True,
    )
    retention_pct = fields.Float(
        string='Retention Percentage (%)',
        default=5.0,
        required=True,
        tracking=True,
    )
    retention_amount = fields.Monetary(
        string='Withheld Retention Amount',
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
    retention_account_id = fields.Many2one(
        'account.account',
        string='Dedicated Retention GL Account',
        domain="[('company_ids', 'in', company_id)]",
    )

    # Defect Liability Period (DLP)
    dlp_start_date = fields.Date(
        string='DLP Commencement Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    dlp_duration_days = fields.Integer(
        string='DLP Duration (Days)',
        default=365,
        required=True,
        tracking=True,
        help='Standard DLP is typically 365 or 400 calendar days following Taking Over Certificate (TOC).',
    )
    dlp_expiry_date = fields.Date(
        string='DLP Expiry Date',
        compute='_compute_dlp_expiry',
        store=True,
        tracking=True,
    )
    is_current_classification = fields.Boolean(
        string='Classified as Current (<= 12M)',
        compute='_compute_current_classification',
        store=True,
        help='Classifies retention as Current Asset/Liability if maturing within 12 months from report date, otherwise Non-Current.',
    )

    # Snag List Rectification Hard-Stop Gate
    snags_rectified_confirmed = fields.Boolean(
        string='Snag List Rectification Certified by PM',
        default=False,
        tracking=True,
        copy=False,
    )
    snag_confirmation_date = fields.Datetime(
        string='PM Certification Date',
        readonly=True,
        copy=False,
    )
    snag_confirmed_by_id = fields.Many2one(
        'res.users',
        string='PM Certifying Snags',
        readonly=True,
        copy=False,
    )
    snag_notes = fields.Text(
        string='Snag Rectification & Final Handover Notes',
        copy=False,
    )
    snag_attachment_ids = fields.Many2many(
        'ir.attachment',
        'contractor_retention_snag_attachment_rel',
        'retention_id',
        'attachment_id',
        string='Snag Clearance & TOC Certificates',
    )

    state = fields.Selection([
        ('held', 'Held Under DLP'),
        ('dlp_expired_flagged', 'DLP Expired (Awaiting Snags)'),
        ('snag_cleared', 'Snags Rectified (Ready for Approval)'),
        ('approved_for_release', 'Approved for Release'),
        ('released', 'Released / Invoiced / Settled'),
    ], string='Status', default='held', required=True, tracking=True, copy=False)

    approver_id = fields.Many2one(
        'res.users',
        string='Release Approved By',
        readonly=True,
        copy=False,
    )
    approval_date = fields.Datetime(
        string='Approval Date',
        readonly=True,
        copy=False,
    )
    withholding_move_id = fields.Many2one(
        'account.move',
        string='Retention Withholding Journal Entry',
        readonly=True,
        copy=False,
    )
    release_move_id = fields.Many2one(
        'account.move',
        string='Retention Release Journal Entry',
        readonly=True,
        copy=False,
    )
    released_by_id = fields.Many2one(
        'res.users',
        string='Released By',
        readonly=True,
        copy=False,
    )
    release_date = fields.Datetime(
        string='Disbursement / Invoicing Date',
        readonly=True,
        copy=False,
    )

    @api.depends('dlp_start_date', 'dlp_duration_days')
    def _compute_dlp_expiry(self):
        for rec in self:
            if rec.dlp_start_date:
                rec.dlp_expiry_date = rec.dlp_start_date + timedelta(days=rec.dlp_duration_days)
            else:
                rec.dlp_expiry_date = False

    @api.depends('dlp_expiry_date')
    def _compute_current_classification(self):
        today = fields.Date.context_today(self)
        horizon = today + timedelta(days=365)
        for rec in self:
            if rec.dlp_expiry_date:
                rec.is_current_classification = rec.dlp_expiry_date <= horizon
            else:
                rec.is_current_classification = True

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Retention')) == _('New Retention'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.retention.register') or _('New Retention')
        return super().create(vals_list)

    @api.model
    def _cron_check_dlp_expiry(self):
        """Daily automated check flagging retention records whose DLP has expired."""
        today = fields.Date.context_today(self)
        expired = self.search([
            ('state', '=', 'held'),
            ('dlp_expiry_date', '<=', today),
        ])
        for rec in expired:
            rec.write({'state': 'dlp_expired_flagged'})
            rec.message_post(
                body=Markup(_("<strong>DLP Expired:</strong> Defect Liability Period concluded on %s. Awaiting PM snag rectification clearance.")) % rec.dlp_expiry_date,
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )

    def action_confirm_snags_rectified(self):
        """Allows Project Manager to certify that snag list items are 100% completed."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_project_manager'):
            raise UserError(_("Access Denied: Only a Contractor Project Manager can certify snag list rectification."))

        if not self.snag_attachment_ids and not (self.snag_notes and len(self.snag_notes.strip()) >= 15):
            raise UserError(_("Please attach the signed Snag Clearance Certificate or document thorough rectification notes."))

        self.write({
            'snags_rectified_confirmed': True,
            'snag_confirmed_by_id': self.env.user.id,
            'snag_confirmation_date': fields.Datetime.now(),
            'state': 'snag_cleared',
        })
        self.message_post(
            body=Markup(_("<strong>Snag List Cleared:</strong> Project Manager %s certified all snag items complete.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_approve_retention_release(self):
        """Management authorization for retention release with strict snag gate."""
        self.ensure_one()
        if not self.snags_rectified_confirmed:
            raise UserError(_(
                "Snag Rectification Governance Hard-Stop:\n\n"
                "Retention release blocked. Project Manager snag rectification has not been certified for project '%s'.\n"
                "All outstanding defect items must be completed before release authorization."
            ) % self.project_id.name)

        threshold = self.company_id.po_md_approval_threshold or 50000.0
        requires_md = self.retention_amount >= threshold
        required_group = 'contractor_core.group_contractor_md' if requires_md else 'contractor_core.group_contractor_manager'

        if not self.env.user.has_group(required_group):
            role_title = _("Managing Director") if requires_md else _("Contractor Manager")
            raise UserError(_("Access Denied: Only a %s can authorize retention release of %s %.2f.") % (
                role_title, self.currency_id.symbol or '', self.retention_amount
            ))

        self.write({
            'state': 'approved_for_release',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Retention Release Approved:</strong> Authorized by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_release_retention(self):
        """Posts accounting entry moving retention back to current trade ledger or releasing payment."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_senior_accountant')):
            raise AccessError(_("Access Denied: Only Senior Accountants or Managers can release retention entries."))
        if self.state != 'approved_for_release':
            raise UserError(_("Retention must be approved by management prior to release."))
        if self.release_move_id:
            raise UserError(_("A retention release journal entry (%s) has already been posted.") % self.release_move_id.name)

        journal = self.env['account.journal'].search([('type', '=', 'general'), ('company_id', '=', self.company_id.id)], limit=1)
        if not journal:
            raise UserError(_("No General Journal found for company '%s'.") % self.company_id.name)

        trade_acc = self.partner_id.property_account_receivable_id if self.retention_type == 'client_receivable' else self.partner_id.property_account_payable_id
        ret_acc = self.retention_account_id or trade_acc

        move_lines = []
        if self.retention_type == 'client_receivable':
            # Debit Trade Receivable, Credit Retention Receivable
            move_lines.append((0, 0, {
                'name': f"Client Retention Release: {self.name} - {self.project_id.name}",
                'account_id': trade_acc.id,
                'debit': self.retention_amount,
                'credit': 0.0,
                'partner_id': self.partner_id.id,
            }))
            move_lines.append((0, 0, {
                'name': f"Clear Retention Receivable: {self.name}",
                'account_id': ret_acc.id,
                'debit': 0.0,
                'credit': self.retention_amount,
                'partner_id': self.partner_id.id,
            }))
        else:
            # Debit Retention Payable, Credit Trade Payable
            move_lines.append((0, 0, {
                'name': f"Clear Subcontractor Retention: {self.name}",
                'account_id': ret_acc.id,
                'debit': self.retention_amount,
                'credit': 0.0,
                'partner_id': self.partner_id.id,
            }))
            move_lines.append((0, 0, {
                'name': f"Subcontractor Retention Payable Released: {self.name}",
                'account_id': trade_acc.id,
                'debit': 0.0,
                'credit': self.retention_amount,
                'partner_id': self.partner_id.id,
            }))

        move = self.env['account.move'].create({
            'journal_id': journal.id,
            'date': fields.Date.context_today(self),
            'ref': f"Retention Release {self.name}",
            'move_type': 'entry',
            'line_ids': move_lines,
        })
        move.action_post()

        self.write({
            'state': 'released',
            'released_by_id': self.env.user.id,
            'release_date': fields.Datetime.now(),
            'release_move_id': move.id,
        })
        self.message_post(
            body=Markup(_("<strong>Retention Formally Released:</strong> Journal entry %s posted.")) % move.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        if any(rec.state == 'released' for rec in self) and not set(vals.keys()).issubset(allowed_meta_fields):
            raise UserError(_("Released Retention Register records are locked and cannot be modified."))
        return super().write(vals)


# =========================================================================
# Integration with Invoices & Vendor Bills
# =========================================================================
class AccountMoveRetentionExtension(models.Model):
    _inherit = 'account.move'

    has_retention = fields.Boolean(
        string='Subject to Retention Withholding',
        default=False,
        tracking=True,
    )
    retention_pct = fields.Float(
        string='Retention Rate (%)',
        default=5.0,
        tracking=True,
    )
    retention_amount = fields.Monetary(
        string='Retention Amount Withheld',
        currency_field='currency_id',
        compute='_compute_retention_amount',
        store=True,
        readonly=False,
        tracking=True,
    )
    retention_account_id = fields.Many2one(
        'account.account',
        string='Dedicated Retention Account',
        domain="[('company_ids', 'in', company_id)]",
    )
    retention_register_id = fields.Many2one(
        'contractor.retention.register',
        string='Linked Retention Register Record',
        readonly=True,
        copy=False,
    )

    @api.depends('has_retention', 'retention_pct', 'amount_untaxed')
    def _compute_retention_amount(self):
        for move in self:
            if move.has_retention and move.amount_untaxed:
                move.retention_amount = (move.amount_untaxed * (move.retention_pct or 5.0)) / 100.0
            else:
                move.retention_amount = 0.0

    def action_post(self):
        res = super().action_post()
        for move in self:
            if move.has_retention and move.retention_amount > 0 and not move.retention_register_id:
                # Resolve project
                project = False
                if hasattr(move, 'project_id') and move.project_id:
                    project = move.project_id
                else:
                    proj_cand = self.env['project.project'].search([('company_id', '=', move.company_id.id)], limit=1)
                    project = proj_cand

                if not project:
                    continue

                r_type = 'client_receivable' if move.move_type in ('out_invoice', 'out_refund') else 'subcontractor_payable'
                reg = self.env['contractor.retention.register'].create({
                    'retention_type': r_type,
                    'project_id': project.id,
                    'partner_id': move.partner_id.id,
                    'invoice_id': move.id,
                    'contract_amount': move.amount_untaxed,
                    'retention_pct': move.retention_pct or 5.0,
                    'retention_amount': move.retention_amount,
                    'retention_account_id': move.retention_account_id.id if move.retention_account_id else False,
                    'dlp_start_date': move.invoice_date or fields.Date.context_today(move),
                    'dlp_duration_days': move.company_id.default_dlp_days or 365,
                })
                move.retention_register_id = reg.id

                # Item 33: Automatically split retention into dedicated ledger account
                journal = self.env['account.journal'].search([
                    ('type', '=', 'general'),
                    ('company_id', '=', move.company_id.id)
                ], limit=1)
                if journal:
                    trade_acc = (
                        move.partner_id.property_account_receivable_id
                        if r_type == 'client_receivable'
                        else move.partner_id.property_account_payable_id
                    )
                    ret_acc = move.retention_account_id or trade_acc
                    if ret_acc and trade_acc:
                        move_lines = []
                        if r_type == 'client_receivable':
                            move_lines.append((0, 0, {
                                'name': f"Retention Withheld ({move.retention_pct}%): {move.name}",
                                'account_id': ret_acc.id,
                                'debit': move.retention_amount,
                                'credit': 0.0,
                                'partner_id': move.partner_id.id,
                                'project_id': project.id,
                            }))
                            move_lines.append((0, 0, {
                                'name': f"Trade Receivable Split: {move.name}",
                                'account_id': trade_acc.id,
                                'debit': 0.0,
                                'credit': move.retention_amount,
                                'partner_id': move.partner_id.id,
                                'project_id': project.id,
                            }))
                        else:
                            move_lines.append((0, 0, {
                                'name': f"Trade Payable Split: {move.name}",
                                'account_id': trade_acc.id,
                                'debit': move.retention_amount,
                                'credit': 0.0,
                                'partner_id': move.partner_id.id,
                                'project_id': project.id,
                            }))
                            move_lines.append((0, 0, {
                                'name': f"Retention Withheld ({move.retention_pct}%): {move.name}",
                                'account_id': ret_acc.id,
                                'debit': 0.0,
                                'credit': move.retention_amount,
                                'partner_id': move.partner_id.id,
                                'project_id': project.id,
                            }))
                        try:
                            split_entry = self.env['account.move'].create({
                                'journal_id': journal.id,
                                'date': move.invoice_date or fields.Date.context_today(move),
                                'ref': f"Retention Split: {move.name}",
                                'move_type': 'entry',
                                'project_id': project.id,
                                'line_ids': move_lines,
                            })
                            split_entry.action_post()
                            reg.withholding_move_id = split_entry.id
                        except Exception:
                            # Gracefully handle any accounting configuration discrepancy
                            pass
        return res

