import json
import os

from odoo import fields, models
from odoo.exceptions import UserError

from .bridge_client import execute_trip_step, get_trip_detail


class ExecuteTripStepWizard(models.TransientModel):
    _name = "execute.trip.step.wizard"
    _description = "执行行程步骤"

    trip_shadow_id = fields.Many2one("coordos.trip.shadow", string="行程", required=True, readonly=True)
    step_code = fields.Char("步骤编码", default="drilling", required=True)
    started_at = fields.Datetime("开始时间")
    ended_at = fields.Datetime("结束时间")
    metrics_json = fields.Text("指标JSON", default='{"actual_depth_m": 32.4}', required=True)

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
            "view_id": self.env.ref("coordos_odoo.view_trip_detail_min_form").id,
            "target": "current",
        }

    def _open_upload_evidence_wizard_action(self, trip):
        action = self.env.ref("coordos_odoo.action_upload_trip_evidence_wizard").read()[0]
        action["context"] = {
            "default_trip_shadow_id": trip.id,
        }
        return action

    def action_execute(self):
        self.ensure_one()
        trip = self.trip_shadow_id
        if not trip or not trip.core_trip_id:
            raise UserError("当前行程缺少 trip_id，请先完成启动行程")

        try:
            metrics = json.loads(self.metrics_json or "{}")
        except ValueError as exc:
            raise UserError(f"指标JSON格式错误: {exc}") from exc
        if not isinstance(metrics, dict):
            raise UserError("指标JSON必须是JSON对象")

        metrics_payload = dict(metrics)
        started_at_str = fields.Datetime.to_string(self.started_at) if self.started_at else ""
        ended_at_str = fields.Datetime.to_string(self.ended_at) if self.ended_at else ""
        if started_at_str:
            metrics_payload["startedAt"] = started_at_str
        if ended_at_str:
            metrics_payload["endedAt"] = ended_at_str

        payload = {
            "step": self.step_code,
            "metrics": metrics_payload,
        }
        core_url = self._core_base_url()
        try:
            result = execute_trip_step(trip.core_trip_id, payload, core_url=core_url)
        except RuntimeError as exc:
            raise UserError(f"执行步骤失败: {exc}") from exc

        try:
            detail = get_trip_detail(trip.core_trip_id, core_url=core_url)
            trip._apply_result(detail)
        except RuntimeError:
            trip._apply_result(result, default_status="step_recorded")

        trip._append_process_log_entry(
            {
                "stepCode": self.step_code,
                "startedAt": started_at_str,
                "endedAt": ended_at_str,
                "metrics": metrics,
            }
        )

        return self._open_upload_evidence_wizard_action(trip)
