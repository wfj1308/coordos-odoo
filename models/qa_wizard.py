import json

from odoo import fields, models
from odoo.exceptions import UserError


class CoordosQAWizard(models.TransientModel):
    _name = "coordos.qa.wizard"
    _description = "CoordOS 智能问答"

    project_id = fields.Many2one("coordos.project", string="项目（可选）")
    trip_id = fields.Many2one("coordos.trip.shadow", string="行程（可选）")
    question = fields.Text("问题", required=True)
    answer = fields.Text("回答", readonly=True)
    evidence_refs = fields.Text("追溯依据", readonly=True)

    def _trip_domain(self):
        if self.trip_id:
            return [("id", "=", self.trip_id.id)]
        if self.project_id:
            return [("project_node", "ilike", (self.project_id.code or "").lower())]
        return []

    def _collect_stats(self):
        trip_model = self.env["coordos.trip.shadow"]
        domain = self._trip_domain()
        trips = trip_model.search(domain) if domain else trip_model.search([], limit=100)
        status_count = {}
        evidence_total = 0
        latest_trip = None
        for trip in trips:
            status = trip.x_status or "unknown"
            status_count[status] = status_count.get(status, 0) + 1
            evidence_total += len(trip.evidence_item_ids)
            if not latest_trip or (trip.x_last_sync_at and trip.x_last_sync_at > latest_trip.x_last_sync_at):
                latest_trip = trip
        return trips, status_count, evidence_total, latest_trip

    def _build_answer(self, question, trips, status_count, evidence_total, latest_trip):
        q = (question or "").strip().lower()
        if not q:
            raise UserError("请输入问题。")

        project = self.project_id
        quality_plan = project.x_quality_plan_json if project else ""
        schedule = project.x_schedule_json if project else ""
        budget = project.x_budget_json if project else ""
        risks = project.x_risk_json if project else ""

        if any(word in q for word in ["状态", "status", "进度"]):
            return (
                f"当前查询范围行程共 {len(trips)} 条，状态分布：{json.dumps(status_count, ensure_ascii=False)}。"
                f"{' 最近一条状态为 ' + (latest_trip.x_status or 'unknown') if latest_trip else ''}"
            )
        if any(word in q for word in ["证据", "evidence", "追溯"]):
            latest = latest_trip.x_evidence_ids if latest_trip else ""
            return f"证据总数约 {evidence_total} 条。最近行程证据引用：{latest or '暂无'}。"
        if any(word in q for word in ["prc", "认证", "断言"]):
            prc = latest_trip.x_prc_hash if latest_trip else ""
            assertion = latest_trip.x_assertion_result if latest_trip else ""
            return f"最近行程 PRC: {prc or '暂无'}；断言结果：{assertion or '暂无'}。"
        if any(word in q for word in ["账本", "ledger", "utxo"]):
            utxo = latest_trip.x_utxo_id if latest_trip else ""
            ledger = latest_trip.x_ledger_summary if latest_trip else ""
            return f"最近行程 UTXO: {utxo or '暂无'}；账本摘要：{ledger or '暂无'}。"
        if any(word in q for word in ["预算", "成本", "budget"]):
            return f"预算分解：{budget or '暂无（请先执行导入生成）'}"
        if any(word in q for word in ["质量计划", "质检", "quality"]):
            return f"质量计划：{quality_plan or '暂无（请先执行导入生成）'}"
        if any(word in q for word in ["进度", "工期", "schedule"]):
            return f"进度计划：{schedule or '暂无（请先执行导入生成）'}"
        if any(word in q for word in ["风险", "偏差", "根因", "risk"]):
            return f"风险评估：{risks or '暂无（请先执行导入生成）'}"

        return (
            f"已检索 {len(trips)} 条行程。可提问示例：项目状态、质量追溯、PRC、账本/UTXO。"
        )

    def action_ask(self):
        self.ensure_one()
        trips, status_count, evidence_total, latest_trip = self._collect_stats()
        answer = self._build_answer(self.question, trips, status_count, evidence_total, latest_trip)
        refs = []
        for trip in trips[:10]:
            refs.append(f"{trip.name} / {trip.core_trip_id} / 状态={trip.x_status}")
        self.write({"answer": answer, "evidence_refs": "\n".join(refs)})
        return {
            "type": "ir.actions.act_window",
            "name": "智能问答",
            "res_model": "coordos.qa.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
