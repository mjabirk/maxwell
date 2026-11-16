# -*- coding: utf-8 -*-
from odoo import api, models, fields

ROLE_GROUP_MAP = {
    'md_user_ids': 'contractor_core.group_contractor_md',
    'manager_user_ids': 'contractor_core.group_contractor_manager',
    'project_manager_user_ids': 'contractor_core.group_contractor_project_manager',
    'sales_admin_user_ids': 'contractor_core.group_contractor_sales_admin',
    'procurement_user_ids': 'contractor_core.group_contractor_procurement_officer',
    'senior_accountant_user_ids': 'contractor_core.group_contractor_senior_accountant',
    'accountant_user_ids': 'contractor_core.group_contractor_accountant',
    'hr_officer_user_ids': 'contractor_core.group_contractor_hr_officer',
    'storekeeper_user_ids': 'contractor_core.group_contractor_storekeeper',
    'site_engineer_user_ids': 'contractor_core.group_contractor_site_engineer',
}


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # -------------------------------------------------------------------------
    # 0. Approval Roles & Managers Assignment
    # -------------------------------------------------------------------------
    md_user_ids = fields.Many2many(
        'res.users',
        'contractor_cfg_md_users_rel',
        'config_id',
        'user_id',
        string='Managing Directors (MD)',
        domain="[('share', '=', False)]",
    )
    manager_user_ids = fields.Many2many(
        'res.users',
        'contractor_cfg_manager_users_rel',
        'config_id',
        'user_id',
        string='Contractor Managers',
        domain="[('share', '=', False)]",
    )
    project_manager_user_ids = fields.Many2many(
        'res.users',
        'contractor_cfg_pm_users_rel',
        'config_id',
        'user_id',
        string='Project Managers (PM)',
        domain="[('share', '=', False)]",
    )
    sales_admin_user_ids = fields.Many2many(
        'res.users',
        'contractor_cfg_sales_admin_users_rel',
        'config_id',
        'user_id',
        string='Sales Administrators',
        domain="[('share', '=', False)]",
    )
    procurement_user_ids = fields.Many2many(
        'res.users',
        'contractor_cfg_procurement_users_rel',
        'config_id',
        'user_id',
        string='Procurement Officers',
        domain="[('share', '=', False)]",
    )
    senior_accountant_user_ids = fields.Many2many(
        'res.users',
        'contractor_cfg_sr_acc_users_rel',
        'config_id',
        'user_id',
        string='Senior Accountants',
        domain="[('share', '=', False)]",
    )
    accountant_user_ids = fields.Many2many(
        'res.users',
        'contractor_cfg_acc_users_rel',
        'config_id',
        'user_id',
        string='Accountants',
        domain="[('share', '=', False)]",
    )
    hr_officer_user_ids = fields.Many2many(
        'res.users',
        'contractor_cfg_hr_users_rel',
        'config_id',
        'user_id',
        string='HR Officers',
        domain="[('share', '=', False)]",
    )
    storekeeper_user_ids = fields.Many2many(
        'res.users',
        'contractor_cfg_storekeeper_users_rel',
        'config_id',
        'user_id',
        string='Storekeepers',
        domain="[('share', '=', False)]",
    )
    site_engineer_user_ids = fields.Many2many(
        'res.users',
        'contractor_cfg_site_eng_users_rel',
        'config_id',
        'user_id',
        string='Site Engineers',
        domain="[('share', '=', False)]",
    )

    # -------------------------------------------------------------------------
    # 1. Quotation & Variation Orders (VO)
    # -------------------------------------------------------------------------
    min_quotation_margin_pct = fields.Float(
        related='company_id.min_quotation_margin_pct',
        readonly=False,
        string='Minimum Quotation Margin (%)',
    )
    quote_approval_sla_hours = fields.Integer(
        related='company_id.quote_approval_sla_hours',
        readonly=False,
        string='Quotation Approval SLA (Hours)',
    )
    quote_escalation_sla_hours = fields.Integer(
        related='company_id.quote_escalation_sla_hours',
        readonly=False,
        string='Quotation Escalation SLA (Hours)',
    )
    sales_release_sla_hours = fields.Integer(
        related='company_id.sales_release_sla_hours',
        readonly=False,
        string='Sales Release SLA (Hours)',
    )
    sales_release_escalation_hours = fields.Integer(
        related='company_id.sales_release_escalation_hours',
        readonly=False,
        string='Sales Release Escalation (Hours)',
    )
    client_followup_days = fields.Integer(
        related='company_id.client_followup_days',
        readonly=False,
        string='Client Follow-up Interval (Days)',
    )
    non_conversion_benchmark_days = fields.Integer(
        related='company_id.non_conversion_benchmark_days',
        readonly=False,
        string='Non-Conversion Benchmark (Days)',
    )
    draft_stagnation_days = fields.Integer(
        related='company_id.draft_stagnation_days',
        readonly=False,
        string='Draft Stagnation Alert (Days)',
    )
    vo_md_approval_threshold = fields.Monetary(
        related='company_id.vo_md_approval_threshold',
        readonly=False,
        string='Variation Order MD Approval Threshold',
    )
    vo_cumulative_ratio_trigger_pct = fields.Float(
        related='company_id.vo_cumulative_ratio_trigger_pct',
        readonly=False,
        string='VO Cumulative Ratio Trigger (%)',
    )
    vo_use_approval_sla_timers = fields.Boolean(
        related='company_id.vo_use_approval_sla_timers',
        readonly=False,
        string='Apply 48h/72h Approval SLAs to VOs',
    )
    new_sku_requires_manager_review = fields.Boolean(
        related='company_id.new_sku_requires_manager_review',
        readonly=False,
        string='New SKU Manual Cost Requires Manager Review',
    )
    lead_reopen_reassessment_days = fields.Integer(
        related='company_id.lead_reopen_reassessment_days',
        readonly=False,
        string='Lead Reopen Fresh Cost Review Threshold (Days)',
    )

    # -------------------------------------------------------------------------
    # 2. Procurement & Purchasing Controls
    # -------------------------------------------------------------------------
    mr_escalation_sla_hours = fields.Integer(
        related='company_id.mr_escalation_sla_hours',
        readonly=False,
        string='Material Requisition Escalation SLA (Hours)',
    )
    rfq_three_vendor_threshold = fields.Monetary(
        related='company_id.rfq_three_vendor_threshold',
        readonly=False,
        string='RFQ 3-Vendor Policy Threshold',
    )
    cs_pm_approval_threshold = fields.Monetary(
        related='company_id.cs_pm_approval_threshold',
        readonly=False,
        string='Comparative Statement PM Approval Limit',
    )
    po_md_approval_threshold = fields.Monetary(
        related='company_id.po_md_approval_threshold',
        readonly=False,
        string='Purchase Order MD Approval Threshold',
    )
    internal_budget_rev_md_threshold = fields.Monetary(
        related='company_id.internal_budget_rev_md_threshold',
        readonly=False,
        string='Internal Budget Revision MD Threshold',
    )
    emergency_po_cap = fields.Monetary(
        related='company_id.emergency_po_cap',
        readonly=False,
        string='Emergency PO Cap',
    )
    emergency_review_timeline_days = fields.Integer(
        related='company_id.emergency_review_timeline_days',
        readonly=False,
        string='Emergency Purchase MD Post-Review SLA (Days)',
    )
    over_delivery_tolerance_pct = fields.Float(
        related='company_id.over_delivery_tolerance_pct',
        readonly=False,
        string='Over-Delivery Tolerance (%)',
    )
    physical_count_frequency = fields.Selection(
        related='company_id.physical_count_frequency',
        readonly=False,
        string='Physical Inventory Count Frequency',
    )

    # -------------------------------------------------------------------------
    # 3. Site Operations & Daily Logs
    # -------------------------------------------------------------------------
    dpr_cutoff_hour = fields.Float(
        related='company_id.dpr_cutoff_hour',
        readonly=False,
        string='Daily Progress Report Cutoff Hour',
    )
    site_entry_lock_hours = fields.Integer(
        related='company_id.site_entry_lock_hours',
        readonly=False,
        string='Site Entry Lockout (Hours)',
    )
    material_variance_tolerance_pct = fields.Float(
        related='company_id.material_variance_tolerance_pct',
        readonly=False,
        string='Material Variance Tolerance (%)',
    )

    # -------------------------------------------------------------------------
    # 4. Human Resources & Labor Governance
    # -------------------------------------------------------------------------
    standard_monthly_hours = fields.Float(
        related='company_id.standard_monthly_hours',
        readonly=False,
        string='Standard Monthly Working Hours',
    )
    standard_daily_hours = fields.Float(
        related='company_id.standard_daily_hours',
        readonly=False,
        string='Standard Daily Working Hours',
    )
    ot_workday_multiplier = fields.Float(
        related='company_id.ot_workday_multiplier',
        readonly=False,
        string='Overtime Multiplier (Workday)',
    )
    ot_friday_multiplier = fields.Float(
        related='company_id.ot_friday_multiplier',
        readonly=False,
        string='Overtime Multiplier (Friday/Rest Day)',
    )
    ot_holiday_multiplier = fields.Float(
        related='company_id.ot_holiday_multiplier',
        readonly=False,
        string='Overtime Multiplier (Public Holiday)',
    )
    annual_leave_salary_days = fields.Float(
        related='company_id.annual_leave_salary_days',
        readonly=False,
        string='Annual Leave Salary Benchmark (Days)',
    )
    eosb_first_5yr_days = fields.Float(
        related='company_id.eosb_first_5yr_days',
        readonly=False,
        string='EOSB Days per Year (First 5 Years)',
    )
    eosb_after_5yr_days = fields.Float(
        related='company_id.eosb_after_5yr_days',
        readonly=False,
        string='EOSB Days per Year (After 5 Years)',
    )
    agency_reconciliation_frequency = fields.Selection(
        related='company_id.agency_reconciliation_frequency',
        readonly=False,
        string='Manpower Agency Reconciliation Frequency',
    )

    # -------------------------------------------------------------------------
    # 5. Financial Controls & Compliance
    # -------------------------------------------------------------------------
    nca_capitalization_threshold = fields.Monetary(
        related='company_id.nca_capitalization_threshold',
        readonly=False,
        string='Non-Current Asset Capitalization Threshold',
    )
    rpt_materiality_threshold = fields.Monetary(
        related='company_id.rpt_materiality_threshold',
        readonly=False,
        string='Related Party Transaction Materiality Threshold',
    )
    ar_escalation_overdue_days = fields.Integer(
        related='company_id.ar_escalation_overdue_days',
        readonly=False,
        string='AR Overdue Escalation (Days)',
    )
    default_retention_pct = fields.Float(
        related='company_id.default_retention_pct',
        readonly=False,
        string='Default Retention Withholding (%)',
    )
    default_dlp_days = fields.Integer(
        related='company_id.default_dlp_days',
        readonly=False,
        string='Default Defect Liability Period (Days)',
    )
    bank_unmatched_escalation_days = fields.Integer(
        related='company_id.bank_unmatched_escalation_days',
        readonly=False,
        string='Bank Unmatched Escalation SLA (Days)',
    )
    petty_cash_default_float_limit = fields.Monetary(
        related='company_id.petty_cash_default_float_limit',
        readonly=False,
        string='Default Petty Cash Float Limit',
    )
    petty_cash_count_frequency = fields.Selection(
        related='company_id.petty_cash_count_frequency',
        readonly=False,
        string='Petty Cash Surprise Count Frequency',
    )
    bad_debt_provisioning_days = fields.Integer(
        related='company_id.bad_debt_provisioning_days',
        readonly=False,
        string='Doubtful Debt Provisioning Age (Days)',
    )
    bad_debt_provisioning_pct = fields.Float(
        related='company_id.bad_debt_provisioning_pct',
        readonly=False,
        string='Default Doubtful Debt Provision (%)',
    )
    month_end_close_day = fields.Integer(
        related='company_id.month_end_close_day',
        readonly=False,
        string='Month-End Close Target Day of Following Month',
    )
    covenant_review_frequency = fields.Selection(
        related='company_id.covenant_review_frequency',
        readonly=False,
        string='Loan Covenant Review Frequency',
    )
    default_expense_account_id = fields.Many2one(
        related='company_id.default_expense_account_id',
        readonly=False,
        string='Default Project Expense Account',
        check_company=True,
    )
    default_cost_accrual_account_id = fields.Many2one(
        related='company_id.default_cost_accrual_account_id',
        readonly=False,
        string='Default Cost Accrual / Clearing Account',
        check_company=True,
    )
    default_payroll_payable_account_id = fields.Many2one(
        related='company_id.default_payroll_payable_account_id',
        readonly=False,
        string='Default Payroll & Settlement Payable Account',
        check_company=True,
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        for field_name, xmlid in ROLE_GROUP_MAP.items():
            group = self.env.ref(xmlid, raise_if_not_found=False)
            res[field_name] = [(6, 0, group.sudo().user_ids.ids)] if group else [(6, 0, [])]
        return res

    def set_values(self):
        super().set_values()
        for field_name, xmlid in ROLE_GROUP_MAP.items():
            group = self.env.ref(xmlid, raise_if_not_found=False)
            if group:
                group.sudo().user_ids = [(6, 0, self[field_name].ids)]

    @api.model
    def _get_classified_fields(self, fnames=None):
        if fnames is None:
            fnames = list(self._fields.keys())
        else:
            fnames = list(fnames)
        company_default_fields = [
            f for f in fnames
            if f.startswith('default_') and not hasattr(self._fields[f], 'default_model')
        ]
        filtered_fnames = [f for f in fnames if f not in company_default_fields]
        res = super()._get_classified_fields(fnames=filtered_fnames)
        res['other'].extend(company_default_fields)
        return res


