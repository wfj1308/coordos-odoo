import json
import logging
import os

from odoo import api, fields, models
from odoo.exceptions import UserError

from .bridge_client import add_trip_evidence, certify_trip, get_ledger_by_trip, get_trip_detail


_logger = logging.getLogger(__name__)


class CoordosTripShadow(models.Model):
    _name = "coordos.trip.shadow"
    _description = "CoordOS 行程影子"
    _order = "id desc"

    name = fields.Char(required=True)
    core_trip_id = fields.Char("Core 行程ID", copy=False)
    project_node = fields.Char("项目节点")
    work_id = fields.Char("工作ID")
    trip_template = fields.Char("行程模板", default="pile_construction")
    trip_usi = fields.Char("行程USI", default="v://trip/engineering/pile@1.1.0")
    input_json = fields.Text("输入JSON", default="{}")
    evidence_json = fields.Text("证据JSON", default="{}")
    assert_json = fields.Text("断言JSON", default="{}")
    mint_json = fields.Text("签发JSON", default="{}")
    x_status = fields.Char("状态", readonly=True, copy=False)
    x_prc_hash = fields.Char("PRC 哈希", readonly=True, copy=False)
    x_utxo_id = fields.Char("UTXO ID", readonly=True, copy=False)
    x_dispatch_plan_id = fields.Char("调度计划ID", readonly=True, copy=False)
    x_evidence_ids = fields.Text("证据ID列表", readonly=True, copy=False)
    x_ledger_json = fields.Text("账本JSON", readonly=True, copy=False)
    x_assertion_result = fields.Text("Assertion 结果", readonly=True, copy=False)
    x_evidence_summary = fields.Text("证据摘要", readonly=True, copy=False)
    x_prc_summary = fields.Text("PRC 摘要", readonly=True, copy=False)
    x_ledger_summary = fields.Text("账本摘要", readonly=True, copy=False)
    x_utxo_summary = fields.Text("UTXO 摘要", readonly=True, copy=False)
    x_admission_id = fields.Char("准入ID", readonly=True, copy=False)
    x_last_sync_at = fields.Datetime("最近同步时间", readonly=True, copy=False)
    x_dashboard_evidence_html = fields.Html("证据卡片", compute="_compute_dashboard_cards", sanitize=False)
    x_dashboard_prc_html = fields.Html("PRC卡片", compute="_compute_dashboard_cards", sanitize=False)
    x_dashboard_ledger_html = fields.Html("账本卡片", compute="_compute_dashboard_cards", sanitize=False)
    x_dashboard_utxo_html = fields.Html("UTXO卡片", compute="_compute_dashboard_cards", sanitize=False)

    process_log_ids = fields.One2many(
        "coordos.trip.step.log", "trip_shadow_id", string="流程日志", readonly=True, copy=False
    )
    evidence_item_ids = fields.One2many(
        "coordos.trip.evidence.item", "trip_shadow_id", string="证据列表", readonly=True, copy=False
    )

    @api.depends(
        "x_evidence_ids",
        "x_prc_hash",
        "x_assertion_result",
        "x_ledger_json",
        "x_utxo_id",
        "x_status",
        "process_log_ids",
        "evidence_item_ids",
    )
    def _compute_dashboard_cards(self):
        def _escape(v):
            return (v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        for rec in self:
            evidence_count = len(rec._loads_json_list(rec.x_evidence_ids))
            steps_count = len(rec.process_log_ids)

            ledger_lines = 0
            if rec.x_ledger_json:
                try:
                    ledger_payload = json.loads(rec.x_ledger_json)
                    data_block = (
                        ledger_payload.get("data")
                        if isinstance(ledger_payload, dict) and isinstance(ledger_payload.get("data"), dict)
                        else {}
                    )
                    if isinstance(ledger_payload.get("lines"), list):
                        ledger_lines = len(ledger_payload.get("lines"))
                    elif isinstance(data_block.get("lines"), list):
                        ledger_lines = len(data_block.get("lines"))
                except ValueError:
                    ledger_lines = 0

            rec.x_dashboard_evidence_html = (
                "<div style='padding:8px;border:1px solid #d8dde6;border-radius:8px;'>"
                f"<div style='font-weight:600;margin-bottom:6px;'>证据</div>"
                f"<div>证据条数：<b>{evidence_count}</b></div>"
                f"<div>流程步骤：<b>{steps_count}</b></div>"
                f"<div>摘要：{_escape(rec.x_evidence_summary or '无')}</div>"
                "</div>"
            )
            rec.x_dashboard_prc_html = (
                "<div style='padding:8px;border:1px solid #d8dde6;border-radius:8px;'>"
                "<div style='font-weight:600;margin-bottom:6px;'>PRC / 断言</div>"
                f"<div>状态：<b>{_escape(rec.x_status or 'unknown')}</b></div>"
                f"<div>PRC 哈希：<code>{_escape(rec.x_prc_hash or '未生成')}</code></div>"
                f"<div>断言结果：{_escape(rec.x_assertion_result or '暂无')}</div>"
                "</div>"
            )
            rec.x_dashboard_ledger_html = (
                "<div style='padding:8px;border:1px solid #d8dde6;border-radius:8px;'>"
                "<div style='font-weight:600;margin-bottom:6px;'>Ledger</div>"
                f"<div>分录行数：<b>{ledger_lines}</b></div>"
                f"<div>摘要：{_escape(rec.x_ledger_summary or '暂无')}</div>"
                "</div>"
            )
            rec.x_dashboard_utxo_html = (
                "<div style='padding:8px;border:1px solid #d8dde6;border-radius:8px;'>"
                "<div style='font-weight:600;margin-bottom:6px;'>UTXO</div>"
                f"<div><code>{_escape(rec.x_utxo_id or '未生成')}</code></div>"
                f"<div>摘要：{_escape(rec.x_utxo_summary or '暂无')}</div>"
                "</div>"
            )

    def _loads_json(self, raw, label):
        try:
            return json.loads(raw or "{}")
        except ValueError as exc:
            raise UserError(f"{label} 的 JSON 格式错误: {exc}") from exc

    @api.model
    def _extract_nested(self, payload, *path):
        cur = payload
        for key in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        return cur

    @api.model
    def _first_present(self, payload, keys):
        if not isinstance(payload, dict):
            return None
        for key in keys:
            if key in payload and payload.get(key) not in (None, ""):
                return payload.get(key)
        return None

    @api.model
    def _extract_trip_id(self, payload):
        return (
            self._first_present(payload, ["trip_id", "tripId", "id"])
            or self._extract_nested(payload, "trip", "id")
            or self._extract_nested(payload, "data", "trip_id")
        )

    @api.model
    def _extract_status(self, payload, default_status=None):
        return (
            self._first_present(payload, ["status", "state"])
            or self._extract_nested(payload, "trip", "status")
            or self._extract_nested(payload, "data", "status")
            or default_status
        )

    @api.model
    def _extract_prc_hash(self, payload):
        return (
            self._first_present(payload, ["prc_hash", "prcHash"])
            or self._extract_nested(payload, "prc", "hash")
            or self._extract_nested(payload, "assertion", "prc_hash")
            or self._extract_nested(payload, "data", "prc_hash")
        )

    @api.model
    def _extract_utxo_id(self, payload):
        value = (
            self._first_present(payload, ["utxo_id", "product_utxo", "productUtxo"])
            or self._extract_nested(payload, "utxo", "id")
            or self._extract_nested(payload, "product", "utxo_id")
            or self._extract_nested(payload, "data", "product_utxo")
        )
        if isinstance(value, dict):
            return value.get("id")
        return value

    @api.model
    def _extract_evidence_ids(self, payload):
        value = (
            self._first_present(payload, ["evidence_ids", "evidenceIds"])
            or self._extract_nested(payload, "data", "evidence_ids")
        )
        if value is None:
            return None
        if isinstance(value, list):
            return value
        return [value]

    @api.model
    def _extract_dispatch_plan_id(self, payload):
        return (
            self._first_present(payload, ["dispatchPlanId", "dispatch_plan_id"])
            or self._extract_nested(payload, "dispatchPlan", "id")
            or self._extract_nested(payload, "dispatch", "plan_id")
            or self._extract_nested(payload, "data", "dispatchPlanId")
            or self._extract_nested(payload, "data", "dispatch_plan_id")
        )

    @api.model
    def _extract_assertion_result(self, payload):
        value = (
            self._first_present(payload, ["verdict", "assertion_result"])
            or self._extract_nested(payload, "assertion")
            or self._extract_nested(payload, "data", "assertion")
            or self._extract_nested(payload, "data", "verdict")
            or self._extract_nested(payload, "result")
            or self._extract_nested(payload, "data", "result")
        )
        if value in (None, ""):
            return None
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @api.model
    def _normalize_process_log(self, items):
        if not isinstance(items, list):
            return []
        normalized = []
        for item in items:
            if not isinstance(item, dict):
                continue
            metrics = item.get("metrics")
            if metrics is None:
                metrics = {}
            elif not isinstance(metrics, dict):
                metrics = {"value": metrics}
            normalized.append(
                {
                    "step_code": item.get("stepCode")
                    or item.get("step_code")
                    or item.get("step")
                    or item.get("code")
                    or "",
                    "started_at": item.get("startedAt") or item.get("started_at") or item.get("startAt") or "",
                    "ended_at": item.get("endedAt") or item.get("ended_at") or item.get("endAt") or "",
                    "metrics_json": json.dumps(metrics, ensure_ascii=False),
                }
            )
        return normalized

    @api.model
    def _extract_process_log(self, payload):
        if not isinstance(payload, dict):
            return None
        candidates = [
            payload.get("processLog"),
            payload.get("process_log"),
            payload.get("steps"),
            self._extract_nested(payload, "trip", "processLog"),
            self._extract_nested(payload, "trip", "process_log"),
            self._extract_nested(payload, "trip", "steps"),
            self._extract_nested(payload, "data", "processLog"),
            self._extract_nested(payload, "data", "process_log"),
            self._extract_nested(payload, "data", "steps"),
            self._extract_nested(payload, "raw", "processLog"),
            self._extract_nested(payload, "raw", "process_log"),
            self._extract_nested(payload, "raw", "steps"),
            self._extract_nested(payload, "raw", "trip", "processLog"),
            self._extract_nested(payload, "raw", "trip", "process_log"),
            self._extract_nested(payload, "raw", "trip", "steps"),
            self._extract_nested(payload, "raw", "data", "processLog"),
            self._extract_nested(payload, "raw", "data", "process_log"),
            self._extract_nested(payload, "raw", "data", "steps"),
        ]
        for value in candidates:
            if isinstance(value, list):
                return self._normalize_process_log(value)
        return None

    def _loads_json_list(self, raw):
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return parsed
        return []

    def _append_process_log_entry(self, entry):
        self.ensure_one()
        rows = self._normalize_process_log([entry])
        if not rows:
            return
        row = rows[0]
        exists = self.process_log_ids.filtered(
            lambda x: x.step_code == row["step_code"]
            and x.started_at == row["started_at"]
            and x.ended_at == row["ended_at"]
            and (x.metrics_json or "") == row["metrics_json"]
        )
        if not exists:
            self.env["coordos.trip.step.log"].create(
                {
                    "trip_shadow_id": self.id,
                    "step_code": row["step_code"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "metrics_json": row["metrics_json"],
                }
            )

    @api.model
    def _build_evidence_summary(self, evidence_ids_raw):
        values = self._loads_json_list(evidence_ids_raw) if evidence_ids_raw else []
        if not values:
            return "无证据"
        preview = ", ".join(str(x) for x in values[:3])
        if len(values) > 3:
            preview = f"{preview} ..."
        return f"{len(values)} 条：{preview}"

    @api.model
    def _build_prc_summary(self, prc_hash):
        return f"PRC: {prc_hash}" if prc_hash else "未生成 PRC"

    @api.model
    def _build_utxo_summary(self, utxo_id):
        return f"UTXO: {utxo_id}" if utxo_id else "未生成 UTXO"

    @api.model
    def _build_ledger_summary(self, ledger_json_raw):
        if not ledger_json_raw:
            return "暂无账本"
        try:
            payload = json.loads(ledger_json_raw)
        except ValueError:
            return "账本JSON格式错误"
        data_block = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
        lines = []
        if isinstance(payload, dict):
            if isinstance(payload.get("lines"), list):
                lines = payload.get("lines")
            elif isinstance(data_block.get("lines"), list):
                lines = data_block.get("lines")
        assets = payload.get("assets") if isinstance(payload, dict) else None
        liabilities = payload.get("liabilities") if isinstance(payload, dict) else None
        equity = payload.get("equity") if isinstance(payload, dict) else None
        summary_parts = [f"lines={len(lines)}"]
        if assets is not None:
            summary_parts.append(f"assets={assets}")
        if liabilities is not None:
            summary_parts.append(f"liabilities={liabilities}")
        if equity is not None:
            summary_parts.append(f"equity={equity}")
        return ", ".join(summary_parts)

    def _sync_evidence_items(self):
        for record in self:
            record.evidence_item_ids.unlink()
            values = record._loads_json_list(record.x_evidence_ids)
            for ref in values:
                ref_str = str(ref)
                kind = ref_str.split("://", 1)[0] if "://" in ref_str else "unknown"
                self.env["coordos.trip.evidence.item"].create(
                    {
                        "trip_shadow_id": record.id,
                        "evidence_ref": ref_str,
                        "evidence_kind": kind,
                    }
                )

    def _recompute_summaries(self):
        for record in self:
            record.write(
                {
                    "x_evidence_summary": record._build_evidence_summary(record.x_evidence_ids),
                    "x_prc_summary": record._build_prc_summary(record.x_prc_hash),
                    "x_utxo_summary": record._build_utxo_summary(record.x_utxo_id),
                    "x_ledger_summary": record._build_ledger_summary(record.x_ledger_json),
                }
            )

    def _apply_result(self, payload, default_status=None):
        now = fields.Datetime.now()
        for record in self:
            vals = {"x_last_sync_at": now}
            trip_id = record._extract_trip_id(payload)
            status = record._extract_status(payload, default_status=default_status)
            prc_hash = record._extract_prc_hash(payload)
            utxo_id = record._extract_utxo_id(payload)
            evidence_ids = record._extract_evidence_ids(payload)
            dispatch_plan_id = record._extract_dispatch_plan_id(payload)
            assertion_result = record._extract_assertion_result(payload)
            admission_id = record._first_present(payload, ["admission_id", "admissionId"])
            project_node = (
                record._first_present(payload, ["projectNodeId", "project_node_id", "projectNode", "project_node"])
                or record._extract_nested(payload, "trip", "project_node_id")
                or record._extract_nested(payload, "raw", "trip", "project_node_id")
            )
            trip_template = (
                record._first_present(
                    payload,
                    [
                        "tripTemplateCode",
                        "trip_template_code",
                        "tripTemplate",
                        "trip_template",
                    ],
                )
                or record._extract_nested(payload, "trip", "trip_template_code")
                or record._extract_nested(payload, "raw", "trip", "trip_template_code")
            )
            process_log = record._extract_process_log(payload)

            if trip_id:
                vals["core_trip_id"] = trip_id
            if status:
                vals["x_status"] = status
            if prc_hash:
                vals["x_prc_hash"] = prc_hash
            if utxo_id:
                vals["x_utxo_id"] = utxo_id
            if dispatch_plan_id:
                vals["x_dispatch_plan_id"] = dispatch_plan_id
            if project_node:
                vals["project_node"] = project_node
            if trip_template:
                vals["trip_template"] = trip_template
            if evidence_ids:
                vals["x_evidence_ids"] = json.dumps(evidence_ids, ensure_ascii=False)
            if admission_id:
                vals["x_admission_id"] = admission_id
            if assertion_result:
                vals["x_assertion_result"] = assertion_result
            if process_log is not None:
                vals["process_log_ids"] = [(5, 0, 0)] + [
                    (
                        0,
                        0,
                        {
                            "step_code": row["step_code"],
                            "started_at": row["started_at"],
                            "ended_at": row["ended_at"],
                            "metrics_json": row["metrics_json"],
                        },
                    )
                    for row in process_log
                ]
            record.write(vals)
            if "x_evidence_ids" in vals:
                record._sync_evidence_items()
            record._recompute_summaries()

    def _build_step_payload(self, json_blob):
        self.ensure_one()
        payload = self._loads_json(json_blob, "step payload")
        if not isinstance(payload, dict):
            raise UserError("步骤载荷必须是 JSON 对象")
        if self.core_trip_id:
            payload.setdefault("trip_id", self.core_trip_id)
        if not payload.get("trip_id"):
            raise UserError("缺少 trip_id，请先启动行程")
        return payload

    def action_open_launch_wizard(self):
        self.ensure_one()
        action = self.env.ref("coordos_odoo.action_trip_launch_wizard").read()[0]
        action["context"] = {
            "default_trip_shadow_id": self.id,
            "default_project_node": self.project_node,
            "default_trip_template": self.trip_template,
            "default_input_json": self.input_json or "{}",
        }
        return action

    def action_open_execute_step_wizard(self):
        self.ensure_one()
        action = self.env.ref("coordos_odoo.action_execute_trip_step_wizard").read()[0]
        action["context"] = {
            "default_trip_shadow_id": self.id,
        }
        return action

    def action_open_upload_evidence_wizard(self):
        self.ensure_one()
        action = self.env.ref("coordos_odoo.action_upload_trip_evidence_wizard").read()[0]
        action["context"] = {
            "default_trip_shadow_id": self.id,
        }
        return action

    def action_open_certify_wizard(self):
        self.ensure_one()
        action = self.env.ref("coordos_odoo.action_certify_trip_wizard").read()[0]
        action["context"] = {
            "default_trip_shadow_id": self.id,
            "default_evidence_ids_text": self.x_evidence_ids or "[]",
        }
        return action

    def action_refresh_status(self):
        for record in self:
            if not record.core_trip_id:
                raise UserError("查询状态前需要 Core 行程ID")
            core_url = (
                self.env["ir.config_parameter"].sudo().get_param("coordos.core_base_url")
                or os.getenv("COORDOS_CORE_URL")
                or "http://coordos-core:8080"
            )
            try:
                result = get_trip_detail(record.core_trip_id, core_url=core_url)
            except RuntimeError:
                result = self.env["coordos.api.mixin"].get_trip_status(record.core_trip_id)
            record._apply_result(result)
        return True

    def action_pull_ledger(self):
        for record in self:
            if not record.core_trip_id:
                raise UserError("缺少 trip_id，请先启动行程")
            core_url = (
                self.env["ir.config_parameter"].sudo().get_param("coordos.core_base_url")
                or os.getenv("COORDOS_CORE_URL")
                or "http://coordos-core:8080"
            )
            try:
                ledger = get_ledger_by_trip(record.core_trip_id, core_url=core_url)
            except RuntimeError as exc:
                raise UserError(f"拉取账本失败: {exc}") from exc
            record.write(
                {
                    "x_ledger_json": json.dumps(ledger, ensure_ascii=False),
                    "x_last_sync_at": fields.Datetime.now(),
                }
            )
            record._recompute_summaries()
        return True

    def action_upload_evidence(self):
        for record in self:
            if not record.core_trip_id:
                raise UserError("缺少 trip_id，请先启动行程")
            payload = record._loads_json(record.evidence_json, "证据JSON")
            if not isinstance(payload, dict):
                raise UserError("证据JSON 必须是 JSON 对象")
            payload.setdefault("evidence", ["photo://1", "report://1"])
            core_url = (
                self.env["ir.config_parameter"].sudo().get_param("coordos.core_base_url")
                or os.getenv("COORDOS_CORE_URL")
                or "http://coordos-core:8080"
            )
            try:
                result = add_trip_evidence(record.core_trip_id, payload, core_url=core_url)
            except RuntimeError as exc:
                raise UserError(f"上传证据失败: {exc}") from exc
            record._apply_result(result, default_status="evidence_added")
            try:
                detail = get_trip_detail(record.core_trip_id, core_url=core_url)
                record._apply_result(detail)
            except RuntimeError:
                pass
            try:
                ledger = get_ledger_by_trip(record.core_trip_id, core_url=core_url)
                record.write({"x_ledger_json": json.dumps(ledger, ensure_ascii=False)})
            except RuntimeError:
                pass
            record._recompute_summaries()
        return True

    def action_submit_assert(self):
        api_mixin = self.env["coordos.api.mixin"]
        for record in self:
            payload = record._build_step_payload(record.assert_json)
            if not payload.get("evidence_ids"):
                payload["evidence_ids"] = record._loads_json_list(record.x_evidence_ids)
            if not payload.get("evidence_ids"):
                raise UserError("请先上传证据，或在断言 JSON 中填写 evidence_ids")
            result = api_mixin.trip_assert(payload)
            record._apply_result(result, default_status="asserted")
        return True

    def action_complete_mint(self):
        api_mixin = self.env["coordos.api.mixin"]
        for record in self:
            payload = record._build_step_payload(record.mint_json)
            result = api_mixin.trip_mint(payload)
            record._apply_result(result, default_status="minted")
        return True

    def action_issue_result(self):
        for record in self:
            if not record.core_trip_id:
                raise UserError("缺少 trip_id，请先启动行程")
            payload = record._loads_json(record.mint_json, "签发JSON")
            if not isinstance(payload, dict):
                raise UserError("签发JSON 必须是 JSON 对象")
            payload.setdefault("evidence_ids", record._loads_json_list(record.x_evidence_ids))
            core_url = (
                self.env["ir.config_parameter"].sudo().get_param("coordos.core_base_url")
                or os.getenv("COORDOS_CORE_URL")
                or "http://coordos-core:8080"
            )
            try:
                result = certify_trip(record.core_trip_id, payload, core_url=core_url)
            except RuntimeError as exc:
                raise UserError(f"签发结果失败: {exc}") from exc
            record._apply_result(result, default_status="certified")
            try:
                detail = get_trip_detail(record.core_trip_id, core_url=core_url)
                record._apply_result(detail)
            except RuntimeError:
                pass
            try:
                ledger = get_ledger_by_trip(record.core_trip_id, core_url=core_url)
                record.write({"x_ledger_json": json.dumps(ledger, ensure_ascii=False)})
            except RuntimeError:
                pass
            record._recompute_summaries()
        return True

    @api.model
    def _parse_trip_items(self, response):
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            for key in ("trips", "items", "data", "list"):
                value = response.get(key)
                if isinstance(value, list):
                    return value
        return []

    @api.model
    def _trip_vals_from_item(self, item):
        if not isinstance(item, dict):
            return {}
        core_trip_id = self._first_present(item, ["trip_id", "tripId", "id"])
        if not core_trip_id and isinstance(item.get("trip"), dict):
            core_trip_id = item["trip"].get("id")
        status = self._first_present(item, ["status", "state", "x_status"])
        project_node = self._first_present(item, ["projectNode", "project_node", "projectNodeId", "project_node_id"])
        work_id = self._first_present(item, ["work_id", "workId"])
        name = self._first_present(item, ["name", "trip_name", "tripName"]) or work_id or core_trip_id
        trip_template = self._first_present(item, ["tripTemplate", "trip_template", "tripTemplateCode"])
        trip_usi = self._first_present(item, ["tripUsi", "trip_usi"])
        prc_hash = self._first_present(item, ["prc_hash", "prcHash"])
        utxo_id = (
            self._first_present(item, ["utxo_id", "product_utxo", "productUtxo"])
            or self._extract_nested(item, "utxo", "id")
        )
        if isinstance(utxo_id, dict):
            utxo_id = utxo_id.get("id")
        vals = {
            "name": name or "行程",
            "core_trip_id": core_trip_id,
            "project_node": project_node,
            "work_id": work_id,
            "trip_template": trip_template,
            "trip_usi": trip_usi,
            "x_status": status,
            "x_prc_hash": prc_hash,
            "x_utxo_id": utxo_id,
            "x_last_sync_at": fields.Datetime.now(),
        }
        return {k: v for k, v in vals.items() if v not in (None, "")}

    @api.model
    def _trip_sync_strict(self):
        value = self.env["ir.config_parameter"].sudo().get_param("coordos.trip_sync_strict")
        if value is None:
            value = os.getenv("COORDOS_TRIP_SYNC_STRICT", "0")
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @api.model
    def _skip_trip_sync_for_core(self):
        if self._trip_sync_strict():
            return False
        core_base = (self.env["coordos.api.mixin"]._configured_core_base() or "").lower()
        return "api.codepeg.com" in core_base

    @api.model
    def sync_from_core(self):
        if self._skip_trip_sync_for_core():
            _logger.info("Core 当前环境未启用 trip 列表接口，跳过行程同步。")
            return 0
        api_mixin = self.env["coordos.api.mixin"]
        try:
            response = api_mixin.get_trip_list()
        except UserError as exc:
            message = str(exc)
            if "404 Client Error: Not Found" in message and "/trip/list" in message:
                _logger.info("Core 未提供 /trip/list，跳过行程同步。")
                return 0
            raise
        items = self._parse_trip_items(response)
        for item in items:
            vals = self._trip_vals_from_item(item)
            if not vals:
                continue
            core_trip_id = vals.get("core_trip_id")
            domain = [
                ("core_trip_id", "=", core_trip_id)
            ] if core_trip_id else [
                ("work_id", "=", vals.get("work_id"))
            ]
            record = self.search(domain, limit=1)
            if record:
                record.write(vals)
            else:
                self.create(vals)
        return len(items)

    def action_sync_from_core(self):
        try:
            count = self.sync_from_core()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "行程同步完成",
                    "message": f"已从 Core 拉取 {count} 条行程",
                    "type": "success",
                },
            }
        except Exception as exc:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "行程同步失败",
                    "message": str(exc),
                    "type": "warning",
                    "sticky": True,
                },
            }

    @api.model
    def cron_sync_from_core(self):
        try:
            self.sync_from_core()
        except Exception:
            _logger.exception("CoordOS 行程同步失败")


class CoordosTripStepLog(models.Model):
    _name = "coordos.trip.step.log"
    _description = "CoordOS 行程流程日志"
    _order = "id asc"

    trip_shadow_id = fields.Many2one("coordos.trip.shadow", required=True, ondelete="cascade", index=True)
    step_code = fields.Char("步骤编码")
    started_at = fields.Char("开始时间")
    ended_at = fields.Char("结束时间")
    metrics_json = fields.Text("指标")


class CoordosTripEvidenceItem(models.Model):
    _name = "coordos.trip.evidence.item"
    _description = "CoordOS 行程证据项"
    _order = "id asc"

    trip_shadow_id = fields.Many2one("coordos.trip.shadow", required=True, ondelete="cascade", index=True)
    evidence_kind = fields.Char("类型")
    evidence_ref = fields.Char("证据")
