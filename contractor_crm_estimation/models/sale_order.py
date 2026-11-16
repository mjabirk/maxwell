# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # Lead linkage
    lead_id = fields.Many2one(
        'crm.lead',
        string='Source Opportunity / Lead',
        index=True,
        ondelete='set null',
        help='CRM Lead from which this contracting quotation and cost estimate was generated.',
    )

    # Controlled Contractor Quotation Workflow Status
    contractor_quote_state = fields.Selection([
        ('draft', 'Draft Estimation'),
        ('submitted', 'Submitted for Manager Review'),
        ('approved_pending_release', 'Approved - Pending Sales Release'),
        ('sent_to_client', 'Sent to Client'),
        ('under_negotiation', 'Under Client Negotiation'),
        ('accepted', 'Customer Accepted'),
        ('converted', 'Converted to Active Project'),
        ('declined', 'Declined'),
        ('lost', 'Lost'),
        ('cancelled', 'Cancelled'),
    ], string='Contractor Quotation Status', default='draft', required=True, tracking=True, copy=False)

    # Revision Chain Tracking
    parent_quote_id = fields.Many2one(
        'sale.order',
        string='Previous Quotation Revision',
        readonly=True,
        copy=False,
        index=True,
        help='The previous/cancelled quotation from which this new revision was generated.',
    )
    revision_quote_ids = fields.One2many(
        'sale.order',
        'parent_quote_id',
        string='Subsequent Revisions',
        readonly=True,
    )
    revision_count = fields.Integer(
        string='Revisions',
        compute='_compute_revision_count',
    )

    # -------------------------------------------------------------------------
    # 4 Dedicated Cost Estimation Blocks
    # -------------------------------------------------------------------------
    est_labour_line_ids = fields.One2many(
        'contractor.cost.labour.line',
        'order_id',
        string='Block 1: Labor Cost Estimation',
        copy=True,
    )
    est_material_line_ids = fields.One2many(
        'contractor.cost.material.line',
        'order_id',
        string='Block 2: Material Cost Estimation',
        copy=True,
    )
    est_legal_line_ids = fields.One2many(
        'contractor.cost.legal.line',
        'order_id',
        string='Block 3: Legal, Permits & Statutory Fees',
        copy=True,
    )
    est_misc_line_ids = fields.One2many(
        'contractor.cost.misc.line',
        'order_id',
        string='Block 4: Miscellaneous & Site Indirects',
        copy=True,
    )

    # Consolidated Cost Totals
    total_labour_cost = fields.Monetary(
        string='Total Labor Cost (Block 1)',
        currency_field='currency_id',
        compute='_compute_estimation_totals',
        store=True,
    )
    total_material_cost = fields.Monetary(
        string='Total Material Cost (Block 2)',
        currency_field='currency_id',
        compute='_compute_estimation_totals',
        store=True,
    )
    total_legal_cost = fields.Monetary(
        string='Total Legal/Permit Cost (Block 3)',
        currency_field='currency_id',
        compute='_compute_estimation_totals',
        store=True,
    )
    total_misc_cost = fields.Monetary(
        string='Total Misc/Indirect Cost (Block 4)',
        currency_field='currency_id',
        compute='_compute_estimation_totals',
        store=True,
    )
    total_estimated_cost = fields.Monetary(
        string='Consolidated Direct & Indirect Cost',
        currency_field='currency_id',
        compute='_compute_estimation_totals',
        store=True,
    )

    # Margin Control & Pricing Calculation
    margin_type = fields.Selection([
        ('percentage', 'Percentage Margin (%)'),
        ('fixed', 'Fixed Monetary Margin'),
    ], string='Margin Mode', default='percentage', required=True)

    margin_percent = fields.Float(
        string='Margin (%)',
        default=15.0,
        digits=(16, 2),
    )
    margin_amount = fields.Monetary(
        string='Margin Amount',
        currency_field='currency_id',
    )
    effective_margin_pct = fields.Float(
        string='Effective Margin (%)',
        compute='_compute_margin_values',
        store=True,
        digits=(16, 2),
        help='Effective gross margin percentage relative to proposed quotation selling value.',
    )
    proposed_quotation_value = fields.Monetary(
        string='Proposed Quotation Value',
        currency_field='currency_id',
        compute='_compute_margin_values',
        store=True,
    )

    # Margin Override Governance
    margin_override_approved = fields.Boolean(
        string='Margin Override Approved',
        default=False,
        readonly=True,
        copy=False,
        tracking=True,
    )
    margin_override_reason = fields.Text(
        string='Margin Override Reason',
        readonly=True,
        copy=False,
    )
    margin_override_user_id = fields.Many2one(
        'res.users',
        string='Override Approved By',
        readonly=True,
        copy=False,
    )
    margin_override_date = fields.Datetime(
        string='Override Date',
        readonly=True,
        copy=False,
    )

    # Workflow Audit Dates & Actors
    original_engineer_id = fields.Many2one(
        'res.users',
        string='Original Estimating Engineer',
        readonly=True,
        copy=False,
        help='The estimating engineer who compiled and originally submitted the 4-block cost estimate.',
    )
    submitted_date = fields.Datetime(
        string='Submitted Date',
        readonly=True,
        copy=False,
    )
    approved_date = fields.Datetime(
        string='Manager Approval Date',
        readonly=True,
        copy=False,
        help='Timestamp of manager approval, initiating the 24-hour Sales Admin dispatch SLA timer.',
    )
    client_dispatch_datetime = fields.Datetime(
        string='Client Dispatch Timestamp',
        readonly=True,
        copy=False,
        help='Timestamp when Sales Administration formally dispatched the approved proposal to the client.',
    )

    # Customer Acceptance Gate & Project Conversion Fields
    acceptance_document_ids = fields.Many2many(
        'ir.attachment',
        'contractor_quote_acceptance_rel',
        'order_id',
        'attachment_id',
        string='Signed Acceptance Documents',
        copy=False,
        help='Mandatory client sign-off (Signed Proposal, LPO, or Contract) before project conversion or billing.',
    )
    acceptance_doc_type = fields.Selection([
        ('signed_quote', 'Signed Quotation / Proposal'),
        ('lpo', 'Client Local Purchase Order (LPO)'),
        ('contract', 'Formal Construction Agreement / Contract'),
        ('other', 'Other Binding Acceptance Document'),
    ], string='Acceptance Document Type', copy=False)
    acceptance_ref = fields.Char(
        string='Acceptance / LPO Reference',
        copy=False,
        help='Client purchase order number, contract reference, or agreement code.',
    )
    acceptance_date = fields.Date(
        string='Acceptance Date',
        default=fields.Date.context_today,
        copy=False,
    )
    project_budget_id = fields.Many2one(
        'contractor.project.budget',
        string='Baseline Project Budget',
        readonly=True,
        copy=False,
    )
    has_new_sku_cost_lines = fields.Boolean(
        string='Contains Non-Catalog New SKUs',
        compute='_compute_has_new_sku_cost_lines',
        store=True,
    )
    has_missing_new_sku_cost_basis = fields.Boolean(
        string='Missing New-SKU Cost Basis',
        compute='_compute_has_missing_new_sku_cost_basis',
    )
    min_required_margin_pct = fields.Float(
        string='Min Required Margin (%)',
        compute='_compute_ux_quotation_indicators',
    )
    margin_meets_minimum = fields.Boolean(
        string='Meets Minimum Margin',
        compute='_compute_ux_quotation_indicators',
    )
    is_approved_step = fields.Boolean(
        string='Quotation Approved Step',
        compute='_compute_ux_quotation_indicators',
    )
    is_dispatched_step = fields.Boolean(
        string='Dispatched to Client Step',
        compute='_compute_ux_quotation_indicators',
    )
    is_acceptance_attached_step = fields.Boolean(
        string='Acceptance Document Attached',
        compute='_compute_ux_quotation_indicators',
    )
    conversion_ready = fields.Boolean(
        string='Ready for Project Conversion',
        compute='_compute_ux_quotation_indicators',
    )
    non_conversion_reason = fields.Text(
        string='Non-Conversion Status Update Reason',
        readonly=True,
        copy=False,
        tracking=True,
    )
    non_conversion_updated_date = fields.Datetime(
        string='Last Non-Conversion Status Update',
        readonly=True,
        copy=False,
        tracking=True,
    )

    @api.depends(
        'est_material_line_ids.is_new_sku',
        'est_material_line_ids.requires_manager_cost_review',
    )
    def _compute_has_new_sku_cost_lines(self):
        for order in self:
            order.has_new_sku_cost_lines = any(
                line.is_new_sku and line.requires_manager_cost_review
                for line in order.est_material_line_ids
            )

    @api.depends(
        'est_material_line_ids.is_new_sku',
        'est_material_line_ids.cost_basis_reason',
    )
    def _compute_has_missing_new_sku_cost_basis(self):
        for order in self:
            order.has_missing_new_sku_cost_basis = any(
                line.is_new_sku and not (line.cost_basis_reason or '').strip()
                for line in order.est_material_line_ids
            )

    @api.depends(
        'company_id.min_quotation_margin_pct',
        'effective_margin_pct',
        'contractor_quote_state',
        'client_dispatch_datetime',
        'acceptance_document_ids',
    )
    def _compute_ux_quotation_indicators(self):
        approved_states = {
            'approved_pending_release', 'sent_to_client', 'under_negotiation', 'accepted', 'converted'
        }
        dispatched_states = {'sent_to_client', 'under_negotiation', 'accepted', 'converted'}
        for order in self:
            min_m = (order.company_id or self.env.company).min_quotation_margin_pct or 15.0
            order.min_required_margin_pct = min_m
            order.margin_meets_minimum = (order.effective_margin_pct or 0.0) >= min_m
            order.is_approved_step = order.contractor_quote_state in approved_states
            order.is_dispatched_step = bool(order.client_dispatch_datetime) or order.contractor_quote_state in dispatched_states
            order.is_acceptance_attached_step = bool(order.acceptance_document_ids)
            order.conversion_ready = bool(
                order.is_approved_step and order.is_dispatched_step and order.is_acceptance_attached_step
            )

    # -------------------------------------------------------------------------
    # Computations
    # -------------------------------------------------------------------------
    @api.depends(
        'est_labour_line_ids.subtotal_cost',
        'est_material_line_ids.subtotal_cost',
        'est_legal_line_ids.estimated_cost',
        'est_misc_line_ids.estimated_cost',
    )
    def _compute_estimation_totals(self):
        for order in self:
            labour = sum(order.est_labour_line_ids.mapped('subtotal_cost'))
            material = sum(order.est_material_line_ids.mapped('subtotal_cost'))
            legal = sum(order.est_legal_line_ids.mapped('estimated_cost'))
            misc = sum(order.est_misc_line_ids.mapped('estimated_cost'))

            order.total_labour_cost = labour
            order.total_material_cost = material
            order.total_legal_cost = legal
            order.total_misc_cost = misc
            order.total_estimated_cost = labour + material + legal + misc

    @api.depends('total_estimated_cost', 'margin_type', 'margin_percent', 'margin_amount')
    def _compute_margin_values(self):
        ctx = self.env.context
        allow_input_sync = ctx.get('from_negotiation_wizard') or ctx.get('from_revision_action')
        for order in self:
            cost = order.total_estimated_cost or 0.0
            is_locked = isinstance(order.id, int) and (
                order._is_post_conversion_locked() or order._is_post_approval_locked()
            )
            if order.margin_type == 'percentage':
                pct = order.margin_percent or 0.0
                m_amt = cost * (pct / 100.0)
                if (not is_locked or allow_input_sync) and abs((order.margin_amount or 0.0) - m_amt) > 1e-6:
                    order.margin_amount = m_amt
                proposed = cost + m_amt
                order.proposed_quotation_value = proposed
                order.effective_margin_pct = (m_amt / proposed * 100.0) if proposed > 0 else 0.0
            else:
                m_amt = order.margin_amount or 0.0
                proposed = cost + m_amt
                eff_pct = (m_amt / proposed * 100.0) if proposed > 0 else 0.0
                order.proposed_quotation_value = proposed
                order.effective_margin_pct = eff_pct
                if (not is_locked or allow_input_sync) and abs((order.margin_percent or 0.0) - eff_pct) > 1e-6:
                    order.margin_percent = eff_pct

    @api.onchange('margin_type', 'margin_percent', 'margin_amount', 'total_estimated_cost')
    def _onchange_margin_sync_lines(self):
        self._compute_margin_values()
        if self.proposed_quotation_value > 0:
            self._sync_order_lines_from_estimation()

    def _get_revision_family_ids(self):
        """Returns all sale.order IDs belonging to the same quotation revision tree."""
        self.ensure_one()
        if not self.id:
            return []
        root = self
        visited = {self.id}
        while root.parent_quote_id and root.parent_quote_id.id not in visited:
            visited.add(root.parent_quote_id.id)
            root = root.parent_quote_id
        all_ids = []
        seen = set()
        queue = [root]
        while queue:
            curr = queue.pop(0)
            if curr.id not in seen:
                seen.add(curr.id)
                all_ids.append(curr.id)
                queue.extend(curr.revision_quote_ids)
        return all_ids

    def _compute_revision_count(self):
        for order in self:
            family_ids = order._get_revision_family_ids() if order.id else []
            order.revision_count = max(0, len(family_ids) - 1)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get('skip_estimation_sync'):
            for rec in records:
                if rec.total_estimated_cost > 0:
                    rec.with_context(skip_estimation_sync=True)._sync_order_lines_from_estimation()
        return records

    revision_reason = fields.Text(
        string='Last Revision Reason',
        tracking=True,
        copy=False,
    )

    _COMMERCIAL_LOCKED_FIELDS = {
        'order_line',
        'est_labour_line_ids',
        'est_material_line_ids',
        'est_legal_line_ids',
        'est_misc_line_ids',
        'margin_type',
        'margin_percent',
        'margin_amount',
        'pricelist_id',
        'currency_id',
        'fiscal_position_id',
        'payment_term_id',
        'partner_id',
        'partner_invoice_id',
        'partner_shipping_id',
        'company_id',
    }

    def _is_post_conversion_locked(self):
        """Returns True once SO is confirmed/converted or linked to an execution project/budget."""
        self.ensure_one()
        if self.state in ('sale', 'done') or self.contractor_quote_state == 'converted' or self.project_budget_id:
            return True
        if getattr(self, 'project_ids', False):
            return True
        if isinstance(self.id, int) and self.id:
            if self.env['project.project'].sudo().search_count([('sale_order_id', '=', self.id)], limit=1):
                return True
        return False

    def _is_post_approval_locked(self):
        """Returns True after Manager Approval or closure, before project conversion."""
        self.ensure_one()
        return (
            self.contractor_quote_state in (
                'approved_pending_release', 'sent_to_client', 'accepted',
                'declined', 'lost', 'cancelled',
            )
            or self.state == 'cancel'
        )

    def _has_authorized_vo_context(self, lines=None):
        """Validates active Variation Order context during approved VO integration."""
        self.ensure_one()
        ctx = self.env.context
        if not ctx.get('from_vo_integration'):
            return False
        active_vo_id = ctx.get('active_vo_id')
        vo_model = self.env['contractor.variation.order'].sudo()
        if active_vo_id:
            vo = vo_model.browse(active_vo_id).exists()
            if not vo or vo.state not in ('manager_approved', 'md_approved', 'client_accepted', 'integrated'):
                return False
            matches_order = (vo.order_id.id == self.id) or (
                vo.project_id and (vo.project_id.id == self.project_id.id or vo.project_id.sale_order_id.id == self.id)
            )
            if not matches_order:
                return False
            if lines:
                if not vo.vo_sale_line_id or any(line.id != vo.vo_sale_line_id.id for line in lines):
                    return False
            return True
        return bool(vo_model.search_count([
            ('order_id', '=', self.id),
            ('state', 'in', ('manager_approved', 'md_approved', 'client_accepted', 'integrated')),
        ]))

    def _check_commercial_edit_lock(self, vals):
        changed_commercial = self._COMMERCIAL_LOCKED_FIELDS & set(vals.keys())
        if not changed_commercial:
            return
        ctx = self.env.context
        if ctx.get('from_convert_to_project'):
            return
        for rec in self:
            if rec._is_post_conversion_locked():
                if set(vals.keys()) <= {'order_line'} and rec._has_authorized_vo_context():
                    continue
                raise UserError(_(
                    "Sales Order %s is confirmed or linked to an active Project / Master Budget and is strictly locked. "
                    "Direct inline edits to lines, quantities, pricing, margins, or cost blocks are prohibited. "
                    "Use an authorized Variation Order (VO) for scope or commercial changes."
                ) % rec.display_name)
            if rec._is_post_approval_locked():
                if ctx.get('from_negotiation_wizard') or ctx.get('from_revision_action'):
                    continue
                if set(vals.keys()) <= {'order_line'} and rec._has_authorized_vo_context():
                    continue
                if rec.contractor_quote_state in ('declined', 'lost', 'cancelled') or rec.state == 'cancel':
                    raise UserError(_(
                        "Quotation %s is closed or cancelled. Direct edits are blocked; use 'Generate New Quotation (New Revision)' "
                        "to create a tracked revision."
                    ) % rec.display_name)
                raise UserError(_(
                    "Quotation %s is already approved/dispatched (%s). Direct edits to lines, pricing, margins, or cost blocks are blocked. "
                    "Use 'Revise Quotation (New Version)' or 'Log Client Negotiation' with a mandatory reason."
                ) % (rec.display_name, rec.contractor_quote_state))

    def write(self, vals):
        self._check_commercial_edit_lock(vals)
        res = super().write(vals)
        sync_triggers = {
            'est_labour_line_ids', 'est_material_line_ids',
            'est_legal_line_ids', 'est_misc_line_ids',
            'margin_type', 'margin_percent', 'margin_amount',
        }
        if not self.env.context.get('skip_estimation_sync') and (sync_triggers & set(vals.keys())):
            for rec in self:
                if rec.contractor_quote_state in ('draft', 'submitted', 'under_negotiation') and rec.total_estimated_cost > 0:
                    rec.with_context(skip_estimation_sync=True)._sync_order_lines_from_estimation()
        return res

    def unlink(self):
        for rec in self:
            if rec._is_post_conversion_locked():
                raise UserError(_(
                    "Cannot delete Sales Order %s because it is confirmed or linked to an active Project / Master Budget."
                ) % rec.display_name)
            if rec._is_post_approval_locked() or rec.contractor_quote_state == 'under_negotiation' or rec.version_history_ids:
                raise UserError(_(
                    "Cannot delete Quotation %s after approval, dispatch, negotiation, or version archival. "
                    "Cancel and issue a new revision instead."
                ) % rec.display_name)
        return super().unlink()

    def action_revise_quotation(self):
        """Opens Reason Wizard to capture a mandatory revision reason before reopening an approved/dispatched quotation."""
        self.ensure_one()
        if self._is_post_conversion_locked():
            raise UserError(_(
                "Sales Order %s is already confirmed/converted to an active Project and cannot be revised. "
                "Please use a formal Variation Order (VO)."
            ) % self.display_name)
        if self.contractor_quote_state not in ('approved_pending_release', 'sent_to_client', 'under_negotiation', 'accepted'):
            raise UserError(_(
                "Only Approved, Dispatched, Under Negotiation, or Accepted pre-conversion quotations can be formally revised."
            ))
        return self.action_open_reason_wizard(
            action_type='return',
            target_state=None,
            callback_method='_callback_revise_quotation',
        )

    def _callback_revise_quotation(self, reason=None):
        """Snapshots immutable version and reopens pre-conversion quotation to draft for formal re-estimation."""
        self.ensure_one()
        if self._is_post_conversion_locked():
            raise UserError(_(
                "Sales Order %s is already confirmed/converted to an active Project and cannot be revised."
            ) % self.display_name)
        clean_reason = (reason or self.env.context.get('revision_reason') or '').strip()
        if not clean_reason or len(clean_reason) < 5:
            raise UserError(_(
                "A mandatory revision reason (minimum 5 characters) is required to revise an approved/sent quotation."
            ))
        self._create_version_snapshot(
            reason=_("Quotation Formal Revision: %s") % clean_reason,
            editor_user=self.env.user,
        )
        self.with_context(from_revision_action=True).write({
            'contractor_quote_state': 'draft',
            'state': 'draft',
            'revision_reason': clean_reason,
            'last_action_reason': clean_reason,
            'last_action_by_id': self.env.user.id,
            'last_action_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_(
                "<p><strong>Quotation Formally Reopened for Revision (v%s):</strong></p>"
                "<p><strong>Reason:</strong> %s</p>"
            )) % (self.version_number, clean_reason),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_view_revisions(self):
        self.ensure_one()
        family_ids = self._get_revision_family_ids()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Quotation Revisions'),
            'res_model': 'sale.order',
            'view_mode': 'list,form',
            'domain': [('id', 'in', family_ids)],
        }

    def _sync_order_lines_from_estimation(self):
        """Ensures sales order line reflects the total proposed quotation value."""
        for order in self:
            if order._is_post_conversion_locked() and not self.env.context.get('from_convert_to_project'):
                continue
            if order.proposed_quotation_value <= 0:
                continue
            if not order.order_line:
                product = self.env['product.product'].search([('type', '=', 'service')], limit=1)
                if not product:
                    product = self.env['product.product'].search([], limit=1)
                order.order_line = [(0, 0, {
                    'product_id': product.id,
                    'name': (order.lead_id.name or order.name) + " - Contracting Turnkey Scope",
                    'product_uom_qty': 1.0,
                    'price_unit': order.proposed_quotation_value,
                })]
            else:
                first_line = order.order_line[0]
                if first_line.product_uom_qty != 1.0:
                    first_line.product_uom_qty = 1.0
                first_line.price_unit = order.proposed_quotation_value

    # -------------------------------------------------------------------------
    # Submission Hard-Stop Gate
    # -------------------------------------------------------------------------
    def action_submit_for_approval(self):
        """Hard-Stop Submission: Blocks submission unless Labour > 0, Material > 0,
        payment terms are set, margin is positive, fresh cost reviews are resolved,
        and margin threshold compliance is validated.
        """
        self.ensure_one()

        if self.total_labour_cost <= 0:
            raise UserError(_("Cost Estimation Hard-Stop: Block 1 (Labor Cost) must be greater than zero."))
        if self.total_material_cost <= 0:
            raise UserError(_("Cost Estimation Hard-Stop: Block 2 (Material Cost) must be greater than zero."))
        if not self.payment_term_id:
            raise UserError(_("Commercial Governance Hard-Stop: Payment Terms must be specified before submission."))
        if self.proposed_quotation_value <= self.total_estimated_cost:
            raise UserError(_("Pricing Hard-Stop: Proposed quotation value must exceed consolidated estimated cost."))

        if self.lead_id and self.lead_id.requires_fresh_cost_review:
            raise UserError(_(
                "Fresh Cost Review Hard-Stop:\n\n"
                "The source lead was previously lost or placed on hold.\n"
                "You must re-verify labor and material rates on the lead and clear the "
                "'Requires Fresh Cost Review' flag before submitting."
            ))

        # Margin Threshold Compliance Check
        min_margin = self.company_id.min_quotation_margin_pct or 15.0
        if self.effective_margin_pct < min_margin:
            if not self.margin_override_approved:
                raise UserError(_(
                    "Margin Governance Hard-Stop:\n\n"
                    "The effective quotation margin (%.2f%%) is below the company minimum threshold (%.2f%%).\n"
                    "Submission is blocked until a Contractor Manager approves a Margin Override."
                ) % (self.effective_margin_pct, min_margin))

        # Assign original engineer if not set
        if not self.original_engineer_id:
            self.original_engineer_id = self.env.user

        self.submitted_date = fields.Datetime.now()
        self._sync_order_lines_from_estimation()

        # Capture initial snapshot (v1)
        version_rec = self._create_version_snapshot(
            reason=_("Initial Engineer Cost Estimate Submission (Labor: %s, Material: %s, Total: %s)") % (
                self.total_labour_cost, self.total_material_cost, self.proposed_quotation_value
            ),
            editor_user=self.env.user,
        )

        self.write({'contractor_quote_state': 'submitted'})
        if self.lead_id:
            self.lead_id.contractor_state = 'quoted'

        # Notify Managers
        mgr_group = self.env.ref('contractor_core.group_contractor_manager', raise_if_not_found=False)
        if mgr_group:
            for mgr in (mgr_group.all_user_ids | mgr_group.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('sale.order'),
                    'res_id': self.id,
                    'user_id': mgr.id,
                    'summary': f"Review Cost Estimate: {self.name} ({self.partner_id.name})",
                    'note': Markup(_("Quotation <b>%s</b> submitted for review. Margin: %.2f%% | Proposed: %s.")) % (
                        self.name, self.effective_margin_pct, self.proposed_quotation_value
                    ),
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_("<strong>Cost Estimate Submitted:</strong> Submitted by %s for Manager Review. Version v%s archived.")) % (
                self.env.user.name, version_rec.version_number
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_approve_margin_override(self):
        """Allows a Contractor Manager to authorize a sub-threshold margin."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can approve margin overrides."))

        return self.action_open_reason_wizard(
            action_type='override',
            callback_method='_callback_margin_override',
        )

    def _callback_margin_override(self, reason=None):
        self.write({
            'margin_override_approved': True,
            'margin_override_reason': reason or self.last_action_reason,
            'margin_override_user_id': self.env.user.id,
            'margin_override_date': fields.Datetime.now(),
        })

    # -------------------------------------------------------------------------
    # Manager 3-Option Review Actions
    # -------------------------------------------------------------------------
    def action_manager_approve(self):
        """Option 1: Approves quotation as-is, moving to approved_pending_release
        and starting the 24h Sales Admin dispatch timer.
        """
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only Contractor Managers can approve quotations."))

        now = fields.Datetime.now()
        self.write({
            'contractor_quote_state': 'approved_pending_release',
            'approved_date': now,
        })

        # Notify Sales Admins
        sales_admin_group = self.env.ref('contractor_core.group_contractor_sales_admin', raise_if_not_found=False)
        if sales_admin_group:
            for admin in (sales_admin_group.all_user_ids | sales_admin_group.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('sale.order'),
                    'res_id': self.id,
                    'user_id': admin.id,
                    'summary': f"Sales Release SLA (24h): Dispatch Quote {self.name}",
                    'note': Markup(_("Quotation <b>%s</b> has been approved. Please perform commercial check and dispatch to client.")) % self.name,
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_("<strong>Manager Approval Granted:</strong> Approved by %s. 24h Sales Release Timer activated.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_manager_return(self):
        """Option 2: Returns quotation to Engineer with mandatory justification."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only Contractor Managers can return quotations."))

        return self.action_open_reason_wizard(
            action_type='return',
            target_state='draft',
            callback_method='_callback_manager_return',
        )

    def _callback_manager_return(self, reason=None):
        clean_reason = reason or self.last_action_reason or _('Returned to Draft by Contractor Manager')
        for order in self:
            order._ensure_snapshot_archived(clean_reason, move_attachments=True)

    def action_manager_edit_and_approve(self):
        """Option 3: Archives engineer's v1 snapshot, creates v2 with manager edits,
        and moves to approved_pending_release.
        """
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only Contractor Managers can edit and approve quotations."))

        # Archive version snapshot of manager's revision
        self._create_version_snapshot(
            reason=_("Manager Revision & Approval (Editor: %s)") % self.env.user.name,
            editor_user=self.env.user,
            move_attachments=True,
        )

        return self.action_manager_approve()

    # -------------------------------------------------------------------------
    # Sales Admin Dispatch Gate
    # -------------------------------------------------------------------------
    def action_confirm_client_dispatch(self):
        """Sales Admin Dispatch Gate: Formally releases quote to client, logging dispatch timestamp."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_sales_admin') and not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only Sales Administrators can execute client quotation dispatch."))

        if self.contractor_quote_state != 'approved_pending_release':
            raise UserError(_("Quotation must be in 'Approved - Pending Sales Release' status to dispatch to client."))

        now = fields.Datetime.now()
        self.write({
            'contractor_quote_state': 'sent_to_client',
            'client_dispatch_datetime': now,
            'state': 'sent',
        })
        if self.lead_id:
            self.lead_id.contractor_state = 'sent_to_client'

        self.message_post(
            body=Markup(_("<strong>Client Dispatch Confirmed:</strong> Quote released to client by %s at %s.")) % (self.env.user.name, now),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_sales_admin_release(self):
        """Alias for action_confirm_client_dispatch."""
        return self.action_confirm_client_dispatch()

    def action_quotation_send(self):
        """Blocks standard email sending while in draft or submitted."""
        for order in self:
            if order.contractor_quote_state in ('draft', 'submitted'):
                raise UserError(_(
                    "Dispatch Blocked: Quotation '%s' cannot be emailed while in '%s' status.\n"
                    "It must be reviewed by Contractor Management and formally released by Sales Administration."
                ) % (order.name, order.contractor_quote_state))
        return super().action_quotation_send()

    # -------------------------------------------------------------------------
    # Client Negotiation Gate
    # -------------------------------------------------------------------------
    def action_log_client_negotiation(self):
        """Opens negotiation wizard to capture client requested modifications."""
        self.ensure_one()
        if self._is_post_conversion_locked():
            raise UserError(_(
                "Sales Order %s is already confirmed or linked to an active Project / Master Budget. "
                "Pre-conversion negotiation revisions are locked; please create a formal Variation Order (VO)."
            ) % self.display_name)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Log Client Negotiation'),
            'res_model': 'contractor.negotiation.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_order_id': self.id,
            },
        }

    def action_log_non_conversion_status(self):
        """Sales Admin non-conversion status update capturing a mandatory justification without cancelling."""
        self.ensure_one()
        return self.action_open_reason_wizard(
            action_type='other',
            callback_method='_callback_non_conversion_status',
        )

    def _callback_non_conversion_status(self, reason=None):
        clean_reason = reason or self.last_action_reason or _('Non-conversion status reviewed and updated.')
        now = fields.Datetime.now()
        for order in self:
            order.write({
                'non_conversion_reason': clean_reason,
                'non_conversion_updated_date': now,
            })
            # Mark open non-conversion benchmark activities on this order as done
            model_id = self.env['ir.model']._get_id('sale.order')
            open_acts = self.env['mail.activity'].search([
                ('res_model_id', '=', model_id),
                ('res_id', '=', order.id),
                ('summary', 'ilike', 'NON-CONVERSION BENCHMARK'),
            ])
            if open_acts:
                open_acts.action_feedback(feedback=clean_reason)
            order.message_post(
                body=Markup(_(
                    "<strong>Non-Conversion Status Updated:</strong> Updated by %s on %s.<br/>"
                    "<strong>Reason:</strong> %s"
                )) % (self.env.user.name, now, clean_reason),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )

    # -------------------------------------------------------------------------
    # Cancellation, Lost/Declined Governance & Quotation Revisions
    # -------------------------------------------------------------------------
    def _ensure_snapshot_archived(self, reason_text, move_attachments=False):
        """Archives a version snapshot if the current state has not been snapshotted yet,
        and moves/links attachments to that version snapshot.
        """
        self.ensure_one()
        VersionDoc = self.env['contractor.document.version'].sudo()
        latest = VersionDoc.search([
            ('res_model', '=', 'sale.order'),
            ('res_id', '=', self.id),
        ], order='version_number desc, id desc', limit=1)
        if not latest or latest.approval_state != self.contractor_quote_state:
            latest = self._create_version_snapshot(
                reason=reason_text,
                editor_user=self.env.user,
                move_attachments=move_attachments,
            )
        else:
            self._move_attachments_to_version(latest, clear_from_record=move_attachments)
        return latest

    def _finalize_closed_quotation(self, target_quote_state, default_reason, reason=None):
        """Shared transition handler for cancelling, declining, or marking a quotation lost."""
        clean_reason = reason or self.env.context.get('cancel_reason') or default_reason
        for order in self:
            order.write({'contractor_quote_state': target_quote_state})
            order._ensure_snapshot_archived(clean_reason, move_attachments=True)
        res = super(SaleOrder, self.with_context(
            disable_cancel_warning=True,
            skip_cancel_reason_wizard=True,
            contractor_target_quote_state=target_quote_state,
        ))._action_cancel()
        self.write({
            'state': 'cancel',
            'contractor_quote_state': target_quote_state,
        })
        return res

    def action_cancel(self):
        """Mandates a documented reason when cancelling a contracting quotation and syncs contractor_quote_state."""
        if any(order.locked for order in self):
            raise UserError(_("You cannot cancel a locked order. Please unlock it first."))
        if any(order.contractor_quote_state == 'converted' for order in self):
            raise UserError(_(
                "Cancellation Blocked:\n\n"
                "This quotation has already been converted to an Active Project and Master Budget.\n"
                "Converted project quotations cannot be cancelled directly."
            ))
        if len(self) == 1 and not self.env.context.get('skip_cancel_reason_wizard'):
            return self.action_open_reason_wizard(
                action_type='cancel',
                target_state='cancelled',
                callback_method='_callback_cancel_quotation',
            )
        return self._callback_cancel_quotation(reason=self.env.context.get('cancel_reason'))

    def _callback_cancel_quotation(self, reason=None):
        return self._finalize_closed_quotation('cancelled', _('Quotation Cancelled'), reason=reason)

    def _action_cancel(self):
        res = super()._action_cancel()
        target_q_state = self.env.context.get('contractor_target_quote_state', 'cancelled')
        self.filtered(
            lambda o: o.contractor_quote_state not in ('declined', 'lost', 'cancelled')
        ).write({'contractor_quote_state': target_q_state})
        return res

    def action_set_quote_lost(self):
        """Marks an active quotation as Lost after capturing a mandatory reason."""
        self.ensure_one()
        return self.action_open_reason_wizard(
            action_type='decline',
            target_state='lost',
            callback_method='_callback_mark_quote_lost',
        )

    def _callback_mark_quote_lost(self, reason=None):
        return self._finalize_closed_quotation('lost', _('Quotation Marked as Lost'), reason=reason)

    def action_set_quote_declined(self):
        """Marks an active quotation as Declined after capturing a mandatory reason."""
        self.ensure_one()
        return self.action_open_reason_wizard(
            action_type='decline',
            target_state='declined',
            callback_method='_callback_mark_quote_declined',
        )

    def _callback_mark_quote_declined(self, reason=None):
        return self._finalize_closed_quotation('declined', _('Quotation Declined'), reason=reason)

    def action_draft(self):
        """Resets a cancelled/declined/lost quotation back to Draft Estimation as the next version."""
        for order in self:
            order._ensure_snapshot_archived(
                order.last_action_reason or _('Archived prior to resetting quotation to Draft'),
                move_attachments=True,
            )
        res = super().action_draft()
        for order in self:
            new_v = max(order.version_number or 1, 2)
            order.write({
                'contractor_quote_state': 'draft',
                'version_number': new_v,
            })
            order.message_post(
                body=Markup(_(
                    "<strong>Quotation Reset to Draft (v%s):</strong> Reopened by %s."
                )) % (new_v, self.env.user.name),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
        return res

    def action_create_new_revision(self):
        """Generates a brand-new Draft Estimation quotation linked as a new revision of this quotation."""
        self.ensure_one()
        self._ensure_snapshot_archived(
            self.last_action_reason or _('Archived prior to generating new quotation revision'),
            move_attachments=True,
        )
        new_version = max(self.version_number or 1, 2)
        default_vals = {
            'parent_quote_id': self.id,
            'version_number': new_version,
            'origin': f"{self.name} (Rev {new_version})",
            'contractor_quote_state': 'draft',
            'state': 'draft',
            'lead_id': self.lead_id.id if self.lead_id else False,
            'project_id': False,
            'project_budget_id': False,
            'margin_override_approved': False,
            'margin_override_reason': False,
            'margin_override_user_id': False,
            'margin_override_date': False,
            'original_engineer_id': self.env.user.id,
            'acceptance_document_ids': [(5, 0, 0)],
            'acceptance_doc_type': False,
            'acceptance_ref': False,
            'acceptance_date': False,
        }
        new_quote = self.with_context(skip_estimation_sync=True).copy(default=default_vals)
        stray_atts = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'sale.order'),
            ('res_id', '=', new_quote.id),
            ('res_field', '=', False),
        ])
        if stray_atts:
            stray_atts.unlink()
        if new_quote.total_estimated_cost > 0:
            new_quote.with_context(skip_estimation_sync=True)._sync_order_lines_from_estimation()

        if self.lead_id and self.lead_id.contractor_state in ('lost', 'declined', 'won', 'sent_to_client', 'negotiation'):
            self.lead_id.contractor_state = 'quoted'

        self.message_post(
            body=Markup(_(
                "<strong>New Quotation Revision Generated:</strong> Created revision <a href='#' data-oe-model='sale.order' data-oe-id='%d'><b>%s</b></a> (v%s) by %s."
            )) % (new_quote.id, new_quote.name, new_version, self.env.user.name),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        new_quote.message_post(
            body=Markup(_(
                "<strong>Revision v%s Initialized:</strong> Generated from quotation <a href='#' data-oe-model='sale.order' data-oe-id='%d'><b>%s</b></a> by %s.<br/>"
                "All 4 cost estimation blocks and commercial margin settings have been carried forward."
            )) % (new_version, self.id, self.name, self.env.user.name),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

        return {
            'type': 'ir.actions.act_window',
            'name': _('Quotation Revision %s (v%s)') % (new_quote.name, new_version),
            'res_model': 'sale.order',
            'res_id': new_quote.id,
            'views': [(False, 'form')],
            'view_mode': 'form',
            'target': 'current',
        }

    # -------------------------------------------------------------------------
    # Customer Acceptance Gate & Auto-Project / Budget Conversion
    # -------------------------------------------------------------------------
    def action_customer_acceptance(self):
        """Marks quotation as accepted once client signed documents are attached."""
        self.ensure_one()
        if not self.acceptance_document_ids:
            raise UserError(_(
                "Customer Acceptance Gate:\n\n"
                "You must upload client signed acceptance documents (Signed Quote, LPO, or Contract) "
                "before marking this quotation as Accepted."
            ))
        if not self.acceptance_doc_type:
            raise UserError(_("Please select the Acceptance Document Type (e.g. Signed Quote, LPO, Contract)."))

        self.write({'contractor_quote_state': 'accepted'})
        if self.lead_id:
            self.lead_id.contractor_state = 'won'

        self.message_post(
            body=Markup(_(
                "<strong>Customer Acceptance Verified:</strong> Doc Type: %s | Ref: %s.<br/>"
                "Attachments: %d documents verified."
            )) % (self.acceptance_doc_type, self.acceptance_ref or 'N/A', len(self.acceptance_document_ids)),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_convert_to_project(self):
        """Customer Acceptance Gate & Project Conversion:
        Validates client acceptance attachments, auto-generates a unique project code,
        creates project.project and initial contractor.project.budget carrying forward
        the 4 estimation blocks as baseline budget lines.
        """
        self.ensure_one()

        # Hard-Stop Acceptance Gate
        if not self.acceptance_document_ids:
            raise UserError(_(
                "Customer Acceptance Gate Hard-Stop:\n\n"
                "Project conversion is strictly blocked until client acceptance documents "
                "(Signed Quote, LPO, or Contract) are uploaded."
            ))

        # 0. Ensure sales order lines are synchronized with 4-block proposed quotation value
        if self.total_estimated_cost > 0:
            self.with_context(skip_estimation_sync=True, from_convert_to_project=True)._sync_order_lines_from_estimation()

        # 1. Generate unique project code
        project_code = self.env['ir.sequence'].next_by_code('contractor.project.code') or f"PRJ-{self.name}"

        # 2. Create Project
        project_vals = {
            'name': f"{self.name} - {self.partner_id.name}",
            'project_code': project_code,
            'partner_id': self.partner_id.id,
            'company_id': self.company_id.id,
            'sale_order_id': self.id,
            'baseline_contract_value': self.proposed_quotation_value or self.amount_untaxed,
        }
        project = self.env['project.project'].create(project_vals)
        project._get_or_create_project_analytic_account()

        # 3. Create Initial Project Budget Carrying Forward 4 Estimation Blocks
        budget_vals = {
            'name': f"Baseline Budget - {project.name}",
            'project_id': project.id,
            'sale_order_id': self.id,
            'contract_amount': self.proposed_quotation_value or self.amount_untaxed,
            'baseline_cost': self.total_estimated_cost,
            'line_ids': [],
        }

        # Block 1 -> Budget Lines
        for line in self.est_labour_line_ids:
            budget_vals['line_ids'].append((0, 0, {
                'block_type': 'labour',
                'name': f"Labor: {line.trade} - {line.role_designation or ''} ({line.worker_category})",
                'quantity': line.estimated_hours if line.calculation_mode == 'hours_rate' else 1.0,
                'unit_cost': line.hourly_cost_rate if line.calculation_mode == 'hours_rate' else line.direct_amount,
                'planned_amount': line.subtotal_cost,
                'notes': f"Mode: {line.calculation_mode}",
            }))

        # Block 2 -> Budget Lines
        for line in self.est_material_line_ids:
            budget_vals['line_ids'].append((0, 0, {
                'block_type': 'material',
                'name': f"Material: {line.item_description}",
                'product_id': line.product_id.id if line.product_id else False,
                'quantity': line.quantity,
                'unit_cost': line.effective_unit_cost,
                'planned_amount': line.subtotal_cost,
                'notes': f"SKU: {'New Non-Catalog' if line.is_new_sku else 'Catalog'} | Basis: {line.cost_basis_reason or ''}",
            }))

        # Block 3 -> Budget Lines
        for line in self.est_legal_line_ids:
            budget_vals['line_ids'].append((0, 0, {
                'block_type': 'legal',
                'name': f"Permits/Legal: {line.description} ({line.fee_category})",
                'quantity': 1.0,
                'unit_cost': line.estimated_cost,
                'planned_amount': line.estimated_cost,
                'notes': f"Authority: {line.issuing_authority or 'N/A'}",
            }))

        # Block 4 -> Budget Lines
        for line in self.est_misc_line_ids:
            budget_vals['line_ids'].append((0, 0, {
                'block_type': 'misc',
                'name': f"Misc/Indirect: {line.description} ({line.misc_category})",
                'quantity': 1.0,
                'unit_cost': line.estimated_cost,
                'planned_amount': line.estimated_cost,
            }))

        budget = self.env['contractor.project.budget'].create(budget_vals)

        # 4. Link & Update Sale Order, and confirm via standard Odoo Sales workflow
        self.with_context(from_convert_to_project=True).write({
            'project_id': project.id,
            'project_budget_id': budget.id,
            'contractor_quote_state': 'converted',
        })
        if self.state in ('draft', 'sent'):
            super(SaleOrder, self.with_context(from_convert_to_project=True)).action_confirm()
        elif self.state != 'sale':
            self.write({'state': 'sale'})
        if self.lead_id:
            self.lead_id.contractor_state = 'won'

        # Post chatter conversion notes
        self.message_post(
            body=Markup(_(
                "<strong>Project & Budget Converted Successfully:</strong><br/>"
                "• Project: <b>%s</b> (Code: <code>%s</code>)<br/>"
                "• Baseline Budget: <b>%s</b> carrying forward 4 estimation blocks (%d lines).<br/>"
                "• Contract Value: %s | Estimated Cost: %s."
            )) % (
                project.name,
                project.project_code,
                budget.name,
                len(budget.line_ids),
                self.proposed_quotation_value,
                self.total_estimated_cost,
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

        return {
            'type': 'ir.actions.act_window',
            'name': _('Active Project'),
            'res_model': 'project.project',
            'res_id': project.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_confirm(self):
        """Hard-Stop Commercial Gate:
        Prevents confirmation of sale order until:
        1. Quote is approved and client signed acceptance documents (Signed Quote, LPO, or Contract) are uploaded.
        2. Status is 'accepted' or 'converted'.
        If invoked on an accepted quotation before project conversion, delegates to action_convert_to_project().
        """
        if self.env.context.get('from_convert_to_project'):
            return super().action_confirm()
        if len(self) == 1 and self.contractor_quote_state == 'accepted' and not self.project_budget_id:
            return self.action_convert_to_project()
        for order in self:
            if not order.acceptance_document_ids:
                raise UserError(_(
                    "Commercial Governance Gate Hard-Stop:\n\n"
                    "Confirmation of Quotation '%s' is strictly blocked until client signed acceptance "
                    "documents (Signed Quotation, LPO, or Contract) are uploaded in the Acceptance Documents tab."
                ) % order.name)
            if order.contractor_quote_state not in ('accepted', 'converted'):
                raise UserError(_(
                    "Commercial Governance Gate Hard-Stop:\n\n"
                    "Quotation '%s' cannot be confirmed while in '%s' status.\n"
                    "It must be reviewed, approved, dispatched, and formally marked as 'Accepted' "
                    "with client acceptance documents attached."
                ) % (order.name, order.contractor_quote_state))
        return super().action_confirm()

    def _create_invoices(self, grouped=False, final=False, date=None):
        """Hard-Stop Invoicing Gate: Prevents invoice creation without client acceptance documents."""
        for order in self:
            if not order.acceptance_document_ids:
                raise UserError(_(
                    "Customer Acceptance Gate Hard-Stop:\n\n"
                    "Customer invoice creation is strictly prohibited for Quotation '%s'.\n"
                    "Contractor governance mandates verified client acceptance documents (Signed Quote, LPO, or Contract) "
                    "before generating customer invoices."
                ) % order.name)
        return super()._create_invoices(grouped=grouped, final=final, date=date)

