# -*- coding: utf-8 -*-
{
    'name': 'Contractor Finance, Accounting & Audit Governance',
    'version': '19.0.1.0.0',
    'category': 'Contractor ERP/Accounting',
    'summary': 'Non-Current Asset Register, 3-Way Bill Matching, 3-Step Payment Maker-Checker, DLP Retentions, Bank Recs, RPT Controls, PoC WIP Revenue & 10-Point Period Close Lockout',
    'description': """
Contractor ERP Suite - Finance, Accounting & Audit Governance
=============================================================
Enterprise construction financial controls, treasury management, and statutory audit integrity:
- Non-Current Asset (NCA) Register & Physical Verification:
    * Capitalization of heavy plant, machinery, and high-value tools.
    * Automated monthly depreciation posting attributing costs to project analytic cost centers.
    * Manager and Managing Director signoff thresholds for asset disposals and physical count impairments.
- Mandatory 3-Way Match & 3-Step Payment Maker-Checker:
    * Compares PO quantities/prices against GRN actual receipts and billed line items.
    * Hard-stop blocking vendor bill posting and payment release on quantity or price exceptions.
    * Automatic detection and adjustment of PO vendor mobilization advances.
    * Enforced 3-step Maker-Checker disbursement workflow: Prepared (Accountant) -> Approved (Manager) -> Released (Disburser).
- Client & Subcontractor Retention Register with DLP & Snag Gate:
    * Dedicated retention receivable (AR) and payable (AP) balance sheet ledgers.
    * Automatic 365/400-day Defect Liability Period (DLP) expiration tracking.
    * Strict hard-stop blocking retention release without Project Manager certified snag list rectification.
- Bank Reconciliation Maker-Checker:
    * Bank journal reconciliation sheet matching ERP payments against bank statements.
    * Enforced segregation of duties between Preparer (Maker) and Reviewer (Checker).
    * Automated daily cron escalating stagnant unmatched items to management.
- Related Party Transactions (RPT) & Partner Current Accounts:
    * Statutory register of shareholders, directors, and affiliates.
    * Materiality gates requiring Board/MD signoff and arm's length justifications.
    * Dedicated individual shareholder current accounts blocking suspense account leakage.
- Site Petty Cash Floats, Loans, Provisions & COA Governance:
    * Imprest cash float management with mandatory voucher receipts and surprise count audits.
    * Loan facility amortization, monthly interest accrual cron, and MD debt approval.
    * Monthly provision engine with live sync for HRMS statutory EOSB liabilities.
    * Chart of accounts cost-head mapping lockout requiring MD authorization.
- Percentage-of-Completion (PoC) Revenue & WIP Over/Under-Billing:
    * IFRS 15 monthly schedule computing physical earned revenue versus billed billings.
    * Posts monthly Contract Asset (Under-Billing / WIP) or Contract Liability (Over-Billing) journal entries.
- 10-Point Month-End / Year-End Period Close Checklist & Lockout:
    * Enforces completion across all 10 operational and accounting gates.
    * Hard-stop blocking period lock if any gate is incomplete without documented carry-forward reasons.
    * Reopening requires Contractor Manager signoff and audit logging.
    * External auditor query and statutory adjustment tracking.
    """,
    'author': 'Contractor ERP Suite Team',
    'website': 'https://www.contractorerp.internal',
    'license': 'LGPL-3',
    'depends': [
        'contractor_hrms',
        'account',
        'purchase_stock',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/ir_rule.xml',
        'data/ir_sequence.xml',
        'data/ir_cron.xml',
        'views/fixed_asset_views.xml',
        'views/ap_and_payment_views.xml',
        'views/retention_register_views.xml',
        'views/bank_reconciliation_views.xml',
        'views/rpt_and_partner_views.xml',
        'views/petty_cash_loans_provisions_views.xml',
        'views/revenue_wip_views.xml',
        'views/period_close_views.xml',
        'views/finance_ux_views.xml',
        'views/contractor_finance_menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}

