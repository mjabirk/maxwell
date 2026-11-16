# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


# =========================================================================
# 1. Project Master Budget Extension
# =========================================================================
class ContractorProjectBudget(models.Model):
    _inherit = ['contractor.project.budget', 'contractor.version.mixin', 'contractor.reason.mixin']
    _name = 'contractor.project.budget'

    is_active = fields.Boolean(
        string='Active Budget',
        default=True,
        tracking=True,
        help='Active master budget for project operations. Only one active budget is allowed per project.',
    )
    override_approved = fields.Boolean(
        string='Budget Overrun Override Approved',
        default=False,
        readonly=True,
        copy=False,
        tracking=True,
    )
    override_reason = fields.Text(
        string='Overrun Justification',
        readonly=True,
        copy=False,
    )
    override_user_id = fields.Many2one(
        'res.users',
        string='Override Approved By',
        readonly=True,
        copy=False,
    )
    override_date = fields.Datetime(
        string='Override Approval Date',
        readonly=True,
        copy=False,
    )
    budget_revision_ids = fields.One2many(
        'contractor.budget.revision',
        'budget_id',
        string='Internal Budget Revisions',
    )
    total_internal_revisions = fields.Monetary(
        string='Cumulative Internal Revisions',
        currency_field='currency_id',
        compute='_compute_revision_totals',
        store=True,
    )

    @api.depends('budget_revision_ids.state', 'budget_revision_ids.revision_amount', 'budget_revision_ids.requires_md_approval')
    def _compute_revision_totals(self):
        for budget in self:
            approved_revs = budget.budget_revision_ids.filtered(
                lambda r: (r.state == 'manager_approved' and not r.requires_md_approval) or r.state == 'md_approved'
            )
            budget.total_internal_revisions = sum(approved_revs.mapped('revision_amount'))

    @api.model
    def _refresh_project_consumption(self, project_ids):
        """Recomputes stored consumption fields on active budgets for the given project IDs."""
        if not project_ids:
            return
        if isinstance(project_ids, int):
            project_ids = [project_ids]
        budgets = self.sudo().search([
            ('project_id', 'in', list(project_ids)),
            ('is_active', '=', True),
        ])
        if budgets:
            budgets.mapped('line_ids').sudo()._compute_line_consumption()

    @api.model
    def check_budget_availability(self, project_id, cost_head, requested_amount, product_id=None):
        """Hard-Stop: Verifies whether sufficient remaining budget exists for a requested expenditure.
        Raises UserError if requested amount exceeds remaining budget and no management override is active.
        """
        if not project_id:
            return True

        budget = self.search([
            ('project_id', '=', project_id),
            ('is_active', '=', True),
        ], limit=1)

        if not budget:
            project = self.env['project.project'].browse(project_id)
            raise UserError(_(
                "Budget Governance Hard-Stop:\n\n"
                "No active Project Master Budget exists for Project '%s'.\n"
                "A project budget carrying baseline and VO allocations is strictly mandatory "
                "before creating Material Requisitions or Purchase Orders."
            ) % project.name)

        # Refresh live consumption before evaluating remaining budget
        budget.line_ids.sudo()._compute_line_consumption()

        # Check line level if cost_head provided
        matching_lines = budget.line_ids.filtered(lambda l: l.block_type == cost_head)
        if product_id and matching_lines.filtered(lambda l: l.product_id.id == product_id):
            matching_lines = matching_lines.filtered(lambda l: l.product_id.id == product_id)

        if matching_lines:
            remaining = sum(matching_lines.mapped('remaining_budget'))
        else:
            remaining = budget.total_revised_budget - sum(budget.line_ids.mapped('actual_cost_amount')) - sum(budget.line_ids.mapped('committed_amount'))

        if requested_amount > remaining:
            if not budget.override_approved:
                project = self.env['project.project'].browse(project_id)
                currency = budget.currency_id.symbol or ''
                raise UserError(_(
                    "Budget Overrun Hard-Stop Enforced:\n\n"
                    "Requested expenditure (%s %.2f) exceeds remaining budget (%s %.2f) "
                    "for Project '%s' under Cost Head '%s'.\n\n"
                    "Procurement is blocked until an internal budget revision or an approved "
                    "management override by a Contractor Manager or Managing Director is executed."
                ) % (currency, requested_amount, currency, remaining, project.name, (cost_head or 'General').title()))
        return True

    def action_approve_override(self):
        """Allows Contractor Manager or MD to authorize a budget overrun override."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only a Contractor Manager can authorize budget overrun overrides."))

        return self.action_open_reason_wizard(
            action_type='override',
            callback_method='_callback_budget_override',
        )

    def _callback_budget_override(self, reason=None):
        self.write({
            'override_approved': True,
            'override_reason': reason or self.last_action_reason,
            'override_user_id': self.env.user.id,
            'override_date': fields.Datetime.now(),
        })
        self.message_post(
            body=Markup(_("<strong>Budget Overrun Override Granted:</strong> Authorized by %s.<br/>Reason: %s")) % (
                self.env.user.name, self.override_reason
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    @api.constrains('project_id', 'is_active')
    def _check_single_active_budget(self):
        for rec in self:
            if rec.is_active and rec.project_id:
                dup = self.sudo().search([
                    ('id', '!=', rec.id),
                    ('project_id', '=', rec.project_id.id),
                    ('company_id', '=', rec.company_id.id),
                    ('is_active', '=', True),
                ], limit=1)
                if dup:
                    raise ValidationError(_(
                        "Project '%s' already has an active Master Budget ('%s'). "
                        "Deactivate the existing budget before activating another."
                    ) % (rec.project_id.display_name, dup.name))


# =========================================================================
# 2. Project Budget Line Cost-Head Tracking
# =========================================================================
class ContractorProjectBudgetLine(models.Model):
    _inherit = 'contractor.project.budget.line'

    block_type = fields.Selection(selection_add=[
        ('subcontractor', 'Subcontractor / Specialist Package'),
    ], ondelete={'subcontractor': 'set default'})

    baseline_amount = fields.Monetary(
        string='Baseline Budget (Quoted)',
        currency_field='currency_id',
        compute='_compute_baseline_amount',
        store=True,
        readonly=True,
    )
    variation_order_amount = fields.Monetary(
        string='Approved VO Allocation',
        currency_field='currency_id',
        compute='_compute_line_amounts',
        store=True,
        readonly=True,
    )
    internal_revision_amount = fields.Monetary(
        string='Internal Revisions',
        currency_field='currency_id',
        compute='_compute_line_amounts',
        store=True,
        readonly=True,
    )
    total_approved_budget = fields.Monetary(
        string='Total Approved Budget',
        currency_field='currency_id',
        compute='_compute_line_amounts',
        store=True,
        readonly=True,
    )
    committed_amount = fields.Monetary(
        string='Committed Cost',
        currency_field='currency_id',
        compute='_compute_line_consumption',
        store=True,
        help='Open approved Material Requisitions and unbilled confirmed Purchase Orders.',
    )
    actual_cost_amount = fields.Monetary(
        string='Actual Posted Cost',
        currency_field='currency_id',
        compute='_compute_line_consumption',
        store=True,
        help='Actual posted material consumption, goods receipt notes, posted vendor bills, and labor hours.',
    )
    remaining_budget = fields.Monetary(
        string='Remaining Budget',
        currency_field='currency_id',
        compute='_compute_line_consumption',
        store=True,
        help='Remaining allowable spend (Total Approved Budget - Committed Cost - Actual Posted Cost).',
    )

    @api.depends('planned_amount')
    def _compute_baseline_amount(self):
        for line in self:
            line.baseline_amount = line.planned_amount

    def _get_block_share(self):
        """Returns this line's proportional share within its budget block_type so block totals aren't duplicated."""
        self.ensure_one()
        if not self.budget_id:
            return 1.0
        same_block_lines = self.budget_id.line_ids.filtered(lambda l: l.block_type == self.block_type)
        if not same_block_lines:
            return 1.0
        block_baseline = sum(same_block_lines.mapped('baseline_amount'))
        if block_baseline > 0:
            return (self.baseline_amount or 0.0) / block_baseline
        return 1.0 if same_block_lines[0] == self else 0.0

    @api.depends(
        'baseline_amount',
        'block_type',
        'budget_id.line_ids.baseline_amount',
        'budget_id.line_ids.block_type',
        'budget_id.vo_line_ids.planned_cost',
        'budget_id.vo_line_ids.labour_cost',
        'budget_id.vo_line_ids.material_cost',
        'budget_id.vo_line_ids.legal_cost',
        'budget_id.vo_line_ids.misc_cost',
        'budget_id.budget_revision_ids.state',
        'budget_id.budget_revision_ids.revision_amount',
        'budget_id.budget_revision_ids.requires_md_approval',
    )
    def _compute_line_amounts(self):
        for line in self:
            if not line.budget_id:
                line.variation_order_amount = 0.0
                line.internal_revision_amount = 0.0
                line.total_approved_budget = line.baseline_amount or 0.0
                continue

            share = line._get_block_share()

            # Revisions for this cost head that completed their approval chain
            approved_revs = line.budget_id.budget_revision_ids.filtered(
                lambda r: r.cost_head == line.block_type and (
                    (r.state == 'manager_approved' and not r.requires_md_approval)
                    or r.state == 'md_approved'
                )
            )
            rev_amt = sum(approved_revs.mapped('revision_amount')) * share

            # VO allocations broken down per block type (with fallback to material for legacy VO lines)
            vo_lines = line.budget_id.vo_line_ids
            vo_block_total = 0.0
            for vo_l in vo_lines:
                has_breakdown = any([
                    vo_l.labour_cost, vo_l.material_cost, vo_l.legal_cost, vo_l.misc_cost
                ])
                if has_breakdown:
                    if line.block_type == 'labour':
                        vo_block_total += vo_l.labour_cost or 0.0
                    elif line.block_type == 'material':
                        vo_block_total += vo_l.material_cost or 0.0
                    elif line.block_type == 'legal':
                        vo_block_total += vo_l.legal_cost or 0.0
                    elif line.block_type == 'misc':
                        vo_block_total += vo_l.misc_cost or 0.0
                elif line.block_type == 'material':
                    vo_block_total += vo_l.planned_cost or 0.0
            vo_amt = vo_block_total * share

            line.variation_order_amount = vo_amt
            line.internal_revision_amount = rev_amt
            line.total_approved_budget = (line.baseline_amount or 0.0) + vo_amt + rev_amt

    @api.depends('total_approved_budget', 'budget_id.project_id', 'block_type', 'baseline_amount')
    def _compute_line_consumption(self):
        for line in self:
            project = line.budget_id.project_id
            if not project:
                line.committed_amount = 0.0
                line.actual_cost_amount = 0.0
                line.remaining_budget = line.total_approved_budget or 0.0
                continue

            share = line._get_block_share()
            if share <= 0.0:
                line.committed_amount = 0.0
                line.actual_cost_amount = 0.0
                line.remaining_budget = line.total_approved_budget or 0.0
                continue

            btype = line.block_type
            has_subcon_budget_line = bool(
                line.budget_id.line_ids.filtered(lambda l: l.block_type == 'subcontractor')
            )

            # 1. Committed cost per block
            total_committed = 0.0
            if btype == 'material':
                if 'contractor.material.requisition.line' in self.env:
                    open_mr_lines = self.env['contractor.material.requisition.line'].sudo().search([
                        ('requisition_id.project_id', '=', project.id),
                        ('requisition_id.state', 'in', ['approved', 'cs_in_progress', 'po_created']),
                    ])
                    open_mr_lines = open_mr_lines.filtered(
                        lambda m: not any(po.state in ('purchase', 'done') for po in m.requisition_id.purchase_order_ids)
                    )
                    total_committed += sum(open_mr_lines.mapped('estimated_subtotal'))

                if 'purchase.order.line' in self.env:
                    open_po_lines = self.env['purchase.order.line'].sudo().search([
                        ('order_id.project_id', '=', project.id),
                        ('order_id.state', 'in', ['purchase', 'done']),
                    ])
                    open_po_lines = open_po_lines.filtered(lambda p: p.qty_invoiced < p.product_qty)
                    total_committed += sum((p.product_qty - p.qty_invoiced) * p.price_unit for p in open_po_lines)
            elif btype == 'labour' and 'contractor.site.attendance.line' in self.env:
                pending_att = self.env['contractor.site.attendance.line'].sudo().search([
                    ('project_id', '=', project.id),
                    ('attendance_id.state', '=', 'submitted'),
                ])
                total_committed += sum(pending_att.mapped('calculated_labour_cost'))

            # 2. Actual cost per block from posted GL items categorized by cost_head
            total_actual = 0.0
            analytic_account_id = str(project.account_id.id) if project.account_id else False
            aml_domain = [
                ('parent_state', '=', 'posted'),
                ('account_id.internal_group', '=', 'expense'),
            ]
            if not analytic_account_id:
                aml_domain.extend([
                    '|', ('project_id', '=', project.id), ('move_id.project_id', '=', project.id)
                ])
            amls = self.env['account.move.line'].sudo().search(aml_domain)
            for aml in amls:
                weight = 0.0
                if analytic_account_id and aml.analytic_distribution and analytic_account_id in aml.analytic_distribution:
                    weight = (aml.analytic_distribution[analytic_account_id] or 0.0) / 100.0
                elif aml.project_id.id == project.id or aml.move_id.project_id.id == project.id:
                    weight = 1.0
                if not weight:
                    continue

                aml_head = getattr(aml, 'cost_head', False) or 'material'
                if aml_head == 'subcontractor' and not has_subcon_budget_line:
                    aml_head = 'misc'
                if aml_head == btype:
                    total_actual += aml.balance * weight

            # Include PM-approved operational records whose DCP has not yet been posted to GL
            if btype == 'labour' and 'contractor.site.attendance.line' in self.env:
                unposted_att = self.env['contractor.site.attendance.line'].sudo().search([
                    ('project_id', '=', project.id),
                    ('attendance_id.state', '=', 'pm_approved'),
                ]).filtered(
                    lambda al: not al.attendance_id.cost_posting_id or al.attendance_id.cost_posting_id.state != 'posted'
                )
                total_actual += sum(unposted_att.mapped('calculated_labour_cost'))

            if btype == 'material' and 'contractor.site.consumption.line' in self.env:
                unposted_cons = self.env['contractor.site.consumption.line'].sudo().search([
                    ('project_id', '=', project.id),
                    ('consumption_id.state', '=', 'pm_approved'),
                ]).filtered(
                    lambda cl: not cl.consumption_id.cost_posting_id or cl.consumption_id.cost_posting_id.state != 'posted'
                )
                total_actual += sum(unposted_cons.mapped('subtotal_cost'))

            if (btype == 'subcontractor' or (btype == 'misc' and not has_subcon_budget_line)) and 'contractor.site.work.log' in self.env:
                unposted_wlogs = self.env['contractor.site.work.log'].sudo().search([
                    ('project_id', '=', project.id),
                    ('crew_type', '=', 'subcontractor'),
                    ('state', '=', 'pm_certified'),
                ]).filtered(
                    lambda w: not w.cost_posting_id or w.cost_posting_id.state != 'posted'
                )
                total_actual += sum(unposted_wlogs.mapped('accrued_subcontractor_cost'))

            if 'contractor.petty.cash.voucher' in self.env:
                pcv_head = btype if btype in ('legal', 'misc', 'material') else None
                if pcv_head:
                    pc_vouchers = self.env['contractor.petty.cash.voucher'].sudo().search([
                        ('project_id', '=', project.id),
                        ('state', 'in', ['paid', 'replenished']),
                    ]).filtered(
                        lambda v: (getattr(v, 'cost_head', False) or 'misc') == pcv_head
                        and (not v.move_id or v.move_id.state != 'posted')
                    )
                    total_actual += sum(pc_vouchers.mapped('amount'))

            line.committed_amount = max(total_committed * share, 0.0)
            line.actual_cost_amount = max(total_actual * share, 0.0)
            line.remaining_budget = (line.total_approved_budget or 0.0) - line.committed_amount - line.actual_cost_amount


# =========================================================================
# 3. Internal Budget Revision Model
# =========================================================================
class ContractorBudgetRevision(models.Model):
    _name = 'contractor.budget.revision'
    _description = 'Contractor Internal Budget Revision'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.version.mixin', 'contractor.reason.mixin', 'contractor.no.delete.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='Revision Number',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Revision'),
        index=True,
    )
    budget_id = fields.Many2one(
        'contractor.project.budget',
        string='Project Master Budget',
        required=True,
        ondelete='restrict',
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        related='budget_id.project_id',
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
    cost_head = fields.Selection([
        ('labour', 'Labor Cost (Block 1)'),
        ('material', 'Material Cost (Block 2)'),
        ('legal', 'Legal & Permits (Block 3)'),
        ('misc', 'Miscellaneous & Site Indirects (Block 4)'),
        ('subcontractor', 'Subcontractor / Specialist Package'),
    ], string='Affected Cost Head', required=True, default='material', tracking=True)

    revision_amount = fields.Monetary(
        string='Revision Net Amount',
        currency_field='currency_id',
        required=True,
        tracking=True,
        help='Positive amount to increase budget allowance; negative amount to reallocate or deduct.',
    )
    reason = fields.Text(
        string='Mandatory Technical Justification',
        required=True,
        tracking=True,
        help='Documented reason for internal budget adjustment (e.g. rate variance, quantity adjustment, contingency transfer).',
    )
    requires_md_approval = fields.Boolean(
        string='Requires Managing Director Approval',
        compute='_compute_requires_md',
        store=True,
        tracking=True,
    )
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
    state = fields.Selection([
        ('draft', 'Draft Revision'),
        ('pm_submitted', 'Submitted by PM'),
        ('manager_approved', 'Approved by Contractor Manager'),
        ('md_approved', 'Approved by Managing Director'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    @api.depends('revision_amount', 'company_id.internal_budget_rev_md_threshold')
    def _compute_requires_md(self):
        for rev in self:
            company = rev.company_id or self.env.company
            threshold = company.internal_budget_rev_md_threshold or 25000.0
            rev.requires_md_approval = abs(rev.revision_amount) >= threshold

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Revision')) == _('New Revision'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.budget.revision') or _('New Revision')
        return super().create(vals_list)

    def action_submit(self):
        """Validates justification and committed/actual floor, and routes through Manager (and MD above threshold)."""
        self.ensure_one()
        clean_reason = (self.reason or '').strip()
        if len(clean_reason) < 10:
            raise UserError(_("Please provide a detailed internal justification (minimum 10 characters)."))

        if self.revision_amount < 0:
            self.budget_id.line_ids.sudo()._compute_line_consumption()
            block_lines = self.budget_id.line_ids.filtered(lambda l: l.block_type == self.cost_head)
            remaining_block = sum(block_lines.mapped('remaining_budget'))
            if abs(self.revision_amount) > remaining_block and not self.budget_id.override_approved:
                raise UserError(_(
                    "Budget Revision Blocked:\n\n"
                    "Cannot reduce cost head '%s' by %s %.2f because only %s %.2f remains "
                    "uncommitted/unspent. Obtain a Budget Overrun Override before reducing below committed spend."
                ) % (self.cost_head, self.currency_id.symbol or '', abs(self.revision_amount), self.currency_id.symbol or '', remaining_block))

        self._compute_requires_md()
        self.state = 'pm_submitted'

        # Snapshot budget before revision
        self.budget_id._create_version_snapshot(
            reason=_("Internal Budget Revision %s Submitted (Amount: %s)") % (self.name, self.revision_amount),
            editor_user=self.env.user,
        )

        # SOP chain: PM -> Manager (-> MD above threshold)
        group_rec = self.env.ref('contractor_core.group_contractor_manager', raise_if_not_found=False)
        if group_rec:
            for user in (group_rec.all_user_ids | group_rec.user_ids).filtered(lambda u: u.active):
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                    'res_model_id': self.env['ir.model']._get_id('contractor.budget.revision'),
                    'res_id': self.id,
                    'user_id': user.id,
                    'summary': f"Review Internal Budget Revision {self.name} for {self.project_id.name}",
                    'note': _("Revision: %s | Cost Head: %s | Requires Final MD Sign-Off: %s") % (
                        self.revision_amount, self.cost_head, 'Yes' if self.requires_md_approval else 'No'
                    ),
                    'date_deadline': fields.Date.context_today(self),
                })

        self.message_post(
            body=Markup(_("<strong>Internal Budget Revision Submitted:</strong> Amount: %s | Requires MD After Manager Review: %s.")) % (
                self.revision_amount, self.requires_md_approval
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_manager_approve(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_manager'):
            raise UserError(_("Access Denied: Only Contractor Managers can approve budget revisions."))

        self.write({
            'state': 'manager_approved',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })

        if self.requires_md_approval:
            group_md = self.env.ref('contractor_core.group_contractor_md', raise_if_not_found=False)
            if group_md:
                for user in (group_md.all_user_ids | group_md.user_ids).filtered(lambda u: u.active):
                    self.env['mail.activity'].create({
                        'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                        'res_model_id': self.env['ir.model']._get_id('contractor.budget.revision'),
                        'res_id': self.id,
                        'user_id': user.id,
                        'summary': f"MD Approval Required: Budget Revision {self.name} ({self.project_id.name})",
                        'note': _("Manager %s reviewed and endorsed revision %s. MD final approval required.") % (
                            self.env.user.name, self.revision_amount
                        ),
                        'date_deadline': fields.Date.context_today(self),
                    })
            self.message_post(
                body=Markup(_("<strong>Manager Review Endorsed:</strong> Forwarded by %s to Managing Director for final threshold sign-off.")) % self.env.user.name,
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
            return True

        self.budget_id._compute_revision_totals()
        self.budget_id.line_ids._compute_line_amounts()

        self.message_post(
            body=Markup(_("<strong>Budget Revision Approved:</strong> Authorized by Contractor Manager %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_md_approve(self):
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_md'):
            raise UserError(_("Access Denied: Only the Managing Director can authorize this high-value revision."))
        if self.state not in ('pm_submitted', 'manager_approved'):
            raise UserError(_("Only submitted or Manager-endorsed revisions can be approved by the Managing Director."))

        self.write({
            'state': 'md_approved',
            'approver_id': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })
        self.budget_id._compute_revision_totals()
        self.budget_id.line_ids._compute_line_amounts()

        self.message_post(
            body=Markup(_("<strong>Executive Approval Granted:</strong> Authorized by Managing Director %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_decline(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only Contractor Managers can decline budget revisions."))
        return self.action_open_reason_wizard(action_type='decline', target_state='declined')


class AccountMoveLineBudgetCostHead(models.Model):
    _inherit = 'account.move.line'

    cost_head = fields.Selection([
        ('labour', 'Labour Cost (Block 1)'),
        ('material', 'Material Cost (Block 2)'),
        ('legal', 'Legal & Permits (Block 3)'),
        ('misc', 'Miscellaneous & Indirects (Block 4)'),
        ('subcontractor', 'Subcontractor Package'),
    ], string='Budget Cost Head', index=True,
       help='Explicit cost-head classification used by the Project Master Budget consumption tracker.')


class AccountMoveBudgetRefresh(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        res = super()._post(soft=soft)
        project_ids = (self.mapped('project_id') | self.mapped('line_ids.project_id')).ids
        if project_ids:
            self.env['contractor.project.budget']._refresh_project_consumption(project_ids)
        return res

    def button_cancel(self):
        project_ids = (self.mapped('project_id') | self.mapped('line_ids.project_id')).ids
        res = super().button_cancel()
        if project_ids:
            self.env['contractor.project.budget']._refresh_project_consumption(project_ids)
        return res
