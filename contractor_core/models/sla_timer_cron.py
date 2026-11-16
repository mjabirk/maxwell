# -*- coding: utf-8 -*-
from datetime import timedelta
from markupsafe import Markup
from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


class ContractorSlaTimer(models.AbstractModel):
    _name = 'contractor.sla.timer'
    _description = 'Contractor SLA Auto-Escalation & Reminder Engine'

    @api.model
    def _cron_process_sla_escalations(self):
        """Hourly scheduled action processing platform SLA timers, reminders, and escalations.
        All thresholds and hours are dynamically evaluated from company configurations.
        """
        companies = self.env['res.company'].search([])
        now = fields.Datetime.now()
        _logger.info("Contractor SLA Cron: Starting processing for %s companies at %s", len(companies), now)

        for company in companies:
            self._process_quotation_approval_slas(company, now)
            self._process_vo_approval_slas(company, now)
            self._process_sales_release_slas(company, now)
            self._process_material_requisition_slas(company, now)
            self._process_client_followup_and_stagnation(company, now)

        _logger.info("Contractor SLA Cron: Completed escalation run successfully.")

    @api.model
    def _get_users_in_group(self, group_xmlid):
        """Helper to find active users belonging to (or inheriting) a specific security group."""
        try:
            group = self.env.ref(group_xmlid, raise_if_not_found=False)
            if group:
                return (group.all_user_ids | group.user_ids).filtered(lambda u: u.active)
        except Exception as e:
            _logger.warning("Error fetching users for group %s: %s", group_xmlid, e)
        return self.env['res.users']

    @api.model
    def _has_tier_triggered(self, record, summary, tier_marker=None, since_dt=None):
        """Returns True if this SLA tier already has an open activity or has already been logged in the current cycle."""
        model_id = self.env['ir.model']._get_id(record._name)
        if self.env['mail.activity'].search_count([
            ('res_model_id', '=', model_id),
            ('res_id', '=', record.id),
            ('summary', '=', summary),
        ]):
            return True

        marker = tier_marker or summary
        msg_domain = [
            ('model', '=', record._name),
            ('res_id', '=', record.id),
        ]
        if marker != summary:
            msg_domain.extend(['|', ('body', 'ilike', marker), ('body', 'ilike', summary)])
        else:
            msg_domain.append(('body', 'ilike', summary))
        if since_dt:
            msg_domain.append(('date', '>=', since_dt))
        return bool(self.env['mail.message'].search_count(msg_domain))

    @api.model
    def _supersede_lower_tier_activities(self, record, summaries):
        """Closes obsolete lower-tier SLA activities when a record escalates to a higher tier."""
        if not summaries:
            return
        model_id = self.env['ir.model']._get_id(record._name)
        obsolete = self.env['mail.activity'].search([
            ('res_model_id', '=', model_id),
            ('res_id', '=', record.id),
            ('summary', 'in', list(summaries)),
        ])
        if obsolete:
            obsolete.unlink()

    @api.model
    def _schedule_governance_activity(self, record, user, summary, note, deadline=None):
        """Creates an activity if an uncompleted activity with the same summary does not already exist."""
        if not user or not record:
            return False

        activity_model = self.env['mail.activity']
        model_id = self.env['ir.model']._get_id(record._name)
        existing = activity_model.search([
            ('res_model_id', '=', model_id),
            ('res_id', '=', record.id),
            ('user_id', '=', user.id),
            ('summary', '=', summary),
        ], limit=1)

        if not existing:
            act_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
            activity_model.create({
                'activity_type_id': act_type.id if act_type else 1,
                'res_model_id': model_id,
                'res_id': record.id,
                'user_id': user.id,
                'summary': summary,
                'note': note,
                'date_deadline': deadline or fields.Date.context_today(self),
            })
            return True
        return False

    @api.model
    def _dispatch_sla_tier(
        self,
        record,
        users,
        summary,
        note,
        chatter_body=None,
        tier_marker=None,
        since_dt=None,
        supersede_summaries=None,
    ):
        """Fires an SLA tier exactly once per workflow cycle: supersedes lower-tier activities,
        assigns tier activities to target users, and posts a single formatted chatter alert.
        """
        if self._has_tier_triggered(record, summary, tier_marker=tier_marker, since_dt=since_dt):
            return False

        self._supersede_lower_tier_activities(record, supersede_summaries)

        for user in users:
            self._schedule_governance_activity(record, user, summary, note)

        if chatter_body and hasattr(record, 'message_post'):
            record.message_post(
                body=chatter_body,
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
        return True

    @api.model
    def _process_quotation_approval_slas(self, company, now):
        """Checks submitted quotations exceeding 48h manager reminder and 72h MD escalation SLAs."""
        if 'sale.order' not in self.env:
            return

        SaleOrder = self.env['sale.order']
        md_users = self._get_users_in_group('contractor_core.group_contractor_md')
        manager_users = self._get_users_in_group('contractor_core.group_contractor_manager')
        has_contractor_state = 'contractor_quote_state' in SaleOrder._fields

        domain = [('company_id', '=', company.id)]
        if has_contractor_state:
            domain.append(('contractor_quote_state', '=', 'submitted'))
        else:
            domain.append(('state', '=', 'draft'))

        quotes = SaleOrder.search(domain)
        esc_hours = company.quote_escalation_sla_hours or 72
        app_hours = company.quote_approval_sla_hours or 48

        for quote in quotes:
            if has_contractor_state:
                ref_date = quote.submitted_date or quote.write_date or quote.create_date or now
            else:
                ref_date = quote.create_date or quote.write_date or now
            elapsed_hours = (now - ref_date).total_seconds() / 3600.0

            rem_summary = f"SLA REMINDER: Quotation {quote.name} Pending Approval ({app_hours}h+ SLA)"
            esc_summary = f"CRITICAL SLA ESCALATION: Quotation {quote.name} Approval Overdue ({esc_hours}h+ SLA)"

            # 1. 72h+ MD Escalation (fires once per submission cycle)
            if elapsed_hours >= esc_hours:
                note = Markup(_(
                    "<p><strong>Executive SLA Escalation:</strong></p>"
                    "<p>Quotation <b>%s</b> has been pending approval for <b>%d hours</b>, exceeding the company "
                    "escalation threshold of %d hours.</p>"
                    "<p>Immediate Managing Director intervention or override required.</p>"
                )) % (quote.name, int(elapsed_hours), esc_hours)
                chatter_body = Markup(_(
                    "<strong>CRITICAL SLA ESCALATION:</strong> Quotation approval has elapsed %d hours "
                    "(threshold: %d hours). Escalated to Managing Director."
                )) % (int(elapsed_hours), esc_hours)

                self._dispatch_sla_tier(
                    quote,
                    md_users,
                    esc_summary,
                    note,
                    chatter_body=chatter_body,
                    tier_marker="<strong>CRITICAL SLA ESCALATION:</strong>",
                    since_dt=ref_date,
                    supersede_summaries=[rem_summary],
                )

            # 2. 48h+ Manager Reminder (fires once per submission cycle)
            elif elapsed_hours >= app_hours:
                note = Markup(_(
                    "<p><strong>Management SLA Reminder:</strong></p>"
                    "<p>Quotation <b>%s</b> has been awaiting review for <b>%d hours</b> (threshold: %d hours).</p>"
                    "<p>Please review quotation margins and cost allocations.</p>"
                )) % (quote.name, int(elapsed_hours), app_hours)
                chatter_body = Markup(_(
                    "<strong>SLA REMINDER:</strong> Quotation has elapsed %d hours awaiting approval "
                    "(threshold: %d hours). Notification dispatched to Contractor Managers."
                )) % (int(elapsed_hours), app_hours)

                self._dispatch_sla_tier(
                    quote,
                    manager_users,
                    rem_summary,
                    note,
                    chatter_body=chatter_body,
                    tier_marker="<strong>SLA REMINDER:</strong>",
                    since_dt=ref_date,
                )

    @api.model
    def _process_sales_release_slas(self, company, now):
        """Checks approved quotations pending client release exceeding 24h Sales Admin and 48h Manager SLAs."""
        if 'sale.order' not in self.env:
            return

        SaleOrder = self.env['sale.order']
        sales_admins = self._get_users_in_group('contractor_core.group_contractor_sales_admin')
        managers = self._get_users_in_group('contractor_core.group_contractor_manager')
        has_contractor_state = 'contractor_quote_state' in SaleOrder._fields

        domain = [('company_id', '=', company.id)]
        if has_contractor_state:
            domain.append(('contractor_quote_state', '=', 'approved_pending_release'))
        else:
            domain.append(('state', '=', 'sale'))

        orders = SaleOrder.search(domain)
        rel_esc_hours = company.sales_release_escalation_hours or 48
        rel_sla_hours = company.sales_release_sla_hours or 24

        for order in orders:
            if has_contractor_state:
                ref_date = order.approved_date or order.write_date or now
            else:
                ref_date = order.date_order or order.write_date or now
            elapsed_hours = (now - ref_date).total_seconds() / 3600.0

            rem_summary = f"SLA DISPATCH REMINDER: Order {order.name} Awaiting Sales Release ({rel_sla_hours}h+ SLA)"
            esc_summary = f"SLA BREACH: Sales Release Pending for {order.name} ({rel_esc_hours}h+ SLA)"

            # 48h+ Manager Escalation
            if elapsed_hours >= rel_esc_hours:
                note = Markup(_(
                    "<p>Sales Order <b>%s</b> approved %d hours ago has not completed client dispatch / sales release.</p>"
                )) % (order.name, int(elapsed_hours))
                chatter_body = Markup(_(
                    "<strong>SLA BREACH:</strong> Sales release has elapsed %d hours without dispatch "
                    "(threshold: %d hours). Escalated to Contractor Managers."
                )) % (int(elapsed_hours), rel_esc_hours)
                self._dispatch_sla_tier(
                    order,
                    managers,
                    esc_summary,
                    note,
                    chatter_body=chatter_body,
                    tier_marker="<strong>SLA BREACH:</strong>",
                    since_dt=ref_date,
                    supersede_summaries=[rem_summary],
                )

            # 24h+ Sales Admin Reminder
            elif elapsed_hours >= rel_sla_hours:
                note = Markup(_(
                    "<p>Please finalize and dispatch sales order <b>%s</b> to the client.</p>"
                )) % order.name
                chatter_body = Markup(_(
                    "<strong>SLA DISPATCH REMINDER:</strong> Sales release has been pending for %d hours "
                    "(threshold: %d hours). Notification dispatched to Sales Administration."
                )) % (int(elapsed_hours), rel_sla_hours)
                self._dispatch_sla_tier(
                    order,
                    sales_admins,
                    rem_summary,
                    note,
                    chatter_body=chatter_body,
                    tier_marker="<strong>SLA DISPATCH REMINDER:</strong>",
                    since_dt=ref_date,
                )

    @api.model
    def _process_vo_approval_slas(self, company, now):
        """Checks submitted Variation Orders exceeding 48h manager reminder and 72h MD escalation SLAs."""
        if not company.vo_use_approval_sla_timers or 'contractor.variation.order' not in self.env:
            return

        VO = self.env['contractor.variation.order']
        md_users = self._get_users_in_group('contractor_core.group_contractor_md')
        manager_users = self._get_users_in_group('contractor_core.group_contractor_manager')
        vos = VO.search([
            ('company_id', '=', company.id),
            ('state', '=', 'submitted'),
        ])
        esc_hours = company.quote_escalation_sla_hours or 72
        app_hours = company.quote_approval_sla_hours or 48

        for vo in vos:
            ref_date = vo.write_date or vo.create_date or now
            elapsed_hours = (now - ref_date).total_seconds() / 3600.0
            rem_summary = f"VO SLA REMINDER: {vo.name} Pending Approval ({app_hours}h+ SLA)"
            esc_summary = f"CRITICAL VO SLA ESCALATION: {vo.name} Approval Overdue ({esc_hours}h+ SLA)"

            if elapsed_hours >= esc_hours:
                note = Markup(_(
                    "<p><strong>Executive VO SLA Escalation:</strong></p>"
                    "<p>Variation Order <b>%s</b> has been pending approval for <b>%d hours</b> "
                    "(threshold: %d hours). Immediate Managing Director intervention required.</p>"
                )) % (vo.name, int(elapsed_hours), esc_hours)
                chatter_body = Markup(_(
                    "<strong>CRITICAL VO SLA ESCALATION:</strong> VO approval has elapsed %d hours "
                    "(threshold: %d hours). Escalated to Managing Director."
                )) % (int(elapsed_hours), esc_hours)
                self._dispatch_sla_tier(
                    vo,
                    md_users,
                    esc_summary,
                    note,
                    chatter_body=chatter_body,
                    tier_marker="<strong>CRITICAL VO SLA ESCALATION:</strong>",
                    since_dt=ref_date,
                    supersede_summaries=[rem_summary],
                )
            elif elapsed_hours >= app_hours:
                note = Markup(_(
                    "<p><strong>Management VO SLA Reminder:</strong></p>"
                    "<p>Variation Order <b>%s</b> has been awaiting review for <b>%d hours</b> "
                    "(threshold: %d hours).</p>"
                )) % (vo.name, int(elapsed_hours), app_hours)
                chatter_body = Markup(_(
                    "<strong>VO SLA REMINDER:</strong> VO has elapsed %d hours awaiting approval "
                    "(threshold: %d hours). Notification dispatched to Contractor Managers."
                )) % (int(elapsed_hours), app_hours)
                self._dispatch_sla_tier(
                    vo,
                    manager_users,
                    rem_summary,
                    note,
                    chatter_body=chatter_body,
                    tier_marker="<strong>VO SLA REMINDER:</strong>",
                    since_dt=ref_date,
                )

    @api.model
    def _process_material_requisition_slas(self, company, now):
        """Checks submitted Material Requisitions pending approval > 24h and escalates to MD."""
        if 'contractor.material.requisition' not in self.env:
            return

        MR = self.env['contractor.material.requisition']
        md_users = self._get_users_in_group('contractor_core.group_contractor_md')
        manager_users = self._get_users_in_group('contractor_core.group_contractor_manager')
        target_users = md_users | manager_users

        submitted_mrs = MR.search([
            ('company_id', '=', company.id),
            ('state', '=', 'submitted'),
            ('escalated_to_md', '=', False),
        ])
        mr_hours = company.mr_escalation_sla_hours or 24

        for mr in submitted_mrs:
            ref_date = mr.submitted_datetime or mr.create_date or now
            elapsed_hours = (now - ref_date).total_seconds() / 3600.0
            if elapsed_hours >= mr_hours:
                mr.write({'escalated_to_md': True})
                project_name = mr.project_id.name if mr.project_id else _('N/A')
                summary = f"URGENT SLA ESCALATION: Material Requisition {mr.name} Unapproved ({mr_hours}h+ SLA)"
                note = Markup(_(
                    "<p>Material Requisition <b>%s</b> for Project <b>%s</b> has been awaiting approval for %d hours "
                    "(threshold: %d hours). Escalated to MD.</p>"
                )) % (mr.name, project_name, int(elapsed_hours), mr_hours)
                chatter_body = Markup(_(
                    "<strong>SLA ESCALATION:</strong> Material Requisition elapsed %d hours "
                    "without approval (threshold: %d hours). Escalated to Managing Director."
                )) % (int(elapsed_hours), mr_hours)

                self._dispatch_sla_tier(
                    mr,
                    target_users,
                    summary,
                    note,
                    chatter_body=chatter_body,
                    tier_marker="<strong>SLA ESCALATION:</strong>",
                    since_dt=ref_date,
                )

    @api.model
    def _process_client_followup_and_stagnation(self, company, now):
        """Processes recurring 15d client follow-ups (15d, 30d, 45d, 60d...), 45d non-conversion alerts, and 7d draft stagnation."""
        if 'sale.order' not in self.env:
            return

        SaleOrder = self.env['sale.order']
        managers = self._get_users_in_group('contractor_core.group_contractor_manager')
        sales_admins = self._get_users_in_group('contractor_core.group_contractor_sales_admin')
        has_contractor_state = 'contractor_quote_state' in SaleOrder._fields

        followup_days = max(company.client_followup_days or 15, 1)
        bench_days = company.non_conversion_benchmark_days or 45
        stag_days = company.draft_stagnation_days or 7

        # 1. Recurring 15-day Follow-up on Sent/Negotiating Quotations (15d, 30d, 45d, 60d, 75d...)
        sent_domain = [('company_id', '=', company.id)]
        if has_contractor_state:
            sent_domain.append(('contractor_quote_state', 'in', ['sent_to_client', 'under_negotiation']))
        else:
            sent_domain.append(('state', '=', 'sent'))

        sent_quotes = SaleOrder.search(sent_domain)

        for quote in sent_quotes:
            if has_contractor_state:
                ref_date = quote.client_dispatch_datetime or quote.date_order or quote.write_date or now
            else:
                ref_date = quote.date_order or quote.write_date or now
            elapsed_days = (now - ref_date).total_seconds() / 86400.0
            salesperson = quote.user_id or self.env.user

            milestone_days = int(elapsed_days // followup_days) * followup_days
            if milestone_days >= followup_days:
                prev_milestones = [
                    f"CLIENT FOLLOW-UP ({d}d): Follow-up on Quotation {quote.name}"
                    for d in range(followup_days, milestone_days, followup_days)
                ]
                # Backward-compatible cleanup of legacy summary strings
                prev_milestones.extend([
                    f"CLIENT FOLLOW-UP ({followup_days}d): Follow-up on Sent Quotation {quote.name}",
                    f"URGENT CLIENT FOLLOW-UP ({followup_days * 2}d): Re-engage Client on {quote.name}",
                ])
                summary_fu = f"CLIENT FOLLOW-UP ({milestone_days}d): Follow-up on Quotation {quote.name}"
                tier_marker = f"<strong>CLIENT FOLLOW-UP ({milestone_days}d):</strong>"
                note = Markup(_(
                    "<p>%d days have elapsed since quotation <b>%s</b> was dispatched to the client. "
                    "Please perform recurring follow-up and log the update.</p>"
                )) % (milestone_days, quote.name)
                chatter_body = Markup(_(
                    "<strong>CLIENT FOLLOW-UP (%dd):</strong> %d days elapsed since quotation dispatch "
                    "(recurring %d-day cadence). Follow-up assigned."
                )) % (milestone_days, int(elapsed_days), followup_days)
                self._dispatch_sla_tier(
                    quote,
                    salesperson | sales_admins,
                    summary_fu,
                    note,
                    chatter_body=chatter_body,
                    tier_marker=tier_marker,
                    since_dt=ref_date,
                    supersede_summaries=prev_milestones,
                )

        # 2. Non-Conversion Benchmark Alert (45d+) on ALL Approved / Sent / Accepted Unconverted Quotations
        bench_domain = [('company_id', '=', company.id)]
        if has_contractor_state:
            bench_domain.append(('contractor_quote_state', 'in', [
                'approved_pending_release', 'sent_to_client', 'under_negotiation', 'accepted',
            ]))
            if 'project_id' in SaleOrder._fields:
                bench_domain.append(('project_id', '=', False))
        else:
            bench_domain.append(('state', '=', 'sent'))

        unconverted_quotes = SaleOrder.search(bench_domain)
        for quote in unconverted_quotes:
            if has_contractor_state:
                ref_date = quote.approved_date or quote.client_dispatch_datetime or quote.date_order or quote.write_date or now
            else:
                ref_date = quote.date_order or quote.write_date or now
            elapsed_days = (now - ref_date).total_seconds() / 86400.0
            if elapsed_days >= bench_days:
                salesperson = quote.user_id or self.env.user
                summary_bench = f"NON-CONVERSION BENCHMARK ({bench_days}d+): Review Quotation {quote.name}"
                note = Markup(_(
                    "<p>Approved/sent quotation <b>%s</b> has remained unconverted for %d days "
                    "(benchmark: %d days).</p>"
                    "<p>Sales Admin must update quotation status with a mandatory reason or mark Lost/On Hold.</p>"
                )) % (quote.name, int(elapsed_days), bench_days)
                chatter_body = Markup(_(
                    "<strong>NON-CONVERSION BENCHMARK:</strong> Quotation has remained unconverted for %d days "
                    "(benchmark: %d days). Manager notified; Sales Admin status update required."
                )) % (int(elapsed_days), bench_days)
                self._dispatch_sla_tier(
                    quote,
                    salesperson | managers | sales_admins,
                    summary_bench,
                    note,
                    chatter_body=chatter_body,
                    tier_marker="<strong>NON-CONVERSION BENCHMARK:</strong>",
                    since_dt=ref_date,
                )

        # 3. 7d Draft Stagnation Alerts
        draft_domain = [
            ('company_id', '=', company.id),
            ('state', '=', 'draft'),
        ]
        if has_contractor_state:
            draft_domain.append(('contractor_quote_state', '=', 'draft'))

        draft_quotes = SaleOrder.search(draft_domain)

        for draft in draft_quotes:
            ref_date = draft.write_date or draft.create_date or now
            elapsed_days = (now - ref_date).total_seconds() / 86400.0
            if elapsed_days >= stag_days:
                salesperson = draft.user_id or self.env.user
                summary = f"DRAFT STAGNATION ({stag_days}d+): Inactive Draft Quote {draft.name}"
                note = Markup(_(
                    "<p>Draft Quotation <b>%s</b> has remained untouched for %d days (threshold: %d days). "
                    "Please update estimation or cancel.</p>"
                )) % (draft.name, int(elapsed_days), stag_days)
                chatter_body = Markup(_(
                    "<strong>DRAFT STAGNATION:</strong> Draft quotation has remained inactive for %d days "
                    "(threshold: %d days). Follow-up reminder scheduled."
                )) % (int(elapsed_days), stag_days)
                self._dispatch_sla_tier(
                    draft,
                    salesperson,
                    summary,
                    note,
                    chatter_body=chatter_body,
                    tier_marker="<strong>DRAFT STAGNATION:</strong>",
                    since_dt=ref_date,
                )


