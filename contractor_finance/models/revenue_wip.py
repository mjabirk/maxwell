# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorProjectWIPSchedule(models.Model):
    _name = 'contractor.project.wip.schedule'
    _description = 'Contractor Project PoC WIP & Over/Under-Billing Schedule'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'period_date desc, id desc'

    name = fields.Char(
        string='WIP Schedule Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New WIP Schedule'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project Site',
        required=True,
        tracking=True,
        index=True,
    )
    period_date = fields.Date(
        string='Month-End Period Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='project_id.company_id',
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )

    # Contract Values
    baseline_contract_value = fields.Monetary(
        string='Original Contract Value',
        currency_field='currency_id',
        compute='_compute_contract_values',
        store=True,
        readonly=False,
    )
    approved_variation_order_value = fields.Monetary(
        string='Approved Variations (VO)',
        currency_field='currency_id',
        compute='_compute_contract_values',
        store=True,
        readonly=False,
    )
    revised_contract_value = fields.Monetary(
        string='Total Revised Contract Value',
        currency_field='currency_id',
        compute='_compute_contract_values',
        store=True,
    )

    # Physical Certified Progress
    progress_calculation_basis = fields.Selection([
        ('poc_progress', 'Certified Physical Progress (%)'),
        ('certified_milestone', 'Consultant IPC Milestone Certificate'),
    ], string='Recognition Method', default='poc_progress', required=True, tracking=True)

    certified_progress_pct = fields.Float(
        string='Certified Cumulative Progress (%)',
        default=0.0,
        required=True,
        tracking=True,
    )
    milestone_certificate_ids = fields.Many2many(
        'ir.attachment',
        'contractor_wip_milestone_attachment_rel',
        'wip_id',
        'attachment_id',
        string='Client / Engineer Certified IPC Certificates',
    )

    # Earned Revenue vs Billed Analysis
    earned_revenue_to_date = fields.Monetary(
        string='Cumulative Earned Revenue',
        currency_field='currency_id',
        compute='_compute_revenue_wip',
        store=True,
        help='Calculated as: Revised Contract Value * Certified Physical Progress (%)',
    )
    billed_to_date = fields.Monetary(
        string='Cumulative Billed to Client',
        currency_field='currency_id',
        compute='_compute_revenue_wip',
        store=True,
        readonly=False,
        help='Total certified client billings posted up to period cutoff date.',
    )
    variance_amount = fields.Monetary(
        string='Revenue Variance (Earned - Billed)',
        currency_field='currency_id',
        compute='_compute_revenue_wip',
        store=True,
    )
    wip_classification = fields.Selection([
        ('under_billing', 'Under-Billing (Contract Asset / WIP)'),
        ('over_billing', 'Over-Billing (Contract Liability / Billings in Excess)'),
        ('balanced', 'Balanced (Earned = Billed)'),
    ], string='IFRS 15 WIP Classification', compute='_compute_revenue_wip', store=True)

    adjustment_amount = fields.Monetary(
        string='Required GL WIP Adjustment',
        currency_field='currency_id',
        compute='_compute_revenue_wip',
        store=True,
    )

    # Accounting Accounts
    journal_id = fields.Many2one(
        'account.journal',
        string='WIP Adjustment Journal',
        domain="[('type', '=', 'general')]",
    )
    contract_asset_account_id = fields.Many2one(
        'account.account',
        string='Contract Asset Account (Under-Billing / WIP)',
        domain="[('account_type', 'in', ['asset_current', 'asset_non_current'])]",
    )
    contract_liability_account_id = fields.Many2one(
        'account.account',
        string='Contract Liability Account (Over-Billing)',
        domain="[('account_type', 'in', ['liability_current', 'liability_non_current'])]",
    )
    revenue_adjustment_account_id = fields.Many2one(
        'account.account',
        string='Revenue Adjustment Account (P&L)',
        domain="[('internal_group', '=', 'income')]",
    )

    state = fields.Selection([
        ('draft', 'Draft Calculation'),
        ('submitted', 'Submitted by QS'),
        ('approved', 'Approved by Manager'),
        ('posted', 'Posted to General Ledger'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    account_move_id = fields.Many2one(
        'account.move',
        string='WIP Journal Entry',
        readonly=True,
        copy=False,
    )
    approved_by_id = fields.Many2one(
        'res.users',
        string='Approved By',
        readonly=True,
        copy=False,
    )
    approval_date = fields.Datetime(
        string='Approval Date',
        readonly=True,
        copy=False,
    )

    @api.depends('project_id', 'project_id.baseline_contract_value', 'project_id.total_approved_vo_value')
    def _compute_contract_values(self):
        for rec in self:
            if not rec.project_id:
                rec.baseline_contract_value = 0.0
                rec.approved_variation_order_value = 0.0
                rec.revised_contract_value = 0.0
                continue

            baseline = getattr(rec.project_id, 'baseline_contract_value', 0.0) or 0.0
            if not baseline:
                sale_orders = self.env['sale.order'].search([
                    ('project_id', '=', rec.project_id.id),
                    ('state', 'in', ['sale', 'done']),
                ])
                baseline = sum(sale_orders.mapped('amount_untaxed'))
            vos = getattr(rec.project_id, 'total_approved_vo_value', 0.0) or 0.0

            rec.baseline_contract_value = baseline
            rec.approved_variation_order_value = vos
            rec.revised_contract_value = baseline + vos

    @api.depends('revised_contract_value', 'certified_progress_pct', 'billed_to_date')
    def _compute_revenue_wip(self):
        for rec in self:
            earned = (rec.revised_contract_value or 0.0) * ((rec.certified_progress_pct or 0.0) / 100.0)
            rec.earned_revenue_to_date = earned

            diff = earned - (rec.billed_to_date or 0.0)
            rec.variance_amount = diff
            rec.adjustment_amount = abs(diff)

            if diff > 1.0:
                rec.wip_classification = 'under_billing'
            elif diff < -1.0:
                rec.wip_classification = 'over_billing'
            else:
                rec.wip_classification = 'balanced'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New WIP Schedule')) == _('New WIP Schedule'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.project.wip.schedule') or _('New WIP Schedule')
        return super().create(vals_list)

    def action_fetch_project_data(self):
        """Pulls cumulative billed amounts from posted invoices for this project."""
        self.ensure_one()
        self._compute_contract_values()

        invoices = self.env['account.move'].search([
            ('move_type', 'in', ['out_invoice', 'out_refund']),
            ('state', '=', 'posted'),
            ('invoice_date', '<=', self.period_date),
        ])
        # Filter for this project
        proj_inv = invoices.filtered(lambda m: getattr(m, 'project_id', False) and m.project_id.id == self.project_id.id)
        tot_billed = sum(proj_inv.mapped('amount_untaxed'))
        self.billed_to_date = tot_billed
        self._compute_revenue_wip()
        return True

    def action_submit(self):
        self.ensure_one()
        if self.certified_progress_pct < 0 or self.certified_progress_pct > 100:
            raise UserError(_("Certified physical progress percentage must be between 0.0% and 100.0%."))

        if self.progress_calculation_basis == 'certified_milestone' and not self.milestone_certificate_ids:
            raise UserError(_("Milestone Certificate Hard-Stop: An uploaded IPC or consultant certificate attachment is mandatory."))

        self.state = 'submitted'
        self.message_post(
            body=Markup(_("<strong>PoC WIP Schedule Submitted:</strong> Progress %s%%, Earned Revenue %s %.2f.")) % (
                self.certified_progress_pct, self.currency_id.symbol or '', self.earned_revenue_to_date
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_approve(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize WIP revenue schedules."))

        self.write({
            'state': 'approved',
            'approved_by_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>WIP Schedule Approved:</strong> Certified by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_post_wip_entry(self):
        """Generates the monthly Over/Under-Billing entry to General Ledger with project analytic distribution."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise AccessError(_("Access Denied: Only Accounting or Management users can post WIP GL adjustments."))
        if self.state != 'approved':
            raise UserError(_("WIP Schedule must be approved by management prior to posting."))
        if self.account_move_id:
            raise UserError(_("A WIP journal entry (%s) has already been posted for this schedule.") % self.account_move_id.name)
        if 'contractor.period.close' in self.env:
            self.env['contractor.period.close']._check_period_not_locked(self.period_date, self.company_id.id)

        if self.wip_classification == 'balanced' or self.adjustment_amount <= 0:
            self.state = 'posted'
            return

        journal = self.journal_id or self.env['account.journal'].search([('type', '=', 'general'), ('company_id', '=', self.company_id.id)], limit=1)
        if not journal:
            raise UserError(_("Please configure a General Journal for WIP adjustments."))

        rev_acc = self.revenue_adjustment_account_id or self.env['account.account'].search([('internal_group', '=', 'income'), ('company_ids', 'in', self.company_id.ids)], limit=1)
        asset_acc = self.contract_asset_account_id or self.env['account.account'].search([('account_type', 'in', ['asset_current', 'asset_non_current']), ('company_ids', 'in', self.company_id.ids)], limit=1)
        liab_acc = self.contract_liability_account_id or self.company_id.default_cost_accrual_account_id or self.env['account.account'].search([('account_type', 'in', ['liability_current', 'liability_non_current']), ('company_ids', 'in', self.company_id.ids)], limit=1)

        analytic_dist = {}
        if hasattr(self.project_id, 'account_id') and self.project_id.account_id:
            analytic_dist = {str(self.project_id.account_id.id): 100.0}

        move_lines = []
        if self.wip_classification == 'under_billing':
            # Under-Billing: Earned > Billed -> Debit Contract Asset (WIP), Credit Revenue Adjustment
            move_lines.append((0, 0, {
                'name': f"Under-Billing (Contract Asset / WIP): {self.project_id.name}",
                'account_id': asset_acc.id,
                'project_id': self.project_id.id,
                'debit': self.adjustment_amount,
                'credit': 0.0,
            }))
            move_lines.append((0, 0, {
                'name': f"Unbilled Earned Revenue PoC: {self.project_id.name}",
                'account_id': rev_acc.id,
                'project_id': self.project_id.id,
                'debit': 0.0,
                'credit': self.adjustment_amount,
                'analytic_distribution': analytic_dist or False,
            }))
        else:
            # Over-Billing: Billed > Earned -> Debit Revenue Adjustment, Credit Contract Liability (Billings in Excess)
            move_lines.append((0, 0, {
                'name': f"Over-Billing Revenue Deferral: {self.project_id.name}",
                'account_id': rev_acc.id,
                'project_id': self.project_id.id,
                'debit': self.adjustment_amount,
                'credit': 0.0,
                'analytic_distribution': analytic_dist or False,
            }))
            move_lines.append((0, 0, {
                'name': f"Billings in Excess of Costs (Contract Liability): {self.project_id.name}",
                'account_id': liab_acc.id,
                'project_id': self.project_id.id,
                'debit': 0.0,
                'credit': self.adjustment_amount,
            }))

        move = self.env['account.move'].create({
            'journal_id': journal.id,
            'date': self.period_date,
            'ref': f"PoC WIP {self.name} - {self.project_id.name}",
            'move_type': 'entry',
            'project_id': self.project_id.id,
            'line_ids': move_lines,
        })
        move.action_post()

        self.write({
            'state': 'posted',
            'account_move_id': move.id,
        })
        self.message_post(
            body=Markup(_("<strong>WIP Journal Entry Posted:</strong> Move %s created for %s %s %.2f.")) % (
                move.name, self.wip_classification, self.currency_id.symbol or '', self.adjustment_amount
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    @api.constrains('project_id', 'period_date')
    def _check_unique_wip_schedule(self):
        for rec in self:
            if not rec.project_id or not rec.period_date:
                continue
            dup = self.sudo().search([
                ('id', '!=', rec.id),
                ('project_id', '=', rec.project_id.id),
                ('period_date', '=', rec.period_date),
            ], limit=1)
            if dup:
                raise ValidationError(_(
                    "A PoC WIP Schedule ('%s') already exists for project '%s' on period date %s."
                ) % (dup.name, rec.project_id.display_name, rec.period_date))

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        if any(rec.state == 'posted' for rec in self) and not set(vals.keys()).issubset(allowed_meta_fields):
            raise UserError(_("Posted WIP Schedules are locked and cannot be modified."))
        return super().write(vals)

