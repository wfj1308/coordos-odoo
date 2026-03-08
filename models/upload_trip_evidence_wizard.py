import json
import os

from odoo import fields, models
from odoo.exceptions import UserError

from .bridge_client import add_trip_evidence, get_ledger_by_trip, get_trip_detail


class UploadTripEvidenceWizard(models.TransientModel):
    _name = "upload.trip.evidence.wizard"
    _description = "上传行程证据"

    trip_shadow_id = fields.Many2one("coordos.trip.shadow", string="行程", required=True, readonly=True)
    photo_refs = fields.Text("照片", default="photo://site-1")
    report_refs = fields.Text("报告", default="report://pile-001")
    artifact_refs = fields.Text("工件", default="v://artifact/pile/001/depth-photo.jpg")

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

    def _open_certify_wizard_action(self, trip):
        action = self.env.ref("coordos_odoo.action_certify_trip_wizard").read()[0]
        action["context"] = {
            "default_trip_shadow_id": trip.id,
            "default_evidence_ids_text": trip.x_evidence_ids or "[]",
        }
        return action

    def _split_refs(self, value):
        if not value:
            return []
        normalized = value.replace("\r", "\n").replace(",", "\n")
        return [item.strip() for item in normalized.split("\n") if item.strip()]

    def action_upload(self):
        self.ensure_one()
        trip = self.trip_shadow_id
        if not trip or not trip.core_trip_id:
            raise UserError("当前行程缺少 trip_id，请先完成启动行程")

        evidence = self._split_refs(self.photo_refs) + self._split_refs(self.report_refs) + self._split_refs(
            self.artifact_refs
        )
        if not evidence:
            raise UserError("请至少填写一条证据（照片/报告/artifact）")

        core_url = self._core_base_url()
        try:
            result = add_trip_evidence(trip.core_trip_id, {"evidence": evidence}, core_url=core_url)
        except RuntimeError as exc:
            raise UserError(f"上传证据失败: {exc}") from exc

        trip._apply_result(result, default_status="evidence_added")

        # 强制刷新 trip 详情，拿到最新状态和 process log
        try:
            detail = get_trip_detail(trip.core_trip_id, core_url=core_url)
            trip._apply_result(detail)
        except RuntimeError:
            pass

        # 进入证据 -> 账本链路
        try:
            ledger = get_ledger_by_trip(trip.core_trip_id, core_url=core_url)
            trip.write({"x_ledger_json": json.dumps(ledger, ensure_ascii=False)})
        except RuntimeError:
            pass
        trip._recompute_summaries()

        return self._open_certify_wizard_action(trip)
