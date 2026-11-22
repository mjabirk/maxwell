# -*- coding: utf-8 -*-
from odoo import api, fields, models

CONTRACTOR_ROLE_GROUP_XMLIDS = (
    'contractor_core.group_contractor_sales_admin',
    'contractor_core.group_contractor_site_engineer',
    'contractor_core.group_contractor_project_manager',
    'contractor_core.group_contractor_procurement_officer',
    'contractor_core.group_contractor_storekeeper',
    'contractor_core.group_contractor_hr_officer',
    'contractor_core.group_contractor_accountant',
    'contractor_core.group_contractor_senior_accountant',
    'contractor_core.group_contractor_manager',
    'contractor_core.group_contractor_md',
)

ALLOWED_CONTRACTOR_ROOT_XMLIDS = (
    'contractor_core.menu_contractor_root',
    'contractor_reports.menu_contractor_reports_root',
)

BLOCKED_STANDARD_ROOT_XMLIDS = (
    'mail.menu_root_discuss',
    'calendar.mail_menu_calendar',
    'contacts.menu_contacts',
    'crm.crm_menu_root',
    'sale.sale_menu_root',
    'spreadsheet_dashboard.spreadsheet_dashboard_menu_root',
    'account.menu_finance',
    'accountant.menu_accounting',
    'project.menu_main_pm',
    'purchase.menu_purchase_root',
    'stock.menu_stock_root',
    'hr.menu_hr_root',
    'hr_attendance.menu_hr_attendance_root',
    'hr_holidays.menu_hr_holidays_root',
    'utm.menu_link_tracker_root',
    'base.menu_management',
    'base.menu_tests',
)


class ResUsers(models.Model):
    _inherit = 'res.users'

    contractor_menu_mode = fields.Selection(
        [
            ('role_default', 'Role-Based Restricted (Default)'),
            ('contractor_only', 'Contractor ERP & Analytics Only'),
            ('unrestricted', 'Unrestricted (Show Standard Apps)'),
        ],
        string='Menu Access Level',
        default='role_default',
        required=True,
        help='Controls top-level app and menu visibility for this user. '
             'In Role-Based Restricted mode, contractor users only see Contractor ERP & Contractor Analytics '
             'scoped to their assigned role, while unwanted standard menus are hidden.',
    )
    contractor_hidden_menu_ids = fields.Many2many(
        'ir.ui.menu',
        'contractor_user_hidden_menu_rel',
        'user_id',
        'menu_id',
        string='Additional Hidden Menus',
        help='Optional per-user menu items (and their submenus) to hide in addition to role restrictions.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        self.env.transaction.invalidate_ormcache()
        return users

    def write(self, vals):
        res = super().write(vals)
        if any(k in vals for k in ('contractor_menu_mode', 'contractor_hidden_menu_ids', 'group_ids', 'company_id', 'active')):
            self.env.transaction.invalidate_ormcache()
        return res

    @api.model
    def _cleanup_contractor_privilege_groups(self):
        """Remove redundant/duplicate Contractor ERP group assignments left by Settings sync."""
        priv = self.env.ref('contractor_core.res_groups_privilege_contractor_erp', raise_if_not_found=False)
        group_md = self.env.ref('contractor_core.group_contractor_md', raise_if_not_found=False)
        group_sales = self.env.ref('contractor_core.group_contractor_sales_admin', raise_if_not_found=False)
        if not priv or not group_md:
            return True

        # 1. Clean Sales Administrator if Managing Director was accidentally added alongside Sales Admin
        if group_sales:
            sales_admins = self.sudo().with_context(active_test=False).search([
                ('login', '=', 'Sales Administrator'),
            ])
            for user in sales_admins:
                if group_sales in user.group_ids and group_md in user.group_ids:
                    user.write({'group_ids': [(3, group_md.id)]})

        # 2. Ensure built-in admin/root accounts hold only group_contractor_md in this privilege
        priv_lower_groups = priv.group_ids - group_md
        for xmlid in ('base.user_admin', 'base.user_root'):
            admin_user = self.env.ref(xmlid, raise_if_not_found=False)
            if admin_user and priv_lower_groups:
                unlink_cmds = [(3, g.id) for g in (admin_user.group_ids & priv_lower_groups)]
                if unlink_cmds:
                    admin_user.sudo().write({'group_ids': unlink_cmds})

        self.env.transaction.invalidate_ormcache()
        return True


class IrUiMenu(models.Model):
    _inherit = 'ir.ui.menu'

    def _resolve_menu_ids_by_xmlid(self, xmlids):
        ids = set()
        for xmlid in xmlids:
            menu = self.env.ref(xmlid, raise_if_not_found=False)
            if menu and menu._name == 'ir.ui.menu':
                ids.add(menu.id)
        return ids

    def _user_has_contractor_role(self, user):
        return any(user.has_group(xmlid) for xmlid in CONTRACTOR_ROLE_GROUP_XMLIDS)

    def _get_contractor_restriction_policy(self):
        """Return tuple (contractor_only, hide_standard_unwanted, hidden_menu_ids)."""
        user = self.env.user
        if self.env.su or user._is_superuser() or user.share:
            return False, False, set()

        mode = user.contractor_menu_mode or 'role_default'
        if mode == 'unrestricted':
            return False, False, set()

        is_system = user.has_group('base.group_system')
        hidden_ids = set(user.sudo().contractor_hidden_menu_ids.ids)

        if is_system and mode != 'contractor_only':
            return False, False, set()

        if mode == 'contractor_only':
            return True, True, hidden_ids

        company = user.company_id or self.env.company
        if not company.restrict_menus_by_role:
            return False, False, hidden_ids

        is_contractor = self._user_has_contractor_role(user)
        contractor_only = bool(is_contractor and company.restrict_contractor_top_menus_only)
        return contractor_only, True, hidden_ids

    def _prune_contractor_menu_ids(self, visible_ids):
        contractor_only, hide_standard, explicit_hidden = self._get_contractor_restriction_policy()
        if not (contractor_only or hide_standard or explicit_hidden):
            return visible_ids

        visible_list = list(visible_ids)
        if not visible_list:
            return visible_ids

        menu_rows = self.sudo().search_read(
            [('id', 'in', visible_list)],
            ['parent_id', 'action'],
        )
        parent_map = {}
        has_action = set()
        for row in menu_rows:
            mid = row['id']
            parent_val = row.get('parent_id')
            parent_map[mid] = parent_val[0] if parent_val else False
            if row.get('action'):
                has_action.add(mid)

        disallowed_seeds = set(explicit_hidden)
        if contractor_only:
            allowed_roots = self._resolve_menu_ids_by_xmlid(ALLOWED_CONTRACTOR_ROOT_XMLIDS)
            for mid, pid in parent_map.items():
                if not pid and mid not in allowed_roots:
                    disallowed_seeds.add(mid)
        elif hide_standard:
            disallowed_seeds |= self._resolve_menu_ids_by_xmlid(BLOCKED_STANDARD_ROOT_XMLIDS)

        if not disallowed_seeds:
            return visible_ids

        kept = set()
        ancestor_cache = {}

        def is_blocked(mid):
            if mid in ancestor_cache:
                return ancestor_cache[mid]
            seen = []
            cur = mid
            blocked = False
            while cur:
                if cur in disallowed_seeds:
                    blocked = True
                    break
                if cur in ancestor_cache:
                    blocked = ancestor_cache[cur]
                    break
                seen.append(cur)
                cur = parent_map.get(cur)
            for node in seen:
                ancestor_cache[node] = blocked
            return blocked

        for mid in parent_map:
            if not is_blocked(mid):
                kept.add(mid)

        # Iteratively prune parent categories left with zero visible children and no action
        while True:
            referenced_parents = {parent_map[mid] for mid in kept if parent_map.get(mid)}
            next_kept = {mid for mid in kept if mid in has_action or mid in referenced_parents}
            if len(next_kept) == len(kept):
                break
            kept = next_kept

        return frozenset(kept)

    def _visible_menu_ids(self, debug=False):
        visible_ids = super()._visible_menu_ids(debug=debug)
        return self._prune_contractor_menu_ids(visible_ids)

    @api.model
    def get_user_roots(self):
        roots = super().get_user_roots()
        if not roots:
            return roots
        allowed_ids = self._visible_menu_ids(debug=self.env.context.get('debug', False))
        return roots.filtered(lambda m: m.id in allowed_ids)
