# -*- coding: utf-8 -*-
from odoo import models, fields


class ResCompany(models.Model):
    _inherit = 'res.company'

    # -------------------------------------------------------------------------
    # 1. Quotation & Variation Orders (VO)
    # -------------------------------------------------------------------------
    min_quotation_margin_pct = fields.Float(
        string='Minimum Quotation Margin (%)',
        default=15.0,
        help='Minimum acceptable gross margin threshold for client quotations. Margins below this require management override.',
    )
    quote_approval_sla_hours = fields.Integer(
        string='Quotation Approval SLA (Hours)',
        default=48,
        help='Duration in hours before an unapproved quotation triggers a reminder to Contractor Managers.',
    )
    quote_escalation_sla_hours = fields.Integer(
        string='Quotation Escalation SLA (Hours)',
        default=72,
        help='Duration in hours before an unapproved quotation escalates to the Managing Director.',
    )
    sales_release_sla_hours = fields.Integer(
        string='Sales Release SLA (Hours)',
        default=24,
        help='SLA duration in hours for sales administration to dispatch order confirmations to execution.',
    )
    sales_release_escalation_hours = fields.Integer(
        string='Sales Release Escalation (Hours)',
        default=48,
        help='Duration in hours before an unreleased sales order escalates to Contractor Management.',
    )
    client_followup_days = fields.Integer(
        string='Client Follow-up Interval (Days)',
        default=15,
        help='Interval in days for recurring automated follow-up alerts on dispatched quotations.',
    )
    non_conversion_benchmark_days = fields.Integer(
        string='Non-Conversion Benchmark (Days)',
        default=45,
        help='Number of days after which an unconverted sent quotation is flagged for stagnation and win/loss review.',
    )
    draft_stagnation_days = fields.Integer(
        string='Draft Stagnation Alert (Days)',
        default=7,
        help='Number of days without modification after which draft quotations trigger stagnation alerts.',
    )
    vo_md_approval_threshold = fields.Monetary(
        string='Variation Order MD Approval Threshold',
        currency_field='currency_id',
        default=50000.0,
        help='Variation order value above which Managing Director approval is mandatory.',
    )
    vo_cumulative_ratio_trigger_pct = fields.Float(
        string='VO Cumulative Ratio Trigger (%)',
        default=10.0,
        help='Cumulative variation order percentage relative to the original contract value triggering mandatory executive review.',
    )
    vo_use_approval_sla_timers = fields.Boolean(
        string='Apply 48h/72h Approval SLAs to VOs',
        default=True,
        help='Enable automated 48h Manager reminder and 72h MD escalation SLA timers on submitted Variation Orders.',
    )
    new_sku_requires_manager_review = fields.Boolean(
        string='New SKU Manual Cost Requires Manager Review',
        default=True,
        help='Flag quotations containing manually priced non-catalog SKUs for explicit Contractor Manager review.',
    )
    lead_reopen_reassessment_days = fields.Integer(
        string='Lead Reopen Fresh Cost Review Threshold (Days)',
        default=30,
        help='Days after closing or holding a lead beyond which reopening mandates fresh labour/material rate review.',
    )

    # -------------------------------------------------------------------------
    # 2. Procurement & Purchasing Controls
    # -------------------------------------------------------------------------
    mr_escalation_sla_hours = fields.Integer(
        string='Material Requisition Escalation SLA (Hours)',
        default=24,
        help='Hours allowed for processing site Material Requisitions before auto-escalation to the Managing Director.',
    )
    rfq_three_vendor_threshold = fields.Monetary(
        string='RFQ 3-Vendor Policy Threshold',
        currency_field='currency_id',
        default=10000.0,
        help='Purchase requisition value threshold above which three competitive vendor quotes are strictly required.',
    )
    cs_pm_approval_threshold = fields.Monetary(
        string='Comparative Statement PM Approval Limit',
        currency_field='currency_id',
        default=10000.0,
        help='Comparative Statement value up to which a Project Manager may grant operational approval.',
    )
    po_md_approval_threshold = fields.Monetary(
        string='Purchase Order MD Approval Threshold',
        currency_field='currency_id',
        default=100000.0,
        help='Purchase order monetary value threshold requiring executive Managing Director approval.',
    )
    internal_budget_rev_md_threshold = fields.Monetary(
        string='Internal Budget Revision MD Threshold',
        currency_field='currency_id',
        default=25000.0,
        help='Monetary variance in project internal budget revisions that mandates MD approval.',
    )
    emergency_po_cap = fields.Monetary(
        string='Emergency PO Cap',
        currency_field='currency_id',
        default=15000.0,
        help='Maximum allowable monetary ceiling for expedited emergency site purchase orders without upfront full committee review.',
    )
    emergency_review_timeline_days = fields.Integer(
        string='Emergency Purchase MD Post-Review SLA (Days)',
        default=3,
        help='Number of days within which the Managing Director must post-review confirmed emergency purchase orders.',
    )
    over_delivery_tolerance_pct = fields.Float(
        string='Over-Delivery Tolerance (%)',
        default=0.0,
        help='Percentage of permissible excess delivery on purchase order line quantities before store receipt is blocked.',
    )
    physical_count_frequency = fields.Selection([
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('project_end', 'At Project Completion'),
    ], string='Physical Inventory Count Frequency', default='monthly',
        help='Required cadence for physical stock counts across central and site stores.',
    )

    # -------------------------------------------------------------------------
    # 3. Site Operations & Daily Logs
    # -------------------------------------------------------------------------
    dpr_cutoff_hour = fields.Float(
        string='Daily Progress Report Cutoff Hour',
        default=9.0,
        help='Daily time cutoff (24-hour decimal, e.g. 9.0 = 09:00 AM) for previous day Daily Progress Report submissions.',
    )
    site_entry_lock_hours = fields.Integer(
        string='Site Entry Lockout (Hours)',
        default=48,
        help='Window in hours after which site labor logs, machinery hours, and progress entries are locked from modification.',
    )
    material_variance_tolerance_pct = fields.Float(
        string='Material Variance Tolerance (%)',
        default=5.0,
        help='Maximum permissible variance between estimated bill of quantities and actual site material consumption.',
    )

    # -------------------------------------------------------------------------
    # 4. Human Resources & Labor Governance
    # -------------------------------------------------------------------------
    standard_monthly_hours = fields.Float(
        string='Standard Monthly Working Hours',
        default=208.0,
        help='Standard baseline monthly working hours utilized for hourly rate derivation and project labor cost allocation.',
    )
    standard_daily_hours = fields.Float(
        string='Standard Daily Working Hours',
        default=8.0,
        help='Standard regular daily working hours before overtime calculation takes effect.',
    )
    ot_workday_multiplier = fields.Float(
        string='Overtime Multiplier (Workday)',
        default=1.25,
        help='Rate multiplier applied to standard hourly rates for overtime hours worked on normal working days.',
    )
    ot_friday_multiplier = fields.Float(
        string='Overtime Multiplier (Friday/Rest Day)',
        default=1.50,
        help='Rate multiplier applied to standard hourly rates for work performed on weekly rest days (e.g. Fridays).',
    )
    ot_holiday_multiplier = fields.Float(
        string='Overtime Multiplier (Public Holiday)',
        default=2.0,
        help='Rate multiplier applied to standard hourly rates for work performed on declared official public holidays.',
    )
    annual_leave_salary_days = fields.Float(
        string='Annual Leave Salary Benchmark (Days)',
        default=21.0,
        help='Annual statutory leave entitlement days utilized for leave accrual and indemnity provisions.',
    )
    eosb_first_5yr_days = fields.Float(
        string='EOSB Days per Year (First 5 Years)',
        default=21.0,
        help='Statutory End-of-Service Benefit basic salary days accrued per year for service up to 5 years.',
    )
    eosb_after_5yr_days = fields.Float(
        string='EOSB Days per Year (After 5 Years)',
        default=30.0,
        help='Statutory End-of-Service Benefit basic salary days accrued per year for service beyond 5 years.',
    )
    agency_reconciliation_frequency = fields.Selection([
        ('weekly', 'Weekly'),
        ('biweekly', 'Bi-Weekly'),
        ('monthly', 'Monthly'),
    ], string='Manpower Agency Reconciliation Frequency', default='monthly',
        help='Required reconciliation cadence for manpower supply agency hours and invoices.',
    )

    # -------------------------------------------------------------------------
    # 5. Financial Controls & Compliance
    # -------------------------------------------------------------------------
    nca_capitalization_threshold = fields.Monetary(
        string='Non-Current Asset Capitalization Threshold',
        currency_field='currency_id',
        default=5000.0,
        help='Monetary expenditure threshold above which purchased equipment or site assets must be capitalized rather than expensed.',
    )
    rpt_materiality_threshold = fields.Monetary(
        string='Related Party Transaction Materiality Threshold',
        currency_field='currency_id',
        default=50000.0,
        help='Materiality threshold for transactions with related entities requiring board/audit disclosure.',
    )
    ar_escalation_overdue_days = fields.Integer(
        string='AR Overdue Escalation (Days)',
        default=30,
        help='Days past invoice due date triggering automated finance management collection escalation.',
    )
    default_retention_pct = fields.Float(
        string='Default Retention Withholding (%)',
        default=5.0,
        help='Default percentage withheld from contractor interim payment certificates for warranty / retention guarantees.',
    )
    default_dlp_days = fields.Integer(
        string='Default Defect Liability Period (Days)',
        default=365,
        help='Default contractual Defect Liability Period (DLP) duration in days before final retention release.',
    )
    bank_unmatched_escalation_days = fields.Integer(
        string='Bank Unmatched Escalation SLA (Days)',
        default=7,
        help='Days un-reconciled bank statement lines are allowed to remain unmatched before finance controller escalation.',
    )
    petty_cash_default_float_limit = fields.Monetary(
        string='Default Petty Cash Float Limit',
        currency_field='currency_id',
        default=5000.0,
        help='Standard imprest float ceiling when creating new site or office petty cash floats.',
    )
    petty_cash_count_frequency = fields.Selection([
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
    ], string='Petty Cash Surprise Count Frequency', default='monthly',
        help='Frequency for independent surprise cash counts on site petty cash floats.',
    )
    bad_debt_provisioning_days = fields.Integer(
        string='Doubtful Debt Provisioning Age (Days)',
        default=90,
        help='Days overdue beyond which AR invoices trigger doubtful debt provisioning review.',
    )
    bad_debt_provisioning_pct = fields.Float(
        string='Default Doubtful Debt Provision (%)',
        default=50.0,
        help='Benchmark provision percentage suggested for overdue doubtful receivables.',
    )
    month_end_close_day = fields.Integer(
        string='Month-End Close Target Day of Following Month',
        default=5,
        help='Target calendar day of the following month by which the 10-point month-end close should complete.',
    )
    covenant_review_frequency = fields.Selection([
        ('quarterly', 'Quarterly'),
        ('semi_annual', 'Semi-Annual'),
        ('annual', 'Annual'),
    ], string='Loan Covenant Review Frequency', default='quarterly',
        help='Required frequency for reviewing bank borrowing covenant compliance.',
    )
    default_expense_account_id = fields.Many2one(
        'account.account',
        string='Default Project Expense Account',
        check_company=True,
        help='Default expense account used for daily cost postings, payroll, and project expense entries when a specific account is not set.',
    )
    default_material_expense_account_id = fields.Many2one(
        'account.account',
        string='Material Consumption Expense Account',
        check_company=True,
        help='Expense account debited when daily site material consumption is posted to Daily Cost Posting (falls back to Default Project Expense Account).',
    )
    default_stock_valuation_account_id = fields.Many2one(
        'account.account',
        string='Inventory / Stock Issue Credit Account',
        check_company=True,
        help='Inventory/Stock Valuation or Cost Clearing account credited when materials are consumed from site/store stock (falls back to Default Cost Accrual Account).',
    )
    default_labour_expense_account_id = fields.Many2one(
        'account.account',
        string='Direct Site Labour & Overtime Expense Account',
        check_company=True,
        help='Expense account debited when daily site labour and overtime costs are posted (falls back to Default Project Expense Account).',
    )
    default_subcontractor_expense_account_id = fields.Many2one(
        'account.account',
        string='Subcontractor Site Works Expense Account',
        check_company=True,
        help='Expense account debited when certified subcontractor work logs are posted (falls back to Default Project Expense Account).',
    )
    default_cost_accrual_account_id = fields.Many2one(
        'account.account',
        string='Default Cost Accrual / Clearing Account',
        check_company=True,
        help='Default liability/accrual clearing account used for daily site cost postings and WIP schedules.',
    )
    default_payroll_payable_account_id = fields.Many2one(
        'account.account',
        string='Default Payroll & Settlement Payable Account',
        check_company=True,
        help='Default payable/liability account used for payroll runs, temporary wage settlements, leave salary, ticket allowances, and EOSB.',
    )
    allow_legacy_import_without_project = fields.Boolean(
        string='Allow Legacy Data Import Without Project Code',
        default=False,
        help='When enabled, historical accounting entries imported via Odoo CSV/Excel import or migration scripts can be created without a Project Code and are tagged as Imported Legacy Records. Normal new entries created in the UI remain strictly restricted. Disable after migration completes.',
    )
    project_restriction_start_date = fields.Date(
        string='Enforce Project Code From Date',
        help='Optional go-live cutoff date. Operational accounting lines dated strictly before this date are treated as historical data and exempt from the mandatory Project Code restriction. Entries on or after this date (all new entries) strictly require a Project Code.',
    )

    # -------------------------------------------------------------------------
    # 7. Role-Based Menu & UI Access Controls
    # -------------------------------------------------------------------------
    restrict_menus_by_role = fields.Boolean(
        string='Restrict Unwanted Menus by User Level',
        default=True,
        help='When enabled, non-administrator users have unwanted standard Odoo menus (Calendar, Contacts, Dashboards, Employees, Time Off, Apps, etc.) hidden according to their user level.',
    )
    restrict_contractor_top_menus_only = fields.Boolean(
        string='Show Only Contractor ERP & Analytics to Contractor Users',
        default=True,
        help='When enabled, Contractor ERP role users only see the top-level Contractor ERP and Contractor Analytics apps, keeping all operational options scoped inside those menus.',
    )

    def write(self, vals):
        res = super().write(vals)
        if any(k in vals for k in ('restrict_menus_by_role', 'restrict_contractor_top_menus_only')):
            self.env.transaction.invalidate_ormcache()
        return res

