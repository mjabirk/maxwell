# -*- coding: utf-8 -*-
from datetime import timedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class AccountMoveThreeWayMatch(models.Model):
    _inherit = 'account.move'

    three_way_match_status = fields.Selection([
        ('not_applicable', 'Not Applicable (Non-PO Expense)'),
        ('matched', '3-Way Matched (PO = GRN = Bill)'),
        ('exception', 'Match Exception (On Hold)'),
        ('manually_cleared', 'Manually Cleared by Management'),
    ], string='3-Way Match Status', default='not_applicable', compute='_compute_three_way_match', store=True, readonly=False, tracking=True)

    is_match_hold = fields.Boolean(
        string='3-Way Match Hold',
        default=False,
        tracking=True,
        help='System gate blocking bill posting and payment release while 3-way discrepancy exists.',
    )
    match_exception_reason = fields.Text(
        string='3-Way Match Variance Details',
        tracking=True,
    )
    cleared_by_id = fields.Many2one(
        'res.users',
        string='Match Exception Cleared By',
        readonly=True,
        copy=False,
    )
    cleared_date = fields.Datetime(
        string='Cleared Date',
        readonly=True,
        copy=False,
    )

    # Vendor Advance Tracking
    open_vendor_advance_amount = fields.Monetary(
        string='Open PO Vendor Advance Balance',
        currency_field='currency_id',
        compute='_compute_vendor_advances',
    )
    has_unadjusted_vendor_advance = fields.Boolean(
        string='Has Unadjusted Vendor Advance',
        compute='_compute_vendor_advances',
    )
    advance_adjustment_amount = fields.Monetary(
        string='Advance Deducted on this Bill',
        currency_field='currency_id',
        default=0.0,
        tracking=True,
    )
    ar_overdue_escalated = fields.Boolean(
        string='90-Day Overdue Escalated',
        default=False,
        copy=False,
    )

    @api.depends('invoice_line_ids.purchase_line_id', 'invoice_line_ids.quantity', 'invoice_line_ids.price_unit')
    def _compute_three_way_match(self):
        for move in self:
            if move.move_type != 'in_invoice':
                move.three_way_match_status = 'not_applicable'
                move.is_match_hold = False
                continue

            po_lines = move.invoice_line_ids.mapped('purchase_line_id')
            if not po_lines:
                move.three_way_match_status = 'not_applicable'
                move.is_match_hold = False
                continue

            if move.three_way_match_status == 'manually_cleared':
                continue

            # Evaluate PO vs GRN vs Billed
            mismatches = []
            tolerance_pct = (move.company_id.over_delivery_tolerance_pct or 0.0) / 100.0

            for line in move.invoice_line_ids.filtered(lambda l: l.purchase_line_id):
                po_line = line.purchase_line_id
                billed_qty = line.quantity
                received_qty = po_line.qty_received
                po_price = po_line.price_unit
                billed_price = line.price_unit

                max_allowed_qty = received_qty * (1.0 + tolerance_pct)
                if billed_qty > max_allowed_qty:
                    mismatches.append(_(
                        "Line '%s': Billed Qty (%.2f) exceeds GRN Received Qty (%.2f) beyond %.1f%% tolerance."
                    ) % (line.name, billed_qty, received_qty, tolerance_pct * 100))

                if billed_price > (po_price * 1.001):  # Allow tiny rounding
                    mismatches.append(_(
                        "Line '%s': Billed Price Unit (%s %.2f) exceeds PO Contracted Price (%s %.2f)."
                    ) % (line.name, line.currency_id.symbol or '', billed_price, line.currency_id.symbol or '', po_price))

            if mismatches:
                move.three_way_match_status = 'exception'
                move.is_match_hold = True
                move.match_exception_reason = "\n".join(mismatches)
            else:
                move.three_way_match_status = 'matched'
                move.is_match_hold = False
                move.match_exception_reason = False

    @api.depends('invoice_line_ids.purchase_line_id.order_id', 'partner_id')
    def _compute_vendor_advances(self):
        for move in self:
            if move.move_type != 'in_invoice':
                move.open_vendor_advance_amount = 0.0
                move.has_unadjusted_vendor_advance = False
                continue

            po_orders = move.invoice_line_ids.mapped('purchase_line_id.order_id')
            if po_orders:
                advances = self.env['account.payment'].search([
                    ('partner_id', '=', move.partner_id.id),
                    ('purchase_order_id', 'in', po_orders.ids),
                    ('is_vendor_advance', '=', True),
                    ('vendor_advance_adjusted', '=', False),
                    ('state', 'in', ['in_process', 'paid']),
                ])
                tot_adv = sum(advances.mapped('amount'))
                move.open_vendor_advance_amount = tot_adv
                move.has_unadjusted_vendor_advance = tot_adv > 0.0
            else:
                move.open_vendor_advance_amount = 0.0
                move.has_unadjusted_vendor_advance = False

    def action_apply_vendor_advance(self):
        """Marks open PO vendor mobilization advances as applied/adjusted against this vendor bill."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise UserError(_("Access Denied: Only Accounting or Management users can adjust vendor advances."))
        if self.move_type != 'in_invoice':
            raise UserError(_("Vendor advance adjustments apply only to Vendor Bills."))

        po_orders = self.invoice_line_ids.mapped('purchase_line_id.order_id')
        domain = [
            ('partner_id', '=', self.partner_id.id),
            ('is_vendor_advance', '=', True),
            ('vendor_advance_adjusted', '=', False),
            ('state', 'in', ['in_process', 'paid']),
        ]
        if po_orders:
            domain.append(('purchase_order_id', 'in', po_orders.ids))

        advances = self.env['account.payment'].search(domain)
        if not advances:
            raise UserError(_("No open unadjusted vendor mobilization advances found for this vendor / PO."))

        tot_adv = sum(advances.mapped('amount'))
        if self.state == 'posted' and self.payment_state in ('not_paid', 'partial'):
            bill_payable = self.line_ids.filtered(
                lambda l: not l.reconciled and l.account_id.account_type == 'liability_payable' and l.credit > 0
            )
            adv_payable = advances.mapped('move_id.line_ids').filtered(
                lambda l: not l.reconciled and l.account_id.account_type == 'liability_payable' and l.debit > 0
            )
            for b_line in bill_payable:
                matching = adv_payable.filtered(lambda l: l.account_id == b_line.account_id and not l.reconciled)
                if matching:
                    (matching + b_line).reconcile()

        advances.write({'vendor_advance_adjusted': True})
        self.advance_adjustment_amount = (self.advance_adjustment_amount or 0.0) + tot_adv
        self._compute_vendor_advances()
        self.message_post(
            body=Markup(_(
                "<strong>Vendor Advance Adjusted:</strong> Applied %s %.2f from %s against Vendor Bill."
            )) % (self.currency_id.symbol or '', tot_adv, ', '.join(advances.mapped('name'))),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    @api.model
    def _cron_escalate_overdue_receivables(self):
        """Escalates posted client invoices overdue beyond company AR threshold to Management & MD."""
        today = fields.Date.context_today(self)
        mgr_group = self.env.ref('contractor_core.group_contractor_manager', raise_if_not_found=False)
        md_group = self.env.ref('contractor_core.group_contractor_md', raise_if_not_found=False)
        recipient_partners = self.env['res.partner']
        if mgr_group:
            recipient_partners |= mgr_group.user_ids.mapped('partner_id')
        if md_group:
            recipient_partners |= md_group.user_ids.mapped('partner_id')

        for company in self.env['res.company'].search([]):
            threshold_days = getattr(company, 'ar_escalation_overdue_days', 0) or 90
            cutoff_date = today - timedelta(days=threshold_days)
            overdue_invoices = self.sudo().search([
                ('company_id', '=', company.id),
                ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted'),
                ('payment_state', 'in', ['not_paid', 'partial']),
                ('ar_overdue_escalated', '=', False),
                ('invoice_date_due', '<=', cutoff_date),
            ])
            for inv in overdue_invoices:
                inv.write({'ar_overdue_escalated': True})
                inv.message_post(
                    body=Markup(_(
                        "<strong>AR Overdue Escalation (%d+ Days):</strong> "
                        "Customer Invoice <strong>%s</strong> (%s) for %s %.2f is overdue since %s. "
                        "Escalated to Management &amp; Managing Director."
                    )) % (
                        threshold_days,
                        inv.name or '',
                        inv.partner_id.name or '',
                        inv.currency_id.symbol or '',
                        inv.amount_residual,
                        inv.invoice_date_due,
                    ),
                    partner_ids=recipient_partners.ids,
                    message_type='notification',
                    subtype_xmlid='mail.mt_note',
                )
        return True

    def action_resolve_match_exception(self):
        """Allows Contractor Senior Accountant or Manager to clear 3-way match exception with justification."""
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_manager') or self.env.user.has_group('contractor_core.group_contractor_senior_accountant')):
            raise UserError(_("Access Denied: Only a Contractor Senior Accountant or Manager can override 3-way match holds."))

        return {
            'name': _('Override 3-Way Match Exception'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': 'override',
                'default_callback_method': '_callback_clear_match_hold',
                'active_model': self._name,
                'active_id': self.id,
                'callback_method': '_callback_clear_match_hold',
            },
        }

    def _callback_clear_match_hold(self, reason=None):
        clean_reason = (reason or getattr(self, 'last_action_reason', '')).strip()
        if len(clean_reason) < 15:
            raise UserError(_("Documented management justification (minimum 15 characters) is required to clear 3-way match holds."))

        self.write({
            'three_way_match_status': 'manually_cleared',
            'is_match_hold': False,
            'cleared_by_id': self.env.user.id,
            'cleared_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>3-Way Match Hold Cleared:</strong> Authorized by %s.<br/>Justification: %s")) % (
                self.env.user.name, clean_reason
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_post(self):
        # Enforce 3-Way Match Hard-Stop
        for move in self:
            if move.move_type == 'in_invoice' and move.is_match_hold:
                raise UserError(_(
                    "3-Way Match Governance Hard-Stop:\n\n"
                    "Vendor Bill '%s' cannot be posted or queued for payment.\n\n"
                    "The bill has an active 3-Way Match Exception Hold:\n%s\n\n"
                    "Quantities or rates must match PO/GRN records or be formally cleared by Senior Management."
                ) % (move.name or 'Draft Bill', move.match_exception_reason or 'Quantity / Price Variance'))
        return super().action_post()


# =========================================================================
# 3-Step Payment Maker-Checker & Vendor Advance Management
# =========================================================================
class AccountPaymentMakerChecker(models.Model):
    _inherit = 'account.payment'

    is_vendor_advance = fields.Boolean(
        string='Is Vendor Mobilization / Advance',
        default=False,
        tracking=True,
        help='Tick if this disbursement is an advance payment against a Purchase Order prior to GRN delivery.',
    )
    purchase_order_id = fields.Many2one(
        'purchase.order',
        string='Linked Purchase Order',
        tracking=True,
    )
    vendor_advance_adjusted = fields.Boolean(
        string='Advance Adjusted on Bill',
        default=False,
        tracking=True,
        copy=False,
    )

    payment_workflow_state = fields.Selection([
        ('draft', 'Draft Payment Request'),
        ('prepared', 'Prepared by Accountant'),
        ('approved', 'Approved by Manager'),
        ('released', 'Released & Disbursed'),
        ('rejected', 'Rejected'),
    ], string='Maker-Checker Status', default='draft', required=True, tracking=True, copy=False)

    prepared_by_id = fields.Many2one(
        'res.users',
        string='Step 1: Prepared By (Accountant)',
        readonly=True,
        copy=False,
        tracking=True,
    )
    preparation_date = fields.Datetime(
        string='Preparation Timestamp',
        readonly=True,
        copy=False,
    )
    approved_by_id = fields.Many2one(
        'res.users',
        string='Step 2: Approved By (Manager)',
        readonly=True,
        copy=False,
        tracking=True,
    )
    approval_date = fields.Datetime(
        string='Approval Timestamp',
        readonly=True,
        copy=False,
    )
    released_by_id = fields.Many2one(
        'res.users',
        string='Step 3: Released By (Disbursement)',
        readonly=True,
        copy=False,
        tracking=True,
    )
    release_date = fields.Datetime(
        string='Release Timestamp',
        readonly=True,
        copy=False,
    )

    def action_prepare_payment(self):
        """Step 1: Accountant prepares and certifies payment calculation."""
        self.ensure_one()
        if self.amount <= 0:
            raise UserError(_("Payment amount must be strictly greater than zero."))

        if not (self.env.user.has_group('contractor_core.group_contractor_accountant') or self.env.user.has_group('contractor_core.group_contractor_senior_accountant')):
            raise UserError(_("Access Denied: Only an Accountant can prepare payment disbursements."))

        self.write({
            'payment_workflow_state': 'prepared',
            'prepared_by_id': self.env.user.id,
            'preparation_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Step 1 (Maker): Payment Prepared</strong> by %s for %s %.2f. Forwarded for Manager authorization.")) % (
                self.env.user.name, self.currency_id.symbol or '', self.amount
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_approve_payment(self):
        """Step 2: Manager checks and approves payment (Strict Maker != Checker)."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize payments."))

        if self.prepared_by_id and self.env.user.id == self.prepared_by_id.id:
            raise UserError(_(
                "Segregation of Duties Hard-Stop:\n\n"
                "The user who prepared this payment (%s) cannot also approve it.\n"
                "A distinct Contractor Manager must review and authorize the disbursement."
            ) % self.prepared_by_id.name)

        self.write({
            'payment_workflow_state': 'approved',
            'approved_by_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Step 2 (Checker): Payment Approved</strong> by Manager %s. Authorized for bank release.")) % (
                self.env.user.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_release_payment(self):
        """Step 3: Releaser posts GL move and executes fund transfer."""
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_manager') or self.env.user.has_group('contractor_core.group_contractor_senior_accountant')):
            raise UserError(_("Access Denied: Only an authorized Senior Accountant or Manager can execute payment release."))

        if self.payment_workflow_state != 'approved':
            raise UserError(_("Payment must be approved in Step 2 before release."))

        self.write({
            'payment_workflow_state': 'released',
            'released_by_id': self.env.user.id,
            'release_date': fields.Datetime.now(),
        })

        # Post the standard payment move
        res = super().action_post()

        self.message_post(
            body=Markup(_("<strong>Step 3 (Releaser): Payment Released</strong> and posted to bank ledger by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return res

    def action_post(self):
        """Enforces 3-step Maker-Checker before posting."""
        for payment in self:
            if payment.payment_workflow_state != 'released':
                raise UserError(_(
                    "3-Step Payment Maker-Checker Hard-Stop:\n\n"
                    "Payment '%s' cannot be posted directly.\n"
                    "It must proceed through the 3 mandatory gates:\n"
                    "1. Preparation (Accountant)\n"
                    "2. Approval (Manager, distinct from preparer)\n"
                    "3. Release (Authorized Releaser)"
                ) % (payment.name or 'Draft Payment'))
        return super().action_post()

