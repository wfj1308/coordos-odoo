import hashlib
import json

from odoo import api, fields, models
from odoo.exceptions import UserError


SIGNATURE_ROLE_LABELS = {
    "inspector": "检查",
    "recorder": "记录",
    "reviewer": "复核",
    "construction": "施工单位",
    "supervisor": "监理工程师",
}


class CoordosQualityTableRecord(models.Model):
    _name = "coordos.quality.table.record"
    _description = "通用质检表数字记录"
    _order = "id desc"

    name = fields.Char("记录编号", default="新建", copy=False)
    table_title = fields.Char("表单标题")
    table_type_code = fields.Char("表单类型编码", default="other")
    quality_template_id = fields.Many2one("coordos.quality.table.template", string="命中模板")
    quality_template_code = fields.Char("模板编码")
    quality_template_version = fields.Integer("模板版本")
    source_file_name = fields.Char("源文件名")
    source_attachment_id = fields.Many2one("ir.attachment", string="源文件", readonly=True, copy=False)
    trip_shadow_id = fields.Many2one("coordos.trip.shadow", string="关联Trip", readonly=True, copy=False)

    pile_id = fields.Many2one("bridge.pile", string="关联桩基")
    pile_ref = fields.Char("桩位引用")
    check_date = fields.Date("检查日期", default=fields.Date.today)

    usi_path = fields.Char("USI路径")
    usi_full_path = fields.Char("完整USI路径")
    engineering_name = fields.Char("工程名称")
    construction_unit = fields.Char("施工单位")
    supervision_unit = fields.Char("监理单位")
    contract_no = fields.Char("合同号")
    bridge_name = fields.Char("桥梁名称")
    pier_name = fields.Char("墩位")
    pile_position = fields.Char("桩位描述")

    ocr_text = fields.Text("OCR文本")
    parsed_data_json = fields.Text("解析结果(JSON)")
    editable_data_json = fields.Text("可编辑数据(JSON)")
    evidence_refs = fields.Text("证据列表")

    inspector_signature_ref = fields.Char("检查签名")
    recorder_signature_ref = fields.Char("记录签名")
    reviewer_signature_ref = fields.Char("复核签名")
    construction_signature_ref = fields.Char("施工单位签名")
    supervisor_signature_ref = fields.Char("监理签名")
    signature_audit_json = fields.Text("签名审计(JSON)", readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.name == "新建":
                rec.name = f"QT-{fields.Date.today().strftime('%Y%m%d')}-{rec.id:04d}"
            if rec.quality_template_id and not rec.quality_template_code:
                rec.quality_template_code = rec.quality_template_id.code
            if not rec.editable_data_json and rec.parsed_data_json:
                rec.editable_data_json = rec.parsed_data_json
            rec._refresh_signature_audit()
        return records

    def write(self, vals):
        if "quality_template_id" in vals and vals.get("quality_template_id") and "quality_template_code" not in vals:
            template = self.env["coordos.quality.table.template"].browse(vals.get("quality_template_id"))
            if template.exists():
                vals["quality_template_code"] = template.code
        res = super().write(vals)
        if {
            "inspector_signature_ref",
            "recorder_signature_ref",
            "reviewer_signature_ref",
            "construction_signature_ref",
            "supervisor_signature_ref",
        }.intersection(vals):
            self._refresh_signature_audit()
        return res

    @staticmethod
    def _split_refs(raw):
        values = []
        for item in (raw or "").replace("\n", ",").split(","):
            part = (item or "").strip()
            if part:
                values.append(part)
        return values

    def _signature_refs_dict(self):
        self.ensure_one()
        return {
            "inspector": (self.inspector_signature_ref or "").strip(),
            "recorder": (self.recorder_signature_ref or "").strip(),
            "reviewer": (self.reviewer_signature_ref or "").strip(),
            "construction": (self.construction_signature_ref or "").strip(),
            "supervisor": (self.supervisor_signature_ref or "").strip(),
        }

    def _build_signature_audit_json(self):
        self.ensure_one()
        source = self._signature_refs_dict()
        now_text = fields.Datetime.to_string(fields.Datetime.now())
        entries = []
        for role, label in SIGNATURE_ROLE_LABELS.items():
            value = str(source.get(role) or "").strip()
            if not value:
                continue
            entries.append(
                {
                    "role": role,
                    "label": label,
                    "signer": value,
                    "ref": value,
                    "timestamp": now_text,
                    "hash": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                }
            )
        return json.dumps(entries, ensure_ascii=False)

    def _refresh_signature_audit(self):
        for rec in self:
            rec.signature_audit_json = rec._build_signature_audit_json()
        return True

    def _signature_data_uri(self, signature_ref):
        self.ensure_one()
        ref = str(signature_ref or "").strip()
        if not ref:
            return ""
        if ref.startswith("data:image/"):
            return ref
        if not ref.startswith("attachment://"):
            return ""
        token = ref.split("://", 1)[1].split("?", 1)[0].strip()
        if not token.isdigit():
            return ""
        attachment = self.env["ir.attachment"].sudo().browse(int(token))
        if not attachment.exists() or not attachment.datas:
            return ""
        datas = attachment.datas.decode("utf-8") if isinstance(attachment.datas, bytes) else attachment.datas
        mimetype = attachment.mimetype or "image/png"
        return f"data:{mimetype};base64,{datas}"

    def _display_items_for_report(self):
        self.ensure_one()
        raw = (self.editable_data_json or self.parsed_data_json or "").strip()
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except Exception:
            return [{"key": "raw_text", "value": raw}]

        if isinstance(payload, dict):
            rows = []
            for key, value in payload.items():
                if isinstance(value, (dict, list)):
                    text = json.dumps(value, ensure_ascii=False)
                else:
                    text = "" if value is None else str(value)
                rows.append({"key": str(key), "value": text})
            return rows
        if isinstance(payload, list):
            rows = []
            for idx, value in enumerate(payload, start=1):
                if isinstance(value, (dict, list)):
                    text = json.dumps(value, ensure_ascii=False)
                else:
                    text = "" if value is None else str(value)
                rows.append({"key": f"item_{idx}", "value": text})
            return rows
        return [{"key": "value", "value": str(payload)}]

    def action_print_quality_table(self):
        self.ensure_one()
        return self.env.ref("coordos_odoo.action_report_quality_table_generic").report_action(self)

    def action_open_source_file(self):
        self.ensure_one()
        if not self.source_attachment_id:
            raise UserError("未找到源文件附件。")
        return {"type": "ir.actions.act_url", "url": f"/web/content/{self.source_attachment_id.id}?download=false", "target": "new"}
