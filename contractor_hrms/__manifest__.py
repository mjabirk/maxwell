# -*- coding: utf-8 -*-
{
    'name': 'Contractor HRMS, Payroll & Tool Custody',
    'version': '20.0.1.0.0',
    'category': 'Contractor ERP/Human Resources',
    'summary': '3-Category Workforce Master, Overtime Cost Engine, 3-Track Payroll, Hand/Power Tool Custody, Leave Salaries & Statutory EOSB Exit Settlements',
    'description': """
Contractor ERP Suite - HRMS, Payroll & Tool Custody
===================================================
Comprehensive human resource and labor governance suite engineered for general, civil, and MEP contractors:
- 3-Category Workforce Master: Permanent in-house staff, Temporary daily-wage operatives, and Manpower supply agency workers.
- Effective-dated hourly labor rates with immutable history and manager reclassification controls.
- Manpower agency SLA agreements with billing terms, capacity quotas, and attached contracts.
- System overtime engine enforcing calendar-driven workday (1.25x), Friday (1.50x), and holiday (2.00x) multipliers.
- Provisional labor cost tracking and documented retroactive labor cost adjustments (LCA).
- 3-Track Payroll Engine:
    * Track 1: Permanent staff monthly payroll with unpaid leave deductions, site overtime, and maker-checker audit.
    * Track 2: Temporary operative wage settlements with shift-based pay and site release workflow.
    * Track 3: Manpower supply agency reconciliation with hard-stop blocking vendor bills when hours/costs mismatch ERP site attendance.
- Personal hand and power tool custody register tracking full assignment chains, returns, damage/loss assessments, and wage deductions.
- Leave salary calculator with statutory rate formulas, advance disbursements, and duplicate claim prevention.
- Air ticket allowance tracking with contractual renewal cycles and early-release justifications.
- Employee separation and exit settlements with statutory EOSB calculation, tool clearance gates, and automatic system access revocation.
    """,
    'author': 'Lareal',
    'website': 'https://www.contractorerp.internal',
    'license': 'LGPL-3',
    'depends': [
        'contractor_site_ops',
        'hr_holidays',
        'account',
    ],
    'data': [
        'security/security_groups.xml',
        'security/ir.access.csv',
        'data/ir_sequence.xml',
        'data/ir_cron.xml',
        'views/hr_employee_views.xml',
        'views/agency_agreement_views.xml',
        'views/labour_cost_engine_views.xml',
        'views/contractor_payroll_views.xml',
        'views/tool_custody_views.xml',
        'views/leave_and_tickets_views.xml',
        'views/employee_exit_views.xml',
        'views/hrms_ux_views.xml',
        'views/contractor_hrms_menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}

