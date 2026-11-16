# -*- coding: utf-8 -*-
from odoo import models, fields, api, _


class AccountMoveThreeWayMatchUx(models.Model):
    _inherit = 'account.move'

    three_way_match_line_ids = fields.Many2many(
        'account.move.line',
        string='3-Way Match Breakdown Lines',
        compute='_compute_three_way_match_line_ids',
    )

    @api.depends('invoice_line_ids', 'invoice_line_ids.display_type', 'move_type')
    def _compute_three_way_match_line_ids(self):
        for move in self:
            if move.move_type != 'in_invoice':
                move.three_way_match_line_ids = self.env['account.move.line']
            else:
                move.three_way_match_line_ids = move.invoice_line_ids.filtered(
                    lambda l: l.display_type == 'product'
                )


class AccountMoveLineThreeWayMatchUx(models.Model):
    _inherit = 'account.move.line'

    po_qty = fields.Float(
        string='PO Qty',
        compute='_compute_line_three_way_match',
    )
    po_price_unit = fields.Float(
        string='PO Unit Price',
        digits='Product Price',
        compute='_compute_line_three_way_match',
    )
    grn_accepted_qty = fields.Float(
        string='GRN Accepted Qty',
        compute='_compute_line_three_way_match',
    )
    qty_variance = fields.Float(
        string='Qty Variance',
        compute='_compute_line_three_way_match',
    )
    price_variance = fields.Float(
        string='Price Variance',
        digits='Product Price',
        compute='_compute_line_three_way_match',
    )
    line_match_status = fields.Selection([
        ('matched', 'Matched'),
        ('qty_mismatch', 'Qty Mismatch'),
        ('price_mismatch', 'Price Mismatch'),
        ('both_mismatch', 'Qty & Price Mismatch'),
        ('no_po', 'No PO Link'),
    ], string='Match Status', compute='_compute_line_three_way_match')
    is_line_mismatch = fields.Boolean(
        string='Has 3-Way Variance',
        compute='_compute_line_three_way_match',
    )

    @api.depends(
        'move_id.move_type',
        'display_type',
        'quantity',
        'price_unit',
        'purchase_line_id',
        'purchase_line_id.product_qty',
        'purchase_line_id.price_unit',
        'purchase_line_id.qty_received',
        'company_id.over_delivery_tolerance_pct',
    )
    def _compute_line_three_way_match(self):
        for line in self:
            if line.move_id.move_type != 'in_invoice' or line.display_type != 'product':
                line.po_qty = 0.0
                line.po_price_unit = 0.0
                line.grn_accepted_qty = 0.0
                line.qty_variance = 0.0
                line.price_variance = 0.0
                line.line_match_status = 'matched'
                line.is_line_mismatch = False
                continue

            po_line = line.purchase_line_id
            if not po_line:
                line.po_qty = 0.0
                line.po_price_unit = 0.0
                line.grn_accepted_qty = 0.0
                line.qty_variance = line.quantity
                line.price_variance = line.price_unit
                line.line_match_status = 'no_po'
                line.is_line_mismatch = False
                continue

            po_qty = po_line.product_qty or 0.0
            po_price = po_line.price_unit or 0.0
            grn_qty = po_line.qty_received or 0.0
            inv_qty = line.quantity or 0.0
            inv_price = line.price_unit or 0.0

            tol_pct = ((line.company_id or self.env.company).over_delivery_tolerance_pct or 0.0) / 100.0
            max_allowed_qty = grn_qty * (1.0 + tol_pct)
            max_allowed_price = po_price * (1.0 + tol_pct)

            qty_var = inv_qty - grn_qty
            price_var = inv_price - po_price
            qty_bad = inv_qty > (max_allowed_qty + 1e-4)
            price_bad = inv_price > (max_allowed_price + 1e-4)

            if qty_bad and price_bad:
                status = 'both_mismatch'
            elif qty_bad:
                status = 'qty_mismatch'
            elif price_bad:
                status = 'price_mismatch'
            else:
                status = 'matched'

            line.po_qty = po_qty
            line.po_price_unit = po_price
            line.grn_accepted_qty = grn_qty
            line.qty_variance = qty_var
            line.price_variance = price_var
            line.line_match_status = status
            line.is_line_mismatch = bool(qty_bad or price_bad)


class ContractorPeriodCloseUx(models.Model):
    _inherit = 'contractor.period.close'

    def _get_gate_inspection_spec(self, gate_key):
        self.ensure_one()
        cid = self.company_id.id
        specs = {
            'gate_bank_recs_approved': (
                _('Bank Reconciliations Requiring Approval'),
                'contractor.bank.reconciliation',
                [('company_id', '=', cid), ('statement_date', '>=', self.date_start), ('statement_date', '<=', self.date_end), ('state', '!=', 'approved')],
            ),
            'gate_ap_bills_accruals_cleared': (
                _('AP Bills on 3-Way Match Hold'),
                'account.move',
                [('company_id', '=', cid), ('move_type', '=', 'in_invoice'), ('invoice_date', '<=', self.date_end), ('is_match_hold', '=', True)],
            ),
            'gate_hrms_payroll_eosb_verified': (
                _('Unsettled Payroll Runs in Period'),
                'contractor.payroll.run',
                [('company_id', '=', cid), ('date_end', '>=', self.date_start), ('date_end', '<=', self.date_end), ('state', '!=', 'released')],
            ),
            'gate_inventory_counts_reconciled': (
                _('Open Stock Adjustment Notes'),
                'contractor.stock.adjustment.note',
                [('company_id', '=', cid), ('state', 'not in', ['adjusted', 'declined'])],
            ),
            'gate_nca_depreciation_posted': (
                _('Assets with Unposted Depreciation'),
                'contractor.fixed.asset',
                [('company_id', '=', cid), ('depreciation_line_ids.schedule_date', '<=', self.date_end), ('depreciation_line_ids.is_posted', '=', False)],
            ),
            'gate_provisions_reviewed': (
                _('Unposted Accounting Provisions'),
                'contractor.accounting.provision',
                [('company_id', '=', cid), ('provision_date', '>=', self.date_start), ('provision_date', '<=', self.date_end), ('state', 'not in', ['posted', 'reversed'])],
            ),
            'gate_poc_revenue_wip_posted': (
                _('PoC WIP Schedules for Period'),
                'contractor.project.wip.schedule',
                [('company_id', '=', cid), ('period_date', '>=', self.date_start), ('period_date', '<=', self.date_end)],
            ),
            'gate_retention_register_reviewed': (
                _('Expired DLP Retentions Pending Review'),
                'contractor.retention.register',
                [('company_id', '=', cid), ('state', '=', 'dlp_expired_flagged'), ('dlp_expiry_date', '<=', self.date_end)],
            ),
            'gate_rpt_partner_accounts_reconciled': (
                _('Unapproved Related Party Transactions'),
                'account.move',
                [('company_id', '=', cid), ('date', '<=', self.date_end), ('is_rpt_transaction', '=', True), ('rpt_manager_approved', '=', False)],
            ),
        }
        if gate_key == 'gate_project_profitability_generated':
            # Inspect the first non-zero pending approval queue, defaulting to Material Requisitions
            pending_mr = self.env['contractor.material.requisition'].search_count([('company_id', '=', cid), ('state', '=', 'submitted')]) if 'contractor.material.requisition' in self.env else 0
            pending_q = self.env['sale.order'].search_count([('company_id', '=', cid), ('contractor_quote_state', '=', 'submitted')])
            if pending_q and not pending_mr:
                return (
                    _('Submitted Quotations Pending Review'),
                    'sale.order',
                    [('company_id', '=', cid), ('contractor_quote_state', '=', 'submitted')],
                )
            return (
                _('Submitted Requisitions Pending Approval'),
                'contractor.material.requisition',
                [('company_id', '=', cid), ('state', '=', 'submitted')],
            )
        return specs.get(gate_key, (
            _('Period Close Record'),
            'contractor.period.close',
            [('id', '=', self.id)],
        ))

    def action_view_gate_exceptions(self):
        """Opens the exact exception records causing a Period Close gate to remain incomplete."""
        self.ensure_one()
        gate_key = self.env.context.get('gate_key', 'gate_ap_bills_accruals_cleared')
        title, res_model, domain = self._get_gate_inspection_spec(gate_key)
        return {
            'name': title,
            'type': 'ir.actions.act_window',
            'res_model': res_model,
            'view_mode': 'list,form',
            'domain': domain,
            'target': 'current',
        }
