# -*- coding: utf-8 -*-
{
    'name': 'Contractor CRM & Cost Estimation',
    'version': '19.0.1.0.0',
    'category': 'Contractor ERP',
    'summary': 'CRM lead qualification, site visit upload gates, 4-block cost estimation, margin governance, customer acceptance, and variation order management.',
    'description': """
Contractor CRM & Cost Estimation Suite
======================================
This module provides complete front-end commercial lifecycle governance for contracting firms:

1. **CRM Lead & Site Visit Upload Gate:**
   - Mandatory lead attributes: Partner, Contact Name, Inquiry Source, Description, and Assigned Manager.
   - Controlled contractor stages: Open, Site Visit, Quoted, Sent to Client, Negotiation, Won, Lost, On Hold, Declined.
   - Mandatory Reason Wizard for setting Lost, On Hold, or Declined.
   - contractor.site.visit model: requires assigned engineer, scheduled date, observations, measurements, photos, and drawings.
   - Hard-stop upload gate: Creating a Cost Estimate / Quotation is blocked until at least one Site Visit is marked Completed with attachments.
   - Reopening leads flags Requires Fresh Cost Review, blocking quotation submission until rates are re-verified.

2. **4-Block Contractor Cost Estimation & Margin Control:**
   - Block 1: Labor Cost Estimation (Permanent/Temporary, role, hourly vs lump sum, rates).
   - Block 2: Material Cost Estimation (Catalog vs Non-catalog New SKU with mandatory cost basis justification).
   - Block 3: Legal, Permits & Statutory Fees (Municipality, civil defense, road occupancy, gate passes).
   - Block 4: Miscellaneous & Site Expenses (Logistics, PPE, welfare, site power, lab testing).
   - Margin Control: Percentage or Fixed margin calculation, proposed quotation value consolidation.
   - Submission Hard-Stop: Enforces Labor > 0, Material > 0, payment terms, and margin threshold compliance against company settings.

3. **Manager 3-Option Review, Dispatch Gate & Negotiation:**
   - Manager Actions on submitted quotes: Approve As-Is (starts 24h Sales Admin timer), Return for Correction (reason wizard), or Edit & Approve (archives v1 and creates v2).
   - Sales Admin Dispatch Gate: Restricts sending quotes to client until formally approved by management. Blocks standard emailing/printing in draft/submitted states.
   - Client Negotiation Wizard: Captures client correspondence, archives version snapshot, and re-routes for manager review.

4. **Customer Acceptance Gate & Project Conversion:**
   - Validates client signed acceptance documents (Signed Quote, LPO, or Contract).
   - Hard-stop: Blocks project conversion and invoice generation without verified acceptance documents.
   - Conversion: Generates unique Project Code, sets up project.project and initial contractor.project.budget carrying forward the 4 estimation blocks as baseline budget lines.

5. **Variation Order (VO) Management:**
   - contractor.variation.order: Scope narrative, schedule impact, and the identical 4-block cost estimation.
   - Threshold routing: Automatic MD approval routing if value exceeds company threshold or cumulative contract ratio trigger.
   - Integration Hard-Stop: Requires client signed acceptance documents.
   - Traceable Integration: Appends accepted VOs as separate, segregated line items (VO 1, VO 2...) on the master budget.
    """,
    'author': 'Contractor ERP Suite',
    'website': 'https://github.com/contractor-erp',
    'license': 'LGPL-3',
    'depends': [
        'contractor_core',
        'crm',
        'sale_management',
        'project',
        'stock',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/ir_rule.xml',
        'data/ir_sequence.xml',
        'views/site_visit_views.xml',
        'views/crm_lead_views.xml',
        'views/sale_order_views.xml',
        'views/project_project_views.xml',
        'views/project_budget_views.xml',
        'views/variation_order_views.xml',
        'views/negotiation_wizard_views.xml',
        'views/contractor_crm_menus.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}