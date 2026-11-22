# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.tools import SQL


# -------------------------------------------------------------------------
# 55. Project Profitability & Cost Variance Analysis
# -------------------------------------------------------------------------
class ContractorReportProjectProfitability(models.Model):
    _name = 'contractor.report.project.profitability'
    _description = 'Project Profitability & Cost Variance Analysis'
    _auto = False
    _order = 'gross_margin desc, id desc'

    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', readonly=True)
    total_approved_budget = fields.Monetary(string='Approved Budget', currency_field='currency_id', readonly=True)
    committed_amount = fields.Monetary(string='Committed Spend', currency_field='currency_id', readonly=True)
    actual_cost_amount = fields.Monetary(string='Actual Incurred Cost', currency_field='currency_id', readonly=True)
    billed_amount = fields.Monetary(string='Total Certified Billings', currency_field='currency_id', readonly=True)
    gross_margin = fields.Monetary(string='Gross Margin / (Loss)', currency_field='currency_id', readonly=True)
    margin_erosion_pct = fields.Float(string='Cost Overrun / Erosion (%)', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                p.id AS id,
                p.id AS project_id,
                p.partner_id AS partner_id,
                COALESCE(bl.total_approved_budget, b.total_revised_budget, 0.0) AS total_approved_budget,
                COALESCE(bl.committed_amount, 0.0) AS committed_amount,
                COALESCE(bl.actual_cost_amount, 0.0) AS actual_cost_amount,
                COALESCE((
                    SELECT SUM(m.amount_total)
                    FROM account_move m
                    WHERE m.project_id = p.id AND m.move_type = 'out_invoice' AND m.state = 'posted'
                ), 0.0) AS billed_amount,
                ROUND((
                    COALESCE((
                        SELECT SUM(m.amount_total)
                        FROM account_move m
                        WHERE m.project_id = p.id AND m.move_type = 'out_invoice' AND m.state = 'posted'
                    ), 0.0) - COALESCE(bl.actual_cost_amount, 0.0)
                )::numeric, 2) AS gross_margin,
                CASE
                    WHEN COALESCE(bl.total_approved_budget, b.total_revised_budget, 0.0) > 0.0
                    THEN ROUND((((COALESCE(bl.actual_cost_amount, 0.0) - COALESCE(bl.total_approved_budget, b.total_revised_budget, 0.0)) / COALESCE(bl.total_approved_budget, b.total_revised_budget, 1.0)) * 100.0)::numeric, 2)
                    ELSE 0.0
                END AS margin_erosion_pct,
                p.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM project_project p
            LEFT JOIN contractor_project_budget b ON b.project_id = p.id AND b.is_active = TRUE
            LEFT JOIN (
                SELECT
                    budget_id,
                    SUM(total_approved_budget) AS total_approved_budget,
                    SUM(committed_amount) AS committed_amount,
                    SUM(actual_cost_amount) AS actual_cost_amount
                FROM contractor_project_budget_line
                GROUP BY budget_id
            ) bl ON bl.budget_id = b.id
            LEFT JOIN res_company rc ON rc.id = p.company_id
        )""")


# -------------------------------------------------------------------------
# 56. PoC Work-in-Progress (WIP) & Over/Under-Billing Schedule
# -------------------------------------------------------------------------
class ContractorReportWIPOverUnderBilling(models.Model):
    _name = 'contractor.report.wip.over.under.billing'
    _description = 'PoC Work-in-Progress (WIP) & Over/Under-Billing Schedule'
    _auto = False
    _order = 'period_date desc, id desc'

    wip_id = fields.Many2one('contractor.project.wip.schedule', string='WIP Reference', readonly=True)
    name = fields.Char(string='Schedule Code', readonly=True)
    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    period_date = fields.Date(string='Period Cutoff', readonly=True)
    revised_contract_value = fields.Monetary(string='Revised Contract Value', currency_field='currency_id', readonly=True)
    certified_progress_pct = fields.Float(string='Certified Progress (%)', readonly=True)
    earned_revenue_to_date = fields.Monetary(string='Cumulative Earned Revenue', currency_field='currency_id', readonly=True)
    billed_to_date = fields.Monetary(string='Cumulative Billed to Client', currency_field='currency_id', readonly=True)
    variance_amount = fields.Monetary(string='Variance (Earned - Billed)', currency_field='currency_id', readonly=True)
    wip_classification = fields.Selection([
        ('under_billing', 'Under-Billing (Contract Asset / WIP)'),
        ('over_billing', 'Over-Billing (Contract Liability / Billings in Excess)'),
        ('balanced', 'Balanced (Earned = Billed)'),
    ], string='IFRS 15 Classification', readonly=True)
    adjustment_amount = fields.Monetary(string='GL Adjustment Required', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                w.id AS id,
                w.id AS wip_id,
                w.name AS name,
                w.project_id AS project_id,
                w.period_date AS period_date,
                w.revised_contract_value AS revised_contract_value,
                w.certified_progress_pct AS certified_progress_pct,
                w.earned_revenue_to_date AS earned_revenue_to_date,
                w.billed_to_date AS billed_to_date,
                w.variance_amount AS variance_amount,
                w.wip_classification AS wip_classification,
                w.adjustment_amount AS adjustment_amount,
                w.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_project_wip_schedule w
            LEFT JOIN res_company rc ON rc.id = w.company_id
        )""")


# -------------------------------------------------------------------------
# 57. Retention Money & Defect Liability Period (DLP) Expiry Ageing
# -------------------------------------------------------------------------
class ContractorReportRetentionDLP(models.Model):
    _name = 'contractor.report.retention.dlp'
    _description = 'Retention Money & Defect Liability Period (DLP) Expiry Ageing'
    _auto = False
    _order = 'dlp_expiry_date asc, id desc'

    retention_id = fields.Many2one('contractor.retention.register', string='Retention Dossier', readonly=True)
    name = fields.Char(string='Retention Reference', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Contracting Party', readonly=True)
    retention_type = fields.Selection([
        ('client_receivable', 'Client Retention Receivable (AR)'),
        ('subcontractor_payable', 'Subcontractor Retention Payable (AP)'),
    ], string='Classification', readonly=True)
    contract_amount = fields.Monetary(string='Base Contract Billing', currency_field='currency_id', readonly=True)
    retention_pct = fields.Float(string='Retention (%)', readonly=True)
    retention_amount = fields.Monetary(string='Withheld Retention', currency_field='currency_id', readonly=True)
    dlp_expiry_date = fields.Date(string='DLP Expiry Date', readonly=True)
    days_to_dlp = fields.Integer(string='Days to Expiry', readonly=True)
    is_current_classification = fields.Boolean(string='Current Maturing (<=12M)', readonly=True)
    snags_rectified_confirmed = fields.Boolean(string='Snags PM Certified', readonly=True)
    state = fields.Selection([
        ('held', 'Held Under DLP'),
        ('dlp_expired_flagged', 'DLP Expired (Awaiting Snags)'),
        ('snag_cleared', 'Snags Rectified (Ready for Approval)'),
        ('approved_for_release', 'Approved for Release'),
        ('released', 'Disbursed / Collected'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                r.id AS id,
                r.id AS retention_id,
                r.name AS name,
                r.project_id AS project_id,
                r.partner_id AS partner_id,
                r.retention_type AS retention_type,
                r.contract_amount AS contract_amount,
                r.retention_pct AS retention_pct,
                r.retention_amount AS retention_amount,
                r.dlp_expiry_date AS dlp_expiry_date,
                (r.dlp_expiry_date - CURRENT_DATE) AS days_to_dlp,
                r.is_current_classification AS is_current_classification,
                r.snags_rectified_confirmed AS snags_rectified_confirmed,
                r.state AS state,
                r.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_retention_register r
            LEFT JOIN res_company rc ON rc.id = r.company_id
        )""")


# -------------------------------------------------------------------------
# 58. Bank Reconciliation Status & Unreconciled Variance Tracker
# -------------------------------------------------------------------------
class ContractorReportBankRecStatus(models.Model):
    _name = 'contractor.report.bank.rec.status'
    _description = 'Bank Reconciliation Status & Unreconciled Variance Tracker'
    _auto = False
    _order = 'statement_date desc, id desc'

    reconciliation_id = fields.Many2one('contractor.bank.reconciliation', string='Bank Rec Sheet', readonly=True)
    name = fields.Char(string='Reconciliation Code', readonly=True)
    journal_id = fields.Many2one('account.journal', string='Bank Journal', readonly=True)
    statement_date = fields.Date(string='Cutoff Date', readonly=True)
    gl_balance = fields.Monetary(string='GL Bank Balance', currency_field='currency_id', readonly=True)
    closing_balance = fields.Monetary(string='Statement Balance', currency_field='currency_id', readonly=True)
    unmatched_payments_total = fields.Monetary(string='Uncleared Payments', currency_field='currency_id', readonly=True)
    unmatched_receipts_total = fields.Monetary(string='Undeposited Receipts', currency_field='currency_id', readonly=True)
    reconciled_difference = fields.Monetary(string='Variance Difference', currency_field='currency_id', readonly=True)
    is_balanced = fields.Boolean(string='Reconciled & Balanced', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Work Sheet'),
        ('submitted', 'Submitted for Review (Maker Done)'),
        ('approved', 'Reconciled & Certified (Checker Done)'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                b.id AS id,
                b.id AS reconciliation_id,
                b.name AS name,
                b.journal_id AS journal_id,
                b.statement_date AS statement_date,
                b.gl_balance AS gl_balance,
                b.closing_balance AS closing_balance,
                b.unmatched_payments_total AS unmatched_payments_total,
                b.unmatched_receipts_total AS unmatched_receipts_total,
                b.reconciled_difference AS reconciled_difference,
                b.is_balanced AS is_balanced,
                b.state AS state,
                b.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_bank_reconciliation b
            LEFT JOIN res_company rc ON rc.id = b.company_id
        )""")


# -------------------------------------------------------------------------
# 59. Accounts Receivable (AR) Aging & Milestone Escalation Log
# -------------------------------------------------------------------------
class ContractorReportARAgeingEscalation(models.Model):
    _name = 'contractor.report.ar.ageing.escalation'
    _description = 'Accounts Receivable (AR) Aging & Milestone Escalation Log'
    _auto = False
    _order = 'days_overdue desc, id desc'

    move_id = fields.Many2one('account.move', string='Customer Invoice', readonly=True)
    name = fields.Char(string='Invoice Number', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client / Debtor', readonly=True)
    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    invoice_date = fields.Date(string='Invoice Date', readonly=True)
    invoice_date_due = fields.Date(string='Due Date', readonly=True)
    amount_total = fields.Monetary(string='Billed Total', currency_field='currency_id', readonly=True)
    amount_residual = fields.Monetary(string='Uncollected Residual', currency_field='currency_id', readonly=True)
    days_overdue = fields.Integer(string='Days Overdue', readonly=True)
    ageing_bucket = fields.Selection([
        ('current', 'Current (Not Due)'),
        ('1_30', '1 - 30 Days Overdue'),
        ('31_60', '31 - 60 Days Overdue'),
        ('61_90', '61 - 90 Days Overdue'),
        ('90_plus', '90+ Days Overdue (Escalated)'),
    ], string='Aging Bracket', readonly=True)
    is_escalated = fields.Boolean(string='Overdue Escalation Triggered (>60d)', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                m.id AS id,
                m.id AS move_id,
                m.name AS name,
                m.partner_id AS partner_id,
                m.project_id AS project_id,
                m.invoice_date AS invoice_date,
                m.invoice_date_due AS invoice_date_due,
                m.amount_total AS amount_total,
                m.amount_residual AS amount_residual,
                GREATEST(0, (CURRENT_DATE - COALESCE(m.invoice_date_due, m.invoice_date))) AS days_overdue,
                CASE
                    WHEN CURRENT_DATE <= COALESCE(m.invoice_date_due, m.invoice_date) THEN 'current'
                    WHEN (CURRENT_DATE - COALESCE(m.invoice_date_due, m.invoice_date)) BETWEEN 1 AND 30 THEN '1_30'
                    WHEN (CURRENT_DATE - COALESCE(m.invoice_date_due, m.invoice_date)) BETWEEN 31 AND 60 THEN '31_60'
                    WHEN (CURRENT_DATE - COALESCE(m.invoice_date_due, m.invoice_date)) BETWEEN 61 AND 90 THEN '61_90'
                    ELSE '90_plus'
                END AS ageing_bucket,
                CASE
                    WHEN COALESCE(m.ar_overdue_escalated, FALSE) OR (CURRENT_DATE - COALESCE(m.invoice_date_due, m.invoice_date)) > COALESCE(rc.ar_escalation_overdue_days, 90) THEN TRUE
                    ELSE FALSE
                END AS is_escalated,
                m.company_id AS company_id,
                m.currency_id AS currency_id
            FROM account_move m
            LEFT JOIN res_company rc ON rc.id = m.company_id
            WHERE m.move_type = 'out_invoice' AND m.state = 'posted' AND m.amount_residual > 0.0
        )""")


# -------------------------------------------------------------------------
# 60. Accounts Payable (AP) Aging & Trade Payables Schedule
# -------------------------------------------------------------------------
class ContractorReportAPAgeing(models.Model):
    _name = 'contractor.report.ap.ageing'
    _description = 'Accounts Payable (AP) Aging & Trade Payables Schedule'
    _auto = False
    _order = 'days_overdue desc, id desc'

    move_id = fields.Many2one('account.move', string='Vendor Bill', readonly=True)
    name = fields.Char(string='Bill Ref', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Supplier / Subcontractor', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    invoice_date = fields.Date(string='Bill Date', readonly=True)
    invoice_date_due = fields.Date(string='Due Date', readonly=True)
    amount_total = fields.Monetary(string='Bill Amount', currency_field='currency_id', readonly=True)
    amount_residual = fields.Monetary(string='Unpaid Balance', currency_field='currency_id', readonly=True)
    days_overdue = fields.Integer(string='Days Overdue', readonly=True)
    ageing_bucket = fields.Selection([
        ('current', 'Current (Not Due)'),
        ('1_30', '1 - 30 Days Overdue'),
        ('31_60', '31 - 60 Days Overdue'),
        ('61_90', '61 - 90 Days Overdue'),
        ('90_plus', '90+ Days Overdue'),
    ], string='Aging Bracket', readonly=True)
    is_match_hold = fields.Boolean(string='3-Way Match Hold', readonly=True)
    three_way_match_status = fields.Selection([
        ('not_applicable', 'Not Applicable'),
        ('matched', '3-Way Matched'),
        ('exception', 'Match Exception'),
        ('manually_cleared', 'Manually Cleared'),
    ], string='Match Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                m.id AS id,
                m.id AS move_id,
                m.name AS name,
                m.partner_id AS partner_id,
                m.project_id AS project_id,
                m.invoice_date AS invoice_date,
                m.invoice_date_due AS invoice_date_due,
                m.amount_total AS amount_total,
                m.amount_residual AS amount_residual,
                GREATEST(0, (CURRENT_DATE - COALESCE(m.invoice_date_due, m.invoice_date))) AS days_overdue,
                CASE
                    WHEN CURRENT_DATE <= COALESCE(m.invoice_date_due, m.invoice_date) THEN 'current'
                    WHEN (CURRENT_DATE - COALESCE(m.invoice_date_due, m.invoice_date)) BETWEEN 1 AND 30 THEN '1_30'
                    WHEN (CURRENT_DATE - COALESCE(m.invoice_date_due, m.invoice_date)) BETWEEN 31 AND 60 THEN '31_60'
                    WHEN (CURRENT_DATE - COALESCE(m.invoice_date_due, m.invoice_date)) BETWEEN 61 AND 90 THEN '61_90'
                    ELSE '90_plus'
                END AS ageing_bucket,
                m.is_match_hold AS is_match_hold,
                m.three_way_match_status AS three_way_match_status,
                m.company_id AS company_id,
                m.currency_id AS currency_id
            FROM account_move m
            WHERE m.move_type = 'in_invoice' AND m.state = 'posted' AND m.amount_residual > 0.0
        )""")


# -------------------------------------------------------------------------
# 61. Related Party Transaction (RPT) Register & Materiality Review Log
# -------------------------------------------------------------------------
class ContractorReportRPTLog(models.Model):
    _name = 'contractor.report.rpt.log'
    _description = 'Related Party Transaction (RPT) Register & Materiality Review Log'
    _auto = False
    _order = 'invoice_date desc, id desc'

    move_id = fields.Many2one('account.move', string='Transaction Reference', readonly=True)
    name = fields.Char(string='Entry Code', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Related Party Counterparty', readonly=True)
    rpt_id = fields.Many2one('contractor.related.party', string='RPT Profile', readonly=True)
    rpt_nature = fields.Selection([
        ('trade_goods_services', 'Commercial Goods & Subcontracting'),
        ('advances_loans', 'Director / Intercompany Loans & Advances'),
        ('equity_drawings', 'Capital Injection / Drawings'),
        ('management_fees', 'Shared Services & Overhead Charges'),
        ('asset_transfer', 'Transfer / Lease of Fixed Assets'),
        ('other', 'Other Related Party Flow'),
    ], string='Nature of Flow', readonly=True)
    is_arms_length = fields.Boolean(string='Arm’s Length Benchmark Certified', readonly=True)
    rpt_manager_approved = fields.Boolean(string='Manager Sign-Off Granted', readonly=True)
    amount_total = fields.Monetary(string='Transaction Value', currency_field='currency_id', readonly=True)
    invoice_date = fields.Date(string='Transaction Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                m.id AS id,
                m.id AS move_id,
                m.name AS name,
                m.partner_id AS partner_id,
                m.rpt_id AS rpt_id,
                m.rpt_nature AS rpt_nature,
                m.is_arms_length AS is_arms_length,
                m.rpt_manager_approved AS rpt_manager_approved,
                m.amount_total AS amount_total,
                m.invoice_date AS invoice_date,
                m.company_id AS company_id,
                m.currency_id AS currency_id
            FROM account_move m
            WHERE m.is_rpt_transaction = TRUE
        )""")


# -------------------------------------------------------------------------
# 62. Partner & Shareholder Current Account Ledger
# -------------------------------------------------------------------------
class ContractorReportPartnerCurrentAccount(models.Model):
    _name = 'contractor.report.partner.current.account'
    _description = 'Partner & Shareholder Current Account Ledger (Injections vs Drawings)'
    _auto = False
    _order = 'net_partner_balance desc, id desc'

    account_record_id = fields.Many2one('contractor.partner.current.account', string='Current Account', readonly=True)
    name = fields.Char(string='Account Title', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Partner / Shareholder', readonly=True)
    rpt_id = fields.Many2one('contractor.related.party', string='RPT Profile', readonly=True)
    total_injections = fields.Monetary(string='Cumulative Capital Injected', currency_field='currency_id', readonly=True)
    total_drawings = fields.Monetary(string='Cumulative Drawings & Personal Expenses', currency_field='currency_id', readonly=True)
    net_partner_balance = fields.Monetary(string='Net Partner Equity / (Owed)', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                a.id AS id,
                a.id AS account_record_id,
                a.name AS name,
                a.partner_id AS partner_id,
                a.rpt_id AS rpt_id,
                a.total_injections AS total_injections,
                a.total_drawings AS total_drawings,
                a.net_partner_balance AS net_partner_balance,
                a.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_partner_current_account a
            LEFT JOIN res_company rc ON rc.id = a.company_id
        )""")


# -------------------------------------------------------------------------
# 63. Petty Cash Float Reconciliation & Surprise Count Discrepancies
# -------------------------------------------------------------------------
class ContractorReportPettyCashRec(models.Model):
    _name = 'contractor.report.petty.cash.rec'
    _description = 'Petty Cash Float Reconciliation & Surprise Count Discrepancies'
    _auto = False
    _order = 'current_balance desc, id desc'

    float_id = fields.Many2one('contractor.petty.cash.float', string='Petty Cash Float', readonly=True)
    name = fields.Char(string='Float Name / Location', readonly=True)
    project_id = fields.Many2one('project.project', string='Project Site / Office', readonly=True)
    custodian_id = fields.Many2one('res.users', string='Designated Cashier', readonly=True)
    float_limit = fields.Monetary(string='Authorized Imprest Limit', currency_field='currency_id', readonly=True)
    current_balance = fields.Monetary(string='Available Cash Balance', currency_field='currency_id', readonly=True)
    spent_amount = fields.Monetary(string='Unreplenished Spent Vouchers', currency_field='currency_id', readonly=True)
    state = fields.Selection([
        ('active', 'Active Imprest Float'),
        ('suspended', 'Suspended'),
        ('closed', 'Closed'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                f.id AS id,
                f.id AS float_id,
                f.name AS name,
                f.project_id AS project_id,
                f.custodian_id AS custodian_id,
                f.float_limit AS float_limit,
                f.current_balance AS current_balance,
                (f.float_limit - f.current_balance) AS spent_amount,
                f.state AS state,
                f.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_petty_cash_float f
            LEFT JOIN res_company rc ON rc.id = f.company_id
        )""")


# -------------------------------------------------------------------------
# 64. Fixed Asset Register & Monthly Project Depreciation Schedule
# -------------------------------------------------------------------------
class ContractorReportNCADepreciation(models.Model):
    _name = 'contractor.report.nca.depreciation'
    _description = 'Fixed Asset Register & Monthly Project Depreciation Schedule'
    _auto = False
    _order = 'acquisition_date desc, id desc'

    asset_id = fields.Many2one('contractor.fixed.asset', string='Fixed Asset', readonly=True)
    asset_code = fields.Char(string='Tag No.', readonly=True)
    name = fields.Char(string='Asset / Equipment Description', readonly=True)
    project_id = fields.Many2one('project.project', string='Deployed Project Site', readonly=True)
    custodian_employee_id = fields.Many2one('hr.employee', string='Asset Custodian', readonly=True)
    acquisition_date = fields.Date(string='Acquisition Date', readonly=True)
    purchase_value = fields.Monetary(string='Gross Capital Cost', currency_field='currency_id', readonly=True)
    cumulative_depreciation = fields.Monetary(string='Accumulated Depreciation', currency_field='currency_id', readonly=True)
    book_value = fields.Monetary(string='Net Book Value (NBV)', currency_field='currency_id', readonly=True)
    depreciation_method = fields.Selection([
        ('straight_line', 'Straight-Line Method'),
        ('declining', 'Declining Balance Method'),
    ], string='Depreciation Method', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Setup'),
        ('running', 'Active & Depreciating'),
        ('fully_depreciated', 'Fully Depreciated'),
        ('disposed', 'Disposed / Scrapped / Sold'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                a.id AS id,
                a.id AS asset_id,
                a.asset_code AS asset_code,
                a.name AS name,
                a.project_id AS project_id,
                a.custodian_employee_id AS custodian_employee_id,
                a.acquisition_date AS acquisition_date,
                a.purchase_value AS purchase_value,
                a.cumulative_depreciation AS cumulative_depreciation,
                a.book_value AS book_value,
                a.depreciation_method AS depreciation_method,
                a.state AS state,
                a.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_fixed_asset a
            LEFT JOIN res_company rc ON rc.id = a.company_id
        )""")


# -------------------------------------------------------------------------
# 65. Loan & Borrowing Facility Schedule & Covenant Compliance
# -------------------------------------------------------------------------
class ContractorReportLoanCovenant(models.Model):
    _name = 'contractor.report.loan.covenant'
    _description = 'Loan & Borrowing Facility Schedule & Covenant Compliance'
    _auto = False
    _order = 'maturity_date asc, id desc'

    facility_id = fields.Many2one('contractor.loan.facility', string='Facility Reference', readonly=True)
    name = fields.Char(string='Loan Code', readonly=True)
    lender_id = fields.Many2one('res.partner', string='Lender / Financial Institution', readonly=True)
    is_related_party_lender = fields.Boolean(string='Related Party Loan', readonly=True)
    loan_type = fields.Selection([
        ('term_loan', 'Long-Term Project Loan'),
        ('working_capital', 'Working Capital Revolver / Overdraft'),
        ('project_finance', 'Project-Specific Back-to-Back Loan'),
        ('letters_of_credit_guarantee', 'LC / Bank Guarantee Facility'),
    ], string='Facility Category', readonly=True)
    principal_amount = fields.Monetary(string='Principal Borrowed', currency_field='currency_id', readonly=True)
    outstanding_balance = fields.Monetary(string='Outstanding Principal Balance', currency_field='currency_id', readonly=True)
    interest_rate_pct = fields.Float(string='Annual Interest Rate (%)', readonly=True)
    start_date = fields.Date(string='Drawdown Date', readonly=True)
    maturity_date = fields.Date(string='Maturity Date', readonly=True)
    days_to_maturity = fields.Integer(string='Days to Maturity', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                f.id AS id,
                f.id AS facility_id,
                f.name AS name,
                f.lender_id AS lender_id,
                f.is_related_party_lender AS is_related_party_lender,
                f.loan_type AS loan_type,
                f.principal_amount AS principal_amount,
                f.outstanding_balance AS outstanding_balance,
                f.interest_rate_pct AS interest_rate_pct,
                f.start_date AS start_date,
                f.maturity_date AS maturity_date,
                (f.maturity_date - CURRENT_DATE) AS days_to_maturity,
                f.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_loan_facility f
            LEFT JOIN res_company rc ON rc.id = f.company_id
        )""")


# -------------------------------------------------------------------------
# 66. 10-Point Period-End Close Checklist Progress
# -------------------------------------------------------------------------
class ContractorReportMonthEndChecklist(models.Model):
    _name = 'contractor.report.month.end.checklist'
    _description = '10-Point Period-End Close Checklist Progress & Gate Compliance'
    _auto = False
    _order = 'date_end desc, id desc'

    close_id = fields.Many2one('contractor.period.close', string='Period Close Reference', readonly=True)
    name = fields.Char(string='Close Number', readonly=True)
    period_type = fields.Selection([
        ('month_end', 'Monthly Financial Close'),
        ('year_end', 'Fiscal Year-End Statutory Close'),
    ], string='Close Category', readonly=True)
    date_start = fields.Date(string='Start Date', readonly=True)
    date_end = fields.Date(string='Cutoff Date', readonly=True)
    completion_percentage = fields.Float(string='Checklist Completion (%)', readonly=True)
    all_gates_cleared = fields.Boolean(string='All 10 Gates Cleared', readonly=True)
    state = fields.Selection([
        ('draft', 'In Progress / Open'),
        ('in_review', 'Under Management Review'),
        ('locked', 'Period Formally Locked'),
        ('reopened', 'Reopened by Management'),
    ], string='Period Status', readonly=True)
    locked_by_id = fields.Many2one('res.users', string='Locked By', readonly=True)
    lock_date = fields.Datetime(string='Lock Timestamp', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                c.id AS id,
                c.id AS close_id,
                c.name AS name,
                c.period_type AS period_type,
                c.date_start AS date_start,
                c.date_end AS date_end,
                c.completion_percentage AS completion_percentage,
                c.all_gates_cleared AS all_gates_cleared,
                c.state AS state,
                c.locked_by_id AS locked_by_id,
                c.lock_date AS lock_date,
                c.company_id AS company_id
            FROM contractor_period_close c
        )""")


# -------------------------------------------------------------------------
# 67. External Audit Query & Financial Adjustment Tracker
# -------------------------------------------------------------------------
class ContractorReportAuditQueryLog(models.Model):
    _name = 'contractor.report.audit.query.log'
    _description = 'External Audit Query & Financial Adjustment Tracker'
    _auto = False
    _order = 'id desc'

    query_id = fields.Many2one('contractor.audit.query', string='Audit Inquiry Reference', readonly=True)
    name = fields.Char(string='Query Title', readonly=True)
    period_close_id = fields.Many2one('contractor.period.close', string='Fiscal Period Close', readonly=True)
    query_ref = fields.Char(string='PBC Inquiry Code', readonly=True)
    auditor_firm = fields.Char(string='Auditor Firm', readonly=True)
    status = fields.Selection([
        ('open', 'Open Inquiry'),
        ('responded', 'Management Responded'),
        ('closed', 'Auditor Accepted & Closed'),
    ], string='Status', readonly=True)
    has_audit_adjustment = fields.Boolean(string='Adjustment Entry Required', readonly=True)
    adjustment_amount = fields.Monetary(string='Audit Adjustment Amount', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                q.id AS id,
                q.id AS query_id,
                q.name AS name,
                q.period_close_id AS period_close_id,
                q.query_ref AS query_ref,
                q.auditor_firm AS auditor_firm,
                q.status AS status,
                q.has_audit_adjustment AS has_audit_adjustment,
                q.adjustment_amount AS adjustment_amount,
                c.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_audit_query q
            JOIN contractor_period_close c ON c.id = q.period_close_id
            LEFT JOIN res_company rc ON rc.id = c.company_id
        )""")


# -------------------------------------------------------------------------
# 70. Monthly Financial Pack & Close KPI Summary
# -------------------------------------------------------------------------
class ContractorReportMonthlyFinancialPack(models.Model):
    _name = 'contractor.report.monthly.financial.pack'
    _description = 'Monthly Financial Close Pack & Executive KPI Summary'
    _auto = False
    _order = 'date_end desc, id desc'

    period_close_id = fields.Many2one('contractor.period.close', string='Close Schedule', readonly=True)
    name = fields.Char(string='Close Reference', readonly=True)
    period_type = fields.Selection([
        ('month_end', 'Monthly Accounting Close'),
        ('year_end', 'Statutory Year-End Close'),
    ], string='Close Category', readonly=True)
    date_start = fields.Date(string='Start Date', readonly=True)
    date_end = fields.Date(string='Cutoff Date', readonly=True)
    state = fields.Selection([
        ('draft', 'In Progress / Open'),
        ('in_review', 'Under Management Review'),
        ('locked', 'Period Formally Locked'),
        ('reopened', 'Reopened by Management'),
    ], string='Period Status', readonly=True)
    completion_percentage = fields.Float(string='Gates Completion (%)', readonly=True)
    all_gates_cleared = fields.Boolean(string='All 10 Gates Cleared', readonly=True)
    invoiced_revenue = fields.Monetary(string='Posted Customer Invoices', currency_field='currency_id', readonly=True)
    vendor_bills_ap = fields.Monetary(string='Posted Vendor Bills', currency_field='currency_id', readonly=True)
    payroll_disbursed = fields.Monetary(string='Released Payroll Cost', currency_field='currency_id', readonly=True)
    provisions_posted = fields.Monetary(string='Posted Month-End Provisions', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_sql(self) -> SQL:
        return SQL("""(
            SELECT
                pc.id AS id,
                pc.id AS period_close_id,
                pc.name AS name,
                pc.period_type AS period_type,
                pc.date_start AS date_start,
                pc.date_end AS date_end,
                pc.state AS state,
                COALESCE(pc.completion_percentage, 0.0) AS completion_percentage,
                COALESCE(pc.all_gates_cleared, FALSE) AS all_gates_cleared,
                COALESCE((
                    SELECT SUM(m.amount_total_signed)
                    FROM account_move m
                    WHERE m.company_id = pc.company_id
                      AND m.move_type = 'out_invoice'
                      AND m.state = 'posted'
                      AND m.invoice_date BETWEEN pc.date_start AND pc.date_end
                ), 0.0) AS invoiced_revenue,
                COALESCE((
                    SELECT SUM(ABS(m.amount_total_signed))
                    FROM account_move m
                    WHERE m.company_id = pc.company_id
                      AND m.move_type = 'in_invoice'
                      AND m.state = 'posted'
                      AND m.invoice_date BETWEEN pc.date_start AND pc.date_end
                ), 0.0) AS vendor_bills_ap,
                COALESCE((
                    SELECT SUM(pr.total_net_payable)
                    FROM contractor_payroll_run pr
                    WHERE pr.company_id = pc.company_id
                      AND pr.state = 'released'
                      AND pr.date_end BETWEEN pc.date_start AND pc.date_end
                ), 0.0) AS payroll_disbursed,
                COALESCE((
                    SELECT SUM(pv.amount)
                    FROM contractor_accounting_provision pv
                    WHERE pv.company_id = pc.company_id
                      AND pv.state = 'posted'
                      AND pv.provision_date BETWEEN pc.date_start AND pc.date_end
                ), 0.0) AS provisions_posted,
                pc.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_period_close pc
            LEFT JOIN res_company rc ON rc.id = pc.company_id
        )""")


