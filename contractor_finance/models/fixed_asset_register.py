# -*- coding: utf-8 -*-
from datetime import timedelta
from dateutil.relativedelta import relativedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorFixedAsset(models.Model):
    _name = 'contractor.fixed.asset'
    _description = 'Contractor Non-Current Fixed Asset (NCA)'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin', 'contractor.reason.mixin']
    _order = 'acquisition_date desc, id desc'

    name = fields.Char(
        string='Asset Description / Equipment Name',
        required=True,
        tracking=True,
        index=True,
    )
    asset_code = fields.Char(
        string='Asset Code / Tag',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Asset'),
        index=True,
    )
    acquisition_date = fields.Date(
        string='Acquisition Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    purchase_value = fields.Monetary(
        string='Gross Acquisition Value',
        currency_field='currency_id',
        required=True,
        tracking=True,
    )
    salvage_value = fields.Monetary(
        string='Salvage / Residual Value',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    useful_life_months = fields.Integer(
        string='Useful Life (Months)',
        default=60,
        required=True,
        tracking=True,
    )
    depreciation_method = fields.Selection([
        ('straight_line', 'Straight-Line Method'),
        ('declining', 'Declining Balance Method'),
    ], string='Depreciation Method', default='straight_line', required=True, tracking=True)

    declining_rate_pct = fields.Float(
        string='Annual Declining Rate (%)',
        default=20.0,
        tracking=True,
    )

    # Site, Custodian & Handover Linkage
    project_id = fields.Many2one(
        'project.project',
        string='Deployed Project Site',
        tracking=True,
        index=True,
    )
    location_id = fields.Many2one(
        'stock.location',
        string='Current Stock / Yard Location',
        tracking=True,
    )
    custodian_employee_id = fields.Many2one(
        'hr.employee',
        string='Asset Custodian Operative',
        tracking=True,
    )
    tool_asset_id = fields.Many2one(
        'contractor.tool.asset',
        string='Linked Tool Custody Record',
        help='Allows capitalizing and tracking high-value personal tools or heavy plant below standard capitalization threshold.',
        tracking=True,
    )
    is_personal_tool = fields.Boolean(
        string='Personal / Portable Site Tool',
        default=False,
        tracking=True,
        help='Exempts personal tools from standard NCA capitalization threshold while retaining fixed asset register tracking.',
    )
    last_physical_verification_date = fields.Date(
        string='Last Physical Verification Date',
        tracking=True,
        copy=False,
    )
    last_verified_by_id = fields.Many2one(
        'res.users',
        string='Last Verified By',
        tracking=True,
        copy=False,
    )

    # Procurement & AP Linkage
    po_id = fields.Many2one(
        'purchase.order',
        string='Source Purchase Order',
        tracking=True,
    )
    grn_picking_id = fields.Many2one(
        'stock.picking',
        string='Source GRN Receipt',
        tracking=True,
    )
    vendor_bill_id = fields.Many2one(
        'account.move',
        string='Source Vendor Bill',
        domain="[('move_type', '=', 'in_invoice')]",
        tracking=True,
    )

    # Accounting & General Ledger Setup
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
        string='Depreciation Journal',
        domain="[('company_id', '=', company_id)]",
    )
    asset_account_id = fields.Many2one(
        'account.account',
        string='Fixed Asset Balance Sheet Account',
        domain="[('company_ids', 'in', company_id)]",
    )
    depreciation_expense_account_id = fields.Many2one(
        'account.account',
        string='Depreciation Expense P&L Account',
        domain="[('company_ids', 'in', company_id)]",
    )
    accumulated_depreciation_account_id = fields.Many2one(
        'account.account',
        string='Accumulated Depreciation Contra Account',
        domain="[('company_ids', 'in', company_id)]",
    )

    state = fields.Selection([
        ('draft', 'Draft Setup'),
        ('running', 'Active & Depreciating'),
        ('fully_depreciated', 'Fully Depreciated'),
        ('disposed', 'Disposed / Scrapped / Sold'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    # Depreciation Lines & Net Book Value
    depreciation_line_ids = fields.One2many(
        'contractor.fixed.asset.depreciation.line',
        'asset_id',
        string='Depreciation Schedule Lines',
        copy=False,
    )
    cumulative_depreciation = fields.Monetary(
        string='Accumulated Depreciation',
        currency_field='currency_id',
        compute='_compute_depreciation_summary',
        store=True,
    )
    book_value = fields.Monetary(
        string='Net Book Value (NBV)',
        currency_field='currency_id',
        compute='_compute_depreciation_summary',
        store=True,
    )

    # Asset Disposal & Impairment
    disposal_date = fields.Date(
        string='Disposal Date',
        tracking=True,
        copy=False,
    )
    disposal_proceeds = fields.Monetary(
        string='Net Disposal Proceeds',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
        copy=False,
    )
    disposal_gain_loss = fields.Monetary(
        string='Gain / (Loss) on Disposal',
        currency_field='currency_id',
        compute='_compute_disposal_gain_loss',
        store=True,
    )
    disposal_reason = fields.Text(
        string='Documented Disposal / Write-Off Rationale',
        copy=False,
    )
    disposal_approver_id = fields.Many2one(
        'res.users',
        string='Disposal Authorized By',
        readonly=True,
        copy=False,
    )
    disposal_move_id = fields.Many2one(
        'account.move',
        string='Disposal Accounting Entry',
        readonly=True,
        copy=False,
    )

    @api.depends('purchase_value', 'depreciation_line_ids.is_posted', 'depreciation_line_ids.depreciation_amount')
    def _compute_depreciation_summary(self):
        for asset in self:
            posted_lines = asset.depreciation_line_ids.filtered(lambda l: l.is_posted)
            tot_dep = sum(posted_lines.mapped('depreciation_amount'))
            asset.cumulative_depreciation = tot_dep
            asset.book_value = max((asset.purchase_value or 0.0) - tot_dep, 0.0)

    @api.depends('book_value', 'disposal_proceeds', 'state')
    def _compute_disposal_gain_loss(self):
        for asset in self:
            if asset.state == 'disposed':
                asset.disposal_gain_loss = (asset.disposal_proceeds or 0.0) - asset.book_value
            else:
                asset.disposal_gain_loss = 0.0

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('asset_code', _('New Asset')) == _('New Asset'):
                vals['asset_code'] = self.env['ir.sequence'].next_by_code('contractor.fixed.asset') or _('New Asset')
        return super().create(vals_list)

    def action_generate_depreciation_schedule(self):
        """Generates periodic monthly depreciation schedule based on selected method."""
        self.ensure_one()
        if self.useful_life_months <= 0:
            raise UserError(_("Useful life in months must be strictly greater than zero."))
        if self.purchase_value <= self.salvage_value:
            raise UserError(_("Purchase value must exceed salvage value to compute depreciation."))

        self.depreciation_line_ids.unlink()

        depreciable_base = self.purchase_value - self.salvage_value
        start_date = self.acquisition_date or fields.Date.context_today(self)
        lines = []

        if self.depreciation_method == 'straight_line':
            monthly_amt = depreciable_base / self.useful_life_months
            running_cum = 0.0
            for i in range(1, self.useful_life_months + 1):
                sch_date = start_date + relativedelta(months=i)
                amt = monthly_amt
                if i == self.useful_life_months:
                    # Rounding balance correction on last month
                    amt = depreciable_base - running_cum
                running_cum += amt
                lines.append((0, 0, {
                    'schedule_date': sch_date,
                    'depreciation_amount': amt,
                    'cumulative_depreciation': running_cum,
                    'book_value': max(self.purchase_value - running_cum, self.salvage_value),
                }))
        else:
            # Declining balance method
            monthly_rate = (self.declining_rate_pct / 100.0) / 12.0
            current_nbv = self.purchase_value
            running_cum = 0.0
            for i in range(1, self.useful_life_months + 1):
                sch_date = start_date + relativedelta(months=i)
                amt = current_nbv * monthly_rate
                if current_nbv - amt < self.salvage_value:
                    amt = max(current_nbv - self.salvage_value, 0.0)
                current_nbv -= amt
                running_cum += amt
                lines.append((0, 0, {
                    'schedule_date': sch_date,
                    'depreciation_amount': amt,
                    'cumulative_depreciation': running_cum,
                    'book_value': current_nbv,
                }))
                if current_nbv <= self.salvage_value:
                    break

        self.write({'depreciation_line_ids': lines})
        return True

    def action_confirm(self):
        """Validates GL configuration, purchase/handover documentation, capitalization threshold, and activates asset running schedule."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise AccessError(_("Access Denied: Only Accounting or Management users can confirm fixed assets."))

        if not self.env.context.get('skip_purchase_doc_check'):
            if not (self.po_id or self.grn_picking_id or self.vendor_bill_id or self.tool_asset_id or self.is_personal_tool):
                raise UserError(_(
                    "Fixed Asset Registration Blocked:\n\n"
                    "Asset '%s' cannot be confirmed without supporting purchase/handover documentation "
                    "(Purchase Order, GRN Receipt, Vendor Bill, or Linked Tool Custody Record)."
                ) % self.name)

        if not self.is_personal_tool and not self.tool_asset_id:
            threshold = getattr(self.company_id, 'nca_capitalization_threshold', 0.0) or 5000.0
            if self.purchase_value < threshold:
                raise UserError(_(
                    "Below NCA Capitalization Threshold:\n\n"
                    "Asset '%s' gross value (%s %.2f) is below the company capitalization threshold (%s %.2f). "
                    "Expense directly or mark as Personal / Portable Site Tool if applicable."
                ) % (self.name, self.currency_id.symbol or '', self.purchase_value, self.currency_id.symbol or '', threshold))

        if not self.depreciation_line_ids:
            self.action_generate_depreciation_schedule()

        self._ensure_accounts()
        self.state = 'running'
        self.message_post(
            body=Markup(_("<strong>Asset Activated:</strong> Fixed asset running schedule confirmed. Total depreciable base: %s %.2f.")) % (
                self.currency_id.symbol or '', self.purchase_value - self.salvage_value
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def _ensure_accounts(self):
        self.ensure_one()
        if not self.journal_id:
            journal = self.env['account.journal'].search([('type', '=', 'general'), ('company_id', '=', self.company_id.id)], limit=1)
            if not journal:
                raise UserError(_("Please configure a General Journal for fixed asset depreciation."))
            self.journal_id = journal

        if not self.depreciation_expense_account_id:
            exp_acc = self.company_id.default_expense_account_id or self.env['account.account'].search([('internal_group', '=', 'expense'), ('company_ids', 'in', self.company_id.ids)], limit=1)
            if not exp_acc:
                raise UserError(_("Please configure a Depreciation Expense Account."))
            self.depreciation_expense_account_id = exp_acc

        if not self.accumulated_depreciation_account_id:
            contra_acc = self.env['account.account'].search([('account_type', 'in', ['asset_non_current', 'asset_fixed']), ('company_ids', 'in', self.company_id.ids)], limit=1) or self.asset_account_id
            if not contra_acc:
                raise UserError(_("Please configure an Accumulated Depreciation Contra-Asset Account."))
            self.accumulated_depreciation_account_id = contra_acc

    def action_post_monthly_depreciation(self):
        """Posts all unposted depreciation lines due up to today."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise AccessError(_("Access Denied: Only Accounting or Management users can post depreciation entries."))
        self._ensure_accounts()
        today = fields.Date.context_today(self)
        due_lines = self.depreciation_line_ids.filtered(lambda l: not l.is_posted and l.schedule_date <= today)

        if not due_lines:
            raise UserError(_("No depreciation schedule lines are currently due for posting."))

        for line in due_lines:
            line._post_depreciation_entry()

        if all(l.is_posted for l in self.depreciation_line_ids):
            self.state = 'fully_depreciated'

        self._compute_depreciation_summary()
        return True

    @api.model
    def _cron_post_monthly_depreciation(self):
        """Automated daily/monthly cron posting due asset depreciation entries across all running assets."""
        today = fields.Date.context_today(self)
        running_assets = self.search([('state', '=', 'running')])
        for asset in running_assets:
            due_lines = asset.depreciation_line_ids.filtered(lambda l: not l.is_posted and l.schedule_date <= today)
            for line in due_lines:
                try:
                    asset._ensure_accounts()
                    line._post_depreciation_entry()
                except Exception:
                    continue
            if all(l.is_posted for l in asset.depreciation_line_ids):
                asset.state = 'fully_depreciated'

    def action_dispose(self):
        """Workflow to dispose/retire asset with mandatory justification and manager/MD signoff."""
        self.ensure_one()
        threshold = self.company_id.po_md_approval_threshold or 50000.0
        requires_md = self.book_value >= threshold

        required_group = 'contractor_core.group_contractor_md' if requires_md else 'contractor_core.group_contractor_manager'
        if not self.env.user.has_group(required_group):
            role_title = _("Managing Director") if requires_md else _("Contractor Manager")
            raise UserError(_("Access Denied: Only a %s can authorize asset disposal for book value %s %.2f.") % (
                role_title, self.currency_id.symbol or '', self.book_value
            ))

        return {
            'name': _('Asset Disposal & Write-Off Authorization'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': 'dispose',
                'default_callback_method': '_callback_dispose_asset',
                'active_model': self._name,
                'active_id': self.id,
                'callback_method': '_callback_dispose_asset',
            },
        }

    def _callback_dispose_asset(self, reason=None):
        clean_reason = (reason or getattr(self, 'last_action_reason', '')).strip()
        if len(clean_reason) < 15:
            raise UserError(_("A comprehensive disposal/retirement justification (minimum 15 characters) is mandatory."))

        self._ensure_accounts()
        disp_date = fields.Date.context_today(self)

        # Generate GL Entry for Asset Disposal
        gain_loss_acc = self.env['account.account'].search([
            ('company_ids', 'in', self.company_id.id),
            ('internal_group', 'in', ['expense', 'income']),
        ], limit=1)

        move_lines = []
        # Remove Accumulated Depreciation (Debit)
        if self.cumulative_depreciation > 0:
            move_lines.append((0, 0, {
                'name': f"Disposal Acc. Depr: {self.asset_code} - {self.name}",
                'account_id': self.accumulated_depreciation_account_id.id,
                'debit': self.cumulative_depreciation,
                'credit': 0.0,
            }))
        # Record Cash Proceeds if any (Debit)
        if self.disposal_proceeds > 0:
            cash_acc = self.journal_id.default_account_id or self.depreciation_expense_account_id
            move_lines.append((0, 0, {
                'name': f"Disposal Proceeds: {self.asset_code}",
                'account_id': cash_acc.id,
                'debit': self.disposal_proceeds,
                'credit': 0.0,
            }))
        # Remove Original Asset Cost (Credit)
        move_lines.append((0, 0, {
            'name': f"Disposal Gross Cost: {self.asset_code} - {self.name}",
            'account_id': (self.asset_account_id or self.accumulated_depreciation_account_id).id,
            'debit': 0.0,
            'credit': self.purchase_value,
        }))

        # Balance difference to Gain/Loss on Disposal
        total_debit = sum(l[2].get('debit', 0.0) for l in move_lines)
        total_credit = sum(l[2].get('credit', 0.0) for l in move_lines)
        diff = total_credit - total_debit

        if abs(diff) > 0.001:
            if diff > 0:  # Loss on disposal (needs debit)
                move_lines.append((0, 0, {
                    'name': f"Loss on Asset Disposal: {self.asset_code}",
                    'account_id': gain_loss_acc.id,
                    'debit': diff,
                    'credit': 0.0,
                }))
            else:  # Gain on disposal (needs credit)
                move_lines.append((0, 0, {
                    'name': f"Gain on Asset Disposal: {self.asset_code}",
                    'account_id': gain_loss_acc.id,
                    'debit': 0.0,
                    'credit': abs(diff),
                }))

        move = self.env['account.move'].create({
            'journal_id': self.journal_id.id,
            'date': disp_date,
            'ref': f"Disposal {self.asset_code} - {self.name}",
            'move_type': 'entry',
            'line_ids': move_lines,
        })
        move.action_post()

        self.write({
            'state': 'disposed',
            'disposal_date': disp_date,
            'disposal_reason': clean_reason,
            'disposal_approver_id': self.env.user.id,
            'disposal_move_id': move.id,
        })
        self.message_post(
            body=Markup(_("<strong>Asset Disposed &amp; Written Off:</strong> Authorized by %s.<br/>Reason: %s<br/>Disposal Move: %s")) % (
                self.env.user.name, clean_reason, move.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )


class ContractorFixedAssetDepreciationLine(models.Model):
    _name = 'contractor.fixed.asset.depreciation.line'
    _description = 'Contractor Asset Depreciation Schedule Line'
    _order = 'schedule_date asc, id asc'

    asset_id = fields.Many2one(
        'contractor.fixed.asset',
        string='Fixed Asset',
        required=True,
        ondelete='cascade',
    )
    schedule_date = fields.Date(
        string='Depreciation Date',
        required=True,
    )
    depreciation_amount = fields.Monetary(
        string='Depreciation Amount',
        currency_field='currency_id',
        required=True,
    )
    cumulative_depreciation = fields.Monetary(
        string='Cumulative Depreciation',
        currency_field='currency_id',
        required=True,
    )
    book_value = fields.Monetary(
        string='Remaining Book Value',
        currency_field='currency_id',
        required=True,
    )
    is_posted = fields.Boolean(
        string='Posted to GL',
        default=False,
        copy=False,
    )
    move_id = fields.Many2one(
        'account.move',
        string='Depreciation Journal Entry',
        readonly=True,
        copy=False,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='asset_id.currency_id',
        readonly=True,
    )

    def _post_depreciation_entry(self):
        """Creates and posts the monthly depreciation GL entry attributing cost to project site."""
        self.ensure_one()
        if self.is_posted:
            return

        asset = self.asset_id
        analytic_dist = {}
        if asset.project_id and hasattr(asset.project_id, 'account_id') and asset.project_id.account_id:
            analytic_dist = {str(asset.project_id.account_id.id): 100.0}

        aml_fields = self.env['account.move.line']._fields
        am_fields = self.env['account.move']._fields
        dep_debit_line = {
            'name': f"Depreciation Expense: {asset.asset_code} - {asset.name}",
            'account_id': asset.depreciation_expense_account_id.id,
            'debit': self.depreciation_amount,
            'credit': 0.0,
            'analytic_distribution': analytic_dist or False,
        }
        if 'project_id' in aml_fields and asset.project_id:
            dep_debit_line['project_id'] = asset.project_id.id
        if 'cost_head' in aml_fields:
            dep_debit_line['cost_head'] = 'misc'

        move_vals = {
            'journal_id': asset.journal_id.id,
            'date': self.schedule_date,
            'ref': f"Depreciation {asset.asset_code} ({self.schedule_date})",
            'move_type': 'entry',
            'line_ids': [
                (0, 0, dep_debit_line),
                (0, 0, {
                    'name': f"Accumulated Depreciation: {asset.asset_code}",
                    'account_id': asset.accumulated_depreciation_account_id.id,
                    'debit': 0.0,
                    'credit': self.depreciation_amount,
                }),
            ],
        }
        if 'project_id' in am_fields and asset.project_id:
            move_vals['project_id'] = asset.project_id.id

        move = self.env['account.move'].create(move_vals)
        move.action_post()
        self.write({
            'is_posted': True,
            'move_id': move.id,
        })


# =========================================================================
# Physical Asset Verification & Discrepancy Adjustments
# =========================================================================
class ContractorAssetVerification(models.Model):
    _name = 'contractor.asset.verification'
    _description = 'Contractor Physical Asset Verification & Audit'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'verification_date desc, id desc'

    name = fields.Char(
        string='Verification Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Verification'),
        index=True,
    )
    verification_date = fields.Date(
        string='Audit / Count Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    verified_by_id = fields.Many2one(
        'res.users',
        string='Auditor / Engineer Conducting Count',
        default=lambda self: self.env.user,
        required=True,
        tracking=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Audited Project Site',
        tracking=True,
    )
    location_id = fields.Many2one(
        'stock.location',
        string='Audited Yard / Storage Location',
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

    line_ids = fields.One2many(
        'contractor.asset.verification.line',
        'verification_id',
        string='Asset Verification Lines',
        copy=True,
    )
    discrepancy_count = fields.Integer(
        string='Discrepancies Detected',
        compute='_compute_totals',
        store=True,
    )
    total_adjustment_amount = fields.Monetary(
        string='Net Impairment / Adjustment Amount',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )

    state = fields.Selection([
        ('draft', 'Draft Physical Count'),
        ('submitted', 'Submitted for Review'),
        ('approved', 'Approved by Management'),
        ('adjusted', 'Adjustments Posted to GL'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    approver_id = fields.Many2one(
        'res.users',
        string='Management Approver',
        readonly=True,
        copy=False,
    )
    approval_date = fields.Datetime(
        string='Approval Date',
        readonly=True,
        copy=False,
    )
    adjustment_move_id = fields.Many2one(
        'account.move',
        string='GL Impairment Entry',
        readonly=True,
        copy=False,
    )

    @api.depends('line_ids.discrepancy_type', 'line_ids.adjustment_amount')
    def _compute_totals(self):
        for rec in self:
            disc_lines = rec.line_ids.filtered(lambda l: l.discrepancy_type != 'none')
            rec.discrepancy_count = len(disc_lines)
            rec.total_adjustment_amount = sum(disc_lines.mapped('adjustment_amount'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Verification')) == _('New Verification'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.asset.verification') or _('New Verification')
        return super().create(vals_list)

    def action_load_assets(self):
        """Pulls active running assets deployed at the audited project site or yard location."""
        self.ensure_one()
        domain = [('state', '=', 'running'), ('company_id', '=', self.company_id.id)]
        if self.project_id:
            domain.append(('project_id', '=', self.project_id.id))
        elif self.location_id:
            domain.append(('location_id', '=', self.location_id.id))

        assets = self.env['contractor.fixed.asset'].search(domain)
        lines = []
        for asset in assets:
            lines.append((0, 0, {
                'asset_id': asset.id,
                'expected_condition': 'good',
                'actual_condition': 'good',
                'discrepancy_type': 'none',
                'adjustment_amount': 0.0,
            }))
        self.line_ids.unlink()
        self.write({'line_ids': lines})

    def action_submit(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Please populate verified assets before submitting."))
        self.state = 'submitted'
        self.message_post(
            body=Markup(_("<strong>Physical Verification Submitted:</strong> %d assets audited. %d discrepancies flagged.")) % (
                len(self.line_ids), self.discrepancy_count
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_approve(self):
        """Authorizes physical count discrepancies."""
        self.ensure_one()
        threshold = self.company_id.po_md_approval_threshold or 50000.0
        requires_md = self.total_adjustment_amount >= threshold

        required_group = 'contractor_core.group_contractor_md' if requires_md else 'contractor_core.group_contractor_manager'
        if not self.env.user.has_group(required_group):
            role_title = _("Managing Director") if requires_md else _("Contractor Manager")
            raise UserError(_("Access Denied: Only a %s can approve asset verification with adjustments totaling %s %.2f.") % (
                role_title, self.currency_id.symbol or '', self.total_adjustment_amount
            ))

        self.write({
            'state': 'approved',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })
        verified_assets = self.line_ids.mapped('asset_id')
        if verified_assets:
            verified_assets.write({
                'last_physical_verification_date': self.verification_date,
                'last_verified_by_id': self.verified_by_id.id or self.env.user.id,
            })
        self.message_post(
            body=Markup(_("<strong>Physical Verification Approved:</strong> Certified by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_post_adjustment(self):
        """Posts accounting adjustments for missing or impaired assets."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise AccessError(_("Access Denied: Only Accounting or Management users can post asset verification adjustments."))
        if self.state != 'approved':
            raise UserError(_("Verification must be approved by management prior to posting GL adjustments."))

        disc_lines = self.line_ids.filtered(lambda l: l.discrepancy_type != 'none' and l.adjustment_amount > 0)
        if not disc_lines:
            self.state = 'adjusted'
            return

        journal = self.env['account.journal'].search([('type', '=', 'general'), ('company_id', '=', self.company_id.id)], limit=1)
        expense_acc = self.company_id.default_expense_account_id or self.env['account.account'].search([('internal_group', '=', 'expense'), ('company_ids', 'in', self.company_id.ids)], limit=1)

        move_lines = []
        for line in disc_lines:
            asset = line.asset_id
            asset_acc = asset.asset_account_id or asset.accumulated_depreciation_account_id
            move_lines.append((0, 0, {
                'name': f"Asset Impairment ({line.discrepancy_type}): {asset.asset_code} - {asset.name}",
                'account_id': expense_acc.id,
                'debit': line.adjustment_amount,
                'credit': 0.0,
            }))
            move_lines.append((0, 0, {
                'name': f"Asset Value Adjustment: {asset.asset_code}",
                'account_id': asset_acc.id,
                'debit': 0.0,
                'credit': line.adjustment_amount,
            }))

            if line.discrepancy_type == 'missing_asset':
                asset.write({'state': 'disposed', 'disposal_reason': _("Written off following physical verification: %s") % (line.notes or '')})

        move = self.env['account.move'].create({
            'journal_id': journal.id if journal else False,
            'date': self.verification_date,
            'ref': f"Asset Audit {self.name}",
            'move_type': 'entry',
            'line_ids': move_lines,
        })
        move.action_post()

        self.write({
            'state': 'adjusted',
            'adjustment_move_id': move.id,
        })
        self.message_post(
            body=Markup(_("<strong>Impairment Adjustments Posted:</strong> Journal Entry %s created.")) % move.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        if any(rec.state == 'adjusted' for rec in self) and not set(vals.keys()).issubset(allowed_meta_fields):
            raise UserError(_("Adjusted Asset Verifications are locked and cannot be modified."))
        return super().write(vals)


class ContractorAssetVerificationLine(models.Model):
    _name = 'contractor.asset.verification.line'
    _description = 'Contractor Asset Verification Line'
    _order = 'asset_id asc, id asc'

    verification_id = fields.Many2one(
        'contractor.asset.verification',
        string='Verification Reference',
        required=True,
        ondelete='cascade',
    )
    asset_id = fields.Many2one(
        'contractor.fixed.asset',
        string='Asset Tag / Description',
        required=True,
    )
    book_value = fields.Monetary(
        string='Net Book Value',
        related='asset_id.book_value',
        readonly=True,
    )
    expected_condition = fields.Selection([
        ('good', 'Good / Operational'),
        ('damaged', 'Damaged / Needs Repair'),
        ('missing', 'Missing / Unlocated'),
    ], string='Expected Condition', default='good', required=True)

    actual_condition = fields.Selection([
        ('good', 'Good / Operational'),
        ('damaged', 'Damaged / Needs Repair'),
        ('missing', 'Missing / Unlocated'),
    ], string='Observed Condition', default='good', required=True)

    discrepancy_type = fields.Selection([
        ('none', 'No Discrepancy (Match)'),
        ('condition_mismatch', 'Physical Condition Impairment'),
        ('missing_asset', 'Missing / Unaccounted Asset'),
        ('unrecorded_asset', 'Surplus / Unrecorded Equipment'),
    ], string='Discrepancy Category', default='none', required=True)

    adjustment_amount = fields.Monetary(
        string='Required Impairment Amount',
        currency_field='currency_id',
        default=0.0,
    )
    notes = fields.Char(
        string='Auditor Observations & Tagging Notes',
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='verification_id.currency_id',
        readonly=True,
    )

    @api.onchange('actual_condition', 'expected_condition')
    def _onchange_condition(self):
        if self.actual_condition == 'missing':
            self.discrepancy_type = 'missing_asset'
            self.adjustment_amount = self.book_value
        elif self.actual_condition != self.expected_condition:
            self.discrepancy_type = 'condition_mismatch'
        else:
            self.discrepancy_type = 'none'
            self.adjustment_amount = 0.0

    def write(self, vals):
        if any(line.verification_id.state == 'adjusted' for line in self):
            raise UserError(_("Lines of an adjusted Asset Verification cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if any(line.verification_id.state == 'adjusted' for line in self):
            raise UserError(_("Lines of an adjusted Asset Verification cannot be deleted."))
        return super().unlink()


# =========================================================================
# Personal Tool Custody -> Fixed Asset Register Auto-Sync
# =========================================================================
class ContractorToolAssetFinanceSync(models.Model):
    _inherit = 'contractor.tool.asset'

    fixed_asset_id = fields.Many2one(
        'contractor.fixed.asset',
        string='Linked Fixed Asset Register Entry',
        copy=False,
        tracking=True,
    )

    def action_sync_to_fixed_asset_register(self):
        """Creates or updates the corresponding Fixed Asset Register entry for personal/site tools."""
        FixedAsset = self.env['contractor.fixed.asset'].sudo()
        for rec in self:
            if rec.fixed_asset_id:
                rec.fixed_asset_id.write({
                    'custodian_employee_id': rec.current_employee_id.id if rec.current_employee_id else False,
                    'project_id': rec.current_project_id.id if rec.current_project_id else False,
                })
                continue
            existing = FixedAsset.search([('tool_asset_id', '=', rec.id)], limit=1)
            if existing:
                rec.fixed_asset_id = existing.id
                continue
            if rec.purchase_value > 0:
                fa = FixedAsset.create({
                    'name': rec.name,
                    'acquisition_date': rec.purchase_date or fields.Date.context_today(rec),
                    'purchase_value': rec.purchase_value,
                    'useful_life_months': 24,
                    'is_personal_tool': True,
                    'tool_asset_id': rec.id,
                    'custodian_employee_id': rec.current_employee_id.id if rec.current_employee_id else False,
                    'project_id': rec.current_project_id.id if rec.current_project_id else False,
                    'company_id': rec.company_id.id,
                })
                rec.fixed_asset_id = fa.id
        return True

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.action_sync_to_fixed_asset_register()
        return records

    def write(self, vals):
        res = super().write(vals)
        if {'current_employee_id', 'current_project_id'} & set(vals.keys()):
            for rec in self.filtered('fixed_asset_id'):
                rec.fixed_asset_id.sudo().write({
                    'custodian_employee_id': rec.current_employee_id.id if rec.current_employee_id else False,
                    'project_id': rec.current_project_id.id if rec.current_project_id else False,
                })
        return res


