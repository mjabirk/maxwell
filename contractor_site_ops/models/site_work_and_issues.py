# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorSiteWorkLog(models.Model):
    _name = 'contractor.site.work.log'
    _description = 'Subcontractor & Crew Daily Work Output Log'
    _inherit = [
        'mail.thread',
        'mail.activity.mixin',
        'contractor.site.lock.mixin',
        'contractor.state.lock.mixin',
        'contractor.no.delete.mixin',
    ]
    _order = 'work_date desc, id desc'

    name = fields.Char(
        string='Work Log Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Log'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project Site',
        required=True,
        index=True,
        tracking=True,
    )
    work_date = fields.Date(
        string='Execution Date',
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
    crew_type = fields.Selection([
        ('in_house', 'In-House Specialized Crew'),
        ('subcontractor', 'Subcontractor / Specialist Trade'),
    ], string='Workforce Type', default='subcontractor', required=True, tracking=True)

    subcontractor_id = fields.Many2one(
        'res.partner',
        string='Subcontractor Company',
        domain="[('is_company', '=', True)]",
        tracking=True,
    )
    work_package = fields.Char(
        string='Work Package / Structural Scope',
        required=True,
        tracking=True,
    )
    uom_id = fields.Many2one(
        'uom.uom',
        string='Output Unit of Measure',
        required=True,
    )
    subcontractor_claimed_qty = fields.Float(
        string='Subcontractor Claimed Qty',
        default=0.0,
        tracking=True,
    )
    site_verified_qty = fields.Float(
        string='Site Engineer Verified Qty',
        required=True,
        default=0.0,
        tracking=True,
        help='Strictly measured and verified physical output installed on site today.',
    )
    agreed_unit_rate = fields.Monetary(
        string='Agreed Contract Unit Rate',
        currency_field='currency_id',
        required=True,
        tracking=True,
        help='Contractually agreed unit cost rate for this trade package.',
    )
    accrued_subcontractor_cost = fields.Monetary(
        string='Daily Accrued Subcontract Cost',
        currency_field='currency_id',
        compute='_compute_accrued_cost',
        store=True,
        help='Calculated daily progress cost accrual (Site Verified Qty * Agreed Unit Rate).',
    )
    state = fields.Selection([
        ('draft', 'Draft Output Log'),
        ('submitted', 'Submitted to PM'),
        ('pm_certified', 'PM Certified (Progress Accrual)'),
        ('declined', 'Declined'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    certified_by = fields.Many2one(
        'res.users',
        string='Certified By PM',
        readonly=True,
        copy=False,
    )
    certification_date = fields.Datetime(
        string='Certification Datetime',
        readonly=True,
        copy=False,
    )
    notes = fields.Text(
        string='Technical Notes & Grid Coordinates',
    )
    cost_posting_id = fields.Many2one(
        'contractor.daily.cost.posting',
        string='Daily Cost Posting',
        readonly=True,
        copy=False,
    )

    @api.depends('site_verified_qty', 'agreed_unit_rate')
    def _compute_accrued_cost(self):
        for log in self:
            log.accrued_subcontractor_cost = (log.site_verified_qty or 0.0) * (log.agreed_unit_rate or 0.0)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Log')) == _('New Log'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.site.work.log') or _('New Log')
        return super().create(vals_list)

    def action_submit(self):
        self.ensure_one()
        self._ensure_state('draft', _("submit work log"))
        if self.site_verified_qty <= 0:
            raise UserError(_("Site verified quantity must be strictly greater than zero to submit work log."))

        self.with_context(bypass_state_lock=True).write({'state': 'submitted'})
        pm_user = self.project_id.user_id
        if pm_user:
            self.env['mail.activity'].create({
                'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                'res_model_id': self.env['ir.model']._get_id('contractor.site.work.log'),
                'res_id': self.id,
                'user_id': pm_user.id,
                'summary': f"Certify Daily Subcontract Output: {self.name}",
                'note': _("Subcontractor %s claimed %.2f %s. Verified: %.2f %s (%s %.2f accrual).") % (
                    self.subcontractor_id.name if self.subcontractor_id else 'In-House Crew',
                    self.subcontractor_claimed_qty, self.uom_id.name,
                    self.site_verified_qty, self.uom_id.name,
                    self.currency_id.symbol or '', self.accrued_subcontractor_cost
                ),
                'date_deadline': fields.Date.context_today(self),
            })

        self.message_post(
            body=Markup(_("<strong>Work Log Submitted:</strong> Forwarded to Project Manager for progress certification.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_pm_certify(self):
        """Certifies daily progress output for real-time cost accrual."""
        self.ensure_one()
        self._ensure_state(('draft', 'submitted'), _("certify work log"))
        if not self.env.user.has_group('contractor_core.group_contractor_project_manager'):
            raise UserError(_("Access Denied: Only a Contractor Project Manager can certify subcontractor work logs."))

        self.with_context(bypass_state_lock=True).write({
            'state': 'pm_certified',
            'certified_by': self.env.user.id,
            'certification_date': fields.Datetime.now(),
        })

        # Auto-trigger / sync Daily Cost Posting
        posting = self.env['contractor.daily.cost.posting'].get_or_create_daily_posting(
            project_id=self.project_id.id,
            cost_date=self.work_date,
        )
        self.with_context(bypass_state_lock=True).write({'cost_posting_id': posting.id})

        self.message_post(
            body=Markup(_(
                "<strong>Progress Output Certified:</strong> Certified by PM %s.<br/>"
                "<em>Note: Certifies operational site progress only; formal vendor payment remains subject to contract milestone certification.</em><br/>"
                "Synced with Daily Cost Posting <a href='#' data-oe-model='contractor.daily.cost.posting' data-oe-id='%s'>%s</a>."
            )) % (self.env.user.name, posting.id, posting.name),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_decline(self):
        self.ensure_one()
        self._ensure_state(('draft', 'submitted'), _("decline work log"))
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_project_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Project Manager can decline subcontractor work logs."))
        return {
            'name': _('Decline Work Log'),
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


class ContractorSiteIssue(models.Model):
    _name = 'contractor.site.issue'
    _description = 'Site Incident & Critical Obstruction Issue'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.state.lock.mixin', 'contractor.no.delete.mixin']
    _order = 'issue_date desc, id desc'
    _state_lock_editable_states = ('open', 'escalated')

    name = fields.Char(
        string='Issue Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Issue'),
        index=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project Site',
        required=True,
        index=True,
        tracking=True,
    )
    dpr_id = fields.Many2one(
        'contractor.daily.progress.report',
        string='Associated DPR',
        ondelete='cascade',
        tracking=True,
    )
    issue_date = fields.Date(
        string='Incident Date',
        required=True,
        default=fields.Date.context_today,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='project_id.company_id',
        store=True,
        readonly=True,
    )
    issue_type = fields.Selection([
        ('delay', 'Project Delay / Obstruction'),
        ('safety_incident', 'Safety Incident / Near Miss'),
        ('access_problem', 'Site Access / Permit Issue'),
        ('design_query', 'Design / Drawing Discrepancy (RFI)'),
        ('material_shortfall', 'Material Shortage / Delivery Defect'),
    ], string='Issue Category', default='delay', required=True, tracking=True)

    severity = fields.Selection([
        ('routine', 'Routine Site Note'),
        ('high', 'High Operational Impact'),
        ('material_safety_critical', 'MATERIAL SAFETY CRITICAL / STOP WORK'),
    ], string='Severity Level', default='routine', required=True, tracking=True)

    description = fields.Text(
        string='Detailed Issue Description & Immediate Site Action',
        required=True,
        tracking=True,
    )
    photo_attachment_ids = fields.Many2many(
        'ir.attachment',
        'contractor_site_issue_attachment_rel',
        'issue_id',
        'attachment_id',
        string='Site Photographs / Evidence',
        help='Photographs or field documents illustrating the obstruction or safety condition.',
    )
    state = fields.Selection([
        ('open', 'Open Site Issue'),
        ('escalated', 'Escalated to Project Manager'),
        ('resolved', 'Resolved & Cleared'),
    ], string='Status', default='open', required=True, tracking=True, copy=False)

    resolution_notes = fields.Text(
        string='Corrective Action & Resolution Notes',
        tracking=True,
    )
    resolved_by = fields.Many2one(
        'res.users',
        string='Resolved By',
        readonly=True,
        copy=False,
    )
    resolved_date = fields.Datetime(
        string='Resolution Date',
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Issue')) == _('New Issue'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.site.issue') or _('New Issue')

        records = super().create(vals_list)

        # Critical Safety Alert Escalation
        for issue in records:
            if issue.severity == 'material_safety_critical':
                issue._escalate_critical_safety_alert()

        return records

    def _escalate_critical_safety_alert(self):
        """Immediately dispatches high-priority alert activity to Project Manager and Contractor Managers."""
        self.ensure_one()
        self.state = 'escalated'

        target_users = self.env['res.users']
        if self.project_id.user_id:
            target_users |= self.project_id.user_id

        group_mgr = self.env.ref('contractor_core.group_contractor_manager', raise_if_not_found=False)
        if group_mgr:
            target_users |= (group_mgr.all_user_ids | group_mgr.user_ids).filtered(lambda u: u.active)

        for user in target_users:
            self.env['mail.activity'].create({
                'activity_type_id': self.env.ref('mail.mail_activity_data_warning').id,
                'res_model_id': self.env['ir.model']._get_id('contractor.site.issue'),
                'res_id': self.id,
                'user_id': user.id,
                'summary': f"CRITICAL SITE SAFETY ALERT: {self.name} on {self.project_id.name}",
                'note': _(
                    "STOP WORK / SAFETY CRITICAL INCIDENT LOGGED.\n\n"
                    "Project: %s\nCategory: %s\nDescription: %s\nImmediate management intervention required."
                ) % (self.project_id.name, self.issue_type, self.description),
                'date_deadline': fields.Date.context_today(self),
            })

        self.message_post(
            body=Markup(_(
                "<div class='alert alert-danger'><strong>CRITICAL SAFETY ALERT TRIGGERED:</strong> "
                "Severity is Material Safety Critical / Stop Work. Escalated immediately to Project Manager and Operations Management.</div>"
            )),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_resolve_issue(self):
        self.ensure_one()
        self._ensure_state(('open', 'escalated'), _("resolve and close issue"))
        clean_notes = (self.resolution_notes or '').strip()
        if len(clean_notes) < 10:
            raise UserError(_("Please provide documented resolution notes (minimum 10 characters) before closing this issue."))

        self.with_context(bypass_state_lock=True).write({
            'state': 'resolved',
            'resolved_by': self.env.user.id,
            'resolved_date': fields.Datetime.now(),
        })

        self.message_post(
            body=Markup(_("<strong>Site Issue Resolved:</strong> Cleared by %s.<br/>Corrective Actions: %s")) % (
                self.env.user.name, self.resolution_notes
            ),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

