import json
import os

from odoo import fields, models
from odoo.exceptions import UserError

from .bridge_client import certify_trip, get_ledger_by_trip, get_trip_detail


class CertifyTripWizard(models.TransientModel):
    _name = "certify.trip.wizard"
    _description = "签发认证"

    trip_shadow_id = fields.Many2one("coordos.trip.shadow", string="行程", required=True, readonly=True)
    evidence_ids_text = fields.Text("证据ID列表(JSON)", default="[]")
    quantity = fields.Float("数量", default=1.0, required=True)
    unit_price = fields.Float("单价", default=0.0, required=True)
    extra_json = fields.Text("扩展JSON", default="{}")

    def _core_base_url(self):
        return (
            self.env["ir.config_parameter"].sudo().get_param("coordos.core_base_url")
            or os.getenv("COORDOS_CORE_URL")
            or "http://coordos-core:8080"
        )

    def _open_detail_action(self, trip):
        return {
            "type": "ir.actions.act_window",
            "name": "行程详情",
            "res_model": "coordos.trip.shadow",
            "res_id": trip.id,
            "view_mode": "form",
            "view_id": self.env.ref("coordos_shell.view_trip_detail_min_form").id,
            "target": "current",
        }

    def _parse_evidence_ids(self):
        text = (self.evidence_ids_text or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise UserError(f"证据ID JSON 格式错误: {exc}") from exc
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, str):
            return [parsed]
        raise UserError("证据ID列表必须是数组或字符串")

    def _parse_extra(self):
        text = (self.extra_json or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise UserError(f"扩展JSON格式错误: {exc}") from exc
        if not isinstance(parsed, dict):
            raise UserError("扩展JSON必须是对象")
        return parsed

    def action_certify(self):
        self.ensure_one()
        trip = self.trip_shadow_id
        if not trip or not trip.core_trip_id:
            raise UserError("当前行程缺少 trip_id，请先启动行程")

        evidence_ids = self._parse_evidence_ids()
        if not evidence_ids:
            evidence_ids = trip._loads_json_list(trip.x_evidence_ids)
        if not evidence_ids:
            raise UserError("没有可用证据，请先上传证据")

        payload = {
            "evidence_ids": evidence_ids,
            "quantity": float(self.quantity or 0),
            "unit_price": float(self.unit_price or 0),
        }
        payload.update(self._parse_extra())

        core_url = self._core_base_url()
        try:
            result = certify_trip(trip.core_trip_id, payload, core_url=core_url)
        except RuntimeError as exc:
            raise UserError(f"签发结果失败: {exc}") from exc

        trip._apply_result(result, default_status="certified")
        try:
            detail = get_trip_detail(trip.core_trip_id, core_url=core_url)
            trip._apply_result(detail)
        except RuntimeError:
            pass
        try:
            ledger = get_ledger_by_trip(trip.core_trip_id, core_url=core_url)
            trip.write({"x_ledger_json": json.dumps(ledger, ensure_ascii=False)})
        except RuntimeError:
            pass
        trip._recompute_summaries()
        return self._open_detail_action(trip)
