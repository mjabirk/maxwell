# -*- coding: utf-8 -*-
from datetime import timedelta
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorMaterialRequisition(models.Model):
    _name = 'contractor.material.requisition'
    _description = 'Contractor Material Requisition (MR)'
    _inherit = [
        'mail.thread',
        'mail.activity.mixin',
        'contractor.version.mixin',
        'contractor.reason.mixin',
        'contractor.state.lock.mixin',
        'contractor.no.delete.mixin',
    ]
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='MR Number',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New MR'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
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
    requested_by = fields.Many2one(
        'res.users',
        string='Requested By',
        default=lambda self: self.env.user,
        readonly=True,
        tracking=True,
    )
    required_date = fields.Date(
        string='Required on Site Date',
        required=True,
        tracking=True,
        default=lambda self: fields.Date.context_today(self) + timedelta(days=7),
    )
    purpose = fields.Text(
        string='Material Usage Purpose / Milestone',
        required=True,
        tracking=True,
        help='State the specific project milestone, floor, or task where these materials will be deployed.',
    )
    stock_checked = fields.Boolean(
        string='Surplus & Central Stock Inspected',
        default=False,
        tracking=True,
        help='Confirmation that the site engineer has reviewed central and inter-project surplus stock prior to requesting external purchase.',
    )
    state = fields.Selection([
        ('draft', 'Draft Requisition'),
        ('submitted', 'Submitted for Approval'),
        ('approved', 'Approved by Manager'),
        ('declined', 'Declined'),
        ('cs_in_progress', 'Comparative Statement In Progress'),
        ('po_created', 'PO Awarded / Ordered'),
        ('done', 'Fully Received & Closed'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    submitted_datetime = fields.Datetime(
        string='Submission Datetime',
        readonly=True,
        copy=False,
    )
    escalated_to_md = fields.Boolean(
        string='Escalated to Managing Director (24h SLA)',
        default=False,
        readonly=True,
        tracking=True,
        copy=False,
    )
    escalation_datetime = fields.Datetime(
        string='Escalation Timestamp',
        readonly=True,
        copy=False,
    )
    approver_id = fields.Many2one(
        'res.users',
        string='Approved By',
        readonly=True,
        copy=False,
    )
    approval_datetime = fields.Datetime(
        string='Approval Datetime',
        readonly=True,
        copy=False,
    )

    line_ids = fields.One2many(
        'contractor.material.requisition.line',
        'requisition_id',
        string='Requested Items',
        copy=True,
    )
    total_estimated_amount = fields.Monetary(
        string='Total Estimated Requisition Value',
        currency_field='currency_id',
        compute='_compute_total_estimated_amount',
        store=True,
    )

    comparative_statement_ids = fields.One2many(
        'contractor.comparative.statement',
        'requisition_id',
        string='Comparative Statements',
    )
    comparative_statement_count = fields.Integer(
        string='CS Count',
        compute='_compute_related_counts',
    )
    purchase_order_ids = fields.One2many(
        'purchase.order',
        'requisition_id',
        string='Purchase Orders',
    )
    purchase_order_count = fields.Integer(
        string='PO Count',
        compute='_compute_related_counts',
    )

    # Budget Health Indicators
    remaining_material_budget = fields.Monetary(
        string='Project Remaining Material Budget',
        currency_field='currency_id',
        compute='_compute_budget_health',
    )
    budget_healthy = fields.Boolean(
        string='Within Allowable Budget',
        compute='_compute_budget_health',
    )
    supporting_attachment_ids = fields.Many2many(
        'ir.attachment',
        'contractor_mr_supporting_att_rel',
        'mr_id',
        'attachment_id',
        string='Supporting Documents / Specs',
        help='Technical specifications, vendor catalog cuts, or site engineer sketches supporting this requisition.',
    )
    is_below_rfq_threshold = fields.Boolean(
        string='Below 3-Vendor RFQ Threshold',
        compute='_compute_is_below_rfq_threshold',
    )
    preferred_vendor_id = fields.Many2one(
        'res.partner',
        string='Direct PO Vendor (Below Threshold)',
        domain="[('supplier_rank', '>=', 0), ('is_company', '=', True)]",
        help='Optional vendor for creating a direct single-vendor PO when MR value is below the 3-Vendor RFQ threshold.',
    )

    @api.depends('total_estimated_amount', 'company_id.rfq_three_vendor_threshold')
    def _compute_is_below_rfq_threshold(self):
        for mr in self:
            threshold = (mr.company_id or self.env.company).rfq_three_vendor_threshold or 10000.0
            mr.is_below_rfq_threshold = (mr.total_estimated_amount or 0.0) < threshold

    @api.depends('line_ids.estimated_subtotal')
    def _compute_total_estimated_amount(self):
        for mr in self:
            mr.total_estimated_amount = sum(mr.line_ids.mapped('estimated_subtotal'))

    @api.depends('comparative_statement_ids', 'purchase_order_ids')
    def _compute_related_counts(self):
        for mr in self:
            mr.comparative_statement_count = len(mr.comparative_statement_ids)
            mr.purchase_order_count = len(mr.purchase_order_ids)

    @api.depends('project_id', 'total_estimated_amount')
    def _compute_budget_health(self):
        for mr in self:
            if not mr.project_id:
                mr.remaining_material_budget = 0.0
                mr.budget_healthy = True
                continue

            budget = self.env['contractor.project.budget'].search([
                ('project_id', '=', mr.project_id.id),
                ('is_active', '=', True),
            ], limit=1)

            if budget:
                budget.line_ids.sudo()._compute_line_consumption()
                mat_lines = budget.line_ids.filtered(lambda l: l.block_type == 'material')
                rem = sum(mat_lines.mapped('remaining_budget'))
                mr.remaining_material_budget = rem
                mr.budget_healthy = mr.total_estimated_amount <= rem or budget.override_approved
            else:
                mr.remaining_material_budget = 0.0
                mr.budget_healthy = False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New MR')) == _('New MR'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.material.requisition') or _('New MR')
        return super().create(vals_list)

    def action_submit(self):
        """Validates stock inspection gate and budget availability before submitting."""
        self.ensure_one()
        self._ensure_state('draft', _("submit material requisition"))
        if not self.line_ids:
            raise UserError(_("Please add at least one material line to this requisition."))

        for line in self.line_ids:
            if line.qty <= 0:
                raise UserError(_("Requested quantity must be strictly greater than zero for product '%s'.") % line.product_id.display_name)

        # Mandatory stock verification hard-stop
        if not self.stock_checked:
            raise UserError(_(
                "Procurement Governance Hard-Stop:\n\n"
                "You must verify and check 'Surplus & Central Stock Inspected' before submitting this requisition.\n"
                "Central stores and inter-project transfers must be utilized where surplus stock exists."
            ))

        # Project Budget Hard-Stop
        self.env['contractor.project.budget'].check_budget_availability(
            project_id=self.project_id.id,
            cost_head='material',
            requested_amount=self.total_estimated_amount,
        )

        self.with_context(bypass_state_lock=True).write({
            'state': 'submitted',
            'submitted_datetime': fields.Datetime.now(),
        })

        # Activity to Contractor Manager
        group_mgr = self.env.ref('contractor_core.group_contractor_manager', raise_if_not_found=False)
        if group_mgr:
            for user in (group_mgr.all_user_ids | group_mgr.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('contractor.material.requisition'),
                    'res_id': self.id,
                    'user_id': user.id,
                    'summary': f"Approve Material Requisition {self.name} for {self.project_id.name}",
                    'note': _("Requisition of %s items valued at %s %.2f requires review.") % (
                        len(self.line_ids), self.currency_id.symbol or '', self.total_estimated_amount
                    ),
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_("<strong>Requisition Submitted:</strong> Forwarded to Contractor Manager for procurement approval.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_manager_approve(self):
        """Contractor Manager authorizes the requisition for comparative RFQ / PO issuance."""
        self.ensure_one()
        self._ensure_state(('draft', 'submitted'), _("approve material requisition"))
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can approve Material Requisitions."))

        self.with_context(bypass_state_lock=True).write({
            'state': 'approved',
            'approver_id': self.env.user.id,
            'approval_datetime': fields.Datetime.now(),
        })
        if self.project_id:
            self.env['contractor.project.budget']._refresh_project_consumption(self.project_id.id)
        self.message_post(
            body=Markup(_("<strong>Requisition Approved:</strong> Authorized by %s. Ready for RFQ / Comparative Statement.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_decline(self):
        self.ensure_one()
        self._ensure_state(('draft', 'submitted'), _("decline material requisition"))
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Manager can decline Material Requisitions."))
        return self.action_open_reason_wizard(action_type='decline', target_state='declined')

    def action_revise_mr(self):
        """Archives the declined requisition version and resets to Draft so it can be revised and resubmitted."""
        self.ensure_one()
        if self.state != 'declined':
            raise UserError(_("Only declined Material Requisitions can be revised into a new version."))
        version_rec = self._create_version_snapshot(
            reason=self.last_action_reason or _('Revised after decline'),
            total_value=self.total_estimated_amount,
            editor_user=self.env.user,
            move_attachments=True,
        )
        self.with_context(bypass_state_lock=True).write({
            'state': 'draft',
            'escalated_to_md': False,
            'escalation_datetime': False,
            'submitted_datetime': False,
        })
        self.message_post(
            body=Markup(_(
                "<strong>Requisition Revised &amp; Reset to Draft:</strong> Archived previous submission as v%s. Ready for modification."
            )) % version_rec.version_number,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_create_direct_po(self):
        """Creates a direct single-vendor Purchase Order when approved MR is below 3-vendor RFQ threshold."""
        self.ensure_one()
        if self.state != 'approved':
            raise UserError(_("A direct PO can only be generated from an Approved Material Requisition."))
        if not self.is_below_rfq_threshold:
            raise UserError(_(
                "3-Vendor RFQ Policy Enforced:\n\n"
                "MR value (%s %.2f) meets or exceeds the RFQ threshold (%s %.2f).\n"
                "Please create a Comparative Statement instead of a direct PO."
            ) % (
                self.currency_id.symbol or '', self.total_estimated_amount,
                self.currency_id.symbol or '', self.company_id.rfq_three_vendor_threshold or 10000.0,
            ))
        if not self.preferred_vendor_id:
            raise UserError(_("Please select a 'Direct PO Vendor (Below Threshold)' before generating the Purchase Order."))

        po_lines = [
            (0, 0, {
                'product_id': line.product_id.id,
                'name': line.description or line.product_id.display_name,
                'product_qty': line.qty,
                'uom_id': line.product_uom_id.id,
                'price_unit': line.estimated_unit_cost,
                'date_planned': fields.Datetime.now(),
            })
            for line in self.line_ids
        ]
        po = self.env['purchase.order'].create({
            'partner_id': self.preferred_vendor_id.id,
            'project_id': self.project_id.id,
            'requisition_id': self.id,
            'origin': self.name,
            'order_line': po_lines,
        })
        self.with_context(bypass_state_lock=True).write({'state': 'po_created'})
        self.message_post(
            body=Markup(_(
                "<strong>Direct Single-Vendor PO Generated (Below RFQ Threshold):</strong> %s created for vendor %s."
            )) % (po.name, self.preferred_vendor_id.name),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return {
            'type': 'ir.actions.act_window',
            'name': _('Direct Purchase Order'),
            'res_model': 'purchase.order',
            'res_id': po.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_create_comparative_statement(self):
        """Launches Comparative Statement wizard/creation populated with this MR's lines."""
        self.ensure_one()
        if self.state not in ('approved', 'cs_in_progress'):
            raise UserError(_("A Comparative Statement can only be created for an Approved Material Requisition."))

        cs = self.env['contractor.comparative.statement'].create({
            'requisition_id': self.id,
        })
        self.with_context(bypass_state_lock=True).write({'state': 'cs_in_progress'})

        return {
            'type': 'ir.actions.act_window',
            'name': _('Comparative Statement (3-Vendor Matrix)'),
            'res_model': 'contractor.comparative.statement',
            'res_id': cs.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_comparative_statements(self):
        self.ensure_one()
        return {
            'name': _('Comparative Statements for %s') % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.comparative.statement',
            'view_mode': 'list,form',
            'domain': [('requisition_id', '=', self.id)],
            'context': {'default_requisition_id': self.id},
        }

    def action_view_purchase_orders(self):
        self.ensure_one()
        return {
            'name': _('Purchase Orders for %s') % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('requisition_id', '=', self.id)],
            'context': {'default_requisition_id': self.id, 'default_project_id': self.project_id.id},
        }

    # =========================================================================
    # 24-Hour SLA Auto-Escalation Cron
    # =========================================================================
    @api.model
    def _cron_check_mr_escalations(self):
        """Delegates to the unified SLA Timer Engine (or runs standalone) to avoid duplicate escalations."""
        if 'contractor.sla.timer' in self.env:
            companies = self.env['res.company'].search([])
            now = fields.Datetime.now()
            for company in companies:
                self.env['contractor.sla.timer']._process_material_requisition_slas(company, now)
            return True
        companies = self.env['res.company'].search([])
        now = fields.Datetime.now()

        for company in companies:
            sla_hours = company.mr_escalation_sla_hours or 24
            cutoff_time = now - timedelta(hours=sla_hours)

            stalled_mrs = self.search([
                ('company_id', '=', company.id),
                ('state', '=', 'submitted'),
                ('escalated_to_md', '=', False),
                ('submitted_datetime', '<=', cutoff_time),
            ])

            for mr in stalled_mrs:
                mr.write({
                    'escalated_to_md': True,
                    'escalation_datetime': now,
                })
                # Notify Managing Director
                group_md = self.env.ref('contractor_core.group_contractor_md', raise_if_not_found=False)
                if group_md:
                    for user in (group_md.all_user_ids | group_md.user_ids).filtered(lambda u: u.active):
                        self.env['mail.activity'].create({
                            'activity_type_id': self.env.ref('mail.mail_activity_data_warning').id,
                            'res_model_id': self.env['ir.model']._get_id('contractor.material.requisition'),
                            'res_id': mr.id,
                            'user_id': user.id,
                            'summary': f"SLA ESCALATION: Unapproved MR {mr.name} ({sla_hours}h overdue)",
                            'note': _(
                                "Material Requisition %s for Project '%s' has remained pending approval for over %d hours. "
                                "Escalated for Executive review."
                            ) % (mr.name, mr.project_id.name, sla_hours),
                            'date_deadline': fields.Date.context_today(mr),
                        })

                mr.message_post(
                    body=Markup(_(
                        "<strong>24h SLA Auto-Escalation:</strong> Requisition has been pending approval for more than %d hours. "
                        "Escalated directly to Managing Director."
                    )) % sla_hours,
                    message_type='notification',
                    subtype_xmlid='mail.mt_note',
                )


class ContractorMaterialRequisitionLine(models.Model):
    _name = 'contractor.material.requisition.line'
    _description = 'Material Requisition Item'
    _inherit = ['contractor.line.state.lock.mixin']
    _order = 'id'
    _line_parent_field = 'requisition_id'

    requisition_id = fields.Many2one(
        'contractor.material.requisition',
        string='MR Reference',
        required=True,
        ondelete='cascade',
    )
    product_id = fields.Many2one(
        'product.product',
        string='Product / SKU',
        required=True,
    )
    description = fields.Char(
        string='Description / Specs',
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='Unit of Measure',
        related='product_id.uom_id',
        readonly=True,
    )
    qty = fields.Float(
        string='Requested Qty',
        required=True,
        default=1.0,
    )
    estimated_unit_cost = fields.Monetary(
        string='Est. Unit Cost',
        currency_field='currency_id',
        compute='_compute_default_cost',
        store=True,
        readonly=False,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='requisition_id.currency_id',
        readonly=True,
    )
    estimated_subtotal = fields.Monetary(
        string='Est. Subtotal',
        currency_field='currency_id',
        compute='_compute_subtotal',
        store=True,
    )

    on_hand_qty = fields.Float(
        string='Company Stock On Hand',
        compute='_compute_stock_info',
    )
    net_available_qty = fields.Float(
        string='Net Available Stock',
        compute='_compute_stock_info',
    )
    ordered_qty = fields.Float(
        string='PO Ordered Qty',
        compute='_compute_fulfillment',
    )
    received_qty = fields.Float(
        string='GRN Received Qty',
        compute='_compute_fulfillment',
    )

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id and not self.description:
            self.description = self.product_id.display_name

    @api.depends('product_id')
    def _compute_default_cost(self):
        for line in self:
            if not line.estimated_unit_cost and line.product_id:
                line.estimated_unit_cost = line.product_id.standard_price or 0.0

    @api.depends('qty', 'estimated_unit_cost')
    def _compute_subtotal(self):
        for line in self:
            line.estimated_subtotal = (line.qty or 0.0) * (line.estimated_unit_cost or 0.0)

    @api.depends('product_id')
    def _compute_stock_info(self):
        for line in self:
            if line.product_id:
                quants = self.env['stock.quant'].search([
                    ('product_id', '=', line.product_id.id),
                    ('location_id.usage', '=', 'internal'),
                ])
                total_qty = sum(quants.mapped('quantity'))
                reserved = sum(quants.mapped('reserved_quantity'))
                line.on_hand_qty = total_qty
                line.net_available_qty = total_qty - reserved
            else:
                line.on_hand_qty = 0.0
                line.net_available_qty = 0.0

    @api.depends('requisition_id.purchase_order_ids.state', 'requisition_id.purchase_order_ids.order_line')
    def _compute_fulfillment(self):
        for line in self:
            po_lines = self.env['purchase.order.line'].search([
                ('order_id.requisition_id', '=', line.requisition_id.id),
                ('order_id.state', 'in', ['purchase', 'done']),
                ('product_id', '=', line.product_id.id),
            ])
            line.ordered_qty = sum(po_lines.mapped('product_qty'))
            line.received_qty = sum(po_lines.mapped('qty_received'))

