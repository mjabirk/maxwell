# -*- coding: utf-8 -*-
from datetime import timedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorRelatedParty(models.Model):
    _name = 'contractor.related.party'
    _description = 'Contractor Related Party (RPT) Register'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'name asc, id desc'

    name = fields.Char(
        string='Related Party Name',
        required=True,
        tracking=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Partner / Contact Card',
        required=True,
        tracking=True,
        index=True,
    )
    relationship_type = fields.Selection([
        ('shareholder', 'Shareholder / Owner'),
        ('director', 'Director / Board Member'),
        ('key_management', 'Key Management Personnel'),
        ('close_family', 'Close Family Member'),
        ('affiliate_company', 'Affiliate / Sister Entity'),
    ], string='Relationship Category', required=True, tracking=True)

    holding_percentage = fields.Float(
        string='Shareholding / Beneficial Interest (%)',
        default=0.0,
        tracking=True,
    )
    notes = fields.Text(
        string='Statutory Relationship Disclosures',
    )
    active = fields.Boolean(
        string='Active',
        default=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        required=True,
    )

    _check_partner_unique = models.Constraint(
        'UNIQUE(partner_id, company_id)',
        'This partner is already registered in the Related Party Register.'
    )


# =========================================================================
# Transaction Extension for RPT Detection & Materiality Gate
# =========================================================================
class AccountMoveRPTExtension(models.Model):
    _inherit = 'account.move'

    is_rpt_transaction = fields.Boolean(
        string='Related Party Transaction (RPT)',
        compute='_compute_rpt_details',
        store=True,
        readonly=False,
        tracking=True,
    )
    rpt_id = fields.Many2one(
        'contractor.related.party',
        string='Identified Related Party Profile',
        compute='_compute_rpt_details',
        store=True,
        readonly=False,
    )
    rpt_nature = fields.Selection([
        ('trade_goods_services', 'Commercial Goods & Subcontracting'),
        ('advances_loans', 'Director / Intercompany Loans & Advances'),
        ('equity_drawings', 'Capital Injection / Drawings'),
        ('management_fees', 'Shared Services & Overhead Charges'),
        ('asset_transfer', 'Transfer / Lease of Fixed Assets'),
        ('other', 'Other Related Party Flow'),
    ], string='RPT Nature', default='trade_goods_services', tracking=True)

    is_arms_length = fields.Boolean(
        string='Conducted at Arm’s Length',
        default=True,
        tracking=True,
        help='Tick if prices and terms match fair market conditions benchmarked against unrelated third parties.',
    )
    arms_length_deviation_justification = fields.Text(
        string='Arm’s Length Deviation Justification',
        tracking=True,
    )
    rpt_manager_approved = fields.Boolean(
        string='RPT Sign-Off Granted',
        default=False,
        tracking=True,
        copy=False,
    )
    rpt_manager_approver_id = fields.Many2one(
        'res.users',
        string='RPT Approved By',
        readonly=True,
        copy=False,
    )
    rpt_manager_approval_date = fields.Datetime(
        string='RPT Approval Timestamp',
        readonly=True,
        copy=False,
    )

    @api.depends('partner_id')
    def _compute_rpt_details(self):
        for move in self:
            if move.partner_id:
                rpt = self.env['contractor.related.party'].search([
                    ('partner_id', '=', move.partner_id.id),
                    ('active', '=', True),
                ], limit=1)
                if rpt:
                    move.is_rpt_transaction = True
                    move.rpt_id = rpt.id
                    continue
            move.is_rpt_transaction = False
            move.rpt_id = False

    def action_approve_rpt(self):
        """Authorizes material RPT transaction."""
        self.ensure_one()
        threshold = self.company_id.rpt_materiality_threshold or 50000.0
        amount = abs(self.amount_total)
        requires_md = amount >= threshold

        required_group = 'contractor_core.group_contractor_md' if requires_md else 'contractor_core.group_contractor_manager'
        if not self.env.user.has_group(required_group):
            role_title = _("Managing Director") if requires_md else _("Contractor Manager")
            raise UserError(_("Access Denied: Only a %s can approve related party transaction of %s %.2f.") % (
                role_title, self.currency_id.symbol or '', amount
            ))

        if not self.is_arms_length:
            justification = (self.arms_length_deviation_justification or '').strip()
            if len(justification) < 15:
                raise UserError(_("Mandatory justification (minimum 15 characters) required when transaction is not at arm's length."))

        self.write({
            'rpt_manager_approved': True,
            'rpt_manager_approver_id': self.env.user.id,
            'rpt_manager_approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>RPT Approved:</strong> Certified by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_post(self):
        for move in self:
            if move.is_rpt_transaction:
                threshold = move.company_id.rpt_materiality_threshold or 10000.0
                if abs(move.amount_total) >= threshold and not move.rpt_manager_approved:
                    raise UserError(_(
                        "Related Party Governance Hard-Stop:\n\n"
                        "Transaction with '%s' is categorized as a Material Related Party Transaction (%s %.2f >= Threshold %s %.2f).\n\n"
                        "Posting is blocked until formal Management / Board RPT Approval is recorded."
                    ) % (
                        move.partner_id.name, move.currency_id.symbol or '', abs(move.amount_total),
                        move.currency_id.symbol or '', threshold
                    ))
                if not move.is_arms_length:
                    justification = (move.arms_length_deviation_justification or '').strip()
                    if len(justification) < 15:
                        raise UserError(_("Documented Arm's Length Deviation Justification is required before posting."))
        return super().action_post()


class SaleOrderRPTExtension(models.Model):
    _inherit = 'sale.order'

    is_rpt_transaction = fields.Boolean(
        string='Is Related Party Transaction (RPT)',
        compute='_compute_rpt_details',
        store=True,
    )
    rpt_id = fields.Many2one(
        'contractor.related.party',
        string='Related Party Profile',
        compute='_compute_rpt_details',
        store=True,
    )

    @api.depends('partner_id', 'company_id')
    def _compute_rpt_details(self):
        RPT = self.env['contractor.related.party'].sudo()
        for order in self:
            if order.partner_id:
                rpt = RPT.search([
                    ('partner_id', '=', order.partner_id.id),
                    ('company_id', '=', order.company_id.id),
                    ('active', '=', True),
                ], limit=1)
                order.is_rpt_transaction = bool(rpt)
                order.rpt_id = rpt.id if rpt else False
            else:
                order.is_rpt_transaction = False
                order.rpt_id = False

    def action_confirm(self):
        res = super().action_confirm()
        for order in self.filtered('is_rpt_transaction'):
            order.message_post(
                body=Markup(_(
                    "<strong>Point-of-Entry RPT Disclosure:</strong> Customer '%s' is registered in the statutory Related Party Register."
                )) % (order.partner_id.name or ''),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
        return res


class PurchaseOrderRPTExtension(models.Model):
    _inherit = 'purchase.order'

    is_rpt_transaction = fields.Boolean(
        string='Is Related Party Transaction (RPT)',
        compute='_compute_rpt_details',
        store=True,
    )
    rpt_id = fields.Many2one(
        'contractor.related.party',
        string='Related Party Profile',
        compute='_compute_rpt_details',
        store=True,
    )

    @api.depends('partner_id', 'company_id')
    def _compute_rpt_details(self):
        RPT = self.env['contractor.related.party'].sudo()
        for order in self:
            if order.partner_id:
                rpt = RPT.search([
                    ('partner_id', '=', order.partner_id.id),
                    ('company_id', '=', order.company_id.id),
                    ('active', '=', True),
                ], limit=1)
                order.is_rpt_transaction = bool(rpt)
                order.rpt_id = rpt.id if rpt else False
            else:
                order.is_rpt_transaction = False
                order.rpt_id = False

    def button_confirm(self):
        res = super().button_confirm()
        for order in self.filtered('is_rpt_transaction'):
            order.message_post(
                body=Markup(_(
                    "<strong>Point-of-Entry RPT Disclosure:</strong> Vendor '%s' is registered in the statutory Related Party Register."
                )) % (order.partner_id.name or ''),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
        return res


# =========================================================================
# Partner & Shareholder Dedicated Current Account Controls
# =========================================================================
class ContractorPartnerCurrentAccount(models.Model):
    _name = 'contractor.partner.current.account'
    _description = 'Contractor Partner & Shareholder Current Account'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'partner_id asc, id desc'

    name = fields.Char(
        string='Current Account Title',
        required=True,
        tracking=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Shareholder / Partner',
        required=True,
        tracking=True,
    )
    rpt_id = fields.Many2one(
        'contractor.related.party',
        string='Linked RPT Profile',
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
    account_id = fields.Many2one(
        'account.account',
        string='Dedicated Equity / Current Account',
        domain="[('company_ids', 'in', company_id), ('account_type', 'in', ['equity', 'liability_current'])]",
        required=True,
        help='Dedicated balance sheet account for this partner. Generic suspense accounts are strictly blocked.',
    )

    line_ids = fields.One2many(
        'contractor.partner.current.account.line',
        'current_account_id',
        string='Drawings, Injections & Expenses',
        copy=False,
    )
    total_injections = fields.Monetary(
        string='Cumulative Capital Injected',
        currency_field='currency_id',
        compute='_compute_balances',
        store=True,
    )
    total_drawings = fields.Monetary(
        string='Cumulative Drawings & Personal Expenses',
        currency_field='currency_id',
        compute='_compute_balances',
        store=True,
    )
    net_partner_balance = fields.Monetary(
        string='Net Current Account Equity',
        currency_field='currency_id',
        compute='_compute_balances',
        store=True,
    )
    last_reconciled_date = fields.Date(
        string='Last Month-End Reconciled Date',
        tracking=True,
        copy=False,
    )
    last_reconciled_by_id = fields.Many2one(
        'res.users',
        string='Last Reconciled By',
        readonly=True,
        copy=False,
    )
    has_long_outstanding_balance = fields.Boolean(
        string='Has Long-Outstanding Balance (90+ Days)',
        compute='_compute_long_outstanding',
        store=True,
    )
    resolution_plan = fields.Text(
        string='Long-Outstanding Balance Resolution Plan',
        tracking=True,
    )

    @api.depends('line_ids.state', 'line_ids.amount', 'line_ids.transaction_type')
    def _compute_balances(self):
        for rec in self:
            posted = rec.line_ids.filtered(lambda l: l.state == 'posted')
            inj = sum(posted.filtered(lambda l: l.transaction_type in ('capital_injection', 'profit_distribution')).mapped('amount'))
            drw = sum(posted.filtered(lambda l: l.transaction_type in ('drawing', 'advance', 'personal_expense_reimbursement')).mapped('amount'))
            rec.total_injections = inj
            rec.total_drawings = drw
            rec.net_partner_balance = inj - drw

    @api.depends('net_partner_balance', 'line_ids.date', 'line_ids.state')
    def _compute_long_outstanding(self):
        cutoff = fields.Date.context_today(self) - timedelta(days=90)
        for rec in self:
            if abs(rec.net_partner_balance or 0.0) <= 0.01:
                rec.has_long_outstanding_balance = False
                continue
            old_posted = rec.line_ids.filtered(lambda l: l.state == 'posted' and l.date and l.date <= cutoff)
            rec.has_long_outstanding_balance = bool(old_posted)

    def action_reconcile_month_end(self):
        """Certifies month-end partner current account reconciliation; enforces resolution plan on aged balances."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_senior_accountant') or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Senior Accountant or Contractor Manager can certify partner current account reconciliations."))
        self._compute_long_outstanding()
        if self.has_long_outstanding_balance and not (self.resolution_plan or '').strip():
            raise UserError(_(
                "Aged Partner Balance Resolution Required:\n\n"
                "Current account '%s' has a long-outstanding balance older than 90 days. "
                "Enter a documented Resolution Plan before certifying month-end reconciliation."
            ) % self.name)
        today = fields.Date.context_today(self)
        self.write({
            'last_reconciled_date': today,
            'last_reconciled_by_id': self.env.user.id,
        })
        self.message_post(
            body=Markup(_("<strong>Partner Current Account Reconciled:</strong> Month-end balance (%s %.2f) confirmed on %s by %s.")) % (
                self.currency_id.symbol or '', self.net_partner_balance, today, self.env.user.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True


class ContractorPartnerCurrentAccountLine(models.Model):
    _name = 'contractor.partner.current.account.line'
    _description = 'Partner Current Account Transaction Line'
    _inherit = ['contractor.state.lock.mixin']
    _order = 'date desc, id desc'

    current_account_id = fields.Many2one(
        'contractor.partner.current.account',
        string='Partner Account',
        required=True,
        ondelete='cascade',
    )
    date = fields.Date(
        string='Transaction Date',
        default=fields.Date.context_today,
        required=True,
    )
    transaction_type = fields.Selection([
        ('capital_injection', 'Capital Injection (Credit Equity)'),
        ('drawing', 'Shareholder Drawing (Debit Equity)'),
        ('advance', 'Short-Term Advance to Partner (Debit)'),
        ('personal_expense_reimbursement', 'Company Settlement of Personal Expense (Debit)'),
        ('profit_distribution', 'Annual Profit / Dividend Share (Credit Equity)'),
    ], string='Transaction Type', required=True)

    amount = fields.Monetary(
        string='Amount',
        currency_field='currency_id',
        required=True,
    )
    purpose = fields.Text(
        string='Documented Nature & Commercial Purpose',
        required=True,
    )
    state = fields.Selection([
        ('draft', 'Draft Entry'),
        ('approved', 'Approved by MD'),
        ('posted', 'Posted to Ledger'),
    ], string='Status', default='draft', required=True, copy=False)

    approved_by_id = fields.Many2one(
        'res.users',
        string='Authorized By (MD)',
        readonly=True,
        copy=False,
    )
    approval_date = fields.Datetime(
        string='Approval Timestamp',
        readonly=True,
        copy=False,
    )
    move_id = fields.Many2one(
        'account.move',
        string='Journal Entry',
        readonly=True,
        copy=False,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='current_account_id.currency_id',
        readonly=True,
    )

    def action_approve(self):
        """Hard-Stop: Drawings & personal expenses strictly require Managing Director sign-off."""
        self.ensure_one()
        self._ensure_state('draft', _("approve partner account entry"))
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Partner drawings and advances strictly require Managing Director authorization."))

        if len((self.purpose or '').strip()) < 10:
            raise UserError(_("A clear business purpose (minimum 10 characters) is required for partner transactions."))

        self.with_context(bypass_state_lock=True).write({
            'state': 'approved',
            'approved_by_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })

    def action_post(self):
        """Creates GL entry using the partner's dedicated equity account."""
        self.ensure_one()
        if self.state != 'approved':
            raise UserError(_("Transaction must be authorized by MD prior to posting."))

        account = self.current_account_id.account_id
        company = self.current_account_id.company_id
        journal = self.env['account.journal'].search([('type', '=', 'general'), ('company_id', '=', company.id)], limit=1)
        cash_or_bank = self.env['account.account'].search([('account_type', 'in', ['asset_cash', 'asset_current']), ('company_ids', 'in', company.ids)], limit=1)

        move_lines = []
        if self.transaction_type in ('capital_injection', 'profit_distribution'):
            # Inbound: Debit Cash/Bank, Credit Partner Equity
            move_lines.append((0, 0, {
                'name': f"{self.transaction_type}: {self.purpose}",
                'account_id': cash_or_bank.id,
                'debit': self.amount,
                'credit': 0.0,
                'partner_id': self.current_account_id.partner_id.id,
            }))
            move_lines.append((0, 0, {
                'name': f"Partner Equity: {self.current_account_id.partner_id.name}",
                'account_id': account.id,
                'debit': 0.0,
                'credit': self.amount,
                'partner_id': self.current_account_id.partner_id.id,
            }))
        else:
            # Outbound: Debit Partner Equity, Credit Cash/Bank
            move_lines.append((0, 0, {
                'name': f"Partner Drawing/Advance: {self.current_account_id.partner_id.name}",
                'account_id': account.id,
                'debit': self.amount,
                'credit': 0.0,
                'partner_id': self.current_account_id.partner_id.id,
            }))
            move_lines.append((0, 0, {
                'name': f"{self.transaction_type}: {self.purpose}",
                'account_id': cash_or_bank.id,
                'debit': 0.0,
                'credit': self.amount,
                'partner_id': self.current_account_id.partner_id.id,
            }))

        move = self.env['account.move'].create({
            'journal_id': journal.id if journal else False,
            'date': self.date,
            'ref': f"Partner Flow {self.current_account_id.partner_id.name}",
            'move_type': 'entry',
            'line_ids': move_lines,
        })
        move.action_post()

        self.with_context(bypass_state_lock=True).write({
            'state': 'posted',
            'move_id': move.id,
        })

