import hashlib
import json

from urllib.parse import unquote, urlparse

from odoo import api, fields, models
from odoo.exceptions import UserError

from .bridge_client import (
    get_bridge_pile_inspection,
    submit_bridge_table7,
    submit_bridge_table13,
)

SIGNATURE_ROLE_LABELS = {
    "inspector": "检查",
    "recorder": "记录",
    "reviewer": "复核",
    "construction": "施工单位",
    "supervisor": "监理工程师",
}


class BridgePile(models.Model):
    _name = "bridge.pile"
    _description = "桥梁桩基"
    _order = "id desc"

    name = fields.Char("桩号", required=True)
    project_id = fields.Many2one("coordos.project", string="项目")
    project_node_id = fields.Char("项目节点ID")
    spu_id = fields.Many2one("coordos.spu", string="关联SPU")

    design_tip_elevation = fields.Float("设计桩尖高程(m)")
    design_diameter = fields.Float("设计桩径(m)")
    design_depth = fields.Float("应钻深度(m)")

    hole_status = fields.Selection(
        [("draft", "未检查"), ("qualified", "合格"), ("nonconformance", "不合格")],
        string="成孔状态",
        default="draft",
    )

    table7_trip_id = fields.Char("桥施7行程ID", readonly=True, copy=False)
    table7_verdict = fields.Char("桥施7判定", readonly=True, copy=False)
    table7_pdf_ref = fields.Char("桥施7 PDF", readonly=True, copy=False)
    table7_result_json = fields.Text("桥施7结果(JSON)", readonly=True, copy=False)
    table7_checked_at = fields.Datetime("桥施7回写时间", readonly=True, copy=False)

    table13_trip_id = fields.Char("桥施13行程ID", readonly=True, copy=False)
    table13_verdict = fields.Char("桥施13判定", readonly=True, copy=False)
    table13_pdf_ref = fields.Char("桥施13 PDF", readonly=True, copy=False)
    table13_result_json = fields.Text("桥施13结果(JSON)", readonly=True, copy=False)
    table13_checked_at = fields.Datetime("桥施13回写时间", readonly=True, copy=False)

    latest_inspection_id = fields.Many2one("bridge.pile.hole.inspection", string="最近桥施7", readonly=True)
    inspection_ids = fields.One2many("bridge.pile.hole.inspection", "pile_id", string="桥施7记录")
    final_inspection_ids = fields.One2many("bridge.pile.final.inspection", "pile_id", string="桥施13记录")
    nonconformance_count = fields.Integer("不合格单数量", compute="_compute_nc_count")

    def _compute_nc_count(self):
        nc_model = self.env["bridge.pile.nonconformance"]
        for rec in self:
            rec.nonconformance_count = nc_model.search_count([("pile_id", "=", rec.id)])

    def _core_url(self):
        return self.env["coordos.api.mixin"]._configured_core_base()

    def _resolve_pile_ref(self):
        self.ensure_one()
        if self.spu_id and self.spu_id.x_core_usi:
            return self.spu_id.x_core_usi
        if self.project_node_id:
            return self.project_node_id
        raise UserError("缺少桩位引用：请先绑定SPU（含 CoordOS USI）或填写项目节点ID。")

    @api.model
    def _split_refs(self, text):
        values = []
        for item in (text or "").replace("\n", ",").split(","):
            part = (item or "").strip()
            if part:
                values.append(part)
        return values

    @api.model
    def _usi_autofill_rules(self):
        defaults = {
            "org": {
                "cn.cncc": {
                    "construction_unit": "中交第二航务工程局有限公司",
                    "supervision_unit": "中交公路规划设计院有限公司",
                }
            },
            "project_alias": {},
        }
        raw = self.env["ir.config_parameter"].sudo().get_param("coordos.usi_autofill_rules_json")
        if not raw:
            return defaults
        try:
            loaded = json.loads(raw)
        except Exception:
            return defaults
        if not isinstance(loaded, dict):
            return defaults
        result = dict(defaults)
        if isinstance(loaded.get("org"), dict):
            result["org"] = loaded["org"]
        if isinstance(loaded.get("project_alias"), dict):
            result["project_alias"] = loaded["project_alias"]
        return result

    @api.model
    def _parse_usi_path(self, usi_path):
        text = str(usi_path or "").strip()
        if not text:
            return "", []
        if "://" not in text:
            text = f"v://{text.lstrip('/')}"
        parsed = urlparse(text)
        org = (parsed.netloc or "").strip().lower()
        segments = [unquote(part).strip() for part in (parsed.path or "").split("/") if part.strip()]
        return org, segments

    @api.model
    def _match_org_rule(self, org, org_rules):
        if not org or not isinstance(org_rules, dict):
            return {}
        org_l = org.lower()
        if isinstance(org_rules.get(org_l), dict):
            return org_rules[org_l]
        best_key = ""
        for key, value in org_rules.items():
            if not isinstance(value, dict):
                continue
            key_l = str(key or "").strip().lower()
            if not key_l:
                continue
            if org_l == key_l or org_l.startswith(f"{key_l}.") or org_l.startswith(key_l):
                if len(key_l) > len(best_key):
                    best_key = key_l
        return org_rules.get(best_key, {}) if best_key else {}

    def _resolve_usi_path(self):
        self.ensure_one()
        if self.spu_id and self.spu_id.x_core_usi and str(self.spu_id.x_core_usi).startswith("v://"):
            return self.spu_id.x_core_usi
        if self.project_node_id and str(self.project_node_id).startswith("v://"):
            return self.project_node_id

        project = self.project_id
        bridge_name = (project.default_bridge_name or "3号大桥").strip() if project else "3号大桥"
        pier_name = (project.default_pier_name or "P1").strip() if project else "P1"
        pile_name = (self.name or "未命名桩").strip()

        if project and project.project_usi and str(project.project_usi).startswith("v://"):
            base = str(project.project_usi).rstrip("/")
            return f"{base}/{bridge_name}/{pier_name}/{pile_name}"

        org = ""
        if project and project.org_code:
            org = str(project.org_code).strip().lower()
        if not org:
            org = self.env["coordos.api.mixin"].current_org_code()
        project_seg = ""
        if project:
            project_seg = (project.code or project.name or "project").strip()
        if not project_seg:
            project_seg = "project"
        return f"v://{org}/{project_seg}/{bridge_name}/{pier_name}/{pile_name}"

    def _resolve_from_usi(self, usi_path):
        self.ensure_one()
        org, segments = self._parse_usi_path(usi_path)
        rules = self._usi_autofill_rules()
        org_rule = self._match_org_rule(org, rules.get("org") or {})
        project_alias = rules.get("project_alias") or {}

        project = self.project_id
        engineering_name = (project.name or "").strip() if project else ""
        construction_unit = (project.construction_unit or "").strip() if project else ""
        supervision_unit = (project.supervision_unit or "").strip() if project else ""
        contract_no = (project.contract_no or "").strip() if project else ""
        bridge_name = (project.default_bridge_name or "").strip() if project else ""
        pier_name = (project.default_pier_name or "").strip() if project else ""
        pile_name = (self.name or "").strip()

        if isinstance(org_rule, dict):
            construction_unit = org_rule.get("construction_unit") or construction_unit
            supervision_unit = org_rule.get("supervision_unit") or supervision_unit
            contract_no = org_rule.get("contract_no") or contract_no
            engineering_name = org_rule.get("engineering_name") or engineering_name

        if segments:
            low0 = segments[0].lower()
            if low0 in {"bridge", "qiao", "桥"}:
                if len(segments) > 1:
                    bridge_name = segments[1]
                if len(segments) > 2:
                    pier_name = segments[2]
                if len(segments) > 3:
                    pile_name = segments[3]
            elif len(segments) >= 4:
                project_seg = segments[0]
                engineering_name = engineering_name or project_alias.get(project_seg) or project_seg
                bridge_name = bridge_name or segments[1]
                pier_name = pier_name or segments[2]
                pile_name = segments[3] or pile_name
            elif len(segments) >= 3:
                bridge_name = bridge_name or segments[0]
                pier_name = pier_name or segments[1]
                pile_name = segments[2] or pile_name
            elif len(segments) >= 2:
                bridge_name = bridge_name or segments[0]
                pile_name = segments[1] or pile_name
            elif len(segments) == 1 and not engineering_name:
                engineering_name = project_alias.get(segments[0]) or segments[0]

        if not engineering_name:
            engineering_name = bridge_name or "未知工程"
        if not construction_unit:
            construction_unit = self.env.company.name or "未知施工单位"
        if not supervision_unit:
            supervision_unit = "未知监理单位"
        if not contract_no:
            contract_no = "待填合同号"

        pile_position = pile_name
        if pier_name and pile_name:
            pile_position = f"{pier_name}墩 {pile_name}号桩"

        return {
            "usi_path": usi_path or "",
            "engineering_name": engineering_name,
            "construction_unit": construction_unit,
            "supervision_unit": supervision_unit,
            "contract_no": contract_no,
            "bridge_name": bridge_name or "",
            "pier_name": pier_name or "",
            "pile_position": pile_position or "",
        }

    def _usi_autofill_values(self):
        self.ensure_one()
        usi_path = self._resolve_usi_path()
        return self._resolve_from_usi(usi_path)

    def _absolute_pdf_url(self, ref):
        if not ref:
            return ""
        ref = str(ref).strip()
        if ref.startswith("http://") or ref.startswith("https://"):
            return ref
        base = self._core_url().rstrip("/")
        if ref.startswith("/"):
            return f"{base}{ref}"
        return f"{base}/{ref}"

    def _extract_data_block(self, payload):
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload if isinstance(payload, dict) else {}

    def _extract_inspection_payload(self, payload):
        data = self._extract_data_block(payload)
        if isinstance(data, dict) and isinstance(data.get("inspection"), dict):
            return data["inspection"]
        if isinstance(payload, dict) and isinstance(payload.get("inspection"), dict):
            return payload["inspection"]
        return data if isinstance(data, dict) else {}

    def _update_from_bridge_result(self, table_no, result):
        self.ensure_one()
        data = self._extract_inspection_payload(result)
        trip_id = data.get("trip_id") or data.get("tripId")
        verdict = data.get("verdict") or data.get("status")
        pdf_ref = data.get("pdf_ref")

        if str(table_no) == "7":
            vals = {
                "table7_trip_id": trip_id,
                "table7_verdict": verdict,
                "table7_pdf_ref": pdf_ref,
                "table7_result_json": json.dumps(result, ensure_ascii=False),
                "table7_checked_at": fields.Datetime.now(),
            }
            if verdict == "qualified":
                vals["hole_status"] = "qualified"
                if self.spu_id:
                    self.spu_id.with_context(skip_core_sync=True).write({"x_status": "qualified"})
            elif verdict:
                vals["hole_status"] = "nonconformance"
                if self.spu_id:
                    self.spu_id.with_context(skip_core_sync=True).write({"x_status": "nonconformance"})
            self.write(vals)
        else:
            self.write(
                {
                    "table13_trip_id": trip_id,
                    "table13_verdict": verdict,
                    "table13_pdf_ref": pdf_ref,
                    "table13_result_json": json.dumps(result, ensure_ascii=False),
                    "table13_checked_at": fields.Datetime.now(),
                }
            )

    def action_open_hole_inspection_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "成孔检查（桥施7表）",
            "res_model": "pile.hole.inspection.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"active_model": "bridge.pile", "active_id": self.id},
        }

    def action_open_table_upload_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "上传桥施表智能解析",
            "res_model": "bridge.table.upload.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_pile_id": self.id,
                "default_pile_ref": self._resolve_pile_ref(),
            },
        }

    def action_open_final_inspection_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "成桩检查（桥施13表）",
            "res_model": "pile.final.inspection.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"active_model": "bridge.pile", "active_id": self.id},
        }

    def run_final_inspection(self):
        self.ensure_one()
        latest_verdict = self.table7_verdict or (self.latest_inspection_id.core_verdict if self.latest_inspection_id else "")
        if latest_verdict != "qualified":
            raise UserError("桥施13表前置不满足：桥施7表必须为 qualified。")
        return self.action_open_final_inspection_wizard()

    def action_refresh_table7_result(self):
        for rec in self:
            if not rec.table7_trip_id:
                raise UserError("缺少桥施7行程ID。")
            result = get_bridge_pile_inspection(rec.table7_trip_id, core_url=rec._core_url())
            rec._update_from_bridge_result("7", result)
        return True

    def action_refresh_table13_result(self):
        for rec in self:
            if not rec.table13_trip_id:
                raise UserError("缺少桥施13行程ID。")
            result = get_bridge_pile_inspection(rec.table13_trip_id, core_url=rec._core_url())
            rec._update_from_bridge_result("13", result)
        return True

    def action_open_table7_pdf(self):
        self.ensure_one()
        record = self.latest_inspection_id
        if not record and self.table7_trip_id:
            record = self.env["bridge.pile.hole.inspection"].search(
                [("pile_id", "=", self.id), ("core_trip_id", "=", self.table7_trip_id)], order="id desc", limit=1
            )
        if record:
            return self.env.ref("coordos_odoo.action_report_bridge_table7").report_action(record)
        if self.table7_pdf_ref:
            return {"type": "ir.actions.act_url", "url": self._absolute_pdf_url(self.table7_pdf_ref), "target": "new"}
        raise UserError("桥施7 PDF 引用为空。")

    def action_open_table13_pdf(self):
        self.ensure_one()
        record = self.env["bridge.pile.final.inspection"].search(
            [("pile_id", "=", self.id), ("core_trip_id", "=", self.table13_trip_id)], order="id desc", limit=1
        )
        if not record:
            record = self.env["bridge.pile.final.inspection"].search([("pile_id", "=", self.id)], order="id desc", limit=1)
        if record:
            return self.env.ref("coordos_odoo.action_report_bridge_table13").report_action(record)
        if self.table13_pdf_ref:
            return {"type": "ir.actions.act_url", "url": self._absolute_pdf_url(self.table13_pdf_ref), "target": "new"}
        raise UserError("桥施13 PDF 引用为空。")

    def action_view_nonconformance(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "不合格记录",
            "res_model": "bridge.pile.nonconformance",
            "view_mode": "tree,form",
            "domain": [("pile_id", "=", self.id)],
            "target": "current",
        }


class BridgePileHoleInspection(models.Model):
    _name = "bridge.pile.hole.inspection"
    _description = "桥施7提交记录"
    _order = "id desc"

    name = fields.Char("记录编号", default="新建", copy=False)
    pile_id = fields.Many2one("bridge.pile", string="桩基", required=True, ondelete="cascade", index=True)
    pile_ref = fields.Char("桩位引用", required=True)
    usi_path = fields.Char("USI路径", copy=False)
    usi_full_path = fields.Char("完整USI路径", copy=False)
    engineering_name = fields.Char("工程名称", copy=False)
    construction_unit = fields.Char("施工单位", copy=False)
    supervision_unit = fields.Char("监理单位", copy=False)
    contract_no = fields.Char("合同号", copy=False)
    page_info = fields.Char("页码信息", copy=False, default="第 1 页  共 1 页")
    bridge_name = fields.Char("桥梁名称", copy=False)
    pier_name = fields.Char("墩位", copy=False)
    pile_position = fields.Char("桩位描述", copy=False)
    check_date = fields.Date("检查日期", default=fields.Date.today, required=True)

    design_depth = fields.Float("应钻深度(m)")
    actual_drilled_depth = fields.Float("实钻深度(m)")
    design_diameter = fields.Float("设计桩径(m)")
    actual_diameter = fields.Float("成孔直径(m)")
    inclination_permille = fields.Float("倾斜度(‰)")
    hole_detector_passed = fields.Boolean("检孔器通过", default=True)

    evidence_refs = fields.Text("佐证材料")
    inspector_signature_ref = fields.Char("检查签名")
    recorder_signature_ref = fields.Char("记录签名")
    reviewer_signature_ref = fields.Char("复核签名")
    construction_signature_ref = fields.Char("施工单位签名")
    supervisor_signature_ref = fields.Char("监理签名")
    signature_audit_json = fields.Text("签名审计(JSON)", readonly=True, copy=False)

    core_trip_id = fields.Char("行程ID", readonly=True, copy=False)
    core_verdict = fields.Char("判定结果", readonly=True, copy=False)
    core_pdf_ref = fields.Char("PDF引用", readonly=True, copy=False)
    core_submit_result_json = fields.Text("提交结果(JSON)", readonly=True, copy=False)
    core_query_result_json = fields.Text("查询结果(JSON)", readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        prepared_vals = []
        for vals in vals_list:
            item = dict(vals)
            pile = self.env["bridge.pile"].browse(item.get("pile_id")) if item.get("pile_id") else self.env["bridge.pile"]
            if pile:
                auto_vals = pile._usi_autofill_values()
                item.setdefault("usi_path", auto_vals.get("usi_path"))
                item.setdefault("usi_full_path", auto_vals.get("usi_path"))
                item.setdefault("engineering_name", auto_vals.get("engineering_name"))
                item.setdefault("construction_unit", auto_vals.get("construction_unit"))
                item.setdefault("supervision_unit", auto_vals.get("supervision_unit"))
                item.setdefault("contract_no", auto_vals.get("contract_no"))
                item.setdefault("page_info", "第 1 页  共 1 页")
                item.setdefault("bridge_name", auto_vals.get("bridge_name"))
                item.setdefault("pier_name", auto_vals.get("pier_name"))
                item.setdefault("pile_position", auto_vals.get("pile_position"))
                if not item.get("pile_ref"):
                    item["pile_ref"] = pile._resolve_pile_ref()
            prepared_vals.append(item)

        records = super().create(prepared_vals)
        for rec in records:
            if rec.name == "新建":
                rec.name = f"T7-{fields.Date.today().strftime('%Y%m%d')}-{rec.id:04d}"
            rec.pile_id.latest_inspection_id = rec.id
            rec._ensure_usi_autofill()
            rec._refresh_signature_audit()
        return records

    def write(self, vals):
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

    def _ensure_usi_autofill(self):
        for rec in self:
            if not rec.pile_id:
                continue
            auto_vals = rec.pile_id._usi_autofill_values()
            patch = {}
            for field_name in [
                "usi_path",
                "usi_full_path",
                "engineering_name",
                "construction_unit",
                "supervision_unit",
                "contract_no",
                "bridge_name",
                "pier_name",
                "pile_position",
            ]:
                if not getattr(rec, field_name):
                    if field_name == "usi_full_path":
                        patch[field_name] = auto_vals.get("usi_path")
                    else:
                        patch[field_name] = auto_vals.get(field_name)
            if not rec.page_info:
                patch["page_info"] = "第 1 页  共 1 页"
            if patch:
                rec.write(patch)
        return True

    def _evidence_refs_as_list(self):
        self.ensure_one()
        raw = (self.evidence_refs or "").strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return self.pile_id._split_refs(raw)

    def _signature_refs_dict(self):
        self.ensure_one()
        return {
            "inspector": (self.inspector_signature_ref or "").strip(),
            "recorder": (self.recorder_signature_ref or "").strip(),
            "reviewer": (self.reviewer_signature_ref or "").strip(),
            "construction": (self.construction_signature_ref or "").strip(),
            "supervisor": (self.supervisor_signature_ref or "").strip(),
        }

    def _build_signature_audit_json(self, signatures=None):
        self.ensure_one()
        source = signatures or self._signature_refs_dict()
        now = fields.Datetime.now()
        now_text = fields.Datetime.to_string(now)
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

    def action_submit_to_core(self):
        nc_model = self.env["bridge.pile.nonconformance"]
        for rec in self:
            if not rec.pile_id:
                raise UserError("请先选择桩基。")
            rec._ensure_usi_autofill()
            pile_ref = rec.pile_ref or rec.pile_id._resolve_pile_ref()
            signatures = rec._signature_refs_dict()
            core_signatures = {
                "inspector": signatures.get("inspector", ""),
                "reviewer": signatures.get("reviewer", ""),
            }
            payload = {
                "pile_ref": pile_ref,
                "measurements": {
                    "design_depth": rec.design_depth,
                    "actual_drilled_depth": rec.actual_drilled_depth,
                    "design_diameter": rec.design_diameter,
                    "actual_diameter": rec.actual_diameter,
                    "inclination_permille": rec.inclination_permille,
                    "hole_detector_passed": bool(rec.hole_detector_passed),
                },
                "evidence": rec._evidence_refs_as_list(),
                "signatures": core_signatures,
            }
            result = submit_bridge_table7(payload, core_url=rec.pile_id._core_url())
            data = rec.pile_id._extract_inspection_payload(result)
            rec.write(
                {
                    "pile_ref": pile_ref,
                    "signature_audit_json": rec._build_signature_audit_json(signatures),
                    "core_trip_id": data.get("trip_id") or data.get("tripId"),
                    "core_verdict": data.get("verdict") or data.get("status"),
                    "core_pdf_ref": data.get("pdf_ref"),
                    "core_submit_result_json": json.dumps(result, ensure_ascii=False),
                }
            )
            rec.pile_id.latest_inspection_id = rec.id
            rec.pile_id._update_from_bridge_result("7", result)

            verdict = (data.get("verdict") or data.get("status") or "").lower()
            if verdict and verdict != "qualified":
                existed = nc_model.search_count([("table_type", "=", "7"), ("inspection7_id", "=", rec.id)])
                if not existed:
                    nc_model.create(
                        {
                            "name": f"NC7-{rec.pile_id.name}-{fields.Date.today()}",
                            "pile_id": rec.pile_id.id,
                            "table_type": "7",
                            "inspection7_id": rec.id,
                            "reason": json.dumps(result, ensure_ascii=False),
                        }
                    )
        return True

    def action_print_bridge_table7(self):
        self.ensure_one()
        self._ensure_usi_autofill()
        return self.env.ref("coordos_odoo.action_report_bridge_table7").report_action(self)

    def action_open_core_pdf(self):
        self.ensure_one()
        if not self.core_pdf_ref:
            raise UserError("当前记录还没有Core PDF引用，请先提交。")
        return {"type": "ir.actions.act_url", "url": self.pile_id._absolute_pdf_url(self.core_pdf_ref), "target": "new"}

    def action_refresh_from_core(self):
        for rec in self:
            if not rec.core_trip_id:
                raise UserError("缺少行程ID。")
            result = get_bridge_pile_inspection(rec.core_trip_id, core_url=rec.pile_id._core_url())
            data = rec.pile_id._extract_inspection_payload(result)
            rec.write(
                {
                    "core_query_result_json": json.dumps(result, ensure_ascii=False),
                    "core_verdict": data.get("verdict") or data.get("status") or rec.core_verdict,
                    "core_pdf_ref": data.get("pdf_ref") or rec.core_pdf_ref,
                }
            )
            rec.pile_id._update_from_bridge_result("7", result)
        return True


class BridgePileFinalInspection(models.Model):
    _name = "bridge.pile.final.inspection"
    _description = "桥施13提交记录"
    _order = "id desc"

    name = fields.Char("记录编号", default="新建", copy=False)
    pile_id = fields.Many2one("bridge.pile", string="桩基", required=True, ondelete="cascade", index=True)
    pile_ref = fields.Char("桩位引用", required=True)
    check_date = fields.Date("检查日期", default=fields.Date.today, required=True)

    design_top_elevation = fields.Float("设计桩顶高程(m)")
    actual_top_elevation = fields.Float("实测桩顶高程(m)")
    design_x = fields.Float("设计X坐标")
    actual_x = fields.Float("实测X坐标")
    design_y = fields.Float("设计Y坐标")
    actual_y = fields.Float("实测Y坐标")
    design_strength = fields.Float("设计强度")
    actual_strength = fields.Float("实测强度")
    integrity_class = fields.Char("完整性等级")

    evidence_refs = fields.Text("佐证材料")
    inspector_signature_ref = fields.Char("检查签名")
    recorder_signature_ref = fields.Char("记录签名")
    reviewer_signature_ref = fields.Char("复核签名")
    construction_signature_ref = fields.Char("施工单位签名")
    supervisor_signature_ref = fields.Char("监理签名")
    signature_audit_json = fields.Text("签名审计(JSON)", readonly=True, copy=False)

    core_trip_id = fields.Char("行程ID", readonly=True, copy=False)
    core_verdict = fields.Char("判定结果", readonly=True, copy=False)
    core_pdf_ref = fields.Char("PDF引用", readonly=True, copy=False)
    core_submit_result_json = fields.Text("提交结果(JSON)", readonly=True, copy=False)
    core_query_result_json = fields.Text("查询结果(JSON)", readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.name == "新建":
                rec.name = f"T13-{fields.Date.today().strftime('%Y%m%d')}-{rec.id:04d}"
            rec._refresh_signature_audit()
        return records

    def write(self, vals):
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

    def _signature_refs_dict(self):
        self.ensure_one()
        return {
            "inspector": (self.inspector_signature_ref or "").strip(),
            "recorder": (self.recorder_signature_ref or "").strip(),
            "reviewer": (self.reviewer_signature_ref or "").strip(),
            "construction": (self.construction_signature_ref or "").strip(),
            "supervisor": (self.supervisor_signature_ref or "").strip(),
        }

    def _build_signature_audit_json(self, signatures=None):
        self.ensure_one()
        source = signatures or self._signature_refs_dict()
        now = fields.Datetime.now()
        now_text = fields.Datetime.to_string(now)
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

    def action_refresh_from_core(self):
        for rec in self:
            if not rec.core_trip_id:
                raise UserError("缺少行程ID。")
            result = get_bridge_pile_inspection(rec.core_trip_id, core_url=rec.pile_id._core_url())
            data = rec.pile_id._extract_inspection_payload(result)
            rec.write(
                {
                    "core_query_result_json": json.dumps(result, ensure_ascii=False),
                    "core_verdict": data.get("verdict") or data.get("status") or rec.core_verdict,
                    "core_pdf_ref": data.get("pdf_ref") or rec.core_pdf_ref,
                }
            )
            rec.pile_id._update_from_bridge_result("13", result)
        return True


class BridgePileNonconformance(models.Model):
    _name = "bridge.pile.nonconformance"
    _description = "桩基不合格记录"
    _order = "id desc"

    name = fields.Char("编号", required=True)
    pile_id = fields.Many2one("bridge.pile", string="桩基", required=True, ondelete="cascade")
    table_type = fields.Selection([("7", "桥施7"), ("13", "桥施13")], string="来源表", required=True)
    inspection7_id = fields.Many2one("bridge.pile.hole.inspection", string="桥施7记录")
    inspection13_id = fields.Many2one("bridge.pile.final.inspection", string="桥施13记录")
    reason = fields.Text("不合格原因", required=True)
    corrective_action = fields.Text("整改措施")
    status = fields.Selection([("open", "待整改"), ("closed", "已关闭")], string="状态", default="open", required=True)
    owner_id = fields.Many2one("res.users", string="负责人", default=lambda self: self.env.user)
    closed_at = fields.Datetime("关闭时间", readonly=True)

    def action_mark_closed(self):
        self.write({"status": "closed", "closed_at": fields.Datetime.now()})
        return True


class PileHoleInspectionWizard(models.TransientModel):
    _name = "pile.hole.inspection.wizard"
    _description = "桥施7表提交向导"

    pile_id = fields.Many2one("bridge.pile", string="桩基", required=True)
    pile_ref = fields.Char("桩位引用", readonly=True)
    check_date = fields.Date("检查日期", default=fields.Date.today, required=True)
    usi_path = fields.Char("USI路径", compute="_compute_usi_autofill", readonly=True)
    usi_full_path = fields.Char("完整USI路径", compute="_compute_usi_autofill", readonly=True)
    engineering_name = fields.Char("工程名称", compute="_compute_usi_autofill", readonly=True)
    construction_unit = fields.Char("施工单位", compute="_compute_usi_autofill", readonly=True)
    supervision_unit = fields.Char("监理单位", compute="_compute_usi_autofill", readonly=True)
    contract_no = fields.Char("合同号", compute="_compute_usi_autofill", readonly=True)
    page_info = fields.Char("页码信息", compute="_compute_usi_autofill", readonly=True)
    bridge_name = fields.Char("桥梁名称", compute="_compute_usi_autofill", readonly=True)
    pier_name = fields.Char("墩位", compute="_compute_usi_autofill", readonly=True)
    pile_position = fields.Char("桩位描述", compute="_compute_usi_autofill", readonly=True)

    design_depth = fields.Float("应钻深度(m)", required=True)
    actual_drilled_depth = fields.Float("实钻深度(m)", required=True)
    design_diameter = fields.Float("设计桩径(m)", required=True)
    actual_diameter = fields.Float("成孔直径(m)", required=True)
    inclination_permille = fields.Float("倾斜度(‰)", required=True)
    hole_detector_passed = fields.Boolean("检孔器通过", default=True)

    evidence_refs = fields.Text("佐证材料（逗号分隔）", default="photo://hole-1,doc://measure-1")
    site_photo = fields.Binary("现场照片")
    site_photo_filename = fields.Char("现场照片文件名")
    inspector_signature_draw = fields.Binary("检查手写签名")
    recorder_signature_draw = fields.Binary("记录手写签名")
    reviewer_signature_draw = fields.Binary("复核手写签名")
    construction_signature_draw = fields.Binary("施工单位手写签名")
    supervisor_signature_draw = fields.Binary("监理手写签名")
    inspector_signature_ref = fields.Char("检查签名", default="sig:inspector")
    recorder_signature_ref = fields.Char("记录签名", default="sig:recorder")
    reviewer_signature_ref = fields.Char("复核签名", default="sig:reviewer")
    construction_signature_ref = fields.Char("施工单位签名", default="sig:construction")
    supervisor_signature_ref = fields.Char("监理签名", default="sig:supervisor")

    @staticmethod
    def _guess_mimetype(file_name, default_type="application/octet-stream"):
        lower = (file_name or "").lower()
        if lower.endswith(".png"):
            return "image/png"
        if lower.endswith(".jpg") or lower.endswith(".jpeg"):
            return "image/jpeg"
        if lower.endswith(".pdf"):
            return "application/pdf"
        return default_type

    def _create_mobile_attachment(self, datas, name, mimetype=None):
        self.ensure_one()
        if not datas:
            return None
        return self.env["ir.attachment"].create(
            {
                "name": name or f"mobile_upload_{fields.Datetime.now()}",
                "datas": datas,
                "res_model": "bridge.pile",
                "res_id": self.pile_id.id,
                "mimetype": mimetype or self._guess_mimetype(name),
            }
        )

    @api.depends("pile_id")
    def _compute_usi_autofill(self):
        for wizard in self:
            if not wizard.pile_id:
                wizard.usi_path = ""
                wizard.usi_full_path = ""
                wizard.engineering_name = ""
                wizard.construction_unit = ""
                wizard.supervision_unit = ""
                wizard.contract_no = ""
                wizard.page_info = "第 1 页  共 1 页   桥施7表"
                wizard.bridge_name = ""
                wizard.pier_name = ""
                wizard.pile_position = ""
                continue
            auto_vals = wizard.pile_id._usi_autofill_values()
            wizard.usi_path = auto_vals.get("usi_path") or ""
            wizard.usi_full_path = wizard.usi_path
            wizard.engineering_name = auto_vals.get("engineering_name") or ""
            wizard.construction_unit = auto_vals.get("construction_unit") or ""
            wizard.supervision_unit = auto_vals.get("supervision_unit") or ""
            wizard.contract_no = auto_vals.get("contract_no") or ""
            wizard.page_info = "第 1 页  共 1 页   桥施7表"
            wizard.bridge_name = auto_vals.get("bridge_name") or ""
            wizard.pier_name = auto_vals.get("pier_name") or ""
            wizard.pile_position = auto_vals.get("pile_position") or ""

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        active_model = self.env.context.get("active_model")
        active_id = self.env.context.get("active_id")
        if active_model == "bridge.pile" and active_id:
            pile = self.env["bridge.pile"].browse(active_id)
            vals.update(
                {
                    "pile_id": pile.id,
                    "pile_ref": pile._resolve_pile_ref(),
                    "design_depth": pile.design_depth,
                    "design_diameter": pile.design_diameter,
                    "actual_diameter": pile.design_diameter,
                    "actual_drilled_depth": pile.design_depth,
                }
            )
        return vals

    def action_submit(self):
        self.ensure_one()
        pile_ref = self.pile_ref or self.pile_id._resolve_pile_ref()
        evidence_list = self.pile_id._split_refs(self.evidence_refs)
        signatures = {
            "inspector": self.inspector_signature_ref or "",
            "recorder": self.recorder_signature_ref or "",
            "reviewer": self.reviewer_signature_ref or "",
            "construction": self.construction_signature_ref or "",
            "supervisor": self.supervisor_signature_ref or "",
        }

        if self.site_photo:
            photo_att = self._create_mobile_attachment(
                self.site_photo,
                self.site_photo_filename or f"site_photo_{fields.Datetime.now()}.jpg",
                self._guess_mimetype(self.site_photo_filename or "site_photo.jpg", "image/jpeg"),
            )
            if photo_att:
                evidence_list.append(f"attachment://{photo_att.id}")

        if self.inspector_signature_draw:
            ins_att = self._create_mobile_attachment(self.inspector_signature_draw, "inspector_sign.png", "image/png")
            if ins_att:
                ins_ref = f"attachment://{ins_att.id}"
                evidence_list.append(ins_ref)
                signatures["inspector"] = ins_ref

        if self.recorder_signature_draw:
            rec_att = self._create_mobile_attachment(self.recorder_signature_draw, "recorder_sign.png", "image/png")
            if rec_att:
                rec_ref = f"attachment://{rec_att.id}"
                evidence_list.append(rec_ref)
                signatures["recorder"] = rec_ref

        if self.reviewer_signature_draw:
            rev_att = self._create_mobile_attachment(self.reviewer_signature_draw, "reviewer_sign.png", "image/png")
            if rev_att:
                rev_ref = f"attachment://{rev_att.id}"
                evidence_list.append(rev_ref)
                signatures["reviewer"] = rev_ref

        if self.construction_signature_draw:
            con_att = self._create_mobile_attachment(self.construction_signature_draw, "construction_sign.png", "image/png")
            if con_att:
                con_ref = f"attachment://{con_att.id}"
                evidence_list.append(con_ref)
                signatures["construction"] = con_ref

        if self.supervisor_signature_draw:
            sup_att = self._create_mobile_attachment(self.supervisor_signature_draw, "supervisor_sign.png", "image/png")
            if sup_att:
                sup_ref = f"attachment://{sup_att.id}"
                evidence_list.append(sup_ref)
                signatures["supervisor"] = sup_ref

        payload = {
            "pile_ref": pile_ref,
            "measurements": {
                "design_depth": self.design_depth,
                "actual_drilled_depth": self.actual_drilled_depth,
                "design_diameter": self.design_diameter,
                "actual_diameter": self.actual_diameter,
                "inclination_permille": self.inclination_permille,
                "hole_detector_passed": bool(self.hole_detector_passed),
            },
            "evidence": evidence_list,
            "signatures": {
                "inspector": signatures["inspector"],
                "reviewer": signatures["reviewer"],
            },
        }
        result = submit_bridge_table7(payload, core_url=self.pile_id._core_url())
        data = self.pile_id._extract_inspection_payload(result)

        record = self.env["bridge.pile.hole.inspection"].create(
            {
                "pile_id": self.pile_id.id,
                "pile_ref": pile_ref,
                "usi_path": self.usi_path,
                "usi_full_path": self.usi_full_path or self.usi_path,
                "engineering_name": self.engineering_name,
                "construction_unit": self.construction_unit,
                "supervision_unit": self.supervision_unit,
                "contract_no": self.contract_no,
                "page_info": self.page_info or "第 1 页  共 1 页",
                "bridge_name": self.bridge_name,
                "pier_name": self.pier_name,
                "pile_position": self.pile_position,
                "check_date": self.check_date,
                "design_depth": self.design_depth,
                "actual_drilled_depth": self.actual_drilled_depth,
                "design_diameter": self.design_diameter,
                "actual_diameter": self.actual_diameter,
                "inclination_permille": self.inclination_permille,
                "hole_detector_passed": self.hole_detector_passed,
                "evidence_refs": json.dumps(payload["evidence"], ensure_ascii=False),
                "inspector_signature_ref": signatures["inspector"],
                "recorder_signature_ref": signatures["recorder"],
                "reviewer_signature_ref": signatures["reviewer"],
                "construction_signature_ref": signatures["construction"],
                "supervisor_signature_ref": signatures["supervisor"],
                "signature_audit_json": json.dumps(
                    [
                        {
                            "role": role,
                            "label": SIGNATURE_ROLE_LABELS[role],
                            "signer": value,
                            "ref": value,
                            "timestamp": fields.Datetime.to_string(fields.Datetime.now()),
                            "hash": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                        }
                        for role, value in signatures.items()
                        if value
                    ],
                    ensure_ascii=False,
                ),
                "core_trip_id": data.get("trip_id") or data.get("tripId"),
                "core_verdict": data.get("verdict") or data.get("status"),
                "core_pdf_ref": data.get("pdf_ref"),
                "core_submit_result_json": json.dumps(result, ensure_ascii=False),
            }
        )
        self.pile_id.latest_inspection_id = record.id
        self.pile_id._update_from_bridge_result("7", result)

        verdict = (data.get("verdict") or "").lower()
        if verdict and verdict != "qualified":
            self.env["bridge.pile.nonconformance"].create(
                {
                    "name": f"NC7-{self.pile_id.name}-{fields.Date.today()}",
                    "pile_id": self.pile_id.id,
                    "table_type": "7",
                    "inspection7_id": record.id,
                    "reason": json.dumps(result, ensure_ascii=False),
                }
            )

        return {
            "type": "ir.actions.act_window",
            "name": "桩基详情",
            "res_model": "bridge.pile",
            "view_mode": "form",
            "res_id": self.pile_id.id,
            "target": "current",
        }


class PileFinalInspectionWizard(models.TransientModel):
    _name = "pile.final.inspection.wizard"
    _description = "桥施13表提交向导"

    pile_id = fields.Many2one("bridge.pile", string="桩基", required=True)
    pile_ref = fields.Char("桩位引用", readonly=True)
    check_date = fields.Date("检查日期", default=fields.Date.today, required=True)

    design_top_elevation = fields.Float("设计桩顶高程(m)", required=True)
    actual_top_elevation = fields.Float("实测桩顶高程(m)", required=True)
    design_x = fields.Float("设计X坐标", required=True)
    actual_x = fields.Float("实测X坐标", required=True)
    design_y = fields.Float("设计Y坐标", required=True)
    actual_y = fields.Float("实测Y坐标", required=True)
    design_strength = fields.Float("设计强度", required=True)
    actual_strength = fields.Float("实测强度", required=True)
    integrity_class = fields.Char("完整性等级", required=True, default="I")

    evidence_refs = fields.Text("佐证材料（逗号分隔）", default="photo://final-1,report://strength-1")
    inspector_signature_draw = fields.Binary("检查手写签名")
    recorder_signature_draw = fields.Binary("记录手写签名")
    reviewer_signature_draw = fields.Binary("复核手写签名")
    construction_signature_draw = fields.Binary("施工单位手写签名")
    supervisor_signature_draw = fields.Binary("监理手写签名")
    inspector_signature_ref = fields.Char("检查签名", default="sig:inspector")
    recorder_signature_ref = fields.Char("记录签名", default="sig:recorder")
    reviewer_signature_ref = fields.Char("复核签名", default="sig:reviewer")
    construction_signature_ref = fields.Char("施工单位签名", default="sig:construction")
    supervisor_signature_ref = fields.Char("监理签名", default="sig:supervisor")

    @staticmethod
    def _guess_mimetype(file_name, default_type="application/octet-stream"):
        lower = (file_name or "").lower()
        if lower.endswith(".png"):
            return "image/png"
        if lower.endswith(".jpg") or lower.endswith(".jpeg"):
            return "image/jpeg"
        if lower.endswith(".pdf"):
            return "application/pdf"
        return default_type

    def _create_mobile_attachment(self, datas, name, mimetype=None):
        self.ensure_one()
        if not datas:
            return None
        return self.env["ir.attachment"].create(
            {
                "name": name or f"mobile_upload_{fields.Datetime.now()}",
                "datas": datas,
                "res_model": "bridge.pile",
                "res_id": self.pile_id.id,
                "mimetype": mimetype or self._guess_mimetype(name),
            }
        )

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        active_model = self.env.context.get("active_model")
        active_id = self.env.context.get("active_id")
        if active_model == "bridge.pile" and active_id:
            pile = self.env["bridge.pile"].browse(active_id)
            vals.update({"pile_id": pile.id, "pile_ref": pile._resolve_pile_ref()})
        return vals

    def action_submit(self):
        self.ensure_one()
        if (self.pile_id.table7_verdict or "") != "qualified":
            raise UserError("桥施13提交前置失败：桥施7判定必须为“合格”。")

        pile_ref = self.pile_ref or self.pile_id._resolve_pile_ref()
        evidence_list = self.pile_id._split_refs(self.evidence_refs)
        signatures = {
            "inspector": self.inspector_signature_ref or "",
            "recorder": self.recorder_signature_ref or "",
            "reviewer": self.reviewer_signature_ref or "",
            "construction": self.construction_signature_ref or "",
            "supervisor": self.supervisor_signature_ref or "",
        }
        draw_map = [
            ("inspector", self.inspector_signature_draw, "inspector_sign.png"),
            ("recorder", self.recorder_signature_draw, "recorder_sign.png"),
            ("reviewer", self.reviewer_signature_draw, "reviewer_sign.png"),
            ("construction", self.construction_signature_draw, "construction_sign.png"),
            ("supervisor", self.supervisor_signature_draw, "supervisor_sign.png"),
        ]
        for role, draw_data, file_name in draw_map:
            if not draw_data:
                continue
            att = self._create_mobile_attachment(draw_data, file_name, "image/png")
            if not att:
                continue
            ref = f"attachment://{att.id}"
            evidence_list.append(ref)
            signatures[role] = ref

        payload = {
            "pile_ref": pile_ref,
            "measurements": {
                "design_top_elevation": self.design_top_elevation,
                "actual_top_elevation": self.actual_top_elevation,
                "design_x": self.design_x,
                "actual_x": self.actual_x,
                "design_y": self.design_y,
                "actual_y": self.actual_y,
                "design_strength": self.design_strength,
                "actual_strength": self.actual_strength,
                "integrity_class": self.integrity_class,
            },
            "evidence": evidence_list,
            "signatures": {
                "inspector": signatures["inspector"],
                "supervisor": signatures["supervisor"],
            },
        }
        result = submit_bridge_table13(payload, core_url=self.pile_id._core_url())
        data = self.pile_id._extract_inspection_payload(result)

        record = self.env["bridge.pile.final.inspection"].create(
            {
                "pile_id": self.pile_id.id,
                "pile_ref": pile_ref,
                "check_date": self.check_date,
                "design_top_elevation": self.design_top_elevation,
                "actual_top_elevation": self.actual_top_elevation,
                "design_x": self.design_x,
                "actual_x": self.actual_x,
                "design_y": self.design_y,
                "actual_y": self.actual_y,
                "design_strength": self.design_strength,
                "actual_strength": self.actual_strength,
                "integrity_class": self.integrity_class,
                "evidence_refs": json.dumps(payload["evidence"], ensure_ascii=False),
                "inspector_signature_ref": signatures["inspector"],
                "recorder_signature_ref": signatures["recorder"],
                "reviewer_signature_ref": signatures["reviewer"],
                "construction_signature_ref": signatures["construction"],
                "supervisor_signature_ref": signatures["supervisor"],
                "signature_audit_json": json.dumps(
                    [
                        {
                            "role": role,
                            "label": SIGNATURE_ROLE_LABELS[role],
                            "signer": value,
                            "ref": value,
                            "timestamp": fields.Datetime.to_string(fields.Datetime.now()),
                            "hash": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                        }
                        for role, value in signatures.items()
                        if value
                    ],
                    ensure_ascii=False,
                ),
                "core_trip_id": data.get("trip_id") or data.get("tripId"),
                "core_verdict": data.get("verdict") or data.get("status"),
                "core_pdf_ref": data.get("pdf_ref"),
                "core_submit_result_json": json.dumps(result, ensure_ascii=False),
            }
        )

        self.pile_id._update_from_bridge_result("13", result)

        verdict = (data.get("verdict") or "").lower()
        if verdict and verdict != "qualified":
            self.env["bridge.pile.nonconformance"].create(
                {
                    "name": f"NC13-{self.pile_id.name}-{fields.Date.today()}",
                    "pile_id": self.pile_id.id,
                    "table_type": "13",
                    "inspection13_id": record.id,
                    "reason": json.dumps(result, ensure_ascii=False),
                }
            )

        return {
            "type": "ir.actions.act_window",
            "name": "桩基详情",
            "res_model": "bridge.pile",
            "view_mode": "form",
            "res_id": self.pile_id.id,
            "target": "current",
        }
