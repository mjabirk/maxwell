# -*- coding: utf-8 -*-
from datetime import timedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorBankReconciliation(models.Model):
    _name = 'contractor.bank.reconciliation'
    _description = 'Contractor Bank Reconciliation Sheet (Maker-Checker)'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'statement_date desc, id desc'

    name = fields.Char(
        string='Reconciliation Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Bank Rec'),
        index=True,
    )
    journal_id = fields.Many2one(
        'account.journal',
        string='Bank Journal',
        domain="[('type', '=', 'bank')]",
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
        related='journal_id.currency_id',
        readonly=True,
    )
    statement_date = fields.Date(
        string='Bank Statement Cutoff Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    opening_balance = fields.Monetary(
        string='Statement Opening Balance',
        currency_field='currency_id',
        required=True,
        tracking=True,
    )
    closing_balance = fields.Monetary(
        string='Statement Closing Balance',
        currency_field='currency_id',
        required=True,
        tracking=True,
    )

    gl_balance = fields.Monetary(
        string='General Ledger Bank Balance',
        currency_field='currency_id',
        compute='_compute_balances',
        store=True,
    )
    unmatched_payments_total = fields.Monetary(
        string='Outstanding Disbursed Payments',
        currency_field='currency_id',
        compute='_compute_balances',
        store=True,
    )
    unmatched_receipts_total = fields.Monetary(
        string='Outstanding Undeposited Receipts',
        currency_field='currency_id',
        compute='_compute_balances',
        store=True,
    )
    reconciled_difference = fields.Monetary(
        string='Unreconciled Variance',
        currency_field='currency_id',
        compute='_compute_balances',
        store=True,
    )
    is_balanced = fields.Boolean(
        string='Fully Balanced (Zero Variance)',
        compute='_compute_balances',
        store=True,
    )

    line_ids = fields.One2many(
        'contractor.bank.reconciliation.line',
        'reconciliation_id',
        string='Reconciliation Audit Items',
        copy=True,
    )

    # Segregation of Duties / Maker-Checker
    state = fields.Selection([
        ('draft', 'Draft Work Sheet'),
        ('submitted', 'Submitted for Review (Maker Done)'),
        ('approved', 'Reconciled & Certified (Checker Done)'),
        ('rejected', 'Rejected back to Preparer'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    prepared_by_id = fields.Many2one(
        'res.users',
        string='Maker: Prepared By (Accountant)',
        readonly=True,
        copy=False,
        tracking=True,
    )
    preparation_date = fields.Datetime(
        string='Preparation Date',
        readonly=True,
        copy=False,
    )
    reviewed_by_id = fields.Many2one(
        'res.users',
        string='Checker: Audited By (Senior Accountant / Manager)',
        readonly=True,
        copy=False,
        tracking=True,
    )
    review_date = fields.Datetime(
        string='Review Timestamp',
        readonly=True,
        copy=False,
    )

    @api.depends('journal_id', 'statement_date', 'closing_balance', 'line_ids.is_matched', 'line_ids.amount', 'line_ids.payment_type')
    def _compute_balances(self):
        for rec in self:
            if not rec.journal_id or not rec.statement_date:
                rec.gl_balance = 0.0
                rec.unmatched_payments_total = 0.0
                rec.unmatched_receipts_total = 0.0
                rec.reconciled_difference = 0.0
                rec.is_balanced = False
                continue

            # Compute GL Balance for Bank Account up to statement_date
            account = rec.journal_id.default_account_id
            gl_bal = 0.0
            if account:
                moves = self.env['account.move.line'].search([
                    ('account_id', '=', account.id),
                    ('date', '<=', rec.statement_date),
                    ('parent_state', '=', 'posted'),
                ])
                gl_bal = sum(moves.mapped('balance'))

            rec.gl_balance = gl_bal

            unmatched_lines = rec.line_ids.filtered(lambda l: not l.is_matched)
            out_amt = sum(unmatched_lines.filtered(lambda l: l.payment_type == 'outbound').mapped('amount'))
            in_amt = sum(unmatched_lines.filtered(lambda l: l.payment_type == 'inbound').mapped('amount'))

            rec.unmatched_payments_total = out_amt
            rec.unmatched_receipts_total = in_amt

            # Bank Statement Closing Balance = GL Balance + Outstanding Cheques (outbound) - Undeposited Receipts (inbound)
            theoretical_statement = gl_bal - in_amt + out_amt
            diff = rec.closing_balance - theoretical_statement
            rec.reconciled_difference = diff
            rec.is_balanced = abs(diff) <= 0.01

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Bank Rec')) == _('New Bank Rec'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.bank.reconciliation') or _('New Bank Rec')
        return super().create(vals_list)

    def action_fetch_unreconciled_items(self):
        """Pulls un-reconciled posted bank payments up to the statement cutoff date."""
        self.ensure_one()
        payments = self.env['account.payment'].search([
            ('journal_id', '=', self.journal_id.id),
            ('date', '<=', self.statement_date),
            ('state', '=', 'posted'),
            ('is_reconciled', '=', False),
        ])

        lines = []
        for p in payments:
            lines.append((0, 0, {
                'payment_id': p.id,
                'date': p.date,
                'partner_id': p.partner_id.id,
                'ref': p.ref or p.name,
                'payment_type': p.payment_type,
                'amount': p.amount,
                'is_matched': False,
            }))

        self.line_ids.unlink()
        self.write({'line_ids': lines})
        self._compute_balances()
        return True

    def action_submit(self):
        """Step 1 (Maker): Accountant finalizes matching and submits for audit."""
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_accountant') or self.env.user.has_group('contractor_core.group_contractor_senior_accountant')):
            raise UserError(_("Access Denied: Only an Accountant can prepare bank reconciliations."))

        self.write({
            'state': 'submitted',
            'prepared_by_id': self.env.user.id,
            'preparation_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Bank Reconciliation Submitted:</strong> Prepared by %s. Forwarded for Senior Accountant audit.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_approve(self):
        """Step 2 (Checker): Senior Accountant / Manager audits and certifies bank reconciliation."""
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_senior_accountant') or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise UserError(_("Access Denied: Only a Senior Accountant or Manager can review and certify bank reconciliations."))

        if self.prepared_by_id and self.env.user.id == self.prepared_by_id.id:
            raise UserError(_(
                "Segregation of Duties Hard-Stop:\n\n"
                "The user who prepared this reconciliation (%s) cannot also review and certify it.\n"
                "A distinct Senior Accountant or Manager must independently audit the statement."
            ) % self.prepared_by_id.name)

        if not self.is_balanced and abs(self.reconciled_difference) > 0.01:
            raise UserError(_(
                "Bank Reconciliation Variance Hard-Stop:\n\n"
                "Cannot certify reconciliation with an outstanding variance of %s %.2f.\n"
                "All items must be matched or an authorized adjustment posted before sign-off."
            ) % (self.currency_id.symbol or '', self.reconciled_difference))

        self.write({
            'state': 'approved',
            'reviewed_by_id': self.env.user.id,
            'review_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Bank Reconciliation Certified:</strong> Audited and approved by %s. Statement is fully balanced.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_reject(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_senior_accountant')):
            raise AccessError(_("Access Denied: Only a Senior Accountant or Manager can reject bank reconciliations."))
        self.write({'state': 'rejected'})
        self.message_post(
            body=Markup(_("<strong>Reconciliation Rejected:</strong> Returned to preparer for corrections.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    @api.constrains('journal_id', 'statement_date', 'state')
    def _check_unique_bank_reconciliation(self):
        for rec in self:
            if rec.state == 'rejected' or not rec.journal_id or not rec.statement_date:
                continue
            dup = self.sudo().search([
                ('id', '!=', rec.id),
                ('journal_id', '=', rec.journal_id.id),
                ('statement_date', '=', rec.statement_date),
                ('state', '!=', 'rejected'),
            ], limit=1)
            if dup:
                raise ValidationError(_(
                    "A Bank Reconciliation ('%s') already exists for journal '%s' on statement date %s."
                ) % (dup.name, rec.journal_id.display_name, rec.statement_date))

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        if any(rec.state == 'approved' for rec in self) and not set(vals.keys()).issubset(allowed_meta_fields):
            raise UserError(_("Approved Bank Reconciliations are locked and cannot be modified."))
        return super().write(vals)

    @api.model
    def _cron_check_unmatched_bank_items(self):
        """Daily cron checking for stagnant unmatched items and escalating to Manager."""
        today = fields.Date.context_today(self)
        companies = self.env['res.company'].search([])
        for comp in companies:
            sla_days = comp.bank_unmatched_escalation_days or 7
            cutoff = today - timedelta(days=sla_days)

            stagnant_lines = self.env['contractor.bank.reconciliation.line'].search([
                ('reconciliation_id.company_id', '=', comp.id),
                ('reconciliation_id.state', 'in', ['draft', 'submitted']),
                ('is_matched', '=', False),
                ('date', '<=', cutoff),
            ])

            if stagnant_lines:
                group_mgr = self.env.ref('contractor_core.group_contractor_manager', raise_if_not_found=False)
                if group_mgr:
                    managers = (group_mgr.all_user_ids | group_mgr.user_ids).filtered(lambda u: u.active)
                    act_type_id = self.env.ref('mail.mail_activity_data_todo').id
                    model_id = self.env['ir.model']._get_id('contractor.bank.reconciliation')
                    for rec in stagnant_lines.mapped('reconciliation_id'):
                        rec_lines = stagnant_lines.filtered(lambda l: l.reconciliation_id == rec)
                        total_amt = sum(rec_lines.mapped('amount'))
                        oldest_date = min(rec_lines.mapped('date'))
                        currency_sym = rec_lines[0].currency_id.symbol or ''
                        summary = f"Stagnant Bank Item Alert: {rec.name}"
                        for mgr in managers:
                            existing = self.env['mail.activity'].search([
                                ('res_model_id', '=', model_id),
                                ('res_id', '=', rec.id),
                                ('user_id', '=', mgr.id),
                                ('summary', '=', summary),
                            ], limit=1)
                            if not existing:
                                self.env['mail.activity'].create({
                                    'activity_type_id': act_type_id,
                                    'res_model_id': model_id,
                                    'res_id': rec.id,
                                    'user_id': mgr.id,
                                    'summary': summary,
                                    'note': _(
                                        "%d unmatched bank item(s) totalling %s %.2f (oldest dated %s) exceed %d-day SLA."
                                    ) % (len(rec_lines), currency_sym, total_amt, oldest_date, sla_days),
                                    'date_deadline': today,
                                })


class ContractorBankReconciliationLine(models.Model):
    _name = 'contractor.bank.reconciliation.line'
    _description = 'Contractor Bank Reconciliation Line Item'
    _order = 'date desc, id desc'

    reconciliation_id = fields.Many2one(
        'contractor.bank.reconciliation',
        string='Reconciliation Reference',
        required=True,
        ondelete='cascade',
    )
    date = fields.Date(
        string='Transaction Date',
        required=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Counterparty (Client / Supplier)',
    )
    payment_id = fields.Many2one(
        'account.payment',
        string='ERP Payment Record',
    )
    statement_line_id = fields.Many2one(
        'account.bank.statement.line',
        string='Bank Statement Line',
    )
    ref = fields.Char(
        string='Cheque / Transfer Reference',
    )
    payment_type = fields.Selection([
        ('inbound', 'Receipt / Customer Deposit'),
        ('outbound', 'Disbursement / Vendor Payment'),
    ], string='Direction', required=True, default='outbound')

    amount = fields.Monetary(
        string='Transaction Amount',
        currency_field='currency_id',
        required=True,
    )
    is_matched = fields.Boolean(
        string='Cleared on Bank Statement',
        default=False,
    )
    unmatched_reason = fields.Selection([
        ('in_transit', 'Deposit in Transit / Outstanding Cheque'),
        ('bank_charge', 'Unrecorded Bank Charges / Fees'),
        ('timing_difference', 'Clearing Timing Difference'),
        ('investigation', 'Under Bank Investigation / Query'),
    ], string='Variance Explanation', default='in_transit')

    currency_id = fields.Many2one(
        'res.currency',
        related='reconciliation_id.currency_id',
        readonly=True,
    )

    def write(self, vals):
        if any(line.reconciliation_id.state == 'approved' for line in self):
            raise UserError(_("Lines of an approved Bank Reconciliation cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if any(line.reconciliation_id.state == 'approved' for line in self):
            raise UserError(_("Lines of an approved Bank Reconciliation cannot be deleted."))
        return super().unlink()

