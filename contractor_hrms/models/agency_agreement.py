# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorAgencyAgreement(models.Model):
    _name = 'contractor.agency.agreement'
    _description = 'Manpower Supply Agency Service Agreement'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.state.lock.mixin', 'contractor.no.delete.mixin']
    _order = 'agreement_date desc, id desc'

    name = fields.Char(
        string='Agreement Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Agreement'),
        index=True,
    )
    agency_id = fields.Many2one(
        'res.partner',
        string='Manpower Supply Agency',
        required=True,
        domain="[('is_company', '=', True)]",
        tracking=True,
        index=True,
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
    agreement_date = fields.Date(
        string='Agreement Date',
        default=fields.Date.context_today,
        required=True,
        tracking=True,
    )
    start_date = fields.Date(
        string='Service Start Date',
        required=True,
        tracking=True,
    )
    end_date = fields.Date(
        string='Service End Date',
        required=True,
        tracking=True,
    )
    rate_type = fields.Selection([
        ('hourly', 'Hourly Billing Rate'),
        ('daily', 'Daily Shift Wage Rate'),
    ], string='Billing Basis', default='hourly', required=True, tracking=True)

    agreed_rate = fields.Monetary(
        string='Agreed Contract Billing Rate',
        currency_field='currency_id',
        required=True,
        tracking=True,
        help='Unit rate billed by the agency (per hour or per standard daily shift).',
    )
    payment_terms = fields.Char(
        string='Payment Terms',
        default='30 Days Net from Verified Reconciliation',
        tracking=True,
    )
    worker_count = fields.Integer(
        string='Contracted Worker Headcount',
        default=1,
        help='Target or contracted number of personnel supplied under this SLA.',
    )
    contract_attachment_ids = fields.Many2many(
        'ir.attachment',
        'contractor_agency_agreement_attachment_rel',
        'agreement_id',
        'attachment_id',
        string='Contract Documents & Signed SLA',
    )
    notes = fields.Text(
        string='Terms, Overtime Conditions & Special Notes',
    )
    state = fields.Selection([
        ('draft', 'Draft Agreement'),
        ('active', 'Active SLA'),
        ('expired', 'Expired'),
        ('terminated', 'Terminated'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)

    employee_ids = fields.One2many(
        'hr.employee',
        'agency_agreement_id',
        string='Deployed Agency Operatives',
    )
    employee_count = fields.Integer(
        string='Deployed Operatives Count',
        compute='_compute_employee_count',
    )

    @api.depends('employee_ids')
    def _compute_employee_count(self):
        for rec in self:
            rec.employee_count = len(rec.employee_ids)

    @api.constrains('start_date', 'end_date')
    def _check_dates(self):
        for rec in self:
            if rec.start_date and rec.end_date and rec.start_date > rec.end_date:
                raise ValidationError(_("Service Start Date cannot be subsequent to Service End Date."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New Agreement')) == _('New Agreement'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.agency.agreement') or _('New Agreement')
        return super().create(vals_list)

    def action_activate(self):
        self.ensure_one()
        self._ensure_state('draft', _("activate agency agreement"))
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_hr_officer')):
            raise AccessError(_("Access Denied: Only HR Officers or Managers can activate agency agreements."))
        if self.agreed_rate <= 0:
            raise UserError(_("Agreed billing rate must be strictly greater than zero."))
        self.with_context(bypass_state_lock=True).write({'state': 'active'})
        self.message_post(
            body=Markup(_("<strong>Agreement Activated:</strong> Manpower SLA confirmed active.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_terminate(self):
        self.ensure_one()
        self._ensure_state(('draft', 'active', 'expired'), _("terminate agency agreement"))
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_manager')):
            raise AccessError(_("Access Denied: Only a Contractor Manager can terminate agency agreements."))
        self.with_context(bypass_state_lock=True).write({'state': 'terminated'})
        self.message_post(
            body=Markup(_("<strong>Agreement Terminated:</strong> Manpower SLA concluded.")),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

