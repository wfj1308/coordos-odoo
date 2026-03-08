import json
import re

from odoo import api, fields, models


VERSION_TRACK_FIELDS = [
    "active",
    "sequence",
    "name",
    "code",
    "target_table_type",
    "min_score",
    "keyword_rules",
    "required_keywords",
    "title_regex",
    "field_patterns_json",
    "default_values_json",
    "notes",
]


class CoordosQualityTableTemplate(models.Model):
    _name = "coordos.quality.table.template"
    _description = "通用质检表模板中心"
    _order = "sequence, id"

    active = fields.Boolean("启用", default=True)
    sequence = fields.Integer("优先级", default=10, help="值越小优先级越高。")
    name = fields.Char("模板名称", required=True)
    code = fields.Char("模板编码", required=True)
    target_table_type = fields.Selection(
        [("7", "桥施7"), ("13", "桥施13"), ("other", "通用表单")],
        string="目标类型",
        default="other",
        required=True,
    )
    min_score = fields.Integer("最小命中分", default=1)
    keyword_rules = fields.Text(
        "关键词规则",
        help="每行一个规则；支持 OR：A|B。前缀 file: 表示仅在文件名中匹配。",
    )
    required_keywords = fields.Text("必含关键词", help="每行一个，缺失任意一项则不匹配。")
    title_regex = fields.Char("标题正则", help="从文本提取标题，优先于默认模板名。")
    field_patterns_json = fields.Text(
        "字段提取规则(JSON)",
        help='示例: {"contract_no":{"regex":"合同段[:：]?([^\\n]+)","type":"string"}}',
    )
    default_values_json = fields.Text("默认值(JSON)", help='示例: {"construction_unit":"某某公司"}')
    notes = fields.Text("备注")

    current_version_no = fields.Integer("当前版本", readonly=True, copy=False, default=0)
    last_version_at = fields.Datetime("最近版本时间", readonly=True, copy=False)
    version_ids = fields.One2many(
        "coordos.quality.table.template.version", "template_id", string="版本历史", readonly=True, copy=False
    )

    _sql_constraints = [
        ("coordos_quality_table_template_code_uniq", "unique(code)", "模板编码必须唯一。"),
    ]

    @staticmethod
    def _split_lines(text):
        return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    @staticmethod
    def _safe_json_loads(raw, fallback):
        text = (raw or "").strip()
        if not text:
            return fallback
        try:
            loaded = json.loads(text)
        except Exception:
            return fallback
        return loaded

    @staticmethod
    def _to_bool(value):
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "y", "通过", "是"}:
            return True
        if text in {"0", "false", "no", "n", "未通过", "否"}:
            return False
        return bool(text)

    @classmethod
    def _coerce_value(cls, value, value_type):
        if value in (None, ""):
            return ""
        vt = str(value_type or "string").strip().lower()
        text = str(value).strip()
        if vt == "float":
            matched = re.search(r"-?\d+(?:\.\d+)?", text)
            return float(matched.group(0)) if matched else ""
        if vt == "int":
            matched = re.search(r"-?\d+", text)
            return int(matched.group(0)) if matched else ""
        if vt == "bool":
            return cls._to_bool(text)
        if vt == "date":
            matched = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text)
            if matched:
                return f"{matched.group(1)}-{int(matched.group(2)):02d}-{int(matched.group(3)):02d}"
            return text
        return text

    def _snapshot_payload(self):
        self.ensure_one()
        payload = {}
        for field_name in VERSION_TRACK_FIELDS:
            payload[field_name] = getattr(self, field_name)
        return payload

    def _create_version_snapshot(self, note=None, force=False):
        version_model = self.env["coordos.quality.table.template.version"]
        for rec in self:
            payload = rec._snapshot_payload()
            payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            latest = version_model.search([("template_id", "=", rec.id)], order="version_no desc,id desc", limit=1)
            if latest and not force and (latest.data_json or "") == payload_json:
                if rec.current_version_no != latest.version_no:
                    rec.with_context(skip_version_track=True).write(
                        {"current_version_no": latest.version_no, "last_version_at": latest.create_date}
                    )
                continue

            next_version = (latest.version_no or 0) + 1 if latest else 1
            created = version_model.create(
                {
                    "template_id": rec.id,
                    "version_no": next_version,
                    "note": note or "",
                    "data_json": payload_json,
                }
            )
            rec.with_context(skip_version_track=True).write(
                {"current_version_no": next_version, "last_version_at": created.create_date}
            )
        return True

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._create_version_snapshot(note="初始化版本", force=True)
        return records

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("skip_version_track"):
            return res
        changed = bool(set(vals.keys()) & set(VERSION_TRACK_FIELDS))
        force_snapshot = bool(self.env.context.get("force_version_snapshot"))
        if changed or force_snapshot:
            note = self.env.context.get("version_note") or ""
            self._create_version_snapshot(note=note, force=force_snapshot)
        return res

    def action_create_manual_snapshot(self):
        self._create_version_snapshot(note="手动快照", force=True)
        return True

    def action_open_import_wizard(self):
        action = self.env.ref("coordos_odoo.action_quality_table_template_import_wizard").read()[0]
        return action

    @api.model
    def action_backfill_versions(self):
        records = self.search([])
        for rec in records:
            has_version = self.env["coordos.quality.table.template.version"].search_count([("template_id", "=", rec.id)]) > 0
            if not has_version or rec.current_version_no <= 0:
                rec._create_version_snapshot(note="历史记录补齐", force=True)
        return True

    def _score_text(self, text, file_name):
        self.ensure_one()
        text_l = (text or "").lower()
        file_l = (file_name or "").lower()

        for required in self._split_lines(self.required_keywords):
            candidates = [part.strip().lower() for part in required.split("|") if part.strip()]
            if candidates and not any(c in text_l for c in candidates):
                return 0

        score = 0
        for rule in self._split_lines(self.keyword_rules):
            in_file = rule.lower().startswith("file:")
            source = file_l if in_file else text_l
            pattern = rule[5:] if in_file else rule
            candidates = [part.strip().lower() for part in pattern.split("|") if part.strip()]
            if candidates and any(c in source for c in candidates):
                score += 1
        return score

    def _extract_by_patterns(self, text):
        self.ensure_one()
        result = {}
        raw_patterns = self._safe_json_loads(self.field_patterns_json, {})
        if not isinstance(raw_patterns, dict):
            raw_patterns = {}

        for key, config in raw_patterns.items():
            field_name = str(key or "").strip()
            if not field_name:
                continue

            regex_list = []
            value_type = "string"
            if isinstance(config, str):
                regex_list = [config]
            elif isinstance(config, list):
                regex_list = [str(item) for item in config if str(item).strip()]
            elif isinstance(config, dict):
                value_type = str(config.get("type") or "string")
                single = config.get("regex")
                multi = config.get("regexes")
                if isinstance(single, str) and single.strip():
                    regex_list.append(single)
                if isinstance(multi, list):
                    regex_list.extend([str(item) for item in multi if str(item).strip()])
            else:
                continue

            value = ""
            for pattern in regex_list:
                try:
                    matched = re.search(pattern, text or "", flags=re.IGNORECASE | re.MULTILINE)
                except Exception:
                    continue
                if not matched:
                    continue
                if matched.groups():
                    value = matched.group(1)
                else:
                    value = matched.group(0)
                break

            if value not in (None, ""):
                result[field_name] = self._coerce_value(value, value_type)

        defaults = self._safe_json_loads(self.default_values_json, {})
        if isinstance(defaults, dict):
            for key, value in defaults.items():
                if key not in result:
                    result[str(key)] = value
        return result

    def detect_payload(self, text, file_name):
        self.ensure_one()
        score = self._score_text(text, file_name)
        if score < max(self.min_score or 0, 0):
            return None

        payload = self._extract_by_patterns(text)
        payload["table_type"] = self.target_table_type or "other"
        payload["template_code"] = self.code
        payload["template_name"] = self.name
        payload["template_id"] = self.id
        payload["template_score"] = score
        payload["template_version"] = self.current_version_no

        title = ""
        if self.title_regex:
            try:
                matched = re.search(self.title_regex, text or "", flags=re.IGNORECASE | re.MULTILINE)
            except Exception:
                matched = None
            if matched:
                title = matched.group(1) if matched.groups() else matched.group(0)
        payload["table_title"] = str(title or self.name or "").strip()
        return payload

    @api.model
    def match_and_extract(self, text, file_name):
        candidates = self.search([("active", "=", True)], order="sequence,id")
        best_payload = {}
        best_score = -1
        best_seq = 10 ** 9
        for tpl in candidates:
            payload = tpl.detect_payload(text or "", file_name or "")
            if not payload:
                continue
            score = int(payload.get("template_score") or 0)
            if score > best_score or (score == best_score and tpl.sequence < best_seq):
                best_payload = payload
                best_score = score
                best_seq = tpl.sequence
        return best_payload


class CoordosQualityTableTemplateVersion(models.Model):
    _name = "coordos.quality.table.template.version"
    _description = "质检表模板版本"
    _order = "template_id, version_no desc, id desc"

    template_id = fields.Many2one("coordos.quality.table.template", string="模板", required=True, ondelete="cascade", index=True)
    template_code = fields.Char("模板编码", related="template_id.code", store=True, readonly=True)
    version_no = fields.Integer("版本号", required=True)
    note = fields.Char("变更说明")
    data_json = fields.Text("版本内容(JSON)", required=True)
    create_uid = fields.Many2one("res.users", string="创建人", readonly=True)
    create_date = fields.Datetime("创建时间", readonly=True)
    is_current = fields.Boolean("当前版本", compute="_compute_is_current")

    _sql_constraints = [
        ("coordos_quality_table_template_version_uniq", "unique(template_id,version_no)", "同一模板版本号必须唯一。"),
    ]

    @api.depends("template_id.current_version_no", "version_no")
    def _compute_is_current(self):
        for rec in self:
            rec.is_current = bool(rec.template_id and rec.template_id.current_version_no == rec.version_no)

    def action_restore_version(self):
        for rec in self:
            payload = CoordosQualityTableTemplate._safe_json_loads(rec.data_json, {})
            if not isinstance(payload, dict):
                continue
            vals = {key: payload.get(key) for key in VERSION_TRACK_FIELDS if key in payload}
            if not vals:
                continue
            rec.template_id.with_context(version_note=f"恢复到v{rec.version_no}").write(vals)
        return True
