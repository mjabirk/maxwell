# -*- coding: utf-8 -*-
from odoo import models, fields, api


class ContractorDocumentVersion(models.Model):
    _name = 'contractor.document.version'
    _description = 'Contractor Document Immutable Version Snapshot'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'contractor.no.delete.mixin']
    _mail_post_access = 'read'
    _order = 'version_number desc, id desc'
    _rec_name = 'display_name'

    name = fields.Char(
        string='Version Reference',
        required=True,
        index=True,
        readonly=True,
    )
    res_model = fields.Char(
        string='Target Document Model',
        required=True,
        index=True,
        readonly=True,
    )
    res_id = fields.Integer(
        string='Target Document ID',
        required=True,
        index=True,
        readonly=True,
    )
    version_number = fields.Integer(
        string='Version Number',
        default=1,
        required=True,
        index=True,
        readonly=True,
    )
    editor_id = fields.Many2one(
        'res.users',
        string='Editor / Author',
        default=lambda self: self.env.user,
        required=True,
        readonly=True,
    )
    timestamp = fields.Datetime(
        string='Snapshot Timestamp',
        default=fields.Datetime.now,
        required=True,
        readonly=True,
    )
    reason = fields.Text(
        string='Revision Reason',
        required=True,
        readonly=True,
    )
    cost = fields.Monetary(
        string='Snapshot Cost',
        currency_field='currency_id',
        readonly=True,
    )
    margin = fields.Float(
        string='Snapshot Margin (%)',
        readonly=True,
    )
    total_value = fields.Monetary(
        string='Snapshot Total Value',
        currency_field='currency_id',
        readonly=True,
    )
    approval_state = fields.Char(
        string='Document State at Snapshot',
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        readonly=True,
    )
    snapshot_data = fields.Text(
        string='Raw JSON Snapshot Data',
        readonly=True,
        help='Serialized JSON string storing full record attributes and line items.',
    )
    snapshot_html = fields.Html(
        string='Snapshot HTML Preview',
        readonly=True,
        sanitize=True,
        help='Rendered read-only table representation of the document at snapshot time.',
    )
    attachment_ids = fields.Many2many(
        'ir.attachment',
        'contractor_document_version_attachment_rel',
        'version_id',
        'attachment_id',
        string='Archived Attachments',
        readonly=True,
        help='Attachments archived with this document version for future reference.',
    )
    attachment_count = fields.Integer(
        string='Attachments',
        compute='_compute_attachment_count',
    )

    @api.depends('attachment_ids')
    def _compute_attachment_count(self):
        for rec in self:
            rec.attachment_count = len(rec.attachment_ids)

    def name_get(self):
        result = []
        for record in self:
            name = record.name or f"v{record.version_number} [{record.res_model} #{record.res_id}]"
            result.append((record.id, name))
        return result

