import json
import logging
import os

from odoo import api, fields, models
from odoo.exceptions import UserError

from .bridge_client import get_smu_kind_template_by_item, get_trip_detail, launch_trip


_logger = logging.getLogger(__name__)


TRIP_TEMPLATE_OPTIONS = [
    ("pile_construction", "桩基施工（pile_construction）"),
    ("earthwork_excavation", "土方开挖（earthwork_excavation）"),
    ("rebar_binding", "钢筋绑扎（rebar_binding）"),
    ("bridge_table_upload", "桥表上传（bridge_table_upload）"),
]

TRIP_TEMPLATE_ITEM_CODE = {
    "pile_construction": "404-1",
    "earthwork_excavation": "602-1",
    "rebar_binding": "403-1-2",
}

TRIP_TEMPLATE_SCHEMA = {
    "pile_construction": {
        "fields": [
            {"key": "diameter_mm", "label": "桩径(mm)", "type": "integer", "default": 1500},
            {"key": "depth_m", "label": "桩深(m)", "type": "number", "default": 32},
            {"key": "concrete_grade", "label": "混凝土标号", "type": "string", "default": "C40"},
        ]
    },
    "earthwork_excavation": {
        "fields": [
            {"key": "cut_volume_m3", "label": "开挖方量(m3)", "type": "number", "default": 1000},
            {"key": "section", "label": "施工区段", "type": "string", "default": "K100+200-K100+800"},
            {"key": "soil_class", "label": "土质类别", "type": "string", "default": "III"},
        ]
    },
    "rebar_binding": {
        "fields": [
            {"key": "bar_type", "label": "钢筋类型", "type": "string", "default": "HRB400"},
            {"key": "weight_t", "label": "重量(t)", "type": "number", "default": 8.2},
            {"key": "spec", "label": "规格", "type": "string", "default": "Φ25"},
        ]
    },
    "bridge_table_upload": {
        "fields": [
            {"key": "table_no", "label": "报表编号", "type": "string", "default": "table7"},
            {"key": "source", "label": "来源", "type": "string", "default": "odoo_upload"},
        ]
    },
}


class LaunchTripWizard(models.TransientModel):
    _name = "launch.trip.wizard"
    _description = "启动行程"

    trip_shadow_id = fields.Many2one("coordos.trip.shadow", string="关联行程", readonly=True)
    project_node_ref = fields.Many2one("coordos.project.node", string="项目节点")
    project_node = fields.Char("项目节点ID", readonly=True)
    trip_template = fields.Selection(selection="_selection_trip_template", string="行程模板", required=True)
    template_item_code = fields.Char("模板条目编码", readonly=True)
    template_id = fields.Char("模板ID", readonly=True)
    template_yaml_path = fields.Char("模板YAML路径", readonly=True)
    input_line_ids = fields.One2many("launch.trip.input.line", "wizard_id", string="模板输入项")
    input_json = fields.Text("输入JSON", default="{}", required=True)
    operator_id = fields.Many2one("coordos.spu", string="操作人（可选）")
    result_trip_id = fields.Char("行程ID", readonly=True)
    result_status = fields.Char("状态", readonly=True)
    result_dispatch_plan_id = fields.Char("调度计划ID", readonly=True)
    energy_consumed = fields.Integer("能耗值", default=80, required=True)

    @api.model
    def _selection_trip_template(self):
        configs = self.env["coordos.trip.template.config"].search([("active", "=", True)], order="id")
        if configs:
            options = [(rec.code, rec.display_name) for rec in configs]
        else:
            options = list(TRIP_TEMPLATE_OPTIONS)

        existing_codes = {code for code, _ in options}
        ctx = self.env.context or {}
        candidates = [ctx.get("default_trip_template")]
        if ctx.get("active_model") == "coordos.trip.shadow" and ctx.get("active_id"):
            trip = self.env["coordos.trip.shadow"].browse(ctx.get("active_id"))
            if trip.exists():
                candidates.append(trip.trip_template)

        for code in candidates:
            if code and code not in existing_codes:
                options.append((code, f"{code}（{code}）"))
                existing_codes.add(code)
        return options

    def _template_config(self, code=None):
        template_code = code or self.trip_template
        if not template_code:
            return self.env["coordos.trip.template.config"]
        return self.env["coordos.trip.template.config"].search([("code", "=", template_code), ("active", "=", True)], limit=1)

    def _schema_for_template(self, code):
        cfg = self._template_config(code)
        if cfg:
            fields_def = cfg.schema_fields()
            if fields_def:
                return {"fields": fields_def}
        return TRIP_TEMPLATE_SCHEMA.get(code, TRIP_TEMPLATE_SCHEMA["pile_construction"])

    @api.onchange("project_node_ref")
    def _onchange_project_node_ref(self):
        self.project_node = self.project_node_ref.node_id if self.project_node_ref else False

    @api.onchange("trip_template")
    def _onchange_trip_template(self):
        if not self.trip_template:
            return
        remote_fields = self._load_template_descriptor()
        self._regenerate_input_lines(sync_json=True, remote_fields=remote_fields)

    @api.onchange("input_line_ids")
    def _onchange_input_line_ids(self):
        if not self.input_line_ids:
            return
        payload = self._input_lines_to_payload()
        self.input_json = json.dumps(payload, ensure_ascii=False)

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        active_model = self.env.context.get("active_model")
        active_id = self.env.context.get("active_id")
        project_node_hint = self.env.context.get("default_project_node")

        if active_model == "coordos.trip.shadow" and active_id:
            trip = self.env["coordos.trip.shadow"].browse(active_id)
            vals["trip_shadow_id"] = trip.id
            vals.setdefault("project_node", trip.project_node or "")
            vals.setdefault("trip_template", trip.trip_template or "pile_construction")
            vals.setdefault("input_json", trip.input_json or vals.get("input_json") or "{}")
            project_node_hint = trip.project_node or project_node_hint

        if active_model == "coordos.spu" and active_id:
            vals["operator_id"] = active_id

        if project_node_hint and not vals.get("project_node_ref"):
            node = self.env["coordos.project.node"].search([("node_id", "=", project_node_hint)], limit=1)
            if node:
                vals["project_node_ref"] = node.id
                vals["project_node"] = node.node_id

        default_tpl = self.env["coordos.trip.template.config"].search([("active", "=", True)], order="id", limit=1)
        template_code = vals.get("trip_template") or (default_tpl.code if default_tpl else "pile_construction")
        valid_templates = {code for code, _name in self._selection_trip_template()}
        if template_code not in valid_templates:
            template_code = (default_tpl.code if default_tpl else "pile_construction")
        vals.setdefault("trip_template", template_code)

        cfg = self._template_config(template_code)
        cfg_default_payload = cfg.default_input_payload() if cfg else {}
        schema = self._schema_for_template(template_code)
        cmd = [(5, 0, 0)]
        payload = {}
        for idx, item in enumerate(schema.get("fields", []), start=1):
            key = item.get("key")
            default = cfg_default_payload.get(key, item.get("default"))
            payload[key] = default
            cmd.append(
                (
                    0,
                    0,
                    {
                        "sequence": idx,
                        "key": key,
                        "label": item.get("label") or key,
                        "value_type": item.get("type") or "string",
                        "value_text": json.dumps(default, ensure_ascii=False)
                        if isinstance(default, (dict, list, bool))
                        else str(default),
                    },
                )
            )
        vals.setdefault("input_line_ids", cmd)
        if not vals.get("input_json") or vals.get("input_json") == "{}":
            vals["input_json"] = json.dumps(payload, ensure_ascii=False)

        item_code = cfg.item_code if cfg and cfg.item_code else TRIP_TEMPLATE_ITEM_CODE.get(template_code)
        if item_code:
            vals.setdefault("template_item_code", item_code)
        if cfg:
            vals.setdefault("template_id", cfg.template_id or False)

        return vals

    def _core_base_url(self):
        return (
            self.env["ir.config_parameter"].sudo().get_param("coordos.core_base_url")
            or os.getenv("COORDOS_CORE_URL")
            or "http://coordos-core:8080"
        )

    def _default_operator_spu_id(self):
        return (
            self.env["ir.config_parameter"].sudo().get_param("coordos.default_operator_spu_id")
            or os.getenv("COORDOS_DEFAULT_OPERATOR_SPU_ID")
        )

    def _open_detail_action(self, trip):
        return {
            "type": "ir.actions.act_window",
            "name": "行程详情",
            "res_model": "coordos.trip.shadow",
            "res_id": trip.id,
            "view_mode": "form",
            "view_id": self.env.ref("coordos_odoo.view_trip_detail_min_form").id,
            "target": "current",
        }

    def _json_type_to_value_type(self, raw_type):
        mapping = {
            "integer": "integer",
            "number": "number",
            "boolean": "boolean",
            "object": "json",
            "array": "json",
            "json": "json",
            "string": "string",
            "text": "string",
        }
        return mapping.get((raw_type or "string").lower(), "string")

    def _normalize_schema_fields(self, source):
        if not isinstance(source, dict):
            return []
        # list-style schema
        list_candidates = [
            source.get("fields"),
            source.get("input_fields"),
            source.get("inputs"),
            source.get("parameters"),
        ]
        for candidate in list_candidates:
            if isinstance(candidate, list):
                normalized = []
                for item in candidate:
                    if not isinstance(item, dict):
                        continue
                    key = item.get("key") or item.get("name") or item.get("id")
                    if not key:
                        continue
                    normalized.append(
                        {
                            "key": key,
                            "label": item.get("label") or item.get("title") or key,
                            "type": self._json_type_to_value_type(item.get("type")),
                            "default": item.get("default"),
                        }
                    )
                if normalized:
                    return normalized

        # JSON-schema style
        schema = source.get("schema") if isinstance(source.get("schema"), dict) else source
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if isinstance(properties, dict):
            normalized = []
            for key, prop in properties.items():
                if not isinstance(prop, dict):
                    continue
                normalized.append(
                    {
                        "key": key,
                        "label": prop.get("title") or key,
                        "type": self._json_type_to_value_type(prop.get("type")),
                        "default": prop.get("default"),
                    }
                )
            if normalized:
                return normalized
        return []

    def _load_template_descriptor(self):
        self.ensure_one()
        cfg = self._template_config(self.trip_template)
        item_code = (cfg.item_code if cfg and cfg.item_code else None) or TRIP_TEMPLATE_ITEM_CODE.get(self.trip_template)
        self.template_item_code = item_code or False
        self.template_id = cfg.template_id if cfg and cfg.template_id else False
        self.template_yaml_path = False
        if not item_code:
            return []

        try:
            result = get_smu_kind_template_by_item(item_code, core_url=self._core_base_url())
        except RuntimeError as exc:
            _logger.warning("读取模板元数据失败 item_code=%s error=%s", item_code, exc)
            return []

        template = result.get("template") or (result.get("data") or {}).get("template") or {}
        if not self.template_id:
            self.template_id = template.get("template_id")
        self.template_yaml_path = template.get("path")
        blocks = [
            result,
            result.get("data"),
            template,
            (result.get("data") or {}).get("template"),
        ]
        for block in blocks:
            fields_def = self._normalize_schema_fields(block)
            if fields_def:
                return fields_def
        return []

    def _regenerate_input_lines(self, sync_json=False, remote_fields=None):
        self.ensure_one()
        cfg = self._template_config(self.trip_template)
        cfg_default_payload = cfg.default_input_payload() if cfg else {}
        schema = self._schema_for_template(self.trip_template)
        if remote_fields:
            schema = {"fields": remote_fields}
        lines = [(5, 0, 0)]
        for idx, item in enumerate(schema.get("fields", []), start=1):
            key = item.get("key")
            default = cfg_default_payload.get(key, item.get("default"))
            value_type = self._json_type_to_value_type(item.get("type"))
            lines.append(
                (
                    0,
                    0,
                    {
                        "sequence": idx,
                        "key": key,
                        "label": item.get("label") or key,
                        "value_type": value_type,
                        "value_text": json.dumps(default, ensure_ascii=False)
                        if isinstance(default, (dict, list, bool))
                        else ("" if default is None else str(default)),
                    },
                )
            )
        self.input_line_ids = lines
        if sync_json:
            self.input_json = json.dumps(self._input_lines_to_payload(), ensure_ascii=False)

    def _line_to_python(self, line):
        raw = (line.value_text or "").strip()
        value_type = line.value_type or "string"

        if value_type == "integer":
            if raw == "":
                return 0
            try:
                return int(float(raw))
            except ValueError as exc:
                raise UserError(f"字段[{line.label}] 需要整数，当前值: {raw}") from exc

        if value_type == "number":
            if raw == "":
                return 0
            try:
                return float(raw)
            except ValueError as exc:
                raise UserError(f"字段[{line.label}] 需要数字，当前值: {raw}") from exc

        if value_type == "boolean":
            lowered = raw.lower()
            if lowered in {"1", "true", "yes", "y", "on"}:
                return True
            if lowered in {"0", "false", "no", "n", "off", ""}:
                return False
            raise UserError(f"字段[{line.label}] 需要布尔值(true/false)，当前值: {raw}")

        if value_type == "json":
            if raw == "":
                return {}
            try:
                return json.loads(raw)
            except ValueError as exc:
                raise UserError(f"字段[{line.label}] 需要JSON，当前值非法: {raw}") from exc

        return raw

    def _input_lines_to_payload(self):
        self.ensure_one()
        payload = {}
        for line in self.input_line_ids.sorted(key=lambda l: (l.sequence, l.id)):
            if not line.key:
                continue
            payload[line.key] = self._line_to_python(line)
        return payload

    def _resolve_input_payload(self):
        self.ensure_one()
        raw = (self.input_json or "").strip()
        if raw:
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise UserError(f"输入JSON格式错误: {exc}") from exc
            if not isinstance(payload, dict):
                raise UserError("输入JSON必须是JSON对象")
            if payload:
                return payload

        if self.input_line_ids:
            return self._input_lines_to_payload()
        return {}

    def action_launch(self):
        self.ensure_one()
        project_node_id = self.project_node_ref.node_id if self.project_node_ref else self.project_node
        if not project_node_id:
            raise UserError("项目节点必选")

        input_payload = self._resolve_input_payload()
        self.input_json = json.dumps(input_payload, ensure_ascii=False)

        api_mixin = self.env["coordos.api.mixin"]
        org_code = api_mixin.current_org_code()
        policy = self.env["coordos.namespace.policy"].match_policy(project_node_id)
        if policy:
            policy.ensure_user_allowed(self.env.user)

        operator_spu_id = self.operator_id.x_core_usi if self.operator_id else False
        if not operator_spu_id and policy and policy.default_operator_spu_id:
            operator_spu_id = policy.default_operator_spu_id
        if not operator_spu_id:
            operator_spu_id = self._default_operator_spu_id()
        core_url = self._core_base_url()
        if "api.codepeg.com" in (core_url or "").lower() and not operator_spu_id:
            raise UserError("当前 Core 要求操作人，请选择操作人或配置默认 operatorSpuId")

        trip_name = (
            policy.build_trip_name(self.trip_template, org_code)
            if policy
            else f"{self.trip_template}_{org_code}_{fields.Date.today().strftime('%Y%m%d')}"
        )
        context_payload = dict(input_payload)
        context_payload.setdefault("org_code", org_code)

        payload = {
            "trip_name": trip_name,
            "executor_spu": operator_spu_id,
            "resources_utxo": [],
            "project_node_id": project_node_id,
            "context": context_payload,
            "energy_consumed": int(self.energy_consumed or 80),
        }

        if policy and policy.require_approval:
            reg = self.env["coordos.trip.registration"].create(
                {
                    "name": trip_name,
                    "state": "pending",
                    "org_code": org_code,
                    "project_node_id": project_node_id,
                    "trip_template": self.trip_template,
                    "operator_spu_id": operator_spu_id or "",
                    "payload_json": json.dumps(payload, ensure_ascii=False),
                    "trip_shadow_id": self.trip_shadow_id.id if self.trip_shadow_id else False,
                }
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "已提交审批",
                    "message": f"注册单 {reg.name} 已创建，等待审批。",
                    "type": "warning",
                    "sticky": False,
                },
            }

        try:
            result = launch_trip(payload, core_url=core_url)
        except RuntimeError as exc:
            raise UserError(f"启动行程失败: {exc}") from exc
        except Exception as exc:
            raise UserError(f"启动行程失败: {exc}") from exc

        trip_id = result.get("tripId")
        status = result.get("status") or "unknown"
        dispatch_plan_id = result.get("dispatchPlanId")
        if not trip_id:
            raise UserError(f"Core 返回缺少 tripId: {result.get('raw')}")

        try:
            detail = get_trip_detail(trip_id, core_url=core_url)
        except RuntimeError:
            detail = {
                "tripId": trip_id,
                "status": status,
                "projectNode": project_node_id,
                "tripTemplate": self.trip_template,
                "dispatchPlanId": dispatch_plan_id,
            }

        trip = self.trip_shadow_id
        if not trip:
            trip = self.env["coordos.trip.shadow"].create(
                {
                    "name": self.trip_template,
                    "project_node": project_node_id,
                    "trip_template": self.trip_template,
                    "input_json": self.input_json,
                }
            )

        trip.write(
            {
                "core_trip_id": detail.get("tripId") or trip_id,
                "x_status": detail.get("status") or status,
                "project_node": detail.get("projectNode") or project_node_id,
                "trip_template": detail.get("tripTemplate") or self.trip_template,
                "x_dispatch_plan_id": detail.get("dispatchPlanId") or dispatch_plan_id,
                "input_json": self.input_json,
                "x_last_sync_at": fields.Datetime.now(),
            }
        )

        self.write(
            {
                "result_trip_id": detail.get("tripId") or trip_id,
                "result_status": detail.get("status") or status,
                "result_dispatch_plan_id": detail.get("dispatchPlanId") or dispatch_plan_id,
                "project_node": project_node_id,
            }
        )
        return self._open_detail_action(trip)


class LaunchTripInputLine(models.TransientModel):
    _name = "launch.trip.input.line"
    _description = "启动行程模板输入项"
    _order = "sequence,id"

    wizard_id = fields.Many2one("launch.trip.wizard", required=True, ondelete="cascade")
    sequence = fields.Integer(default=10)
    key = fields.Char("键", required=True, readonly=True)
    label = fields.Char("字段", required=True, readonly=True)
    value_type = fields.Selection(
        [
            ("integer", "整数"),
            ("number", "数字"),
            ("boolean", "布尔"),
            ("json", "JSON"),
            ("string", "文本"),
        ],
        string="类型",
        default="string",
        readonly=True,
    )
    value_text = fields.Text("值")

    @api.model_create_multi
    def create(self, vals_list):
        patched = []
        for vals in vals_list:
            item = dict(vals)
            key = (item.get("key") or "").strip()
            label = (item.get("label") or "").strip()
            if not key:
                wizard = self.env["launch.trip.wizard"].browse(item.get("wizard_id")) if item.get("wizard_id") else False
                seq = int(item.get("sequence") or 0)

                # 1) Prefer existing lines in the same wizard (same sequence).
                if wizard and seq > 0:
                    existing = wizard.input_line_ids.filtered(lambda l: l.sequence == seq)[:1]
                    if existing and existing.key:
                        key = existing.key
                        label = label or existing.label or key
                        item.setdefault("value_type", existing.value_type or "string")

                # 2) Fallback: derive key from input_json field order.
                if (not key) and wizard and wizard.input_json and seq > 0:
                    try:
                        payload = json.loads(wizard.input_json)
                    except ValueError:
                        payload = {}
                    if isinstance(payload, dict):
                        keys = list(payload.keys())
                        if 1 <= seq <= len(keys):
                            key = str(keys[seq - 1])
                            label = label or key

                # 3) Last fallback: deterministic placeholder.
                if not key:
                    key = f"field_{seq or 1}"
                    label = label or key

            item["key"] = key
            item["label"] = label or key
            item.setdefault("value_type", "string")
            patched.append(item)

        return super().create(patched)
