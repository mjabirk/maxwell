# -*- coding: utf-8 -*-
from datetime import timedelta
from dateutil.relativedelta import relativedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


# =========================================================================
# 1. Site Petty Cash Floats, Vouchers & Surprise Counts
# =========================================================================
class ContractorPettyCashFloat(models.Model):
    _name = 'contractor.petty.cash.float'
    _description = 'Contractor Site & Office Petty Cash Float'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'project_id asc, id desc'

    name = fields.Char(
        string='Float Name / Location Tag',
        required=True,
        tracking=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Assigned Project Site / Office',
        tracking=True,
        index=True,
    )
    custodian_id = fields.Many2one(
        'res.users',
        string='Designated Site Cashier / Custodian',
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
    journal_id = fields.Many2one(
        'account.journal',
        string='Petty Cash Journal',
        domain="[('type', '=', 'cash'), ('company_id', '=', company_id)]",
        required=True,
    )
    float_limit = fields.Monetary(
        string='Authorized Imprest Float Limit',
        currency_field='currency_id',
        required=True,
        tracking=True,
    )
    current_balance = fields.Monetary(
        string='Available Unspent Cash Balance',
        currency_field='currency_id',
        compute='_compute_current_balance',
        store=True,
        tracking=True,
    )
    voucher_ids = fields.One2many(
        'contractor.petty.cash.voucher',
        'float_id',
        string='Disbursement Vouchers',
    )
    surprise_count_ids = fields.One2many(
        'contractor.petty.cash.count',
        'float_id',
        string='Surprise Cash Count Audits',
    )
    state = fields.Selection([
        ('active', 'Active Imprest Float'),
        ('suspended', 'Suspended'),
        ('closed', 'Closed'),
    ], string='Status', default='active', required=True, tracking=True)
    has_unresolved_discrepancy = fields.Boolean(
        string='Has Unresolved Count Discrepancy',
        compute='_compute_discrepancy_flags',
        store=True,
    )
    repeated_discrepancy_flag = fields.Boolean(
        string='Repeated Discrepancy Flag (MD Alert)',
        default=False,
        tracking=True,
        copy=False,
    )

    @api.depends('float_limit', 'voucher_ids.amount', 'voucher_ids.state')
    def _compute_current_balance(self):
        for rec in self:
            unreplenished = rec.voucher_ids.filtered(lambda v: v.state == 'paid')
            spent = sum(unreplenished.mapped('amount'))
            rec.current_balance = max((rec.float_limit or 0.0) - spent, 0.0)

    @api.depends('surprise_count_ids.variance_amount', 'surprise_count_ids.discrepancy_resolved')
    def _compute_discrepancy_flags(self):
        for rec in self:
            unresolved = rec.surprise_count_ids.filtered(
                lambda c: abs(c.variance_amount or 0.0) > 0.01 and not c.discrepancy_resolved
            )
            rec.has_unresolved_discrepancy = bool(unresolved)

    def action_replenish_float(self):
        """Replenishes float up to imprest limit; strictly blocks if unresolved cash count discrepancy exists."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise AccessError(_("Access Denied: Only Accounting or Management users can replenish petty cash floats."))
        self._compute_discrepancy_flags()
        if self.has_unresolved_discrepancy:
            raise UserError(_(
                "Petty Cash Replenishment Blocked:\n\n"
                "Float '%s' has an unresolved surprise count cash discrepancy. "
                "Investigate and formally resolve the discrepancy before replenishing."
            ) % self.name)
        paid_vouchers = self.voucher_ids.filtered(lambda v: v.state == 'paid')
        if not paid_vouchers:
            raise UserError(_("No disbursed vouchers pending replenishment on float '%s'.") % self.name)
        replenish_amt = sum(paid_vouchers.mapped('amount'))
        paid_vouchers.with_context(bypass_state_lock=True).write({'state': 'replenished'})
        self._compute_current_balance()
        self.message_post(
            body=Markup(_("<strong>Float Replenished:</strong> %s %.2f restored to imprest limit by %s.")) % (
                self.currency_id.symbol or '', replenish_amt, self.env.user.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True


class ContractorPettyCashVoucher(models.Model):
    _name = 'contractor.petty.cash.voucher'
    _description = 'Contractor Petty Cash Expense Voucher'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.state.lock.mixin', 'contractor.no.delete.mixin']
    _order = 'voucher_date desc, id desc'

    name = fields.Char(
        string='Voucher Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Voucher'),
        index=True,
    )
    float_id = fields.Many2one(
        'contractor.petty.cash.float',
        string='Source Cash Float',
        required=True,
        tracking=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Charged Project Site',
        required=True,
        tracking=True,
    )
    voucher_date = fields.Date(
        string='Disbursement Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='float_id.company_id',
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    expense_account_id = fields.Many2one(
        'account.account',
        string='Expense Account',
        domain="[('company_ids', 'in', company_id), ('internal_group', '=', 'expense')]",
        required=True,
    )
    cost_head = fields.Selection([
        ('labour', 'Labour Cost (Block 1)'),
        ('material', 'Material Cost (Block 2)'),
        ('legal', 'Legal & Permits (Block 3)'),
        ('misc', 'Miscellaneous & Indirects (Block 4)'),
        ('subcontractor', 'Subcontractor Package'),
    ], string='Budget Cost Head', default='misc', required=True, tracking=True)
    amount = fields.Monetary(
        string='Voucher Amount',
        currency_field='currency_id',
        required=True,
        tracking=True,
    )
    recipient_name = fields.Char(
        string='Paid To (Name / Supplier)',
        required=True,
        tracking=True,
    )
    purpose = fields.Text(
        string='Commercial Description & Purpose',
        required=True,
    )
    receipt_attachment_ids = fields.Many2many(
        'ir.attachment',
        'contractor_petty_cash_voucher_attachment_rel',
        'voucher_id',
        'attachment_id',
        string='Scanned Cash Receipts / Bills',
    )
    state = fields.Selection([
        ('draft', 'Draft Voucher'),
        ('approved', 'Approved by Site Engineer'),
        ('paid', 'Disbursed to Recipient'),
        ('replenished', 'Replenished by Accounts'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    approved_by_id = fields.Many2one(
        'res.users',
        string='Approved By',
        readonly=True,
        copy=False,
    )
    move_id = fields.Many2one(
        'account.move',
        string='Accounting Entry',
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Voucher')) == _('New Voucher'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.petty.cash.voucher') or _('New Voucher')
        return super().create(vals_list)

    def action_approve(self):
        self.ensure_one()
        self._ensure_state('draft', _("approve petty cash voucher"))
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_site_engineer') or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise AccessError(_("Access Denied: Only Site Engineers, Project Managers, Accountants, or Managers can approve petty cash vouchers."))
        if not self.receipt_attachment_ids:
            raise UserError(_("Receipt Attachment Hard-Stop: A scanned receipt or vendor bill attachment is strictly mandatory."))
        self.with_context(bypass_state_lock=True).write({
            'state': 'approved',
            'approved_by_id': self.env.user.id,
        })

    def action_disburse(self):
        """Hard-Stop: Block disbursement exceeding available unspent cash balance, post tagged GL move if configured."""
        self.ensure_one()
        self._ensure_state(('draft', 'approved'), _("disburse petty cash voucher"))
        if self.amount > self.float_id.current_balance:
            raise UserError(_(
                "Petty Cash Balance Hard-Stop:\n\n"
                "Voucher amount (%s %.2f) exceeds remaining float balance (%s %.2f).\n"
                "Please request a float replenishment before making this disbursement."
            ) % (
                self.currency_id.symbol or '', self.amount,
                self.currency_id.symbol or '', self.float_id.current_balance
            ))

        vals = {'state': 'paid'}
        cash_journal = self.float_id.journal_id
        cash_acc = cash_journal.default_account_id if cash_journal else False
        if cash_journal and cash_acc and self.expense_account_id and not self.move_id:
            aml_fields = self.env['account.move.line']._fields
            am_fields = self.env['account.move']._fields
            debit_line = {
                'name': f"Petty Cash {self.name}: {self.purpose[:80]}",
                'account_id': self.expense_account_id.id,
                'debit': self.amount,
                'credit': 0.0,
            }
            if 'project_id' in aml_fields and self.project_id:
                debit_line['project_id'] = self.project_id.id
            if 'cost_head' in aml_fields:
                debit_line['cost_head'] = self.cost_head or 'misc'
            if self.project_id and hasattr(self.project_id, 'account_id') and self.project_id.account_id:
                debit_line['analytic_distribution'] = {str(self.project_id.account_id.id): 100.0}

            move_vals = {
                'journal_id': cash_journal.id,
                'date': self.voucher_date,
                'ref': f"PCV {self.name}",
                'move_type': 'entry',
                'line_ids': [
                    (0, 0, debit_line),
                    (0, 0, {
                        'name': f"Petty Cash Float Disbursement: {self.float_id.name}",
                        'account_id': cash_acc.id,
                        'debit': 0.0,
                        'credit': self.amount,
                    }),
                ],
            }
            if 'project_id' in am_fields and self.project_id:
                move_vals['project_id'] = self.project_id.id
            move = self.env['account.move'].create(move_vals)
            move.action_post()
            vals['move_id'] = move.id

        self.with_context(bypass_state_lock=True).write(vals)
        self.float_id._compute_current_balance()
        if self.project_id and 'contractor.project.budget' in self.env:
            self.env['contractor.project.budget']._refresh_project_consumption(self.project_id.ids)


class ContractorPettyCashCount(models.Model):
    _name = 'contractor.petty.cash.count'
    _description = 'Independent Surprise Petty Cash Count Audit'
    _inherit = ['contractor.state.lock.mixin']
    _order = 'count_date desc, id desc'

    def _get_state_lock_extra_allowed_fields(self, record):
        if record.state == 'verified' and not record.discrepancy_resolved:
            return ('resolution_notes',)
        return ()

    float_id = fields.Many2one(
        'contractor.petty.cash.float',
        string='Audited Float',
        required=True,
        ondelete='cascade',
    )
    count_date = fields.Date(
        string='Audit Date',
        default=fields.Date.context_today,
        required=True,
    )
    counted_by_id = fields.Many2one(
        'res.users',
        string='Auditor Conducting Surprise Count',
        default=lambda self: self.env.user,
        required=True,
    )
    custodian_id = fields.Many2one(
        'res.users',
        related='float_id.custodian_id',
        string='Float Custodian',
        readonly=True,
    )
    physical_cash_counted = fields.Monetary(
        string='Actual Physical Cash Counted',
        currency_field='currency_id',
        required=True,
    )
    expected_cash_balance = fields.Monetary(
        string='Expected ERP Cash Balance',
        currency_field='currency_id',
        related='float_id.current_balance',
        readonly=True,
    )
    variance_amount = fields.Monetary(
        string='Count Variance (Shortage / Overage)',
        currency_field='currency_id',
        compute='_compute_variance',
        store=True,
    )
    notes = fields.Text(
        string='Auditor Explanation of Discrepancies',
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='float_id.currency_id',
        readonly=True,
    )
    state = fields.Selection([
        ('draft', 'Recorded'),
        ('verified', 'Verified'),
    ], string='Audit Status', default='draft', required=True)
    discrepancy_resolved = fields.Boolean(
        string='Discrepancy Resolved',
        default=False,
    )
    resolution_notes = fields.Text(
        string='Discrepancy Resolution Notes',
    )

    @api.constrains('counted_by_id', 'custodian_id')
    def _check_independent_auditor(self):
        for rec in self:
            if rec.counted_by_id and rec.custodian_id and rec.counted_by_id == rec.custodian_id:
                raise ValidationError(_(
                    "Internal Audit Hard-Stop:\n\n"
                    "The surprise count auditor (%s) cannot be the float custodian.\n"
                    "Surprise counts must be conducted independently by an external auditor, Senior Accountant, or Manager."
                ) % rec.custodian_id.name)

    @api.depends('physical_cash_counted', 'expected_cash_balance')
    def _compute_variance(self):
        for rec in self:
            rec.variance_amount = rec.physical_cash_counted - rec.expected_cash_balance

    def _escalate_if_discrepancy(self):
        mgr_group = self.env.ref('contractor_core.group_contractor_manager', raise_if_not_found=False)
        md_group = self.env.ref('contractor_core.group_contractor_md', raise_if_not_found=False)
        for rec in self:
            if abs(rec.variance_amount or 0.0) <= 0.01:
                continue
            prior_discrepancies = rec.float_id.surprise_count_ids.filtered(
                lambda c: c.id != rec.id and abs(c.variance_amount or 0.0) > 0.01
            )
            partners = mgr_group.user_ids.mapped('partner_id') if mgr_group else self.env['res.partner']
            if prior_discrepancies:
                rec.float_id.sudo().write({'repeated_discrepancy_flag': True})
                if md_group:
                    partners |= md_group.user_ids.mapped('partner_id')
                rec.float_id.message_post(
                    body=Markup(_(
                        "<strong>REPEATED Petty Cash Discrepancy Escalation (MD Alert):</strong> "
                        "Surprise count on %s found variance of %s %.2f (%s)."
                    )) % (rec.count_date, rec.currency_id.symbol or '', rec.variance_amount, rec.notes or ''),
                    partner_ids=partners.ids,
                    message_type='notification',
                    subtype_xmlid='mail.mt_note',
                )
            else:
                rec.float_id.message_post(
                    body=Markup(_(
                        "<strong>Petty Cash Count Discrepancy Alert:</strong> "
                        "Surprise count on %s found variance of %s %.2f. Replenishment blocked until resolved."
                    )) % (rec.count_date, rec.currency_id.symbol or '', rec.variance_amount),
                    partner_ids=partners.ids,
                    message_type='notification',
                    subtype_xmlid='mail.mt_note',
                )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._escalate_if_discrepancy()
        records.mapped('float_id')._compute_discrepancy_flags()
        return records

    def action_verify_count(self):
        self.ensure_one()
        self._ensure_state('draft', _("verify cash count"))
        self.with_context(bypass_state_lock=True).write({'state': 'verified'})
        self._escalate_if_discrepancy()
        self.float_id._compute_discrepancy_flags()
        return True

    def action_resolve_discrepancy(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_senior_accountant') or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Senior Accountant or Contractor Manager can resolve cash count discrepancies."))
        self.with_context(bypass_state_lock=True).write({'discrepancy_resolved': True})
        self.float_id._compute_discrepancy_flags()
        self.float_id.message_post(
            body=Markup(_("<strong>Petty Cash Variance Resolved:</strong> Count dated %s cleared by %s.")) % (
                self.count_date, self.env.user.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True


# =========================================================================
# 2. Loans & Financing Facilities
# =========================================================================
class ContractorLoanFacility(models.Model):
    _name = 'contractor.loan.facility'
    _description = 'Contractor Bank & Shareholder Loan Facility'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'start_date desc, id desc'

    name = fields.Char(
        string='Facility Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Loan'),
        index=True,
    )
    lender_id = fields.Many2one(
        'res.partner',
        string='Lender / Financial Institution',
        required=True,
        tracking=True,
    )
    is_related_party_lender = fields.Boolean(
        string='Is Related Party Lender',
        compute='_compute_rpt_lender',
        store=True,
    )
    loan_type = fields.Selection([
        ('term_loan', 'Long-Term Project Loan'),
        ('working_capital', 'Working Capital Revolver / Overdraft'),
        ('project_finance', 'Project-Specific Back-to-Back Loan'),
        ('letters_of_credit_guarantee', 'LC / Bank Guarantee Facility'),
    ], string='Facility Category', default='term_loan', required=True, tracking=True)

    principal_amount = fields.Monetary(
        string='Principal Borrowed Amount',
        currency_field='currency_id',
        required=True,
        tracking=True,
    )
    interest_rate_pct = fields.Float(
        string='Annual Interest Rate (%)',
        required=True,
        tracking=True,
    )
    collateral_description = fields.Text(
        string='Mortgage & Collateral Pledged',
        tracking=True,
    )
    covenant_terms = fields.Text(
        string='Financial Covenants & Conditions',
        tracking=True,
    )
    covenant_compliance_status = fields.Selection([
        ('compliant', 'Compliant'),
        ('watch', 'Watch / Near Threshold'),
        ('breach', 'In Breach'),
    ], string='Covenant Compliance Status', default='compliant', tracking=True)
    last_covenant_review_date = fields.Date(
        string='Last Covenant Review Date',
        tracking=True,
    )
    register_as_related_party = fields.Boolean(
        string='Related Party Financing (Auto-Register in RPT)',
        default=False,
        tracking=True,
    )
    start_date = fields.Date(
        string='Drawdown / Facility Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    maturity_date = fields.Date(
        string='Maturity Date',
        required=True,
        tracking=True,
    )
    outstanding_balance = fields.Monetary(
        string='Current Outstanding Balance',
        currency_field='currency_id',
        compute='_compute_outstanding',
        store=True,
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
    loan_account_id = fields.Many2one(
        'account.account',
        string='Loan Liability Account',
        domain="[('company_ids', 'in', company_id), ('account_type', 'in', ['liability_current', 'liability_non_current'])]",
    )
    interest_expense_account_id = fields.Many2one(
        'account.account',
        string='Interest Expense Account',
        domain="[('company_ids', 'in', company_id), ('internal_group', '=', 'expense')]",
    )

    repayment_schedule_ids = fields.One2many(
        'contractor.loan.repayment.line',
        'facility_id',
        string='Amortization Schedule',
        copy=False,
    )
    state = fields.Selection([
        ('draft', 'Draft Proposal'),
        ('md_approved', 'Approved by MD'),
        ('active', 'Active & Drawndown'),
        ('closed', 'Fully Settled'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    md_approver_id = fields.Many2one(
        'res.users',
        string='MD Authorized By',
        readonly=True,
        copy=False,
    )
    md_approval_date = fields.Datetime(
        string='MD Approval Timestamp',
        readonly=True,
        copy=False,
    )

    @api.depends('lender_id')
    def _compute_rpt_lender(self):
        for rec in self:
            if rec.lender_id:
                rpt = self.env['contractor.related.party'].search([
                    ('partner_id', '=', rec.lender_id.id),
                    ('active', '=', True),
                ], limit=1)
                rec.is_related_party_lender = bool(rpt)
            else:
                rec.is_related_party_lender = False

    @api.depends('principal_amount', 'repayment_schedule_ids.is_paid', 'repayment_schedule_ids.principal_due')
    def _compute_outstanding(self):
        for rec in self:
            paid = sum(rec.repayment_schedule_ids.filtered(lambda l: l.is_paid).mapped('principal_due'))
            rec.outstanding_balance = max((rec.principal_amount or 0.0) - paid, 0.0)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Loan')) == _('New Loan'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.loan.facility') or _('New Loan')
        return super().create(vals_list)

    def action_approve_md(self):
        """Hard-Stop: Loans and credit facilities strictly require Managing Director approval."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Only the Managing Director can authorize financing facilities and bank loans."))

        if (self.register_as_related_party or self.is_related_party_lender) and self.lender_id:
            RPT = self.env['contractor.related.party'].sudo().with_context(active_test=False)
            existing_rpt = RPT.search([
                ('partner_id', '=', self.lender_id.id),
                ('company_id', '=', self.company_id.id),
            ], limit=1)
            if existing_rpt:
                if not existing_rpt.active:
                    existing_rpt.active = True
            else:
                RPT.create({
                    'name': self.lender_id.name,
                    'partner_id': self.lender_id.id,
                    'relationship_type': 'shareholder',
                    'company_id': self.company_id.id,
                    'notes': _("Auto-registered from MD-authorized Loan Facility %s.") % self.name,
                })
            self._compute_rpt_lender()

        self.write({
            'state': 'md_approved',
            'md_approver_id': self.env.user.id,
            'md_approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Financing Facility Approved by MD:</strong> Certified by %s for %s %.2f.")) % (
                self.env.user.name, self.currency_id.symbol or '', self.principal_amount
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_review_covenants(self):
        """Records covenant compliance review and alerts leadership if watch or breach."""
        self.ensure_one()
        self.last_covenant_review_date = fields.Date.context_today(self)
        self.message_post(
            body=Markup(_("<strong>Covenant Review Logged:</strong> Status is <strong>%s</strong> (Reviewed by %s).")) % (
                dict(self._fields['covenant_compliance_status'].selection).get(self.covenant_compliance_status, ''),
                self.env.user.name,
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def write(self, vals):
        material_fields = {
            'lender_id', 'loan_type', 'principal_amount',
            'interest_rate_pct', 'start_date', 'maturity_date', 'collateral_description',
        }
        if (set(vals.keys()) & material_fields) and not self.env.su and not self.env.user.has_group('contractor_core.group_contractor_md'):
            for rec in self:
                if rec.state in ('md_approved', 'active', 'closed'):
                    raise UserError(_(
                        "Loan Governance Hard-Stop:\n\n"
                        "Material loan terms on facility '%s' cannot be modified after MD approval without Managing Director authorization."
                    ) % rec.name)
        return super().write(vals)

    def action_drawdown(self):
        self.ensure_one()
        if self.state != 'md_approved':
            raise UserError(_("Facility must be authorized by MD prior to drawdown."))
        self.state = 'active'

    @api.model
    def _cron_accrue_loan_interest(self):
        """Monthly cron generating interest accrual entries across all active facilities."""
        today = fields.Date.context_today(self)
        active_loans = self.search([('state', '=', 'active'), ('outstanding_balance', '>', 0)])
        for loan in active_loans:
            monthly_interest = (loan.outstanding_balance * (loan.interest_rate_pct / 100.0)) / 12.0
            if monthly_interest <= 0 or not loan.interest_expense_account_id or not loan.loan_account_id:
                continue

            journal = self.env['account.journal'].search([('type', '=', 'general'), ('company_id', '=', loan.company_id.id)], limit=1)
            try:
                move = self.env['account.move'].create({
                    'journal_id': journal.id if journal else False,
                    'date': today,
                    'ref': f"Interest Accrual {loan.name} ({today})",
                    'move_type': 'entry',
                    'line_ids': [
                        (0, 0, {
                            'name': f"Interest Accrual: {loan.name}",
                            'account_id': loan.interest_expense_account_id.id,
                            'debit': monthly_interest,
                            'credit': 0.0,
                        }),
                        (0, 0, {
                            'name': f"Interest Payable: {loan.name}",
                            'account_id': loan.loan_account_id.id,
                            'debit': 0.0,
                            'credit': monthly_interest,
                        }),
                    ],
                })
                move.action_post()
            except Exception:
                continue


class ContractorLoanRepaymentLine(models.Model):
    _name = 'contractor.loan.repayment.line'
    _description = 'Loan Amortization Repayment Schedule'
    _order = 'due_date asc, id asc'

    facility_id = fields.Many2one(
        'contractor.loan.facility',
        string='Loan Facility',
        required=True,
        ondelete='cascade',
    )
    due_date = fields.Date(
        string='Instalment Due Date',
        required=True,
    )
    principal_due = fields.Monetary(
        string='Principal Due',
        currency_field='currency_id',
        required=True,
    )
    interest_due = fields.Monetary(
        string='Interest Due',
        currency_field='currency_id',
        required=True,
    )
    total_due = fields.Monetary(
        string='Total Instalment Amount',
        currency_field='currency_id',
        compute='_compute_total',
    )
    is_paid = fields.Boolean(
        string='Settled / Paid',
        default=False,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='facility_id.currency_id',
        readonly=True,
    )

    @api.depends('principal_due', 'interest_due')
    def _compute_total(self):
        for line in self:
            line.total_due = line.principal_due + line.interest_due


# =========================================================================
# 3. Monthly Accounting Provisions Engine & HRMS EOSB Sync
# =========================================================================
class ContractorAccountingProvision(models.Model):
    _name = 'contractor.accounting.provision'
    _description = 'Contractor Monthly Accounting Provision Engine'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'provision_date desc, id desc'

    name = fields.Char(
        string='Provision Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Provision'),
        index=True,
    )
    provision_date = fields.Date(
        string='Provision Month-End Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    provision_type = fields.Selection([
        ('unbilled_vendor_liability', 'Unbilled Subcontractor / Vendor Accrual'),
        ('hrms_eosb_liability_sync', 'Monthly Permanent Staff EOSB Gratuity Accrual'),
        ('bad_doubtful_debts', 'Allowance for Expected Credit Losses (ECL / Bad Debts)'),
        ('slow_moving_stock_writedown', 'Slow-Moving / Surplus Stock Write-Down'),
        ('accrued_expense', 'General Operating Expense Accrual'),
    ], string='Provision Classification', required=True, tracking=True)

    calculation_basis = fields.Text(
        string='Mandatory Calculation Methodology & Audit Basis',
        required=True,
        tracking=True,
    )
    amount = fields.Monetary(
        string='Provision Amount',
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
    expense_account_id = fields.Many2one(
        'account.account',
        string='Expense Account (P&L)',
        domain="[('company_ids', 'in', company_id), ('internal_group', '=', 'expense')]",
        required=True,
    )
    provision_account_id = fields.Many2one(
        'account.account',
        string='Provision Account (Balance Sheet Liability)',
        domain="[('company_ids', 'in', company_id), ('account_type', 'in', ['liability_current', 'liability_non_current', 'asset_current'])]",
        required=True,
    )
    journal_id = fields.Many2one(
        'account.journal',
        string='Journal',
        domain="[('company_id', '=', company_id), ('type', '=', 'general')]",
        required=True,
    )
    state = fields.Selection([
        ('draft', 'Draft Calculation'),
        ('approved', 'Approved by Manager'),
        ('posted', 'Posted to GL'),
        ('reversed', 'Reversed upon Settlement'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    move_id = fields.Many2one(
        'account.move',
        string='Provision Journal Entry',
        readonly=True,
        copy=False,
    )
    requires_md_approval = fields.Boolean(
        string='Requires MD Approval',
        compute='_compute_requires_md_approval',
        store=True,
    )

    @api.depends('provision_type', 'amount', 'company_id.po_md_approval_threshold')
    def _compute_requires_md_approval(self):
        for rec in self:
            threshold = rec.company_id.po_md_approval_threshold or 50000.0
            rec.requires_md_approval = (rec.provision_type == 'bad_doubtful_debts' and (rec.amount or 0.0) >= threshold)
    reversal_move_id = fields.Many2one(
        'account.move',
        string='Settlement Reversal Entry',
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Provision')) == _('New Provision'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.accounting.provision') or _('New Provision')
        return super().create(vals_list)

    def action_sync_hrms_eosb(self):
        """Pulls current total EOSB liability across all active permanent employees and computes delta provision."""
        self.ensure_one()
        perm_staff = self.env['hr.employee'].search([
            ('workforce_category', '=', 'permanent'),
            ('company_id', '=', self.company_id.id),
            ('active', '=', True),
        ])

        today = fields.Date.context_today(self)
        total_eosb = 0.0
        for emp in perm_staff:
            join_date = emp.create_date.date() if emp.create_date else today
            years = (today - join_date).days / 365.25
            if years >= 1.0:
                daily_basic = (emp.monthly_basic_salary or 0.0) / 30.0
                first_5_days = getattr(self.company_id, 'eosb_first_5yr_days', 0) or 21.0
                after_5_days = getattr(self.company_id, 'eosb_after_5yr_days', 0) or 30.0
                first_5 = min(years, 5.0)
                beyond_5 = max(years - 5.0, 0.0)
                total_eosb += (first_5 * first_5_days * daily_basic) + (beyond_5 * after_5_days * daily_basic)

        # Read existing balance on provision_account_id
        existing_bal = 0.0
        if self.provision_account_id:
            lines = self.env['account.move.line'].search([
                ('account_id', '=', self.provision_account_id.id),
                ('parent_state', '=', 'posted'),
            ])
            existing_bal = abs(sum(lines.mapped('balance')))

        delta = max(total_eosb - existing_bal, 0.0)
        self.write({
            'provision_type': 'hrms_eosb_liability_sync',
            'amount': delta,
            'calculation_basis': _(
                "HRMS EOSB Sync: %d permanent staff audited.\n"
                "Total Statutory Cumulative EOSB Obligation: %s %.2f\n"
                "Current General Ledger Balance: %s %.2f\n"
                "Monthly Delta Provision Required: %s %.2f"
            ) % (
                len(perm_staff), self.currency_id.symbol or '', total_eosb,
                self.currency_id.symbol or '', existing_bal,
                self.currency_id.symbol or '', delta
            )
        })

    def action_approve(self):
        self.ensure_one()
        if self.requires_md_approval:
            if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_md')):
                raise AccessError(_(
                    "Access Denied: Material doubtful debt provisions (%s %.2f or above) require Managing Director authorization."
                ) % (self.currency_id.symbol or '', self.company_id.po_md_approval_threshold or 50000.0))
        elif not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Manager can authorize accounting provisions."))
        self.state = 'approved'

    def action_post(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise AccessError(_("Access Denied: Only Accounting or Management users can post accounting provisions."))
        if self.state != 'approved':
            raise UserError(_("Provision must be approved prior to posting."))
        if self.move_id:
            raise UserError(_("An accounting journal entry (%s) has already been posted for this provision.") % self.move_id.name)
        if 'contractor.period.close' in self.env:
            self.env['contractor.period.close']._check_period_not_locked(self.provision_date, self.company_id.id)

        move = self.env['account.move'].create({
            'journal_id': self.journal_id.id,
            'date': self.provision_date,
            'ref': f"Provision {self.name} - {self.provision_type}",
            'move_type': 'entry',
            'line_ids': [
                (0, 0, {
                    'name': f"Provision Expense: {self.name}",
                    'account_id': self.expense_account_id.id,
                    'debit': self.amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': f"Provision Liability: {self.name}",
                    'account_id': self.provision_account_id.id,
                    'debit': 0.0,
                    'credit': self.amount,
                }),
            ],
        })
        move.action_post()
        self.write({
            'state': 'posted',
            'move_id': move.id,
        })

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        if any(rec.state == 'posted' for rec in self) and not set(vals.keys()).issubset(allowed_meta_fields):
            raise UserError(_("Posted Accounting Provisions are locked and cannot be modified."))
        return super().write(vals)


# =========================================================================
# 4. Chart of Accounts (COA) Governance Extension
# =========================================================================
class AccountAccountCOAGovernance(models.Model):
    _inherit = 'account.account'

    is_project_cost_head = fields.Boolean(
        string='Is Project Cost Head Account',
        default=False,
        help='Indicates this account is tied directly to project budget cost control blocks.',
    )
    cost_head_category = fields.Selection([
        ('labour', 'Labour Direct Cost'),
        ('material', 'Material Direct Cost'),
        ('subcontractor', 'Subcontractor Cost'),
        ('equipment', 'Plant & Equipment Cost'),
        ('overhead', 'Site Overhead & Preliminaries'),
        ('none', 'Not a Cost Head'),
    ], string='Project Cost Head Classification', default='none')

    mapping_history_ids = fields.One2many(
        'contractor.account.mapping.history',
        'account_id',
        string='Mapping Reclassification History',
    )
    coa_approval_state = fields.Selection([
        ('approved', 'Approved & Active'),
        ('proposed', 'Proposed by Senior Accountant'),
        ('manager_approved', 'Manager Approved – Pending MD'),
    ], string='COA Approval Status', default='approved', tracking=True)
    mapped_previous_account_id = fields.Many2one(
        'account.account',
        string='Mapped Previous Account (Restructure)',
        tracking=True,
    )
    restructure_effective_date = fields.Date(
        string='Restructure Effective Date',
        tracking=True,
    )
    coa_change_reason = fields.Text(
        string='COA Change / Restructure Justification',
    )

    def action_propose_coa_change(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_senior_accountant') or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Senior Accountant or Manager can propose COA structural changes."))
        self.coa_approval_state = 'proposed'
        return True

    def action_manager_approve_coa(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Manager can review COA changes."))
        self.coa_approval_state = 'manager_approved'
        return True

    def action_md_approve_coa(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_md')):
            raise AccessError(_("Access Denied: Only the Managing Director can finalize COA changes or restructurings."))
        self.coa_approval_state = 'approved'
        self.env['contractor.account.mapping.history'].create({
            'account_id': self.id,
            'effective_date': self.restructure_effective_date or fields.Date.context_today(self),
            'old_category': self.mapped_previous_account_id.code if self.mapped_previous_account_id else self.cost_head_category,
            'new_category': self.code or self.cost_head_category,
            'reason': self.coa_change_reason or _("COA structure / mapping approved by Managing Director %s") % self.env.user.name,
            'authorized_by_id': self.env.user.id,
        })
        return True

    def write(self, vals):
        if 'cost_head_category' in vals or 'is_project_cost_head' in vals:
            if not self.env.user.has_group('contractor_core.group_contractor_md'):
                raise UserError(_(
                    "COA Governance Hard-Stop:\n\n"
                    "Modifying project cost-head account mappings requires Managing Director authorization.\n"
                    "Contact Finance Leadership to reclassify chart of account cost mappings."
                ))
            for acc in self:
                old_val = acc.cost_head_category
                new_val = vals.get('cost_head_category', old_val)
                self.env['contractor.account.mapping.history'].create({
                    'account_id': acc.id,
                    'effective_date': fields.Date.context_today(acc),
                    'old_category': old_val,
                    'new_category': new_val,
                    'reason': _("Reclassified by Managing Director %s") % self.env.user.name,
                    'authorized_by_id': self.env.user.id,
                })
        return super().write(vals)


class ContractorAccountMappingHistory(models.Model):
    _name = 'contractor.account.mapping.history'
    _description = 'COA Cost Head Mapping Audit History'
    _order = 'effective_date desc, id desc'

    account_id = fields.Many2one(
        'account.account',
        string='Account',
        required=True,
        ondelete='cascade',
    )
    effective_date = fields.Date(
        string='Effective Date',
        required=True,
    )
    old_category = fields.Char(
        string='Prior Cost Head Mapping',
    )
    new_category = fields.Char(
        string='New Cost Head Mapping',
    )
    reason = fields.Text(
        string='Reclassification Business Justification',
        required=True,
    )
    authorized_by_id = fields.Many2one(
        'res.users',
        string='Authorized By (MD)',
    )


# =========================================================================
# 5. Employee Advances & Staff Loans Tracking (SOP Finance Sec 5)
# =========================================================================
class ContractorEmployeeAdvance(models.Model):
    _name = 'contractor.employee.advance'
    _description = 'Contractor Employee Salary Advance & Personal Loan'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.state.lock.mixin', 'contractor.no.delete.mixin']
    _order = 'request_date desc, id desc'

    name = fields.Char(
        string='Advance Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Advance'),
        index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Employee / Operative',
        required=True,
        tracking=True,
        index=True,
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
    request_date = fields.Date(
        string='Request Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    advance_type = fields.Selection([
        ('salary_advance', 'Monthly Salary Advance'),
        ('personal_loan', 'Staff Personal Loan'),
        ('site_operational_advance', 'Site Operational Cash Advance'),
    ], string='Advance Category', default='salary_advance', required=True, tracking=True)

    advance_amount = fields.Monetary(
        string='Advance Amount',
        currency_field='currency_id',
        required=True,
        tracking=True,
    )
    monthly_installment = fields.Monetary(
        string='Monthly Payroll Installment',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    recovered_amount = fields.Monetary(
        string='Recovered Amount to Date',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    outstanding_balance = fields.Monetary(
        string='Outstanding Balance',
        currency_field='currency_id',
        compute='_compute_outstanding_balance',
        store=True,
    )
    reason = fields.Text(
        string='Purpose / Justification',
        required=True,
    )
    state = fields.Selection([
        ('draft', 'Draft Request'),
        ('submitted', 'Submitted to HR'),
        ('hr_verified', 'HR Verified'),
        ('approved', 'Management Approved'),
        ('disbursed', 'Disbursed (Active Recovery)'),
        ('recovered', 'Fully Recovered'),
        ('deducted_at_exit', 'Deducted at Exit Settlement'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    hr_verified_by_id = fields.Many2one(
        'res.users',
        string='HR Verified By',
        readonly=True,
        copy=False,
    )
    approved_by_id = fields.Many2one(
        'res.users',
        string='Management Approved By',
        readonly=True,
        copy=False,
    )
    disbursement_journal_id = fields.Many2one(
        'account.journal',
        string='Disbursement Cash / Bank Journal',
        domain="[('type', 'in', ['bank', 'cash']), ('company_id', '=', company_id)]",
    )
    account_move_id = fields.Many2one(
        'account.move',
        string='Disbursement Journal Entry',
        readonly=True,
        copy=False,
    )

    @api.depends('advance_amount', 'recovered_amount')
    def _compute_outstanding_balance(self):
        for rec in self:
            rec.outstanding_balance = max((rec.advance_amount or 0.0) - (rec.recovered_amount or 0.0), 0.0)

    @api.constrains('advance_amount', 'monthly_installment')
    def _check_positive_amounts(self):
        for rec in self:
            if (rec.advance_amount or 0.0) <= 0.0:
                raise ValidationError(_("Advance Amount must be strictly positive."))
            if (rec.monthly_installment or 0.0) < 0.0:
                raise ValidationError(_("Monthly Payroll Installment cannot be negative."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Advance')) == _('New Advance'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.employee.advance') or _('New Advance')
        return super().create(vals_list)

    def action_submit(self):
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Only draft advance requests can be submitted."))
        self.with_context(bypass_state_lock=True).write({'state': 'submitted'})
        return True

    def action_hr_verify(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_hr_officer') or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only HR Officers or Managers can verify employee advances."))
        if self.state != 'submitted':
            raise UserError(_("Only submitted advance requests can be HR verified."))
        self.with_context(bypass_state_lock=True).write({
            'state': 'hr_verified',
            'hr_verified_by_id': self.env.user.id,
        })
        return True

    def action_approve(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager') or self.env.user.has_group('contractor_core.group_contractor_md')):
            raise AccessError(_("Access Denied: Only Contractor Management or MD can approve employee advances."))
        if self.state not in ('submitted', 'hr_verified'):
            raise UserError(_("Advance request must be submitted or HR verified before approval."))
        self.with_context(bypass_state_lock=True).write({
            'state': 'approved',
            'approved_by_id': self.env.user.id,
        })
        return True

    def action_disburse(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise AccessError(_("Access Denied: Only Accounting or Management users can disburse employee advances."))
        if self.state != 'approved':
            raise UserError(_("Only management-approved advances can be disbursed."))

        vals = {'state': 'disbursed'}
        journal = self.disbursement_journal_id or self.env['account.journal'].search([
            ('type', 'in', ['bank', 'cash']),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        receivable_acc = self.env['account.account'].search([
            ('account_type', 'in', ['asset_receivable', 'asset_current']),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1)
        credit_acc = journal.default_account_id if journal else False
        if journal and receivable_acc and credit_acc and not self.account_move_id:
            move = self.env['account.move'].create({
                'journal_id': journal.id,
                'date': fields.Date.context_today(self),
                'ref': f"Emp Advance {self.name} - {self.employee_id.name}",
                'move_type': 'entry',
                'line_ids': [
                    (0, 0, {
                        'name': f"Employee Advance Receivable: {self.employee_id.name}",
                        'account_id': receivable_acc.id,
                        'partner_id': self.employee_id.work_contact_id.id if self.employee_id.work_contact_id else False,
                        'debit': self.advance_amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': f"Employee Advance Disbursement: {self.name}",
                        'account_id': credit_acc.id,
                        'debit': 0.0,
                        'credit': self.advance_amount,
                    }),
                ],
            })
            move.action_post()
            vals['account_move_id'] = move.id

        self.with_context(bypass_state_lock=True).write(vals)
        self.message_post(
            body=Markup(_("<strong>Employee Advance Disbursed:</strong> %s %.2f to %s. Active for payroll deduction.")) % (
                self.currency_id.symbol or '', self.advance_amount, self.employee_id.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_decline(self):
        self.ensure_one()
        self._ensure_state(('draft', 'submitted', 'hr_verified', 'approved'), _("decline advance request"))
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager') or self.env.user.has_group('contractor_core.group_contractor_hr_officer')):
            raise AccessError(_("Access Denied: Only HR or Management can decline employee advances."))
        return {
            'name': _('Decline Employee Advance Request'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': 'decline',
                'default_target_state': 'declined',
            },
        }


