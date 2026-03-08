from odoo import fields, models


class CoordosProject(models.Model):
    _name = "coordos.project"
    _description = "CoordOS 项目"

    name = fields.Char("名称", required=True)
    code = fields.Char("编码")
    project_usi = fields.Char("项目USI")
    org_code = fields.Char("组织编码")
    contract_no = fields.Char("合同号")
    construction_unit = fields.Char("施工单位")
    supervision_unit = fields.Char("监理单位")
    default_bridge_name = fields.Char("默认桥梁名称")
    default_pier_name = fields.Char("默认墩位")
    x_quality_plan_json = fields.Text("质量计划(JSON)", readonly=True, copy=False)
    x_schedule_json = fields.Text("进度计划(JSON)", readonly=True, copy=False)
    x_budget_json = fields.Text("预算分解(JSON)", readonly=True, copy=False)
    x_risk_json = fields.Text("风险评估(JSON)", readonly=True, copy=False)
    x_generated_at = fields.Datetime("最近生成时间", readonly=True, copy=False)

    def namespace_prefix(self):
        self.ensure_one()
        if self.code:
            return self.code.strip().lower()
        if self.project_usi and "v://" in self.project_usi:
            return self.project_usi.split("v://", 1)[1].split("/", 1)[0].lower()
        return "project"

    def ensure_root_node(self):
        self.ensure_one()
        node_model = self.env["coordos.project.node"]
        root_node_id = f"v://project-node/{self.namespace_prefix()}/root"
        node = node_model.search([("node_id", "=", root_node_id)], limit=1)
        if node:
            if node.project_id != self:
                node.write({"project_id": self.id, "active": True})
            return node
        return node_model.create(
            {
                "name": f"{self.name or self.code or '项目'}-根节点",
                "project_id": self.id,
                "node_id": root_node_id,
                "active": True,
            }
        )

    def ensure_spu_node(self, spu):
        self.ensure_one()
        self.ensure_root_node()
        if not spu or not spu.x_core_usi:
            return self.env["coordos.project.node"]
        safe_spu = spu.x_core_usi.replace("://", "/").replace("/", "-")
        node_id = f"v://project-node/{self.namespace_prefix()}/spu/{safe_spu}"
        node_model = self.env["coordos.project.node"]
        node = node_model.search([("node_id", "=", node_id)], limit=1)
        vals = {
            "name": spu.name or spu.code or "SPU",
            "project_id": self.id,
            "node_id": node_id,
            "active": True,
        }
        if node:
            node.write(vals)
            return node
        return node_model.create(vals)

    def action_open_ai_qa(self):
        self.ensure_one()
        action = self.env.ref("coordos_odoo.action_coordos_qa_wizard").read()[0]
        action["context"] = {
            "default_project_id": self.id,
            "default_question": f"{self.name} 项目当前状态与风险如何？",
        }
        return action


class CoordosProjectNode(models.Model):
    _name = "coordos.project.node"
    _description = "CoordOS 项目节点"
    _order = "id desc"

    name = fields.Char("名称", required=True)
    project_id = fields.Many2one("coordos.project", string="项目")
    node_id = fields.Char("项目节点ID", required=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("coordos_project_node_id_uniq", "unique(node_id)", "项目节点ID必须唯一。"),
    ]
