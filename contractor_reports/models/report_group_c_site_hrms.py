# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.tools import SQL


# -------------------------------------------------------------------------
# 36. Daily Progress Report Compliance & Submission Timeliness
# -------------------------------------------------------------------------
class ContractorReportDPRCompliance(models.Model):
    _name = 'contractor.report.dpr.compliance'
    _description = 'Daily Progress Report Compliance & Submission Timeliness'
    _auto = False
    _order = 'report_date desc, id desc'

    dpr_id = fields.Many2one('contractor.daily.progress.report', string='DPR Reference', readonly=True)
    name = fields.Char(string='DPR Number', readonly=True)
    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    site_engineer_id = fields.Many2one('res.users', string='Site Engineer', readonly=True)
    report_date = fields.Date(string='Report Date', readonly=True)
    create_date = fields.Datetime(string='Logged Timestamp', readonly=True)
    approval_date = fields.Datetime(string='PM Approval Datetime', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft DPR'),
        ('submitted', 'Submitted to PM'),
        ('pm_approved', 'Approved by PM'),
    ], string='Status', readonly=True)
    is_late = fields.Boolean(string='Submitted After 48h Cutoff', readonly=True)
    late_amendment_approved = fields.Boolean(string='Late Amendment PM Authorized', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                d.id AS id,
                d.id AS dpr_id,
                d.name AS name,
                d.project_id AS project_id,
                d.site_engineer_id AS site_engineer_id,
                d.report_date AS report_date,
                d.create_date AS create_date,
                d.approval_date AS approval_date,
                d.state AS state,
                CASE
                    WHEN COALESCE(d.is_late_submission, COALESCE(d.submitted_datetime, d.create_date) > ((d.report_date + interval '1 day')::timestamp + (COALESCE(rc.dpr_cutoff_hour, 10.0) * interval '1 hour'))) THEN TRUE
                    ELSE FALSE
                END AS is_late,
                d.late_amendment_approved AS late_amendment_approved,
                d.company_id AS company_id
            FROM contractor_daily_progress_report d
            LEFT JOIN res_company rc ON rc.id = d.company_id
        """)


# -------------------------------------------------------------------------
# 37. Site Attendance & Workforce Cost Breakdown
# -------------------------------------------------------------------------
class ContractorReportSiteAttendanceCost(models.Model):
    _name = 'contractor.report.site.attendance.cost'
    _description = 'Site Attendance & Workforce Cost Breakdown by Trade & Category'
    _auto = False
    _order = 'attendance_date desc, id desc'

    attendance_id = fields.Many2one('contractor.site.attendance', string='Attendance Sheet', readonly=True)
    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Operative', readonly=True)
    worker_category = fields.Selection([
        ('permanent', 'Permanent Direct Staff'),
        ('temporary', 'Daily / Temporary Wage Worker'),
        ('manpower_supply', 'Outsourced Manpower Supply'),
    ], string='Worker Category', readonly=True)
    job_title_id = fields.Many2one('hr.job', string='Designation / Trade', readonly=True)
    attendance_date = fields.Date(string='Work Date', readonly=True)
    scheduled_hours = fields.Float(string='Scheduled Hours', readonly=True)
    regular_hours = fields.Float(string='Regular Hours', readonly=True)
    overtime_hours = fields.Float(string='Overtime Hours', readonly=True)
    regular_cost = fields.Monetary(string='Regular Cost', currency_field='currency_id', readonly=True)
    ot_cost = fields.Monetary(string='OT Cost', currency_field='currency_id', readonly=True)
    total_cost = fields.Monetary(string='Total Labor Cost', currency_field='currency_id', readonly=True)
    attendance_status = fields.Selection([
        ('present', 'Present on Site'),
        ('no_show', 'No-Show / Absent'),
        ('unscheduled_addition', 'Unscheduled Addition'),
        ('on_leave', 'Approved Leave'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                l.id AS id,
                l.attendance_id AS attendance_id,
                l.project_id AS project_id,
                l.employee_id AS employee_id,
                l.worker_category AS worker_category,
                l.job_title_id AS job_title_id,
                l.attendance_date AS attendance_date,
                l.scheduled_hours AS scheduled_hours,
                l.regular_hours AS regular_hours,
                l.overtime_hours AS overtime_hours,
                l.regular_cost AS regular_cost,
                l.ot_cost AS ot_cost,
                ROUND((l.regular_cost + l.ot_cost)::numeric, 2) AS total_cost,
                l.attendance_status AS attendance_status,
                a.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_site_attendance_line l
            JOIN contractor_site_attendance a ON a.id = l.attendance_id
            LEFT JOIN res_company rc ON rc.id = a.company_id
        """)


# -------------------------------------------------------------------------
# 38. Daily Site Material Consumption Variance
# -------------------------------------------------------------------------
class ContractorReportMaterialVariance(models.Model):
    _name = 'contractor.report.material.variance'
    _description = 'Daily Site Material Consumption Variance vs Standard'
    _auto = False
    _order = 'consumption_date desc, id desc'

    consumption_id = fields.Many2one('contractor.site.consumption', string='Consumption Sheet', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    product_id = fields.Many2one('product.product', string='Material / SKU', readonly=True)
    cost_head = fields.Selection([
        ('civil', 'Civil & Structural'),
        ('mep', 'MEP (Mechanical, Electrical, Plumbing)'),
        ('finishing', 'Architectural & Finishing'),
        ('external', 'External & Infrastructure'),
        ('general', 'General Site Works'),
    ], string='Cost Head', readonly=True)
    consumption_date = fields.Date(string='Consumption Date', readonly=True)
    issued_to_site_qty = fields.Float(string='Issued to Site Qty', readonly=True)
    previously_consumed_qty = fields.Float(string='Previously Consumed', readonly=True)
    consumed_today_qty = fields.Float(string='Consumed Today', readonly=True)
    remaining_site_stock_qty = fields.Float(string='Remaining Working Stock', readonly=True)
    unit_cost = fields.Monetary(string='Unit Cost', currency_field='currency_id', readonly=True)
    subtotal_cost = fields.Monetary(string='Subtotal Cost', currency_field='currency_id', readonly=True)
    variance_pct = fields.Float(string='Variance (%)', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                l.id AS id,
                l.consumption_id AS consumption_id,
                l.project_id AS project_id,
                l.product_id AS product_id,
                l.cost_head AS cost_head,
                l.consumption_date AS consumption_date,
                l.issued_to_site_qty AS issued_to_site_qty,
                l.previously_consumed_qty AS previously_consumed_qty,
                l.consumed_today_qty AS consumed_today_qty,
                l.remaining_site_stock_qty AS remaining_site_stock_qty,
                l.unit_cost AS unit_cost,
                l.subtotal_cost AS subtotal_cost,
                l.variance_pct AS variance_pct,
                c.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_site_consumption_line l
            JOIN contractor_site_consumption c ON c.id = l.consumption_id
            LEFT JOIN res_company rc ON rc.id = c.company_id
        """)


# -------------------------------------------------------------------------
# 39. Site Incident, Delay & Safety Obstruction Log
# -------------------------------------------------------------------------
class ContractorReportSiteIssues(models.Model):
    _name = 'contractor.report.site.issues'
    _description = 'Site Incident, Delay & Safety Obstruction Log'
    _auto = False
    _order = 'issue_date desc, id desc'

    issue_id = fields.Many2one('contractor.site.issue', string='Issue Reference', readonly=True)
    name = fields.Char(string='Issue Code', readonly=True)
    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    issue_date = fields.Date(string='Incident Date', readonly=True)
    issue_type = fields.Selection([
        ('delay', 'Project Delay / Obstruction'),
        ('safety_incident', 'Safety Incident / Near Miss'),
        ('access_problem', 'Site Access / Permit Issue'),
        ('design_query', 'Design / Drawing Discrepancy (RFI)'),
        ('material_shortfall', 'Material Shortage / Delivery Defect'),
    ], string='Category', readonly=True)
    severity = fields.Selection([
        ('routine', 'Routine Site Note'),
        ('high', 'High Operational Impact'),
        ('material_safety_critical', 'MATERIAL SAFETY CRITICAL / STOP WORK'),
    ], string='Severity', readonly=True)
    state = fields.Selection([
        ('open', 'Open Site Issue'),
        ('escalated', 'Escalated to Project Manager'),
        ('resolved', 'Resolved & Cleared'),
    ], string='Status', readonly=True)
    days_open = fields.Integer(string='Days Unresolved', readonly=True)
    resolved_by = fields.Many2one('res.users', string='Resolved By', readonly=True)
    resolved_date = fields.Datetime(string='Resolution Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                i.id AS id,
                i.id AS issue_id,
                i.name AS name,
                i.project_id AS project_id,
                i.issue_date AS issue_date,
                i.issue_type AS issue_type,
                i.severity AS severity,
                i.state AS state,
                CASE
                    WHEN i.state != 'resolved' THEN GREATEST(0, (CURRENT_DATE - i.issue_date))
                    ELSE GREATEST(0, (i.resolved_date::date - i.issue_date))
                END AS days_open,
                i.resolved_by AS resolved_by,
                i.resolved_date AS resolved_date,
                i.company_id AS company_id
            FROM contractor_site_issue i
        """)


# -------------------------------------------------------------------------
# 40. Daily Cost Posting Reconciliation
# -------------------------------------------------------------------------
class ContractorReportDailyCostReconciliation(models.Model):
    _name = 'contractor.report.daily.cost.reconciliation'
    _description = 'Daily Cost Posting Reconciliation (Labour, Material, Subcon)'
    _auto = False
    _order = 'cost_date desc, id desc'

    dcp_id = fields.Many2one('contractor.daily.cost.posting', string='DCP Reference', readonly=True)
    name = fields.Char(string='Posting Code', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    cost_date = fields.Date(string='Posting Date', readonly=True)
    labour_cost_total = fields.Monetary(string='Labour Cost', currency_field='currency_id', readonly=True)
    material_cost_total = fields.Monetary(string='Material Cost', currency_field='currency_id', readonly=True)
    subcontractor_cost_total = fields.Monetary(string='Subcontractor Cost', currency_field='currency_id', readonly=True)
    grand_total_cost = fields.Monetary(string='Grand Total Incurred', currency_field='currency_id', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Cost Posting'),
        ('posted', 'Posted to Budget & Accounts'),
        ('correction_requested', 'Correction Requested by Accounts'),
    ], string='Status', readonly=True)
    account_move_id = fields.Many2one('account.move', string='Journal Entry', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                p.id AS id,
                p.id AS dcp_id,
                p.name AS name,
                p.project_id AS project_id,
                p.cost_date AS cost_date,
                p.labour_cost_total AS labour_cost_total,
                p.material_cost_total AS material_cost_total,
                p.subcontractor_cost_total AS subcontractor_cost_total,
                p.grand_total_cost AS grand_total_cost,
                p.state AS state,
                p.account_move_id AS account_move_id,
                p.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_daily_cost_posting p
            LEFT JOIN res_company rc ON rc.id = p.company_id
        """)


# -------------------------------------------------------------------------
# 41. 48-Hour Locked Entries Late Amendment Register
# -------------------------------------------------------------------------
class ContractorReportLateAmendedEntries(models.Model):
    _name = 'contractor.report.late.amended.entries'
    _description = '48-Hour Locked Entries Late Amendment Register'
    _auto = False
    _order = 'amendment_date desc, id desc'

    doc_source = fields.Char(string='Document Source Type', readonly=True)
    res_id = fields.Integer(string='Document ID', readonly=True)
    doc_name = fields.Char(string='Document Ref', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    work_date = fields.Date(string='Original Work Date', readonly=True)
    late_amendment_reason = fields.Text(string='PM Justification Reason', readonly=True)
    late_amendment_approver_id = fields.Many2one('res.users', string='Authorized PM', readonly=True)
    amendment_date = fields.Datetime(string='Authorization Timestamp', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                (1000000 + d.id) AS id,
                'Daily Progress Report' AS doc_source,
                d.id AS res_id,
                d.name AS doc_name,
                d.project_id AS project_id,
                d.report_date AS work_date,
                d.late_amendment_reason AS late_amendment_reason,
                d.late_amendment_approver_id AS late_amendment_approver_id,
                d.late_amendment_approval_date AS amendment_date,
                d.company_id AS company_id
            FROM contractor_daily_progress_report d
            WHERE d.late_amendment_approved = TRUE

            UNION ALL

            SELECT
                (2000000 + a.id) AS id,
                'Site Attendance Sheet' AS doc_source,
                a.id AS res_id,
                a.name AS doc_name,
                a.project_id AS project_id,
                a.attendance_date AS work_date,
                a.late_amendment_reason AS late_amendment_reason,
                a.late_amendment_approver_id AS late_amendment_approver_id,
                a.late_amendment_approval_date AS amendment_date,
                a.company_id AS company_id
            FROM contractor_site_attendance a
            WHERE a.late_amendment_approved = TRUE

            UNION ALL

            SELECT
                (3000000 + c.id) AS id,
                'Material Consumption Sheet' AS doc_source,
                c.id AS res_id,
                c.name AS doc_name,
                c.project_id AS project_id,
                c.consumption_date AS work_date,
                c.late_amendment_reason AS late_amendment_reason,
                c.late_amendment_approver_id AS late_amendment_approver_id,
                c.late_amendment_approval_date AS amendment_date,
                c.company_id AS company_id
            FROM contractor_site_consumption c
            WHERE c.late_amendment_approved = TRUE

            UNION ALL

            SELECT
                (4000000 + w.id) AS id,
                'Site Work Progress Log' AS doc_source,
                w.id AS res_id,
                w.name AS doc_name,
                w.project_id AS project_id,
                w.work_date AS work_date,
                w.late_amendment_reason AS late_amendment_reason,
                w.late_amendment_approver_id AS late_amendment_approver_id,
                w.late_amendment_approval_date AS amendment_date,
                w.company_id AS company_id
            FROM contractor_site_work_log w
            WHERE w.late_amendment_approved = TRUE
        """)


# -------------------------------------------------------------------------
# 42. Next-Day Site Schedule vs Actual Attendance
# -------------------------------------------------------------------------
class ContractorReportScheduleVsActual(models.Model):
    _name = 'contractor.report.schedule.vs.actual'
    _description = 'Next-Day Site Schedule vs Actual Attendance'
    _auto = False
    _order = 'schedule_date desc, id desc'

    schedule_line_id = fields.Many2one('contractor.site.schedule.line', string='Schedule Item', readonly=True)
    project_id = fields.Many2one('project.project', string='Project', readonly=True)
    schedule_date = fields.Date(string='Deployment Date', readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Operative', readonly=True)
    worker_category = fields.Selection([
        ('permanent', 'Permanent Direct Staff'),
        ('temporary', 'Daily / Temporary Wage Worker'),
        ('manpower_supply', 'Outsourced Manpower Supply'),
    ], string='Category', readonly=True)
    job_title_id = fields.Many2one('hr.job', string='Designation', readonly=True)
    planned_shift_hours = fields.Float(string='Planned Hours', readonly=True)
    actual_hours = fields.Float(string='Actual Present Hours', readonly=True)
    is_no_show = fields.Boolean(string='No-Show Flag', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                sl.id AS id,
                sl.id AS schedule_line_id,
                sl.project_id AS project_id,
                sl.schedule_date AS schedule_date,
                sl.employee_id AS employee_id,
                sl.worker_category AS worker_category,
                sl.job_title_id AS job_title_id,
                sl.planned_shift_hours AS planned_shift_hours,
                COALESCE(al.regular_hours + al.overtime_hours, 0.0) AS actual_hours,
                CASE
                    WHEN al.id IS NULL OR al.attendance_status = 'no_show' THEN TRUE
                    ELSE FALSE
                END AS is_no_show,
                s.company_id AS company_id
            FROM contractor_site_schedule_line sl
            JOIN contractor_site_schedule s ON s.id = sl.schedule_id
            LEFT JOIN contractor_site_attendance_line al ON al.employee_id = sl.employee_id AND al.attendance_date = sl.schedule_date
        """)


# -------------------------------------------------------------------------
# 43. Employee Wage & Billing Rate History Audit
# -------------------------------------------------------------------------
class ContractorReportEmployeeRateHistory(models.Model):
    _name = 'contractor.report.employee.rate.history'
    _description = 'Employee Wage & Billing Rate History Audit'
    _auto = False
    _order = 'effective_date desc, id desc'

    history_id = fields.Many2one('contractor.dated.rate.history', string='Rate Record', readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Employee / Operative', readonly=True)
    classification = fields.Selection([
        ('foreman', 'Foreman / Supervisor'),
        ('carpenter', 'Carpenter'),
        ('mason', 'Mason'),
        ('electrician', 'Electrician'),
        ('plumber', 'Plumber'),
        ('welder', 'Welder / Fabricator'),
        ('steel_fixer', 'Steel Fixer'),
        ('operator', 'Heavy Equipment Operator'),
        ('driver', 'Site Driver / Logistics'),
        ('surveyor', 'Site Surveyor'),
        ('safety', 'HSE / Safety Officer'),
        ('qa_qc', 'QA/QC Inspector'),
        ('helper', 'General Labor / Helper'),
        ('engineer', 'Site Engineer'),
    ], string='Trade / Trade Classification', readonly=True)
    effective_date = fields.Date(string='Effective From', readonly=True)
    effective_to = fields.Date(string='Effective To', readonly=True)
    system_hourly_rate = fields.Monetary(string='System Hourly Rate', currency_field='currency_id', readonly=True)
    override_hourly_rate = fields.Monetary(string='Override Hourly Rate', currency_field='currency_id', readonly=True)
    effective_hourly_rate = fields.Monetary(string='Effective Cost Rate', currency_field='currency_id', readonly=True)
    has_override = fields.Boolean(string='Override Applied', readonly=True)
    reason = fields.Text(string='Rate Revision Justification', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                h.id AS id,
                h.id AS history_id,
                h.employee_id AS employee_id,
                h.classification AS classification,
                h.effective_date AS effective_date,
                h.effective_to AS effective_to,
                h.system_hourly_rate AS system_hourly_rate,
                h.override_hourly_rate AS override_hourly_rate,
                h.effective_hourly_rate AS effective_hourly_rate,
                CASE
                    WHEN h.override_hourly_rate > 0.0 THEN TRUE
                    ELSE FALSE
                END AS has_override,
                h.reason AS reason,
                h.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_dated_rate_history h
            LEFT JOIN res_company rc ON rc.id = h.company_id
        """)


# -------------------------------------------------------------------------
# 44. Daily Labour Deployment & Cost by Project
# -------------------------------------------------------------------------
class ContractorReportDailyLabourByProject(models.Model):
    _name = 'contractor.report.daily.labour.by.project'
    _description = 'Daily Labour Deployment & Cost by Project'
    _auto = False
    _order = 'attendance_date desc, total_labour_cost desc'

    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    attendance_date = fields.Date(string='Date', readonly=True)
    worker_category = fields.Selection([
        ('permanent', 'Permanent Direct Staff'),
        ('temporary', 'Daily / Temporary Wage Worker'),
        ('manpower_supply', 'Outsourced Manpower Supply'),
    ], string='Category', readonly=True)
    headcount = fields.Integer(string='Headcount Present', readonly=True)
    regular_hours = fields.Float(string='Total Regular Hours', readonly=True)
    overtime_hours = fields.Float(string='Total OT Hours', readonly=True)
    total_hours = fields.Float(string='Combined Hours', readonly=True)
    total_labour_cost = fields.Monetary(string='Total Deployed Cost', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                MIN(l.id) AS id,
                l.project_id AS project_id,
                l.attendance_date AS attendance_date,
                l.worker_category AS worker_category,
                COUNT(l.id) AS headcount,
                SUM(l.regular_hours) AS regular_hours,
                SUM(l.overtime_hours) AS overtime_hours,
                SUM(l.regular_hours + l.overtime_hours) AS total_hours,
                SUM(l.regular_cost + l.ot_cost) AS total_labour_cost,
                a.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_site_attendance_line l
            JOIN contractor_site_attendance a ON a.id = l.attendance_id
            LEFT JOIN res_company rc ON rc.id = a.company_id
            WHERE l.attendance_status = 'present'
            GROUP BY l.project_id, l.attendance_date, l.worker_category, a.company_id, rc.currency_id
        """)


# -------------------------------------------------------------------------
# 45. Overtime Hours & Premium Cost Analysis
# -------------------------------------------------------------------------
class ContractorReportOvertimeAnalysis(models.Model):
    _name = 'contractor.report.overtime.analysis'
    _description = 'Overtime Hours & Premium Cost Analysis by Project & Trade'
    _auto = False
    _order = 'attendance_date desc, ot_cost desc'

    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    attendance_date = fields.Date(string='Work Date', readonly=True)
    job_title_id = fields.Many2one('hr.job', string='Trade / Designation', readonly=True)
    worker_category = fields.Selection([
        ('permanent', 'Permanent Direct Staff'),
        ('temporary', 'Daily / Temporary Wage Worker'),
        ('manpower_supply', 'Outsourced Manpower Supply'),
    ], string='Category', readonly=True)
    overtime_hours = fields.Float(string='OT Hours Incurred', readonly=True)
    ot_multiplier = fields.Float(string='OT Multiplier', readonly=True)
    ot_cost = fields.Monetary(string='Total OT Premium Amount', currency_field='currency_id', readonly=True)
    ot_approved = fields.Boolean(string='PM Authorized', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                l.id AS id,
                l.project_id AS project_id,
                l.attendance_date AS attendance_date,
                l.job_title_id AS job_title_id,
                l.worker_category AS worker_category,
                l.overtime_hours AS overtime_hours,
                l.ot_multiplier AS ot_multiplier,
                l.ot_cost AS ot_cost,
                l.ot_approved AS ot_approved,
                a.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_site_attendance_line l
            JOIN contractor_site_attendance a ON a.id = l.attendance_id
            LEFT JOIN res_company rc ON rc.id = a.company_id
            WHERE l.overtime_hours > 0.0
        """)


# -------------------------------------------------------------------------
# 46. Tool Custody & Physical Location Register
# -------------------------------------------------------------------------
class ContractorReportToolRegister(models.Model):
    _name = 'contractor.report.tool.register'
    _description = 'Tool Custody & Physical Location Register'
    _auto = False
    _order = 'tool_code desc, id desc'

    tool_id = fields.Many2one('contractor.tool.asset', string='Tool Asset', readonly=True)
    tool_code = fields.Char(string='Asset Tag / Serial', readonly=True)
    name = fields.Char(string='Tool Description', readonly=True)
    tool_type = fields.Selection([
        ('hand_tool', 'Hand Tool (Wrench, Hammer, Level)'),
        ('power_tool', 'Power Tool (Drill, Grinder, Breaker)'),
        ('measuring_instrument', 'Measuring / Surveying Instrument'),
        ('safety_equipment', 'PPE / Specialized Safety Gear'),
        ('heavy_tool', 'Light Plant & Compactor'),
    ], string='Category', readonly=True)
    condition = fields.Selection([
        ('good', 'Good / Operational'),
        ('damaged', 'Damaged / Needs Repair'),
        ('lost', 'Lost / Missing'),
    ], string='Condition', readonly=True)
    status = fields.Selection([
        ('in_store', 'In Central / Site Store'),
        ('assigned', 'In Field Custody (Assigned)'),
        ('under_repair', 'Under Repair'),
        ('written_off', 'Scrapped / Written Off'),
    ], string='Custody Status', readonly=True)
    current_employee_id = fields.Many2one('hr.employee', string='Custodian Operative', readonly=True)
    current_project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    purchase_date = fields.Date(string='Acquisition Date', readonly=True)
    purchase_value = fields.Monetary(string='Replacement Value', currency_field='currency_id', readonly=True)
    deduction_amount = fields.Monetary(string='Assessed Liability', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                t.id AS id,
                t.id AS tool_id,
                t.tool_code AS tool_code,
                t.name AS name,
                t.tool_type AS tool_type,
                t.condition AS condition,
                t.status AS status,
                t.current_employee_id AS current_employee_id,
                t.current_project_id AS current_project_id,
                t.purchase_date AS purchase_date,
                t.purchase_value AS purchase_value,
                t.deduction_amount AS deduction_amount,
                t.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_tool_asset t
            LEFT JOIN res_company rc ON rc.id = t.company_id
        """)


# -------------------------------------------------------------------------
# 47. Unreturned & Damaged Tools by Operative / Project
# -------------------------------------------------------------------------
class ContractorReportUnreturnedTools(models.Model):
    _name = 'contractor.report.unreturned.tools'
    _description = 'Unreturned & Damaged Tools by Operative / Project'
    _auto = False
    _order = 'purchase_value desc, id desc'

    tool_id = fields.Many2one('contractor.tool.asset', string='Tool Asset', readonly=True)
    tool_code = fields.Char(string='Tag No.', readonly=True)
    name = fields.Char(string='Tool Description', readonly=True)
    current_employee_id = fields.Many2one('hr.employee', string='Responsible Operative', readonly=True)
    current_project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    condition = fields.Selection([
        ('good', 'Good / Operational'),
        ('damaged', 'Damaged / Needs Repair'),
        ('lost', 'Lost / Missing'),
    ], string='Condition', readonly=True)
    status = fields.Selection([
        ('in_store', 'In Central / Site Store'),
        ('assigned', 'In Field Custody (Assigned)'),
        ('under_repair', 'Under Repair'),
        ('written_off', 'Scrapped / Written Off'),
    ], string='Status', readonly=True)
    purchase_value = fields.Monetary(string='Exposure Value', currency_field='currency_id', readonly=True)
    deduction_amount = fields.Monetary(string='Recovery Surcharge', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                t.id AS id,
                t.id AS tool_id,
                t.tool_code AS tool_code,
                t.name AS name,
                t.current_employee_id AS current_employee_id,
                t.current_project_id AS current_project_id,
                t.condition AS condition,
                t.status AS status,
                t.purchase_value AS purchase_value,
                t.deduction_amount AS deduction_amount,
                t.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_tool_asset t
            LEFT JOIN res_company rc ON rc.id = t.company_id
            WHERE t.condition IN ('damaged', 'lost') OR (t.status = 'assigned' AND t.current_employee_id IS NOT NULL)
        """)


# -------------------------------------------------------------------------
# 48. Manpower Supply Agency Reconciliation
# -------------------------------------------------------------------------
class ContractorReportAgencyReconciliation(models.Model):
    _name = 'contractor.report.agency.reconciliation'
    _description = 'Manpower Supply Agency Reconciliation & Hours Variance Audit'
    _auto = False
    _order = 'date_end desc, id desc'

    recon_id = fields.Many2one('contractor.agency.reconciliation', string='MAR Reference', readonly=True)
    name = fields.Char(string='Reconciliation Code', readonly=True)
    agency_id = fields.Many2one('res.partner', string='Supplier Agency', readonly=True)
    agreement_id = fields.Many2one('contractor.agency.agreement', string='SLA Agreement', readonly=True)
    date_start = fields.Date(string='Period Start', readonly=True)
    date_end = fields.Date(string='Period End', readonly=True)
    erp_total_hours = fields.Float(string='ERP Site Hours', readonly=True)
    agency_invoiced_hours = fields.Float(string='Agency Invoiced Hours', readonly=True)
    variance_hours = fields.Float(string='Variance Hours', readonly=True)
    variance_amount = fields.Monetary(string='Discrepancy Amount', currency_field='currency_id', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Reconciliation'),
        ('matched', 'Verified & Matched'),
        ('discrepancy_flagged', 'Discrepancy Flagged (Payment Blocked)'),
        ('resolved', 'Resolved with Agency'),
        ('declined', 'Declined'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                r.id AS id,
                r.id AS recon_id,
                r.name AS name,
                r.agency_id AS agency_id,
                r.agreement_id AS agreement_id,
                r.date_start AS date_start,
                r.date_end AS date_end,
                r.erp_total_hours AS erp_total_hours,
                r.agency_invoiced_hours AS agency_invoiced_hours,
                r.variance_hours AS variance_hours,
                r.variance_amount AS variance_amount,
                r.state AS state,
                r.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_agency_reconciliation r
            LEFT JOIN res_company rc ON rc.id = r.company_id
        """)


# -------------------------------------------------------------------------
# 49. Payroll Summary by Workforce Track
# -------------------------------------------------------------------------
class ContractorReportPayrollSummary(models.Model):
    _name = 'contractor.report.payroll.summary'
    _description = 'Payroll Summary by Workforce Track (Direct, Temp, Agency)'
    _auto = False
    _order = 'date_end desc, id desc'

    payroll_track = fields.Char(string='Workforce Track', readonly=True)
    name = fields.Char(string='Run Reference', readonly=True)
    date_start = fields.Date(string='Period Start', readonly=True)
    date_end = fields.Date(string='Period End', readonly=True)
    total_disbursement = fields.Monetary(string='Total Payroll Value', currency_field='currency_id', readonly=True)
    state = fields.Char(string='Lifecycle Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                (1000000 + p.id) AS id,
                'Track 1: Direct Permanent Staff' AS payroll_track,
                p.name AS name,
                p.date_start AS date_start,
                p.date_end AS date_end,
                p.total_net_payable AS total_disbursement,
                p.state::varchar AS state,
                p.company_id AS company_id,
                rc1.currency_id AS currency_id
            FROM contractor_payroll_run p
            LEFT JOIN res_company rc1 ON rc1.id = p.company_id

            UNION ALL

            SELECT
                (2000000 + t.id) AS id,
                'Track 2: Temporary & Daily Wage' AS payroll_track,
                t.name AS name,
                t.date_start AS date_start,
                t.date_end AS date_end,
                t.total_wage_payable AS total_disbursement,
                t.state::varchar AS state,
                t.company_id AS company_id,
                rc2.currency_id AS currency_id
            FROM contractor_temp_wage_settlement t
            LEFT JOIN res_company rc2 ON rc2.id = t.company_id

            UNION ALL

            SELECT
                (3000000 + a.id) AS id,
                'Track 3: Outsourced Manpower Supply' AS payroll_track,
                a.name AS name,
                a.date_start AS date_start,
                a.date_end AS date_end,
                a.agency_invoiced_amount AS total_disbursement,
                a.state::varchar AS state,
                a.company_id AS company_id,
                rc3.currency_id AS currency_id
            FROM contractor_agency_reconciliation a
            LEFT JOIN res_company rc3 ON rc3.id = a.company_id
        """)


# -------------------------------------------------------------------------
# 50. Annual Leave Utilization vs Project Blackout Periods
# -------------------------------------------------------------------------
class ContractorReportLeaveUtilization(models.Model):
    _name = 'contractor.report.leave.utilization'
    _description = 'Annual Leave Utilization vs Project Blackout Periods'
    _auto = False
    _order = 'date_from desc, id desc'

    leave_id = fields.Many2one('hr.leave', string='Leave Request', readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Employee', readonly=True)
    holiday_status_id = fields.Many2one('hr.leave.type', string='Leave Type', readonly=True)
    date_from = fields.Datetime(string='Start Date', readonly=True)
    date_to = fields.Datetime(string='End Date', readonly=True)
    number_of_days = fields.Float(string='Leave Days', readonly=True)
    is_critical_project_period = fields.Boolean(string='Critical Project Window', readonly=True)
    state = fields.Selection([
        ('draft', 'To Submit'),
        ('confirm', 'To Approve'),
        ('refuse', 'Refused'),
        ('validate1', 'Second Approval'),
        ('validate', 'Approved'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                hl.id AS id,
                hl.id AS leave_id,
                hl.employee_id AS employee_id,
                hl.holiday_status_id AS holiday_status_id,
                hl.date_from AS date_from,
                hl.date_to AS date_to,
                hl.number_of_days AS number_of_days,
                COALESCE(hl.is_critical_project_period, FALSE) AS is_critical_project_period,
                hl.state AS state,
                e.company_id AS company_id
            FROM hr_leave hl
            JOIN hr_employee e ON e.id = hl.employee_id
        """)


# -------------------------------------------------------------------------
# 51. Leave Salary Advance & Entitlement Payout Register
# -------------------------------------------------------------------------
class ContractorReportLeaveSalaryPayout(models.Model):
    _name = 'contractor.report.leave.salary.payout'
    _description = 'Leave Salary Advance & Entitlement Payout Register'
    _auto = False
    _order = 'create_date desc, id desc'

    request_id = fields.Many2one('contractor.leave.salary.request', string='LSR Reference', readonly=True)
    name = fields.Char(string='Payout Number', readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Permanent Employee', readonly=True)
    leave_id = fields.Many2one('hr.leave', string='Approved Leave', readonly=True)
    leave_days = fields.Float(string='Entitled Days', readonly=True)
    daily_salary_rate = fields.Monetary(string='Daily Rate', currency_field='currency_id', readonly=True)
    leave_salary_amount = fields.Monetary(string='Calculated Statutory Amount', currency_field='currency_id', readonly=True)
    approved_amount = fields.Monetary(string='Approved Disbursement', currency_field='currency_id', readonly=True)
    payout_method = fields.Selection([
        ('advance_release', 'Direct Advance Release before Travel'),
        ('with_payroll', 'Processed with Monthly Payroll Run'),
    ], string='Payout Method', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Calculation'),
        ('hr_prepared', 'Prepared by HR'),
        ('accountant_approved', 'Approved by Senior Accountant'),
        ('released', 'Released & Disbursed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', readonly=True)
    create_date = fields.Datetime(string='Lodged Date', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                r.id AS id,
                r.id AS request_id,
                r.name AS name,
                r.employee_id AS employee_id,
                r.leave_id AS leave_id,
                r.leave_days AS leave_days,
                r.daily_salary_rate AS daily_salary_rate,
                r.leave_salary_amount AS leave_salary_amount,
                r.approved_amount AS approved_amount,
                r.payout_method AS payout_method,
                r.state AS state,
                r.create_date AS create_date,
                r.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_leave_salary_request r
            LEFT JOIN res_company rc ON rc.id = r.company_id
        """)


# -------------------------------------------------------------------------
# 52. Annual Air Ticket Allowance Schedule & Due Dates
# -------------------------------------------------------------------------
class ContractorReportTicketAllowanceDue(models.Model):
    _name = 'contractor.report.ticket.allowance.due'
    _description = 'Annual Air Ticket Allowance Schedule & Due Dates'
    _auto = False
    _order = 'request_date desc, id desc'

    request_id = fields.Many2one('contractor.ticket.allowance.request', string='TAR Reference', readonly=True)
    name = fields.Char(string='Request Number', readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Beneficiary Employee', readonly=True)
    request_date = fields.Date(string='Request Date', readonly=True)
    disbursement_type = fields.Selection([
        ('cash_allowance', 'Cash Travel Allowance Payout'),
        ('ticket_booking', 'Direct Airline Ticket Booking'),
    ], string='Disbursement Method', readonly=True)
    allowance_amount = fields.Monetary(string='Allowance Value', currency_field='currency_id', readonly=True)
    is_early_release = fields.Boolean(string='Early Cycle Release', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Request'),
        ('hr_prepared', 'Prepared by HR'),
        ('accountant_approved', 'Approved by Senior Accountant'),
        ('released', 'Released & Disbursed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                t.id AS id,
                t.id AS request_id,
                t.name AS name,
                t.employee_id AS employee_id,
                t.request_date AS request_date,
                t.disbursement_type AS disbursement_type,
                t.allowance_amount AS allowance_amount,
                t.is_early_release AS is_early_release,
                t.state AS state,
                t.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_ticket_allowance_request t
            LEFT JOIN res_company rc ON rc.id = t.company_id
        """)


# -------------------------------------------------------------------------
# 53. End-of-Service Gratuity (EOSB) Actuarial Liability
# -------------------------------------------------------------------------
class ContractorReportEOSBLiability(models.Model):
    _name = 'contractor.report.eosb.liability'
    _description = 'End-of-Service Gratuity (EOSB) Actuarial Liability by Employee'
    _auto = False
    _order = 'accrued_eosb_liability desc, id desc'

    employee_id = fields.Many2one('hr.employee', string='Permanent Employee', readonly=True)
    job_id = fields.Many2one('hr.job', string='Designation', readonly=True)
    department_id = fields.Many2one('hr.department', string='Department', readonly=True)
    join_date = fields.Date(string='Joining Date', readonly=True)
    service_years = fields.Float(string='Years of Service', readonly=True)
    monthly_basic_salary = fields.Monetary(string='Monthly Basic Salary', currency_field='currency_id', readonly=True)
    accrued_eosb_liability = fields.Monetary(string='Accrued Statutory Gratuity Liability', currency_field='currency_id', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                e.id AS id,
                e.id AS employee_id,
                hv.job_id AS job_id,
                hv.department_id AS department_id,
                COALESCE(hv.contract_date_start, e.create_date::date) AS join_date,
                ROUND((GREATEST(0, (CURRENT_DATE - COALESCE(hv.contract_date_start, e.create_date::date))) / 365.25)::numeric, 2) AS service_years,
                COALESCE(e.monthly_basic_salary, 0.0) AS monthly_basic_salary,
                CASE
                    -- Under 1 year: No statutory EOSB entitlement
                    WHEN (CURRENT_DATE - COALESCE(hv.contract_date_start, e.create_date::date)) < 365 THEN 0.0
                    -- 1 to 5 years: configurable first-5-year days per year
                    WHEN (CURRENT_DATE - COALESCE(hv.contract_date_start, e.create_date::date)) <= (5 * 365) THEN
                        ROUND(((COALESCE(e.monthly_basic_salary, 0.0) / 30.0) * COALESCE(rc.eosb_first_5yr_days, 21.0) * ((CURRENT_DATE - COALESCE(hv.contract_date_start, e.create_date::date)) / 365.25))::numeric, 2)
                    -- Above 5 years: configurable first-5-year + after-5-year days
                    ELSE
                        ROUND((
                            ((COALESCE(e.monthly_basic_salary, 0.0) / 30.0) * COALESCE(rc.eosb_first_5yr_days, 21.0) * 5.0) +
                            ((COALESCE(e.monthly_basic_salary, 0.0) / 30.0) * COALESCE(rc.eosb_after_5yr_days, 30.0) * (((CURRENT_DATE - COALESCE(hv.contract_date_start, e.create_date::date)) / 365.25) - 5.0))
                        )::numeric, 2)
                END AS accrued_eosb_liability,
                e.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM hr_employee e
            LEFT JOIN hr_version hv ON hv.id = e.current_version_id
            LEFT JOIN res_company rc ON rc.id = e.company_id
            WHERE e.workforce_category = 'permanent' AND e.active = TRUE
        """)


# -------------------------------------------------------------------------
# 54. Employee Exit & Final Settlement Clearance Register
# -------------------------------------------------------------------------
class ContractorReportFinalSettlement(models.Model):
    _name = 'contractor.report.final.settlement'
    _description = 'Employee Exit & Final Settlement Clearance Register'
    _auto = False
    _order = 'last_working_day desc, id desc'

    exit_id = fields.Many2one('contractor.employee.exit', string='Exit Dossier', readonly=True)
    name = fields.Char(string='Settlement Reference', readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Departed Employee', readonly=True)
    workforce_category = fields.Selection([
        ('permanent', 'Permanent Direct Staff'),
        ('temporary', 'Daily / Temporary Wage Worker'),
        ('manpower_supply', 'Outsourced Manpower Supply'),
    ], string='Category', readonly=True)
    last_working_day = fields.Date(string='Last Working Day', readonly=True)
    exit_type = fields.Selection([
        ('resignation', 'Resignation by Employee'),
        ('termination', 'Termination by Employer'),
        ('contract_end', 'Contract Expiry / Non-Renewal'),
    ], string='Exit Separation Type', readonly=True)
    years_of_service = fields.Float(string='Length of Service', readonly=True)
    eosb_amount = fields.Monetary(string='EOSB Gratuity', currency_field='currency_id', readonly=True)
    damaged_lost_tool_deductions = fields.Monetary(string='Tool Deductions', currency_field='currency_id', readonly=True)
    net_settlement_amount = fields.Monetary(string='Net Settlement Disbursed', currency_field='currency_id', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Calculation'),
        ('clearance_pending', 'Clearance & Tool Return Pending'),
        ('hr_approved', 'Approved by HR Officer'),
        ('accountant_audited', 'Audited by Senior Accountant'),
        ('manager_approved', 'Approved by Manager'),
        ('settled', 'Disbursed & Closed'),
        ('cancelled', 'Cancelled'),
    ], string='Settlement Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                x.id AS id,
                x.id AS exit_id,
                x.name AS name,
                x.employee_id AS employee_id,
                x.workforce_category AS workforce_category,
                x.last_working_day AS last_working_day,
                x.exit_type AS exit_type,
                x.years_of_service AS years_of_service,
                x.eosb_amount AS eosb_amount,
                x.damaged_lost_tool_deductions AS damaged_lost_tool_deductions,
                x.net_settlement_amount AS net_settlement_amount,
                x.state AS state,
                x.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_employee_exit x
            LEFT JOIN res_company rc ON rc.id = x.company_id
        """)


# -------------------------------------------------------------------------
# 68. Site Labour Budget vs Actual Cost Variance
# -------------------------------------------------------------------------
class ContractorReportLabourBudgetVariance(models.Model):
    _name = 'contractor.report.labour.budget.variance'
    _description = 'Site Labour Budget vs Actual Cost Variance'
    _auto = False
    _order = 'variance_pct desc, id desc'

    project_id = fields.Many2one('project.project', string='Project Site', readonly=True)
    budget_id = fields.Many2one('contractor.project.budget', string='Master Budget', readonly=True)
    baseline_budget = fields.Monetary(string='Baseline Labour Budget', currency_field='currency_id', readonly=True)
    vo_addition_amount = fields.Monetary(string='VO Additions', currency_field='currency_id', readonly=True)
    revision_addition_amount = fields.Monetary(string='Internal Revisions', currency_field='currency_id', readonly=True)
    total_approved_budget = fields.Monetary(string='Approved Labour Budget', currency_field='currency_id', readonly=True)
    committed_amount = fields.Monetary(string='Committed Labour', currency_field='currency_id', readonly=True)
    actual_cost_amount = fields.Monetary(string='Actual Labour Cost', currency_field='currency_id', readonly=True)
    remaining_budget = fields.Monetary(string='Remaining Labour Budget', currency_field='currency_id', readonly=True)
    variance_pct = fields.Float(string='Overrun / (Savings) (%)', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                l.id AS id,
                b.project_id AS project_id,
                b.id AS budget_id,
                COALESCE(l.baseline_amount, 0.0) AS baseline_budget,
                COALESCE(l.variation_order_amount, 0.0) AS vo_addition_amount,
                COALESCE(l.internal_revision_amount, 0.0) AS revision_addition_amount,
                COALESCE(l.total_approved_budget, 0.0) AS total_approved_budget,
                COALESCE(l.committed_amount, 0.0) AS committed_amount,
                COALESCE(l.actual_cost_amount, 0.0) AS actual_cost_amount,
                COALESCE(l.remaining_budget, 0.0) AS remaining_budget,
                CASE
                    WHEN COALESCE(l.total_approved_budget, 0.0) > 0 THEN
                        ROUND((((COALESCE(l.actual_cost_amount, 0.0) + COALESCE(l.committed_amount, 0.0)) - l.total_approved_budget)::numeric / l.total_approved_budget::numeric) * 100.0, 2)
                    ELSE 0.0
                END AS variance_pct,
                b.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_project_budget_line l
            JOIN contractor_project_budget b ON b.id = l.budget_id
            LEFT JOIN res_company rc ON rc.id = b.company_id
            WHERE l.block_type = 'labour'
        """)


# -------------------------------------------------------------------------
# 69. Employee Advances & Personal Loans Ledger
# -------------------------------------------------------------------------
class ContractorReportEmployeeAdvances(models.Model):
    _name = 'contractor.report.employee.advances'
    _description = 'Employee Advances & Personal Loans Recovery Tracker'
    _auto = False
    _order = 'outstanding_balance desc, request_date desc, id desc'

    advance_id = fields.Many2one('contractor.employee.advance', string='Advance Reference', readonly=True)
    name = fields.Char(string='Reference', readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Employee', readonly=True)
    advance_type = fields.Selection([
        ('salary_advance', 'Monthly Salary Advance'),
        ('personal_loan', 'Staff Personal Loan'),
        ('site_operational_advance', 'Site Operational Cash Advance'),
    ], string='Advance Category', readonly=True)
    request_date = fields.Date(string='Request Date', readonly=True)
    advance_amount = fields.Monetary(string='Original Advance', currency_field='currency_id', readonly=True)
    monthly_installment = fields.Monetary(string='Monthly Installment', currency_field='currency_id', readonly=True)
    recovered_amount = fields.Monetary(string='Recovered to Date', currency_field='currency_id', readonly=True)
    outstanding_balance = fields.Monetary(string='Outstanding Balance', currency_field='currency_id', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft Request'),
        ('submitted', 'Submitted to HR'),
        ('hr_verified', 'HR Verified'),
        ('approved', 'Management Approved'),
        ('disbursed', 'Disbursed (Active Recovery)'),
        ('recovered', 'Fully Recovered'),
        ('deducted_at_exit', 'Deducted at Exit Settlement'),
        ('declined', 'Declined'),
    ], string='Status', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)

    @property
    def _table_query(self):
        return SQL("""
            SELECT
                a.id AS id,
                a.id AS advance_id,
                a.name AS name,
                a.employee_id AS employee_id,
                a.advance_type AS advance_type,
                a.request_date AS request_date,
                COALESCE(a.advance_amount, 0.0) AS advance_amount,
                COALESCE(a.monthly_installment, 0.0) AS monthly_installment,
                COALESCE(a.recovered_amount, 0.0) AS recovered_amount,
                COALESCE(a.outstanding_balance, 0.0) AS outstanding_balance,
                a.state AS state,
                a.company_id AS company_id,
                rc.currency_id AS currency_id
            FROM contractor_employee_advance a
            LEFT JOIN res_company rc ON rc.id = a.company_id
        """)


