# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


# =========================================================================
# 1. Purchase Order Revisions & Governance
# =========================================================================
class PurchaseOrder(models.Model):
    _name = 'purchase.order'
    _inherit = ['purchase.order', 'contractor.version.mixin', 'contractor.reason.mixin', 'contractor.no.delete.mixin']

    requisition_id = fields.Many2one(
        'contractor.material.requisition',
        string='Material Requisition',
        readonly=True,
        copy=False,
        index=True,
    )
    comparative_statement_id = fields.Many2one(
        'contractor.comparative.statement',
        string='Comparative Statement',
        readonly=True,
        copy=False,
        index=True,
    )
    requires_md_approval = fields.Boolean(
        string='Requires Managing Director Approval',
        compute='_compute_po_approvals',
        store=True,
    )
    md_approval_state = fields.Selection([
        ('not_required', 'Not Required'),
        ('pending', 'Pending MD Approval'),
        ('approved', 'Approved by MD'),
        ('declined', 'Declined by MD'),
    ], string='Executive Sign-Off Status', default='not_required', tracking=True, copy=False)

    md_approver_id = fields.Many2one(
        'res.users',
        string='MD Approver',
        readonly=True,
        copy=False,
    )
    md_approval_date = fields.Datetime(
        string='MD Approval Date',
        readonly=True,
        copy=False,
    )

    @api.depends('amount_total', 'company_id.po_md_approval_threshold')
    def _compute_po_approvals(self):
        for po in self:
            thresh = po.company_id.po_md_approval_threshold or 50000.0
            po.requires_md_approval = (po.amount_total or 0.0) >= thresh
            if not po.requires_md_approval and po.md_approval_state == 'pending':
                po.md_approval_state = 'not_required'

    def action_request_md_approval(self):
        self.ensure_one()
        self._compute_po_approvals()
        if not self.requires_md_approval:
            raise UserError(_("This Purchase Order is below the Executive Threshold and does not require MD approval."))

        self.md_approval_state = 'pending'
        group_md = self.env.ref('contractor_core.group_contractor_md', raise_if_not_found=False)
        if group_md:
            for user in (group_md.all_user_ids | group_md.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('purchase.order'),
                    'res_id': self.id,
                    'user_id': user.id,
                    'summary': f"Executive Clearance Required: PO {self.name}",
                    'note': _("Purchase order of %s %.2f exceeds executive threshold. Approval required.") % (
                        self.currency_id.symbol or '', self.amount_total
                    ),
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_("<strong>Executive Approval Requested:</strong> Forwarded to Managing Director.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_md_approve_po(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Only the Managing Director can execute executive PO approvals."))

        self.write({
            'md_approval_state': 'approved',
            'md_approver_id': self.env.user.id,
            'md_approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Executive Approval Granted:</strong> Authorized by Managing Director %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def button_confirm(self):
        """Hard-Stops on Confirmation:
        1. Must be linked to approved MR or be an Emergency Purchase.
        2. Managing Director approval check if threshold exceeded.
        3. Project budget overrun check.
        """
        for order in self:
            # 1. MR or Emergency Purchase Hard-Stop
            is_emergency = getattr(order, 'is_emergency_purchase', False)
            if is_emergency:
                if not getattr(order, 'emergency_reason_code', False) or len((getattr(order, 'emergency_explanation', '') or '').strip()) < 15:
                    raise UserError(_(
                        "Emergency Purchase Governance Hard-Stop:\n\n"
                        "PO '%s' requires a valid Emergency Classification and Justification (min 15 characters)."
                    ) % order.name)
                if not getattr(order, 'emergency_manager_approved', False):
                    raise UserError(_(
                        "Emergency Purchase Authorization Hard-Stop:\n\n"
                        "Emergency PO '%s' cannot be confirmed without prior Contractor Manager authorization "
                        "(click 'Authorize Emergency Purchase')."
                    ) % order.name)
                cap = order.company_id.emergency_po_cap or 15000.0
                if order.amount_total > cap:
                    raise UserError(_(
                        "Emergency Purchase Cap Hard-Stop:\n\n"
                        "Emergency PO '%s' (%s %.2f) exceeds the company Emergency Cap (%s %.2f)."
                    ) % (order.name, order.currency_id.symbol or '', order.amount_total, order.currency_id.symbol or '', cap))
            else:
                if not order.requisition_id:
                    raise UserError(_(
                        "Procurement Governance Hard-Stop:\n\n"
                        "Direct confirmation of Purchase Order '%s' is prohibited.\n\n"
                        "All standard purchases must originate from an approved Material Requisition (MR) "
                        "or be designated and authorized as an Emergency Purchase."
                    ) % order.name)
                if order.requisition_id.state not in ('approved', 'cs_in_progress', 'po_created', 'done'):
                    raise UserError(_(
                        "Procurement Governance Hard-Stop:\n\n"
                        "Material Requisition '%s' is in '%s' state and must be approved before confirming PO '%s'."
                    ) % (order.requisition_id.name, order.requisition_id.state, order.name))

            # 2. Executive Threshold Check
            if order.requires_md_approval and order.md_approval_state != 'approved':
                if self.env.user.has_group('contractor_core.group_contractor_md'):
                    order.write({
                        'md_approval_state': 'approved',
                        'md_approver_id': self.env.user.id,
                        'md_approval_date': fields.Datetime.now(),
                    })
                else:
                    order.action_request_md_approval()
                    raise UserError(_(
                        "Executive Threshold Hard-Stop:\n\n"
                        "PO '%s' value (%s %.2f) meets or exceeds the PO MD Approval Threshold (%s %.2f).\n"
                        "Managing Director sign-off is mandatory before this order can be confirmed."
                    ) % (
                        order.name, order.currency_id.symbol or '', order.amount_total,
                        order.currency_id.symbol or '', order.company_id.po_md_approval_threshold or 50000.0
                    ))

            # 3. Budget Check (avoid double-counting commitment already reserved by source MR)
            if order.project_id:
                req_amt = order.amount_total
                if order.requisition_id and order.requisition_id.state in ('approved', 'cs_in_progress', 'po_created'):
                    has_prior_confirmed_po = any(
                        p.id != order.id and p.state in ('purchase', 'done')
                        for p in order.requisition_id.purchase_order_ids
                    )
                    if not has_prior_confirmed_po:
                        req_amt = max(req_amt - (order.requisition_id.total_estimated_amount or 0.0), 0.0)
                if req_amt > 0:
                    self.env['contractor.project.budget'].check_budget_availability(
                        project_id=order.project_id.id,
                        cost_head='material',
                        requested_amount=req_amt,
                    )

        res = super().button_confirm()
        self.env['contractor.project.budget']._refresh_project_consumption(self.mapped('project_id').ids)

        # Propagate emergency flags to pickings if emergency purchase
        for order in self:
            if getattr(order, 'is_emergency_purchase', False):
                order.picking_ids.write({
                    'is_emergency_purchase': True,
                    'emergency_reason_code': order.emergency_reason_code,
                    'emergency_explanation': order.emergency_explanation,
                })
        return res

    @api.onchange('requisition_id', 'comparative_statement_id')
    def _onchange_procurement_lineage_project(self):
        if not self.project_id:
            if self.requisition_id and self.requisition_id.project_id:
                self.project_id = self.requisition_id.project_id
            elif self.comparative_statement_id and self.comparative_statement_id.project_id:
                self.project_id = self.comparative_statement_id.project_id

    def _prepare_picking(self):
        res = super()._prepare_picking()
        if self.project_id:
            res['project_id'] = self.project_id.id
        elif self.requisition_id and self.requisition_id.project_id:
            res['project_id'] = self.requisition_id.project_id.id
        return res

    def write(self, vals):
        # Version snapshotting for post-confirmation modifications
        for po in self:
            if po.state in ('purchase', 'done') and ('order_line' in vals or 'partner_id' in vals):
                po._create_version_snapshot(
                    reason=_("Post-confirmation modification of Purchase Order terms or lines."),
                    editor_user=self.env.user,
                )
        return super().write(vals)

    def button_cancel(self):
        for po in self:
            if po.state in ('purchase', 'done') and not self.env.context.get('skip_reason_check'):
                if not getattr(po, 'last_action_reason', None):
                    return po.action_open_reason_wizard(
                        action_type='cancel',
                        target_state='cancel',
                        callback_method='_callback_cancel_po',
                    )
        res = super().button_cancel()
        self.env['contractor.project.budget']._refresh_project_consumption(self.mapped('project_id').ids)
        return res

    def _callback_cancel_po(self, reason=None):
        return self.with_context(skip_reason_check=True).button_cancel()


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    def _prepare_stock_move_vals(self, picking, price_unit, product_uom_qty, product_uom):
        vals = super()._prepare_stock_move_vals(picking, price_unit, product_uom_qty, product_uom)
        project = self.order_id.project_id or (picking and picking.project_id)
        if project:
            vals['project_id'] = project.id
        return vals


class StockRule(models.Model):
    _inherit = 'stock.rule'

    def _prepare_purchase_order(self, company_id, origins, values):
        res = super()._prepare_purchase_order(company_id, origins, values)
        if not res.get('project_id'):
            for val in values:
                if val.get('project_id'):
                    proj_val = val['project_id']
                    res['project_id'] = proj_val[0] if isinstance(proj_val, list) else proj_val
                    break
        return res


# =========================================================================
# 2. Stock Picking GRN Controls & Over-Delivery Protection
# =========================================================================
class StockPicking(models.Model):
    _inherit = 'stock.picking'

    over_delivery_detected = fields.Boolean(
        string='Over-Delivery Detected',
        compute='_compute_over_delivery',
        store=True,
    )
    over_delivery_approved = fields.Boolean(
        string='Over-Delivery Authorized by Manager',
        default=False,
        tracking=True,
        copy=False,
    )
    over_delivery_approved_by = fields.Many2one(
        'res.users',
        string='Over-Delivery Approved By',
        readonly=True,
        copy=False,
    )
    over_delivery_reason = fields.Text(
        string='Over-Delivery Acceptance Reason',
        readonly=True,
        copy=False,
    )
    has_line_rejections = fields.Boolean(
        string='Has Rejected Items',
        compute='_compute_rejections',
    )

    @api.depends('move_ids.quantity', 'move_ids.product_uom_qty', 'company_id.over_delivery_tolerance_pct')
    def _compute_over_delivery(self):
        for picking in self:
            detected = False
            tolerance = picking.company_id.over_delivery_tolerance_pct or 0.0
            mult = 1.0 + (tolerance / 100.0)
            for move in picking.move_ids:
                if move.product_uom_qty > 0 and move.quantity > (move.product_uom_qty * mult):
                    detected = True
                    break
            picking.over_delivery_detected = detected

    @api.depends('move_ids.rejected_qty')
    def _compute_rejections(self):
        for picking in self:
            picking.has_line_rejections = any(m.rejected_qty > 0 for m in picking.move_ids)

    def action_approve_over_delivery(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize excess deliveries."))

        return self.action_open_reason_wizard(
            action_type='override',
            callback_method='_callback_over_delivery',
        )

    def _callback_over_delivery(self, reason=None):
        self.write({
            'over_delivery_approved': True,
            'over_delivery_approved_by': self.env.user.id,
            'over_delivery_reason': reason or self.last_action_reason,
        })
        self.message_post(
            body=Markup(_("<strong>Over-Delivery Authorized:</strong> Approved by Contractor Manager %s.<br/>Reason: %s")) % (
                self.env.user.name, self.over_delivery_reason
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def button_validate(self):
        grn_allowed_groups = (
            'contractor_core.group_contractor_storekeeper',
            'contractor_core.group_contractor_site_engineer',
            'contractor_core.group_contractor_project_manager',
            'contractor_core.group_contractor_manager',
            'contractor_core.group_contractor_md',
            'base.group_system',
        )
        for picking in self:
            if picking.picking_type_code == 'incoming' and not self.env.su:
                if not any(self.env.user.has_group(g) for g in grn_allowed_groups):
                    raise AccessError(_(
                        "GRN Governance Hard-Stop:\n\n"
                        "Only authorized Storekeepers, Site Engineers, Project Managers, or Managers may validate Goods Receipt Notes."
                    ))
            tolerance = picking.company_id.over_delivery_tolerance_pct or 0.0
            mult = 1.0 + (tolerance / 100.0)
            rejection_notes = []
            for move in picking.move_ids:
                if move.product_uom_qty > 0 and move.quantity > (move.product_uom_qty * mult):
                    if not picking.over_delivery_approved:
                        raise UserError(_(
                            "GRN Over-Delivery Hard-Stop Enforced:\n\n"
                            "Product '%s' received quantity (%.2f) exceeds ordered quantity (%.2f) "
                            "beyond the allowable company tolerance (%.1f%%).\n\n"
                            "Receipt validation is blocked until authorized by a Contractor Manager."
                        ) % (move.product_id.display_name, move.quantity, move.product_uom_qty, tolerance))
                if move.rejected_qty and move.rejected_qty > 0:
                    if not (move.rejection_reason or '').strip():
                        raise UserError(_(
                            "GRN Rejection Hard-Stop:\n\n"
                            "Product '%s' has rejected quantity (%.2f). Please specify the 'Reason for Rejection' before validating."
                        ) % (move.product_id.display_name, move.rejected_qty))
                    if not move.rejection_action:
                        move.rejection_action = 'return_to_vendor'
                    action_label = dict(move._fields['rejection_action'].selection).get(move.rejection_action, move.rejection_action)
                    rejection_notes.append(
                        f"<li>{move.product_id.display_name}: Accepted {move.quantity:.2f}, Rejected {move.rejected_qty:.2f} "
                        f"({action_label}) — {move.rejection_reason}</li>"
                    )
            if rejection_notes:
                picking.message_post(
                    body=Markup(_(
                        "<strong>GRN Quality Rejections Logged (Excluded from Accepted Stock):</strong><ul>%s</ul>"
                    )) % Markup(''.join(rejection_notes)),
                    message_type='notification',
                    subtype_xmlid='mail.mt_note',
                )
        return super().button_validate()


class StockMove(models.Model):
    _inherit = 'stock.move'

    rejected_qty = fields.Float(
        string='Rejected Qty (Damaged/Non-Compliant)',
        default=0.0,
    )
    rejection_reason = fields.Char(
        string='Reason for Rejection',
    )
    rejection_action = fields.Selection([
        ('return_to_vendor', 'Return to Vendor'),
        ('debit_note', 'Debit Note / Credit'),
        ('replacement', 'Await Vendor Replacement'),
    ], string='Rejection Disposition')


# =========================================================================
# 3. Leftover Stock Disposition Model (LOD)
# =========================================================================
class ContractorLeftoverDisposition(models.Model):
    _name = 'contractor.leftover.disposition'
    _description = 'Contractor Leftover Material Disposition'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.version.mixin', 'contractor.reason.mixin', 'contractor.no.delete.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='Disposition Number',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New LOD'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Source Project',
        required=True,
        index=True,
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
    disposition_type = fields.Selection([
        ('return_to_store', 'Return to Central Store'),
        ('transfer_to_project', 'Transfer to Another Active Project'),
        ('write_off', 'Scrap / Inventory Write-Off'),
    ], string='Disposition Strategy', required=True, default='return_to_store', tracking=True)

    target_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Target Central Warehouse',
        domain="[('warehouse_type', '=', 'central')]",
    )
    target_project_id = fields.Many2one(
        'project.project',
        string='Target Project',
    )
    state = fields.Selection([
        ('draft', 'Draft Disposition'),
        ('submitted', 'Submitted for Verification'),
        ('verified', 'Verified by Storekeeper / Receiving PM'),
        ('approved', 'Approved by Manager / PM'),
        ('md_approved', 'Approved by Managing Director'),
        ('executed', 'Executed & Transferred'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    verified_by_id = fields.Many2one(
        'res.users',
        string='Verified By (Storekeeper / Receiving PM)',
        readonly=True,
        copy=False,
    )
    requires_md_approval = fields.Boolean(
        string='Requires MD Approval (Write-Off)',
        compute='_compute_total_value',
        store=True,
    )
    approver_id = fields.Many2one(
        'res.users',
        string='Approved By',
        readonly=True,
        copy=False,
    )
    execution_date = fields.Datetime(
        string='Execution Date',
        readonly=True,
        copy=False,
    )
    line_ids = fields.One2many(
        'contractor.leftover.disposition.line',
        'disposition_id',
        string='Surplus Materials',
        copy=True,
    )
    total_value = fields.Monetary(
        string='Total Surplus Value',
        currency_field='currency_id',
        compute='_compute_total_value',
        store=True,
    )
    picking_id = fields.Many2one(
        'stock.picking',
        string='Generated Stock Picking',
        readonly=True,
        copy=False,
    )
    cost_adjustment_move_id = fields.Many2one(
        'account.move',
        string='Project Cost Adjustment Entry',
        readonly=True,
        copy=False,
    )

    @api.depends('line_ids.subtotal_cost', 'disposition_type', 'company_id.po_md_approval_threshold')
    def _compute_total_value(self):
        for lod in self:
            tot = sum(lod.line_ids.mapped('subtotal_cost'))
            lod.total_value = tot
            thresh = (lod.company_id or self.env.company).po_md_approval_threshold or 50000.0
            lod.requires_md_approval = (lod.disposition_type == 'write_off' and tot >= thresh)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New LOD')) == _('New LOD'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.leftover.disposition') or _('New LOD')
        return super().create(vals_list)

    def action_submit(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Please specify at least one surplus material item."))

        if self.disposition_type == 'return_to_store' and not self.target_warehouse_id:
            raise UserError(_("Target Central Warehouse is mandatory for Return to Central Store."))
        if self.disposition_type == 'transfer_to_project' and not self.target_project_id:
            raise UserError(_("Target Project is mandatory for Transfer to Another Project."))
        if self.disposition_type == 'transfer_to_project' and self.target_project_id.id == self.project_id.id:
            raise UserError(_("Source Project and Target Project must be distinct."))

        self.state = 'submitted'
        group_mgr = self.env.ref('contractor_core.group_contractor_manager', raise_if_not_found=False)
        if group_mgr:
            for user in (group_mgr.all_user_ids | group_mgr.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('contractor.leftover.disposition'),
                    'res_id': self.id,
                    'user_id': user.id,
                    'summary': f"Approve Surplus Stock Disposition: {self.name}",
                    'note': _("Disposition plan for project %s valued at %s %.2f requires approval.") % (
                        self.project_id.name, self.currency_id.symbol or '', self.total_value
                    ),
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_("<strong>Surplus Disposition Submitted:</strong> Forwarded to Contractor Manager for sign-off.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_verify(self):
        """Physical/operational verification step (Storekeeper for Return/Write-Off; Receiving PM for Project Transfer)."""
        self.ensure_one()
        if not (
            self.env.su
            or self.env.user.has_group('contractor_core.group_contractor_storekeeper')
            or self.env.user.has_group('contractor_core.group_contractor_project_manager')
            or self.env.user.has_group('contractor_core.group_contractor_manager')
        ):
            raise AccessError(_("Access Denied: Only Storekeepers, Project Managers, or Managers can verify leftover stock."))
        self.write({
            'state': 'verified',
            'verified_by_id': self.env.user.id,
        })
        self.message_post(
            body=Markup(_("<strong>Surplus Stock Verified:</strong> Quantity &amp; condition verified by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_manager_approve(self):
        self.ensure_one()
        is_pm_allowed = (
            self.disposition_type == 'return_to_store'
            and self.env.user.has_group('contractor_core.group_contractor_project_manager')
        )
        if not (self.env.user.has_group('contractor_core.group_contractor_manager') or is_pm_allowed):
            raise UserError(_("Access Denied: Only a Contractor Manager (or Donor PM for Store Returns) can authorize leftover material dispositions."))

        if self.requires_md_approval and not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_(
                "Executive Threshold Exceeded:\n\n"
                "Write-off value (%s %.2f) requires Managing Director approval."
            ) % (self.currency_id.symbol or '', self.total_value))

        target_state = 'md_approved' if (self.requires_md_approval and self.env.user.has_group('contractor_core.group_contractor_md')) else 'approved'
        self.write({
            'state': target_state,
            'approver_id': self.env.user.id,
        })
        self.message_post(
            body=Markup(_("<strong>Disposition Plan Approved:</strong> Authorized by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_md_approve(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Only the Managing Director can approve high-value write-offs."))
        self.write({
            'state': 'md_approved',
            'approver_id': self.env.user.id,
        })
        self.message_post(
            body=Markup(_("<strong>Executive Approval Granted:</strong> Write-off authorized by Managing Director %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def _post_project_cost_adjustment_entry(self):
        """Credits donor project cost for returns/transfers and debits central accrual or receiving project."""
        self.ensure_one()
        if self.total_value <= 0 or self.cost_adjustment_move_id:
            return False
        misc_journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        expense_acc = self.company_id.default_expense_account_id or self.env['account.account'].search([
            ('internal_group', '=', 'expense'),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1)
        clearing_acc = self.company_id.default_cost_accrual_account_id or expense_acc
        if not misc_journal or not expense_acc:
            return False

        donor_analytic = {str(self.project_id.account_id.id): 100.0} if self.project_id.account_id else False
        move_lines = []
        if self.disposition_type == 'return_to_store':
            move_lines = [
                (0, 0, {
                    'name': f"Leftover Return Credit: {self.name} ({self.project_id.name})",
                    'account_id': expense_acc.id,
                    'debit': 0.0,
                    'credit': self.total_value,
                    'project_id': self.project_id.id,
                    'cost_head': 'material',
                    'analytic_distribution': donor_analytic,
                }),
                (0, 0, {
                    'name': f"Central Store Inventory Return: {self.name}",
                    'account_id': clearing_acc.id,
                    'debit': self.total_value,
                    'credit': 0.0,
                }),
            ]
        elif self.disposition_type == 'transfer_to_project' and self.target_project_id:
            recv_analytic = {str(self.target_project_id.account_id.id): 100.0} if self.target_project_id.account_id else False
            move_lines = [
                (0, 0, {
                    'name': f"Leftover Transfer Out Credit: {self.name} ({self.project_id.name})",
                    'account_id': expense_acc.id,
                    'debit': 0.0,
                    'credit': self.total_value,
                    'project_id': self.project_id.id,
                    'cost_head': 'material',
                    'analytic_distribution': donor_analytic,
                }),
                (0, 0, {
                    'name': f"Leftover Transfer In Charge: {self.name} ({self.target_project_id.name})",
                    'account_id': expense_acc.id,
                    'debit': self.total_value,
                    'credit': 0.0,
                    'project_id': self.target_project_id.id,
                    'cost_head': 'material',
                    'analytic_distribution': recv_analytic,
                }),
            ]
        else:
            # Write-off: no credit to donor project cost; optionally reclassify/log as write-off
            return False

        move = self.env['account.move'].create({
            'journal_id': misc_journal.id,
            'date': fields.Date.context_today(self),
            'ref': f"LOD {self.name}",
            'move_type': 'entry',
            'project_id': self.project_id.id,
            'line_ids': move_lines,
        })
        move.action_post()
        return move

    def action_execute(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_storekeeper') or self.env.user.has_group('contractor_core.group_contractor_procurement_officer')):
            raise AccessError(_("Access Denied: Only Storekeepers, Procurement Officers, or Managers can execute surplus dispositions."))
        allowed_states = ('md_approved',) if self.requires_md_approval else ('approved', 'md_approved')
        if self.state not in allowed_states:
            raise UserError(_("Only approved dispositions can be executed."))
        if self.picking_id:
            raise UserError(_("A stock picking (%s) has already been processed for this disposition.") % self.picking_id.name)

        # Create picking or inventory scrap
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'internal'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)

        dest_location = False
        if self.disposition_type == 'return_to_store' and self.target_warehouse_id:
            dest_location = self.target_warehouse_id.lot_stock_id
        elif self.disposition_type == 'write_off':
            dest_location = self.env['stock.location'].search([
                ('usage', '=', 'inventory'),
                ('company_id', 'in', [self.company_id.id, False]),
            ], limit=1)

        if not dest_location:
            dest_location = self.env['stock.location'].search([
                ('usage', '=', 'internal'),
                ('company_id', 'in', [self.company_id.id, False]),
            ], limit=1)

        source_location = self.env['stock.location'].search([
            ('usage', '=', 'internal'),
            ('project_id', '=', self.project_id.id),
        ], limit=1) or dest_location

        target_proj = self.target_project_id or self.project_id
        moves = []
        for line in self.line_ids:
            moves.append((0, 0, {
                'description_picking': f"Surplus Disposition: {line.product_id.display_name}",
                'product_id': line.product_id.id,
                'product_uom': line.product_uom_id.id,
                'product_uom_qty': line.qty,
                'location_id': source_location.id,
                'location_dest_id': dest_location.id,
                'company_id': self.company_id.id,
                'project_id': target_proj.id,
            }))

        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id if picking_type else False,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
            'origin': self.name,
            'project_id': target_proj.id,
            'move_ids': moves,
        })
        picking.action_confirm()
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.button_validate()

        cost_move = self._post_project_cost_adjustment_entry()

        self.write({
            'state': 'executed',
            'execution_date': fields.Datetime.now(),
            'picking_id': picking.id,
            'cost_adjustment_move_id': cost_move.id if cost_move else False,
        })

        proj_ids = [self.project_id.id]
        if self.target_project_id:
            proj_ids.append(self.target_project_id.id)
        self.env['contractor.project.budget']._refresh_project_consumption(proj_ids)

        self.message_post(
            body=Markup(_("<strong>Disposition Executed:</strong> Stock picking %s processed.%s")) % (
                picking.name,
                f" Cost adjustment JE: {cost_move.name}." if cost_move else "",
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_decline(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Manager can decline surplus dispositions."))
        return self.action_open_reason_wizard(action_type='decline', target_state='declined')

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        if any(rec.state == 'executed' for rec in self) and not set(vals.keys()).issubset(allowed_meta_fields):
            raise UserError(_("Executed Leftover Dispositions are locked and cannot be modified."))
        return super().write(vals)


class ContractorLeftoverDispositionLine(models.Model):
    _name = 'contractor.leftover.disposition.line'
    _description = 'Surplus Material Item'

    disposition_id = fields.Many2one(
        'contractor.leftover.disposition',
        string='Disposition Reference',
        required=True,
        ondelete='cascade',
    )
    product_id = fields.Many2one(
        'product.product',
        string='Product / SKU',
        required=True,
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='UoM',
        related='product_id.uom_id',
        readonly=True,
    )
    qty = fields.Float(
        string='Surplus Qty',
        required=True,
        default=1.0,
    )
    unit_cost = fields.Monetary(
        string='Unit Cost',
        currency_field='currency_id',
        compute='_compute_default_cost',
        store=True,
        readonly=False,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='disposition_id.currency_id',
        readonly=True,
    )
    subtotal_cost = fields.Monetary(
        string='Subtotal Cost',
        currency_field='currency_id',
        compute='_compute_subtotal',
        store=True,
    )
    disposition_reason = fields.Char(
        string='Condition / Remarks',
        default='Usable surplus from project milestone completion',
    )

    @api.depends('product_id')
    def _compute_default_cost(self):
        for line in self:
            if not line.unit_cost and line.product_id:
                line.unit_cost = line.product_id.standard_price or 0.0

    @api.depends('qty', 'unit_cost')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal_cost = (line.qty or 0.0) * (line.unit_cost or 0.0)

    def write(self, vals):
        if any(line.disposition_id.state == 'executed' for line in self):
            raise UserError(_("Lines of an executed Leftover Disposition cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if any(line.disposition_id.state == 'executed' for line in self):
            raise UserError(_("Lines of an executed Leftover Disposition cannot be deleted."))
        return super().unlink()


# =========================================================================
# 4. Physical Stock Adjustment Note (SAN)
# =========================================================================
class ContractorStockAdjustmentNote(models.Model):
    _name = 'contractor.stock.adjustment.note'
    _description = 'Physical Stock Count & Variance Investigation Note'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.version.mixin', 'contractor.reason.mixin', 'contractor.no.delete.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='SAN Number',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New SAN'),
        index=True,
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Warehouse / Store',
        required=True,
        tracking=True,
    )
    location_id = fields.Many2one(
        'stock.location',
        string='Storage Location',
        required=True,
        domain="[('usage', '=', 'internal')]",
        tracking=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Associated Project',
        related='warehouse_id.project_id',
        store=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='warehouse_id.company_id',
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        readonly=True,
    )
    count_date = fields.Date(
        string='Physical Count Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Draft Physical Count'),
        ('investigation', 'Under Variance Investigation'),
        ('manager_approved', 'Approved by Contractor Manager'),
        ('md_approved', 'Approved by Managing Director'),
        ('adjusted', 'Stock Adjusted & Reconciled'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    investigation_report = fields.Text(
        string='Investigation Findings & Reconciliation Report',
        tracking=True,
    )
    requires_md_approval = fields.Boolean(
        string='Requires Managing Director Approval',
        compute='_compute_totals',
        store=True,
    )
    approver_id = fields.Many2one(
        'res.users',
        string='Approved By',
        readonly=True,
        copy=False,
    )
    site_engineer_verified_by = fields.Many2one(
        'res.users',
        string='Jointly Co-Verified By (Site Engineer / Storekeeper)',
        readonly=True,
        copy=False,
    )
    approval_date = fields.Datetime(
        string='Approval Date',
        readonly=True,
        copy=False,
    )
    line_ids = fields.One2many(
        'contractor.stock.adjustment.note.line',
        'san_id',
        string='Count Variance Items',
        copy=True,
    )
    total_variance_value = fields.Monetary(
        string='Net Variance Value',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    total_abs_variance_value = fields.Monetary(
        string='Absolute Variance Value',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )

    @api.depends('line_ids.variance_value', 'company_id.internal_budget_rev_md_threshold')
    def _compute_totals(self):
        for san in self:
            net = sum(san.line_ids.mapped('variance_value'))
            abs_val = sum(abs(v) for v in san.line_ids.mapped('variance_value'))
            san.total_variance_value = net
            san.total_abs_variance_value = abs_val
            threshold = san.company_id.internal_budget_rev_md_threshold or 25000.0
            san.requires_md_approval = abs_val >= threshold

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New SAN')) == _('New SAN'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.stock.adjustment.note') or _('New SAN')
        return super().create(vals_list)

    def action_submit_investigation(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Please add counted stock items before submitting for investigation."))

        self.state = 'investigation'
        self.message_post(
            body=Markup(_("<strong>Physical Count Submitted:</strong> Discrepancies moved to investigation status.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_co_verify(self):
        """Records joint Site Engineer / Storekeeper count verification sign-off."""
        self.ensure_one()
        if not (
            self.env.su
            or self.env.user.has_group('contractor_core.group_contractor_site_engineer')
            or self.env.user.has_group('contractor_core.group_contractor_storekeeper')
            or self.env.user.has_group('contractor_core.group_contractor_project_manager')
            or self.env.user.has_group('contractor_core.group_contractor_manager')
        ):
            raise AccessError(_("Access Denied: Only Site Engineers, Storekeepers, or Managers can co-verify physical counts."))
        self.write({'site_engineer_verified_by': self.env.user.id})
        self.message_post(
            body=Markup(_("<strong>Joint Physical Count Verification:</strong> Co-verified by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_manager_approve(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize stock adjustments."))

        if self.requires_md_approval and not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_(
                "Executive Threshold Exceeded:\n\n"
                "Total absolute stock count discrepancy (%s %.2f) requires Managing Director authorization."
            ) % (self.currency_id.symbol or '', self.total_abs_variance_value))

        target_state = 'md_approved' if self.env.user.has_group('contractor_core.group_contractor_md') and self.requires_md_approval else 'manager_approved'
        self.write({
            'state': target_state,
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Stock Adjustment Approved:</strong> Authorized by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_md_approve(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Only the Managing Director can authorize this high-value stock adjustment."))

        self.write({
            'state': 'md_approved',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Executive Sign-Off Granted:</strong> Authorized by Managing Director %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_apply_adjustment(self):
        """Applies inventory quant adjustments in Odoo stock system."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_storekeeper')):
            raise AccessError(_("Access Denied: Only Storekeepers or Managers can apply inventory adjustments."))
        if self.state not in ('manager_approved', 'md_approved'):
            raise UserError(_("Adjustments can only be posted after manager or executive sign-off."))

        for line in self.line_ids:
            quant = self.env['stock.quant'].search([
                ('product_id', '=', line.product_id.id),
                ('location_id', '=', self.location_id.id),
            ], limit=1)

            if quant:
                quant.inventory_quantity = line.counted_qty
                quant.action_apply_inventory()
            else:
                new_quant = self.env['stock.quant'].create({
                    'product_id': line.product_id.id,
                    'location_id': self.location_id.id,
                    'inventory_quantity': line.counted_qty,
                })
                new_quant.action_apply_inventory()

        self.write({
            'state': 'adjusted',
        })
        if self.project_id:
            self.env['contractor.project.budget']._refresh_project_consumption(self.project_id.id)
        self.message_post(
            body=Markup(_("<strong>Stock Quantities Reconciled:</strong> Inventory adjustments successfully applied to store records.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_decline(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Manager can decline Stock Adjustment Notes."))
        return self.action_open_reason_wizard(action_type='decline', target_state='declined')

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        if any(rec.state == 'adjusted' for rec in self) and not set(vals.keys()).issubset(allowed_meta_fields):
            raise UserError(_("Adjusted Stock Adjustment Notes are locked and cannot be modified."))
        return super().write(vals)


class ContractorStockAdjustmentNoteLine(models.Model):
    _name = 'contractor.stock.adjustment.note.line'
    _description = 'Physical Stock Adjustment Line'

    san_id = fields.Many2one(
        'contractor.stock.adjustment.note',
        string='SAN Reference',
        required=True,
        ondelete='cascade',
    )
    product_id = fields.Many2one(
        'product.product',
        string='Product / SKU',
        required=True,
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='UoM',
        related='product_id.uom_id',
        readonly=True,
    )
    system_qty = fields.Float(
        string='System On-Hand Qty',
        compute='_compute_system_qty',
        store=True,
        readonly=False,
    )
    counted_qty = fields.Float(
        string='Physical Counted Qty',
        required=True,
        default=0.0,
    )
    variance_qty = fields.Float(
        string='Variance Qty',
        compute='_compute_variance',
        store=True,
    )
    unit_cost = fields.Monetary(
        string='Unit Cost',
        currency_field='currency_id',
        compute='_compute_default_cost',
        store=True,
        readonly=False,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='san_id.currency_id',
        readonly=True,
    )
    variance_value = fields.Monetary(
        string='Variance Value',
        currency_field='currency_id',
        compute='_compute_variance',
        store=True,
    )
    variance_reason = fields.Char(
        string='Explanation / Discrepancy Reason',
        help='Reason for variance (breakage, measurement rounding, theft, unrecorded receipt).',
    )

    @api.depends('product_id', 'san_id.location_id')
    def _compute_system_qty(self):
        for line in self:
            if line.product_id and line.san_id.location_id:
                quants = self.env['stock.quant'].search([
                    ('product_id', '=', line.product_id.id),
                    ('location_id', '=', line.san_id.location_id.id),
                ])
                line.system_qty = sum(quants.mapped('quantity'))
            else:
                line.system_qty = 0.0

    @api.depends('product_id')
    def _compute_default_cost(self):
        for line in self:
            if not line.unit_cost and line.product_id:
                line.unit_cost = line.product_id.standard_price or 0.0

    @api.depends('counted_qty', 'system_qty', 'unit_cost')
    def _compute_variance(self):
        for line in self:
            diff = (line.counted_qty or 0.0) - (line.system_qty or 0.0)
            line.variance_qty = diff
            line.variance_value = diff * (line.unit_cost or 0.0)

    def write(self, vals):
        if any(line.san_id.state == 'adjusted' for line in self):
            raise UserError(_("Lines of an adjusted Stock Adjustment Note cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if any(line.san_id.state == 'adjusted' for line in self):
            raise UserError(_("Lines of an adjusted Stock Adjustment Note cannot be deleted."))
        return super().unlink()

