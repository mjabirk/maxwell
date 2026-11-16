# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, time
from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ContractorSiteLockMixin(models.AbstractModel):
    _name = 'contractor.site.lock.mixin'
    _description = 'Contractor Site Operations 48-Hour Entry Lock Mixin'

    is_entry_locked = fields.Boolean(
        string='Site Entry Locked (48h Cutoff)',
        compute='_compute_entry_lock',
        help='True if elapsed time past work date cutoff exceeds company site entry lock hours (default 48h).',
    )
    late_amendment_approved = fields.Boolean(
        string='Late Amendment Authorized by PM',
        default=False,
        tracking=True,
        copy=False,
        help='Indicates that a Project Manager has authorized retroactive editing on this locked site document.',
    )
    late_amendment_reason = fields.Text(
        string='Late Amendment Justification',
        copy=False,
        tracking=True,
    )
    late_amendment_approver_id = fields.Many2one(
        'res.users',
        string='Late Amendment Authorized By',
        readonly=True,
        copy=False,
    )
    late_amendment_approval_date = fields.Datetime(
        string='Late Amendment Approval Date',
        readonly=True,
        copy=False,
    )
    is_locked = fields.Boolean(
        string='Locked',
        related='is_entry_locked',
        readonly=True,
    )
    late_amendment_requested = fields.Boolean(
        string='Late Amendment Requested',
        default=False,
        tracking=True,
        copy=False,
    )
    late_amendment_request_reason = fields.Text(
        string='Late Amendment Request Reason',
        copy=False,
        tracking=True,
    )
    late_amendment_requester_id = fields.Many2one(
        'res.users',
        string='Amendment Requested By',
        readonly=True,
        copy=False,
    )
    late_amendment_request_date = fields.Datetime(
        string='Amendment Request Date',
        readonly=True,
        copy=False,
    )

    def _get_lock_date(self):
        """Returns the operational date governing the lock cutoff for the record."""
        self.ensure_one()
        for field_name in ('report_date', 'attendance_date', 'consumption_date', 'work_date', 'schedule_date', 'date'):
            if hasattr(self, field_name) and getattr(self, field_name):
                return getattr(self, field_name)
        if self.create_date:
            return self.create_date.date()
        return fields.Date.context_today(self)

    def _is_entry_locked_now(self):
        """Returns True if more than configured lock_hours have elapsed since work-date midnight."""
        self.ensure_one()
        target_date = self._get_lock_date()
        if not target_date:
            return False
        company = getattr(self, 'company_id', None) or self.env.company
        lock_hours = company.site_entry_lock_hours or 48
        work_day_end = datetime.combine(target_date + timedelta(days=1), time(hour=0, minute=0))
        elapsed_hours = (fields.Datetime.now() - work_day_end).total_seconds() / 3600.0
        return elapsed_hours > lock_hours

    @api.depends_context('company')
    def _compute_entry_lock(self):
        for rec in self:
            rec.is_entry_locked = rec._is_entry_locked_now()

    @api.model_create_multi
    def create(self, vals_list):
        now = fields.Datetime.now()
        for vals in vals_list:
            if not vals.get('late_amendment_approved'):
                # Extract target date if present in vals
                target_date = None
                for fname in ('report_date', 'attendance_date', 'consumption_date', 'work_date', 'schedule_date', 'date'):
                    if fname in vals and vals[fname]:
                        val = vals[fname]
                        if isinstance(val, str):
                            target_date = fields.Date.from_string(val)
                        else:
                            target_date = val
                        break

                if target_date:
                    company = self.env.company
                    lock_hours = company.site_entry_lock_hours or 48

                    work_day_end = datetime.combine(target_date + timedelta(days=1), time(hour=0, minute=0))
                    elapsed = (now - work_day_end).total_seconds() / 3600.0
                    if elapsed > lock_hours and not self.env.su:
                        raise UserError(_(
                            "48-Hour Site Entry Lock Enforced:\n\n"
                            "Creating a site record for retroactive date '%s' is prohibited because more than "
                            "%d hours have passed since the work date ended (%s).\n\n"
                            "Late entries require Project Manager authorization."
                        ) % (target_date, lock_hours, work_day_end.strftime('%Y-%m-%d %H:%M')))
        return super().create(vals_list)

    def write(self, vals):
        # Exclude amendment approval fields from locking check
        allowed_bypass_keys = {
            'is_entry_locked',
            'is_locked',
            'late_amendment_approved',
            'late_amendment_reason',
            'late_amendment_approver_id',
            'late_amendment_approval_date',
            'late_amendment_requested',
            'late_amendment_request_reason',
            'late_amendment_requester_id',
            'late_amendment_request_date',
            'message_follower_ids',
            'activity_ids',
        }
        modified_keys = set(vals.keys()) - allowed_bypass_keys

        if modified_keys and not self.env.su:
            for rec in self:
                if rec._is_entry_locked_now() and not rec.late_amendment_approved:
                    company = getattr(rec, 'company_id', None) or self.env.company
                    lock_hours = company.site_entry_lock_hours or 48
                    raise UserError(_(
                        "48-Hour Site Entry Lock Enforced:\n\n"
                        "This operational site record for date '%s' is locked.\n"
                        "More than %d hours have elapsed since the operational cutoff.\n\n"
                        "Edits are strictly blocked unless a Late Amendment is authorized by the Project Manager with documented justification."
                    ) % (rec._get_lock_date(), lock_hours))

        return super().write(vals)

    def action_approve_late_amendment(self):
        """Allows Project Manager to authorize retroactive edits on locked site records."""
        self.ensure_one()
        if not self.env.user.has_group('contractor_core.group_contractor_project_manager'):
            raise UserError(_("Access Denied: Only a Contractor Project Manager can authorize late amendments on locked site records."))

        if hasattr(self, 'action_open_reason_wizard'):
            return self.action_open_reason_wizard(
                action_type='override',
                callback_method='_callback_late_amendment',
            )

        self.write({
            'late_amendment_approved': True,
            'late_amendment_approver_id': self.env.user.id,
            'late_amendment_approval_date': fields.Datetime.now(),
            'late_amendment_reason': _("Authorized by Project Manager %s.") % self.env.user.name,
        })
        if hasattr(self, 'message_post'):
            self.message_post(
                body=Markup(_("<strong>Late Amendment Authorized:</strong> Approved by Project Manager %s.")) % self.env.user.name,
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
        return True

    def _callback_late_amendment(self, reason=None):
        self.write({
            'late_amendment_approved': True,
            'late_amendment_reason': reason or getattr(self, 'last_action_reason', _('Late amendment granted by PM.')),
            'late_amendment_approver_id': self.env.user.id,
            'late_amendment_approval_date': fields.Datetime.now(),
        })
        if hasattr(self, 'message_post'):
            self.message_post(
                body=Markup(_("<strong>Late Amendment Authorized:</strong> Approved by %s.<br/>Reason: %s")) % (
                    self.env.user.name, self.late_amendment_reason
                ),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )

    def action_request_late_amendment(self):
        """Guided workflow allowing Site Engineers to request PM late amendment authorization on locked records."""
        self.ensure_one()
        return {
            'name': _('Request Late Amendment'),
            'type': 'ir.actions.act_window',
            'res_model': 'contractor.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
                'default_action_type': 'reopen',
                'default_callback_method': '_callback_request_late_amendment',
            },
        }

    def _callback_request_late_amendment(self, reason=None):
        """Records the late amendment request and creates a PM follow-up activity/notification."""
        clean_reason = reason or getattr(self, 'last_action_reason', _('Late amendment requested by site team.'))
        for rec in self:
            rec.write({
                'late_amendment_requested': True,
                'late_amendment_request_reason': clean_reason,
                'late_amendment_requester_id': self.env.user.id,
                'late_amendment_request_date': fields.Datetime.now(),
            })
            pm_user = False
            if hasattr(rec, 'pm_id') and rec.pm_id:
                pm_user = rec.pm_id
            elif hasattr(rec, 'project_id') and rec.project_id and rec.project_id.user_id:
                pm_user = rec.project_id.user_id
            if not pm_user:
                pm_group = self.env.ref('contractor_core.group_contractor_project_manager', raise_if_not_found=False)
                if pm_group:
                    users = (pm_group.all_user_ids | pm_group.user_ids).filtered(lambda u: u.active)
                    pm_user = users[:1]
            if pm_user and hasattr(rec, 'activity_schedule'):
                rec.activity_schedule(
                    'mail.mail_activity_data_todo',
                    user_id=pm_user.id,
                    summary=_('Late Amendment Requested: %s') % (getattr(rec, 'name', None) or rec.display_name),
                    note=Markup(_('Requested by <strong>%s</strong>:<br/>%s')) % (
                        self.env.user.name, clean_reason
                    ),
                    date_deadline=fields.Date.context_today(rec),
                )


