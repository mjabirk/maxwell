import base64
import re
from urllib.parse import parse_qs, urlparse

from lxml import html as lxml_html
from markupsafe import Markup

from odoo import api, models
from odoo.tools import is_html_empty
from odoo.tools.image import image_data_uri
from odoo.tools.misc import file_open

_WEB_IMAGE_ID_RE = re.compile(r'^/web/image/(\d+)(?:-[^/?#]+)?(?:/[^?#]*)?$')
_WEB_IMAGE_MODEL_RE = re.compile(r'^/web/image/([^/]+)/(\d+)/([^/?#]+)(?:/[^?#]*)?$')


class ResCompany(models.Model):
    _inherit = 'res.company'

    @api.model
    def _get_maxwell_static_banner_data_uri(self, banner_type='header'):
        filename = 'maxwell_header.jpg' if banner_type == 'header' else 'maxwell_footer.jpg'
        with file_open(f'maxwell_custom/static/src/img/{filename}', 'rb') as f:
            b64_data = base64.b64encode(f.read()).decode('ascii')
        return f'data:image/jpeg;base64,{b64_data}'

    def _get_maxwell_header_logo_data_uri(self):
        self.ensure_one()
        if self.logo:
            return image_data_uri(self.logo)
        return self._get_maxwell_static_banner_data_uri('header')

    def _resolve_local_web_image_data_uri(self, src):
        if not src or src.startswith('data:'):
            return src
        parsed = urlparse(src)
        path = parsed.path or ''
        if not path.startswith('/web/image'):
            return src

        raw_bytes = None
        mimetype = None

        match_att = _WEB_IMAGE_ID_RE.match(path)
        if match_att:
            att = self.env['ir.attachment'].sudo().browse(int(match_att.group(1)))
            if att.exists() and att.raw:
                raw_bytes = att.raw
                mimetype = att.mimetype
        else:
            model_name = None
            rec_id = None
            field_name = None
            match_model = _WEB_IMAGE_MODEL_RE.match(path)
            if match_model:
                model_name, rec_id_str, field_name = match_model.groups()
                rec_id = int(rec_id_str)
            else:
                qs = parse_qs(parsed.query or '')
                if qs.get('model') and qs.get('id'):
                    model_name = qs['model'][0]
                    try:
                        rec_id = int(qs['id'][0])
                    except ValueError:
                        rec_id = None
                    field_name = (qs.get('field') or ['datas'])[0]

            if model_name and rec_id and model_name in self.env:
                rec = self.env[model_name].sudo().browse(rec_id)
                if rec.exists() and field_name in rec._fields:
                    val = rec[field_name]
                    if model_name == 'ir.attachment' and field_name in ('datas', 'raw') and rec.raw:
                        raw_bytes = rec.raw
                        mimetype = rec.mimetype
                    elif val:
                        raw_bytes = val.content if hasattr(val, 'content') else base64.b64decode(val)

        if raw_bytes:
            if mimetype and mimetype.startswith('image/') and mimetype != 'image/webp':
                b64_data = base64.b64encode(raw_bytes).decode('ascii')
                return f'data:{mimetype};base64,{b64_data}'
            return image_data_uri(raw_bytes)
        return src

    def _get_maxwell_footer_html(self):
        self.ensure_one()
        footer_html = self.report_footer
        if is_html_empty(footer_html):
            uri = self._get_maxwell_static_banner_data_uri('footer')
            return Markup(
                f'<img src="{uri}" class="maxwell-footer-img" alt="Maxwell Footer"/>'
            )

        wrapper = lxml_html.fragment_fromstring(
            f'<div class="maxwell-footer-content">{footer_html}</div>',
            create_parent=False,
        )
        for img in wrapper.xpath('.//img[@src]'):
            img.set('src', self._resolve_local_web_image_data_uri(img.get('src')))
            img.attrib.pop('width', None)
            img.attrib.pop('height', None)
        rendered = ''.join(
            (wrapper.text or '')
            + ''.join(lxml_html.tostring(child, encoding='unicode') for child in wrapper)
        )
        return Markup(rendered)


class IrActionsReport(models.Model):
    _inherit = 'ir.actions.report'

    @api.model
    def _bind_maxwell_optional_paperformats(self):
        paperformat = self.env.ref('maxwell_custom.paperformat_maxwell', raise_if_not_found=False)
        if not paperformat:
            return
        optional_xmlids = (
            'sale_pdf_quote_builder.action_report_saleorder_raw',
            'account_edi_ubl_cii.action_report_account_invoices_generated_by_odoo',
        )
        for xmlid in optional_xmlids:
            report = self.env.ref(xmlid, raise_if_not_found=False)
            if report and report.paperformat_id != paperformat:
                report.paperformat_id = paperformat
