import json
import os

import requests

from odoo import api, fields, models


class CoordosParserProfile(models.Model):
    _name = "coordos.parser.profile"
    _description = "CoordOS 解析器配置"

    active = fields.Boolean("启用", default=True)
    name = fields.Char("名称", required=True)
    parser_kind = fields.Selection(
        [
            ("bid", "中标清单"),
            ("drawing", "图纸"),
            ("contract", "合同"),
        ],
        string="解析类型",
        required=True,
    )
    mode = fields.Selection(
        [
            ("local", "本地规则"),
            ("openai_compatible", "OpenAI兼容"),
        ],
        string="模式",
        default="local",
        required=True,
    )
    endpoint = fields.Char("接口地址")
    model_name = fields.Char("模型名")
    api_key = fields.Char("API Key")
    timeout_sec = fields.Integer("超时秒", default=20)
    system_prompt = fields.Text("系统提示词")
    output_schema_json = fields.Text("输出Schema(JSON)")

    _sql_constraints = [
        ("coordos_parser_profile_kind_name_uniq", "unique(parser_kind,name)", "同类型解析器名称必须唯一。"),
    ]

    def _parse_openai_compatible(self, text):
        self.ensure_one()
        endpoint = (self.endpoint or "").strip()
        if not endpoint:
            return {}
        headers = {"Content-Type": "application/json"}
        api_key = (self.api_key or "").strip() or os.getenv("COORDOS_AI_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        schema = {}
        if self.output_schema_json:
            try:
                schema = json.loads(self.output_schema_json)
            except ValueError:
                schema = {}

        payload = {
            "model": self.model_name or os.getenv("COORDOS_AI_MODEL", ""),
            "messages": [
                {"role": "system", "content": self.system_prompt or "Extract structured JSON."},
                {"role": "user", "content": text or ""},
            ],
            "temperature": 0,
        }
        if schema:
            payload["response_format"] = {"type": "json_object"}

        resp = requests.post(endpoint, headers=headers, json=payload, timeout=max(self.timeout_sec or 20, 5))
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        content = (
            (((data.get("choices") or [{}])[0]).get("message") or {}).get("content")
            if isinstance(data, dict)
            else None
        )
        if not content:
            return {}
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}

    def parse_text(self, text):
        self.ensure_one()
        if self.mode == "openai_compatible":
            try:
                return self._parse_openai_compatible(text)
            except Exception:
                return {}
        return {}

    @api.model
    def parse_with_active(self, parser_kind, text):
        profile = self.search([("active", "=", True), ("parser_kind", "=", parser_kind)], order="id", limit=1)
        if not profile:
            return {}
        return profile.parse_text(text)
