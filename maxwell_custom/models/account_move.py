import base64
from odoo import fields, models
from odoo.tools.misc import file_open


class AccountMove(models.Model):
    _inherit = 'account.move'

    supplier_vendor_id = fields.Char(
        string='Supplier / Vendor ID',
        help='Supplier / Vendor Identifier displayed on printed documents.',
    )
    lpo_reference = fields.Char(
        string='LPO Reference / No.',
        help='LPO Reference Number.',
    )
    lpo_received_date = fields.Date(
        string='LPO Received on',
        help='Date when the LPO was received.',
    )
    maxwell_customer_id = fields.Char(
        string='Maxwell Customer ID',
        help='Maxwell Customer Identifier displayed on printed documents.',
    )
    quote_reference_no = fields.Char(
        string='Quote Reference No.',
        help='Reference quotation number.',
    )
    work_completed_date = fields.Date(
        string='Work Completed on',
        help='Date of work completion.',
    )
    prepared_by_designation = fields.Char(
        string='Prepared By Designation',
        default='Accountant',
    )

    def _get_name_invoice_report(self):
        self.ensure_one()
        return 'maxwell_custom.report_invoice_document_maxwell'

    def _get_maxwell_banner_data_uri(self, banner_type='header'):
        filename = 'maxwell_header.jpg' if banner_type == 'header' else 'maxwell_footer.jpg'
        with file_open(f'maxwell_custom/static/src/img/{filename}', 'rb') as f:
            b64_data = base64.b64encode(f.read()).decode('ascii')
        return f'data:image/jpeg;base64,{b64_data}'

    def _get_maxwell_amount_in_words(self):
        self.ensure_one()
        if self.currency_id:
            return self.currency_id.amount_to_text(self.amount_total)
        return ''

