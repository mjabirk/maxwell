# -*- coding: utf-8 -*-
from odoo import models, fields


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    worker_category = fields.Selection([
        ('permanent', 'Permanent / In-House'),
        ('temporary', 'Temporary / Daily Wage'),
        ('manpower_supply', 'Manpower Supply Agency'),
    ], string='Worker Category', default='permanent', tracking=True,
       help='Worker classification governing site scheduling, attendance recording, and payroll integration.')

