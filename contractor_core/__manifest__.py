# -*- coding: utf-8 -*-
{
    'name': 'Contractor ERP Core Platform Governance',
    'version': '19.0.1.0.0',
    'category': 'Contractor ERP',
    'summary': 'Reusable platform governance mixins, security roles, central configurable settings, immutable version history, and SLA auto-escalation timers for Contractor ERP suite.',
    'description': """
Contractor ERP Core Platform Governance
=======================================
This foundational module provides the central platform architecture, governance mixins,
hierarchical security roles, configurable operational thresholds, immutable document versioning,
and SLA auto-escalation timers for the generic Contractor ERP suite.

Key Components:
---------------
1. **10 Generic Security Roles:**
   - Sales Administrator
   - Site Engineer
   - Project Manager
   - Procurement Officer
   - Storekeeper
   - HR Officer
   - Accountant
   - Senior Accountant
   - Contractor Manager
   - Managing Director (MD)

2. **Central Configurable Parameters Hub:**
   - Quotation & VO Margins, Approval/Release SLAs, Client Follow-up intervals, and MD thresholds.
   - Procurement RFQ 3-vendor rules, PO thresholds, Emergency PO caps, and Over-delivery tolerances.
   - Site DPR cutoff timers, site entry lockout hours, and material variance tolerances.
   - HR monthly/daily working hours, workday/Friday/holiday overtime multipliers, and leave benchmarks.
   - Finance capitalization thresholds, materiality limits, AR overdue SLAs, retention, and DLP periods.

3. **Reusable Abstract Mixins:**
   - contractor.project.mixin: Mandatory Project Code alignment and analytic account distribution sync.
   - contractor.no.delete.mixin: Strictly prevents unlinking records, mandating cancel/decline reasons.
   - contractor.version.mixin: Immutable JSON/HTML document version snapshots without overwriting.
   - contractor.reason.mixin: Mandatory reason wizard for declining, returning, cancelling, or overriding.
   - contractor.effective.date.mixin: Historical effective-dated labor rate history preventing retroactive recalculation.

4. **SLA Reminder & Auto-Escalation Engine:**
   - Hourly cron job evaluating quotation approval, sales release, material requisition, and follow-up SLAs.
    """,
    'author': 'Contractor ERP Suite',
    'website': 'https://github.com/contractor-erp',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'project',
        'analytic',
        'account',
        'stock',
        'hr',
        'purchase',
        'sale_management',
        'hr_attendance'
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'security/ir_rule.xml',
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
        'views/document_version_views.xml',
        'views/dated_rate_history_views.xml',
        'views/reason_wizard_views.xml',
        'views/contractor_menus.xml',
        'views/executive_approval_inbox_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'contractor_core/static/src/views/fields/*',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}

