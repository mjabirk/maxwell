# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class ContractorVariationOrder(models.Model):
    _name = 'contractor.variation.order'
    _description = 'Contractor Scope Variation Order (VO)'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.version.mixin', 'contractor.reason.mixin', 'contractor.no.delete.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='VO Number',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New VO'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        required=True,
        index=True,
        ondelete='restrict',
        tracking=True,
    )
    order_id = fields.Many2one(
        'sale.order',
        string='Original Sales Contract',
        compute='_compute_order_id',
        store=True,
        readonly=False,
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
    reason_type = fields.Selection([
        ('client_request', 'Client Requested Scope Addition'),
        ('site_condition', 'Unforeseen Site Condition / Structural Finding'),
        ('design_change', 'Consultant / Architectural Design Change'),
        ('statutory', 'Regulatory / Statutory Compliance Mandate'),
        ('other', 'Other Contractual Variation'),
    ], string='Variation Driver', required=True, tracking=True, default='client_request')

    scope_description = fields.Text(
        string='Scope of Variation',
        required=True,
        tracking=True,
        help='Detailed technical narrative explaining modifications, additions, or omissions to baseline scope.',
    )
    timeline_impact_days = fields.Integer(
        string='Schedule Extension (Days)',
        default=0,
        tracking=True,
        help='Number of additional contractual calendar days requested from client for executing this variation.',
    )

    # -------------------------------------------------------------------------
    # 4 Dedicated Cost Estimation Blocks (Identical to Sales Estimation)
    # -------------------------------------------------------------------------
    est_labour_line_ids = fields.One2many(
        'contractor.cost.labour.line',
        'variation_order_id',
        string='Block 1: Labor Cost Estimation',
        copy=True,
    )
    est_material_line_ids = fields.One2many(
        'contractor.cost.material.line',
        'variation_order_id',
        string='Block 2: Material Cost Estimation',
        copy=True,
    )
    est_legal_line_ids = fields.One2many(
        'contractor.cost.legal.line',
        'variation_order_id',
        string='Block 3: Legal, Permits & Statutory Fees',
        copy=True,
    )
    est_misc_line_ids = fields.One2many(
        'contractor.cost.misc.line',
        'variation_order_id',
        string='Block 4: Miscellaneous & Site Indirects',
        copy=True,
    )

    # Consolidated Cost Totals
    total_labour_cost = fields.Monetary(
        string='VO Labor Cost',
        currency_field='currency_id',
        compute='_compute_estimation_totals',
        store=True,
    )
    total_material_cost = fields.Monetary(
        string='VO Material Cost',
        currency_field='currency_id',
        compute='_compute_estimation_totals',
        store=True,
    )
    total_legal_cost = fields.Monetary(
        string='VO Legal/Permit Cost',
        currency_field='currency_id',
        compute='_compute_estimation_totals',
        store=True,
    )
    total_misc_cost = fields.Monetary(
        string='VO Misc/Indirect Cost',
        currency_field='currency_id',
        compute='_compute_estimation_totals',
        store=True,
    )
    total_estimated_cost = fields.Monetary(
        string='Consolidated VO Direct Cost',
        currency_field='currency_id',
        compute='_compute_estimation_totals',
        store=True,
    )

    # Margin & Total Pricing
    margin_type = fields.Selection([
        ('percentage', 'Percentage Margin (%)'),
        ('fixed', 'Fixed Monetary Margin'),
    ], string='Margin Mode', default='percentage', required=True)

    margin_percent = fields.Float(
        string='VO Margin (%)',
        default=15.0,
        digits=(16, 2),
    )
    margin_amount = fields.Monetary(
        string='VO Margin Amount',
        currency_field='currency_id',
    )
    total_vo_value = fields.Monetary(
        string='Total Variation Order Value',
        currency_field='currency_id',
        compute='_compute_pricing',
        store=True,
        tracking=True,
        help='Total billable value of this variation order submitted to the client.',
    )

    # Approval Routing & Threshold Metadata
    requires_md_approval = fields.Boolean(
        string='Requires Managing Director Approval',
        default=False,
        readonly=True,
        copy=False,
        tracking=True,
    )
    cumulative_vo_ratio = fields.Float(
        string='Cumulative VO Contract Ratio (%)',
        readonly=True,
        copy=False,
        digits=(16, 2),
    )
    approval_date = fields.Datetime(
        string='Approval Date',
        readonly=True,
        copy=False,
    )

    # Client Acceptance Verification
    client_acceptance_attachment_ids = fields.Many2many(
        'ir.attachment',
        'contractor_vo_acceptance_rel',
        'vo_id',
        'attachment_id',
        string='Client Signed Acceptance Documents',
        help='Mandatory signed client approval (Client LPO, VO Form, or Addendum) before budget integration.',
    )
    client_acceptance_ref = fields.Char(
        string='Client Acceptance Reference',
        help='Client change order number or signed letter reference.',
    )
    client_acceptance_date = fields.Date(
        string='Client Acceptance Date',
    )
    vo_sale_line_id = fields.Many2one(
        'sale.order.line',
        string='Contract Billing Schedule Line',
        readonly=True,
        copy=False,
    )

    state = fields.Selection([
        ('draft', 'Draft Scope'),
        ('submitted', 'Submitted for Review'),
        ('manager_approved', 'Approved by Contractor Manager'),
        ('md_approved', 'Approved by Managing Director'),
        ('sent_to_client', 'Sent to Client'),
        ('client_accepted', 'Client Accepted'),
        ('integrated', 'Integrated into Project Budget'),
        ('declined', 'Declined'),
        ('withdrawn', 'Withdrawn'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    @api.depends('project_id', 'project_id.sale_order_id')
    def _compute_order_id(self):
        for vo in self:
            if vo.project_id and vo.project_id.sale_order_id and not vo.order_id:
                vo.order_id = vo.project_id.sale_order_id

    @api.depends(
        'est_labour_line_ids.subtotal_cost',
        'est_material_line_ids.subtotal_cost',
        'est_legal_line_ids.estimated_cost',
        'est_misc_line_ids.estimated_cost',
    )
    def _compute_estimation_totals(self):
        for vo in self:
            labour = sum(vo.est_labour_line_ids.mapped('subtotal_cost'))
            material = sum(vo.est_material_line_ids.mapped('subtotal_cost'))
            legal = sum(vo.est_legal_line_ids.mapped('estimated_cost'))
            misc = sum(vo.est_misc_line_ids.mapped('estimated_cost'))

            vo.total_labour_cost = labour
            vo.total_material_cost = material
            vo.total_legal_cost = legal
            vo.total_misc_cost = misc
            vo.total_estimated_cost = labour + material + legal + misc

    @api.depends('total_estimated_cost', 'margin_type', 'margin_percent', 'margin_amount')
    def _compute_pricing(self):
        for vo in self:
            cost = vo.total_estimated_cost or 0.0
            if vo.margin_type == 'percentage':
                pct = vo.margin_percent or 0.0
                m_amt = cost * (pct / 100.0)
                vo.margin_amount = m_amt
                vo.total_vo_value = cost + m_amt
            else:
                m_amt = vo.margin_amount or 0.0
                vo.total_vo_value = cost + m_amt

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New VO')) == _('New VO'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.variation.order') or _('New VO')
        return super().create(vals_list)

    # -------------------------------------------------------------------------
    # Routing & Approval Logic
    # -------------------------------------------------------------------------
    def action_submit(self):
        """Prepares VO, checks company MD thresholds and cumulative contract ratios,
        and routes to either Contractor Manager or Managing Director.
        """
        self.ensure_one()
        if self.total_estimated_cost <= 0:
            raise UserError(_("Variation Order Submission Gate: Consolidated VO cost must be greater than zero."))

        company = self.company_id or self.env.company
        md_threshold = company.vo_md_approval_threshold or 50000.0
        ratio_trigger = company.vo_cumulative_ratio_trigger_pct or 10.0

        # Calculate cumulative existing approved/accepted VOs
        existing_vos = self.search([
            ('project_id', '=', self.project_id.id),
            ('id', '!=', self.id),
            ('state', 'in', ['manager_approved', 'md_approved', 'sent_to_client', 'client_accepted', 'integrated']),
        ])
        cumulative_vo_sum = sum(existing_vos.mapped('total_vo_value')) + self.total_vo_value
        baseline_val = self.project_id.baseline_contract_value or (self.order_id and self.order_id.proposed_quotation_value) or 1.0
        cumulative_ratio = (cumulative_vo_sum / baseline_val) * 100.0

        self.cumulative_vo_ratio = cumulative_ratio

        # Determine approval requirement
        exceeds_monetary = self.total_vo_value >= md_threshold
        exceeds_ratio = cumulative_ratio >= ratio_trigger
        requires_md = exceeds_monetary or exceeds_ratio

        self.requires_md_approval = requires_md
        self.state = 'submitted'

        # Snapshot v1
        self._create_version_snapshot(
            reason=_("Initial Variation Order Submission (%s) - MD Required: %s") % (self.total_vo_value, requires_md),
            editor_user=self.env.user,
        )

        target_group = 'contractor_core.group_contractor_md' if requires_md else 'contractor_core.group_contractor_manager'
        group_rec = self.env.ref(target_group, raise_if_not_found=False)
        if group_rec:
            for user in (group_rec.all_user_ids | group_rec.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('contractor.variation.order'),
                    'res_id': self.id,
                    'user_id': user.id,
                    'summary': f"Approve Variation Order {self.name} for {self.project_id.name}",
                    'note': Markup(_(
                        "VO Value: %s | Cumulative Ratio: %.2f%%<br/>"
                        "Routing: %s"
                    )) % (self.total_vo_value, cumulative_ratio, 'Managing Director Approval' if requires_md else 'Manager Approval'),
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_(
                "<strong>Variation Order Submitted:</strong> Value: %s | Ratio: %.2f%%.<br/>"
                "Requires MD Approval: <b>%s</b> (Threshold: %s | Ratio Trigger: %.1f%%)."
            )) % (self.total_vo_value, cumulative_ratio, 'YES' if requires_md else 'NO', md_threshold, ratio_trigger),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_manager_approve(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only Contractor Managers can approve Variation Orders."))

        if self.requires_md_approval:
            raise UserError(_(
                "Executive Threshold Exceeded:\n\n"
                "This Variation Order exceeds the company MD approval threshold or cumulative contract ratio trigger.\n"
                "Approval by the Managing Director is strictly mandatory."
            ))

        self.write({
            'state': 'manager_approved',
            'approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Manager Approval Granted:</strong> Approved by Contractor Manager %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_md_approve(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Only the Managing Director can authorize high-value Variation Orders."))

        self.write({
            'state': 'md_approved',
            'approval_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Executive Approval Granted:</strong> Approved by Managing Director %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_send_to_client(self):
        self.ensure_one()
        if self.state not in ('manager_approved', 'md_approved'):
            raise UserError(_("Variation Order must be approved internally before sending to the client."))
        self.write({'state': 'sent_to_client'})
        self.message_post(
            body=Markup(_("<strong>Dispatched to Client:</strong> VO proposal transmitted to client for formal sign-off.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_client_accept(self):
        self.ensure_one()
        if not self.client_acceptance_attachment_ids:
            raise UserError(_(
                "Client Acceptance Gate Hard-Stop:\n\n"
                "You must upload client signed acceptance documents (Signed VO Form, LPO, or Contract Addendum) "
                "before marking this Variation Order as Client Accepted."
            ))
        self.write({
            'state': 'client_accepted',
            'client_acceptance_date': self.client_acceptance_date or fields.Date.context_today(self),
        })
        self.message_post(
            body=Markup(_("<strong>Client Acceptance Recorded:</strong> Signed documents verified. Ready for budget integration.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_integrate_vo(self):
        """Hard-Stop Budget Integration: Cannot integrate into budget or bill until
        client_acceptance_attachment_ids is uploaded.
        Adds the accepted VO as a separate, traceable line item (VO 1, VO 2...) on
        contractor.project.budget (never merging into baseline) and updates project contract value.
        """
        self.ensure_one()
        if not self.client_acceptance_attachment_ids:
            raise UserError(_(
                "Hard-Stop Integration Gate:\n\n"
                "Cannot integrate into budget or billing until client acceptance documents are uploaded."
            ))

        budget = self.project_id.budget_ids[:1]
        if not budget:
            # Create master budget for project if none exists
            budget = self.env['contractor.project.budget'].create({
                'name': f"Master Budget - {self.project_id.name}",
                'project_id': self.project_id.id,
                'contract_amount': self.project_id.baseline_contract_value or 0.0,
            })

        # Calculate next VO sequence index (e.g. VO 1, VO 2...)
        existing_vo_count = len(budget.vo_line_ids) + 1
        vo_tag = f"VO {existing_vo_count}"

        # Create separate, traceable VO line item on the master budget (never merging into baseline)
        self.env['contractor.project.budget.vo.line'].create({
            'budget_id': budget.id,
            'vo_id': self.id,
            'vo_sequence': vo_tag,
            'name': f"[{vo_tag}] {self.name} - {self.scope_description[:80]}",
            'planned_cost': self.total_estimated_cost,
            'labour_cost': self.total_labour_cost,
            'material_cost': self.total_material_cost,
            'legal_cost': self.total_legal_cost,
            'misc_cost': self.total_misc_cost,
            'billed_amount': self.total_vo_value,
            'schedule_impact_days': self.timeline_impact_days,
            'integration_date': fields.Datetime.now(),
        })

        # Ensure master budget has at least one line per cost block carrying VO cost
        for block_type, block_amt in (
            ('labour', self.total_labour_cost),
            ('material', self.total_material_cost),
            ('legal', self.total_legal_cost),
            ('misc', self.total_misc_cost),
        ):
            if block_amt > 0.0 and not budget.line_ids.filtered(lambda l: l.block_type == block_type):
                self.env['contractor.project.budget.line'].create({
                    'budget_id': budget.id,
                    'block_type': block_type,
                    'name': f"VO Allocation ({block_type.title()})",
                    'quantity': 1.0,
                    'unit_cost': 0.0,
                    'planned_amount': 0.0,
                    'notes': f"Auto-created block container for {vo_tag}",
                })

        # Update linked Sale Order billing schedule with accepted VO line
        if self.order_id and self.total_vo_value > 0.0:
            line_desc = f"[{vo_tag}] {self.name}: {self.scope_description[:120]}"
            vo_ctx = {'skip_estimation_sync': True, 'from_vo_integration': True, 'active_vo_id': self.id}
            if self.vo_sale_line_id and self.vo_sale_line_id.exists():
                self.vo_sale_line_id.with_context(**vo_ctx).write({
                    'name': line_desc,
                    'price_unit': self.total_vo_value,
                })
            else:
                product = self.env['product.product'].search([('type', '=', 'service')], limit=1) or self.env['product.product'].search([], limit=1)
                if product:
                    sol = self.env['sale.order.line'].with_context(**vo_ctx).create({
                        'order_id': self.order_id.id,
                        'product_id': product.id,
                        'name': line_desc,
                        'product_uom_qty': 1.0,
                        'price_unit': self.total_vo_value,
                    })
                    self.vo_sale_line_id = sol.id

        self.write({'state': 'integrated'})

        # Update Project milestone & contract metrics
        self.project_id._compute_vo_totals()

        self.message_post(
            body=Markup(_(
                "<strong>Variation Order Integrated:</strong> Traceable budget item <b>%s</b> appended to "
                "Project Master Budget <b>%s</b>.<br/>"
                "• Added Direct Cost Budget: %s<br/>"
                "• Added Contract Billable Value: %s<br/>"
                "• Added Schedule Extension: %d calendar days."
            )) % (vo_tag, budget.name, self.total_estimated_cost, self.total_vo_value, self.timeline_impact_days),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

        return {
            'type': 'ir.actions.act_window',
            'name': _('Project Master Budget'),
            'res_model': 'contractor.project.budget',
            'res_id': budget.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_return_for_revision(self):
        """Returns the VO back to draft for estimator revision after capturing a mandatory reason and snapshot."""
        self.ensure_one()
        return self.action_open_reason_wizard(
            action_type='return',
            target_state='draft',
            callback_method='_callback_return_for_revision',
        )

    def _callback_return_for_revision(self, reason=None):
        clean_reason = reason or self.last_action_reason or _('Returned for revision by Management')
        for vo in self:
            vo._create_version_snapshot(
                reason=clean_reason,
                editor_user=self.env.user,
                move_attachments=True,
            )
            if vo.state != 'draft':
                vo.state = 'draft'

    def action_decline(self):
        self.ensure_one()
        return self.action_open_reason_wizard(action_type='decline', target_state='declined')

    def action_withdraw(self):
        self.ensure_one()
        return self.action_open_reason_wizard(action_type='withdraw', target_state='withdrawn')

