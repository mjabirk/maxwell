# -*- coding: utf-8 -*-
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError, ValidationError


class CrmLead(models.Model):
    _name = 'crm.lead'
    _inherit = ['crm.lead', 'contractor.no.delete.mixin', 'contractor.reason.mixin']

    # Mandatory governance fields on CRM Lead
    partner_id = fields.Many2one(
        'res.partner',
        string='Customer',
        required=True,
        ondelete='restrict',
        index=True,
        help='Customer / Client organization inquiring for contracting services.',
    )
    contact_name = fields.Char(
        string='Contact Name',
        required=True,
        help='Full name of primary client contact person.',
    )
    source_id = fields.Many2one(
        'utm.source',
        string='Inquiry Source',
        required=True,
        ondelete='restrict',
        help='Marketing or referral channel through which the project inquiry originated.',
    )
    description = fields.Html(
        string='Scope of Work / Description',
        required=True,
        help='Initial client project scope, site address, and technical specifications.',
    )
    assigned_manager_id = fields.Many2one(
        'res.users',
        string='Assigned Estimating Manager',
        required=True,
        index=True,
        domain="[('share', '=', False)]",
        help='Contractor Manager responsible for assigning site engineers and approving quotations.',
    )
    enquiry_date = fields.Datetime(
        string='Enquiry Date',
        default=fields.Datetime.now,
        readonly=True,
        copy=False,
        help='Timestamp when the initial contracting inquiry was logged into the system.',
    )
    project_location = fields.Char(
        string='Project Location',
        tracking=True,
        help='Geographical district, plot number, or site address for the prospective contracting project.',
    )
    expected_project_type = fields.Selection([
        ('fit_out', 'Interior Fit-Out'),
        ('renovation', 'Renovation / Refurbishment'),
        ('civil_mep', 'Civil & MEP Works'),
        ('other', 'Other Contracting Works'),
    ], string='Expected Project Type', default='fit_out', tracking=True)
    assigned_engineer_id = fields.Many2one(
        'hr.employee',
        string='Assigned Engineer',
        index=True,
        tracking=True,
        help='Default site engineer designated for technical inspection and estimation.',
    )
    contractor_remarks = fields.Text(
        string='Lead Remarks',
        help='Commercial and operational remarks captured during inquiry qualification.',
    )
    closed_or_held_date = fields.Datetime(
        string='Closed / Placed On Hold Date',
        readonly=True,
        copy=False,
    )

    # Controlled Contractor Pipeline Status
    contractor_state = fields.Selection([
        ('open', 'Open Inquiry'),
        ('site_visit', 'Site Visit Stage'),
        ('quoted', 'Cost Estimated / Quoted'),
        ('sent_to_client', 'Sent to Client'),
        ('negotiation', 'Under Client Negotiation'),
        ('won', 'Won / Awarded'),
        ('lost', 'Lost'),
        ('on_hold', 'On Hold'),
        ('declined', 'Declined / Rejected'),
    ], string='Contractor Pipeline Stage', default='open', tracking=True, required=True, copy=False, group_expand=True)

    requires_fresh_cost_review = fields.Boolean(
        string='Requires Fresh Cost Review',
        default=False,
        copy=False,
        tracking=True,
        help='Flagged when a previously inactive/lost/on-hold lead is reopened, requiring re-estimation of labor and material rates.',
    )

    # Relationship to Site Visits
    site_visit_ids = fields.One2many(
        'contractor.site.visit',
        'lead_id',
        string='Site Visits',
    )
    site_visit_count = fields.Integer(
        string='Site Visit Count',
        compute='_compute_site_visit_info',
        store=True,
    )
    has_completed_site_visit = fields.Boolean(
        string='Has Completed Site Visit',
        compute='_compute_site_visit_info',
        store=True,
    )

    # Relationship to Linked Quotations
    contractor_order_ids = fields.One2many(
        'sale.order',
        'lead_id',
        string='Contractor Cost Estimates & Quotations',
    )
    contractor_order_count = fields.Integer(
        string='Quotation Count',
        compute='_compute_order_count',
    )

    @api.depends('site_visit_ids.state')
    def _compute_site_visit_info(self):
        for lead in self:
            visits = lead.site_visit_ids
            lead.site_visit_count = len(visits)
            lead.has_completed_site_visit = any(v.state == 'completed' for v in visits)

    def _compute_order_count(self):
        for lead in self:
            lead.contractor_order_count = len(lead.contractor_order_ids)

    def write(self, vals):
        if vals.get('contractor_state') in ('lost', 'on_hold', 'declined'):
            vals.setdefault('closed_or_held_date', fields.Datetime.now())
        return super().write(vals)

    # Controlled Stage Actions requiring Reason Wizard
    def action_set_lost(self):
        """Forces mandatory reason wizard when marking a lead as Lost."""
        self.ensure_one()
        return self.action_open_reason_wizard(action_type='decline', target_state='lost')

    def action_set_on_hold(self):
        """Forces mandatory reason wizard when placing a lead On Hold."""
        self.ensure_one()
        return self.action_open_reason_wizard(action_type='hold', target_state='on_hold')

    def action_set_declined(self):
        """Forces mandatory reason wizard when declining an inquiry."""
        self.ensure_one()
        return self.action_open_reason_wizard(action_type='decline', target_state='declined')

    def action_reopen_lead(self):
        """Reopens a lost or on-hold lead, mandating a fresh cost review before quotation submission."""
        self.ensure_one()
        reassess_days = self.company_id.lead_reopen_reassessment_days or 30
        elapsed_days = 0
        if self.closed_or_held_date:
            elapsed_days = max(0, (fields.Datetime.now() - self.closed_or_held_date).days)
        self.write({
            'contractor_state': 'open',
            'requires_fresh_cost_review': True,
        })
        self.message_post(
            body=Markup(_(
                "<strong>Lead Reopened:</strong> Contractor pipeline status reset to 'Open Inquiry' "
                "(Inactive duration: %d days; reassessment threshold: %d days).<br/>"
                "<span class='text-danger'><strong>Governance Notice:</strong> Fresh cost review is strictly mandatory "
                "before any new quotation can be submitted for approval.</span>"
            )) % (elapsed_days, reassess_days),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        return True

    def action_clear_fresh_cost_review(self):
        """Allows assigned manager or engineer to attest that fresh rates have been verified."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group('contractor_core.group_contractor_sales_admin') or self.env.user.has_group('contractor_core.group_contractor_site_engineer')):
            raise AccessError(_("Access Denied: Only Sales/Estimation Engineers or Managers can attest a fresh cost review."))
        self.write({'requires_fresh_cost_review': False})
        self.message_post(
            body=Markup(_("<strong>Fresh Cost Review Attestation:</strong> Labor and material cost basis re-verified by %s.")) % self.env.user.name,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

    def action_create_cost_estimate(self):
        """Site Visit Upload Gate: Block creating a Cost Estimate or Quotation from a Lead
        until at least one Site Visit record is marked 'completed' with observations/uploads.
        """
        self.ensure_one()
        if self.contractor_state in ('won', 'lost', 'declined'):
            raise UserError(_(
                "Lead '%s' is closed (%s). Reopen the lead before creating a new Cost Estimate."
            ) % (self.name, self.contractor_state))
        if not self.has_completed_site_visit:
            raise UserError(_(
                "Site Visit Upload Gate Enforced:\n\n"
                "You cannot create a Cost Estimate or Quotation for Lead '%s'.\n"
                "Contractor platform governance strictly requires at least one Site Visit "
                "marked as COMPLETED with field observations, measurements, photos, and drawings."
            ) % self.name)

        return {
            'type': 'ir.actions.act_window',
            'name': _('New Cost Estimate & Quotation'),
            'res_model': 'sale.order',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_partner_id': self.partner_id.id,
                'default_lead_id': self.id,
                'default_origin': self.name,
                'default_user_id': self.env.user.id,
                'default_company_id': self.company_id.id,
            },
        }

    def action_sale_quotations_new(self):
        """Redirect standard sale_crm New Quotation through the Site Visit Upload Gate."""
        return self.action_create_cost_estimate()

    def action_new_quotation(self):
        """Redirect standard sale_crm quotation action through the Site Visit Upload Gate."""
        return self.action_create_cost_estimate()

    def action_view_site_visits(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('contractor_crm_estimation.action_contractor_site_visit')
        action['domain'] = [('lead_id', '=', self.id)]
        action['context'] = {
            'default_lead_id': self.id,
            'default_partner_id': self.partner_id.id,
            'default_assigned_engineer_id': self.assigned_engineer_id.id if self.assigned_engineer_id else False,
        }
        return action

    def action_view_contractor_orders(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Cost Estimates & Quotations'),
            'res_model': 'sale.order',
            'view_mode': 'list,form',
            'domain': [('lead_id', '=', self.id)],
            'context': {'default_lead_id': self.id, 'default_partner_id': self.partner_id.id, 'create': False},
        }

