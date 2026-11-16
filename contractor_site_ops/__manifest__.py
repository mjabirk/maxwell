# -*- coding: utf-8 -*-
{
    'name': 'Contractor Site Operations & Daily Cost Governance',
    'version': '19.0.1.0.0',
    'category': 'Contractor ERP',
    'summary': '48-Hour Entry Lock, Staff Scheduling & Double-Booking Block, DPR Continuity, Attendance & OT Approval, Storekeeper Reconciliation, and Automatic Daily Cost Posting',
    'author': 'Antigravity DeepMind',
    'license': 'LGPL-3',
    'depends': [
        'contractor_budget_procurement',
        'hr',
        'hr_attendance',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/ir_rule.xml',
        'data/ir_sequence.xml',
        'views/site_staff_schedule_views.xml',
        'views/daily_progress_report_views.xml',
        'views/site_attendance_views.xml',
        'views/site_material_consumption_views.xml',
        'views/site_work_and_issues_views.xml',
        'views/daily_cost_posting_views.xml',
        'views/contractor_site_ops_menus.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}

