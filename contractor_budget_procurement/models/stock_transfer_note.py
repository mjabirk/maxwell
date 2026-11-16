# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


# =========================================================================
# 1. Warehouse & Location Classification
# =========================================================================
class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    warehouse_type = fields.Selection([
        ('central', 'Central Warehouse / Store'),
        ('project_site', 'Project Site Store'),
        ('other', 'Other Storage Location'),
    ], string='Contractor Store Type', default='other', required=True)

    project_id = fields.Many2one(
        'project.project',
        string='Assigned Project',
        ondelete='set null',
        help='Project associated with this site store (if project_site type).',
    )


class StockLocation(models.Model):
    _inherit = 'stock.location'

    warehouse_type = fields.Selection(
        related='warehouse_id.warehouse_type',
        string='Warehouse Classification',
        store=True,
        readonly=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Assigned Project',
        compute='_compute_location_project_id',
        store=True,
        readonly=False,
        index=True,
        help='Project associated with this location (inherited from warehouse if set, or assigned directly per sub-location).',
    )

    @api.depends('warehouse_id', 'warehouse_id.project_id')
    def _compute_location_project_id(self):
        for loc in self:
            if loc.warehouse_id and loc.warehouse_id.project_id:
                loc.project_id = loc.warehouse_id.project_id


# =========================================================================
# 2. Consolidated Stock Availability Wizard & Breakdown
# =========================================================================
class ContractorStockAvailabilityWizard(models.TransientModel):
    _name = 'contractor.stock.availability.wizard'
    _description = 'Consolidated Stock Availability Across Stores'

    product_id = fields.Many2one(
        'product.product',
        string='Product / SKU',
        required=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        required=True,
    )
    line_ids = fields.One2many(
        'contractor.stock.availability.line',
        'wizard_id',
        string='Store Inventory Breakdown',
        compute='_compute_availability_lines',
        readonly=False,
    )
    total_central_on_hand = fields.Float(
        string='Central Store On Hand',
        compute='_compute_totals',
    )
    total_site_on_hand = fields.Float(
        string='Project Sites On Hand',
        compute='_compute_totals',
    )
    total_free_on_hand = fields.Float(
        string='Free Stock On Hand',
        compute='_compute_totals',
    )
    total_project_on_hand = fields.Float(
        string='Project-Allocated On Hand',
        compute='_compute_totals',
    )
    total_net_available = fields.Float(
        string='Total Net Available Stock',
        compute='_compute_totals',
    )

    def _build_availability_lines_for_products(self, products, company):
        """Groups quants by product, location, and effective project_id (Free Stock vs Project-Allocated)."""
        lines = []
        if not products:
            return lines
        internal_locations = self.env['stock.location'].search([
            ('usage', '=', 'internal'),
            ('company_id', 'in', [company.id, False]),
        ])
        for prod in products:
            for loc in internal_locations:
                quants = self.env['stock.quant'].search([
                    ('product_id', '=', prod.id),
                    ('location_id', '=', loc.id),
                ])
                if not quants:
                    continue
                wh = loc.warehouse_id
                grouped = {}
                for q in quants:
                    eff_proj_id = (
                        (q.project_id.id if q.project_id else False)
                        or (loc.project_id.id if loc.project_id else False)
                        or (wh.project_id.id if wh and wh.project_id else False)
                    )
                    bucket = grouped.setdefault(eff_proj_id, {'qty': 0.0, 'reserved': 0.0})
                    bucket['qty'] += q.quantity
                    bucket['reserved'] += q.reserved_quantity

                # Display Free Stock (False) first, then allocated project buckets
                sorted_keys = sorted(grouped.keys(), key=lambda pid: (0 if not pid else 1, pid or 0))
                for eff_proj_id in sorted_keys:
                    qty = grouped[eff_proj_id]['qty']
                    reserved = grouped[eff_proj_id]['reserved']
                    if qty > 0 or reserved > 0:
                        lines.append((0, 0, {
                            'product_id': prod.id,
                            'location_id': loc.id,
                            'warehouse_id': wh.id if wh else False,
                            'warehouse_type': wh.warehouse_type if wh else 'other',
                            'project_id': eff_proj_id,
                            'stock_type': 'project_allocated' if eff_proj_id else 'free_stock',
                            'quantity': qty,
                            'reserved_quantity': reserved,
                            'net_available_qty': qty - reserved,
                        }))
        return lines

    @api.depends('product_id', 'company_id')
    def _compute_availability_lines(self):
        for wiz in self:
            if not wiz.product_id:
                wiz.line_ids = [(5, 0, 0)]
                continue
            wiz.line_ids = wiz._build_availability_lines_for_products(
                wiz.product_id, wiz.company_id or self.env.company
            )

    @api.depends('line_ids')
    def _compute_totals(self):
        for wiz in self:
            central_lines = wiz.line_ids.filtered(lambda l: l.warehouse_type == 'central')
            site_lines = wiz.line_ids.filtered(lambda l: l.warehouse_type == 'project_site')
            free_lines = wiz.line_ids.filtered(lambda l: not l.project_id)
            proj_lines = wiz.line_ids.filtered(lambda l: bool(l.project_id))
            wiz.total_central_on_hand = sum(central_lines.mapped('quantity'))
            wiz.total_site_on_hand = sum(site_lines.mapped('quantity'))
            wiz.total_free_on_hand = sum(free_lines.mapped('quantity'))
            wiz.total_project_on_hand = sum(proj_lines.mapped('quantity'))
            wiz.total_net_available = sum(wiz.line_ids.mapped('net_available_qty'))


class ContractorStockAvailabilityLine(models.TransientModel):
    _name = 'contractor.stock.availability.line'
    _description = 'Stock Availability Line per Store'

    wizard_id = fields.Many2one(
        'contractor.stock.availability.wizard',
        string='Wizard Reference',
        ondelete='cascade',
    )
    product_id = fields.Many2one(
        'product.product',
        string='Product',
        required=True,
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Warehouse',
    )
    location_id = fields.Many2one(
        'stock.location',
        string='Storage Location',
        required=True,
    )
    warehouse_type = fields.Selection([
        ('central', 'Central Store'),
        ('project_site', 'Project Site Store'),
        ('other', 'Other'),
    ], string='Store Type')
    project_id = fields.Many2one(
        'project.project',
        string='Assigned Project',
    )
    stock_type = fields.Selection([
        ('free_stock', 'Free Stock'),
        ('project_allocated', 'Project-Allocated Stock'),
    ], string='Allocation Type')
    quantity = fields.Float(
        string='On Hand Qty',
    )
    reserved_quantity = fields.Float(
        string='Reserved Qty',
    )
    net_available_qty = fields.Float(
        string='Net Available Qty',
    )


# =========================================================================
# 3. Inter-Project Stock Transfer Note (STN)
# =========================================================================
class ContractorStockTransferNote(models.Model):
    _name = 'contractor.stock.transfer.note'
    _description = 'Inter-Project Stock Transfer Note (STN)'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.version.mixin', 'contractor.reason.mixin', 'contractor.no.delete.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='STN Number',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New STN'),
        index=True,
    )
    donor_project_id = fields.Many2one(
        'project.project',
        string='Donor Project',
        required=True,
        tracking=True,
        index=True,
    )
    donor_location_id = fields.Many2one(
        'stock.location',
        string='Donor Location',
        domain="[('usage', '=', 'internal')]",
        required=True,
        tracking=True,
    )
    receiving_project_id = fields.Many2one(
        'project.project',
        string='Receiving Project',
        required=True,
        tracking=True,
        index=True,
    )
    receiving_location_id = fields.Many2one(
        'stock.location',
        string='Receiving Location',
        domain="[('usage', '=', 'internal')]",
        required=True,
        tracking=True,
    )
    donor_engineer_id = fields.Many2one(
        'res.users',
        string='Donor Site Engineer / In-Charge',
        tracking=True,
        help='Site engineer or supervisor responsible for releasing materials from donor project site.',
    )
    requester_id = fields.Many2one(
        'res.users',
        string='Requested By',
        default=lambda self: self.env.user,
        readonly=True,
    )
    manager_id = fields.Many2one(
        'res.users',
        string='Approved By Manager',
        readonly=True,
        copy=False,
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
    picking_id = fields.Many2one(
        'stock.picking',
        string='Stock Picking Transfer',
        readonly=True,
        copy=False,
    )
    account_move_id = fields.Many2one(
        'account.move',
        string='Analytic Cost Reallocation Journal Entry',
        readonly=True,
        copy=False,
    )
    state = fields.Selection([
        ('draft', 'Draft STN'),
        ('pending_donor_approval', 'Pending Donor Engineer Approval'),
        ('donor_approved', 'Donor Approved / Pending Manager'),
        ('approved', 'Manager Approved'),
        ('transferred', 'Transferred & Executed'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    transfer_date = fields.Datetime(
        string='Transfer Execution Date',
        readonly=True,
        copy=False,
    )
    reason = fields.Text(
        string='Transfer Justification',
        required=True,
        tracking=True,
        help='Reason for transferring stock between projects (surplus inventory utilization, emergency substitution, etc.).',
    )
    line_ids = fields.One2many(
        'contractor.stock.transfer.note.line',
        'stn_id',
        string='Transfer Lines',
        copy=True,
    )
    total_cost = fields.Monetary(
        string='Total Transfer Cost Value',
        currency_field='currency_id',
        compute='_compute_total_cost',
        store=True,
    )

    @api.constrains('donor_project_id', 'receiving_project_id')
    def _check_locations(self):
        for stn in self:
            if stn.donor_project_id and stn.receiving_project_id and stn.donor_project_id == stn.receiving_project_id:
                raise ValidationError(_("Donor Project and Receiving Project must be different for an Inter-Project Stock Transfer."))

    @api.depends('line_ids.subtotal_cost')
    def _compute_total_cost(self):
        for stn in self:
            stn.total_cost = sum(stn.line_ids.mapped('subtotal_cost'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New STN')) == _('New STN'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.stock.transfer.note') or _('New STN')
        return super().create(vals_list)

    def _get_donor_quant_domain(self, product_id):
        self.ensure_one()
        return [
            '&',
            '&', ('product_id', '=', product_id), ('location_id', '=', self.donor_location_id.id),
            '|',
            ('project_id', '=', self.donor_project_id.id),
            '&', ('project_id', '=', False), ('location_id.project_id', '=', self.donor_project_id.id),
        ]

    def action_submit(self):
        """Validates STN lines and requests approval from Donor Site Engineer."""
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Please add at least one material line to transfer."))

        for line in self.line_ids:
            if line.qty <= 0:
                raise UserError(_("Transfer quantity must be strictly greater than zero for product '%s'.") % line.product_id.display_name)
            # Check availability for donor_project_id at donor_location_id (supports shared single location)
            quant = self.env['stock.quant'].search(self._get_donor_quant_domain(line.product_id.id))
            avail = sum(quant.mapped('quantity')) - sum(quant.mapped('reserved_quantity'))
            if line.qty > avail:
                raise UserError(_(
                    "Insufficient Stock for Donor Project '%s':\n\n"
                    "Product '%s' has only %.2f units available at location '%s' (Requested: %.2f)."
                ) % (
                    self.donor_project_id.display_name, line.product_id.display_name,
                    avail, self.donor_location_id.display_name, line.qty,
                ))

        self.state = 'pending_donor_approval'

        # Activity to Donor Engineer
        target_user = self.donor_engineer_id or self.donor_project_id.user_id
        if target_user:
            self.env['mail.activity'].create({
                'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                'res_model_id': self.env['ir.model']._get_id('contractor.stock.transfer.note'),
                'res_id': self.id,
                'user_id': target_user.id,
                'summary': f"Approve Stock Release: {self.name} to {self.receiving_project_id.name}",
                'note': _("Transfer request of %s items valued at %s %s from your project.") % (
                    len(self.line_ids), self.currency_id.symbol or '', self.total_cost
                ),
                'date_deadline': fields.Date.context_today(self),
            })

        self.message_post(
            body=Markup(_("<strong>STN Submitted:</strong> Material transfer submitted for Donor Site Engineer approval.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_donor_approve(self):
        """Donor Site Engineer approves material release; notifies Contractor Manager."""
        self.ensure_one()
        # Ensure user has authority
        if self.donor_engineer_id and self.env.user != self.donor_engineer_id:
            if not self.env.user.has_group('contractor_core.group_contractor_manager'):
                raise UserError(_("Only the assigned Donor Site Engineer (%s) or a Contractor Manager can approve material release.") % self.donor_engineer_id.name)

        self.state = 'donor_approved'

        # Notify Contractor Manager
        group_mgr = self.env.ref('contractor_core.group_contractor_manager', raise_if_not_found=False)
        if group_mgr:
            for user in (group_mgr.all_user_ids | group_mgr.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('contractor.stock.transfer.note'),
                    'res_id': self.id,
                    'user_id': user.id,
                    'summary': f"Final Approval Required: STN {self.name}",
                    'note': _("Donor site engineer approved transfer from %s to %s. Awaiting Contractor Manager authorization.") % (
                        self.donor_project_id.name, self.receiving_project_id.name
                    ),
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_("<strong>Donor Approval Granted:</strong> Released by %s. Forwarded to Contractor Manager.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_manager_approve(self):
        """Contractor Manager gives final administrative clearance."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize inter-project transfers."))

        self.write({
            'state': 'approved',
            'manager_id': self.env.user.id,
        })
        self.message_post(
            body=Markup(_("<strong>Manager Approval Granted:</strong> STN authorized by %s. Ready for execution.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_execute_transfer(self):
        """Executes the internal stock picking and transfers project analytic cost at original purchase cost."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_storekeeper') or self.env.user.has_group('contractor_core.group_contractor_procurement_officer')):
            raise AccessError(_("Access Denied: Only Storekeepers, Procurement Officers, or Managers can execute Stock Transfer Notes."))
        if self.state != 'approved':
            raise UserError(_("Only approved STNs can be executed."))
        if self.picking_id or self.account_move_id:
            raise UserError(_("Stock transfer and accounting shift have already been executed for this STN."))

        # 1. Find or determine picking type
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'internal'),
            ('warehouse_id', 'in', [self.donor_location_id.warehouse_id.id, self.receiving_location_id.warehouse_id.id, False]),
            ('company_id', '=', self.company_id.id),
        ], limit=1)

        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'internal'),
                ('company_id', '=', self.company_id.id),
            ], limit=1)

        if not picking_type:
            raise UserError(_("No Internal Transfer operation type found in company '%s'.") % self.company_id.name)

        # 2. Build stock move values (handles both cross-location and same-location project reallocation)
        moves = []
        for line in self.line_ids:
            moves.append((0, 0, {
                'description_picking': f"STN Transfer: {line.product_id.display_name} ({self.donor_project_id.name} -> {self.receiving_project_id.name})",
                'product_id': line.product_id.id,
                'product_uom': line.product_uom_id.id,
                'product_uom_qty': line.qty,
                'location_id': self.donor_location_id.id,
                'location_dest_id': self.receiving_location_id.id,
                'company_id': self.company_id.id,
                'source_project_id': self.donor_project_id.id,
                'restrict_source_project': True,
                'project_id': self.receiving_project_id.id,
            }))

        picking_vals = {
            'picking_type_id': picking_type.id,
            'location_id': self.donor_location_id.id,
            'location_dest_id': self.receiving_location_id.id,
            'origin': self.name,
            'project_id': self.receiving_project_id.id,
            'move_ids': moves,
        }
        picking = self.env['stock.picking'].create(picking_vals)
        picking.action_confirm()
        picking.action_assign()

        # Set quantities done and ensure source project attribution stays pinned for shared locations
        for move in picking.move_ids:
            for ml in move.move_line_ids:
                if not ml.source_quant_pinned or ml.source_project_id != self.donor_project_id:
                    ml.write({
                        'source_project_id': self.donor_project_id.id,
                        'source_quant_pinned': True,
                    })
            move.quantity = move.product_uom_qty
            move.picked = True

        # Validate picking
        picking.button_validate()

        # 3. Create Analytic Cost Reallocation Journal Entry if analytic accounts exist
        move_rec = self._create_analytic_cost_transfer_entry()

        self.write({
            'state': 'transferred',
            'transfer_date': fields.Datetime.now(),
            'picking_id': picking.id,
            'account_move_id': move_rec.id if move_rec else False,
        })

        self.message_post(
            body=Markup(_("<strong>Transfer Executed:</strong> Stock picking <a href='#' data-oe-model='stock.picking' data-oe-id='%s'>%s</a> processed. Cost shifted at original purchase price.")) % (
                picking.id, picking.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def _create_analytic_cost_transfer_entry(self):
        """Creates a journal entry to shift material cost from donor project analytic account to receiving project."""
        donor_analytic = self.donor_project_id.account_id
        if not donor_analytic and hasattr(self.donor_project_id, '_get_or_create_project_analytic_account'):
            donor_analytic = self.donor_project_id._get_or_create_project_analytic_account()
        recv_analytic = self.receiving_project_id.account_id
        if not recv_analytic and hasattr(self.receiving_project_id, '_get_or_create_project_analytic_account'):
            recv_analytic = self.receiving_project_id._get_or_create_project_analytic_account()

        if not donor_analytic or not recv_analytic:
            return False

        # Find general journal for miscellaneous operations
        misc_journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)

        if not misc_journal:
            return False

        # Find default expense account
        expense_account = self.company_id.default_expense_account_id or self.env['account.account'].search([
            ('internal_group', '=', 'expense'),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1)

        if not expense_account:
            return False

        lines = [
            (0, 0, {
                'name': f"STN {self.name}: Stock Transferred In to {self.receiving_project_id.name}",
                'account_id': expense_account.id,
                'debit': self.total_cost,
                'credit': 0.0,
                'analytic_distribution': {str(recv_analytic.id): 100.0},
            }),
            (0, 0, {
                'name': f"STN {self.name}: Stock Transferred Out from {self.donor_project_id.name}",
                'account_id': expense_account.id,
                'debit': 0.0,
                'credit': self.total_cost,
                'analytic_distribution': {str(donor_analytic.id): 100.0},
            }),
        ]

        move = self.env['account.move'].create({
            'journal_id': misc_journal.id,
            'date': fields.Date.context_today(self),
            'ref': f"STN {self.name} Material Cost Reallocation",
            'move_type': 'entry',
            'line_ids': lines,
        })
        move.action_post()
        return move

    def action_decline(self):
        self.ensure_one()
        return self.action_open_reason_wizard(action_type='decline', target_state='declined')

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        if any(rec.state == 'transferred' for rec in self) and not set(vals.keys()).issubset(allowed_meta_fields):
            raise UserError(_("Completed Stock Transfer Notes are locked and cannot be modified."))
        return super().write(vals)


class ContractorStockTransferNoteLine(models.Model):
    _name = 'contractor.stock.transfer.note.line'
    _description = 'Inter-Project Stock Transfer Note Line'

    stn_id = fields.Many2one(
        'contractor.stock.transfer.note',
        string='STN Reference',
        required=True,
        ondelete='cascade',
    )
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
    qty = fields.Float(
        string='Transfer Qty',
        required=True,
        default=1.0,
    )
    available_qty = fields.Float(
        string='Donor Net Available',
        compute='_compute_available_qty',
    )
    original_purchase_cost = fields.Monetary(
        string='Original Purchase Unit Cost',
        currency_field='currency_id',
        compute='_compute_cost',
        store=True,
        readonly=False,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='stn_id.currency_id',
        readonly=True,
    )
    subtotal_cost = fields.Monetary(
        string='Subtotal Cost',
        currency_field='currency_id',
        compute='_compute_subtotal',
        store=True,
    )

    @api.depends('product_id', 'stn_id.donor_location_id', 'stn_id.donor_project_id')
    def _compute_available_qty(self):
        for line in self:
            if line.product_id and line.stn_id.donor_location_id and line.stn_id.donor_project_id:
                quants = self.env['stock.quant'].search(
                    line.stn_id._get_donor_quant_domain(line.product_id.id)
                )
                line.available_qty = sum(quants.mapped('quantity')) - sum(quants.mapped('reserved_quantity'))
            elif line.product_id and line.stn_id.donor_location_id:
                quants = self.env['stock.quant'].search([
                    ('product_id', '=', line.product_id.id),
                    ('location_id', '=', line.stn_id.donor_location_id.id),
                ])
                line.available_qty = sum(quants.mapped('quantity')) - sum(quants.mapped('reserved_quantity'))
            else:
                line.available_qty = 0.0

    @api.depends('product_id')
    def _compute_cost(self):
        for line in self:
            if not line.original_purchase_cost and line.product_id:
                # Use standard_price (cost) as default original purchase cost
                line.original_purchase_cost = line.product_id.standard_price or 0.0

    @api.depends('qty', 'original_purchase_cost')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal_cost = (line.qty or 0.0) * (line.original_purchase_cost or 0.0)

    def write(self, vals):
        if any(line.stn_id.state == 'transferred' for line in self):
            raise UserError(_("Lines of a completed Stock Transfer Note cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if any(line.stn_id.state == 'transferred' for line in self):
            raise UserError(_("Lines of a completed Stock Transfer Note cannot be deleted."))
        return super().unlink()

