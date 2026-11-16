# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorDailyCostPosting(models.Model):
    _name = 'contractor.daily.cost.posting'
    _description = 'Automatic Daily Project Cost Posting'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'cost_date desc, id desc'

    name = fields.Char(
        string='Posting Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New DCP'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        required=True,
        index=True,
        tracking=True,
    )
    cost_date = fields.Date(
        string='Cost Date',
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
    dpr_id = fields.Many2one(
        'contractor.daily.progress.report',
        string='Daily Progress Report (DPR)',
        readonly=True,
    )
    attendance_id = fields.Many2one(
        'contractor.site.attendance',
        string='Site Attendance Sheet',
        readonly=True,
    )
    consumption_id = fields.Many2one(
        'contractor.site.consumption',
        string='Material Consumption Sheet',
        readonly=True,
    )
    work_log_ids = fields.Many2many(
        'contractor.site.work.log',
        'contractor_dcp_work_log_rel',
        'dcp_id',
        'log_id',
        string='Certified Work Logs',
        readonly=True,
    )
    line_ids = fields.One2many(
        'contractor.daily.cost.posting.line',
        'cost_posting_id',
        string='Calculated Cost Items',
        readonly=True,
    )
    labour_cost_total = fields.Monetary(
        string='Total Labour Cost',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    material_cost_total = fields.Monetary(
        string='Total Material Cost',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    subcontractor_cost_total = fields.Monetary(
        string='Total Subcontractor Cost',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    grand_total_cost = fields.Monetary(
        string='Daily Grand Total Project Cost',
        currency_field='currency_id',
        compute='_compute_totals',
        store=True,
    )
    account_move_id = fields.Many2one(
        'account.move',
        string='Project Cost Journal Entry',
        readonly=True,
        copy=False,
    )
    state = fields.Selection([
        ('draft', 'Draft Cost Posting'),
        ('posted', 'Posted to Budget & Accounts'),
        ('correction_requested', 'Correction Requested by Accounts'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    correction_request_ids = fields.One2many(
        'contractor.cost.correction.request',
        'cost_posting_id',
        string='Accountant Flagged Corrections',
    )
    correction_count = fields.Integer(
        string='Correction Requests',
        compute='_compute_correction_count',
    )
    zero_activity_explanation = fields.Char(
        string='Zero-Activity Explanation',
        help='Optional documented reason if a project site had no operational activity on this day.',
    )

    _check_uniq_project_cost_date = models.Constraint(
        'UNIQUE(project_id, cost_date)',
        'A Daily Cost Posting already exists for this Project on this date!'
    )

    @api.depends('line_ids.subtotal_cost', 'line_ids.cost_category')
    def _compute_totals(self):
        for dcp in self:
            labour = sum(dcp.line_ids.filtered(lambda l: l.cost_category == 'labour').mapped('subtotal_cost'))
            material = sum(dcp.line_ids.filtered(lambda l: l.cost_category == 'material').mapped('subtotal_cost'))
            subcon = sum(dcp.line_ids.filtered(lambda l: l.cost_category == 'subcontractor').mapped('subtotal_cost'))
            dcp.labour_cost_total = labour
            dcp.material_cost_total = material
            dcp.subcontractor_cost_total = subcon
            dcp.grand_total_cost = labour + material + subcon

    @api.depends('correction_request_ids')
    def _compute_correction_count(self):
        for dcp in self:
            dcp.correction_count = len(dcp.correction_request_ids)

    def action_view_corrections(self):
        self.ensure_one()
        return {
            'name': _('Accountant Correction Requests'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.cost.correction.request',
            'view_mode': 'list,form',
            'domain': [('cost_posting_id', '=', self.id)],
            'context': {'default_cost_posting_id': self.id, 'default_project_id': self.project_id.id},
        }

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New DCP')) == _('New DCP'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.daily.cost.posting') or _('New DCP')

            # Hard-Stop: Strictly block manual lump-sum creation without source documents
            if not self.env.su and not self.env.context.get('bypass_cost_posting_source_check'):
                has_source = (
                    vals.get('attendance_id') or
                    vals.get('consumption_id') or
                    vals.get('work_log_ids') or
                    vals.get('dpr_id') or
                    vals.get('line_ids')
                )
                if not has_source:
                    raise UserError(_(
                        "Cost Accounting Governance Hard-Stop:\n\n"
                        "Manual creation of lump-sum Daily Cost Postings is prohibited.\n\n"
                        "All project operational costs must be automatically generated from approved site documentation "
                        "(Site Attendance, Material Consumption, or Subcontractor Work Logs)."
                    ))

        return super().create(vals_list)

    def write(self, vals):
        allowed_meta_fields = {
            'message_follower_ids', 'message_ids', 'activity_ids',
            'message_main_attachment_id', 'website_message_ids',
            'last_action_reason', 'last_action_by_id', 'last_action_date',
        }
        posted_recs = self.filtered(lambda r: r.state == 'posted')
        if posted_recs and not set(vals.keys()).issubset(allowed_meta_fields):
            if vals != {'state': 'correction_requested'}:
                raise UserError(_(
                    "Immutable Posted Record Hard-Stop:\n\n"
                    "Posted Daily Cost Postings cannot be modified directly. "
                    "Use 'Flag Correction to PM' to request site rectification."
                ))
        # Prevent manual modification of site calculated figures by Accounts
        if not self.env.su and ('line_ids' in vals or 'grand_total_cost' in vals):
            if not self.env.context.get('bypass_cost_posting_source_check'):
                if self.env.user.has_group('contractor_core.group_contractor_accountant') and not self.env.user.has_group('contractor_core.group_contractor_project_manager'):
                    raise UserError(_(
                        "Silent Site Edit Prohibited:\n\n"
                        "Accountants are blocked from directly altering operational site quantities or costs.\n\n"
                        "Please use the 'Flag Correction to PM' button to formally route discrepancies back to the Project Manager."
                    ))
        return super().write(vals)

    @api.model
    def get_or_create_daily_posting(self, project_id, cost_date):
        """Idempotently finds or creates the Daily Cost Posting for a project and date, re-aggregating approved costs."""
        posting = self.with_context(bypass_cost_posting_source_check=True).search([
            ('project_id', '=', project_id),
            ('cost_date', '=', cost_date),
        ], limit=1)

        if not posting:
            posting = self.with_context(bypass_cost_posting_source_check=True).create({
                'project_id': project_id,
                'cost_date': cost_date,
            })

        posting._sync_cost_lines()
        return posting

    def _sync_cost_lines(self):
        """Pulls and consolidates approved operational costs for labour, material, and subcontractors."""
        self.ensure_one()
        if self.state == 'posted':
            return

        lines = []

        # 1. Associated DPR
        dpr = self.env['contractor.daily.progress.report'].search([
            ('project_id', '=', self.project_id.id),
            ('report_date', '=', self.cost_date),
        ], limit=1)
        if dpr:
            self.dpr_id = dpr.id

        # 2. Approved Labour Attendance
        attendance = self.env['contractor.site.attendance'].search([
            ('project_id', '=', self.project_id.id),
            ('attendance_date', '=', self.cost_date),
            ('state', '=', 'pm_approved'),
        ], limit=1)
        if attendance:
            self.attendance_id = attendance.id
            for aline in attendance.line_ids.filtered(lambda l: l.calculated_labour_cost > 0):
                lines.append((0, 0, {
                    'cost_category': 'labour',
                    'description': f"Labour: {aline.employee_id.name} ({aline.job_title_id.name or 'Operative'}) - {aline.regular_hours}h reg + {aline.overtime_hours}h OT",
                    'source_ref': f"{attendance.name} / Line #{aline.id}",
                    'employee_id': aline.employee_id.id,
                    'quantity': aline.regular_hours + aline.overtime_hours,
                    'unit_cost': aline.hourly_rate,
                    'subtotal_cost': aline.calculated_labour_cost,
                }))

        # 3. Reconciled & Approved Material Consumption
        consumption = self.env['contractor.site.consumption'].search([
            ('project_id', '=', self.project_id.id),
            ('consumption_date', '=', self.cost_date),
            ('state', '=', 'pm_approved'),
        ], limit=1)
        if consumption:
            self.consumption_id = consumption.id
            for cline in consumption.line_ids.filtered(lambda l: l.subtotal_cost > 0):
                lines.append((0, 0, {
                    'cost_category': 'material',
                    'description': f"Material: {cline.product_id.display_name} ({cline.work_location or 'Site'})",
                    'source_ref': f"{consumption.name} / Line #{cline.id}",
                    'product_id': cline.product_id.id,
                    'quantity': cline.consumed_today_qty,
                    'uom_id': cline.product_uom_id.id,
                    'unit_cost': cline.unit_cost,
                    'subtotal_cost': cline.subtotal_cost,
                }))

        # 4. PM Certified Subcontractor Work Logs (exclude in_house crews to avoid double-counting attendance labour)
        work_logs = self.env['contractor.site.work.log'].search([
            ('project_id', '=', self.project_id.id),
            ('work_date', '=', self.cost_date),
            ('crew_type', '=', 'subcontractor'),
            ('state', '=', 'pm_certified'),
        ])
        if work_logs:
            self.work_log_ids = [(6, 0, work_logs.ids)]
            for wlog in work_logs.filtered(lambda w: w.accrued_subcontractor_cost > 0):
                subcon_name = wlog.subcontractor_id.name if wlog.subcontractor_id else _('In-House Crew')
                lines.append((0, 0, {
                    'cost_category': 'subcontractor',
                    'description': f"Subcontractor: {subcon_name} - {wlog.work_package} ({wlog.site_verified_qty} {wlog.uom_id.name})",
                    'source_ref': f"{wlog.name}",
                    'subcontractor_id': wlog.subcontractor_id.id if wlog.subcontractor_id else False,
                    'quantity': wlog.site_verified_qty,
                    'uom_id': wlog.uom_id.id,
                    'unit_cost': wlog.agreed_unit_rate,
                    'subtotal_cost': wlog.accrued_subcontractor_cost,
                }))

        # Replace existing lines
        self.line_ids.unlink()
        self.with_context(bypass_cost_posting_source_check=True).write({
            'line_ids': lines,
        })
        self._compute_totals()

    def action_post_to_budget_and_accounts(self):
        """Generates project-tagged journal entry and updates project master budget actuals."""
        self.ensure_one()
        if not (self.env.user.has_group('contractor_core.group_contractor_accountant') or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise UserError(_("Access Denied: Only Contractor Accountants or Managers can post daily project costs."))

        if self.state != 'draft':
            raise UserError(_("Only draft Daily Cost Postings can be posted to Budget and Accounts."))
        if self.account_move_id:
            raise UserError(_("A journal entry (%s) has already been posted for this Daily Cost Posting.") % self.account_move_id.name)
        if 'contractor.period.close' in self.env:
            self.env['contractor.period.close']._check_period_not_locked(self.cost_date, self.company_id.id)

        if not self.line_ids:
            raise UserError(_("Cannot post an empty Daily Cost Posting with no operational line items."))

        analytic_acc = self.project_id.account_id
        if not analytic_acc:
            raise UserError(_("Project '%s' does not have a linked Analytic Account for project cost tagging.") % self.project_id.name)

        # Find or create general journal entry
        misc_journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)

        if not misc_journal:
            raise UserError(_("No General Journal found for company '%s'.") % self.company_id.name)

        expense_acc = self.company_id.default_expense_account_id or self.env['account.account'].search([
            ('internal_group', '=', 'expense'),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1)

        wip_acc = self.company_id.default_cost_accrual_account_id or self.env['account.account'].search([
            ('account_type', 'in', ['liability_current', 'equity']),
            ('company_ids', 'in', self.company_id.ids),
        ], limit=1) or expense_acc

        move_lines = []
        for cat_key, cat_label, cat_amt in (
            ('labour', 'Daily Labour Cost', self.labour_cost_total),
            ('material', 'Daily Material Consumption', self.material_cost_total),
            ('subcontractor', 'Daily Subcontractor Accrual', self.subcontractor_cost_total),
        ):
            if cat_amt > 0.0:
                move_lines.append((0, 0, {
                    'name': f"[DCP] {cat_label}: {self.project_id.name} ({self.cost_date})",
                    'account_id': expense_acc.id,
                    'debit': cat_amt,
                    'credit': 0.0,
                    'project_id': self.project_id.id,
                    'cost_head': cat_key,
                    'analytic_distribution': {str(analytic_acc.id): 100.0},
                }))
        move_lines.append((0, 0, {
            'name': f"Daily Accrued Site Costs: {self.project_id.name} ({self.cost_date})",
            'account_id': wip_acc.id,
            'debit': 0.0,
            'credit': self.grand_total_cost,
            'project_id': self.project_id.id,
        }))

        move = self.env['account.move'].create({
            'journal_id': misc_journal.id,
            'date': self.cost_date,
            'ref': f"DCP {self.name} - {self.project_id.name}",
            'move_type': 'entry',
            'project_id': self.project_id.id,
            'line_ids': move_lines,
        })
        move.action_post()

        self.with_context(bypass_cost_posting_source_check=True).write({
            'state': 'posted',
            'account_move_id': move.id,
        })

        # Update Master Budget actuals across all blocks
        self.env['contractor.project.budget']._refresh_project_consumption(self.project_id.id)

    def action_check_daily_completeness(self):
        """Checks all active projects with an active master budget for unexplained zero activity on cost_date."""
        self.ensure_one()
        target_date = self.cost_date or fields.Date.context_today(self)
        active_budgets = self.env['contractor.project.budget'].sudo().search([
            ('is_active', '=', True),
            ('company_id', '=', self.company_id.id),
        ])
        missing_projects = []
        for budget in active_budgets:
            proj = budget.project_id
            if getattr(proj, 'active', True) is False:
                continue
            has_dpr = self.env['contractor.daily.progress.report'].sudo().search_count([
                ('project_id', '=', proj.id), ('report_date', '=', target_date),
            ])
            has_att = self.env['contractor.site.attendance'].sudo().search_count([
                ('project_id', '=', proj.id), ('attendance_date', '=', target_date),
            ])
            dcp = self.sudo().search([
                ('project_id', '=', proj.id), ('cost_date', '=', target_date),
            ], limit=1)
            has_expl = bool(dcp and dcp.zero_activity_explanation)
            if not (has_dpr or has_att or dcp or has_expl):
                missing_projects.append(proj.display_name)

        if missing_projects:
            msg = _(
                "Completeness Check (%s): Unexplained zero activity detected on %d active project(s): %s"
            ) % (target_date, len(missing_projects), ", ".join(missing_projects))
            notif_type = 'warning'
        else:
            msg = _(
                "Completeness Check (%s): All active projects have logged site activity or documented zero-activity explanations."
            ) % target_date
            notif_type = 'success'

        self.message_post(body=msg, message_type='comment', subtype_xmlid='mail.mt_note')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Daily Accounting Completeness Check'),
                'message': msg,
                'type': notif_type,
                'sticky': bool(missing_projects),
            },
        }

        self.message_post(
            body=Markup(_("<strong>Posted to Budget & Accounts:</strong> Journal Entry <a href='#' data-oe-model='account.move' data-oe-id='%s'>%s</a> created and posted. Master Budget actuals refreshed.")) % (
                move.id, move.name
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_flag_correction_to_pm(self):
        """Accountant initiates a formal correction request instead of silently editing site figures."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_accountant')):
            raise AccessError(_("Access Denied: Only Accounting or Management users can flag cost discrepancies to the PM."))
        return {
            'name': _('Flag Cost Discrepancy to Project Manager'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.cost.correction.request',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_cost_posting_id': self.id,
                'default_project_id': self.project_id.id,
                'default_cost_date': self.cost_date,
            },
        }


class ContractorDailyCostPostingLine(models.Model):
    _name = 'contractor.daily.cost.posting.line'
    _description = 'Daily Cost Posting Line Item'
    _order = 'cost_category, id'

    cost_posting_id = fields.Many2one(
        'contractor.daily.cost.posting',
        string='Posting Reference',
        required=True,
        ondelete='cascade',
    )
    cost_category = fields.Selection([
        ('labour', 'Labour Direct Cost'),
        ('material', 'Material Consumed'),
        ('subcontractor', 'Subcontractor Accrual'),
    ], string='Cost Head', required=True)

    description = fields.Char(
        string='Description',
        required=True,
    )
    source_ref = fields.Char(
        string='Source Document Reference',
    )
    product_id = fields.Many2one(
        'product.product',
        string='Product / SKU',
    )
    employee_id = fields.Many2one(
        'hr.employee',
        string='Operative',
    )
    subcontractor_id = fields.Many2one(
        'res.partner',
        string='Subcontractor',
    )
    quantity = fields.Float(
        string='Quantity / Hours',
        default=1.0,
    )
    uom_id = fields.Many2one(
        'uom.uom',
        string='UoM',
    )
    unit_cost = fields.Monetary(
        string='Unit Cost / Rate',
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='cost_posting_id.currency_id',
        readonly=True,
    )
    subtotal_cost = fields.Monetary(
        string='Subtotal Cost',
        currency_field='currency_id',
        required=True,
    )

    def write(self, vals):
        if any(line.cost_posting_id.state == 'posted' for line in self):
            raise UserError(_("Lines belonging to a posted Daily Cost Posting cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if any(line.cost_posting_id.state == 'posted' for line in self):
            raise UserError(_("Lines belonging to a posted Daily Cost Posting cannot be deleted."))
        return super().unlink()


class ContractorCostCorrectionRequest(models.Model):
    _name = 'contractor.cost.correction.request'
    _description = 'Accountant Cost Correction Request to PM'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='Request Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New CCR'),
        index=True,
    )
    cost_posting_id = fields.Many2one(
        'contractor.daily.cost.posting',
        string='Daily Cost Posting',
        required=True,
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        related='cost_posting_id.project_id',
        store=True,
        readonly=True,
    )
    cost_date = fields.Date(
        string='Cost Date',
        related='cost_posting_id.cost_date',
        store=True,
        readonly=True,
    )
    accountant_id = fields.Many2one(
        'res.users',
        string='Flagged By Accountant',
        default=lambda self: self.env.user,
        readonly=True,
    )
    pm_id = fields.Many2one(
        'res.users',
        string='Responsible PM',
        related='project_id.user_id',
        store=True,
        readonly=True,
    )
    discrepancy_details = fields.Text(
        string='Identified Discrepancy & Misclassification',
        required=True,
        help='Describe incorrect cost head, unrecorded hours, wrong product rate, or project misallocation.',
    )
    proposed_correction = fields.Text(
        string='Accountant Proposed Correction',
        required=True,
        help='Detail the exact rectification requested from the site team.',
    )
    state = fields.Selection([
        ('requested', 'Flagged to PM'),
        ('acknowledged', 'Acknowledged by PM'),
        ('rectified', 'Rectified & Re-Posted'),
        ('rejected', 'Rejected by PM'),
    ], string='Status', default='requested', required=True, tracking=True, copy=False)

    pm_response = fields.Text(
        string='PM Response & Site Rectification Notes',
        tracking=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New CCR')) == _('New CCR'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.cost.correction.request') or _('New CCR')
        records = super().create(vals_list)

        for req in records:
            req.cost_posting_id.state = 'correction_requested'
            # Notify PM
            pm_user = req.pm_id or req.project_id.user_id
            if pm_user:
                self.env['mail.activity'].create({
                    'activity_type_id': self.env.ref('mail.mail_activity_data_warning').id,
                    'res_model_id': self.env['ir.model']._get_id('contractor.cost.correction.request'),
                    'res_id': req.id,
                    'user_id': pm_user.id,
                    'summary': f"Accounting Discrepancy Flagged: {req.name} on {req.project_id.name}",
                    'note': _("Discrepancy: %s\nProposed: %s") % (req.discrepancy_details, req.proposed_correction),
                    'date_deadline': fields.Date.context_today(req),
                })
        return records

    def action_pm_acknowledge(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_project_manager')):
            raise AccessError(_("Access Denied: Only Project Managers can acknowledge cost correction requests."))
        self.state = 'acknowledged'
        self.message_post(
            body=Markup(_("<strong>Acknowledged by PM:</strong> Reviewed by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_pm_rectify(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_project_manager')):
            raise AccessError(_("Access Denied: Only Project Managers can rectify cost correction requests."))
        clean_resp = (self.pm_response or '').strip()
        if len(clean_resp) < 10:
            raise UserError(_("Please document the site rectification actions taken (minimum 10 characters)."))

        self.state = 'rectified'
        # Re-sync posting lines
        self.cost_posting_id._sync_cost_lines()
        self.cost_posting_id.state = 'draft'

        self.message_post(
            body=Markup(_("<strong>Rectified by PM:</strong> Site figures adjusted. Daily Cost Posting re-synced.<br/>Notes: %s")) % self.pm_response,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_pm_reject(self):
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_project_manager')):
            raise AccessError(_("Access Denied: Only Project Managers can reject cost correction requests."))
        clean_resp = (self.pm_response or '').strip()
        if len(clean_resp) < 10:
            raise UserError(_("Please provide technical justification for rejecting the accountant request (minimum 10 characters)."))

        self.state = 'rejected'
        self.cost_posting_id.state = 'draft'

        self.message_post(
            body=Markup(_("<strong>Rejected by PM:</strong> Operational figures sustained.<br/>Justification: %s")) % self.pm_response,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

