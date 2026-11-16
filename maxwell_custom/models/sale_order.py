import base64
from odoo import api, fields, models
from odoo.tools.misc import file_open


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    supplier_vendor_id = fields.Char(
        string='Supplier / Vendor ID',
        help='Maxwell Supplier / Vendor Identifier displayed on printed documents.',
    )
    lpo_reference = fields.Char(
        string='LPO Reference / No.',
        help='Customer LPO Reference Number.',
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
    payment_due_date = fields.Date(
        string='Payment Due Date',
        help='Expected payment due date.',
    )
    prepared_by_designation = fields.Char(
        string='Prepared By Designation',
        default='Accountant',
    )

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

    def _prepare_invoice(self):
        invoice_vals = super()._prepare_invoice()
        invoice_vals.update({
            'supplier_vendor_id': self.supplier_vendor_id,
            'lpo_reference': self.lpo_reference or self.client_order_ref or '',
            'lpo_received_date': self.lpo_received_date,
            'maxwell_customer_id': self.maxwell_customer_id or self.partner_id.ref or '',
            'quote_reference_no': self.quote_reference_no or self.name or '',
            'work_completed_date': self.work_completed_date,
            'prepared_by_designation': self.prepared_by_designation or 'Accountant',
        })
        return invoice_vals

