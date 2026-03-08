from odoo import fields, models


class CoordosTrip(models.Model):
    _name = "coordos.trip"
    _description = "CoordOS 行程"

    name = fields.Char("名称", required=True)
    project_node = fields.Char("项目节点", required=True)
    status = fields.Selection(
        [
            ("planned", "计划中"),
            ("running", "进行中"),
            ("done", "已完成"),
        ],
        string="状态",
        default="planned",
        required=True,
    )

    def action_start(self):
        self.ensure_one()
        payload = {
            "tripTemplate": "pile_construction",
            "projectNode": self.project_node,
            "work_id": self.project_node,
            "tripUsi": "v://trip/engineering/pile@1.1.0",
            "input": {},
        }
        result = self.env["coordos.api.mixin"].trip_start(payload)
        status = result.get("status")
        if not status and result.get("ok"):
            status = "running"
        self.status = status or "running"

    def action_open_launch_wizard(self):
        self.ensure_one()
        action = self.env.ref("coordos_odoo.action_trip_launch_wizard").read()[0]
        action["context"] = {
            "default_trip_id": self.id,
            "default_project_node": self.project_node,
            "default_work_id": self.project_node,
        }
        return action
