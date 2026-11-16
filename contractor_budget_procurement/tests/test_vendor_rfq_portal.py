# -*- coding: utf-8 -*-
import re
from datetime import timedelta
from odoo import fields
from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestVendorRfqPortal(HttpCase):
    """End-to-end HTTP & workflow test for the Zero-Login Tokenized Vendor RFQ Portal."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.partner = cls.env['res.partner'].create({'name': 'RFQ Test Client LLC', 'is_company': True})
        cls.vendor_a = cls.env['res.partner'].create({
            'name': 'Alpha Building Supplies WLL',
            'is_company': True,
            'email': 'quotes@alpha-supplies.example.com',
            'supplier_rank': 1,
        })
        cls.vendor_b = cls.env['res.partner'].create({
            'name': 'Beta Industrial Trading WLL',
            'is_company': True,
            'email': 'rfq@beta-trading.example.com',
            'supplier_rank': 1,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Portland Cement 50kg (RFQ Test)',
            'type': 'consu',
            'is_storable': True,
            'standard_price': 100.0,
        })
        cls.project = cls.env['project.project'].create({
            'name': 'RFQ Portal Tower Project',
            'partner_id': cls.partner.id,
            'company_id': cls.company.id,
        })
        cls.budget = cls.env['contractor.project.budget'].create({
            'name': 'RFQ Portal Test Budget',
            'project_id': cls.project.id,
            'contract_amount': 200000.0,
        })
        cls.env['contractor.project.budget.line'].create({
            'budget_id': cls.budget.id,
            'name': 'Material Cost Head',
            'block_type': 'material',
            'planned_amount': 50000.0,
        })

    def test_01_zero_login_rfq_portal_to_cs_and_po(self):
        # 1. Create and approve MR
        mr = self.env['contractor.material.requisition'].create({
            'project_id': self.project.id,
            'required_date': fields.Date.context_today(self.env.user) + timedelta(days=10),
            'purpose': 'Foundation pour cement requirement for RFQ portal test.',
            'stock_checked': True,
            'line_ids': [(0, 0, {
                'product_id': self.product.id,
                'description': 'OPC Grade 42.5 Cement Bag',
                'qty': 10.0,
                'estimated_unit_cost': 130.0,
            })],
        })
        mr.action_submit()
        mr.action_manager_approve()
        self.assertEqual(mr.state, 'approved')

        # 2. Launch Multi-Vendor RFQ Send Wizard for Vendor A & Vendor B
        wiz = self.env['contractor.rfq.send.wizard'].with_context(
            default_requisition_id=mr.id,
        ).create({
            'requisition_id': mr.id,
            'vendor_ids': [(6, 0, [self.vendor_a.id, self.vendor_b.id])],
            'deadline_date': mr.required_date,
            'send_email': True,
        })
        res_act = wiz.action_send_invitations()
        self.assertEqual(res_act['res_model'], 'contractor.rfq.vendor.invite')
        invites = mr.rfq_invite_ids
        self.assertEqual(len(invites), 2)

        # Duplicate wizard invocation must not create duplicate active invites
        wiz_dup = self.env['contractor.rfq.send.wizard'].create({
            'requisition_id': mr.id,
            'vendor_ids': [(6, 0, [self.vendor_a.id])],
            'deadline_date': mr.required_date,
            'send_email': False,
        })
        wiz_dup.action_send_invitations()
        self.assertEqual(len(mr.rfq_invite_ids), 2)

        inv_a = invites.filtered(lambda i: i.vendor_id == self.vendor_a)
        inv_b = invites.filtered(lambda i: i.vendor_id == self.vendor_b)
        self.assertEqual(inv_a.state, 'sent')
        self.assertTrue(inv_a.name.startswith('RFQ-INV-'))
        self.assertTrue(inv_a.access_token)
        self.assertIn(f"/rfq/quote/{inv_a.id}/{inv_a.access_token}", inv_a.portal_url)
        self.assertEqual(len(inv_a.line_ids), 1)

        # 3. Verify unauthenticated access via HTTP (clear session cookies -> public user)
        self.env.flush_all()
        self.opener.cookies.clear()
        bad_resp = self.url_open(f"/rfq/quote/{inv_a.id}/invalid-token-123")
        self.assertEqual(bad_resp.status_code, 404)

        get_url = f"/rfq/quote/{inv_a.id}/{inv_a.access_token}"
        get_resp = self.url_open(get_url)
        self.assertEqual(get_resp.status_code, 200)
        html = get_resp.text
        self.assertIn(inv_a.name, html)
        self.assertIn(self.vendor_a.name, html)
        self.assertIn('OPC Grade 42.5 Cement Bag', html)

        csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        self.assertTrue(csrf_match, "Portal form must embed CSRF token")
        csrf_token = csrf_match.group(1)

        # 4. Submit Vendor A quote + PDF without logging in
        inv_a_line = inv_a.line_ids[0]
        pdf_payload = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
        post_url = f"/rfq/quote/{inv_a.id}/{inv_a.access_token}/submit"
        post_resp = self.url_open(
            post_url,
            data={
                'csrf_token': csrf_token,
                f'unit_price_{inv_a_line.id}': '125.50',
                f'note_{inv_a_line.id}': 'Al Safwa Brand - Ready Stock',
                'vendor_reference': 'QTN-ALPHA-2026-01',
                'delivery_days': '4',
                'payment_terms': '15 Days Net',
                'vendor_notes': 'Valid for 30 days.',
            },
            files={
                'quotation_file': ('alpha_signed_quote.pdf', pdf_payload, 'application/pdf'),
            },
        )
        self.assertEqual(post_resp.status_code, 200)
        self.assertIn('Your Quotation Has Been Submitted', post_resp.text)

        # Reload ORM state updated by HTTP worker
        self.env.invalidate_all()
        inv_a.invalidate_recordset()
        self.assertEqual(inv_a.state, 'submitted')
        self.assertAlmostEqual(inv_a.total_quoted_amount, 1255.0, places=2)
        self.assertEqual(inv_a.delivery_days, 4)
        self.assertEqual(inv_a.payment_terms, '15 Days Net')
        self.assertEqual(inv_a.vendor_reference, 'QTN-ALPHA-2026-01')
        self.assertTrue(inv_a.submitted_datetime)
        self.assertTrue(inv_a.quotation_attachment_ids)
        self.assertEqual(inv_a.quotation_attachment_ids[0].name, 'alpha_signed_quote.pdf')

        # Mirrored attachment on Material Requisition
        mr_atts = self.env['ir.attachment'].search([
            ('res_model', '=', 'contractor.material.requisition'),
            ('res_id', '=', mr.id),
            ('name', 'ilike', 'alpha_signed_quote.pdf'),
        ])
        self.assertTrue(mr_atts, "Uploaded quote PDF must also mirror onto Material Requisition")

        # Re-visiting GET URL renders read-only summary + working tokenized PDF download
        summary_resp = self.url_open(get_url)
        self.assertEqual(summary_resp.status_code, 200)
        self.assertIn('Submitted Quotation Summary', summary_resp.text)
        dl_match = re.search(r'href="(/web/content/\d+\?access_token=[^"&]+&(?:amp;)?download=1)"', summary_resp.text)
        self.assertTrue(dl_match, "Read-only summary must provide tokenized attachment download URL")
        dl_url = dl_match.group(1).replace('&amp;', '&')
        dl_resp = self.url_open(dl_url)
        self.assertEqual(dl_resp.status_code, 200)
        self.assertEqual(dl_resp.content, pdf_payload)

        # Attempting duplicate POST while submitted must NOT overwrite prices
        repost_resp = self.url_open(
            post_url,
            data={
                'csrf_token': csrf_token,
                f'unit_price_{inv_a_line.id}': '999.00',
                'delivery_days': '1',
                'payment_terms': 'Cash',
            },
            files={
                'quotation_file': ('tamper.pdf', pdf_payload, 'application/pdf'),
            },
        )
        self.assertEqual(repost_resp.status_code, 200)
        self.assertIn('no longer open for submission', repost_resp.text)
        self.env.invalidate_all()
        self.assertAlmostEqual(inv_a.total_quoted_amount, 1255.0, places=2)

        # 5. Approve & Sync Vendor A bid into Comparative Statement
        cs_act = inv_a.action_sync_to_comparative_statement()
        self.assertEqual(cs_act['res_model'], 'contractor.comparative.statement')
        cs = self.env['contractor.comparative.statement'].browse(cs_act['res_id'])
        self.assertEqual(inv_a.state, 'approved')
        self.assertEqual(len(cs.line_ids), 1)
        self.assertAlmostEqual(cs.line_ids[0].quoted_price, 125.50, places=2)

        # Verify Vendor B submitting while draft CS exists auto-syncs into that draft CS
        self.env.flush_all()
        get_b = self.url_open(f"/rfq/quote/{inv_b.id}/{inv_b.access_token}")
        csrf_b = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_b.text).group(1)
        inv_b_line = inv_b.line_ids[0]
        post_b = self.url_open(
            f"/rfq/quote/{inv_b.id}/{inv_b.access_token}/submit",
            data={
                'csrf_token': csrf_b,
                f'unit_price_{inv_b_line.id}': '140.00',
                'delivery_days': '6',
                'payment_terms': '30 Days Net',
                'vendor_reference': 'QTN-BETA-99',
            },
            files={
                'quotation_file': ('beta_quote.pdf', b'%PDF-1.4\n%Beta', 'application/pdf'),
            },
        )
        self.assertEqual(post_b.status_code, 200)
        self.env.invalidate_all()
        self.assertEqual(inv_b.state, 'submitted')
        self.assertEqual(len(cs.line_ids), 2, "Vendor B portal submission should auto-sync to existing draft CS")

        # 6. Award Vendor A, approve CS (PM tier < 10,000), and generate PO with PDF attached
        cs_line_a = cs.line_ids.filtered(lambda l: l.vendor_id == self.vendor_a)
        cs_line_a.is_selected = True
        cs.action_submit()
        cs.action_pm_approve()
        po_act = cs.action_generate_purchase_orders()
        po = self.env['purchase.order'].search(po_act['domain'], limit=1)
        self.assertTrue(po)
        self.assertEqual(po.partner_id, self.vendor_a)
        self.assertAlmostEqual(po.order_line[0].price_unit, 125.50, places=2)
        po_atts = self.env['ir.attachment'].search([
            ('res_model', '=', 'purchase.order'),
            ('res_id', '=', po.id),
            ('name', '=', 'alpha_signed_quote.pdf'),
        ])
        self.assertTrue(po_atts, "Generated PO must carry the winning vendor's portal PDF attachment")

        # 7. Verify action_reopen_for_vendor resets state to sent
        inv_b.action_reopen_for_vendor()
        self.assertEqual(inv_b.state, 'sent')
