from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import UserError


class CoordosSpu(models.Model):
    _name = "coordos.spu"
    _description = "CoordOS SPU（影子模型）"

    name = fields.Char("名称", required=True)
    category = fields.Selection(
        [
            ("qual", "质量资源（qual）"),
            ("person", "人员/执行者（person）"),
            ("perf", "性能/能量（perf）"),
        ],
        string="类别",
        required=True,
    )
    code = fields.Char("编码")
    project_id = fields.Many2one("coordos.project", string="项目")
    x_core_usi = fields.Char("CoordOS USI", readonly=True, copy=False)
    x_utxo_id = fields.Char("UTXO ID", readonly=True, copy=False)
    x_status = fields.Char("状态", readonly=True, copy=False)
    x_graph_node_id = fields.Char("图谱节点ID", readonly=True, copy=False)

    def _default_owner_spu(self):
        owner = self.env["ir.config_parameter"].sudo().get_param("coordos.core_default_owner_spu")
        if owner:
            return owner
        core_base = (self.env["coordos.api.mixin"]._configured_core_base() or "").lower()
        if "api.codepeg.com" in core_base:
            return "did:cp:test"
        return "v://spu/person/demo@1.0.0"

    def _is_api_success(self, result):
        return result.get("status") == "success" or bool(result.get("ok"))

    def _ensure_namespace_allowed(self, vals):
        project_id = vals.get("project_id")
        if not project_id:
            return
        project = self.env["coordos.project"].browse(project_id)
        namespace = (project.code or "").strip().lower()
        if not namespace:
            return
        policy = self.env["coordos.namespace.policy"].search(
            [("active", "=", True), ("namespace_prefix", "=", namespace)],
            limit=1,
        )
        if policy:
            policy.ensure_user_allowed(self.env.user)

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get("skip_core_register"):
            draft_vals = []
            for vals in vals_list:
                self._ensure_namespace_allowed(vals)
                item = dict(vals)
                item.setdefault("x_status", "draft")
                draft_vals.append(item)
            records = super().create(draft_vals)
            for rec in records:
                if rec.project_id:
                    node = rec.project_id.ensure_spu_node(rec)
                    if node:
                        rec.with_context(skip_core_sync=True).write({"x_graph_node_id": node.node_id})
            return records

        api_mixin = self.env["coordos.api.mixin"]
        patched_vals_list = []
        for vals in vals_list:
            self._ensure_namespace_allowed(vals)
            owner = vals.get("owner") or self._default_owner_spu()
            payload = {
                "category": vals.get("category"),
                "name": vals.get("name"),
                "owner": owner,
                "metadata": {
                    "name": vals.get("name"),
                    "code": vals.get("code"),
                    "project_id": vals.get("project_id"),
                },
            }
            result = api_mixin.register_spu(payload)
            if not self._is_api_success(result):
                raise UserError(result.get("reason") or result.get("error") or "SPU 注册失败")

            data_block = result.get("data") if isinstance(result.get("data"), dict) else {}
            spu_data = result.get("spu", {}) if isinstance(result.get("spu"), dict) else {}
            utxo_data = result.get("utxo", {}) if isinstance(result.get("utxo"), dict) else {}
            if not spu_data and isinstance(data_block.get("spu"), dict):
                spu_data = data_block.get("spu")
            if not utxo_data and isinstance(data_block.get("utxo"), dict):
                utxo_data = data_block.get("utxo")
            new_vals = dict(vals)
            new_vals["x_core_usi"] = spu_data.get("id") or result.get("spu_id") or result.get("id")
            new_vals["x_utxo_id"] = utxo_data.get("id") or result.get("utxo_id")
            new_vals["x_status"] = spu_data.get("status") or result.get("status") or "verified"
            patched_vals_list.append(new_vals)
        records = super().create(patched_vals_list)
        for rec in records:
            if rec.project_id:
                node = rec.project_id.ensure_spu_node(rec)
                if node:
                    rec.with_context(skip_core_sync=True).write({"x_graph_node_id": node.node_id})
        return records

    def write(self, vals):
        if self.env.context.get("skip_core_sync"):
            return super().write(vals)

        api_mixin = self.env["coordos.api.mixin"]
        strict_update = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("coordos.spu_update_strict", "0")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        core_base = (api_mixin._configured_core_base() or "").lower()
        for record in self:
            if (not strict_update) and "api.codepeg.com" in core_base:
                super(CoordosSpu, record.with_context(skip_core_sync=True)).write(vals)
                continue

            owner = vals.get("owner") or record._default_owner_spu()
            payload = {
                "spu_id": record.x_core_usi,
                "category": vals.get("category", record.category),
                "owner": owner,
                "name": vals.get("name", record.name),
                "code": vals.get("code", record.code),
                "project_id": vals.get("project_id", record.project_id.id if record.project_id else False),
                "metadata": {
                    "name": vals.get("name", record.name),
                    "code": vals.get("code", record.code),
                    "project_id": vals.get("project_id", record.project_id.id if record.project_id else False),
                },
            }
            try:
                result = api_mixin.core_update_spu(payload)
            except UserError as exc:
                message = str(exc)
                # 某些 Core 的阶段版本仅支持注册/查询，不支持更新。
                if (not strict_update) and "404 Client Error: Not Found" in message:
                    super(CoordosSpu, record.with_context(skip_core_sync=True)).write(vals)
                    continue
                raise

            if not self._is_api_success(result):
                raise UserError(result.get("reason") or result.get("error") or "SPU 更新失败")

            rec_vals = dict(vals)
            spu_data = result.get("spu", {})
            utxo_data = result.get("utxo", {})
            rec_vals["x_core_usi"] = spu_data.get("id") or result.get("spu_id") or record.x_core_usi
            rec_vals["x_utxo_id"] = utxo_data.get("id") or result.get("utxo_id") or record.x_utxo_id
            rec_vals["x_status"] = spu_data.get("status") or result.get("status") or record.x_status or "verified"
            super(CoordosSpu, record.with_context(skip_core_sync=True)).write(rec_vals)
        return True

    def action_view_graph(self):
        self.ensure_one()
        if not self.x_core_usi:
            raise UserError("该 SPU 缺少 CoordOS USI。")
        api_mixin = self.env["coordos.api.mixin"]
        api_mixin.get_spu_graph(self.x_core_usi)
        encoded_id = quote(self.x_core_usi, safe="")
        return {
            "type": "ir.actions.act_url",
            "url": api_mixin._core_url(f"/spu/{encoded_id}/graph"),
            "target": "new",
        }

    def action_view_finance(self):
        api_mixin = self.env["coordos.api.mixin"]
        api_mixin.get_finance_balance({"period": "current"})
        return {
            "type": "ir.actions.act_url",
            "url": api_mixin._core_url("/peg/finance/balance-sheet"),
            "target": "new",
        }
