# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ContractorSiteVisit(models.Model):
    _name = 'contractor.site.visit'
    _description = 'Contractor Site Visit Inspection & Field Survey'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin', 'contractor.reason.mixin']
    _order = 'scheduled_date desc, id desc'

    name = fields.Char(
        string='Site Visit Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New Site Visit'),
        index=True,
    )
    lead_id = fields.Many2one(
        'crm.lead',
        string='Source CRM Lead',
        required=True,
        ondelete='restrict',
        index=True,
        tracking=True,
        help='A CRM Lead is strictly required before a Site Visit can be scheduled.',
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Client / Partner',
        related='lead_id.partner_id',
        store=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='lead_id.company_id',
        store=True,
        readonly=True,
    )
    assigned_engineer_id = fields.Many2one(
        'hr.employee',
        string='Assigned Site Engineer',
        required=True,
        index=True,
        tracking=True,
        help='Qualified site engineer responsible for conducting inspections, measurements, and capturing site conditions.',
    )
    engineer_user_id = fields.Many2one(
        'res.users',
        string='Engineer User Account',
        related='assigned_engineer_id.user_id',
        store=True,
        readonly=True,
    )
    scheduled_date = fields.Datetime(
        string='Scheduled Date',
        required=True,
        default=fields.Datetime.now,
        tracking=True,
    )
    completion_date = fields.Datetime(
        string='Completion Date',
        readonly=True,
        tracking=True,
    )
    state = fields.Selection([
        ('draft', 'Scheduled'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', required=True, tracking=True)

    observations = fields.Text(
        string='Site Observations & Field Conditions',
        help='Detailed technical notes on site terrain, access limitations, utilities, structural status, and hazards.',
    )
    measurements = fields.Text(
        string='Dimensions & Site Measurements',
        help='Precise dimensional data, square meters/cubic volume, level checks, and survey measurements.',
    )
    photo_ids = fields.Many2many(
        'ir.attachment',
        'contractor_site_visit_photos_rel',
        'visit_id',
        'attachment_id',
        string='Site Photos',
        help='Mandatory photographic evidence of current site conditions before estimation.',
    )
    drawing_ids = fields.Many2many(
        'ir.attachment',
        'contractor_site_visit_drawings_rel',
        'visit_id',
        'attachment_id',
        string='Sketches & As-Built Drawings',
        help='Mandatory architectural drawings, CAD plans, or field sketch attachments.',
    )

    @api.model
    def _check_site_visit_assignment_rights(self):
        user = self.env.user
        allowed_groups = (
            'base.group_system',
            'contractor_core.group_contractor_manager',
            'contractor_core.group_contractor_md',
            'contractor_core.group_contractor_sales_admin',
            'contractor_core.group_contractor_project_manager',
        )
        if not self.env.su and not any(user.has_group(g) for g in allowed_groups):
            raise AccessError(_(
                "Access Denied: Only Contractor Management, Project Managers, or Sales Administration "
                "may schedule or reassign Site Visits."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_site_visit_assignment_rights()
        for vals in vals_list:
            if vals.get('name', _('New Site Visit')) == _('New Site Visit'):
                vals['name'] = self.env['ir.sequence'].next_by_code('contractor.site.visit') or _('New Site Visit')
        records = super().create(vals_list)
        for record in records:
            if record.lead_id and record.lead_id.contractor_state == 'open':
                record.lead_id.contractor_state = 'site_visit'
        return records

    def write(self, vals):
        if 'assigned_engineer_id' in vals:
            self._check_site_visit_assignment_rights()
        return super().write(vals)

    def action_complete(self):
        """Validates that observations, measurements, photos, and drawings are present before completing."""
        self.ensure_one()
        obs = (self.observations or '').strip()
        meas = (self.measurements or '').strip()

        if len(obs) < 5:
            raise UserError(_(
                "Site Visit Upload Gate: Detailed site observations (minimum 5 characters) are mandatory "
                "before completing the inspection."
            ))
        if len(meas) < 5:
            raise UserError(_(
                "Site Visit Upload Gate: Detailed field measurements and dimensions are mandatory "
                "before completing the inspection."
            ))
        if not self.photo_ids:
            raise UserError(_(
                "Site Visit Upload Gate: At least one site photograph must be uploaded "
                "before this visit can be marked as completed."
            ))
        if not self.drawing_ids:
            raise UserError(_(
                "Site Visit Upload Gate: At least one architectural drawing, CAD plan, or field sketch "
                "must be uploaded before this visit can be marked as completed."
            ))

        self.write({
            'state': 'completed',
            'completion_date': fields.Datetime.now(),
        })

        if self.lead_id:
            self.lead_id.contractor_state = 'site_visit'
            self.lead_id.message_post(
                body=Markup(_(
                    "<strong>Site Visit Completed:</strong> %s conducted by %s.<br/>"
                    "Photos: %d attached | Drawings: %d attached."
                )) % (self.name, self.assigned_engineer_id.name, len(self.photo_ids), len(self.drawing_ids)),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
        return True

    def action_cancel(self):
        """Cancels the site visit with mandatory justification."""
        self.ensure_one()
        return self.action_open_reason_wizard(action_type='cancel', target_state='cancelled')

