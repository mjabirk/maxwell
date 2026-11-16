# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.tools import SQL


# -------------------------------------------------------------------------
# 1. Lead Pipeline Funnel Analysis
# -------------------------------------------------------------------------
class ContractorReportLeadFunnel(models.Model):
    _name = 'contractor.report.lead.funnel'
    _description = 'Lead Pipeline Funnel Analysis'
    _auto = False
    _order = 'enquiry_date desc, id desc'

    lead_id = fields.Many2one('crm.lead', string='Lead / Enquiry', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client / Prospect', readonly=True)
    user_id = fields.Many2one('res.users', string='Estimating Engineer', readonly=True)
    assigned_manager_id = fields.Many2one('res.users', string='Assigned Manager', readonly=True)
    source_id = fields.Many2one('utm.source', string='Source Channel', readonly=True)
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
    ], string='Pipeline Stage', readonly=True)
    enquiry_date = fields.Datetime(string='Enquiry Date', readonly=True)
    has_completed_site_visit = fields.Boolean(string='Site Visit Done', readonly=True)
    expected_revenue = fields.Monetary(string='Estimated Project Value', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                l.id AS id,
                l.id AS lead_id,
                l.partner_id AS partner_id,
                l.user_id AS user_id,
                l.assigned_manager_id AS assigned_manager_id,
                l.source_id AS source_id,
                l.contractor_state AS contractor_state,
                l.enquiry_date AS enquiry_date,
                l.has_completed_site_visit AS has_completed_site_visit,
                l.expected_revenue AS expected_revenue,
                l.company_id AS company_id,
                c.currency_id AS currency_id
            FROM crm_lead l
            LEFT JOIN res_company c ON c.id = l.company_id
        """)


# -------------------------------------------------------------------------
# 2. Lead-to-Project Win Rate Report
# -------------------------------------------------------------------------
class ContractorReportWinRate(models.Model):
    _name = 'contractor.report.win.rate'
    _description = 'Lead-to-Project Win Rate Analysis'
    _auto = False
    _order = 'period_month desc, user_id asc'

    user_id = fields.Many2one('res.users', string='Estimating Engineer', readonly=True)
    period_month = fields.Char(string='Enquiry Month', readonly=True)
    value_band = fields.Char(string='Project Value Band', readonly=True)
    total_leads = fields.Integer(string='Total Inquiries', readonly=True)
    won_leads = fields.Integer(string='Awarded Projects (Won)', readonly=True)
    lost_leads = fields.Integer(string='Lost / Declined', readonly=True)
    win_rate_pct = fields.Float(string='Win Rate (%)', readonly=True)
    total_won_value = fields.Monetary(string='Total Won Value', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                row_number() OVER () AS id,
                sub.user_id,
                sub.period_month,
                sub.value_band,
                sub.total_leads,
                sub.won_leads,
                sub.lost_leads,
                CASE WHEN sub.total_leads > 0 THEN ROUND((sub.won_leads::numeric / sub.total_leads::numeric) * 100.0, 2) ELSE 0.0 END AS win_rate_pct,
                sub.total_won_value,
                sub.company_id,
                sub.currency_id
            FROM (
                SELECT
                    l.user_id AS user_id,
                    to_char(COALESCE(l.enquiry_date, l.create_date), 'YYYY-MM') AS period_month,
                    CASE
                        WHEN COALESCE(l.expected_revenue, 0) < 50000 THEN '1. Small (< 50k)'
                        WHEN COALESCE(l.expected_revenue, 0) < 250000 THEN '2. Medium (50k - 250k)'
                        ELSE '3. Large (> 250k)'
                    END AS value_band,
                    count(l.id) AS total_leads,
                    sum(CASE WHEN l.contractor_state = 'won' THEN 1 ELSE 0 END) AS won_leads,
                    sum(CASE WHEN l.contractor_state IN ('lost', 'declined') THEN 1 ELSE 0 END) AS lost_leads,
                    sum(CASE WHEN l.contractor_state = 'won' THEN COALESCE(l.expected_revenue, 0) ELSE 0 END) AS total_won_value,
                    l.company_id AS company_id,
                    c.currency_id AS currency_id
                FROM crm_lead l
                LEFT JOIN res_company c ON c.id = l.company_id
                GROUP BY l.user_id, to_char(COALESCE(l.enquiry_date, l.create_date), 'YYYY-MM'),
                         CASE
                             WHEN COALESCE(l.expected_revenue, 0) < 50000 THEN '1. Small (< 50k)'
                             WHEN COALESCE(l.expected_revenue, 0) < 250000 THEN '2. Medium (50k - 250k)'
                             ELSE '3. Large (> 250k)'
                         END, l.company_id, c.currency_id
            ) sub
        """)


# -------------------------------------------------------------------------
# 3. Loss / Decline Reason Analysis
# -------------------------------------------------------------------------
class ContractorReportLossDeclineReason(models.Model):
    _name = 'contractor.report.loss.decline.reason'
    _description = 'Loss and Decline Reason Analysis'
    _auto = False
    _order = 'lost_date desc, id desc'

    lead_id = fields.Many2one('crm.lead', string='Lead Reference', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    user_id = fields.Many2one('res.users', string='Estimating Engineer', readonly=True)
    assigned_manager_id = fields.Many2one('res.users', string='Assigned Manager', readonly=True)
    contractor_state = fields.Selection([
        ('lost', 'Lost to Competitor'),
        ('on_hold', 'Client On-Hold'),
        ('declined', 'Declined / Rejected'),
    ], string='Final Outcome', readonly=True)
    lost_reason = fields.Char(string='Documented Reason / Justification', readonly=True)
    expected_revenue = fields.Monetary(string='Lost Value', currency_field='currency_id', readonly=True)
    lost_date = fields.Datetime(string='Outcome Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                l.id AS id,
                l.id AS lead_id,
                l.partner_id AS partner_id,
                l.user_id AS user_id,
                l.assigned_manager_id AS assigned_manager_id,
                l.contractor_state AS contractor_state,
                COALESCE(l.last_action_reason, 'Unspecified reason') AS lost_reason,
                COALESCE(l.expected_revenue, 0) AS expected_revenue,
                l.write_date AS lost_date,
                l.company_id AS company_id,
                c.currency_id AS currency_id
            FROM crm_lead l
            LEFT JOIN res_company c ON c.id = l.company_id
            WHERE l.contractor_state IN ('lost', 'on_hold', 'declined')
        """)


# -------------------------------------------------------------------------
# 4. Quotation Approval Timeframe vs. 48h SLA
# -------------------------------------------------------------------------
class ContractorReportQuoteApprovalSLA(models.Model):
    _name = 'contractor.report.quote.approval.sla'
    _description = 'Quotation Approval vs 48h SLA'
    _auto = False
    _order = 'submitted_date desc, id desc'

    order_id = fields.Many2one('sale.order', string='Quotation', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    user_id = fields.Many2one('res.users', string='Estimating Engineer', readonly=True)
    original_engineer_id = fields.Many2one('res.users', string='Original Engineer', readonly=True)
    submitted_date = fields.Datetime(string='Submission Timestamp', readonly=True)
    approved_date = fields.Datetime(string='Approval Timestamp', readonly=True)
    approval_hours = fields.Float(string='Turnaround (Hours)', readonly=True)
    is_sla_breached = fields.Boolean(string='Breached 48h SLA', readonly=True)
    contractor_quote_state = fields.Selection([
        ('draft', 'Draft Estimation'),
        ('submitted', 'Submitted for Review'),
        ('approved_pending_release', 'Approved - Pending Release'),
        ('sent_to_client', 'Sent to Client'),
        ('under_negotiation', 'Under Negotiation'),
        ('accepted', 'Accepted'),
        ('converted', 'Converted'),
        ('declined', 'Declined'),
        ('lost', 'Lost'),
        ('cancelled', 'Cancelled'),
    ], string='Status', readonly=True)
    proposed_quotation_value = fields.Monetary(string='Quotation Value', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                so.id AS id,
                so.id AS order_id,
                so.partner_id AS partner_id,
                so.user_id AS user_id,
                so.original_engineer_id AS original_engineer_id,
                so.submitted_date AS submitted_date,
                so.approved_date AS approved_date,
                ROUND(EXTRACT(EPOCH FROM (COALESCE(so.approved_date, NOW()) - so.submitted_date)) / 3600.0, 2) AS approval_hours,
                CASE WHEN EXTRACT(EPOCH FROM (COALESCE(so.approved_date, NOW()) - so.submitted_date)) / 3600.0 > COALESCE(rc.quote_approval_sla_hours, 48.0) THEN TRUE ELSE FALSE END AS is_sla_breached,
                so.contractor_quote_state AS contractor_quote_state,
                COALESCE(so.proposed_quotation_value, so.amount_total) AS proposed_quotation_value,
                so.company_id AS company_id,
                so.currency_id AS currency_id
            FROM sale_order so
            LEFT JOIN res_company rc ON rc.id = so.company_id
            WHERE so.submitted_date IS NOT NULL
        """)


# -------------------------------------------------------------------------
# 5. Sent Quotations Pending Client Response Ageing
# -------------------------------------------------------------------------
class ContractorReportQuoteAgeing(models.Model):
    _name = 'contractor.report.quote.ageing'
    _description = 'Sent Quotations Client Response Ageing'
    _auto = False
    _order = 'days_outstanding desc, id desc'

    order_id = fields.Many2one('sale.order', string='Quotation Reference', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    user_id = fields.Many2one('res.users', string='Estimating Engineer', readonly=True)
    client_dispatch_datetime = fields.Datetime(string='Dispatch to Client Date', readonly=True)
    days_outstanding = fields.Integer(string='Days Pending Response', readonly=True)
    ageing_bucket = fields.Selection([
        ('bucket_15', '0 - 15 Days (Fresh)'),
        ('bucket_30', '16 - 30 Days (Follow-up Due)'),
        ('bucket_45', '31 - 45 Days (Critical Follow-up)'),
        ('bucket_over45', 'Over 45 Days (Stagnant Benchmark Exceeded)'),
    ], string='Ageing Bracket', readonly=True)
    proposed_quotation_value = fields.Monetary(string='Quotation Value', currency_field='currency_id', readonly=True)
    contractor_quote_state = fields.Selection([
        ('sent_to_client', 'Sent to Client'),
        ('under_negotiation', 'Under Negotiation'),
    ], string='Negotiation Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                so.id AS id,
                so.id AS order_id,
                so.partner_id AS partner_id,
                so.user_id AS user_id,
                so.client_dispatch_datetime AS client_dispatch_datetime,
                ROUND(EXTRACT(DAY FROM (NOW() - COALESCE(so.client_dispatch_datetime, so.create_date))))::integer AS days_outstanding,
                CASE
                    WHEN EXTRACT(DAY FROM (NOW() - COALESCE(so.client_dispatch_datetime, so.create_date))) <= COALESCE(rc.client_followup_days, 15) THEN 'bucket_15'
                    WHEN EXTRACT(DAY FROM (NOW() - COALESCE(so.client_dispatch_datetime, so.create_date))) <= (COALESCE(rc.client_followup_days, 15) * 2) THEN 'bucket_30'
                    WHEN EXTRACT(DAY FROM (NOW() - COALESCE(so.client_dispatch_datetime, so.create_date))) <= COALESCE(rc.non_conversion_benchmark_days, 45) THEN 'bucket_45'
                    ELSE 'bucket_over45'
                END AS ageing_bucket,
                COALESCE(so.proposed_quotation_value, so.amount_total) AS proposed_quotation_value,
                so.contractor_quote_state AS contractor_quote_state,
                so.company_id AS company_id,
                so.currency_id AS currency_id
            FROM sale_order so
            LEFT JOIN res_company rc ON rc.id = so.company_id
            WHERE so.contractor_quote_state IN ('sent_to_client', 'under_negotiation')
        """)


# -------------------------------------------------------------------------
# 6. Quotation-to-Invoice Conversion Time vs 45-day Benchmark
# -------------------------------------------------------------------------
class ContractorReportQuoteConversionTime(models.Model):
    _name = 'contractor.report.quote.conversion.time'
    _description = 'Quotation-to-Project Conversion Time Analysis'
    _auto = False
    _order = 'conversion_days desc, id desc'

    order_id = fields.Many2one('sale.order', string='Converted Quotation', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    user_id = fields.Many2one('res.users', string='Estimating Engineer', readonly=True)
    create_date = fields.Datetime(string='Quotation Created Date', readonly=True)
    acceptance_date = fields.Date(string='Client Acceptance Date', readonly=True)
    conversion_days = fields.Integer(string='Conversion Duration (Days)', readonly=True)
    is_benchmark_exceeded = fields.Boolean(string='Exceeded 45-Day Benchmark', readonly=True)
    proposed_quotation_value = fields.Monetary(string='Project Value', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                so.id AS id,
                so.id AS order_id,
                so.partner_id AS partner_id,
                so.user_id AS user_id,
                so.create_date AS create_date,
                so.acceptance_date AS acceptance_date,
                COALESCE((so.acceptance_date - so.create_date::date), 0)::integer AS conversion_days,
                CASE WHEN (so.acceptance_date - so.create_date::date) > COALESCE(rc.non_conversion_benchmark_days, 45) THEN TRUE ELSE FALSE END AS is_benchmark_exceeded,
                COALESCE(so.proposed_quotation_value, so.amount_total) AS proposed_quotation_value,
                so.company_id AS company_id,
                so.currency_id AS currency_id
            FROM sale_order so
            LEFT JOIN res_company rc ON rc.id = so.company_id
            WHERE so.contractor_quote_state IN ('accepted', 'converted')
              AND so.acceptance_date IS NOT NULL
        """)


# -------------------------------------------------------------------------
# 7. Non-Converted Approved Quotations
# -------------------------------------------------------------------------
class ContractorReportNonConvertedQuotes(models.Model):
    _name = 'contractor.report.non.converted.quotes'
    _description = 'Non-Converted Approved Quotations'
    _auto = False
    _order = 'days_since_approval desc, id desc'

    order_id = fields.Many2one('sale.order', string='Quotation', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    user_id = fields.Many2one('res.users', string='Estimating Engineer', readonly=True)
    approved_date = fields.Datetime(string='Manager Approval Date', readonly=True)
    days_since_approval = fields.Integer(string='Days Since Approval', readonly=True)
    contractor_quote_state = fields.Selection([
        ('approved_pending_release', 'Approved - Pending Release'),
        ('sent_to_client', 'Sent to Client'),
        ('under_negotiation', 'Under Negotiation'),
        ('accepted', 'Client Accepted - Unconverted'),
        ('declined', 'Declined'),
        ('lost', 'Lost'),
        ('cancelled', 'Cancelled'),
    ], string='Current Status', readonly=True)
    proposed_quotation_value = fields.Monetary(string='Quotation Value', currency_field='currency_id', readonly=True)
    margin_percent = fields.Float(string='Quotation Margin (%)', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                so.id AS id,
                so.id AS order_id,
                so.partner_id AS partner_id,
                so.user_id AS user_id,
                so.approved_date AS approved_date,
                ROUND(EXTRACT(DAY FROM (NOW() - so.approved_date)))::integer AS days_since_approval,
                so.contractor_quote_state AS contractor_quote_state,
                COALESCE(so.proposed_quotation_value, so.amount_total) AS proposed_quotation_value,
                so.margin_percent AS margin_percent,
                so.company_id AS company_id,
                so.currency_id AS currency_id
            FROM sale_order so
            WHERE so.approved_date IS NOT NULL
              AND so.project_id IS NULL
              AND so.contractor_quote_state IN ('approved_pending_release', 'sent_to_client', 'under_negotiation', 'accepted', 'declined', 'lost', 'cancelled')
        """)


# -------------------------------------------------------------------------
# 8. Revision History & Negotiation Log across Versions
# -------------------------------------------------------------------------
class ContractorReportQuoteRevisionLog(models.Model):
    _name = 'contractor.report.quote.revision.log'
    _description = 'Quotation Revision History & Negotiation Log'
    _auto = False
    _order = 'order_id desc, version_number desc'

    order_id = fields.Many2one('sale.order', string='Base Quotation', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    version_number = fields.Integer(string='Revision Version', readonly=True)
    total_estimated_cost = fields.Monetary(string='Direct Cost', currency_field='currency_id', readonly=True)
    margin_amount = fields.Monetary(string='Margin Amount', currency_field='currency_id', readonly=True)
    proposed_quotation_value = fields.Monetary(string='Quoted Price', currency_field='currency_id', readonly=True)
    effective_margin_pct = fields.Float(string='Margin (%)', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                so.id AS id,
                so.id AS order_id,
                so.partner_id AS partner_id,
                COALESCE(so.version_number, 1) AS version_number,
                COALESCE(so.total_estimated_cost, 0) AS total_estimated_cost,
                COALESCE(so.margin_amount, 0) AS margin_amount,
                COALESCE(so.proposed_quotation_value, so.amount_total) AS proposed_quotation_value,
                COALESCE(so.effective_margin_pct, so.margin_percent) AS effective_margin_pct,
                so.company_id AS company_id,
                so.currency_id AS currency_id
            FROM sale_order so
        """)


# -------------------------------------------------------------------------
# 9. Draft Quotation Stagnation Report
# -------------------------------------------------------------------------
class ContractorReportDraftStagnation(models.Model):
    _name = 'contractor.report.draft.stagnation'
    _description = 'Draft Quotation Stagnation Beyond SLA'
    _auto = False
    _order = 'stagnation_days desc, id desc'

    order_id = fields.Many2one('sale.order', string='Draft Quotation', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    user_id = fields.Many2one('res.users', string='Estimating Engineer', readonly=True)
    create_date = fields.Datetime(string='Created Timestamp', readonly=True)
    stagnation_days = fields.Integer(string='Days in Draft Stage', readonly=True)
    is_stagnant_alert = fields.Boolean(string='Stagnant (> 7 Days)', readonly=True)
    total_estimated_cost = fields.Monetary(string='Draft Estimated Cost', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                so.id AS id,
                so.id AS order_id,
                so.partner_id AS partner_id,
                so.user_id AS user_id,
                so.create_date AS create_date,
                ROUND(EXTRACT(DAY FROM (NOW() - so.create_date)))::integer AS stagnation_days,
                CASE WHEN EXTRACT(DAY FROM (NOW() - so.create_date)) > COALESCE(rc.draft_stagnation_days, 7) THEN TRUE ELSE FALSE END AS is_stagnant_alert,
                COALESCE(so.total_estimated_cost, 0) AS total_estimated_cost,
                so.company_id AS company_id,
                so.currency_id AS currency_id
            FROM sale_order so
            LEFT JOIN res_company rc ON rc.id = so.company_id
            WHERE so.contractor_quote_state = 'draft'
        """)


# -------------------------------------------------------------------------
# 10. Estimated Cost vs. Margin Analysis
# -------------------------------------------------------------------------
class ContractorReportMarginAnalysis(models.Model):
    _name = 'contractor.report.margin.analysis'
    _description = 'Estimated Cost vs Margin Analysis'
    _auto = False
    _order = 'proposed_quotation_value desc, id desc'

    order_id = fields.Many2one('sale.order', string='Quotation', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    user_id = fields.Many2one('res.users', string='Estimating Engineer', readonly=True)
    total_labour_cost = fields.Monetary(string='Labor Cost', currency_field='currency_id', readonly=True)
    total_material_cost = fields.Monetary(string='Material Cost', currency_field='currency_id', readonly=True)
    total_legal_cost = fields.Monetary(string='Legal / Permits', currency_field='currency_id', readonly=True)
    total_misc_cost = fields.Monetary(string='Misc / Site Indirects', currency_field='currency_id', readonly=True)
    total_estimated_cost = fields.Monetary(string='Total Direct Cost', currency_field='currency_id', readonly=True)
    margin_amount = fields.Monetary(string='Margin Amount', currency_field='currency_id', readonly=True)
    effective_margin_pct = fields.Float(string='Effective Margin (%)', readonly=True)
    proposed_quotation_value = fields.Monetary(string='Quoted Price', currency_field='currency_id', readonly=True)
    contractor_quote_state = fields.Selection([
        ('draft', 'Draft Estimation'),
        ('submitted', 'Submitted for Review'),
        ('approved_pending_release', 'Approved - Pending Release'),
        ('sent_to_client', 'Sent to Client'),
        ('under_negotiation', 'Under Negotiation'),
        ('accepted', 'Customer Accepted'),
        ('converted', 'Converted to Active Project'),
        ('declined', 'Declined'),
        ('lost', 'Lost'),
        ('cancelled', 'Cancelled'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                so.id AS id,
                so.id AS order_id,
                so.partner_id AS partner_id,
                so.user_id AS user_id,
                COALESCE(so.total_labour_cost, 0) AS total_labour_cost,
                COALESCE(so.total_material_cost, 0) AS total_material_cost,
                COALESCE(so.total_legal_cost, 0) AS total_legal_cost,
                COALESCE(so.total_misc_cost, 0) AS total_misc_cost,
                COALESCE(so.total_estimated_cost, 0) AS total_estimated_cost,
                COALESCE(so.margin_amount, 0) AS margin_amount,
                COALESCE(so.effective_margin_pct, so.margin_percent) AS effective_margin_pct,
                COALESCE(so.proposed_quotation_value, so.amount_total) AS proposed_quotation_value,
                so.contractor_quote_state AS contractor_quote_state,
                so.company_id AS company_id,
                so.currency_id AS currency_id
            FROM sale_order so
        """)


# -------------------------------------------------------------------------
# 11. Sales Admin Release Timeframe vs 24h SLA
# -------------------------------------------------------------------------
class ContractorReportSalesReleaseSLA(models.Model):
    _name = 'contractor.report.sales.release.sla'
    _description = 'Sales Admin Release vs 24h SLA'
    _auto = False
    _order = 'approved_date desc, id desc'

    order_id = fields.Many2one('sale.order', string='Approved Quotation', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    approved_date = fields.Datetime(string='Manager Approved Date', readonly=True)
    client_dispatch_datetime = fields.Datetime(string='Client Dispatch Date', readonly=True)
    release_hours = fields.Float(string='Dispatch Turnaround (Hours)', readonly=True)
    is_sla_breached = fields.Boolean(string='Breached 24h Dispatch SLA', readonly=True)
    proposed_quotation_value = fields.Monetary(string='Quotation Value', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                so.id AS id,
                so.id AS order_id,
                so.partner_id AS partner_id,
                so.approved_date AS approved_date,
                so.client_dispatch_datetime AS client_dispatch_datetime,
                ROUND(EXTRACT(EPOCH FROM (COALESCE(so.client_dispatch_datetime, NOW()) - so.approved_date)) / 3600.0, 2) AS release_hours,
                CASE WHEN EXTRACT(EPOCH FROM (COALESCE(so.client_dispatch_datetime, NOW()) - so.approved_date)) / 3600.0 > COALESCE(rc.sales_release_sla_hours, 24.0) THEN TRUE ELSE FALSE END AS is_sla_breached,
                COALESCE(so.proposed_quotation_value, so.amount_total) AS proposed_quotation_value,
                so.company_id AS company_id,
                so.currency_id AS currency_id
            FROM sale_order so
            LEFT JOIN res_company rc ON rc.id = so.company_id
            WHERE so.approved_date IS NOT NULL
        """)


# -------------------------------------------------------------------------
# 12. Quotations in Approved - Pending Client Release
# -------------------------------------------------------------------------
class ContractorReportPendingClientRelease(models.Model):
    _name = 'contractor.report.pending.client.release'
    _description = 'Approved Quotations Pending Client Dispatch'
    _auto = False
    _order = 'hours_pending desc, id desc'

    order_id = fields.Many2one('sale.order', string='Quotation', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    user_id = fields.Many2one('res.users', string='Estimating Engineer', readonly=True)
    approved_date = fields.Datetime(string='Manager Approved Timestamp', readonly=True)
    hours_pending = fields.Float(string='Hours Awaiting Dispatch', readonly=True)
    is_overdue = fields.Boolean(string='Exceeds 24h Gate', readonly=True)
    proposed_quotation_value = fields.Monetary(string='Quotation Value', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                so.id AS id,
                so.id AS order_id,
                so.partner_id AS partner_id,
                so.user_id AS user_id,
                so.approved_date AS approved_date,
                ROUND(EXTRACT(EPOCH FROM (NOW() - so.approved_date)) / 3600.0, 2) AS hours_pending,
                CASE WHEN EXTRACT(EPOCH FROM (NOW() - so.approved_date)) / 3600.0 > COALESCE(rc.sales_release_sla_hours, 24.0) THEN TRUE ELSE FALSE END AS is_overdue,
                COALESCE(so.proposed_quotation_value, so.amount_total) AS proposed_quotation_value,
                so.company_id AS company_id,
                so.currency_id AS currency_id
            FROM sale_order so
            LEFT JOIN res_company rc ON rc.id = so.company_id
            WHERE so.contractor_quote_state = 'approved_pending_release'
        """)


# -------------------------------------------------------------------------
# 13. Variation Order Register
# -------------------------------------------------------------------------
class ContractorReportVORegister(models.Model):
    _name = 'contractor.report.vo.register'
    _description = 'Scope Variation Order Register'
    _auto = False
    _order = 'create_date desc, id desc'

    vo_id = fields.Many2one('contractor.variation.order', string='VO Reference', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    order_id = fields.Many2one('sale.order', string='Base Contract', readonly=True)
    reason_type = fields.Selection([
        ('client_request', 'Client Requested Scope Addition'),
        ('site_condition', 'Unforeseen Site Condition / Structural Finding'),
        ('design_change', 'Consultant / Architectural Design Change'),
        ('statutory', 'Regulatory / Statutory Compliance Mandate'),
        ('other', 'Other Contractual Variation'),
    ], string='Variation Driver', readonly=True)
    total_estimated_cost = fields.Monetary(string='VO Direct Cost', currency_field='currency_id', readonly=True)
    margin_amount = fields.Monetary(string='VO Margin', currency_field='currency_id', readonly=True)
    total_vo_value = fields.Monetary(string='Total VO Value', currency_field='currency_id', readonly=True)
    timeline_impact_days = fields.Integer(string='Schedule Extension (Days)', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Scope'),
        ('submitted', 'Submitted for Review'),
        ('manager_approved', 'Approved by Manager'),
        ('md_approved', 'Approved by MD'),
        ('sent_to_client', 'Sent to Client'),
        ('client_accepted', 'Client Accepted'),
        ('integrated', 'Integrated into Project Budget'),
        ('declined', 'Declined'),
        ('withdrawn', 'Withdrawn'),
    ], string='Status', readonly=True)
    create_date = fields.Datetime(string='Created Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                vo.id AS id,
                vo.id AS vo_id,
                vo.project_id AS project_id,
                vo.order_id AS order_id,
                vo.reason_type AS reason_type,
                COALESCE(vo.total_estimated_cost, 0) AS total_estimated_cost,
                COALESCE(vo.margin_amount, 0) AS margin_amount,
                COALESCE(vo.total_vo_value, 0) AS total_vo_value,
                COALESCE(vo.timeline_impact_days, 0) AS timeline_impact_days,
                vo.state AS state,
                vo.create_date AS create_date,
                vo.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_variation_order vo
            LEFT JOIN res_company rc ON rc.id = vo.company_id
        """)


# -------------------------------------------------------------------------
# 14. VO Internal Approval Timeframe by Manager/MD
# -------------------------------------------------------------------------
class ContractorReportVOApprovalSLA(models.Model):
    _name = 'contractor.report.vo.approval.sla'
    _description = 'VO Approval Timeframe by Manager and MD'
    _auto = False
    _order = 'create_date desc, id desc'

    vo_id = fields.Many2one('contractor.variation.order', string='VO Reference', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    create_date = fields.Datetime(string='Submission Timestamp', readonly=True)
    approval_date = fields.Datetime(string='Approval Timestamp', readonly=True)
    approval_hours = fields.Float(string='Approval Turnaround (Hours)', readonly=True)
    requires_md_approval = fields.Boolean(string='Required MD Approval', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Scope'),
        ('submitted', 'Submitted for Review'),
        ('manager_approved', 'Approved by Manager'),
        ('md_approved', 'Approved by MD'),
        ('sent_to_client', 'Sent to Client'),
        ('client_accepted', 'Client Accepted'),
        ('integrated', 'Integrated'),
        ('declined', 'Declined'),
        ('withdrawn', 'Withdrawn'),
    ], string='Status', readonly=True)
    total_vo_value = fields.Monetary(string='VO Value', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                vo.id AS id,
                vo.id AS vo_id,
                vo.project_id AS project_id,
                vo.create_date AS create_date,
                vo.approval_date AS approval_date,
                ROUND(EXTRACT(EPOCH FROM (COALESCE(vo.approval_date, NOW()) - vo.create_date)) / 3600.0, 2) AS approval_hours,
                COALESCE(vo.requires_md_approval, FALSE) AS requires_md_approval,
                vo.state AS state,
                COALESCE(vo.total_vo_value, 0) AS total_vo_value,
                vo.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_variation_order vo
            LEFT JOIN res_company rc ON rc.id = vo.company_id
            WHERE vo.state NOT IN ('draft')
        """)


# -------------------------------------------------------------------------
# 15. Cumulative VO-to-Original Contract Value Ratio
# -------------------------------------------------------------------------
class ContractorReportVOContractRatio(models.Model):
    _name = 'contractor.report.vo.contract.ratio'
    _description = 'Cumulative VO-to-Contract Value Ratio'
    _auto = False
    _order = 'cumulative_vo_ratio desc, project_id asc'

    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    original_contract_value = fields.Monetary(string='Original Contract Value', currency_field='currency_id', readonly=True)
    total_vo_value = fields.Monetary(string='Approved Variations Value', currency_field='currency_id', readonly=True)
    cumulative_vo_ratio = fields.Float(string='VO Ratio to Contract (%)', readonly=True)
    is_critical_expansion = fields.Boolean(string='Critical Scope Expansion (> 20%)', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                p.id AS id,
                p.id AS project_id,
                p.partner_id AS partner_id,
                COALESCE(b.baseline_amount, 0) AS original_contract_value,
                COALESCE(vo_sum.total_vo, 0) AS total_vo_value,
                CASE
                    WHEN COALESCE(b.baseline_amount, 0) > 0 THEN
                        ROUND((COALESCE(vo_sum.total_vo, 0)::numeric / b.baseline_amount::numeric) * 100.0, 2)
                    ELSE 0.0
                END AS cumulative_vo_ratio,
                CASE
                    WHEN COALESCE(b.baseline_amount, 0) > 0 AND (COALESCE(vo_sum.total_vo, 0) / b.baseline_amount) > (COALESCE(c.vo_cumulative_ratio_trigger_pct, 20.0) / 100.0) THEN TRUE
                    ELSE FALSE
                END AS is_critical_expansion,
                p.company_id AS company_id,
                c.currency_id AS currency_id
            FROM project_project p
            LEFT JOIN res_company c ON c.id = p.company_id
            LEFT JOIN (
                SELECT
                    project_id,
                    sum(COALESCE(contract_amount, total_baseline_budget, 0)) AS baseline_amount
                FROM contractor_project_budget
                GROUP BY project_id
            ) b ON b.project_id = p.id
            LEFT JOIN (
                SELECT
                    project_id,
                    sum(total_vo_value) AS total_vo
                FROM contractor_variation_order
                WHERE state IN ('manager_approved', 'md_approved', 'client_accepted', 'integrated')
                GROUP BY project_id
            ) vo_sum ON vo_sum.project_id = p.id
        """)


# -------------------------------------------------------------------------
# 16. Declined / Withdrawn VO Analysis
# -------------------------------------------------------------------------
class ContractorReportVODeclined(models.Model):
    _name = 'contractor.report.vo.declined'
    _description = 'Declined and Withdrawn Variation Orders'
    _auto = False
    _order = 'write_date desc, id desc'

    vo_id = fields.Many2one('contractor.variation.order', string='VO Reference', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    reason_type = fields.Selection([
        ('client_request', 'Client Requested Scope Addition'),
        ('site_condition', 'Unforeseen Site Condition / Structural Finding'),
        ('design_change', 'Consultant / Architectural Design Change'),
        ('statutory', 'Regulatory / Statutory Compliance Mandate'),
        ('other', 'Other Contractual Variation'),
    ], string='Variation Driver', readonly=True)
    scope_description = fields.Text(string='Scope Narrative', readonly=True)
    total_vo_value = fields.Monetary(string='Proposed VO Value', currency_field='currency_id', readonly=True)
    state = fields.Selection([
        ('declined', 'Declined by Management / Client'),
        ('withdrawn', 'Withdrawn by Estimator'),
    ], string='Final Status', readonly=True)
    write_date = fields.Datetime(string='Resolution Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                vo.id AS id,
                vo.id AS vo_id,
                vo.project_id AS project_id,
                p.partner_id AS partner_id,
                vo.reason_type AS reason_type,
                vo.scope_description AS scope_description,
                COALESCE(vo.total_vo_value, 0) AS total_vo_value,
                vo.state AS state,
                vo.write_date AS write_date,
                vo.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_variation_order vo
            LEFT JOIN project_project p ON p.id = vo.project_id
            LEFT JOIN res_company rc ON rc.id = vo.company_id
            WHERE vo.state IN ('declined', 'withdrawn')
        """)

