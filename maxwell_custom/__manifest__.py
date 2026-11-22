{
    'name': 'Maxwell Custom Reports (Sales, Purchase & Invoice)',
    'version': '20.0.1.0.0',
    'category': 'Reporting',
    'summary': 'Maxwell Trading and Contracting WLL custom PDF report format for Sales Order, Purchase Order, and Invoice',
    'description': """
Maxwell Custom Report Formats
=============================
Updates the PDF print format for:
- Sales Order / Quotation (`sale.order`)
- Purchase Order / Request for Quotation (`purchase.order`)
- Customer Invoice / Vendor Bill (`account.move`)
to match the official Maxwell Trading and Contracting WLL layout.
    """,
    'author': 'Maxwell Trading and Contracting WLL',
    'depends': ['web', 'sale', 'purchase', 'account'],
    'data': [
        'views/sale_order_views.xml',
        'views/purchase_order_views.xml',
        'views/account_move_views.xml',
        'report/paperformat.xml',
        'report/maxwell_report_templates.xml',
    ],
    'assets': {
        'web.report_assets_common': [
            'maxwell_custom/static/src/css/maxwell_report.css',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
