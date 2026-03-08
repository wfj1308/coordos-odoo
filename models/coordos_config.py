import json
from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError

from .bridge_client import get_trip_detail, launch_trip


class CoordosTripTemplateConfig(models.Model):
    _name = "coordos.trip.template.config"
    _description = "CoordOS 行程模板配置"
    _rec_name = "display_name"

    active = fields.Boolean("启用", default=True)
    name = fields.Char("模板名称", required=True)
    code = fields.Char("模板编码", required=True)
    item_code = fields.Char("SMU 条目编码")
    template_id = fields.Char("模板ID")
    namespace_prefix = fields.Char("命名空间前缀")
    input_schema_json = fields.Text("输入字段Schema(JSON)")
    default_input_json = fields.Text("默认输入JSON", default="{}")
    display_name = fields.Char(compute="_compute_display_name", store=False)

    _sql_constraints = [
        ("coordos_trip_template_code_uniq", "unique(code)", "模板编码必须唯一。"),
    ]

    @api.depends("name", "code")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"{rec.name}（{rec.code}）" if rec.code else rec.name

    def schema_fields(self):
        self.ensure_one()
        if not self.input_schema_json:
            return []
        try:
            data = json.loads(self.input_schema_json)
        except ValueError:
            return []
        if isinstance(data, dict):
            data = data.get("fields") or data.get("input_fields") or []
        if not isinstance(data, list):
            return []
        fields_list = []
        for item in data:
            if not isinstance(item, dict):
                continue
            key = item.get("key") or item.get("name")
            if not key:
                continue
            fields_list.append(
                {
                    "key": key,
                    "label": item.get("label") or key,
                    "type": item.get("type") or "string",
                    "default": item.get("default"),
                }
            )
        return fields_list

    def default_input_payload(self):
        self.ensure_one()
        if not self.default_input_json:
            return {}
        try:
            payload = json.loads(self.default_input_json)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}


class CoordosNamespacePolicy(models.Model):
    _name = "coordos.namespace.policy"
    _description = "CoordOS 命名空间策略"

    active = fields.Boolean("启用", default=True)
    name = fields.Char("策略名称", required=True)
    namespace_prefix = fields.Char("命名空间前缀", required=True)
    require_approval = fields.Boolean("启动行程需审批", default=False)
    trip_name_pattern = fields.Char(
        "Trip 命名规则",
        default="{template}_{org}_{date}_{seq}",
        help="可用占位符：{template} {org} {date} {ts} {seq}",
    )
    default_operator_spu_id = fields.Char("默认执行者 SPU ID")
    allowed_group_ids = fields.Many2many(
        "res.groups",
        "coordos_namespace_policy_group_rel",
        "policy_id",
        "group_id",
        string="允许角色",
    )

    _sql_constraints = [
        ("coordos_namespace_prefix_uniq", "unique(namespace_prefix)", "命名空间前缀必须唯一。"),
    ]

    @api.model
    def extract_namespace(self, project_node_id):
        node = (project_node_id or "").strip()
        if not node:
            return ""
        marker = "v://project-node/"
        if marker in node:
            rest = node.split(marker, 1)[1]
            return rest.split("/", 1)[0]
        return node.split("/", 1)[0]

    @api.model
    def match_policy(self, project_node_id):
        namespace = self.extract_namespace(project_node_id)
        if not namespace:
            return self.browse()
        return self.search([("active", "=", True), ("namespace_prefix", "=", namespace)], limit=1)

    def ensure_user_allowed(self, user):
        self.ensure_one()
        if not self.allowed_group_ids:
            return
        allowed = bool(set(self.allowed_group_ids.ids) & set(user.groups_id.ids))
        if not allowed:
            raise UserError("当前用户没有该命名空间注册权限。请联系项目经理或公司管理员。")

    def build_trip_name(self, template_code, org_code):
        self.ensure_one()
        pattern = (self.trip_name_pattern or "{template}_{org}_{date}_{seq}").strip()
        seq = self.env["coordos.trip.registration"].sudo().search_count([]) + 1
        values = {
            "template": (template_code or "trip").strip(),
            "org": (org_code or "org").strip(),
            "date": fields.Date.today().strftime("%Y%m%d"),
            "ts": datetime.now().strftime("%Y%m%d%H%M%S"),
            "seq": f"{seq:04d}",
        }
        try:
            name = pattern.format(**values)
        except Exception:
            name = f"{values['template']}_{values['org']}_{values['date']}_{values['seq']}"
        return name[:120]


class CoordosTripRegistration(models.Model):
    _name = "coordos.trip.registration"
    _description = "CoordOS 行程注册审批"
    _order = "id desc"

    name = fields.Char("名称", required=True)
    state = fields.Selection(
        [
            ("pending", "待审批"),
            ("approved", "已审批"),
            ("rejected", "已驳回"),
            ("registered", "已注册"),
        ],
        string="状态",
        default="pending",
        required=True,
    )
    requested_by = fields.Many2one("res.users", string="申请人", default=lambda self: self.env.user, readonly=True)
    approved_by = fields.Many2one("res.users", string="审批人", readonly=True)
    approved_at = fields.Datetime("审批时间", readonly=True)
    org_code = fields.Char("组织编码")
    project_node_id = fields.Char("项目节点ID", required=True)
    trip_template = fields.Char("行程模板", required=True)
    operator_spu_id = fields.Char("执行者SPU")
    payload_json = fields.Text("启动载荷(JSON)", required=True)
    reason = fields.Text("审批意见")
    trip_shadow_id = fields.Many2one("coordos.trip.shadow", string="关联行程")
    core_trip_id = fields.Char("Core 行程ID", readonly=True)
    result_status = fields.Char("结果状态", readonly=True)

    def _core_base_url(self):
        return self.env["coordos.api.mixin"]._configured_core_base()

    def _open_trip(self):
        self.ensure_one()
        if not self.trip_shadow_id:
            return {"type": "ir.actions.act_window_close"}
        return {
            "type": "ir.actions.act_window",
            "name": "行程详情",
            "res_model": "coordos.trip.shadow",
            "res_id": self.trip_shadow_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_approve(self):
        for rec in self:
            rec.write(
                {
                    "state": "approved",
                    "approved_by": self.env.user.id,
                    "approved_at": fields.Datetime.now(),
                }
            )
        return True

    def action_reject(self):
        for rec in self:
            rec.write(
                {
                    "state": "rejected",
                    "approved_by": self.env.user.id,
                    "approved_at": fields.Datetime.now(),
                }
            )
        return True

    def action_register(self):
        for rec in self:
            if rec.state not in {"approved", "pending"}:
                raise UserError("仅待审批/已审批记录可执行注册。")
            try:
                payload = json.loads(rec.payload_json or "{}")
            except ValueError as exc:
                raise UserError(f"载荷JSON非法: {exc}") from exc
            result = launch_trip(payload, core_url=rec._core_base_url())
            trip_id = result.get("tripId")
            status = result.get("status") or "unknown"
            if not trip_id:
                raise UserError("Core 未返回 tripId。")

            try:
                detail = get_trip_detail(trip_id, core_url=rec._core_base_url())
            except Exception:
                detail = {
                    "tripId": trip_id,
                    "status": status,
                    "projectNode": rec.project_node_id,
                    "tripTemplate": rec.trip_template,
                    "dispatchPlanId": result.get("dispatchPlanId"),
                }

            trip = rec.trip_shadow_id
            if not trip:
                trip = self.env["coordos.trip.shadow"].create(
                    {
                        "name": rec.trip_template,
                        "project_node": rec.project_node_id,
                        "trip_template": rec.trip_template,
                        "input_json": "{}",
                    }
                )
            trip.write(
                {
                    "core_trip_id": detail.get("tripId") or trip_id,
                    "x_status": detail.get("status") or status,
                    "project_node": detail.get("projectNode") or rec.project_node_id,
                    "trip_template": detail.get("tripTemplate") or rec.trip_template,
                    "x_dispatch_plan_id": detail.get("dispatchPlanId") or result.get("dispatchPlanId"),
                    "x_last_sync_at": fields.Datetime.now(),
                }
            )
            rec.write(
                {
                    "state": "registered",
                    "core_trip_id": detail.get("tripId") or trip_id,
                    "result_status": detail.get("status") or status,
                    "trip_shadow_id": trip.id,
                    "approved_by": self.env.user.id,
                    "approved_at": fields.Datetime.now(),
                }
            )
        return self[0]._open_trip()
