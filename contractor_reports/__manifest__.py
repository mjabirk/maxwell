# -*- coding: utf-8 -*-
{
    'name': 'Contractor ERP - Analytics & 67 Standard Reports',
    'version': '20.0.1.0.0',
    'category': 'Contractor ERP/Analytics',
    'summary': '67 Standard Analytical Reports for Construction ERP: Sales, Estimation, Procurement, Stores, Site Operations, HRMS & Finance',
    'description': """
Contractor ERP Analytics Suite
==============================
Provides 67 comprehensive PostgreSQL analytical views (_auto = False) and Odoo 20 list, pivot, graph, and search views across 4 core business domains:

1. Group A (16 Reports): Sales, Quotations & Variation Orders
2. Group B (19 Reports): Procurement, Inventory & Emergency Purchases
3. Group C1 (19 Reports): Site Operations, Daily Tracking & Workforce HRMS
4. Group C2 (13 Reports): Construction Accounting, Treasury & WIP Profitability
    """,
    'author': 'Antigravity Deepmind',
    'license': 'LGPL-3',
    'depends': [
        'contractor_finance',
    ],
    'data': [
        'security/ir.access.csv',
        'views/report_group_a_sales_views.xml',
        'views/report_group_b_procurement_views.xml',
        'views/report_group_c_site_hrms_views.xml',
        'views/report_group_c_finance_views.xml',
        'views/contractor_reports_menus.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}

