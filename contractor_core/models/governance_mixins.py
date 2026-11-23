# -*- coding: utf-8 -*-
import json
import sys
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.orm.models import MetaModel


# =========================================================================
# 1. Project Governance Mixin
# =========================================================================
class ContractorProjectMixin(models.AbstractModel):
    _name = 'contractor.project.mixin'
    _description = 'Contractor Project Code & Analytic Alignment Governance Mixin'

    project_id = fields.Many2one(
        'project.project',
        string='Project Code',
        index=True,
        help='Project Code linking this operational record to the master project and its analytic accounting account.',
    )

    def _get_or_create_project_analytic_account(self):
        """Ensures the project has an associated analytic account."""
        self.ensure_one()
        if not self.project_id:
            return False
        return self.project_id._get_or_create_project_analytic_account()

    def _sync_analytic_distribution_from_project(self):
        """Auto-syncs analytic_distribution with the project's analytic account."""
        for rec in self:
            if rec.project_id:
                analytic_account = rec._get_or_create_project_analytic_account()
                if analytic_account and hasattr(rec, 'analytic_distribution'):
                    rec.analytic_distribution = {str(analytic_account.id): 100.0}

    @api.onchange('project_id')
    def _onchange_project_id_sync_analytic(self):
        self._sync_analytic_distribution_from_project()


# =========================================================================
# 2. Immutable No-Delete Governance Mixin
# =========================================================================
class ContractorNoDeleteMixin(models.AbstractModel):
    _name = 'contractor.no.delete.mixin'
    _description = 'Contractor Platform Governance Immutable No-Delete Mixin'

    def unlink(self):
        """Overrides unlink to strictly prohibit deletion of records once created.
        Instructs users to use Cancel, Decline, or Withdraw with mandatory reason.
        Only the superuser (self.env.su) is permitted to bypass this rule during system maintenance.
        """
        if not self.env.su:
            record_desc = self._description or self._name
            raise UserError(_(
                "Deletion of %s records is strictly prohibited by Contractor Platform Governance.\n\n"
                "To maintain full auditability and compliance, records cannot be unlinked once created.\n"
                "Please use Cancel, Decline, or Withdraw with a documented justification instead."
            ) % record_desc)
        return super().unlink()


# =========================================================================
# 3. Version History & Snapshot Mixin
# =========================================================================
class ContractorVersionMixin(models.AbstractModel):
    _name = 'contractor.version.mixin'
    _description = 'Contractor Document Version History & Snapshot Mixin'

    version_number = fields.Integer(
        string='Version Number',
        default=1,
        copy=False,
        readonly=True,
        help='Sequential version index of this document.',
    )
    version_history_ids = fields.One2many(
        'contractor.document.version',
        compute='_compute_version_history_ids',
        string='Version History Snapshots',
        readonly=True,
    )
    version_count = fields.Integer(
        string='Version Count',
        compute='_compute_version_history_ids',
        help='Total number of saved version history snapshots.',
    )

    def _compute_version_history_ids(self):
        version_model = self.env['contractor.document.version']
        for rec in self:
            real_id = rec._origin.id if rec._origin else (rec.id if isinstance(rec.id, int) else False)
            if real_id:
                res_ids = [real_id]
                if 'parent_quote_id' in rec._fields:
                    curr = rec.parent_quote_id
                    visited = {real_id}
                    while curr and curr.id and curr.id not in visited:
                        visited.add(curr.id)
                        res_ids.append(curr.id)
                        curr = curr.parent_quote_id
                snapshots = version_model.search([
                    ('res_model', '=', rec._name),
                    ('res_id', 'in', res_ids),
                ], order='version_number desc, id desc')
                rec.version_history_ids = snapshots
                rec.version_count = len(snapshots)
            else:
                rec.version_history_ids = False
                rec.version_count = 0

    def action_view_versions(self):
        self.ensure_one()
        return {
            'name': _('Version Snapshots'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.document.version',
            'view_mode': 'list,form',
            'domain': [('res_model', '=', self._name), ('res_id', '=', self.id)],
            'context': {'default_res_model': self._name, 'default_res_id': self.id},
        }

    def _get_record_attachments(self):
        """Collects all attachments linked directly, via Many2many fields, or via chatter messages on this record."""
        self.ensure_one()
        Attachment = self.env['ir.attachment'].sudo()
        if not self.id or not isinstance(self.id, int):
            return Attachment

        direct_atts = Attachment.search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('res_field', '=', False),
        ])

        m2m_atts = Attachment
        for fname, field in self._fields.items():
            if field.type == 'many2many' and field.comodel_name == 'ir.attachment' and not field.compute:
                m2m_atts |= getattr(self, fname).sudo()

        msg_atts = Attachment
        if 'message_ids' in self._fields:
            msg_atts = self.sudo().message_ids.mapped('attachment_ids').filtered(
                lambda a: a.res_model != 'contractor.document.version'
            )

        return (direct_atts | m2m_atts | msg_atts).filtered(
            lambda a: a.res_model != 'contractor.document.version'
        )

    def _move_attachments_to_version(self, version_record, clear_from_record=True):
        """Links all attachments on this record to version_record and optionally removes them from self."""
        self.ensure_one()
        if not version_record:
            return
        attachments = self._get_record_attachments()
        if attachments:
            version_record.sudo().write({'attachment_ids': [(4, att.id) for att in attachments]})

        if clear_from_record:
            clear_vals = {}
            for fname, field in self._fields.items():
                if field.type == 'many2many' and field.comodel_name == 'ir.attachment' and not field.compute:
                    if getattr(self, fname):
                        clear_vals[fname] = [(5, 0, 0)]
            if self._name == 'sale.order':
                for acc_field in ('acceptance_doc_type', 'acceptance_ref', 'acceptance_date'):
                    if acc_field in self._fields and getattr(self, acc_field):
                        clear_vals[acc_field] = False
            if clear_vals:
                self.with_context(skip_estimation_sync=True).write(clear_vals)

            if 'message_ids' in self._fields and attachments:
                msgs = self.sudo().message_ids.filtered(lambda m: m.attachment_ids & attachments)
                for msg in msgs:
                    to_remove = msg.attachment_ids & attachments
                    msg.write({'attachment_ids': [(3, att.id) for att in to_remove]})

            remaining_direct = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', self._name),
                ('res_id', '=', self.id),
                ('res_field', '=', False),
            ])
            all_to_move = attachments | remaining_direct
            if all_to_move:
                version_record.sudo().write({'attachment_ids': [(4, att.id) for att in all_to_move]})
                all_to_move.sudo().write({
                    'res_model': 'contractor.document.version',
                    'res_id': version_record.id,
                })

    def _create_version_snapshot(self, reason, editor_user=None, move_attachments=False):
        """Serializes current record attributes and child line values into an immutable
        read-only JSON/HTML snapshot record (v1, v2, v3...) without overwriting previous versions.
        """
        self.ensure_one()
        editor = editor_user or self.env.user
        current_v = self.version_number or 1
        record_attachments = self._get_record_attachments()

        # Extract financial summary values dynamically
        total_val = (
            getattr(self, 'proposed_quotation_value', None)
            or getattr(self, 'amount_total', None)
            or getattr(self, 'total_value', None)
            or getattr(self, 'price_total', None)
            or 0.0
        )
        cost_val = (
            getattr(self, 'total_estimated_cost', None)
            or getattr(self, 'total_cost', None)
            or getattr(self, 'cost_amount', None)
            or getattr(self, 'amount_untaxed', None)
            or 0.0
        )
        margin_val = (
            getattr(self, 'effective_margin_pct', None)
            or getattr(self, 'margin_pct', None)
            or getattr(self, 'margin', None)
            or (((total_val - cost_val) / total_val * 100.0) if total_val else 0.0)
        )
        currency = (
            getattr(self, 'currency_id', False)
            or self.env.company.currency_id
        )
        approval_state = (
            getattr(self, 'contractor_quote_state', False)
            or getattr(self, 'contractor_state', False)
            or getattr(self, 'state', False)
            or 'draft'
        )

        # Serialize record fields to dictionary
        snapshot_dict = {
            'metadata': {
                'model': self._name,
                'res_id': self.id,
                'display_name': self.display_name,
                'version_number': current_v,
                'editor': editor.name,
                'timestamp': fields.Datetime.now().isoformat(),
                'reason': reason,
                'total_val': float(total_val),
                'cost_val': float(cost_val),
                'margin_val': float(margin_val),
                'approval_state': approval_state,
            },
            'fields': {},
            'lines': [],
            'attachments': [
                {'id': a.id, 'name': a.name, 'mimetype': a.mimetype}
                for a in record_attachments
            ],
        }

        # Collect scalar fields via sudo so cross-module group-restricted fields archive safely
        sudo_rec = self.sudo()
        for fname, field in sudo_rec._fields.items():
            if field.type in ('char', 'integer', 'float', 'monetary', 'boolean', 'selection', 'date', 'datetime', 'text'):
                snapshot_dict['fields'][fname] = getattr(sudo_rec, fname)
            elif field.type == 'many2one':
                m2o_val = getattr(sudo_rec, fname)
                snapshot_dict['fields'][fname] = {'id': m2o_val.id, 'name': m2o_val.display_name} if m2o_val else False

        # Collect line items if standard lines exist
        lines_rel = (
            getattr(sudo_rec, 'order_line', None)
            or getattr(sudo_rec, 'line_ids', None)
            or getattr(sudo_rec, 'move_ids', None)
        )
        if lines_rel:
            for line in lines_rel:
                line_data = {'id': line.id, 'display_name': line.display_name}
                for l_fname in ('product_id', 'name', 'product_uom_qty', 'quantity', 'price_unit', 'price_subtotal', 'price_total'):
                    if l_fname in line._fields:
                        val = getattr(line, l_fname)
                        line_data[l_fname] = val.display_name if hasattr(val, 'display_name') else val
                snapshot_dict['lines'].append(line_data)

        # Build clean HTML summary table
        currency_symbol = currency.symbol if currency else ''
        html_lines = []
        for l in snapshot_dict['lines']:
            p_name = l.get('name') or (l.get('product_id') and l['product_id']) or 'Line Item'
            qty = l.get('product_uom_qty') or l.get('quantity') or 0.0
            price = l.get('price_unit') or 0.0
            subtot = l.get('price_subtotal') or l.get('price_total') or 0.0
            html_lines.append(
                f"<tr><td>{p_name}</td><td class='text-end'>{qty}</td><td class='text-end'>{price:,.2f}</td><td class='text-end'>{subtot:,.2f}</td></tr>"
            )

        lines_table = ""
        if html_lines:
            lines_table = f"""
            <table class="table table-sm table-bordered mt-2">
                <thead>
                    <tr class="table-light">
                        <th>Description</th><th class="text-end">Qty</th><th class="text-end">Unit Price</th><th class="text-end">Subtotal</th>
                    </tr>
                </thead>
                <tbody>{''.join(html_lines)}</tbody>
            </table>
            """

        snapshot_html = f"""
        <div class="contractor-version-card p-3 border rounded">
            <h5>Version Snapshot v{current_v}</h5>
            <div class="row">
                <div class="col-6">
                    <p><strong>Editor:</strong> {editor.name}<br/>
                    <strong>Date:</strong> {fields.Datetime.now()}<br/>
                    <strong>State:</strong> <span class="badge bg-secondary">{approval_state}</span></p>
                </div>
                <div class="col-6 text-end">
                    <p><strong>Total Value:</strong> {currency_symbol} {total_val:,.2f}<br/>
                    <strong>Total Cost:</strong> {currency_symbol} {cost_val:,.2f}<br/>
                    <strong>Gross Margin:</strong> {margin_val:.2f}%</p>
                </div>
            </div>
            <div class="alert alert-info py-2"><strong>Revision Reason:</strong> {reason}</div>
            {lines_table}
        </div>
        """

        version_record = self.env['contractor.document.version'].sudo().create({
            'name': f"v{current_v} - {self.display_name or self.id}",
            'res_model': self._name,
            'res_id': self.id,
            'version_number': current_v,
            'editor_id': editor.id,
            'timestamp': fields.Datetime.now(),
            'reason': reason,
            'cost': cost_val,
            'margin': margin_val,
            'total_value': total_val,
            'approval_state': approval_state,
            'currency_id': currency.id if currency else False,
            'snapshot_data': json.dumps(snapshot_dict, default=str),
            'snapshot_html': Markup(snapshot_html),
            'attachment_ids': [(6, 0, record_attachments.ids)],
        })

        # Advance version counter on the record
        self.version_number = current_v + 1

        if hasattr(self, 'message_post'):
            self.message_post(
                body=Markup(_(
                    "<strong>Version Snapshot v%s Created</strong> by %s.<br/>"
                    "<strong>Reason:</strong> %s"
                )) % (current_v, editor.name, reason),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )

        if move_attachments:
            self._move_attachments_to_version(version_record, clear_from_record=True)

        return version_record


# =========================================================================
# 4. Mandatory Action Reason Governance Mixin
# =========================================================================
class ContractorReasonMixin(models.AbstractModel):
    _name = 'contractor.reason.mixin'
    _description = 'Contractor Mandatory Action Reason Governance Mixin'

    last_action_reason = fields.Text(
        string='Last Action Reason',
        readonly=True,
        copy=False,
        help='Mandatory justification logged for the most recent state change, return, or rejection.',
    )
    last_action_by_id = fields.Many2one(
        'res.users',
        string='Last Action By',
        readonly=True,
        copy=False,
    )
    last_action_date = fields.Datetime(
        string='Last Action Date',
        readonly=True,
        copy=False,
    )

    def action_open_reason_wizard(self, action_type='decline', target_state=None, callback_method=None):
        """Opens the modal contractor.reason.wizard prompting the user for a mandatory reason."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Document Governance: %s') % (action_type.title() if action_type else 'Action'),
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': action_type,
                'default_target_state': target_state,
                'default_callback_method': callback_method,
            },
        }


# =========================================================================
# 5. Effective Date Rate Mixin
# =========================================================================
class ContractorEffectiveDateMixin(models.AbstractModel):
    _name = 'contractor.effective.date.mixin'
    _description = 'Contractor Historical Effective Date Labor Rate Mixin'

    @api.model
    def get_rate_on_date(self, employee_id, target_date=None):
        """Helper to retrieve effective hourly labor rate on any given target date."""
        return self.env['contractor.dated.rate.history'].sudo().get_rate_on_date(employee_id, target_date)


# =========================================================================
# 5B. Concurrent Workflow State Lock Mixins
# =========================================================================
class ContractorStateLockMixin(models.AbstractModel):
    """Prevents stale-form concurrent edits after a record leaves editable state(s)."""
    _name = 'contractor.state.lock.mixin'
    _description = 'Contractor Concurrent Workflow State Edit Lock Mixin'

    _state_lock_field = 'state'
    _state_lock_editable_states = ('draft',)

    @api.model
    def _get_state_lock_allowed_fields(self):
        return {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
            'version_number',
            'escalated_to_md', 'escalation_datetime', 'escalation_notified',
            'late_amendment_requested', 'late_amendment_reason',
            'late_amendment_approved', 'late_amendment_approved_by',
            'late_amendment_approved_date',
            'is_entry_locked', 'is_locked',
        }

    def _get_state_lock_extra_allowed_fields(self, record):
        """Override in subclasses to allow specific fields in specific post-draft states."""
        return ()

    def _get_state_lock_label(self, rec):
        field_name = self._state_lock_field
        val = rec[field_name]
        field = rec._fields.get(field_name)
        if field and field.type == 'selection':
            selection = field._description_selection(self.env)
            return dict(selection).get(val, val)
        return val

    def _ensure_state(self, allowed_states, action_label=None):
        """Guard workflow action buttons against stale forms or duplicate clicks."""
        if isinstance(allowed_states, str):
            allowed_states = (allowed_states,)
        for rec in self:
            if rec[self._state_lock_field] not in allowed_states:
                raise UserError(_(
                    "Cannot %(action)s on '%(name)s' because its current status is '%(state)s'. "
                    "Another user may have already updated or approved it — please reload the page."
                ) % {
                    'action': action_label or _("perform this action"),
                    'name': rec.display_name,
                    'state': self._get_state_lock_label(rec),
                })

    def write(self, vals):
        if not self or not vals or self.env.su or self.env.context.get('bypass_state_lock'):
            return super().write(vals)
        write_keys = set(vals.keys())
        base_allowed = self._get_state_lock_allowed_fields()
        for rec in self:
            rec_state = rec[self._state_lock_field]
            if rec_state in self._state_lock_editable_states:
                continue
            rec_allowed = base_allowed | set(self._get_state_lock_extra_allowed_fields(rec))
            if not write_keys.issubset(rec_allowed):
                raise UserError(_(
                    "Record '%(name)s' is currently in '%(state)s' status and can no longer be edited. "
                    "Another user may have updated or approved it while you had this form open. "
                    "Please reload the page to see the latest status."
                ) % {
                    'name': rec.display_name,
                    'state': self._get_state_lock_label(rec),
                })
        return super().write(vals)


class ContractorLineStateLockMixin(models.AbstractModel):
    """Prevents stale-form line create/write/unlink once the parent leaves editable state(s)."""
    _name = 'contractor.line.state.lock.mixin'
    _description = 'Contractor Child Line State Edit Lock Mixin'

    _line_parent_field = False
    _line_editable_states = ('draft',)

    @api.model
    def _get_line_lock_allowed_fields(self):
        return set()

    def _get_line_parent_state_label(self, parent):
        state_field_name = getattr(parent, '_state_lock_field', 'state')
        val = parent[state_field_name]
        field = parent._fields.get(state_field_name)
        if field and field.type == 'selection':
            return dict(field._description_selection(self.env)).get(val, val)
        return val

    def _check_parent_editable_state(self, parents, action_label):
        if self.env.su or self.env.context.get('bypass_state_lock'):
            return
        for parent in parents:
            if not parent:
                continue
            state_field_name = getattr(parent, '_state_lock_field', 'state')
            editable = getattr(self, '_line_editable_states', ('draft',))
            if parent[state_field_name] not in editable:
                raise UserError(_(
                    "Cannot %(action)s '%(name)s' because its current status is '%(state)s'. "
                    "Another user may have updated or approved it while you had this form open. "
                    "Please reload the page to see the latest status."
                ) % {
                    'action': action_label,
                    'name': parent.display_name,
                    'state': self._get_line_parent_state_label(parent),
                })

    @api.model_create_multi
    def create(self, vals_list):
        if self._line_parent_field and not self.env.su and not self.env.context.get('bypass_state_lock'):
            parent_ids = {vals[self._line_parent_field] for vals in vals_list if vals.get(self._line_parent_field)}
            if parent_ids:
                comodel = self.env[self._fields[self._line_parent_field].comodel_name]
                self._check_parent_editable_state(comodel.browse(parent_ids).exists(), _("add lines to"))
        return super().create(vals_list)

    def write(self, vals):
        if self and self._line_parent_field and vals and not self.env.su and not self.env.context.get('bypass_state_lock'):
            if not set(vals.keys()).issubset(self._get_line_lock_allowed_fields()):
                parents = self.mapped(self._line_parent_field)
                if vals.get(self._line_parent_field):
                    comodel = self.env[self._fields[self._line_parent_field].comodel_name]
                    parents |= comodel.browse(vals[self._line_parent_field]).exists()
                self._check_parent_editable_state(parents, _("modify lines of"))
        return super().write(vals)

    def unlink(self):
        if self and self._line_parent_field and not self.env.su and not self.env.context.get('bypass_state_lock'):
            parents = self.mapped(self._line_parent_field)
            self._check_parent_editable_state(parents, _("delete lines from"))
        return super().unlink()


# =========================================================================
# 6. Model Enforcement Across Required Models
# =========================================================================

# --- 6.1 Stock Picking Enforcement ---
class StockPicking(models.Model):
    _name = 'stock.picking'
    _inherit = ['stock.picking', 'contractor.project.mixin', 'contractor.no.delete.mixin', 'contractor.reason.mixin']

    project_id = fields.Many2one(
        'project.project',
        string='Project Code',
        required=False,
        index=True,
        help='Project Code required on project stock operations and material deliveries.',
    )
    is_central_stock_purchase = fields.Boolean(
        string='Central Store Stock Movement',
        default=False,
        tracking=True,
        help='Indicates receipts or movements into Central Warehouse stock without charging a specific project.',
    )

    @api.model
    def _is_unassigned_or_central_storage(self, location=None, warehouse=None):
        """Returns True if location/warehouse is central stock or general company storage without a project."""
        if location and getattr(location, 'project_id', False):
            return False
        wh = warehouse or (location.warehouse_id if location else False)
        if wh:
            if getattr(wh, 'project_id', False) or getattr(wh, 'warehouse_type', False) == 'project_site':
                return False
        return True

    def _is_central_warehouse_operation(self):
        self.ensure_one()
        if self.picking_type_code in ('incoming', 'internal'):
            return True
        pick_wh = self.picking_type_id.warehouse_id if self.picking_type_id else False
        return any(
            wh and getattr(wh, 'warehouse_type', False) == 'central'
            for wh in (
                self.location_dest_id.warehouse_id if self.location_dest_id else False,
                self.location_id.warehouse_id if self.location_id else False,
                pick_wh,
            )
        )

    @api.constrains('project_id', 'is_central_stock_purchase', 'picking_type_id', 'location_id', 'location_dest_id')
    def _check_project_id_required(self):
        for picking in self:
            if picking.project_id or picking.is_central_stock_purchase or picking._is_central_warehouse_operation():
                continue
            raise ValidationError(_(
                "Project Code is strictly required on Stock Picking '%s' unless it is a Central Store Stock Movement "
                "or operates on central/unassigned storage."
            ) % (picking.name or picking.display_name))

    @api.model
    def _resolve_project_id_from_origin(self, origin):
        if not origin:
            return False
        raw_tokens = [t.strip() for part in str(origin).split(',') for t in part.split('/') if t.strip()]
        tokens = []
        for tok in raw_tokens:
            tokens.append(tok)
            for prefix in ('Return of ', 'Retour de '):
                if tok.startswith(prefix):
                    tokens.append(tok[len(prefix):].strip())
        if not tokens:
            return False

        if 'sale.order' in self.env:
            so = self.env['sale.order'].sudo().search([('name', 'in', tokens), ('project_id', '!=', False)], limit=1)
            if so:
                return so.project_id.id
        if 'purchase.order' in self.env:
            po = self.env['purchase.order'].sudo().search([('name', 'in', tokens), ('project_id', '!=', False)], limit=1)
            if po:
                return po.project_id.id
        sp = self.env['stock.picking'].sudo().search([('name', 'in', tokens), ('project_id', '!=', False)], limit=1)
        if sp:
            return sp.project_id.id
        if 'contractor.material.requisition' in self.env:
            mr = self.env['contractor.material.requisition'].sudo().search([('name', 'in', tokens), ('project_id', '!=', False)], limit=1)
            if mr:
                return mr.project_id.id
        if 'contractor.stock.transfer.note' in self.env:
            stn = self.env['contractor.stock.transfer.note'].sudo().search([('name', 'in', tokens)], limit=1)
            if stn and (stn.receiving_project_id or stn.donor_project_id):
                return (stn.receiving_project_id or stn.donor_project_id).id
        if 'contractor.leftover.disposition' in self.env:
            lod = self.env['contractor.leftover.disposition'].sudo().search([('name', 'in', tokens)], limit=1)
            if lod and (lod.target_project_id or lod.project_id):
                return (lod.target_project_id or lod.project_id).id
        return False

    @api.model
    def _resolve_project_id_from_vals(self, vals):
        if vals.get('is_central_stock_purchase'):
            return False
        if vals.get('purchase_id') and 'purchase.order' in self.env:
            po = self.env['purchase.order'].sudo().browse(vals['purchase_id'])
            if getattr(po, 'is_central_stock_purchase', False):
                return False
        if vals.get('project_id'):
            return vals['project_id']
        if self.env.context.get('default_project_id'):
            return self.env.context['default_project_id']
        if vals.get('sale_id') and 'sale.order' in self.env:
            so = self.env['sale.order'].sudo().browse(vals['sale_id'])
            if so.project_id:
                return so.project_id.id
        if vals.get('purchase_id') and 'purchase.order' in self.env:
            po = self.env['purchase.order'].sudo().browse(vals['purchase_id'])
            if po.project_id:
                return po.project_id.id
        if vals.get('return_id'):
            ret_pick = self.env['stock.picking'].sudo().browse(vals['return_id'])
            if ret_pick.project_id:
                return ret_pick.project_id.id
        if vals.get('backorder_id'):
            bo_pick = self.env['stock.picking'].sudo().browse(vals['backorder_id'])
            if bo_pick.project_id:
                return bo_pick.project_id.id
        if vals.get('origin'):
            proj_id = self._resolve_project_id_from_origin(vals['origin'])
            if proj_id:
                return proj_id

        # Inspect move commands if provided
        for move_key in ('move_ids', 'move_ids_without_package'):
            for cmd in vals.get(move_key) or []:
                if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[0] == 0 and isinstance(cmd[2], dict):
                    m_vals = cmd[2]
                    if m_vals.get('project_id'):
                        return m_vals['project_id']
                    if m_vals.get('sale_line_id') and 'sale.order.line' in self.env:
                        sol = self.env['sale.order.line'].sudo().browse(m_vals['sale_line_id'])
                        if sol.order_id.project_id:
                            return sol.order_id.project_id.id
                    if m_vals.get('purchase_line_id') and 'purchase.order.line' in self.env:
                        pol = self.env['purchase.order.line'].sudo().browse(m_vals['purchase_line_id'])
                        if pol.order_id.project_id:
                            return pol.order_id.project_id.id
                elif isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == 4:
                    mv = self.env['stock.move'].sudo().browse(cmd[1])
                    if mv.project_id:
                        return mv.project_id.id

        # Inspect locations and picking type warehouse
        for loc_key in ('location_dest_id', 'location_id'):
            loc_id = vals.get(loc_key)
            if loc_id:
                loc = self.env['stock.location'].sudo().browse(loc_id)
                if getattr(loc, 'project_id', False):
                    return loc.project_id.id
                if loc.warehouse_id and getattr(loc.warehouse_id, 'project_id', False):
                    return loc.warehouse_id.project_id.id

        if vals.get('picking_type_id'):
            pt = self.env['stock.picking.type'].sudo().browse(vals['picking_type_id'])
            if pt.warehouse_id and getattr(pt.warehouse_id, 'project_id', False):
                return pt.warehouse_id.project_id.id

        return False

    @api.onchange('is_central_stock_purchase')
    def _onchange_is_central_stock_purchase_clear_project(self):
        if self.is_central_stock_purchase:
            self.project_id = False

    @api.onchange('picking_type_id', 'location_id', 'location_dest_id', 'origin', 'return_id', 'backorder_id')
    def _onchange_auto_populate_project_id(self):
        if self.is_central_stock_purchase or self.project_id:
            return
        if self.return_id and self.return_id.project_id:
            self.project_id = self.return_id.project_id
            return
        if self.backorder_id and self.backorder_id.project_id:
            self.project_id = self.backorder_id.project_id
            return
        if getattr(self, 'sale_id', False) and self.sale_id.project_id:
            self.project_id = self.sale_id.project_id
            return
        if getattr(self, 'purchase_id', False) and self.purchase_id.project_id:
            self.project_id = self.purchase_id.project_id
            return
        if self.origin:
            proj_id = self._resolve_project_id_from_origin(self.origin)
            if proj_id:
                self.project_id = proj_id
                return
        for loc in (self.location_dest_id, self.location_id):
            if loc:
                if getattr(loc, 'project_id', False):
                    self.project_id = loc.project_id
                    return
                if loc.warehouse_id and getattr(loc.warehouse_id, 'project_id', False):
                    self.project_id = loc.warehouse_id.project_id
                    return
        if self.picking_type_id and self.picking_type_id.warehouse_id and getattr(self.picking_type_id.warehouse_id, 'project_id', False):
            self.project_id = self.picking_type_id.warehouse_id.project_id

    @api.onchange('project_id')
    def _onchange_project_id_propagate_moves(self):
        self._sync_analytic_distribution_from_project()
        if self.project_id:
            for move in self.move_ids:
                move.project_id = self.project_id

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('is_central_stock_purchase'):
                if vals.get('purchase_id') and 'purchase.order' in self.env:
                    po = self.env['purchase.order'].sudo().browse(vals['purchase_id'])
                    if getattr(po, 'is_central_stock_purchase', False):
                        vals['is_central_stock_purchase'] = True
                elif vals.get('return_id'):
                    ret_pick = self.env['stock.picking'].sudo().browse(vals['return_id'])
                    if ret_pick.is_central_stock_purchase:
                        vals['is_central_stock_purchase'] = True
                elif vals.get('backorder_id'):
                    bo_pick = self.env['stock.picking'].sudo().browse(vals['backorder_id'])
                    if bo_pick.is_central_stock_purchase:
                        vals['is_central_stock_purchase'] = True
            if not vals.get('project_id') and not vals.get('is_central_stock_purchase'):
                resolved_proj_id = self._resolve_project_id_from_vals(vals)
                if resolved_proj_id:
                    vals['project_id'] = resolved_proj_id
            if vals.get('project_id') or vals.get('is_central_stock_purchase'):
                for move_key in ('move_ids', 'move_ids_without_package'):
                    for cmd in vals.get(move_key) or []:
                        if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[0] == 0 and isinstance(cmd[2], dict):
                            if vals.get('is_central_stock_purchase'):
                                cmd[2]['is_central_stock_purchase'] = True
                            elif vals.get('project_id') and not cmd[2].get('project_id'):
                                cmd[2]['project_id'] = vals['project_id']

        pickings = super().create(vals_list)
        for picking in pickings:
            if picking.project_id:
                moves_missing_proj = picking.move_ids.filtered(lambda m: m.project_id != picking.project_id)
                if moves_missing_proj:
                    moves_missing_proj.write({'project_id': picking.project_id.id})
                picking._sync_analytic_distribution_from_project()
        return pickings

    def write(self, vals):
        res = super().write(vals)
        if 'project_id' in vals and vals['project_id']:
            for picking in self:
                picking.move_ids.write({'project_id': vals['project_id']})
                picking._sync_analytic_distribution_from_project()
        return res

    def _prepare_return_picking_default_values(self):
        vals = super()._prepare_return_picking_default_values()
        if getattr(self, 'is_central_stock_purchase', False):
            vals['is_central_stock_purchase'] = True
            vals['project_id'] = False
        elif self.project_id:
            vals['project_id'] = self.project_id.id
        return vals

    def _prepare_return_move_default_values(self, move_id):
        vals = super()._prepare_return_move_default_values(move_id)
        if getattr(move_id, 'is_central_stock_purchase', False) or getattr(self, 'is_central_stock_purchase', False):
            vals['is_central_stock_purchase'] = True
            vals['project_id'] = False
        elif move_id.project_id:
            vals['project_id'] = move_id.project_id.id
        elif self.project_id:
            vals['project_id'] = self.project_id.id
        return vals


# --- 6.2 Stock Move, Stock Rule & Return Picking Enforcement ---
class StockMove(models.Model):
    _name = 'stock.move'
    _inherit = ['stock.move', 'contractor.project.mixin', 'contractor.no.delete.mixin']

    project_id = fields.Many2one(
        'project.project',
        string='Project Code',
        compute='_compute_project_id',
        store=True,
        readonly=False,
        required=False,
        index=True,
    )
    is_central_stock_purchase = fields.Boolean(
        string='Central Store Stock Movement',
        default=False,
        help='Indicates a stock movement for Central Store replenishment without a project code.',
    )

    @api.depends('picking_id.project_id', 'is_central_stock_purchase')
    def _compute_project_id(self):
        for move in self:
            if move.is_central_stock_purchase or (move.picking_id and move.picking_id.is_central_stock_purchase):
                continue
            if move.project_id:
                continue
            if move.picking_id and move.picking_id.project_id:
                move.project_id = move.picking_id.project_id
            elif getattr(move, 'sale_line_id', False) and move.sale_line_id.order_id.project_id:
                move.project_id = move.sale_line_id.order_id.project_id
            elif getattr(move, 'purchase_line_id', False) and move.purchase_line_id.order_id.project_id:
                move.project_id = move.purchase_line_id.order_id.project_id
            elif move.origin_returned_move_id and move.origin_returned_move_id.project_id:
                move.project_id = move.origin_returned_move_id.project_id

    @api.model
    def _resolve_move_project_id_from_vals(self, vals):
        if vals.get('is_central_stock_purchase'):
            return False
        if vals.get('picking_id'):
            picking = self.env['stock.picking'].sudo().browse(vals['picking_id'])
            if getattr(picking, 'is_central_stock_purchase', False):
                return False
        if vals.get('purchase_line_id') and 'purchase.order.line' in self.env:
            pol = self.env['purchase.order.line'].sudo().browse(vals['purchase_line_id'])
            if getattr(pol.order_id, 'is_central_stock_purchase', False):
                return False
        if vals.get('project_id'):
            return vals['project_id']
        if self.env.context.get('default_project_id'):
            return self.env.context['default_project_id']
        if vals.get('picking_id'):
            picking = self.env['stock.picking'].sudo().browse(vals['picking_id'])
            if picking.project_id:
                return picking.project_id.id
        if vals.get('sale_line_id') and 'sale.order.line' in self.env:
            sol = self.env['sale.order.line'].sudo().browse(vals['sale_line_id'])
            if sol.order_id.project_id:
                return sol.order_id.project_id.id
        if vals.get('purchase_line_id') and 'purchase.order.line' in self.env:
            pol = self.env['purchase.order.line'].sudo().browse(vals['purchase_line_id'])
            if pol.order_id.project_id:
                return pol.order_id.project_id.id
        if vals.get('origin_returned_move_id'):
            orig_mv = self.env['stock.move'].sudo().browse(vals['origin_returned_move_id'])
            if orig_mv.project_id:
                return orig_mv.project_id.id
        if vals.get('origin'):
            proj_id = self.env['stock.picking']._resolve_project_id_from_origin(vals['origin'])
            if proj_id:
                return proj_id
        for loc_key in ('location_dest_id', 'location_id'):
            loc_id = vals.get(loc_key)
            if loc_id:
                loc = self.env['stock.location'].sudo().browse(loc_id)
                if getattr(loc, 'project_id', False):
                    return loc.project_id.id
                if loc.warehouse_id and getattr(loc.warehouse_id, 'project_id', False):
                    return loc.warehouse_id.project_id.id
        if vals.get('warehouse_id'):
            wh = self.env['stock.warehouse'].sudo().browse(vals['warehouse_id'])
            if getattr(wh, 'project_id', False):
                return wh.project_id.id
        return False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('is_central_stock_purchase'):
                if vals.get('picking_id'):
                    picking = self.env['stock.picking'].sudo().browse(vals['picking_id'])
                    if getattr(picking, 'is_central_stock_purchase', False):
                        vals['is_central_stock_purchase'] = True
                elif vals.get('purchase_line_id') and 'purchase.order.line' in self.env:
                    pol = self.env['purchase.order.line'].sudo().browse(vals['purchase_line_id'])
                    if getattr(pol.order_id, 'is_central_stock_purchase', False):
                        vals['is_central_stock_purchase'] = True
            if not vals.get('project_id') and not vals.get('is_central_stock_purchase'):
                proj_id = self._resolve_move_project_id_from_vals(vals)
                if proj_id:
                    vals['project_id'] = proj_id
        return super().create(vals_list)

    def _get_new_picking_values(self):
        vals = super()._get_new_picking_values()
        if any(self.mapped('is_central_stock_purchase')):
            vals['is_central_stock_purchase'] = True
            vals['project_id'] = False
            return vals
        if not vals.get('project_id'):
            project = self.mapped('project_id')[:1]
            if not project and 'sale_line_id' in self._fields:
                project = self.mapped('sale_line_id.order_id.project_id')[:1]
            if not project and 'purchase_line_id' in self._fields:
                po = self.mapped('purchase_line_id.order_id')[:1]
                if po:
                    project = po.project_id or (getattr(po, 'requisition_id', False) and po.requisition_id.project_id)
            if not project and 'project_id' in self.env['stock.location']._fields:
                project = self.mapped('location_dest_id.project_id')[:1] or self.mapped('location_id.project_id')[:1]
            if not project and 'project_id' in self.env['stock.warehouse']._fields:
                project = self.mapped('warehouse_id.project_id')[:1]
            if project:
                vals['project_id'] = project.id
            elif vals.get('origin'):
                proj_id = self.env['stock.picking']._resolve_project_id_from_origin(vals['origin'])
                if proj_id:
                    vals['project_id'] = proj_id
        return vals

    def _assign_picking_values(self, picking):
        vals = super()._assign_picking_values(picking)
        if any(self.mapped('is_central_stock_purchase')) and not picking.is_central_stock_purchase:
            vals['is_central_stock_purchase'] = True
        if not picking.project_id and not (picking.is_central_stock_purchase or vals.get('is_central_stock_purchase')):
            project = self.mapped('project_id')[:1]
            if not project and 'sale_line_id' in self._fields:
                project = self.mapped('sale_line_id.order_id.project_id')[:1]
            if not project and 'purchase_line_id' in self._fields:
                po = self.mapped('purchase_line_id.order_id')[:1]
                if po:
                    project = po.project_id or (getattr(po, 'requisition_id', False) and po.requisition_id.project_id)
            if project:
                vals['project_id'] = project.id
        return vals

    def _prepare_procurement_values(self):
        res = super()._prepare_procurement_values()
        if self.project_id:
            res['project_id'] = self.project_id.id
        elif getattr(self, 'sale_line_id', False) and self.sale_line_id.order_id.project_id:
            res['project_id'] = self.sale_line_id.order_id.project_id.id
        return res

    def _is_central_or_unassigned_move(self):
        self.ensure_one()
        dest_wh = self.location_dest_id.warehouse_id if self.location_dest_id else False
        src_wh = self.location_id.warehouse_id if self.location_id else False
        pick = self.picking_id
        pick_wh = pick.picking_type_id.warehouse_id if (pick and pick.picking_type_id) else False
        if any(wh and getattr(wh, 'warehouse_type', False) == 'central' for wh in (dest_wh, src_wh, pick_wh, self.warehouse_id)):
            return True
        if pick:
            return pick.picking_type_code in ('incoming', 'internal')
        return self.env['stock.picking']._is_unassigned_or_central_storage(self.location_dest_id, self.warehouse_id)

    @api.constrains('project_id', 'picking_id', 'is_central_stock_purchase', 'location_id', 'location_dest_id')
    def _check_project_id_required(self):
        for move in self:
            if move.project_id:
                continue
            if move.is_central_stock_purchase or (move.picking_id and move.picking_id.is_central_stock_purchase):
                continue
            if move._is_central_or_unassigned_move():
                continue
            if move.picking_id and not move.picking_id.project_id:
                raise ValidationError(_("Every stock move must be assigned to a Project Code."))


class StockRule(models.Model):
    _inherit = 'stock.rule'

    def _get_custom_move_fields(self):
        fields_list = super()._get_custom_move_fields()
        for fname in ('project_id', 'is_central_stock_purchase'):
            if fname not in fields_list:
                fields_list.append(fname)
        return fields_list

    def _push_prepare_move_copy_values(self, move_to_copy):
        vals = super()._push_prepare_move_copy_values(move_to_copy)
        if move_to_copy.is_central_stock_purchase:
            vals['is_central_stock_purchase'] = True
        elif move_to_copy.project_id:
            vals['project_id'] = move_to_copy.project_id.id
        return vals


# --- 6.2B Project Analytic Account Governance ---
class ProjectProjectGovernance(models.Model):
    _inherit = 'project.project'

    def _get_or_create_project_analytic_account(self):
        """Ensures this project has an associated analytic account and returns it."""
        self.ensure_one()
        if not self.account_id:
            if hasattr(self, '_create_analytic_account'):
                self._create_analytic_account()
            if not self.account_id:
                plan = self.env['account.analytic.plan'].search([], limit=1)
                if not plan:
                    plan = self.env['account.analytic.plan'].create({'name': 'Projects'})
                code = getattr(self, 'project_code', False)
                analytic_account = self.env['account.analytic.account'].create({
                    'name': f"{code} - {self.name}" if code else self.name,
                    'company_id': self.company_id.id if self.company_id else False,
                    'plan_id': plan.id,
                })
                self.account_id = analytic_account
        return self.account_id


# --- 6.3 Account Move Line Enforcement (Operational Cost/Revenue Lines) ---
class AccountMoveLine(models.Model):
    _name = 'account.move.line'
    _inherit = ['account.move.line', 'contractor.project.mixin']

    project_id = fields.Many2one(
        'project.project',
        string='Project Code',
        required=False,
        index=True,
        help='Mandatory on operational cost/revenue lines to align ledger postings with project analytics.',
    )

    @api.model
    def _resolve_project_id_from_analytic_dist(self, analytic_distribution):
        if not isinstance(analytic_distribution, dict):
            return False
        account_ids = []
        for key in analytic_distribution.keys():
            for part in str(key).split(','):
                part = part.strip()
                if part.isdigit():
                    account_ids.append(int(part))
        if not account_ids:
            return False
        project = self.env['project.project'].sudo().search([
            ('account_id', 'in', account_ids)
        ], limit=1)
        return project.id if project else False

    def _is_legacy_project_exempt(self):
        """Return True when this line is exempt from mandatory Project Code enforcement
        because it belongs to an imported legacy record or predates the company's
        configured Project Code enforcement start date.
        Normal new entries remain strictly restricted.
        """
        self.ensure_one()
        move = self.move_id
        company = self.company_id or (move.company_id if move else self.env.company)

        # 1. Record already tagged as an imported legacy entry
        if move and move.is_legacy_import:
            return True

        # 2. Cutoff date: historical entries strictly before the go-live date
        start_date = company.project_restriction_start_date if company else False
        entry_date = (
            (move.date if move else False)
            or (move.invoice_date if move else False)
            or self.date
        )
        if start_date and entry_date and entry_date < start_date:
            return True

        # 3. Active import context (Odoo CSV/Excel import when enabled in Settings,
        #    or authorized migration script context)
        if self.env.context.get('import_file') and company and company.allow_legacy_import_without_project:
            return True
        # Note: @api.constrains runs under sudo() in Odoo 20 (env.su=True while keeping
        # the caller's env.uid), so check uid == 1 or base.group_system rather than env.su.
        if self.env.context.get('contractor_legacy_import') and (
            (company and company.allow_legacy_import_without_project)
            or self.env.uid == 1
            or self.env.user.has_group('base.group_system')
        ):
            return True

        return False

    @api.constrains('project_id', 'account_id', 'display_type', 'analytic_distribution')
    def _check_operational_project_required(self):
        """Enforces Project Code on operational cost and revenue lines.
        Balance sheet accounts (receivable, payable, tax, cash/bank) are excluded.
        General ledger entries (move_type == 'entry') without a move-level project_id
        are allowed for company-wide corporate accruals/settlements unless linked to a project.
        Imported legacy entries and entries dated prior to the configured cutoff date are exempt.
        """
        for line in self:
            if line.display_type in ('line_section', 'line_subsection', 'line_note'):
                continue
            account = line.account_id
            if not account:
                continue
            if line._is_legacy_project_exempt():
                continue
            # Operational lines belong to income or expense account types
            is_income = account.internal_group == 'income' or account.account_type in ('income', 'income_other')
            is_expense = account.internal_group == 'expense' or account.account_type in ('expense', 'expense_depreciation', 'expense_direct_cost')
            if not (is_income or is_expense):
                continue
            has_project = bool(
                line.project_id
                or (line.move_id and line.move_id.project_id)
                or (line.analytic_distribution and self._resolve_project_id_from_analytic_dist(line.analytic_distribution))
            )
            if not has_project:
                is_invoice_or_bill = line.move_id.move_type in (
                    'out_invoice', 'out_refund', 'in_invoice', 'in_refund', 'out_receipt', 'in_receipt'
                )
                has_move_project = bool(line.move_id and line.move_id.project_id)
                if is_invoice_or_bill or has_move_project:
                    raise ValidationError(_(
                        "Project Code is strictly required on operational cost/revenue line '%s' (Account: %s - %s)."
                    ) % (line.name or account.name, account.code, account.name))

    @api.onchange('project_id')
    def _onchange_project_id_sync_line_analytic(self):
        if self.project_id and self.project_id.account_id:
            self.analytic_distribution = {str(self.project_id.account_id.id): 100.0}

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('project_id'):
                if self.env.context.get('default_project_id'):
                    vals['project_id'] = self.env.context['default_project_id']
                elif vals.get('analytic_distribution'):
                    proj_id = self._resolve_project_id_from_analytic_dist(vals['analytic_distribution'])
                    if proj_id:
                        vals['project_id'] = proj_id
                if not vals.get('project_id') and vals.get('purchase_line_id') and 'purchase.order.line' in self.env:
                    pol = self.env['purchase.order.line'].sudo().browse(vals['purchase_line_id'])
                    if pol.order_id.project_id:
                        vals['project_id'] = pol.order_id.project_id.id
                elif not vals.get('project_id') and vals.get('sale_line_ids') and 'sale.order.line' in self.env:
                    for cmd in vals['sale_line_ids']:
                        if isinstance(cmd, (list, tuple)) and len(cmd) >= 2:
                            if cmd[0] == 4:
                                sol = self.env['sale.order.line'].sudo().browse(cmd[1])
                                if sol.order_id.project_id:
                                    vals['project_id'] = sol.order_id.project_id.id
                                    break
                            elif cmd[0] == 6 and len(cmd) >= 3 and cmd[2]:
                                sol = self.env['sale.order.line'].sudo().browse(cmd[2][0])
                                if sol.order_id.project_id:
                                    vals['project_id'] = sol.order_id.project_id.id
                                    break
                if not vals.get('project_id') and vals.get('move_id'):
                    move = self.env['account.move'].sudo().browse(vals['move_id'])
                    if getattr(move, 'project_id', False):
                        vals['project_id'] = move.project_id.id
                    elif move.invoice_origin:
                        proj_id = self.env['stock.picking']._resolve_project_id_from_origin(move.invoice_origin)
                        if proj_id:
                            vals['project_id'] = proj_id
                    elif move.ref:
                        proj_id = self.env['stock.picking']._resolve_project_id_from_origin(move.ref)
                        if proj_id:
                            vals['project_id'] = proj_id
            if vals.get('project_id') and not vals.get('analytic_distribution'):
                project = self.env['project.project'].browse(vals['project_id'])
                if project.account_id:
                    vals['analytic_distribution'] = {str(project.account_id.id): 100.0}
        return super().create(vals_list)


class AccountMove(models.Model):
    _inherit = 'account.move'

    project_id = fields.Many2one(
        'project.project',
        string='Project Code',
        compute='_compute_project_id',
        store=True,
        readonly=False,
        index=True,
        help='Project associated with this invoice / bill.'
    )
    is_legacy_import = fields.Boolean(
        string='Imported Legacy Record',
        default=False,
        copy=False,
        index=True,
        help='Marks historical accounting entries imported from a legacy system so they remain exempt from mandatory Project Code validation while all new entries strictly require a Project Code.',
    )

    @api.model
    def _can_mark_legacy_import(self, company_id=False):
        """Return True if the caller or company configuration permits tagging legacy imports."""
        company = self.env['res.company'].browse(company_id) if company_id else self.env.company
        if company and company.allow_legacy_import_without_project:
            return True
        return bool(self.env.su or self.env.user.has_group('base.group_system'))

    @api.depends('invoice_line_ids.project_id', 'line_ids.project_id')
    def _compute_project_id(self):
        for move in self:
            if not move.project_id:
                proj = move.invoice_line_ids.mapped('project_id') or move.line_ids.mapped('project_id')
                move.project_id = proj[0] if proj else False

    @api.onchange('project_id')
    def _onchange_project_id_sync_lines(self):
        if self.project_id:
            for line in self.invoice_line_ids:
                if not line.project_id:
                    line.project_id = self.project_id

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            can_mark = self._can_mark_legacy_import(vals.get('company_id'))
            if vals.get('is_legacy_import'):
                if not can_mark:
                    vals['is_legacy_import'] = False
            else:
                if self.env.context.get('contractor_legacy_import') and can_mark:
                    vals['is_legacy_import'] = True
                elif self.env.context.get('import_file'):
                    company = self.env['res.company'].browse(vals['company_id']) if vals.get('company_id') else self.env.company
                    if company and company.allow_legacy_import_without_project:
                        vals['is_legacy_import'] = True

            proj_id = vals.get('project_id') or self.env.context.get('default_project_id')
            if not proj_id and vals.get('invoice_origin'):
                proj_id = self.env['stock.picking']._resolve_project_id_from_origin(vals['invoice_origin'])
            if not proj_id and vals.get('ref'):
                proj_id = self.env['stock.picking']._resolve_project_id_from_origin(vals['ref'])
            if proj_id:
                for key in ('line_ids', 'invoice_line_ids'):
                    for cmd in vals.get(key) or []:
                        if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[0] == 0 and isinstance(cmd[2], dict):
                            if not cmd[2].get('project_id'):
                                cmd[2]['project_id'] = proj_id
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('is_legacy_import') and not (self.env.su or self.env.user.has_group('base.group_system')):
            raise AccessError(_("Only System Administrators can mark existing accounting entries as legacy imports."))
        return super().write(vals)


# --- 6.4 Sale Order Enforcement ---
class SaleOrder(models.Model):
    _name = 'sale.order'
    _inherit = ['sale.order', 'contractor.project.mixin', 'contractor.no.delete.mixin', 'contractor.version.mixin', 'contractor.reason.mixin']

    project_id = fields.Many2one(
        'project.project',
        string='Project Code',
        required=False,
        index=True,
        copy=False,
        help='Project Code linked to this quotation/sales order (auto-created upon conversion to active project).',
    )

    @api.onchange('project_id')
    def _onchange_project_id_propagate(self):
        self._sync_analytic_distribution_from_project()
        if self.project_id and self.project_id.account_id:
            dist = {str(self.project_id.account_id.id): 100.0}
            for line in self.order_line:
                line.analytic_distribution = dist

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        for order in orders:
            if order.project_id and order.project_id.account_id:
                dist = {str(order.project_id.account_id.id): 100.0}
                order.order_line.write({'analytic_distribution': dist})
        return orders

    def write(self, vals):
        res = super().write(vals)
        if 'project_id' in vals and vals['project_id']:
            for order in self:
                if order.project_id.account_id:
                    dist = {str(order.project_id.account_id.id): 100.0}
                    order.order_line.write({'analytic_distribution': dist})
        return res


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def _prepare_procurement_values(self):
        values = super()._prepare_procurement_values()
        if self.order_id.project_id:
            values['project_id'] = self.order_id.project_id.id
        return values

    def _prepare_invoice_line(self, **optional_values):
        res = super()._prepare_invoice_line(**optional_values)
        if self.order_id.project_id:
            res['project_id'] = self.order_id.project_id.id
        return res


# --- 6.5 Purchase Order Enforcement ---
class PurchaseOrder(models.Model):
    _name = 'purchase.order'
    _inherit = ['purchase.order', 'contractor.project.mixin', 'contractor.no.delete.mixin', 'contractor.version.mixin', 'contractor.reason.mixin']

    project_id = fields.Many2one(
        'project.project',
        string='Project Code',
        required=False,
        index=True,
        help='Project Code linked to this procurement requisition / purchase order (leave empty for Central Store Replenishment).',
    )
    is_central_stock_purchase = fields.Boolean(
        string='Central Store Replenishment (No Project)',
        default=False,
        tracking=True,
        help='Check this for general Central Warehouse stock replenishment orders without assigning a Project Code.',
    )

    @api.constrains('project_id', 'is_central_stock_purchase')
    def _check_project_id_required(self):
        for order in self:
            if not order.is_central_stock_purchase and not order.project_id:
                raise ValidationError(_(
                    "Project Code is strictly required on Purchase Order '%s' unless 'Central Store Replenishment (No Project)' is checked."
                ) % (order.name or order.display_name))

    @api.model
    def _get_default_central_receipt_picking_type(self, company=None):
        company = company or self.env.company
        wh_model = self.env['stock.warehouse'].sudo()
        if 'warehouse_type' in wh_model._fields:
            central_wh = wh_model.search([
                ('company_id', '=', company.id),
                ('warehouse_type', '=', 'central'),
            ], limit=1)
            if central_wh and central_wh.in_type_id:
                return central_wh.in_type_id
            fallback_wh = wh_model.search([
                ('company_id', '=', company.id),
                ('warehouse_type', '!=', 'project_site'),
                ('project_id', '=', False),
            ], limit=1)
            if fallback_wh and fallback_wh.in_type_id:
                return fallback_wh.in_type_id
        default_wh = wh_model.search([('company_id', '=', company.id)], limit=1)
        return default_wh.in_type_id if default_wh and default_wh.in_type_id else False

    @api.onchange('is_central_stock_purchase', 'company_id')
    def _onchange_is_central_stock_purchase(self):
        if self.is_central_stock_purchase:
            self.project_id = False
            central_pt = self._get_default_central_receipt_picking_type(self.company_id or self.env.company)
            if central_pt:
                self.picking_type_id = central_pt

    @api.onchange('project_id')
    def _onchange_project_id_propagate(self):
        self._sync_analytic_distribution_from_project()
        if self.project_id and self.project_id.account_id:
            dist = {str(self.project_id.account_id.id): 100.0}
            for line in self.order_line:
                line.analytic_distribution = dist

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('is_central_stock_purchase'):
                if not vals.get('picking_type_id'):
                    comp = self.env['res.company'].browse(vals['company_id']) if vals.get('company_id') else self.env.company
                    central_pt = self._get_default_central_receipt_picking_type(comp)
                    if central_pt:
                        vals['picking_type_id'] = central_pt.id
            elif not vals.get('project_id'):
                if vals.get('requisition_id') and 'contractor.material.requisition' in self.env:
                    mr = self.env['contractor.material.requisition'].sudo().browse(vals['requisition_id'])
                    if mr.project_id:
                        vals['project_id'] = mr.project_id.id
                elif vals.get('comparative_statement_id') and 'contractor.comparative.statement' in self.env:
                    cs = self.env['contractor.comparative.statement'].sudo().browse(vals['comparative_statement_id'])
                    if cs.project_id:
                        vals['project_id'] = cs.project_id.id
                elif vals.get('origin'):
                    proj_id = self.env['stock.picking']._resolve_project_id_from_origin(vals['origin'])
                    if proj_id:
                        vals['project_id'] = proj_id
        orders = super().create(vals_list)
        for order in orders:
            if order.project_id and order.project_id.account_id:
                dist = {str(order.project_id.account_id.id): 100.0}
                order.order_line.write({'analytic_distribution': dist})
        return orders

    def write(self, vals):
        res = super().write(vals)
        if 'project_id' in vals and vals['project_id']:
            for order in self:
                if order.project_id.account_id:
                    dist = {str(order.project_id.account_id.id): 100.0}
                    order.order_line.write({'analytic_distribution': dist})
        return res


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    def _prepare_account_move_line(self, move=False):
        res = super()._prepare_account_move_line(move=move)
        if self.order_id.project_id:
            res['project_id'] = self.order_id.project_id.id
        return res


# --- 6.6 HR Attendance Enforcement ---
class HrAttendance(models.Model):
    _name = 'hr.attendance'
    _inherit = ['hr.attendance', 'contractor.project.mixin', 'contractor.no.delete.mixin']

    project_id = fields.Many2one(
        'project.project',
        string='Project Code',
        required=False,
        index=True,
        default=lambda self: self.env['project.project'].search([('company_id', '=', self.env.company.id)], limit=1).id,
        help='Project Code where site work or shifts were performed.',
    )


# =========================================================================
# 7. Safe Dynamic Model Registration Wrapper
# =========================================================================
# Wrap the module's registered models in a SafeModelList so that if
# contractor_core is loaded on a minimal environment without sale,
# purchase, or hr_attendance installed, it gracefully omits models
# whose parents are not in the registry instead of raising TypeError.
class _SafeModelList(list):
    def __iter__(self):
        frame = sys._getframe(1)
        registry = frame.f_locals.get('self')
        for item in super().__iter__():
            if registry is not None and hasattr(registry, 'models'):
                inherit = getattr(item, '_inherit', None)
                if isinstance(inherit, list):
                    parents = inherit
                elif isinstance(inherit, str):
                    parents = [inherit]
                else:
                    parents = []
                missing = [p for p in parents if p not in registry.models and p != getattr(item, '_name', None)]
                if missing:
                    continue
            yield item


if 'contractor_core' in MetaModel._module_to_models__:
    MetaModel._module_to_models__['contractor_core'] = _SafeModelList(
        MetaModel._module_to_models__['contractor_core']
    )

