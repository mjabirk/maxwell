# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.tools import SQL


# -------------------------------------------------------------------------
# 17. Consolidated Stock Analysis
# -------------------------------------------------------------------------
class ContractorReportConsolidatedStock(models.Model):
    _name = 'contractor.report.consolidated.stock'
    _description = 'Consolidated Stock Analysis across Central & Project Stores'
    _auto = False
    _order = 'product_id, location_id'

    product_id = fields.Many2one('product.product', string='Material / SKU', readonly=True)
    product_tmpl_id = fields.Many2one('product.template', string='Product Template', readonly=True)
    categ_id = fields.Many2one('product.category', string='Material Category', readonly=True)
    location_id = fields.Many2one('stock.location', string='Store Location', readonly=True)
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse / Depot', readonly=True)
    project_id = fields.Many2one('project.project', string='Associated Project', readonly=True)
    quantity = fields.Float(string='On-Hand Qty', readonly=True)
    reserved_quantity = fields.Float(string='Reserved Qty', readonly=True)
    available_quantity = fields.Float(string='Available Qty', readonly=True)
    standard_price = fields.Monetary(string='Unit Standard Cost', currency_field='currency_id', readonly=True)
    inventory_value = fields.Monetary(string='Total Valuation', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                sq.id AS id,
                sq.product_id AS product_id,
                pp.product_tmpl_id AS product_tmpl_id,
                pt.categ_id AS categ_id,
                sq.location_id AS location_id,
                sw.id AS warehouse_id,
                sw.project_id AS project_id,
                sq.quantity AS quantity,
                sq.reserved_quantity AS reserved_quantity,
                (sq.quantity - sq.reserved_quantity) AS available_quantity,
                COALESCE((pp.standard_price->>COALESCE(sq.company_id, 1)::text)::numeric, 0.0) AS standard_price,
                ROUND((sq.quantity * COALESCE((pp.standard_price->>COALESCE(sq.company_id, 1)::text)::numeric, 0.0))::numeric, 2) AS inventory_value,
                sq.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM stock_quant sq
            JOIN product_product pp ON pp.id = sq.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN stock_location sl ON sl.id = sq.location_id
            LEFT JOIN stock_warehouse sw ON sw.lot_stock_id = sl.id OR sl.parent_path LIKE ('%%/' || sw.view_location_id || '/%%')
            LEFT JOIN res_company rc ON rc.id = sq.company_id
            WHERE sl.usage = 'internal'
        """)


# -------------------------------------------------------------------------
# 18. Inter-Project STN Register & Cost Neutrality
# -------------------------------------------------------------------------
class ContractorReportInterprojectSTN(models.Model):
    _name = 'contractor.report.interproject.stn'
    _description = 'Inter-Project Stock Transfer Note Register & Cost Neutrality'
    _auto = False
    _order = 'create_date desc, id desc'

    stn_id = fields.Many2one('contractor.stock.transfer.note', string='STN Reference', readonly=True)
    donor_project_id = fields.Many2one('project.project', string='Donor Project', readonly=True)
    receiving_project_id = fields.Many2one('project.project', string='Receiving Project', readonly=True)
    product_id = fields.Many2one('product.product', string='Transferred Material', readonly=True)
    transfer_qty = fields.Float(string='Transfer Qty', readonly=True)
    unit_cost = fields.Monetary(string='Unit Cost', currency_field='currency_id', readonly=True)
    total_cost = fields.Monetary(string='Total Value Reallocated', currency_field='currency_id', readonly=True)
    create_date = fields.Datetime(string='Transfer Date', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft STN'),
        ('submitted', 'Submitted to Donor PM'),
        ('donor_approved', 'Donor PM Approved'),
        ('manager_approved', 'Approved by Manager'),
        ('transferred', 'Goods In Transit'),
        ('received', 'Received & Reallocated'),
        ('declined', 'Declined'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                l.id AS id,
                s.id AS stn_id,
                s.donor_project_id AS donor_project_id,
                s.receiving_project_id AS receiving_project_id,
                l.product_id AS product_id,
                l.qty AS transfer_qty,
                l.original_purchase_cost AS unit_cost,
                ROUND((l.qty * l.original_purchase_cost)::numeric, 2) AS total_cost,
                s.create_date AS create_date,
                s.state AS state,
                s.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_stock_transfer_note_line l
            JOIN contractor_stock_transfer_note s ON s.id = l.stn_id
            LEFT JOIN res_company rc ON rc.id = s.company_id
        """)


# -------------------------------------------------------------------------
# 19. Material Requisition Approval SLA & Ageing
# -------------------------------------------------------------------------
class ContractorReportMRApprovalSLA(models.Model):
    _name = 'contractor.report.mr.approval.sla'
    _description = 'Material Requisition Approval SLA & Ageing vs 24h Benchmark'
    _auto = False
    _order = 'submitted_datetime desc, id desc'

    mr_id = fields.Many2one('contractor.material.requisition', string='Requisition (MR)', readonly=True)
    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    requested_by = fields.Many2one('res.users', string='Requested By', readonly=True)
    approver_id = fields.Many2one('res.users', string='Approver', readonly=True)
    submitted_datetime = fields.Datetime(string='Submission Datetime', readonly=True)
    approval_datetime = fields.Datetime(string='Approval Datetime', readonly=True)
    approval_hours = fields.Float(string='Turnaround Hours', readonly=True)
    is_sla_breached = fields.Boolean(string='SLA Breached (>24h)', readonly=True)
    escalated_to_md = fields.Boolean(string='Escalated to MD', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Requisition'),
        ('submitted', 'Submitted for Approval'),
        ('approved', 'Approved by Manager'),
        ('declined', 'Declined'),
        ('cs_in_progress', 'Comparative Statement In Progress'),
        ('po_created', 'PO Awarded / Ordered'),
        ('done', 'Fully Received & Closed'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                r.id AS id,
                r.id AS mr_id,
                r.project_id AS project_id,
                r.requested_by AS requested_by,
                r.approver_id AS approver_id,
                r.submitted_datetime AS submitted_datetime,
                r.approval_datetime AS approval_datetime,
                ROUND((EXTRACT(EPOCH FROM (COALESCE(r.approval_datetime, NOW()) - r.submitted_datetime)) / 3600.0)::numeric, 1) AS approval_hours,
                CASE
                    WHEN EXTRACT(EPOCH FROM (COALESCE(r.approval_datetime, NOW()) - r.submitted_datetime)) / 3600.0 > COALESCE(rc.mr_escalation_sla_hours, 24.0) THEN TRUE
                    ELSE FALSE
                END AS is_sla_breached,
                r.escalated_to_md AS escalated_to_md,
                r.state AS state,
                r.company_id AS company_id
            FROM contractor_material_requisition r
            LEFT JOIN res_company rc ON rc.id = r.company_id
            WHERE r.submitted_datetime IS NOT NULL
        """)


# -------------------------------------------------------------------------
# 20. Requisition Revision History & Audit Log
# -------------------------------------------------------------------------
class ContractorReportMRRevisionHistory(models.Model):
    _name = 'contractor.report.mr.revision.history'
    _description = 'Material Requisition Revision History & Audit Log'
    _auto = False
    _order = 'write_date desc, id desc'

    mr_id = fields.Many2one('contractor.material.requisition', string='Requisition (MR)', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    version_number = fields.Integer(string='Revision No.', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Requisition'),
        ('submitted', 'Submitted for Approval'),
        ('approved', 'Approved by Manager'),
        ('declined', 'Declined'),
        ('cs_in_progress', 'Comparative Statement In Progress'),
        ('po_created', 'PO Awarded / Ordered'),
        ('done', 'Fully Received & Closed'),
    ], string='Status', readonly=True)
    last_action_reason = fields.Text(string='Action Reason', readonly=True)
    approver_id = fields.Many2one('res.users', string='Approver / Action User', readonly=True)
    write_date = fields.Datetime(string='Last Modified', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                r.id AS id,
                r.id AS mr_id,
                r.project_id AS project_id,
                r.version_number AS version_number,
                r.state AS state,
                r.last_action_reason AS last_action_reason,
                r.approver_id AS approver_id,
                r.write_date AS write_date,
                r.company_id AS company_id
            FROM contractor_material_requisition r
        """)


# -------------------------------------------------------------------------
# 21. Budget Override Register with Approver & Mandatory Reason
# -------------------------------------------------------------------------
class ContractorReportBudgetOverride(models.Model):
    _name = 'contractor.report.budget.override'
    _description = 'Budget Override Register with Approver & Mandatory Reason'
    _auto = False
    _order = 'override_date desc, id desc'

    budget_id = fields.Many2one('contractor.project.budget', string='Project Budget', readonly=True)
    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    total_approved_budget = fields.Monetary(string='Total Approved Budget', currency_field='currency_id', readonly=True)
    committed_amount = fields.Monetary(string='Committed Amount', currency_field='currency_id', readonly=True)
    actual_cost_amount = fields.Monetary(string='Actual Cost Incurred', currency_field='currency_id', readonly=True)
    remaining_budget = fields.Monetary(string='Remaining Deficit / Buffer', currency_field='currency_id', readonly=True)
    override_reason = fields.Text(string='Override Reason', readonly=True)
    override_user_id = fields.Many2one('res.users', string='Authorized By', readonly=True)
    override_date = fields.Datetime(string='Authorization Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                b.id AS id,
                b.id AS budget_id,
                b.project_id AS project_id,
                COALESCE(bl.total_approved_budget, b.total_revised_budget, 0.0) AS total_approved_budget,
                COALESCE(bl.committed_amount, 0.0) AS committed_amount,
                COALESCE(bl.actual_cost_amount, 0.0) AS actual_cost_amount,
                COALESCE(bl.remaining_budget, b.total_revised_budget, 0.0) AS remaining_budget,
                b.override_reason AS override_reason,
                b.override_user_id AS override_user_id,
                b.override_date AS override_date,
                b.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_project_budget b
            LEFT JOIN res_company rc ON rc.id = b.company_id
            LEFT JOIN (
                SELECT
                    budget_id,
                    SUM(total_approved_budget) AS total_approved_budget,
                    SUM(committed_amount) AS committed_amount,
                    SUM(actual_cost_amount) AS actual_cost_amount,
                    SUM(remaining_budget) AS remaining_budget
                FROM contractor_project_budget_line
                GROUP BY budget_id
            ) bl ON bl.budget_id = b.id
            WHERE b.override_approved = TRUE
        """)


# -------------------------------------------------------------------------
# 22. 3-Vendor RFQ Compliance vs Exceptions
# -------------------------------------------------------------------------
class ContractorReportRFQCompliance(models.Model):
    _name = 'contractor.report.rfq.compliance'
    _description = '3-Vendor RFQ Compliance vs Exceptions Analysis'
    _auto = False
    _order = 'create_date desc, id desc'

    cs_id = fields.Many2one('contractor.comparative.statement', string='Comparative Statement (CS)', readonly=True)
    name = fields.Char(string='CS Reference', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    requisition_id = fields.Many2one('contractor.material.requisition', string='Requisition', readonly=True)
    vendor_count = fields.Integer(string='Vendor Quotes Received', readonly=True)
    is_compliant = fields.Boolean(string='3-Vendor Rule Compliant', readonly=True)
    is_sole_source = fields.Boolean(string='Sole-Source Claimed', readonly=True)
    fewer_quotes_reason = fields.Text(string='Fewer Quotes Justification', readonly=True)
    selected_quote_amount = fields.Monetary(string='Awarded Bid Amount', currency_field='currency_id', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Matrix'),
        ('submitted', 'Submitted for Review'),
        ('manager_approved', 'Approved by Manager'),
        ('md_approved', 'Approved by Managing Director'),
        ('pos_created', 'Purchase Order(s) Issued'),
        ('declined', 'Declined'),
    ], string='Status', readonly=True)
    create_date = fields.Datetime(string='Matrix Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                cs.id AS id,
                cs.id AS cs_id,
                cs.name AS name,
                cs.project_id AS project_id,
                cs.requisition_id AS requisition_id,
                cs.vendor_count AS vendor_count,
                CASE
                    WHEN cs.vendor_count >= 3 OR cs.is_sole_source = TRUE THEN TRUE
                    ELSE FALSE
                END AS is_compliant,
                cs.is_sole_source AS is_sole_source,
                cs.fewer_quotes_reason AS fewer_quotes_reason,
                cs.selected_quote_amount AS selected_quote_amount,
                cs.state AS state,
                cs.create_date AS create_date,
                cs.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_comparative_statement cs
            LEFT JOIN res_company rc ON rc.id = cs.company_id
        """)


# -------------------------------------------------------------------------
# 23. Sole-Source & Proprietary Brand Procurements
# -------------------------------------------------------------------------
class ContractorReportSoleSource(models.Model):
    _name = 'contractor.report.sole.source'
    _description = 'Sole-Source & Proprietary Brand Procurements with Justification'
    _auto = False
    _order = 'create_date desc, id desc'

    cs_id = fields.Many2one('contractor.comparative.statement', string='Comparative Statement', readonly=True)
    name = fields.Char(string='CS Reference', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    sole_source_reason = fields.Text(string='Technical & Legal Justification', readonly=True)
    selected_quote_amount = fields.Monetary(string='Procurement Value', currency_field='currency_id', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Matrix'),
        ('submitted', 'Submitted for Review'),
        ('manager_approved', 'Approved by Manager'),
        ('md_approved', 'Approved by Managing Director'),
        ('pos_created', 'Purchase Order(s) Issued'),
        ('declined', 'Declined'),
    ], string='Status', readonly=True)
    create_date = fields.Datetime(string='Date Claimed', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                cs.id AS id,
                cs.id AS cs_id,
                cs.name AS name,
                cs.project_id AS project_id,
                cs.sole_source_reason AS sole_source_reason,
                cs.selected_quote_amount AS selected_quote_amount,
                cs.state AS state,
                cs.create_date AS create_date,
                cs.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_comparative_statement cs
            LEFT JOIN res_company rc ON rc.id = cs.company_id
            WHERE cs.is_sole_source = TRUE
        """)


# -------------------------------------------------------------------------
# 24. PO Amendments & Cancellation Register with Reason Log
# -------------------------------------------------------------------------
class ContractorReportPOAmendments(models.Model):
    _name = 'contractor.report.po.amendments'
    _description = 'PO Amendments & Cancellation Register with Reason Log'
    _auto = False
    _order = 'write_date desc, id desc'

    po_id = fields.Many2one('purchase.order', string='Purchase Order', readonly=True)
    name = fields.Char(string='PO Number', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    version_number = fields.Integer(string='Version No.', readonly=True)
    state = fields.Selection([
        ('draft', 'RFQ'),
        ('sent', 'RFQ Sent'),
        ('to approve', 'To Approve'),
        ('purchase', 'Purchase Order'),
        ('done', 'Locked'),
        ('cancel', 'Cancelled'),
    ], string='Status', readonly=True)
    last_action_reason = fields.Text(string='Reason Log', readonly=True)
    amount_total = fields.Monetary(string='PO Amount', currency_field='currency_id', readonly=True)
    date_order = fields.Datetime(string='Order Date', readonly=True)
    write_date = fields.Datetime(string='Amendment Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                po.id AS id,
                po.id AS po_id,
                po.name AS name,
                po.partner_id AS partner_id,
                po.project_id AS project_id,
                po.version_number AS version_number,
                po.state AS state,
                po.last_action_reason AS last_action_reason,
                po.amount_total AS amount_total,
                po.date_order AS date_order,
                po.write_date AS write_date,
                po.company_id AS company_id,
                po.currency_id AS currency_id
            FROM purchase_order po
            WHERE po.state = 'cancel' OR po.version_number > 1
        """)


# -------------------------------------------------------------------------
# 25. Goods Rejection & Return to Vendor Analysis
# -------------------------------------------------------------------------
class ContractorReportGoodsRejection(models.Model):
    _name = 'contractor.report.goods.rejection'
    _description = 'Goods Rejection & Return to Vendor Analysis'
    _auto = False
    _order = 'date desc, id desc'

    picking_id = fields.Many2one('stock.picking', string='Return Picking', readonly=True)
    name = fields.Char(string='Transfer Ref', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    product_id = fields.Many2one('product.product', string='Rejected Material', readonly=True)
    product_qty = fields.Float(string='Rejected Qty', readonly=True)
    date = fields.Datetime(string='Return Date', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('waiting', 'Waiting Another Operation'),
        ('confirmed', 'Waiting'),
        ('assigned', 'Ready'),
        ('done', 'Done'),
        ('cancel', 'Cancelled'),
    ], string='Status', readonly=True)
    origin = fields.Char(string='Source Document', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                m.id AS id,
                sp.id AS picking_id,
                sp.name AS name,
                sp.partner_id AS partner_id,
                m.product_id AS product_id,
                m.product_uom_qty AS product_qty,
                sp.date_done AS date,
                sp.state AS state,
                sp.origin AS origin,
                sp.company_id AS company_id
            FROM stock_move m
            JOIN stock_picking sp ON sp.id = m.picking_id
            JOIN stock_location sl ON sl.id = m.location_dest_id
            WHERE sl.usage = 'supplier' AND sp.state = 'done'
        """)


# -------------------------------------------------------------------------
# 26. 3-Way Match Exception Register & Days Held
# -------------------------------------------------------------------------
class ContractorReportThreeWayExceptions(models.Model):
    _name = 'contractor.report.three.way.exceptions'
    _description = '3-Way Match Exception Register & Days Held'
    _auto = False
    _order = 'invoice_date desc, id desc'

    move_id = fields.Many2one('account.move', string='Vendor Bill', readonly=True)
    name = fields.Char(string='Bill Number', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Vendor / Supplier', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    invoice_date = fields.Date(string='Bill Date', readonly=True)
    amount_total = fields.Monetary(string='Bill Amount', currency_field='currency_id', readonly=True)
    match_exception_reason = fields.Text(string='Variance Breakdown', readonly=True)
    days_held = fields.Integer(string='Days on Hold', readonly=True)
    three_way_match_status = fields.Selection([
        ('not_applicable', 'Not Applicable'),
        ('matched', '3-Way Matched'),
        ('exception', 'Match Exception (On Hold)'),
        ('manually_cleared', 'Manually Cleared'),
    ], string='Match Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                m.id AS id,
                m.id AS move_id,
                m.name AS name,
                m.partner_id AS partner_id,
                m.project_id AS project_id,
                m.invoice_date AS invoice_date,
                m.amount_total AS amount_total,
                m.match_exception_reason AS match_exception_reason,
                GREATEST(0, (CURRENT_DATE - COALESCE(m.invoice_date, m.create_date::date))) AS days_held,
                m.three_way_match_status AS three_way_match_status,
                m.company_id AS company_id,
                m.currency_id AS currency_id
            FROM account_move m
            WHERE m.move_type = 'in_invoice' AND m.three_way_match_status = 'exception'
        """)


# -------------------------------------------------------------------------
# 27. Vendor Advance Aging & Unadjusted Prepayment Exposure
# -------------------------------------------------------------------------
class ContractorReportVendorAdvances(models.Model):
    _name = 'contractor.report.vendor.advances'
    _description = 'Vendor Advance Aging & Unadjusted Prepayment Exposure'
    _auto = False
    _order = 'date desc, id desc'

    payment_id = fields.Many2one('account.payment', string='Advance Payment Ref', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    purchase_order_id = fields.Many2one('purchase.order', string='Linked PO', readonly=True)
    date = fields.Date(string='Payment Date', readonly=True)
    amount = fields.Monetary(string='Disbursed Advance Amount', currency_field='currency_id', readonly=True)
    days_outstanding = fields.Integer(string='Days Outstanding', readonly=True)
    payment_workflow_state = fields.Selection([
        ('draft', 'Draft Payment Request'),
        ('prepared', 'Prepared by Accountant'),
        ('approved', 'Approved by Manager'),
        ('released', 'Released & Disbursed'),
        ('rejected', 'Rejected'),
    ], string='Maker-Checker Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                p.id AS id,
                p.id AS payment_id,
                p.partner_id AS partner_id,
                p.purchase_order_id AS purchase_order_id,
                p.date AS date,
                p.amount AS amount,
                GREATEST(0, (CURRENT_DATE - p.date)) AS days_outstanding,
                p.payment_workflow_state AS payment_workflow_state,
                p.company_id AS company_id,
                p.currency_id AS currency_id
            FROM account_payment p
            WHERE p.is_vendor_advance = TRUE
        """)


# -------------------------------------------------------------------------
# 28. Payment Maker-Checker SLA Timeframes
# -------------------------------------------------------------------------
class ContractorReportPaymentMakerChecker(models.Model):
    _name = 'contractor.report.payment.maker.checker'
    _description = 'Payment Maker-Checker SLA & Segregation of Duties Timeframes'
    _auto = False
    _order = 'preparation_date desc, id desc'

    payment_id = fields.Many2one('account.payment', string='Disbursement', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Payee', readonly=True)
    amount = fields.Monetary(string='Payment Amount', currency_field='currency_id', readonly=True)
    payment_workflow_state = fields.Selection([
        ('draft', 'Draft Payment Request'),
        ('prepared', 'Prepared by Accountant'),
        ('approved', 'Approved by Manager'),
        ('released', 'Released & Disbursed'),
        ('rejected', 'Rejected'),
    ], string='Status', readonly=True)
    prepared_by_id = fields.Many2one('res.users', string='Maker (Accountant)', readonly=True)
    preparation_date = fields.Datetime(string='Prep Timestamp', readonly=True)
    approved_by_id = fields.Many2one('res.users', string='Checker (Manager)', readonly=True)
    approval_date = fields.Datetime(string='Approval Timestamp', readonly=True)
    released_by_id = fields.Many2one('res.users', string='Disbursed By', readonly=True)
    release_date = fields.Datetime(string='Release Timestamp', readonly=True)
    approval_sla_hours = fields.Float(string='Prep-to-Approval (Hours)', readonly=True)
    release_sla_hours = fields.Float(string='Approval-to-Release (Hours)', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                p.id AS id,
                p.id AS payment_id,
                p.partner_id AS partner_id,
                p.amount AS amount,
                p.payment_workflow_state AS payment_workflow_state,
                p.prepared_by_id AS prepared_by_id,
                p.preparation_date AS preparation_date,
                p.approved_by_id AS approved_by_id,
                p.approval_date AS approval_date,
                p.released_by_id AS released_by_id,
                p.release_date AS release_date,
                CASE
                    WHEN p.preparation_date IS NOT NULL AND p.approval_date IS NOT NULL
                    THEN ROUND((EXTRACT(EPOCH FROM (p.approval_date - p.preparation_date)) / 3600.0)::numeric, 1)
                    ELSE 0.0
                END AS approval_sla_hours,
                CASE
                    WHEN p.approval_date IS NOT NULL AND p.release_date IS NOT NULL
                    THEN ROUND((EXTRACT(EPOCH FROM (p.release_date - p.approval_date)) / 3600.0)::numeric, 1)
                    ELSE 0.0
                END AS release_sla_hours,
                p.company_id AS company_id,
                p.currency_id AS currency_id
            FROM account_payment p
            WHERE p.payment_workflow_state IS NOT NULL
        """)


# -------------------------------------------------------------------------
# 29. Leftover Material Disposition Register
# -------------------------------------------------------------------------
class ContractorReportLeftoverDisposition(models.Model):
    _name = 'contractor.report.leftover.disposition'
    _description = 'Leftover Material Disposition Register by Project'
    _auto = False
    _order = 'create_date desc, id desc'

    disposition_id = fields.Many2one('contractor.leftover.disposition', string='Disposition Ref', readonly=True)
    name = fields.Char(string='LOD Number', readonly=True)
    project_id = fields.Many2one('project.project', string='Source Project', readonly=True)
    disposition_type = fields.Selection([
        ('return_to_store', 'Return to Central Store'),
        ('transfer_to_project', 'Transfer to Another Active Project'),
        ('write_off', 'Scrap / Inventory Write-Off'),
    ], string='Disposition Strategy', readonly=True)
    total_value = fields.Monetary(string='Surplus Valuation', currency_field='currency_id', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Disposition'),
        ('submitted', 'Submitted for Review'),
        ('approved', 'Approved by Manager'),
        ('executed', 'Executed & Transferred'),
        ('declined', 'Declined'),
    ], string='Status', readonly=True)
    create_date = fields.Datetime(string='Date Lodged', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                d.id AS id,
                d.id AS disposition_id,
                d.name AS name,
                d.project_id AS project_id,
                d.disposition_type AS disposition_type,
                d.total_value AS total_value,
                d.state AS state,
                d.create_date AS create_date,
                d.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_leftover_disposition d
            LEFT JOIN res_company rc ON rc.id = d.company_id
        """)


# -------------------------------------------------------------------------
# 30. Physical Stock Audit Variance Analysis
# -------------------------------------------------------------------------
class ContractorReportPhysicalStockVariance(models.Model):
    _name = 'contractor.report.physical.stock.variance'
    _description = 'Physical Stock Audit Variance Analysis'
    _auto = False
    _order = 'create_date desc, id desc'

    san_id = fields.Many2one('contractor.stock.adjustment.note', string='Stock Audit (SAN)', readonly=True)
    location_id = fields.Many2one('stock.location', string='Store Location', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    product_id = fields.Many2one('product.product', string='Material / SKU', readonly=True)
    system_qty = fields.Float(string='System On-Hand Qty', readonly=True)
    counted_qty = fields.Float(string='Counted Qty', readonly=True)
    variance_qty = fields.Float(string='Variance Qty', readonly=True)
    unit_cost = fields.Monetary(string='Unit Cost', currency_field='currency_id', readonly=True)
    variance_value = fields.Monetary(string='Variance Valuation', currency_field='currency_id', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Count'),
        ('submitted', 'Submitted for Verification'),
        ('manager_approved', 'Approved by Manager'),
        ('md_approved', 'Approved by Managing Director'),
        ('adjusted', 'Adjusted & Posted to GL'),
        ('declined', 'Declined'),
    ], string='Audit Status', readonly=True)
    create_date = fields.Datetime(string='Audit Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                l.id AS id,
                s.id AS san_id,
                s.location_id AS location_id,
                s.project_id AS project_id,
                l.product_id AS product_id,
                l.system_qty AS system_qty,
                l.counted_qty AS counted_qty,
                l.variance_qty AS variance_qty,
                l.unit_cost AS unit_cost,
                ROUND((l.variance_qty * l.unit_cost)::numeric, 2) AS variance_value,
                s.state AS state,
                s.create_date AS create_date,
                s.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_stock_adjustment_note_line l
            JOIN contractor_stock_adjustment_note s ON s.id = l.san_id
            LEFT JOIN res_company rc ON rc.id = s.company_id
        """)


# -------------------------------------------------------------------------
# 31. Emergency PO Frequency by Project & Month
# -------------------------------------------------------------------------
class ContractorReportEmergencyFrequency(models.Model):
    _name = 'contractor.report.emergency.frequency'
    _description = 'Emergency PO Frequency by Project & Month'
    _auto = False
    _order = 'period_month desc, emergency_po_count desc'

    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    period_month = fields.Char(string='Month (YYYY-MM)', readonly=True)
    emergency_po_count = fields.Integer(string='Emergency PO Count', readonly=True)
    total_emergency_spend = fields.Monetary(string='Total Emergency Spend', currency_field='currency_id', readonly=True)
    avg_po_value = fields.Monetary(string='Average Emergency PO Value', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                MIN(po.id) AS id,
                po.project_id AS project_id,
                TO_CHAR(po.date_order, 'YYYY-MM') AS period_month,
                COUNT(po.id) AS emergency_po_count,
                SUM(po.amount_total) AS total_emergency_spend,
                ROUND((AVG(po.amount_total))::numeric, 2) AS avg_po_value,
                po.company_id AS company_id,
                po.currency_id AS currency_id
            FROM purchase_order po
            WHERE po.is_emergency_purchase = TRUE AND po.state NOT IN ('cancel')
            GROUP BY po.project_id, TO_CHAR(po.date_order, 'YYYY-MM'), po.company_id, po.currency_id
        """)


# -------------------------------------------------------------------------
# 32. Emergency PO Price Variance vs Standard Cost
# -------------------------------------------------------------------------
class ContractorReportEmergencyPriceVariance(models.Model):
    _name = 'contractor.report.emergency.price.variance'
    _description = 'Emergency PO Price Variance vs Standard Cost'
    _auto = False
    _order = 'date_order desc, id desc'

    po_id = fields.Many2one('purchase.order', string='Emergency PO', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    product_id = fields.Many2one('product.product', string='Material / SKU', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Supplier', readonly=True)
    product_qty = fields.Float(string='Procured Qty', readonly=True)
    price_unit = fields.Monetary(string='Emergency Unit Price', currency_field='currency_id', readonly=True)
    standard_price = fields.Monetary(string='Standard Cost', currency_field='currency_id', readonly=True)
    price_variance = fields.Monetary(string='Premium per Unit', currency_field='currency_id', readonly=True)
    variance_pct = fields.Float(string='Premium (%)', readonly=True)
    date_order = fields.Datetime(string='Order Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                pol.id AS id,
                po.id AS po_id,
                po.project_id AS project_id,
                pol.product_id AS product_id,
                po.partner_id AS partner_id,
                pol.product_qty AS product_qty,
                pol.price_unit AS price_unit,
                COALESCE((pp.standard_price->>COALESCE(po.company_id, 1)::text)::numeric, 0.0) AS standard_price,
                ROUND((pol.price_unit - COALESCE((pp.standard_price->>COALESCE(po.company_id, 1)::text)::numeric, 0.0))::numeric, 2) AS price_variance,
                CASE
                    WHEN COALESCE((pp.standard_price->>COALESCE(po.company_id, 1)::text)::numeric, 0.0) > 0.0
                    THEN ROUND((((pol.price_unit - (pp.standard_price->>COALESCE(po.company_id, 1)::text)::numeric) / (pp.standard_price->>COALESCE(po.company_id, 1)::text)::numeric) * 100.0)::numeric, 2)
                    ELSE 0.0
                END AS variance_pct,
                po.date_order AS date_order,
                po.company_id AS company_id,
                po.currency_id AS currency_id
            FROM purchase_order_line pol
            JOIN purchase_order po ON po.id = pol.order_id
            JOIN product_product pp ON pp.id = pol.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            WHERE po.is_emergency_purchase = TRUE AND po.state NOT IN ('cancel')
        """)


# -------------------------------------------------------------------------
# 33. Emergency PO Vendor Concentration & Spend
# -------------------------------------------------------------------------
class ContractorReportEmergencyVendorConc(models.Model):
    _name = 'contractor.report.emergency.vendor.conc'
    _description = 'Emergency PO Vendor Concentration & Spend'
    _auto = False
    _order = 'total_emergency_spend desc, order_count desc'

    partner_id = fields.Many2one('res.partner', string='Vendor / Supplier', readonly=True)
    order_count = fields.Integer(string='Emergency Orders', readonly=True)
    total_emergency_spend = fields.Monetary(string='Total Emergency Spend', currency_field='currency_id', readonly=True)
    avg_order_value = fields.Monetary(string='Average Order Value', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                po.partner_id AS id,
                po.partner_id AS partner_id,
                COUNT(po.id) AS order_count,
                SUM(po.amount_total) AS total_emergency_spend,
                ROUND((AVG(po.amount_total))::numeric, 2) AS avg_order_value,
                po.company_id AS company_id,
                po.currency_id AS currency_id
            FROM purchase_order po
            WHERE po.is_emergency_purchase = TRUE AND po.state NOT IN ('cancel')
            GROUP BY po.partner_id, po.company_id, po.currency_id
        """)


# -------------------------------------------------------------------------
# 34. Emergency PO Reasons Analysis
# -------------------------------------------------------------------------
class ContractorReportEmergencyReasons(models.Model):
    _name = 'contractor.report.emergency.reasons'
    _description = 'Emergency PO Reasons Analysis'
    _auto = False
    _order = 'order_count desc, total_spend desc'

    emergency_reason_code = fields.Selection([
        ('site_safety', 'Immediate Site Safety Risk / Hazard'),
        ('job_stoppage', 'Critical Job Stoppage / Delay Risk'),
        ('equipment_breakdown', 'Essential Plant / Equipment Breakdown'),
        ('client_urgent', 'Client Urgent Operational Directive'),
        ('other', 'Other – Explanation Mandatory'),
    ], string='Emergency Classification', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    order_count = fields.Integer(string='Emergency PO Count', readonly=True)
    total_spend = fields.Monetary(string='Total Spend Incurred', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                MIN(po.id) AS id,
                po.emergency_reason_code AS emergency_reason_code,
                po.project_id AS project_id,
                COUNT(po.id) AS order_count,
                SUM(po.amount_total) AS total_spend,
                po.company_id AS company_id,
                po.currency_id AS currency_id
            FROM purchase_order po
            WHERE po.is_emergency_purchase = TRUE AND po.state NOT IN ('cancel')
            GROUP BY po.emergency_reason_code, po.project_id, po.company_id, po.currency_id
        """)


# -------------------------------------------------------------------------
# 35. Emergency PO MD Post-Facto Review Log
# -------------------------------------------------------------------------
class ContractorReportEmergencyMDReview(models.Model):
    _name = 'contractor.report.emergency.md.review'
    _description = 'Emergency PO MD Post-Facto Review Log & Compliance'
    _auto = False
    _order = 'date_order desc, id desc'

    po_id = fields.Many2one('purchase.order', string='Emergency PO', readonly=True)
    name = fields.Char(string='PO Reference', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    amount_total = fields.Monetary(string='Disbursed Amount', currency_field='currency_id', readonly=True)
    emergency_reason_code = fields.Selection([
        ('site_safety', 'Immediate Site Safety Risk / Hazard'),
        ('job_stoppage', 'Critical Job Stoppage / Delay Risk'),
        ('equipment_breakdown', 'Essential Plant / Equipment Breakdown'),
        ('client_urgent', 'Client Urgent Operational Directive'),
        ('other', 'Other – Explanation Mandatory'),
    ], string='Classification', readonly=True)
    md_post_event_review_state = fields.Selection([
        ('pending', 'Pending MD Review'),
        ('reviewed', 'Reviewed & Accepted by MD'),
        ('exception_flagged', 'Exception Flagged by MD'),
    ], string='MD Review Status', readonly=True)
    emergency_approver_id = fields.Many2one('res.users', string='Authorized By (Manager)', readonly=True)
    emergency_approval_date = fields.Datetime(string='Manager Approval Date', readonly=True)
    md_reviewer_id = fields.Many2one('res.users', string='MD Reviewer', readonly=True)
    md_review_date = fields.Datetime(string='MD Review Date', readonly=True)
    date_order = fields.Datetime(string='Order Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                po.id AS id,
                po.id AS po_id,
                po.name AS name,
                po.partner_id AS partner_id,
                po.project_id AS project_id,
                po.amount_total AS amount_total,
                po.emergency_reason_code AS emergency_reason_code,
                po.md_post_event_review_state AS md_post_event_review_state,
                po.emergency_approver_id AS emergency_approver_id,
                po.emergency_approval_date AS emergency_approval_date,
                po.md_reviewed_by AS md_reviewer_id,
                po.md_review_date AS md_review_date,
                po.date_order AS date_order,
                po.company_id AS company_id,
                po.currency_id AS currency_id
            FROM purchase_order po
            WHERE po.is_emergency_purchase = TRUE
        """)


# -------------------------------------------------------------------------
# 36. Free Stock vs. Project-Allocated Stock Analysis
# -------------------------------------------------------------------------
class ContractorReportFreeVsProjectStock(models.Model):
    _name = 'contractor.report.free_vs_project_stock'
    _description = 'Free Stock vs. Project-Allocated Stock Analytical Report'
    _auto = False
    _order = 'stock_type, product_id, location_id'

    product_id = fields.Many2one('product.product', string='Material / SKU', readonly=True)
    categ_id = fields.Many2one('product.category', string='Material Category', readonly=True)
    location_id = fields.Many2one('stock.location', string='Store Location', readonly=True)
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse', readonly=True)
    project_id = fields.Many2one('project.project', string='Assigned Project', readonly=True)
    stock_type = fields.Selection([
        ('free_stock', 'Free Stock'),
        ('project_allocated', 'Project-Allocated Stock'),
    ], string='Allocation Type', readonly=True)
    quantity = fields.Float(string='On-Hand Qty', readonly=True)
    reserved_quantity = fields.Float(string='Reserved Qty', readonly=True)
    available_quantity = fields.Float(string='Available Qty', readonly=True)
    inventory_value = fields.Monetary(string='Inventory Value', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                sq.id AS id,
                sq.product_id AS product_id,
                pt.categ_id AS categ_id,
                sq.location_id AS location_id,
                sw.id AS warehouse_id,
                proj.id AS project_id,
                CASE
                    WHEN proj.id IS NOT NULL THEN 'project_allocated'
                    ELSE 'free_stock'
                END AS stock_type,
                sq.quantity AS quantity,
                sq.reserved_quantity AS reserved_quantity,
                (sq.quantity - sq.reserved_quantity) AS available_quantity,
                ROUND((sq.quantity * COALESCE((pp.standard_price->>COALESCE(sq.company_id, rc.id, 1)::text)::numeric, 0.0))::numeric, 2) AS inventory_value,
                sq.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM stock_quant sq
            JOIN product_product pp ON pp.id = sq.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN stock_location sl ON sl.id = sq.location_id
            LEFT JOIN stock_warehouse sw ON sw.id = sl.warehouse_id
            LEFT JOIN project_project proj ON proj.id = COALESCE(sq.project_id, sl.project_id, sw.project_id)
            LEFT JOIN res_company rc ON rc.id = sq.company_id
            WHERE sl.usage = 'internal'
              AND (sq.quantity != 0 OR sq.reserved_quantity != 0)
        """)

