# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorSiteConsumption(models.Model):
    _name = 'contractor.site.consumption'
    _description = 'Daily Site Material Consumption & Store Reconciliation'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.site.lock.mixin', 'contractor.no.delete.mixin']
    _order = 'consumption_date desc, id desc'

    name = fields.Char(
        string='Consumption Sheet Number',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Sheet'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project Site',
        required=True,
        index=True,
        tracking=True,
    )
    consumption_date = fields.Date(
        string='Consumption Date',
        required=True,
        default=fields.Date.context_today,
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
    site_engineer_id = fields.Many2one(
        'res.users',
        string='Recorded By (Site Engineer)',
        default=lambda self: self.env.user,
        tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Draft Entry'),
        ('reconciled', 'Reconciled by Storekeeper'),
        ('pm_approved', 'Approved by PM / Costed'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    line_ids = fields.One2many(
        'contractor.site.consumption.line',
        'consumption_id',
        string='Consumed Material Lines',
        copy=True,
    )
    total_consumption_cost = fields.Monetary(
        string='Total Consumed Material Cost',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    variance_flagged = fields.Boolean(
        string='Tolerance Variance Flagged',
        compute='_compute_totals',
        store=True,
        tracking=True,
    )
    reconciliation_notes = fields.Text(
        string='Storekeeper Investigation & Reconciliation Notes',
        tracking=True,
        help='Mandatory if material consumed exceeds store issues beyond company tolerance percentage.',
    )
    reconciled_by = fields.Many2one(
        'res.users',
        string='Reconciled By Storekeeper',
        readonly=True,
        copy=False,
    )
    reconciliation_date = fields.Datetime(
        string='Reconciliation Datetime',
        readonly=True,
        copy=False,
    )
    approver_id = fields.Many2one(
        'res.users',
        string='PM Approved By',
        readonly=True,
        copy=False,
    )
    approval_date = fields.Datetime(
        string='PM Approval Datetime',
        readonly=True,
        copy=False,
    )
    cost_posting_id = fields.Many2one(
        'contractor.daily.cost.posting',
        string='Daily Project Cost Posting',
        readonly=True,
        copy=False,
    )

    _check_uniq_project_consumption_date = models.Constraint(
        'UNIQUE(project_id, consumption_date)',
        'A daily site material consumption sheet already exists for this Project on this date!'
    )

    @api.depends('line_ids.subtotal_cost', 'line_ids.variance_flagged')
    def _compute_totals(self):
        for rec in self:
            rec.total_consumption_cost = sum(rec.line_ids.mapped('subtotal_cost'))
            rec.variance_flagged = any(rec.line_ids.mapped('variance_flagged'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Sheet')) == _('New Sheet'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.site.consumption') or _('New Sheet')
        return super().create(vals_list)

    def action_submit_to_storekeeper(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Please add at least one material consumption line."))

        for line in self.line_ids:
            if line.consumed_today_qty <= 0:
                raise UserError(_("Consumed quantity must be strictly greater than zero for product '%s'.") % line.product_id.display_name)
            if not line.source_picking_id and not line.source_po_id:
                raise UserError(_("Mandatory Reference: Each line must link to a Source Stock Picking (GRN/Issue) or Purchase Order for product '%s'.") % line.product_id.display_name)

        # Notify Storekeeper
        group_store = self.env.ref('contractor_core.group_contractor_storekeeper', raise_if_not_found=False)
        if group_store:
            for user in (group_store.all_user_ids | group_store.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('contractor.site.consumption'),
                    'res_id': self.id,
                    'user_id': user.id,
                    'summary': f"Reconcile Daily Material Consumption: {self.name} for {self.project_id.name}",
                    'note': _("Material consumption of %s items valued at %s %.2f requires storekeeper reconciliation.") % (
                        len(self.line_ids), self.currency_id.symbol or '', self.total_consumption_cost
                    ),
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_("<strong>Material Consumption Submitted:</strong> Dispatched to Storekeeper for stock issue reconciliation.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_reconcile_storekeeper(self):
        """Storekeeper verifies consumed quantities against issued store receipts; checks tolerance."""
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_storekeeper') or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise UserError(_("Access Denied: Only a Contractor Storekeeper or Manager can reconcile site consumption."))

        company = self.company_id or self.env.company
        tolerance = company.material_variance_tolerance_pct or 5.0

        if self.variance_flagged:
            if not self.reconciliation_notes or len(self.reconciliation_notes.strip()) < 15:
                raise UserError(_(
                    "Storekeeper Material Reconciliation Hard-Stop:\n\n"
                    "One or more material lines exceed store-issued quantities beyond the allowable tolerance (%.1f%%).\n\n"
                    "Documented investigation findings (minimum 15 characters) in 'Reconciliation Notes' are mandatory before closing reconciliation."
                ) % tolerance)

        self.write({
            'state': 'reconciled',
            'reconciled_by': self.env.user.id,
            'reconciliation_date': fields.Datetime.now(),
        })

        # Activity to PM
        pm_user = self.project_id.user_id
        if pm_user:
            self.env['mail.activity'].create({
                'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                'res_model_id': self.env['ir.model']._get_id('contractor.site.consumption'),
                'res_id': self.id,
                'user_id': pm_user.id,
                'summary': f"PM Approval Required: Reconciled Material Consumption {self.name}",
                'note': _("Storekeeper reconciled consumption for %s. Variance Flagged: %s.") % (
                    self.project_id.name, self.variance_flagged
                ),
                'date_deadline': fields.Date.context_today(self),
            })

        self.message_post(
            body=Markup(_("<strong>Consumption Reconciled by Storekeeper:</strong> Verified by %s. Forwarded to PM for cost approval.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_pm_approve(self):
        """Authorizes material consumption and triggers daily project cost posting."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_project_manager'):
            raise UserError(_("Access Denied: Only a Contractor Project Manager can approve material consumption sheets."))

        self.write({
            'state': 'pm_approved',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })

        # Auto-trigger / sync Daily Cost Posting
        posting = self.env['contractor.daily.cost.posting'].get_or_create_daily_posting(
            project_id=self.project_id.id,
            cost_date=self.consumption_date,
        )
        self.cost_posting_id = posting.id

        self.message_post(
            body=Markup(_("<strong>Material Consumption Approved:</strong> Authorized by Project Manager %s. Synced with Daily Cost Posting <a href='#' data-oe-model='contractor.daily.cost.posting' data-oe-id='%s'>%s</a>.")) % (
                self.env.user.name, posting.id, posting.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_decline(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_project_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Project Manager can decline material consumption sheets."))
        return {
            'name': _('Decline Material Consumption'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': 'decline',
                'default_target_state': 'declined',
            },
        }


class ContractorSiteConsumptionLine(models.Model):
    _name = 'contractor.site.consumption.line'
    _description = 'Site Material Consumption Line'
    _order = 'product_id, id'

    consumption_id = fields.Many2one(
        'contractor.site.consumption',
        string='Consumption Sheet',
        required=True,
        ondelete='cascade',
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        related='consumption_id.project_id',
        store=True,
        readonly=True,
    )
    consumption_date = fields.Date(
        string='Consumption Date',
        related='consumption_id.consumption_date',
        store=True,
        readonly=True,
    )
    cost_head = fields.Selection([
        ('civil', 'Civil & Structural'),
        ('mep', 'MEP (Mechanical, Electrical, Plumbing)'),
        ('finishing', 'Architectural & Finishing'),
        ('external', 'External & Infrastructure'),
        ('general', 'General Site Works'),
    ], string='Cost Head', default='civil', required=True)

    product_id = fields.Many2one(
        'product.product',
        string='Material / SKU',
        required=True,
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='Unit of Measure',
        related='product_id.uom_id',
        readonly=True,
    )
    source_picking_id = fields.Many2one(
        'stock.picking',
        string='Source Stock Transfer / GRN',
        help='Goods receipt note (GRN) or stock transfer note issuing this material to the project site.',
    )
    source_po_id = fields.Many2one(
        'purchase.order',
        string='Source Purchase Order',
        help='Purchase order under which this material was procured for the project.',
    )
    issued_to_site_qty = fields.Float(
        string='Store Issued Qty',
        compute='_compute_quantities',
        store=True,
        readonly=False,
        help='Total quantity received on site from stores or suppliers.',
    )
    previously_consumed_qty = fields.Float(
        string='Previously Consumed Qty',
        compute='_compute_quantities',
        store=True,
        readonly=False,
        help='Cumulative quantity consumed on this project prior to this date.',
    )
    consumed_today_qty = fields.Float(
        string='Consumed Today',
        required=True,
        default=0.0,
    )
    remaining_site_stock_qty = fields.Float(
        string='Remaining Site Working Stock',
        compute='_compute_quantities',
        store=True,
        help='Theoretical balance of material remaining on site location.',
    )
    unit_cost = fields.Monetary(
        string='Unit Cost',
        currency_field='currency_id',
        compute='_compute_unit_cost',
        store=True,
        readonly=False,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='consumption_id.currency_id',
        readonly=True,
    )
    subtotal_cost = fields.Monetary(
        string='Subtotal Cost',
        currency_field='currency_id',
        compute='_compute_subtotal',
        store=True,
    )
    variance_pct = fields.Float(
        string='Over-Consumption %',
        compute='_compute_variance',
        store=True,
    )
    variance_flagged = fields.Boolean(
        string='Variance Flagged',
        compute='_compute_variance',
        store=True,
    )
    work_location = fields.Char(
        string='Placement Zone / Grid',
    )

    @api.onchange('source_picking_id')
    def _onchange_source_picking(self):
        if self.source_picking_id:
            # Auto-link PO if available on picking
            if getattr(self.source_picking_id, 'purchase_id', None):
                self.source_po_id = self.source_picking_id.purchase_id
            # Auto-compute issued qty from picking moves
            moves = self.source_picking_id.move_ids.filtered(lambda m: m.product_id.id == self.product_id.id)
            if moves:
                self.issued_to_site_qty = sum(moves.mapped('quantity'))

    @api.depends('product_id', 'project_id', 'source_picking_id', 'consumed_today_qty')
    def _compute_quantities(self):
        for line in self:
            if not line.product_id or not line.project_id:
                line.issued_to_site_qty = 0.0
                line.previously_consumed_qty = 0.0
                line.remaining_site_stock_qty = 0.0
                continue

            # 1. Total issued to site
            if line.source_picking_id:
                moves = line.source_picking_id.move_ids.filtered(lambda m: m.product_id.id == line.product_id.id)
                issued = sum(moves.mapped('quantity'))
            else:
                # Search all done pickings for this project and product
                all_moves = self.env['stock.move'].search([
                    ('project_id', '=', line.project_id.id),
                    ('product_id', '=', line.product_id.id),
                    ('state', '=', 'done'),
                ])
                issued = sum(all_moves.mapped('quantity'))

            # 2. Previously consumed on earlier dates
            earlier_lines = self.search([
                ('project_id', '=', line.project_id.id),
                ('product_id', '=', line.product_id.id),
                ('consumption_date', '<', line.consumption_date or fields.Date.context_today(line)),
                ('consumption_id.state', 'in', ['reconciled', 'pm_approved']),
            ])
            prev = sum(earlier_lines.mapped('consumed_today_qty'))

            line.issued_to_site_qty = issued
            line.previously_consumed_qty = prev
            line.remaining_site_stock_qty = max(issued - prev - (line.consumed_today_qty or 0.0), 0.0)

    @api.depends('product_id', 'source_po_id', 'source_picking_id')
    def _compute_unit_cost(self):
        for line in self:
            if line.unit_cost:
                continue
            cost = 0.0
            if line.source_po_id:
                po_lines = line.source_po_id.order_line.filtered(lambda l: l.product_id.id == line.product_id.id)
                if po_lines:
                    cost = po_lines[0].price_unit
            if not cost and line.product_id:
                cost = line.product_id.standard_price or 0.0
            line.unit_cost = cost

    @api.depends('consumed_today_qty', 'unit_cost')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal_cost = (line.consumed_today_qty or 0.0) * (line.unit_cost or 0.0)

    @api.depends('consumed_today_qty', 'issued_to_site_qty', 'previously_consumed_qty')
    def _compute_variance(self):
        for line in self:
            company = line.consumption_id.company_id or self.env.company
            tolerance = company.material_variance_tolerance_pct or 5.0

            total_consumed = (line.previously_consumed_qty or 0.0) + (line.consumed_today_qty or 0.0)
            issued = line.issued_to_site_qty or 0.0

            if issued > 0 and total_consumed > issued:
                variance = ((total_consumed - issued) / issued) * 100.0
                line.variance_pct = variance
                line.variance_flagged = variance > tolerance
            else:
                line.variance_pct = 0.0
                line.variance_flagged = False

