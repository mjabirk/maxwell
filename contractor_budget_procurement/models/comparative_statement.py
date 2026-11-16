# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorComparativeStatement(models.Model):
    _name = 'contractor.comparative.statement'
    _description = 'Contractor Comparative Statement (3-Vendor Matrix)'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.version.mixin', 'contractor.reason.mixin', 'contractor.no.delete.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='CS Number',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New CS'),
        index=True,
    )
    requisition_id = fields.Many2one(
        'contractor.material.requisition',
        string='Source Material Requisition',
        required=True,
        index=True,
        tracking=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        related='requisition_id.project_id',
        store=True,
        readonly=True,
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

    state = fields.Selection([
        ('draft', 'Draft Matrix'),
        ('submitted', 'Submitted for Review'),
        ('pm_approved', 'Approved by Project Manager'),
        ('manager_approved', 'Approved by Manager'),
        ('md_approved', 'Approved by Managing Director'),
        ('pos_created', 'Purchase Order(s) Issued'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    # 3-Vendor Rule & Sole-Source Governance
    is_sole_source = fields.Boolean(
        string='Sole-Source Exemption Claimed',
        default=False,
        tracking=True,
        help='Tick if the procurement is strictly restricted to an exclusive manufacturer/vendor, exempting 3-quote requirement.',
    )
    sole_source_reason = fields.Text(
        string='Sole-Source Technical Justification',
        tracking=True,
        help='Technical and legal explanation for proprietary sole-source procurement.',
    )
    sole_source_attachment_ids = fields.Many2many(
        'ir.attachment',
        'contractor_cs_sole_source_rel',
        'cs_id',
        'attachment_id',
        string='Sole-Source Justification Documents',
        help='Mandatory manufacturer proprietary letters, client specifications, or patent documents.',
    )
    fewer_quotes_reason = fields.Text(
        string='Justification for Fewer Than 3 Quotes',
        tracking=True,
        help='Mandatory justification if total value exceeds 3-vendor threshold but fewer than 3 vendor quotes could be obtained.',
    )

    line_ids = fields.One2many(
        'contractor.comparative.statement.line',
        'statement_id',
        string='Bid Matrix Items',
        copy=True,
    )

    vendor_count = fields.Integer(
        string='Distinct Vendors Count',
        compute='_compute_matrix_analytics',
        store=True,
    )
    lowest_quote_amount = fields.Monetary(
        string='Lowest Bid Amount',
        currency_field='currency_id',
        compute='_compute_matrix_analytics',
        store=True,
    )
    selected_quote_amount = fields.Monetary(
        string='Selected / Awarded Bid Amount',
        currency_field='currency_id',
        compute='_compute_matrix_analytics',
        store=True,
    )
    requires_md_approval = fields.Boolean(
        string='Requires Managing Director Approval',
        compute='_compute_matrix_analytics',
        store=True,
        help='Triggered if selected bid value meets or exceeds the company PO MD Approval Threshold.',
    )
    approval_tier = fields.Selection([
        ('pm', 'Project Manager'),
        ('manager', 'Contractor Manager'),
        ('md', 'Managing Director'),
    ], string='Required Approval Tier', compute='_compute_matrix_analytics', store=True)

    approver_id = fields.Many2one(
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

    purchase_order_ids = fields.One2many(
        'purchase.order',
        'comparative_statement_id',
        string='Generated Purchase Orders',
    )
    purchase_order_count = fields.Integer(
        string='Awarded PO Count',
        compute='_compute_po_count',
    )

    @api.depends('purchase_order_ids')
    def _compute_po_count(self):
        for cs in self:
            cs.purchase_order_count = len(cs.purchase_order_ids)

    @api.depends(
        'line_ids.vendor_id', 'line_ids.subtotal', 'line_ids.is_selected',
        'company_id.po_md_approval_threshold', 'company_id.cs_pm_approval_threshold',
    )
    def _compute_matrix_analytics(self):
        for cs in self:
            distinct_vendors = cs.line_ids.mapped('vendor_id')
            cs.vendor_count = len(distinct_vendors)

            # Group totals by vendor
            vendor_totals = {}
            for line in cs.line_ids:
                if line.vendor_id:
                    vendor_totals[line.vendor_id.id] = vendor_totals.get(line.vendor_id.id, 0.0) + line.subtotal

            if vendor_totals:
                cs.lowest_quote_amount = min(vendor_totals.values())
            else:
                cs.lowest_quote_amount = 0.0

            selected_lines = cs.line_ids.filtered(lambda l: l.is_selected)
            cs.selected_quote_amount = sum(selected_lines.mapped('subtotal'))

            company = cs.company_id or self.env.company
            md_thresh = company.po_md_approval_threshold or 50000.0
            pm_thresh = company.cs_pm_approval_threshold or 10000.0
            if cs.selected_quote_amount >= md_thresh:
                cs.requires_md_approval = True
                cs.approval_tier = 'md'
            elif cs.selected_quote_amount <= pm_thresh:
                cs.requires_md_approval = False
                cs.approval_tier = 'pm'
            else:
                cs.requires_md_approval = False
                cs.approval_tier = 'manager'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New CS')) == _('New CS'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.comparative.statement') or _('New CS')
        return super().create(vals_list)

    def action_submit(self):
        """Hard-Stop validations for 3-Vendor Rule, Sole-Source, Non-Lowest Award, and Budget Availability."""
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Please populate vendor quote lines before submitting the Comparative Statement."))

        selected_lines = self.line_ids.filtered(lambda l: l.is_selected)
        if not selected_lines:
            raise UserError(_("Governance Hard-Stop:\nYou must select at least one vendor line for award ('Award / Selected Vendor')."))

        company = self.company_id or self.env.company
        three_vendor_threshold = company.rfq_three_vendor_threshold or 10000.0

        # 1. 3-Vendor RFQ Rule Enforcement
        if self.selected_quote_amount >= three_vendor_threshold or self.lowest_quote_amount >= three_vendor_threshold:
            if self.is_sole_source:
                if not self.sole_source_reason or len(self.sole_source_reason.strip()) < 15:
                    raise UserError(_(
                        "Sole-Source Exemption Gate:\n\n"
                        "A detailed Sole-Source Technical Justification (minimum 15 characters) is mandatory."
                    ))
                if not self.sole_source_attachment_ids:
                    raise UserError(_(
                        "Sole-Source Exemption Gate:\n\n"
                        "You must attach supporting documentation (client directive, manufacturer exclusivity letter) "
                        "in 'Sole-Source Justification Documents' before submitting."
                    ))
            elif self.vendor_count < 3:
                if not self.fewer_quotes_reason or len(self.fewer_quotes_reason.strip()) < 15:
                    raise UserError(_(
                        "3-Vendor RFQ Compliance Hard-Stop:\n\n"
                        "Procurement value (%s %.2f) meets or exceeds the company 3-vendor threshold (%s %.2f).\n\n"
                        "A minimum of 3 independent competitive vendor bids is strictly mandatory.\n"
                        "Currently, only %d vendor(s) are evaluated.\n\n"
                        "To proceed with fewer quotes, provide a documented justification in 'Justification for Fewer Than 3 Quotes'."
                    ) % (
                        self.currency_id.symbol or '', self.selected_quote_amount,
                        self.currency_id.symbol or '', three_vendor_threshold,
                        self.vendor_count
                    ))

        # 2. Non-Lowest Bidder Justification Check
        for sline in selected_lines:
            other_lines = self.line_ids.filtered(
                lambda l: l.product_id.id == sline.product_id.id and l.spec_compliant and l.quoted_price < sline.quoted_price
            )
            if other_lines and (not sline.selection_justification or len(sline.selection_justification.strip()) < 15):
                cheapest = min(other_lines.mapped('quoted_price'))
                raise UserError(_(
                    "Non-Lowest Bid Selection Justification Required:\n\n"
                    "For product '%s', vendor '%s' was selected at %s %.2f, but vendor '%s' offered %s %.2f.\n"
                    "You must provide a documented reason (minimum 15 characters) in 'Selection Justification' "
                    "(e.g. delivery lead time advantage, warranty terms, brand specification)."
                ) % (
                    sline.product_id.display_name, sline.vendor_id.name,
                    self.currency_id.symbol or '', sline.quoted_price,
                    other_lines[0].vendor_id.name, self.currency_id.symbol or '', cheapest
                ))

        # 3. Budget Availability Hard-Stop
        self.env['contractor.project.budget'].check_budget_availability(
            project_id=self.project_id.id,
            cost_head='material',
            requested_amount=self.selected_quote_amount,
        )

        self._compute_matrix_analytics()
        self.write({
            'state': 'submitted',
        })

        # Activity routing
        if self.approval_tier == 'md':
            target_group = 'contractor_core.group_contractor_md'
            route_label = 'Managing Director'
        elif self.approval_tier == 'pm':
            target_group = 'contractor_core.group_contractor_project_manager'
            route_label = 'Project Manager'
        else:
            target_group = 'contractor_core.group_contractor_manager'
            route_label = 'Manager'
        group_rec = self.env.ref(target_group, raise_if_not_found=False)
        if group_rec:
            for user in (group_rec.all_user_ids | group_rec.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('contractor.comparative.statement'),
                    'res_id': self.id,
                    'user_id': user.id,
                    'summary': f"Approve Bid Award: CS {self.name} for {self.project_id.name}",
                    'note': _("Bid award of %s %.2f (%d vendors evaluated). Approver routing: %s.") % (
                        self.currency_id.symbol or '', self.selected_quote_amount, self.vendor_count,
                        route_label
                    ),
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_("<strong>Comparative Statement Submitted:</strong> Award value: %s %.2f | Vendors: %d | Approval Tier: %s.")) % (
                self.currency_id.symbol or '', self.selected_quote_amount, self.vendor_count, route_label
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_pm_approve(self):
        """Project Manager signs off comparative evaluation within PM threshold."""
        self.ensure_one()
        if not (
            self.env.user.has_group('contractor_core.group_contractor_project_manager')
            or self.env.user.has_group('contractor_core.group_contractor_manager')
            or self.env.user.has_group('contractor_core.group_contractor_md')
        ):
            raise UserError(_("Access Denied: Only a Project Manager or Contractor Manager can approve this Comparative Statement."))

        if self.approval_tier != 'pm':
            pm_thresh = (self.company_id or self.env.company).cs_pm_approval_threshold or 10000.0
            raise UserError(_(
                "Approval Threshold Exceeded:\n\n"
                "Award value (%s %.2f) exceeds Project Manager approval limit (%s %.2f). "
                "Contractor Manager or MD approval is required."
            ) % (
                self.currency_id.symbol or '', self.selected_quote_amount,
                self.currency_id.symbol or '', pm_thresh,
            ))

        self.write({
            'state': 'pm_approved',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Project Manager Approved:</strong> Authorized by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_manager_approve(self):
        """Contractor Manager signs off comparative evaluation."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can approve Comparative Statements."))

        if self.requires_md_approval and not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_(
                "Executive Approval Threshold Exceeded:\n\n"
                "The selected award (%s %.2f) meets or exceeds the PO MD Approval Threshold (%s %.2f).\n"
                "Managing Director sign-off is mandatory."
            ) % (
                self.currency_id.symbol or '', self.selected_quote_amount,
                self.currency_id.symbol or '', self.company_id.po_md_approval_threshold or 50000.0
            ))

        target_state = 'md_approved' if self.env.user.has_group('contractor_core.group_contractor_md') and self.requires_md_approval else 'manager_approved'
        self.write({
            'state': target_state,
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })

        self.message_post(
            body=Markup(_("<strong>Comparative Evaluation Approved:</strong> Authorized by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_md_approve(self):
        """Managing Director grants final executive procurement clearance."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Only the Managing Director can authorize this high-value procurement."))

        self.write({
            'state': 'md_approved',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })

        self.message_post(
            body=Markup(_("<strong>Executive Clearance Granted:</strong> Authorized by Managing Director %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_generate_purchase_orders(self):
        """Generates confirmed / ready-to-confirm Purchase Orders for the awarded vendors."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_procurement_officer')):
            raise AccessError(_("Access Denied: Only Procurement Officers or Managers can generate Purchase Orders."))
        if self.state not in ('pm_approved', 'manager_approved', 'md_approved'):
            raise UserError(_("Purchase Orders can only be generated after approval."))

        selected_lines = self.line_ids.filtered(lambda l: l.is_selected)
        if not selected_lines:
            raise UserError(_("No selected vendor lines found to create purchase orders."))

        # Group lines by vendor
        vendor_lines = {}
        for line in selected_lines:
            vendor_lines.setdefault(line.vendor_id, []).append(line)

        created_pos = self.env['purchase.order']
        for vendor, lines in vendor_lines.items():
            po_lines_vals = []
            for l in lines:
                po_lines_vals.append((0, 0, {
                    'product_id': l.product_id.id,
                    'name': l.description or l.product_id.display_name,
                    'product_qty': l.qty,
                    'product_uom_id': l.product_uom_id.id,
                    'price_unit': l.quoted_price,
                    'date_planned': fields.Datetime.now(),
                }))

            po_vals = {
                'partner_id': vendor.id,
                'project_id': self.project_id.id,
                'requisition_id': self.requisition_id.id,
                'comparative_statement_id': self.id,
                'origin': f"{self.requisition_id.name} / {self.name}",
                'order_line': po_lines_vals,
            }

            if self.requires_md_approval and self.state == 'md_approved':
                po_vals.update({
                    'md_approval_state': 'approved',
                    'md_approver_id': self.approver_id.id or self.env.user.id,
                    'md_approval_date': fields.Datetime.now(),
                })

            po = self.env['purchase.order'].create(po_vals)
            created_pos |= po

        self.write({
            'state': 'pos_created',
        })
        self.requisition_id.write({
            'state': 'po_created',
        })

        self.message_post(
            body=Markup(_("<strong>Purchase Orders Generated:</strong> Created %d PO(s): %s.")) % (
                len(created_pos), ", ".join(created_pos.mapped('name'))
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

        return {
            'name': _('Generated Purchase Orders'),
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_pos.ids)],
        }

    def action_view_purchase_orders(self):
        self.ensure_one()
        return {
            'name': _('Awarded Purchase Orders'),
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.purchase_order_ids.ids)],
            'context': {'default_project_id': self.project_id.id, 'default_comparative_statement_id': self.id},
        }

    def action_decline(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Manager can decline Comparative Statements."))
        return self.action_open_reason_wizard(action_type='decline', target_state='declined')


class ContractorComparativeStatementLine(models.Model):
    _name = 'contractor.comparative.statement.line'
    _description = 'Bid Matrix Line Item'
    _order = 'product_id, quoted_price, id'

    statement_id = fields.Many2one(
        'contractor.comparative.statement',
        string='Comparative Statement',
        required=True,
        ondelete='cascade',
    )
    vendor_id = fields.Many2one(
        'res.partner',
        string='Supplier / Vendor',
        required=True,
    )
    product_id = fields.Many2one(
        'product.product',
        string='Material / Item',
        required=True,
    )
    description = fields.Char(
        string='Item Description',
    )
    qty = fields.Float(
        string='Quantity',
        required=True,
        default=1.0,
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='UoM',
        related='product_id.uom_id',
        readonly=True,
    )
    quoted_price = fields.Monetary(
        string='Quoted Unit Price',
        currency_field='currency_id',
        required=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='statement_id.currency_id',
        readonly=True,
    )
    subtotal = fields.Monetary(
        string='Subtotal',
        currency_field='currency_id',
        compute='_compute_subtotal',
        store=True,
    )
    delivery_days = fields.Integer(
        string='Delivery Days',
        default=7,
        help='Vendor committed delivery timeline in days.',
    )
    payment_terms = fields.Char(
        string='Offered Payment Terms',
        default='30 Days Net',
    )
    spec_compliant = fields.Boolean(
        string='Spec Compliant',
        default=True,
        help='Technical evaluation confirms compliance with architect/client specs.',
    )
    compliance_notes = fields.Text(
        string='Compliance / Quality Remarks',
    )
    is_selected = fields.Boolean(
        string='Award / Selected Vendor',
        default=False,
    )
    selection_justification = fields.Text(
        string='Selection Justification',
        help='Mandatory if this bid is selected despite another compliant vendor offering a lower price.',
    )

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id and not self.description:
            self.description = self.product_id.display_name

    @api.depends('qty', 'quoted_price')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = (line.qty or 0.0) * (line.quoted_price or 0.0)

