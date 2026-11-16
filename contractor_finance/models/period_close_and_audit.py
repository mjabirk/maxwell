# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class ContractorPeriodClose(models.Model):
    _name = 'contractor.period.close'
    _description = 'Contractor 10-Point Period Close Checklist & Lock Engine'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin', 'contractor.reason.mixin']
    _order = 'date_end desc, id desc'

    name = fields.Char(
        string='Period Close Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Period Close'),
        index=True,
    )
    period_type = fields.Selection([
        ('month_end', 'Monthly Financial Close'),
        ('year_end', 'Fiscal Year-End Statutory Close'),
    ], string='Close Category', default='month_end', required=True, tracking=True)

    date_start = fields.Date(
        string='Period Start Date',
        required=True,
        tracking=True,
    )
    date_end = fields.Date(
        string='Period Cutoff Date',
        required=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        required=True,
    )

    # -------------------------------------------------------------------------
    # 10-Point Mandatory Close Checklist Gates
    # -------------------------------------------------------------------------
    gate_bank_recs_approved = fields.Boolean(
        string='Gate 1: All Bank Accounts Reconciled & Certified (Maker-Checker)',
        default=False,
        tracking=True,
    )
    gate_ap_bills_accruals_cleared = fields.Boolean(
        string='Gate 2: AP 3-Way Match Exceptions Cleared & Unbilled Accruals Posted',
        default=False,
        tracking=True,
    )
    gate_hrms_payroll_eosb_verified = fields.Boolean(
        string='Gate 3: HRMS Payroll Runs Released & Monthly EOSB Provision Synced',
        default=False,
        tracking=True,
    )
    gate_inventory_counts_reconciled = fields.Boolean(
        string='Gate 4: Site Physical Inventory & Leftover Surplus Reconciled',
        default=False,
        tracking=True,
    )
    gate_nca_depreciation_posted = fields.Boolean(
        string='Gate 5: Non-Current Asset (NCA) Depreciation Posted to Projects',
        default=False,
        tracking=True,
    )
    gate_provisions_reviewed = fields.Boolean(
        string='Gate 6: Bad Debts (ECL), Stock & Warranty Provisions Reviewed',
        default=False,
        tracking=True,
    )
    gate_poc_revenue_wip_posted = fields.Boolean(
        string='Gate 7: PoC Revenue & Over/Under-Billing (WIP) Posted to GL',
        default=False,
        tracking=True,
    )
    gate_retention_register_reviewed = fields.Boolean(
        string='Gate 8: Client & Subcontractor Retention DLP Register Audited',
        default=False,
        tracking=True,
    )
    gate_rpt_partner_accounts_reconciled = fields.Boolean(
        string='Gate 9: Related Party (RPT) & Partner Current Accounts Approved',
        default=False,
        tracking=True,
    )
    gate_project_profitability_generated = fields.Boolean(
        string='Gate 10: Budget-vs-Actual Project Profitability Generated',
        default=False,
        tracking=True,
    )

    # Year-End Extension Gates
    gate_ye_physical_asset_count_100pct = fields.Boolean(
        string='YE Gate 11: 100% Fixed Asset Physical Count Completed',
        default=False,
        tracking=True,
    )
    gate_ye_retention_current_noncurrent_split = fields.Boolean(
        string='YE Gate 12: IFRS Current vs Non-Current Retention Split Validated',
        default=False,
        tracking=True,
    )
    gate_ye_lender_confirmations_received = fields.Boolean(
        string='YE Gate 13: Direct Bank, Guarantee & Lender Confirmations Received',
        default=False,
        tracking=True,
    )
    gate_ye_ifrs_draft_statements_ready = fields.Boolean(
        string='YE Gate 14: Draft IFRS Financial Statements & Notes Prepared',
        default=False,
        tracking=True,
    )

    completion_percentage = fields.Float(
        string='Checklist Completion (%)',
        compute='_compute_completion_percentage',
        store=True,
    )
    all_gates_cleared = fields.Boolean(
        string='All Mandatory Gates Completed',
        compute='_compute_completion_percentage',
        store=True,
    )

    # Exception & Carry Forward
    carry_forward_reason = fields.Text(
        string='Management Carry-Forward & Exception Justification',
        help='Mandatory justification if locking the period with any pending checklist items.',
        tracking=True,
    )
    carry_forward_authorized_by_id = fields.Many2one(
        'res.users',
        string='Exception Authorized By',
        readonly=True,
        copy=False,
    )

    state = fields.Selection([
        ('draft', 'In Progress / Open'),
        ('in_review', 'Under Management Review'),
        ('locked', 'Period Formally Locked'),
        ('reopened', 'Reopened by Management'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    locked_by_id = fields.Many2one(
        'res.users',
        string='Locked By (Manager)',
        readonly=True,
        copy=False,
    )
    lock_date = fields.Datetime(
        string='Lock Timestamp',
        readonly=True,
        copy=False,
    )
    reopened_by_id = fields.Many2one(
        'res.users',
        string='Reopened By',
        readonly=True,
        copy=False,
    )
    reopen_date = fields.Datetime(
        string='Reopen Timestamp',
        readonly=True,
        copy=False,
    )
    reopen_reason = fields.Text(
        string='Documented Reopen Justification',
        copy=False,
    )

    audit_query_ids = fields.One2many(
        'contractor.audit.query',
        'period_close_id',
        string='External Audit Queries',
    )
    year_end_manager_signed_off = fields.Boolean(
        string='Year-End Manager Sign-Off',
        default=False,
        tracking=True,
        copy=False,
    )
    year_end_signoff_by_id = fields.Many2one(
        'res.users',
        string='Year-End Sign-Off By',
        readonly=True,
        copy=False,
    )
    year_end_signoff_date = fields.Datetime(
        string='Year-End Sign-Off Timestamp',
        readonly=True,
        copy=False,
    )
    auditor_access_approved = fields.Boolean(
        string='External Auditor Access Approved',
        default=False,
        tracking=True,
        copy=False,
    )
    auditor_access_approved_by_id = fields.Many2one(
        'res.users',
        string='Auditor Access Approved By',
        readonly=True,
        copy=False,
    )
    auditor_access_approved_date = fields.Datetime(
        string='Auditor Access Approved Timestamp',
        readonly=True,
        copy=False,
    )

    @api.depends(
        'period_type',
        'gate_bank_recs_approved',
        'gate_ap_bills_accruals_cleared',
        'gate_hrms_payroll_eosb_verified',
        'gate_inventory_counts_reconciled',
        'gate_nca_depreciation_posted',
        'gate_provisions_reviewed',
        'gate_poc_revenue_wip_posted',
        'gate_retention_register_reviewed',
        'gate_rpt_partner_accounts_reconciled',
        'gate_project_profitability_generated',
        'gate_ye_physical_asset_count_100pct',
        'gate_ye_retention_current_noncurrent_split',
        'gate_ye_lender_confirmations_received',
        'gate_ye_ifrs_draft_statements_ready',
    )
    def _compute_completion_percentage(self):
        for rec in self:
            mandatory_gates = [
                rec.gate_bank_recs_approved,
                rec.gate_ap_bills_accruals_cleared,
                rec.gate_hrms_payroll_eosb_verified,
                rec.gate_inventory_counts_reconciled,
                rec.gate_nca_depreciation_posted,
                rec.gate_provisions_reviewed,
                rec.gate_poc_revenue_wip_posted,
                rec.gate_retention_register_reviewed,
                rec.gate_rpt_partner_accounts_reconciled,
                rec.gate_project_profitability_generated,
            ]
            if rec.period_type == 'year_end':
                mandatory_gates.extend([
                    rec.gate_ye_physical_asset_count_100pct,
                    rec.gate_ye_retention_current_noncurrent_split,
                    rec.gate_ye_lender_confirmations_received,
                    rec.gate_ye_ifrs_draft_statements_ready,
                ])

            tot = len(mandatory_gates)
            cleared = sum(1 for g in mandatory_gates if g)
            pct = (cleared / tot * 100.0) if tot > 0 else 0.0
            rec.completion_percentage = round(pct, 1)
            rec.all_gates_cleared = (cleared == tot)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Period Close')) == _('New Period Close'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.period.close') or _('New Period Close')
        return super().create(vals_list)

    def action_run_automated_checks(self):
        """Scans database records across all 10 mandatory gates and updates verification status."""
        self.ensure_one()

        # Check 1: Bank Reconciliations (no unapproved recs, no unmatched lines, and active bank journals covered)
        period_recs = self.env['contractor.bank.reconciliation'].search([
            ('company_id', '=', self.company_id.id),
            ('statement_date', '>=', self.date_start),
            ('statement_date', '<=', self.date_end),
        ])
        unbalanced_recs = period_recs.filtered(lambda r: r.state != 'approved')
        unmatched_lines = self.env['contractor.bank.reconciliation.line'].search_count([
            ('reconciliation_id', 'in', period_recs.ids),
            ('is_matched', '=', False),
        ]) if period_recs else 0
        bank_activity = self.env['account.move.line'].search_count([
            ('company_id', '=', self.company_id.id),
            ('journal_id.type', '=', 'bank'),
            ('parent_state', '=', 'posted'),
            ('date', '>=', self.date_start),
            ('date', '<=', self.date_end),
        ])
        self.gate_bank_recs_approved = (
            len(unbalanced_recs) == 0
            and unmatched_lines == 0
            and (not bank_activity or bool(period_recs.filtered(lambda r: r.state == 'approved')))
        )

        # Check 2: AP 3-Way Match & Unallocated Customer Receipts
        open_match_exceptions = self.env['account.move'].search_count([
            ('company_id', '=', self.company_id.id),
            ('move_type', '=', 'in_invoice'),
            ('invoice_date', '<=', self.date_end),
            ('is_match_hold', '=', True),
        ])
        unallocated_ar_receipts = self.env['account.payment'].search_count([
            ('company_id', '=', self.company_id.id),
            ('payment_type', '=', 'inbound'),
            ('state', 'in', ['in_process', 'paid']),
            ('is_reconciled', '=', False),
            ('date', '<=', self.date_end),
        ])
        self.gate_ap_bills_accruals_cleared = (open_match_exceptions == 0 and unallocated_ar_receipts == 0)

        # Check 3: HRMS Payroll Runs
        open_payrolls = self.env['contractor.payroll.run'].search_count([
            ('company_id', '=', self.company_id.id),
            ('date_end', '>=', self.date_start),
            ('date_end', '<=', self.date_end),
            ('state', '!=', 'released'),
        ])
        self.gate_hrms_payroll_eosb_verified = open_payrolls == 0

        # Check 4: Inventory Stock Adjustments & Leftover Dispositions
        open_san = self.env['contractor.stock.adjustment.note'].search_count([
            ('company_id', '=', self.company_id.id),
            ('state', 'not in', ['adjusted', 'declined']),
        ])
        open_lod = self.env['contractor.leftover.disposition'].search_count([
            ('company_id', '=', self.company_id.id),
            ('state', 'not in', ['executed', 'declined']),
        ])
        self.gate_inventory_counts_reconciled = (open_san == 0 and open_lod == 0)

        # Check 5: Asset Depreciation
        due_asset_lines = self.env['contractor.fixed.asset.depreciation.line'].search_count([
            ('asset_id.company_id', '=', self.company_id.id),
            ('schedule_date', '<=', self.date_end),
            ('is_posted', '=', False),
        ])
        self.gate_nca_depreciation_posted = due_asset_lines == 0

        # Check 6: Accounting Provisions Reviewed & Posted
        open_provisions = self.env['contractor.accounting.provision'].search_count([
            ('company_id', '=', self.company_id.id),
            ('provision_date', '>=', self.date_start),
            ('provision_date', '<=', self.date_end),
            ('state', 'not in', ['posted', 'reversed']),
        ])
        self.gate_provisions_reviewed = open_provisions == 0

        # Check 7: PoC WIP Schedules
        active_projects = self.env['project.project'].search([('company_id', '=', self.company_id.id)])
        wip_schedules = self.env['contractor.project.wip.schedule'].search_count([
            ('company_id', '=', self.company_id.id),
            ('period_date', '>=', self.date_start),
            ('period_date', '<=', self.date_end),
            ('state', '=', 'posted'),
        ])
        self.gate_poc_revenue_wip_posted = wip_schedules >= len(active_projects) if active_projects else True

        # Check 8: Retention Register DLP Expiry Review
        expired_unreviewed_retentions = self.env['contractor.retention.register'].search_count([
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'dlp_expired_flagged'),
            ('dlp_expiry_date', '<=', self.date_end),
        ])
        self.gate_retention_register_reviewed = expired_unreviewed_retentions == 0

        # Check 9: RPT Approvals & Month-End Partner Current Account Reconciliations
        unapproved_rpt_moves = self.env['account.move'].search_count([
            ('company_id', '=', self.company_id.id),
            ('date', '<=', self.date_end),
            ('is_rpt_transaction', '=', True),
            ('rpt_manager_approved', '=', False),
        ])
        unreconciled_partner_accounts = self.env['contractor.partner.current.account'].search_count([
            ('company_id', '=', self.company_id.id),
            '|',
            ('last_reconciled_date', '=', False),
            ('last_reconciled_date', '<', self.date_start),
        ])
        self.gate_rpt_partner_accounts_reconciled = (unapproved_rpt_moves == 0 and unreconciled_partner_accounts == 0)

        # Check 10: Open Audit Queries & Pending Operational Approvals
        open_queries = len(self.audit_query_ids.filtered(lambda q: q.status != 'closed'))
        pending_mrs = self.env['contractor.material.requisition'].search_count([
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'submitted'),
        ]) if 'contractor.material.requisition' in self.env else 0
        pending_cs = self.env['contractor.comparative.statement'].search_count([
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'submitted'),
        ]) if 'contractor.comparative.statement' in self.env else 0
        pending_vos = self.env['contractor.variation.order'].search_count([
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'submitted'),
        ]) if 'contractor.variation.order' in self.env else 0
        pending_quotes = self.env['sale.order'].search_count([
            ('company_id', '=', self.company_id.id),
            ('contractor_quote_state', '=', 'submitted'),
        ])
        self.gate_project_profitability_generated = (
            open_queries == 0 and pending_mrs == 0 and pending_cs == 0 and pending_vos == 0 and pending_quotes == 0
        )

        self._compute_completion_percentage()
        self.message_post(
            body=Markup(_("<strong>Automated Close Scan Executed:</strong> Checklist status updated (Current score: %.1f%%).")) % self.completion_percentage,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_signoff_year_end(self):
        """Records formal Manager statutory Year-End close sign-off prior to locking."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise UserError(_("Access Denied: Only a Contractor Manager can grant Year-End Sign-Off."))
        if self.period_type != 'year_end':
            raise UserError(_("Year-End Sign-Off applies only to Year-End Closing schedules."))
        self.write({
            'year_end_manager_signed_off': True,
            'year_end_signoff_by_id': self.env.user.id,
            'year_end_signoff_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Year-End Statutory Sign-Off Recorded:</strong> Authorized by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_approve_auditor_access(self):
        """Records management authorization for external auditor read access to closing pack."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager') or self.env.user.has_group('contractor_core.group_contractor_md')):
            raise UserError(_("Access Denied: Only Management or MD can authorize external auditor access."))
        self.write({
            'auditor_access_approved': True,
            'auditor_access_approved_by_id': self.env.user.id,
            'auditor_access_approved_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>External Auditor Access Approved:</strong> Granted by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_submit_for_review(self):
        self.ensure_one()
        self.state = 'in_review'

    def action_lock_period(self):
        """Hard-Stop: Block period locking if any gate is incomplete without documented carry-forward."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can lock accounting periods."))

        self._compute_completion_percentage()

        if self.period_type == 'year_end' and not self.year_end_manager_signed_off:
            raise UserError(_(
                "Year-End Statutory Sign-Off Required:\n\n"
                "Please record formal Manager Year-End Sign-Off ('Year-End Manager Sign-Off' button) before locking the Year-End schedule."
            ))

        if not self.all_gates_cleared:
            clean_cf = (self.carry_forward_reason or '').strip()
            if len(clean_cf) < 15:
                raise UserError(_(
                    "Period Close Governance Hard-Stop:\n\n"
                    "Cannot lock accounting period '%s'.\n"
                    "Checklist is currently %.1f%% complete (Gates are outstanding).\n\n"
                    "To lock with pending gates, a documented 'Management Carry-Forward & Exception Justification' (minimum 15 characters) is strictly mandatory."
                ) % (self.name, self.completion_percentage))
            self.carry_forward_authorized_by_id = self.env.user.id

        self.write({
            'state': 'locked',
            'locked_by_id': self.env.user.id,
            'lock_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Accounting Period Formally Locked:</strong> Authorized by Contractor Manager %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_reopen_period(self):
        """Allows Manager to reopen locked period with mandatory justification."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can reopen a locked accounting period."))

        return {
            'name': _('Reopen Accounting Period'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': 'reopen',
                'default_callback_method': '_callback_reopen_period',
                'active_model': self._name,
                'active_id': self.id,
                'callback_method': '_callback_reopen_period',
            },
        }

    def _callback_reopen_period(self, reason=None):
        clean_reason = (reason or getattr(self, 'last_action_reason', '')).strip()
        if len(clean_reason) < 15:
            raise UserError(_("Documented management justification (minimum 15 characters) is required to reopen a closed period."))

        self.write({
            'state': 'reopened',
            'reopened_by_id': self.env.user.id,
            'reopen_date': fields.Datetime.now(),
            'reopen_reason': clean_reason,
        })
        self.message_post(
            body=Markup(_("<strong>Accounting Period Reopened:</strong> Authorized by %s.<br/>Reason: %s")) % (
                self.env.user.name, clean_reason
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    @api.model
    def _check_period_not_locked(self, target_date, company_id):
        """Hard-Stop: Blocks posting or backdating accounting entries into any locked period."""
        if not target_date or not company_id:
            return True
        locked_period = self.sudo().search([
            ('company_id', '=', company_id),
            ('state', '=', 'locked'),
            ('date_start', '<=', target_date),
            ('date_end', '>=', target_date),
        ], limit=1)
        if locked_period:
            raise ValidationError(_(
                "Locked Accounting Period Hard-Stop:\n\n"
                "Posting date %s falls within locked accounting period '%s' (%s to %s).\n"
                "Financial postings into closed periods are strictly prohibited. "
                "Reopen the period with documented Manager authorization first."
            ) % (target_date, locked_period.name, locked_period.date_start, locked_period.date_end))
        return True

    def write(self, vals):
        allowed_meta_fields = {
            'last_action_reason', 'last_action_by_id', 'last_action_date',
            'message_follower_ids', 'message_ids', 'activity_ids', 'message_main_attachment_id',
        }
        allowed_reopen_fields = {'state', 'reopened_by_id', 'reopen_date', 'reopen_reason'}
        locked_recs = self.filtered(lambda r: r.state == 'locked')
        if locked_recs and not set(vals.keys()).issubset(allowed_meta_fields):
            is_valid_reopen = (
                'state' in vals
                and vals['state'] != 'locked'
                and set(vals.keys()).issubset(allowed_reopen_fields | allowed_meta_fields)
                and (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager'))
            )
            if not is_valid_reopen:
                raise UserError(_(
                    "Locked Accounting Period Hard-Stop:\n\n"
                    "Modifications to locked accounting period records are strictly prohibited. "
                    "Use 'Reopen Accounting Period' with Manager justification first."
                ))
        return super().write(vals)

    def unlink(self):
        if any(rec.state == 'locked' for rec in self):
            raise UserError(_("Locked accounting periods cannot be deleted."))
        return super().unlink()


class AccountMovePeriodLock(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        if 'contractor.period.close' in self.env and not self.env.context.get('install_mode'):
            period_model = self.env['contractor.period.close']
            for move in self:
                if move.date and move.company_id:
                    period_model._check_period_not_locked(move.date, move.company_id.id)
        return super()._post(soft=soft)


# =========================================================================
# External Audit Query & Adjustment Tracker
# =========================================================================
class ContractorAuditQuery(models.Model):
    _name = 'contractor.audit.query'
    _description = 'Contractor External Audit Query & Adjustment Tracker'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'id desc'

    name = fields.Char(
        string='Audit Query Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Audit Query'),
        index=True,
    )
    period_close_id = fields.Many2one(
        'contractor.period.close',
        string='Associated Period Close',
        required=True,
        tracking=True,
    )
    query_ref = fields.Char(
        string='Auditor PBC / Inquiry Code',
        required=True,
        tracking=True,
    )
    auditor_firm = fields.Char(
        string='External Audit Firm',
        required=True,
        default='Independent External Auditors',
        tracking=True,
    )
    query_text = fields.Text(
        string='Auditor Inquiry & Documentation Request',
        required=True,
    )
    management_response = fields.Text(
        string='Contractor Management Formal Response',
        tracking=True,
    )
    status = fields.Selection([
        ('open', 'Open Inquiry'),
        ('responded', 'Management Responded'),
        ('closed', 'Auditor Accepted & Closed'),
    ], string='Status', default='open', required=True, tracking=True, copy=False)

    has_audit_adjustment = fields.Boolean(
        string='Requires GL Audit Adjustment Entry',
        default=False,
        tracking=True,
    )
    adjustment_amount = fields.Monetary(
        string='Audit Adjustment Amount',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='period_close_id.company_id',
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    audit_adjustment_ref = fields.Char(
        string='Auditor Adjustment Schedule Code',
    )
    account_move_id = fields.Many2one(
        'account.move',
        string='Adjustment Journal Entry',
        readonly=True,
        copy=False,
    )
    md_approved = fields.Boolean(
        string='MD Authorized',
        default=False,
        tracking=True,
        copy=False,
    )
    md_approver_id = fields.Many2one(
        'res.users',
        string='MD Authorized By',
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Audit Query')) == _('New Audit Query'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.audit.query') or _('New Audit Query')
        return super().create(vals_list)

    def action_approve_md(self):
        """Hard-Stop: Material audit adjustments require Managing Director signoff."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Only the Managing Director can authorize material statutory audit adjustments."))

        self.write({
            'md_approved': True,
            'md_approver_id': self.env.user.id,
        })
        self.message_post(
            body=Markup(_("<strong>Audit Adjustment Authorized by MD:</strong> Certified by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

