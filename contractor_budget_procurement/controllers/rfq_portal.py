# -*- coding: utf-8 -*-
import base64
import hmac
from markupsafe import Markup
from odoo import http, fields, _
from odoo.http import request


class ContractorRfqPortalController(http.Controller):
    """Zero-login tokenized public RFQ portal allowing invited vendors to submit quotes & PDFs."""

    def _get_authorized_invite(self, invite_id, token):
        invite = request.env['contractor.rfq.vendor.invite'].sudo().browse(invite_id).exists()
        if not invite or not invite.access_token or not token:
            return None
        if not hmac.compare_digest(str(invite.access_token), str(token)):
            return None
        return invite

    def _prepare_attachment_links(self, invite):
        links = []
        for att in invite.quotation_attachment_ids.sudo():
            if not att.access_token:
                att.generate_access_token()
            links.append({
                'id': att.id,
                'name': att.name,
                'mimetype': att.mimetype or '',
                'url': f"/web/content/{att.id}?access_token={att.access_token}&download=1",
            })
        return links

    def _build_portal_context(self, invite, error_message=None, form_values=None):
        company = invite.company_id.sudo() or request.env.company.sudo()
        currency = invite.currency_id.sudo() or company.currency_id
        form_values = form_values or {}
        line_inputs = {}
        for line in invite.line_ids:
            raw_price = form_values.get(f'unit_price_{line.id}', line.quoted_price or 0.0)
            try:
                price_num = float(raw_price) if raw_price not in (None, '') else 0.0
            except (TypeError, ValueError):
                price_num = 0.0
            line_inputs[line.id] = {
                'price': raw_price,
                'subtotal': (line.qty or 0.0) * price_num,
                'note': form_values.get(f'note_{line.id}', line.vendor_line_note or ''),
            }
        return {
            'invite': invite,
            'company': company,
            'currency': currency,
            'readonly_mode': invite.state in ('submitted', 'approved', 'expired'),
            'attachment_links': self._prepare_attachment_links(invite),
            'error_message': error_message,
            'vendor_reference': form_values.get('vendor_reference', invite.vendor_reference or ''),
            'delivery_days': form_values.get('delivery_days', invite.delivery_days or 7),
            'payment_terms': form_values.get('payment_terms', invite.payment_terms or '30 Days Net'),
            'vendor_notes': form_values.get('vendor_notes', invite.vendor_notes or ''),
            'line_inputs': line_inputs,
        }

    @http.route(
        '/rfq/quote/<int:invite_id>/<string:token>',
        type='http',
        auth='public',
        website=False,
        methods=['GET'],
        csrf=True,
    )
    def rfq_quote_page(self, invite_id, token, **kwargs):
        invite = self._get_authorized_invite(invite_id, token)
        if not invite:
            raise request.not_found()
        qcontext = self._build_portal_context(invite)
        return request.render('contractor_budget_procurement.rfq_portal_form', qcontext)

    @http.route(
        '/rfq/quote/<int:invite_id>/<string:token>/submit',
        type='http',
        auth='public',
        website=False,
        methods=['POST'],
        csrf=True,
    )
    def rfq_quote_submit(self, invite_id, token, **post):
        invite = self._get_authorized_invite(invite_id, token)
        if not invite:
            raise request.not_found()

        if invite.state not in ('draft', 'sent'):
            qcontext = self._build_portal_context(
                invite,
                error_message=_("This RFQ invitation is no longer open for submission (Status: %s).") % invite.state,
            )
            return request.render('contractor_budget_procurement.rfq_portal_form', qcontext)

        uploaded_files = [
            f for f in request.httprequest.files.getlist('quotation_file')
            if f and getattr(f, 'filename', '')
        ]
        if not uploaded_files:
            qcontext = self._build_portal_context(
                invite,
                error_message=_("Please attach at least one signed quotation document (PDF, image, Excel, or Word)."),
                form_values=post,
            )
            return request.render('contractor_budget_procurement.rfq_portal_form', qcontext)

        line_updates = []
        total_amount = 0.0
        for line in invite.line_ids:
            raw_price = (post.get(f'unit_price_{line.id}') or '0').strip()
            try:
                unit_price = float(raw_price)
            except ValueError:
                qcontext = self._build_portal_context(
                    invite,
                    error_message=_("Invalid unit price entered for item '%s'.") % (line.description or line.product_id.display_name),
                    form_values=post,
                )
                return request.render('contractor_budget_procurement.rfq_portal_form', qcontext)

            if unit_price < 0:
                qcontext = self._build_portal_context(
                    invite,
                    error_message=_("Unit price cannot be negative for item '%s'.") % (line.description or line.product_id.display_name),
                    form_values=post,
                )
                return request.render('contractor_budget_procurement.rfq_portal_form', qcontext)

            line_note = (post.get(f'note_{line.id}') or '').strip()
            line_updates.append((line, unit_price, line_note))
            total_amount += (line.qty or 0.0) * unit_price

        if total_amount <= 0.0:
            qcontext = self._build_portal_context(
                invite,
                error_message=_("Total quoted amount must be greater than zero. Please enter unit prices."),
                form_values=post,
            )
            return request.render('contractor_budget_procurement.rfq_portal_form', qcontext)

        try:
            delivery_days = max(int((post.get('delivery_days') or '7').strip()), 0)
        except ValueError:
            delivery_days = 7

        payment_terms = (post.get('payment_terms') or '').strip() or invite.payment_terms or '30 Days Net'
        vendor_reference = (post.get('vendor_reference') or '').strip()
        vendor_notes = (post.get('vendor_notes') or '').strip()

        # Persist line prices & notes
        for line, unit_price, line_note in line_updates:
            line.sudo().write({
                'quoted_price': unit_price,
                'vendor_line_note': line_note,
            })

        # Create ir.attachment records for both the Invite and the parent MR
        att_model = request.env['ir.attachment'].sudo()
        invite_att_ids = []
        mr_att_ids = []
        uploaded_names = []
        for fobj in uploaded_files:
            raw_bytes = fobj.read()
            if not raw_bytes:
                continue
            b64_data = base64.b64encode(raw_bytes)
            mimetype = getattr(fobj, 'content_type', None) or 'application/octet-stream'
            uploaded_names.append(fobj.filename)
            inv_att = att_model.create({
                'name': fobj.filename,
                'datas': b64_data,
                'res_model': 'contractor.rfq.vendor.invite',
                'res_id': invite.id,
                'mimetype': mimetype,
            })
            invite_att_ids.append(inv_att.id)
            if invite.requisition_id:
                mr_att = att_model.create({
                    'name': f"{invite.vendor_id.name} - {fobj.filename}",
                    'datas': b64_data,
                    'res_model': 'contractor.material.requisition',
                    'res_id': invite.requisition_id.id,
                    'mimetype': mimetype,
                })
                mr_att_ids.append(mr_att.id)

        if not invite_att_ids:
            qcontext = self._build_portal_context(
                invite,
                error_message=_("Uploaded quotation file appears to be empty. Please upload a non-empty file."),
                form_values=post,
            )
            return request.render('contractor_budget_procurement.rfq_portal_form', qcontext)

        now_dt = fields.Datetime.now()
        invite.sudo().write({
            'state': 'submitted',
            'submitted_datetime': now_dt,
            'delivery_days': delivery_days,
            'payment_terms': payment_terms,
            'vendor_reference': vendor_reference,
            'vendor_notes': vendor_notes,
            'quotation_attachment_ids': [(4, aid) for aid in invite_att_ids],
        })

        currency_sym = invite.currency_id.symbol or ''
        msg_body = Markup(_(
            "<strong>Vendor Quotation Submitted Online (%s):</strong><br/>"
            "Vendor: %s | Quote Ref: %s | Delivery: %s days | Payment Terms: %s<br/>"
            "Total Quoted Amount: <strong>%s %.2f</strong><br/>"
            "Files Attached: %s"
        )) % (
            invite.name,
            invite.vendor_id.name,
            vendor_reference or '-',
            delivery_days,
            payment_terms,
            currency_sym,
            invite.total_quoted_amount,
            ', '.join(uploaded_names),
        )
        author_partner_id = invite.vendor_id.id

        invite.sudo().message_post(
            body=msg_body,
            author_id=author_partner_id,
            attachment_ids=invite_att_ids,
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )
        if invite.requisition_id:
            invite.requisition_id.sudo().message_post(
                body=msg_body,
                author_id=author_partner_id,
                attachment_ids=mr_att_ids,
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )

        # Auto-sync lines into any draft Comparative Statement on this MR
        cs_model = request.env['contractor.comparative.statement'].sudo()
        draft_cs = invite.comparative_statement_id if (
            invite.comparative_statement_id and invite.comparative_statement_id.state == 'draft'
        ) else cs_model.search([
            ('requisition_id', '=', invite.requisition_id.id),
            ('state', '=', 'draft'),
        ], order='id desc', limit=1)
        if draft_cs:
            invite.sudo()._upsert_lines_to_comparative_statement(draft_cs, attach_to_chatter=True)
            if not invite.comparative_statement_id:
                invite.sudo().write({'comparative_statement_id': draft_cs.id})

        thank_context = self._build_portal_context(invite)
        thank_context['uploaded_names'] = uploaded_names
        return request.render('contractor_budget_procurement.rfq_portal_thank_you', thank_context)
