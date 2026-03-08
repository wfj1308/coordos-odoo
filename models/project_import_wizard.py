import base64
import csv
import io
import json
import re
from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError

from .bridge_client import generate_project_tree, get_project_tree


class CoordosProjectImportWizard(models.TransientModel):
    _name = "coordos.project.import.wizard"
    _description = "CoordOS 项目文件导入向导"

    project_name = fields.Char("项目名称", required=True)
    project_code = fields.Char("项目编码", required=True)
    project_usi = fields.Char("项目USI")
    bid_list_file = fields.Binary("中标清单（Excel/CSV）", attachment=True)
    bid_list_filename = fields.Char()
    drawings_file = fields.Binary("图纸（PDF）", attachment=True)
    drawings_filename = fields.Char()
    contract_file = fields.Binary("合同（PDF/Word）", attachment=True)
    contract_filename = fields.Char()
    parsed_data = fields.Text("解析预览", readonly=True)
    drawings_specs_json = fields.Text("图纸解析(JSON)", readonly=True)
    contract_terms_json = fields.Text("合同解析(JSON)", readonly=True)
    import_payload_json = fields.Text("生成载荷(JSON)", readonly=True)
    generated_summary = fields.Text("生成结果", readonly=True)
    template_id = fields.Many2one("coordos.trip.template.config", string="默认行程模板")
    generate_spu_drafts = fields.Boolean("自动生成 SPU 草稿", default=True)
    register_spu_to_core = fields.Boolean("立即注册 SPU 到 Core", default=False)
    create_initial_trip_registration = fields.Boolean("创建初始 Trip 注册单", default=True)
    create_trip_chain_registrations = fields.Boolean("创建 Execute/Evidence/Certify 链任务", default=True)

    @api.onchange("project_code")
    def _onchange_project_code(self):
        if self.project_code and not self.project_usi:
            self.project_usi = f"v://{self.project_code.lower()}"

    @api.onchange("bid_list_file", "drawings_file", "contract_file", "project_name", "project_code")
    def _onchange_files(self):
        preview = []
        bid_items = self._parse_bid_list_items()
        drawings_specs = self._parse_drawings_specs()
        contract_terms = self._parse_contract_terms()
        preview.append(f"项目: {self.project_name or '-'} / {self.project_code or '-'}")
        preview.append(f"清单项: {len(bid_items)}")
        if bid_items:
            sample = bid_items[0]
            preview.append(
                f"清单示例: item_code={sample.get('item_code')} total={sample.get('total')} unit={sample.get('unit')}"
            )
        preview.append(self._describe_binary_file("图纸", self.drawings_file, self.drawings_filename))
        preview.append(self._describe_binary_file("合同", self.contract_file, self.contract_filename))
        preview.append(
            f"图纸关键: 混凝土={drawings_specs.get('concrete_grade', '-')}, 钢筋={drawings_specs.get('rebar_type', '-')}, 桩径={drawings_specs.get('diameter_mm', '-')}"
        )
        preview.append(
            f"合同关键: 工期={contract_terms.get('duration_months', '-') }月, 预算={contract_terms.get('budget', '-')}, 违约金={contract_terms.get('penalty_ratio', '-')}"
        )

        payload = self._build_project_tree_payload(bid_items)
        self.import_payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
        self.drawings_specs_json = json.dumps(drawings_specs, ensure_ascii=False, indent=2)
        self.contract_terms_json = json.dumps(contract_terms, ensure_ascii=False, indent=2)
        self.parsed_data = "\n".join(line for line in preview if line)

    def _decode_binary(self, value):
        if not value:
            return b""
        return base64.b64decode(value)

    def _describe_binary_file(self, label, value, filename):
        if not value:
            return f"{label}: 未上传"
        size = len(self._decode_binary(value))
        return f"{label}: {filename or '未命名文件'} ({size} bytes)"

    def _parse_bid_list_items(self):
        if not self.bid_list_file:
            return []
        filename = (self.bid_list_filename or "").lower()
        raw = self._decode_binary(self.bid_list_file)
        if filename.endswith(".csv"):
            return self._parse_csv_items(raw)
        if filename.endswith(".xlsx"):
            items = self._parse_xlsx_items(raw)
            if items:
                return items
        return []

    def _parse_csv_items(self, raw):
        encodings = ["utf-8-sig", "gbk", "utf-8"]
        headers_map = {
            "item_code": {"item_code", "编码", "清单编码", "定额编码"},
            "total": {"total", "数量", "工程量", "合计"},
            "remaining": {"remaining", "剩余", "未完成"},
            "unit": {"unit", "单位"},
        }
        for enc in encodings:
            try:
                text = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            rows = list(csv.DictReader(io.StringIO(text)))
            result = []
            for row in rows:
                normalized = {}
                for key, aliases in headers_map.items():
                    for src in aliases:
                        if src in row and row[src] not in (None, ""):
                            normalized[key] = row[src]
                            break
                if not normalized.get("item_code"):
                    continue
                total = self._to_float(normalized.get("total"), default=0)
                remaining = self._to_float(normalized.get("remaining"), default=total)
                result.append(
                    {
                        "item_code": str(normalized.get("item_code")).strip(),
                        "total": total,
                        "remaining": remaining,
                        "unit": str(normalized.get("unit") or "项").strip(),
                    }
                )
            if result:
                return result
        return []

    def _parse_xlsx_items(self, raw):
        try:
            import openpyxl
        except Exception:
            return []
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheet = wb.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(col).strip() if col is not None else "" for col in rows[0]]
        idx = {}
        for i, header in enumerate(headers):
            h = header.lower()
            if h in {"item_code", "编码", "清单编码", "定额编码"}:
                idx["item_code"] = i
            if h in {"total", "数量", "工程量", "合计"}:
                idx["total"] = i
            if h in {"remaining", "剩余", "未完成"}:
                idx["remaining"] = i
            if h in {"unit", "单位"}:
                idx["unit"] = i
        result = []
        for row in rows[1:]:
            code_idx = idx.get("item_code")
            if code_idx is None:
                continue
            item_code = row[code_idx] if code_idx < len(row) else None
            if not item_code:
                continue
            total = self._to_float(row[idx.get("total")] if idx.get("total") is not None and idx.get("total") < len(row) else 0)
            remaining = self._to_float(
                row[idx.get("remaining")] if idx.get("remaining") is not None and idx.get("remaining") < len(row) else total,
                default=total,
            )
            unit = row[idx.get("unit")] if idx.get("unit") is not None and idx.get("unit") < len(row) else "项"
            result.append(
                {
                    "item_code": str(item_code).strip(),
                    "total": total,
                    "remaining": remaining,
                    "unit": str(unit or "项").strip(),
                }
            )
        return result

    def _extract_pdf_text(self, binary_value):
        raw = self._decode_binary(binary_value)
        if not raw:
            return ""
        try:
            from pypdf import PdfReader
        except Exception:
            try:
                from PyPDF2 import PdfReader
            except Exception:
                return ""
        try:
            reader = PdfReader(io.BytesIO(raw))
            chunks = []
            for page in reader.pages[:8]:
                text = page.extract_text() or ""
                if text:
                    chunks.append(text)
            return "\n".join(chunks)
        except Exception:
            return ""

    def _first_regex_value(self, text, patterns, default=None, cast=None):
        if not text:
            return default
        for pattern in patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if not m:
                continue
            value = m.group(1).strip()
            if cast:
                try:
                    return cast(value)
                except Exception:
                    continue
            return value
        return default

    def _parse_drawings_specs(self):
        text = self._extract_pdf_text(self.drawings_file)
        profile_result = self.env["coordos.parser.profile"].parse_with_active("drawing", text)
        if isinstance(profile_result, dict) and profile_result:
            return {
                "concrete_grade": profile_result.get("concrete_grade") or profile_result.get("concreteGrade") or "C40",
                "rebar_type": profile_result.get("rebar_type") or profile_result.get("rebarType") or "HRB400",
                "diameter_mm": self._to_float(profile_result.get("diameter_mm") or profile_result.get("diameterMm"), default=1500),
                "depth_m": self._to_float(profile_result.get("depth_m") or profile_result.get("depthM"), default=32),
            }
        specs = {
            "concrete_grade": self._first_regex_value(text, [r"(C\d{2,3})"], default="C40"),
            "rebar_type": self._first_regex_value(text, [r"(HRB\d{3,4}|HPB\d{3,4})"], default="HRB400"),
            "diameter_mm": self._first_regex_value(text, [r"桩径[：:\s]*([0-9]{3,4})", r"diameter[=:\s]*([0-9]{3,4})"], default=1500, cast=float),
            "depth_m": self._first_regex_value(text, [r"桩深[：:\s]*([0-9]+(?:\.[0-9]+)?)", r"depth[=:\s]*([0-9]+(?:\.[0-9]+)?)"], default=32, cast=float),
        }
        return specs

    def _parse_contract_terms(self):
        text = self._extract_pdf_text(self.contract_file)
        profile_result = self.env["coordos.parser.profile"].parse_with_active("contract", text)
        if isinstance(profile_result, dict) and profile_result:
            return {
                "budget": self._to_float(profile_result.get("budget"), default=0),
                "duration_months": int(self._to_float(profile_result.get("duration_months") or profile_result.get("durationMonths"), default=9)),
                "penalty_ratio": profile_result.get("penalty_ratio") or profile_result.get("penaltyRatio") or "5%",
            }
        budget = self._first_regex_value(
            text,
            [r"预算[：:\s]*([0-9]+(?:\.[0-9]+)?)", r"合同价[：:\s]*([0-9]+(?:\.[0-9]+)?)"],
            default=0,
            cast=float,
        )
        duration = self._first_regex_value(
            text,
            [r"工期[：:\s]*([0-9]+)\s*个?月", r"duration[=:\s]*([0-9]+)"],
            default=9,
            cast=int,
        )
        penalty = self._first_regex_value(
            text,
            [r"违约金[：:\s]*([0-9]+(?:\.[0-9]+)?%)", r"penalty[=:\s]*([0-9]+(?:\.[0-9]+)?%)"],
            default="5%",
        )
        return {
            "budget": budget,
            "duration_months": duration,
            "penalty_ratio": penalty,
        }

    def _build_quality_plan(self, bid_items, drawings_specs):
        checkpoints = []
        for item in bid_items[:10]:
            checkpoints.append(
                {
                    "item_code": item.get("item_code"),
                    "target": f"{item.get('total')} {item.get('unit')}",
                    "criteria": drawings_specs.get("concrete_grade") or "按规范",
                }
            )
        if not checkpoints:
            checkpoints = [
                {"item_code": "pile", "target": "100%", "criteria": drawings_specs.get("concrete_grade", "C40")}
            ]
        return {
            "plan_type": "quality",
            "checkpoints": checkpoints,
        }

    def _build_schedule(self, contract_terms):
        months = max(int(contract_terms.get("duration_months") or 9), 1)
        return {
            "plan_type": "schedule",
            "duration_months": months,
            "phases": [
                {"name": "准备", "month_span": [1, 1]},
                {"name": "主体施工", "month_span": [2, max(2, months - 2)]},
                {"name": "收尾验收", "month_span": [max(2, months - 1), months]},
            ],
        }

    def _build_budget(self, bid_items, contract_terms):
        if bid_items:
            lines = []
            total = 0.0
            for item in bid_items:
                amount = float(item.get("total") or 0) * float(item.get("unit_price") or 0)
                total += amount
                lines.append(
                    {
                        "item_code": item.get("item_code"),
                        "total": item.get("total"),
                        "unit": item.get("unit"),
                        "amount": amount,
                    }
                )
            if total <= 0:
                total = float(contract_terms.get("budget") or 0)
            return {"plan_type": "budget", "total": total, "lines": lines}
        return {"plan_type": "budget", "total": float(contract_terms.get("budget") or 0), "lines": []}

    def _build_risk(self, bid_items, drawings_specs, contract_terms):
        risks = []
        depth = float(drawings_specs.get("depth_m") or 0)
        if depth >= 30:
            risks.append({"risk": "深桩施工偏差", "level": "high", "mitigation": "增加过程测量频次"})
        budget = float(contract_terms.get("budget") or 0)
        if budget > 0 and len(bid_items) > 120:
            risks.append({"risk": "清单复杂导致成本偏差", "level": "medium", "mitigation": "周度滚动对比"})
        if not risks:
            risks.append({"risk": "常规施工风险", "level": "low", "mitigation": "按标准工序执行"})
        return {"plan_type": "risk", "items": risks}

    def _to_float(self, value, default=0):
        if value in (None, ""):
            return default
        try:
            return float(value)
        except Exception:
            return default

    def _current_org_code(self):
        return self.env["coordos.api.mixin"].current_org_code()

    def _default_project_usi(self):
        if self.project_usi:
            return self.project_usi
        code = (self.project_code or "").strip().lower()
        if code:
            return f"v://{code}"
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"v://project-{ts}"

    def _build_project_tree_payload(self, bid_items):
        project_usi = self._default_project_usi()
        instances = bid_items[:5]
        if not instances:
            instances = [
                {"item_code": "404-2-1", "total": 156, "remaining": 156, "unit": "m3"},
                {"item_code": "403-1-2", "total": 32.5, "remaining": 32.5, "unit": "t"},
            ]
        return {
            "project_usi": project_usi,
            "structure": {
                "bridges": [
                    {
                        "id": "bridge-001",
                        "name": self.project_name or "1号桥",
                        "piers": [{"id": "pier-1", "instances": instances}],
                    }
                ]
            },
        }

    def _core_base_url(self):
        return self.env["coordos.api.mixin"]._configured_core_base()

    def _ensure_namespace_permission(self):
        policy_model = self.env["coordos.namespace.policy"]
        namespace = (self.project_code or "").strip().lower()
        if not namespace:
            return
        policy = policy_model.search([("active", "=", True), ("namespace_prefix", "=", namespace)], limit=1)
        if policy:
            policy.ensure_user_allowed(self.env.user)

    def _sync_project_nodes(self, project, tree_data):
        node_model = self.env["coordos.project.node"]

        def _upsert(name, node_id):
            existing = node_model.search([("node_id", "=", node_id)], limit=1)
            vals = {"name": name or node_id, "project_id": project.id, "node_id": node_id, "active": True}
            if existing:
                existing.write(vals)
            else:
                node_model.create(vals)

        project_usi = self._default_project_usi()
        root = project.ensure_root_node()
        root_node = root.node_id
        _upsert("项目根节点", root_node)

        data = tree_data.get("data") if isinstance(tree_data, dict) and isinstance(tree_data.get("data"), dict) else tree_data
        if not isinstance(data, dict):
            return

        project_tree = data.get("project_tree") if isinstance(data.get("project_tree"), dict) else data
        structure = project_tree.get("structure") if isinstance(project_tree, dict) else {}
        tree = structure if isinstance(structure, dict) else project_tree
        bridges = tree.get("bridges") if isinstance(tree, dict) else []
        if not isinstance(bridges, list):
            bridges = []
        namespace = (self.project_code or "project").lower()
        for bridge in bridges:
            bridge_id = bridge.get("id")
            if not bridge_id:
                continue
            bridge_node_id = f"v://project-node/{namespace}/{bridge_id}"
            _upsert(bridge.get("name") or bridge_id, bridge_node_id)
            piers = bridge.get("piers") if isinstance(bridge.get("piers"), list) else []
            for pier in piers:
                pier_id = pier.get("id")
                if not pier_id:
                    continue
                pier_node_id = f"v://project-node/{namespace}/{bridge_id}/{pier_id}"
                _upsert(pier_id, pier_node_id)

        try:
            fetched = get_project_tree(project_usi, core_url=self._core_base_url())
            fetched_data = fetched.get("data") if isinstance(fetched, dict) and isinstance(fetched.get("data"), dict) else fetched
            if isinstance(fetched_data, dict):
                root = fetched_data.get("project_usi") or fetched_data.get("projectUsi") or project_usi
                _upsert("项目USI", str(root))
        except Exception:
            # 不阻塞主流程
            return

    def _generate_spu_from_bid(self, project, bid_items):
        if not self.generate_spu_drafts:
            return 0
        spu_model = self.env["coordos.spu"]
        count = 0
        for item in bid_items:
            code = item.get("item_code")
            if not code:
                continue
            existed = spu_model.search([("project_id", "=", project.id), ("code", "=", code)], limit=1)
            if existed:
                continue
            vals = {
                "name": f"{project.name}-{code}",
                "category": "qual",
                "code": code,
                "project_id": project.id,
            }
            if self.register_spu_to_core:
                spu_model.create(vals)
            else:
                spu_model.with_context(skip_core_register=True).create(vals)
            count += 1
        return count

    def _create_initial_registration(self, project, root_node_id):
        if not self.create_initial_trip_registration:
            return self.env["coordos.trip.registration"]
        template = self.template_id or self.env["coordos.trip.template.config"].search(
            [("active", "=", True)],
            order="id",
            limit=1,
        )
        if not template:
            return self.env["coordos.trip.registration"]

        org_code = self._current_org_code()
        policy = self.env["coordos.namespace.policy"].match_policy(root_node_id)
        if policy:
            policy.ensure_user_allowed(self.env.user)
            name = policy.build_trip_name(template.code, org_code)
            operator_spu_id = policy.default_operator_spu_id or ""
            state = "pending" if policy.require_approval else "approved"
        else:
            name = f"{template.code}_{org_code}_{fields.Date.today().strftime('%Y%m%d')}"
            operator_spu_id = ""
            state = "approved"

        input_payload = template.default_input_payload()
        payload = {
            "trip_name": name,
            "executor_spu": operator_spu_id,
            "resources_utxo": [],
            "project_node_id": root_node_id,
            "context": input_payload,
            "energy_consumed": 80,
        }
        return self.env["coordos.trip.registration"].create(
            {
                "name": name,
                "state": state,
                "org_code": org_code,
                "project_node_id": root_node_id,
                "trip_template": template.code,
                "operator_spu_id": operator_spu_id,
                "payload_json": json.dumps(payload, ensure_ascii=False),
            }
        )

    def _create_trip_chain_registrations(self, template, org_code, root_node_id):
        if not self.create_trip_chain_registrations:
            return self.env["coordos.trip.registration"]
        policy = self.env["coordos.namespace.policy"].match_policy(root_node_id)
        if policy:
            policy.ensure_user_allowed(self.env.user)
            operator_spu_id = policy.default_operator_spu_id or ""
            base_state = "pending" if policy.require_approval else "approved"
        else:
            operator_spu_id = ""
            base_state = "approved"

        stages = [
            ("execute", "执行步骤"),
            ("evidence", "上传证据"),
            ("certify", "签发认证"),
        ]
        created = self.env["coordos.trip.registration"]
        for stage_key, stage_name in stages:
            trip_name = (
                policy.build_trip_name(template.code, org_code) + f"_{stage_key}"
                if policy
                else f"{template.code}_{org_code}_{fields.Date.today().strftime('%Y%m%d')}_{stage_key}"
            )
            context_payload = template.default_input_payload()
            context_payload["stage"] = stage_key
            payload = {
                "trip_name": trip_name,
                "executor_spu": operator_spu_id,
                "resources_utxo": [],
                "project_node_id": root_node_id,
                "context": context_payload,
                "energy_consumed": 80,
            }
            reg = self.env["coordos.trip.registration"].create(
                {
                    "name": f"{stage_name}-{trip_name}",
                    "state": base_state,
                    "org_code": org_code,
                    "project_node_id": root_node_id,
                    "trip_template": template.code,
                    "operator_spu_id": operator_spu_id,
                    "payload_json": json.dumps(payload, ensure_ascii=False),
                }
            )
            created |= reg
        return created

    def action_confirm_import(self):
        self.ensure_one()
        if not self.bid_list_file:
            raise UserError("至少上传中标清单（Excel/CSV）。")
        self._ensure_namespace_permission()

        bid_items = self._parse_bid_list_items()
        drawings_specs = self._parse_drawings_specs()
        contract_terms = self._parse_contract_terms()
        payload = self._build_project_tree_payload(bid_items)
        core_url = self._core_base_url()

        try:
            generated = generate_project_tree(payload, core_url=core_url)
        except RuntimeError as exc:
            raise UserError(f"项目树生成失败: {exc}") from exc

        project_vals = {
            "name": self.project_name,
            "code": self.project_code,
            "project_usi": payload.get("project_usi"),
            "org_code": self._current_org_code(),
            "x_quality_plan_json": json.dumps(self._build_quality_plan(bid_items, drawings_specs), ensure_ascii=False),
            "x_schedule_json": json.dumps(self._build_schedule(contract_terms), ensure_ascii=False),
            "x_budget_json": json.dumps(self._build_budget(bid_items, contract_terms), ensure_ascii=False),
            "x_risk_json": json.dumps(self._build_risk(bid_items, drawings_specs, contract_terms), ensure_ascii=False),
            "x_generated_at": fields.Datetime.now(),
        }
        project = self.env["coordos.project"].search([("code", "=", self.project_code)], limit=1)
        if project:
            project.write(project_vals)
        else:
            project = self.env["coordos.project"].create(project_vals)

        self._sync_project_nodes(project, generated)
        bid_created = self._generate_spu_from_bid(project, bid_items)
        root_node_id = project.ensure_root_node().node_id
        registration = self._create_initial_registration(project, root_node_id)
        chain_regs = self._create_trip_chain_registrations(
            self.template_id or self.env["coordos.trip.template.config"].search([("active", "=", True)], order="id", limit=1),
            org_code=self._current_org_code(),
            root_node_id=root_node_id,
        )

        org_code = self._current_org_code()
        self.generated_summary = (
            f"导入成功\n"
            f"组织编码: {org_code}\n"
            f"项目USI: {payload.get('project_usi')}\n"
            f"清单项数量: {len(bid_items)}\n"
            f"新增SPU数量: {bid_created}\n"
            f"初始注册单: {(registration.name if registration else '未创建')}\n"
            f"链任务单数量: {len(chain_regs)}\n"
            f"质量计划: 已生成\n"
            f"进度计划: 已生成\n"
            f"预算分解: 已生成\n"
            f"风险评估: 已生成\n"
            f"Core返回: {json.dumps(generated, ensure_ascii=False)[:3000]}"
        )
        self.import_payload_json = json.dumps(payload, ensure_ascii=False, indent=2)

        return {
            "type": "ir.actions.act_window",
            "name": "项目",
            "res_model": "coordos.project",
            "res_id": project.id,
            "view_mode": "form",
            "target": "current",
        }
