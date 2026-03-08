import base64
import io
import json
import os
import re
import tempfile
import zipfile
from datetime import date

from odoo import api, fields, models
from odoo.exceptions import UserError


class BridgeTableUploadWizard(models.TransientModel):
    _name = "bridge.table.upload.wizard"
    _description = "Bridge Table Upload Wizard"

    file = fields.Binary(string="Upload File (PDF/Image/Word)")
    file_name = fields.Char("File Name")
    table_type = fields.Selection(
        [
            ("auto", "Auto Detect"),
            ("7", "Bridge Table 7"),
            ("13", "Bridge Table 13"),
            ("other", "Other"),
        ],
        string="Table Type",
        default="auto",
        required=True,
    )
    resolved_table_type = fields.Selection(
        [
            ("7", "Bridge Table 7"),
            ("13", "Bridge Table 13"),
            ("other", "Other"),
        ],
        string="Resolved Table Type",
        readonly=True,
    )
    pile_id = fields.Many2one("bridge.pile", string="Pile")
    auto_submit_core = fields.Boolean("Auto Submit Core (Table 7 only)", default=False)
    strict_auto = fields.Boolean("Strict Auto Pipeline", default=True, readonly=True)

    status = fields.Selection(
        [("draft", "Draft"), ("parsed", "Parsed"), ("generated", "Generated")],
        string="Status",
        default="draft",
        readonly=True,
    )
    ocr_result = fields.Text("OCR Text", readonly=True)
    parsed_data_json = fields.Text("Parsed JSON", readonly=True)
    matched_template_id = fields.Many2one("coordos.quality.table.template", string="Matched Template", readonly=True)
    matched_template_code = fields.Char("Matched Template Code", readonly=True)
    matched_template_name = fields.Char("Matched Template Name", readonly=True)

    usi_path = fields.Char("USI Path", readonly=True)
    usi_full_path = fields.Char("USI Full Path", readonly=True)
    engineering_name = fields.Char("Engineering Name", readonly=True)
    construction_unit = fields.Char("Construction Unit", readonly=True)
    supervision_unit = fields.Char("Supervision Unit", readonly=True)
    contract_no = fields.Char("Contract No", readonly=True)
    bridge_name = fields.Char("Bridge Name", readonly=True)
    pier_name = fields.Char("Pier", readonly=True)
    pile_position = fields.Char("Pile Position", readonly=True)

    pile_ref = fields.Char("Pile Ref")
    check_date = fields.Date("Check Date")

    design_depth = fields.Float("Design Depth")
    actual_drilled_depth = fields.Float("Actual Drilled Depth")
    design_diameter = fields.Float("Design Diameter")
    actual_diameter = fields.Float("Actual Diameter")
    inclination_permille = fields.Float("Inclination Permille")
    hole_detector_passed = fields.Boolean("Hole Detector Passed", default=True)

    design_top_elevation = fields.Float("Design Top Elevation")
    actual_top_elevation = fields.Float("Actual Top Elevation")
    design_x = fields.Float("Design X")
    actual_x = fields.Float("Actual X")
    design_y = fields.Float("Design Y")
    actual_y = fields.Float("Actual Y")
    design_strength = fields.Float("Design Strength")
    actual_strength = fields.Float("Actual Strength")
    integrity_class = fields.Char("Integrity Class")

    evidence_refs = fields.Text("Evidence Refs")
    photo = fields.Binary("Photo")
    photo_name = fields.Char("Photo Name")
    inspector_signature_draw = fields.Binary("Inspector Signature Draw")
    recorder_signature_draw = fields.Binary("Recorder Signature Draw")
    reviewer_signature_draw = fields.Binary("Reviewer Signature Draw")
    construction_signature_draw = fields.Binary("Construction Signature Draw")
    supervisor_signature_draw = fields.Binary("Supervisor Signature Draw")
    inspector_signature_ref = fields.Char("Inspector Signature Ref")
    recorder_signature_ref = fields.Char("Recorder Signature Ref")
    reviewer_signature_ref = fields.Char("Reviewer Signature Ref")
    construction_signature_ref = fields.Char("Construction Signature Ref")
    supervisor_signature_ref = fields.Char("Supervisor Signature Ref")

    generated_trip_shadow_id = fields.Many2one("coordos.trip.shadow", string="Generated Trip")
    generated_model = fields.Char("Generated Model")
    generated_res_id = fields.Integer("Generated Record ID")
    generated_message = fields.Char("Generated Message")

    TABLE7_CN = "\u6865\u65bd7"
    TABLE13_CN = "\u6865\u65bd13"

    @api.model
    def _zh(self, text):
        return text.encode("utf-8").decode("utf-8")

    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        active_model = self.env.context.get("active_model")
        active_id = self.env.context.get("active_id")
        pile_id = vals.get("pile_id") or self.env.context.get("default_pile_id")
        if active_model == "bridge.pile" and active_id and not pile_id:
            pile_id = active_id
        if pile_id:
            pile = self.env["bridge.pile"].browse(pile_id)
            vals["pile_id"] = pile.id
            vals.setdefault("design_depth", pile.design_depth)
            vals.setdefault("design_diameter", pile.design_diameter)
            vals.setdefault("actual_drilled_depth", pile.design_depth)
            vals.setdefault("actual_diameter", pile.design_diameter)
            try:
                vals.setdefault("pile_ref", pile._resolve_pile_ref())
            except Exception:
                vals.setdefault("pile_ref", pile.project_node_id or "")
        return vals

    @api.onchange("pile_id")
    def _onchange_pile_id_fill(self):
        for wizard in self:
            if not wizard.pile_id:
                continue
            wizard.design_depth = wizard.design_depth or wizard.pile_id.design_depth
            wizard.design_diameter = wizard.design_diameter or wizard.pile_id.design_diameter
            wizard.actual_drilled_depth = wizard.actual_drilled_depth or wizard.pile_id.design_depth
            wizard.actual_diameter = wizard.actual_diameter or wizard.pile_id.design_diameter
            if not wizard.pile_ref:
                try:
                    wizard.pile_ref = wizard.pile_id._resolve_pile_ref()
                except Exception:
                    wizard.pile_ref = wizard.pile_id.project_node_id or ""
            wizard._fill_header_from_usi()

    def _reopen_self(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Upload Bridge Table",
            "res_model": "bridge.table.upload.wizard",
            "view_mode": "form",
            "res_id": self.id,
            "target": "new",
        }

    def _decode_source_payload(self):
        self.ensure_one()
        source_data = self.file or self.photo
        source_name = self.file_name or self.photo_name or "upload_image.jpg"
        if not source_data:
            raise UserError("\u8bf7\u5148\u4e0a\u4f20\u6587\u4ef6\u3002")

        suffix = os.path.splitext((source_name or "").lower())[1]
        if suffix == ".doc":
            raise UserError("\u6682\u4e0d\u652f\u6301 .doc \u81ea\u52a8\u89e3\u6790\uff0c\u8bf7\u8f6c\u4e3a .docx \u6216 PDF/\u56fe\u7247\u3002")

        return base64.b64decode(source_data), source_name

    def _resolve_table_type(self, parsed, text, source_name):
        self.ensure_one()
        if self.table_type != "auto":
            resolved = self.table_type
        else:
            resolved = (parsed or {}).get("table_type") or "other"
            if resolved == "other":
                resolved = self._guess_table_type(f"{text}\n{source_name or ''}")
        if resolved not in {"7", "13", "other"}:
            return "other"
        return resolved

    @staticmethod
    def _is_missing_value(value):
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, tuple, set, dict)):
            return not bool(value)
        return False

    @staticmethod
    def _merge_refs(base_refs, extra_refs):
        merged = []
        seen = set()
        for ref in list(base_refs or []) + list(extra_refs or []):
            value = str(ref or "").strip()
            if not value or value in seen:
                continue
            merged.append(value)
            seen.add(value)
        return merged

    def _clear_auto_extracted_fields(self):
        self.ensure_one()
        reset_fields = [
            "resolved_table_type",
            "ocr_result",
            "parsed_data_json",
            "matched_template_id",
            "matched_template_code",
            "matched_template_name",
            "usi_path",
            "usi_full_path",
            "engineering_name",
            "construction_unit",
            "supervision_unit",
            "contract_no",
            "bridge_name",
            "pier_name",
            "pile_position",
            "pile_ref",
            "check_date",
            "design_depth",
            "actual_drilled_depth",
            "design_diameter",
            "actual_diameter",
            "inclination_permille",
            "hole_detector_passed",
            "design_top_elevation",
            "actual_top_elevation",
            "design_x",
            "actual_x",
            "design_y",
            "actual_y",
            "design_strength",
            "actual_strength",
            "integrity_class",
            "evidence_refs",
            "inspector_signature_ref",
            "recorder_signature_ref",
            "reviewer_signature_ref",
            "construction_signature_ref",
            "supervisor_signature_ref",
            "inspector_signature_draw",
            "recorder_signature_draw",
            "reviewer_signature_draw",
            "construction_signature_draw",
            "supervisor_signature_draw",
            "generated_trip_shadow_id",
            "generated_model",
            "generated_res_id",
            "generated_message",
        ]
        for field_name in reset_fields:
            field = self._fields.get(field_name)
            if not field:
                continue
            if field.type in {"many2one", "float", "integer", "date", "datetime", "boolean", "binary"}:
                setattr(self, field_name, False)
            else:
                setattr(self, field_name, "")
        self.status = "draft"

    def _apply_parsed_result(self, resolved, text, data):
        self.ensure_one()
        parsed = self._sanitize_obj(data or {})
        self._clear_auto_extracted_fields()

        self.resolved_table_type = resolved
        self.ocr_result = self._clean_text(text)[:50000]
        self.parsed_data_json = self._clean_text(json.dumps(parsed, ensure_ascii=False, indent=2))
        self.matched_template_id = parsed.get("template_id") or False
        self.matched_template_code = parsed.get("template_code") or ""
        self.matched_template_name = parsed.get("template_name") or ""

        if parsed.get("pile_ref"):
            self.pile_ref = parsed["pile_ref"]
        self._try_resolve_pile_from_ref()
        if parsed.get("check_date"):
            self.check_date = parsed["check_date"]

        for key in [
            "design_depth",
            "actual_drilled_depth",
            "design_diameter",
            "actual_diameter",
            "inclination_permille",
            "hole_detector_passed",
            "design_top_elevation",
            "actual_top_elevation",
            "design_x",
            "actual_x",
            "design_y",
            "actual_y",
            "design_strength",
            "actual_strength",
            "integrity_class",
            "inspector_signature_ref",
            "recorder_signature_ref",
            "reviewer_signature_ref",
            "construction_signature_ref",
            "supervisor_signature_ref",
        ]:
            if key in parsed and parsed.get(key) not in (None, ""):
                setattr(self, key, parsed.get(key))

        if parsed.get("evidence"):
            self.evidence_refs = ",".join([self._clean_text(v) for v in parsed["evidence"] if self._clean_text(v)])

        if self.pile_id:
            if not self.pile_ref:
                try:
                    self.pile_ref = self.pile_id._resolve_pile_ref()
                except Exception:
                    self.pile_ref = self.pile_id.project_node_id or ""
            if resolved == "7":
                if not self.design_depth:
                    self.design_depth = self.pile_id.design_depth
                if not self.design_diameter:
                    self.design_diameter = self.pile_id.design_diameter
                if not self.actual_drilled_depth:
                    self.actual_drilled_depth = self.design_depth
                if not self.actual_diameter:
                    self.actual_diameter = self.design_diameter

        self._fill_header_from_usi()
        self.status = "parsed"
        return parsed

    def _run_auto_parse(self):
        self.ensure_one()
        file_data, source_name = self._decode_source_payload()
        text, data = self._ocr_and_extract_table(file_data, source_name or "")
        resolved = self._resolve_table_type(data, text, source_name)
        parsed = self._apply_parsed_result(resolved, text, data)
        return resolved, parsed

    def _parsed_payload(self):
        self.ensure_one()
        raw = (self.parsed_data_json or "").strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _strict_validate_auto_pipeline(self, resolved_type):
        self.ensure_one()
        missing = []

        if self._is_missing_value(self.ocr_result):
            missing.append("OCR\u6587\u672c")
        if self._is_missing_value(self.pile_ref):
            missing.append("\u6869\u4f4d\u5f15\u7528(pile_ref)")
        if self._is_missing_value(self.usi_path):
            missing.append("USI\u8def\u5f84")

        if resolved_type in {"7", "13"} and not self.pile_id:
            missing.append("\u5173\u8054\u6869\u57fa")

        if resolved_type == "7":
            for label, value in [
                ("\u68c0\u67e5\u65e5\u671f", self.check_date),
                ("\u5e94\u94bb\u6df1\u5ea6", self.design_depth),
                ("\u5b9e\u94bb\u6df1\u5ea6", self.actual_drilled_depth),
                ("\u8bbe\u8ba1\u6869\u5f84", self.design_diameter),
                ("\u6210\u5b54\u76f4\u5f84", self.actual_diameter),
                ("\u503e\u659c\u5ea6", self.inclination_permille),
            ]:
                if self._is_missing_value(value):
                    missing.append(label)
        elif resolved_type == "13":
            for label, value in [
                ("\u68c0\u67e5\u65e5\u671f", self.check_date),
                ("\u8bbe\u8ba1\u6869\u9876\u9ad8\u7a0b", self.design_top_elevation),
                ("\u5b9e\u6d4b\u6869\u9876\u9ad8\u7a0b", self.actual_top_elevation),
                ("\u8bbe\u8ba1X", self.design_x),
                ("\u5b9e\u6d4bX", self.actual_x),
                ("\u8bbe\u8ba1Y", self.design_y),
                ("\u5b9e\u6d4bY", self.actual_y),
                ("\u8bbe\u8ba1\u5f3a\u5ea6", self.design_strength),
                ("\u5b9e\u6d4b\u5f3a\u5ea6", self.actual_strength),
                ("\u5b8c\u6574\u6027\u7b49\u7ea7", self.integrity_class),
            ]:
                if self._is_missing_value(value):
                    missing.append(label)
        else:
            if not self.matched_template_id:
                missing.append("\u547d\u4e2d\u6a21\u677f(\u8d28\u68c0\u8868\u6a21\u677f\u4e2d\u5fc3)")
            parsed = self._parsed_payload()
            generic_fields = parsed.get("generic_fields")
            if not isinstance(generic_fields, dict) or not generic_fields:
                missing.append("\u901a\u7528\u5b57\u6bb5\u8bc6\u522b\u7ed3\u679c(generic_fields)")

        if missing:
            raise UserError(
                "\u4e25\u683c\u81ea\u52a8\u6d41\u7a0b\u6821\u9a8c\u5931\u8d25\uff0c\u7f3a\u5c11\u5173\u952e\u4fe1\u606f\uff1a\n- "
                + "\n- ".join(missing)
                + "\n\n\u8bf7\u5148\u5728\u300c\u8d28\u68c0\u8868\u6a21\u677f\u4e2d\u5fc3\u300d\u5b8c\u5584\u8bc6\u522b\u6a21\u677f\u540e\u91cd\u8bd5\u3002"
            )

    def _create_output_pdf_attachment(self, record, resolved_type):
        self.ensure_one()
        if not record:
            return None
        report_xmlid = {
            "7": "coordos_odoo.action_report_bridge_table7",
            "13": "coordos_odoo.action_report_bridge_table13",
            "other": "coordos_odoo.action_report_quality_table_generic",
        }.get(resolved_type or "other")
        if not report_xmlid:
            return None

        report = self.env.ref(report_xmlid, raise_if_not_found=False)
        if not report:
            return None
        try:
            pdf_bytes, _ = report._render_qweb_pdf(record.ids)
        except Exception:
            return None
        if not pdf_bytes:
            return None

        file_name = f"{record._name.replace('.', '_')}_{record.id}.pdf"
        return self.env["ir.attachment"].create(
            {
                "name": file_name,
                "datas": base64.b64encode(pdf_bytes),
                "mimetype": "application/pdf",
                "res_model": record._name,
                "res_id": record.id,
            }
        )

    @staticmethod
    def _split_refs(raw):
        values = []
        for item in (raw or "").replace("\r", "\n").replace(",", "\n").split("\n"):
            part = (item or "").strip()
            if part:
                values.append(part)
        return values

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        return str(value).replace("\x00", "").strip()

    @classmethod
    def _sanitize_obj(cls, value):
        if isinstance(value, dict):
            return {cls._clean_text(k): cls._sanitize_obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._sanitize_obj(v) for v in value]
        if isinstance(value, str):
            return cls._clean_text(value)
        return value

    @staticmethod
    def _extract_first_float(text, patterns):
        for pattern in patterns:
            matched = re.search(pattern, text, flags=re.IGNORECASE)
            if matched:
                try:
                    return float(matched.group(1))
                except Exception:
                    continue
        return None

    @staticmethod
    def _extract_date(text):
        matched = re.search(r"(20\d{2})[-/\u5e74](\d{1,2})[-/\u6708](\d{1,2})", text)
        if not matched:
            return None
        try:
            return date(int(matched.group(1)), int(matched.group(2)), int(matched.group(3))).isoformat()
        except Exception:
            return None

    @staticmethod
    def _normalize_for_match(text):
        normalized = (text or "").lower()
        normalized = re.sub(r"\s+", "", normalized)
        normalized = normalized.replace("（", "(").replace("）", ")")
        normalized = normalized.replace("：", ":").replace("，", ",")
        return normalized

    @staticmethod
    def _extract_generic_kv_pairs(text, max_items=200):
        rows = {}
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            matched = re.match(r"^([^\s:：]{1,40})\s*[:：]\s*(.+)$", line)
            if not matched:
                continue
            key = matched.group(1).strip()
            value = matched.group(2).strip()
            if not key or not value:
                continue
            rows[key] = value
            if len(rows) >= max_items:
                break
        return rows

    @staticmethod
    def _extract_table_title(text):
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        if not lines:
            return ""
        for line in lines[:20]:
            if 4 <= len(line) <= 60 and ("表" in line or "检查" in line or "检验" in line):
                return line
        return lines[0][:60]

    def _guess_table_type(self, text):
        normalized = self._normalize_for_match(text)
        rules7 = [
            "\u6865\u65bd7",
            "\u6865\u65bd\u88687",
            "\u6210\u5b54\u68c0\u67e5",
            "\u6869\u57fa\u6210\u5b54",
            "\u68c0\u5b54\u5668",
            "\u5b9e\u94bb\u6df1\u5ea6",
            "\u5e94\u94bb\u6df1\u5ea6",
            "\u503e\u659c\u5ea6",
        ]
        rules13 = [
            "\u6865\u65bd13",
            "\u6865\u65bd\u886813",
            "\u6210\u6869\u68c0\u67e5",
            "\u5b8c\u6574\u6027\u7b49\u7ea7",
            "\u8bbe\u8ba1\u6869\u9876\u9ad8\u7a0b",
            "\u5b9e\u6d4b\u6869\u9876\u9ad8\u7a0b",
            "\u8bbe\u8ba1\u5f3a\u5ea6",
        ]
        if "\u6865\u65bd7" in normalized or "\u6865\u65bd\u88687" in normalized:
            return "7"
        if "\u6865\u65bd13" in normalized or "\u6865\u65bd\u886813" in normalized:
            return "13"
        score7 = sum(1 for kw in rules7 if kw in normalized)
        score13 = sum(1 for kw in rules13 if kw in normalized)
        if score7 >= 2 and score7 > score13:
            return "7"
        if score13 >= 2 and score13 > score7:
            return "13"
        return "other"

    @staticmethod
    def _module_available(module_name):
        try:
            import importlib.util
            return bool(importlib.util.find_spec(module_name))
        except Exception:
            return False

    @staticmethod
    def _is_likely_binary_suffix(suffix):
        return suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".pdf"}

    @staticmethod
    def _is_probably_binary_content(raw_bytes):
        if not raw_bytes:
            return False
        if b"\x00" in raw_bytes:
            return True
        sample = raw_bytes[:4096]
        non_printable = sum(1 for b in sample if b < 9 or (13 < b < 32))
        return (non_printable / max(len(sample), 1)) > 0.25

    @staticmethod
    def _guess_mimetype(file_name, default_type="application/octet-stream"):
        lower = (file_name or "").lower()
        if lower.endswith(".png"):
            return "image/png"
        if lower.endswith(".jpg") or lower.endswith(".jpeg"):
            return "image/jpeg"
        if lower.endswith(".pdf"):
            return "application/pdf"
        if lower.endswith(".docx"):
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return default_type

    def _create_attachment(self, target_model, target_id, datas, name, mimetype=None):
        self.ensure_one()
        if not datas:
            return None
        return self.env["ir.attachment"].create(
            {
                "name": name or f"upload_{fields.Datetime.now()}",
                "datas": datas,
                "res_model": target_model,
                "res_id": target_id,
                "mimetype": mimetype or self._guess_mimetype(name),
            }
        )

    def _try_resolve_pile_from_ref(self):
        self.ensure_one()
        if self.pile_id or not self.pile_ref:
            return
        ref = self._clean_text(self.pile_ref)
        tail = ref.rstrip("/").split("/")[-1] if "/" in ref else ref
        domain = ["|", "|", ("project_node_id", "=", ref), ("spu_id.x_core_usi", "=", ref), ("name", "=", tail)]
        pile = self.env["bridge.pile"].search(domain, limit=1)
        if pile:
            self.pile_id = pile

    def _ocr_and_extract_table(self, file_data, file_name):
        filename = (file_name or "").lower()
        suffix = os.path.splitext(filename)[1]
        text_parts = []

        if suffix == ".docx":
            try:
                with zipfile.ZipFile(io.BytesIO(file_data)) as zf:
                    for xml_name in ("word/document.xml", "word/header1.xml", "word/header2.xml", "word/footer1.xml"):
                        if xml_name in zf.namelist():
                            content = zf.read(xml_name).decode("utf-8", errors="ignore")
                            text_parts.extend(re.findall(r"<w:t[^>]*>(.*?)</w:t>", content))
            except Exception:
                pass

        if suffix == ".pdf":
            try:
                import fitz
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(file_data)
                    tmp_path = tmp.name
                try:
                    doc = fitz.open(tmp_path)
                    for page in doc[:10]:
                        page_text = page.get_text("text") or ""
                        if page_text:
                            text_parts.append(page_text)
                finally:
                    os.unlink(tmp_path)
            except Exception:
                pass

        if not text_parts and suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}:
            with tempfile.NamedTemporaryFile(suffix=suffix or ".png", delete=False) as tmp:
                tmp.write(file_data)
                tmp_path = tmp.name
            try:
                if self._module_available("paddleocr"):
                    try:
                        from paddleocr import PaddleOCR
                        ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
                        result = ocr.ocr(tmp_path, cls=True) or []
                        for line in result:
                            for item in line:
                                if isinstance(item, (list, tuple)) and len(item) >= 2:
                                    value = item[1][0] if isinstance(item[1], (list, tuple)) else ""
                                    if value:
                                        text_parts.append(str(value))
                    except Exception:
                        pass

                if not text_parts and self._module_available("pytesseract"):
                    try:
                        import pytesseract
                        from PIL import Image
                        with Image.open(tmp_path) as img:
                            text = pytesseract.image_to_string(img, lang="chi_sim+eng") or ""
                        if text.strip():
                            text_parts.append(text)
                    except Exception:
                        pass
            finally:
                os.unlink(tmp_path)

        if not text_parts and not self._is_likely_binary_suffix(suffix):
            if not self._is_probably_binary_content(file_data):
                decoded = file_data.decode("utf-8", errors="ignore")
                if decoded:
                    text_parts.append(decoded)

        cleaned_parts = [self._clean_text(part) for part in text_parts if self._clean_text(part)]
        text = "\n".join(cleaned_parts).strip()
        data = self._extract_fields_from_text(text, file_name)
        return self._clean_text(text), self._sanitize_obj(data)

    def _extract_fields_from_text(self, text, file_name=None):
        normalized = self._clean_text(text)
        normalized_no_space = self._normalize_for_match(normalized)
        data = {}

        template_payload = self.env["coordos.quality.table.template"].match_and_extract(normalized, file_name or "")
        if isinstance(template_payload, dict):
            data.update(self._sanitize_obj(template_payload))

        try:
            ai_payload = self.env["coordos.parser.profile"].parse_with_active("quality_table", normalized)
        except Exception:
            ai_payload = {}
        if isinstance(ai_payload, dict):
            for key, value in self._sanitize_obj(ai_payload).items():
                if key not in data or data.get(key) in (None, "", []):
                    data[key] = value

        def put_if_empty(key, value):
            if value in (None, ""):
                return
            if key not in data or data.get(key) in (None, "", []):
                data[key] = value

        pile_ref_match = re.search(r"v://[^\s,\]\[\"']+", normalized)
        if pile_ref_match:
            put_if_empty("pile_ref", pile_ref_match.group(0))

        put_if_empty("table_type", self._guess_table_type(normalized))
        put_if_empty("table_title", self._extract_table_title(normalized))
        put_if_empty("check_date", self._extract_date(normalized))

        put_if_empty(
            "design_depth",
            self._extract_first_float(normalized_no_space, [r"(?:\u5e94\u94bb\u6df1\u5ea6|design_depth)[:：]?\s*(-?\d+(?:\.\d+)?)"]),
        )
        put_if_empty(
            "actual_drilled_depth",
            self._extract_first_float(normalized_no_space, [r"(?:\u5b9e\u94bb\u6df1\u5ea6|actual_drilled_depth)[:：]?\s*(-?\d+(?:\.\d+)?)"]),
        )
        put_if_empty(
            "design_diameter",
            self._extract_first_float(normalized_no_space, [r"(?:\u8bbe\u8ba1\u6869\u5f84|design_diameter)[:：]?\s*(-?\d+(?:\.\d+)?)"]),
        )
        put_if_empty(
            "actual_diameter",
            self._extract_first_float(normalized_no_space, [r"(?:\u6210\u5b54\u76f4\u5f84|actual_diameter)[:：]?\s*(-?\d+(?:\.\d+)?)"]),
        )
        put_if_empty(
            "inclination_permille",
            self._extract_first_float(normalized_no_space, [r"(?:\u503e\u659c\u5ea6|inclination_permille)[:：]?\s*(-?\d+(?:\.\d+)?)"]),
        )

        if re.search(r"(\u68c0\u5b54\u5668\u901a\u8fc7|hole_detector_passed[:：=]?true)", normalized_no_space, flags=re.IGNORECASE):
            put_if_empty("hole_detector_passed", True)
        elif re.search(r"(\u68c0\u5b54\u5668\u672a\u901a\u8fc7|hole_detector_passed[:：=]?false)", normalized_no_space, flags=re.IGNORECASE):
            put_if_empty("hole_detector_passed", False)

        put_if_empty(
            "design_top_elevation",
            self._extract_first_float(normalized_no_space, [r"(?:\u8bbe\u8ba1\u6869\u9876\u9ad8\u7a0b|design_top_elevation)[:：]?\s*(-?\d+(?:\.\d+)?)"]),
        )
        put_if_empty(
            "actual_top_elevation",
            self._extract_first_float(normalized_no_space, [r"(?:\u5b9e\u6d4b\u6869\u9876\u9ad8\u7a0b|actual_top_elevation)[:：]?\s*(-?\d+(?:\.\d+)?)"]),
        )
        put_if_empty("design_x", self._extract_first_float(normalized_no_space, [r"(?:design_x)[:：]?\s*(-?\d+(?:\.\d+)?)"]))
        put_if_empty("actual_x", self._extract_first_float(normalized_no_space, [r"(?:actual_x)[:：]?\s*(-?\d+(?:\.\d+)?)"]))
        put_if_empty("design_y", self._extract_first_float(normalized_no_space, [r"(?:design_y)[:：]?\s*(-?\d+(?:\.\d+)?)"]))
        put_if_empty("actual_y", self._extract_first_float(normalized_no_space, [r"(?:actual_y)[:：]?\s*(-?\d+(?:\.\d+)?)"]))
        put_if_empty(
            "design_strength",
            self._extract_first_float(normalized_no_space, [r"(?:\u8bbe\u8ba1\u5f3a\u5ea6|design_strength)[:：]?\s*(-?\d+(?:\.\d+)?)"]),
        )
        put_if_empty(
            "actual_strength",
            self._extract_first_float(normalized_no_space, [r"(?:\u5b9e\u6d4b\u5f3a\u5ea6|actual_strength)[:：]?\s*(-?\d+(?:\.\d+)?)"]),
        )

        integrity_match = re.search(r"(?:\u5b8c\u6574\u6027\u7b49\u7ea7|integrity_class)\s*[:：]?\s*([A-Za-z0-9\u4e00-\u9fa5IVX]+)", normalized)
        if integrity_match:
            put_if_empty("integrity_class", integrity_match.group(1).strip())

        refs = re.findall(r"(?:photo|doc|report|v|attachment)://[^\s,\]\[\"']+", normalized, flags=re.IGNORECASE)
        if refs:
            data["evidence"] = refs

        sigs = re.findall(r"sig:[A-Za-z0-9_-]+", normalized)
        if sigs:
            put_if_empty("inspector_signature_ref", sigs[0])
            if len(sigs) > 1:
                put_if_empty("recorder_signature_ref", sigs[1])
            if len(sigs) > 2:
                put_if_empty("reviewer_signature_ref", sigs[2])
            if len(sigs) > 3:
                put_if_empty("construction_signature_ref", sigs[3])
            if len(sigs) > 4:
                put_if_empty("supervisor_signature_ref", sigs[4])

        generic_fields = self._extract_generic_kv_pairs(normalized)
        if generic_fields:
            existed = data.get("generic_fields")
            if isinstance(existed, dict):
                merged = dict(existed)
                for k, v in generic_fields.items():
                    if k not in merged or merged.get(k) in (None, ""):
                        merged[k] = v
                data["generic_fields"] = merged
            else:
                data["generic_fields"] = generic_fields

        return self._sanitize_obj(data)

    def _fill_header_from_usi(self):
        self.ensure_one()
        if not self.pile_id and self.pile_ref:
            self._try_resolve_pile_from_ref()
        if self.pile_id:
            auto_vals = self.pile_id._usi_autofill_values()
            self.usi_path = auto_vals.get("usi_path") or ""
            self.usi_full_path = self.usi_path
            self.engineering_name = auto_vals.get("engineering_name") or ""
            self.construction_unit = auto_vals.get("construction_unit") or ""
            self.supervision_unit = auto_vals.get("supervision_unit") or ""
            self.contract_no = auto_vals.get("contract_no") or ""
            self.bridge_name = auto_vals.get("bridge_name") or ""
            self.pier_name = auto_vals.get("pier_name") or ""
            self.pile_position = auto_vals.get("pile_position") or ""
        elif self.pile_ref and self.pile_ref.startswith("v://"):
            self.usi_path = self.pile_ref
            self.usi_full_path = self.pile_ref

        if not self.check_date:
            self.check_date = fields.Date.today()
        if not self.evidence_refs:
            self.evidence_refs = "photo://hole-1,doc://measure-1"

    def action_parse_only(self):
        self.ensure_one()
        self._run_auto_parse()
        return self._reopen_self()

    def _ensure_parsed(self):
        self.ensure_one()
        if self.status != "parsed":
            self._run_auto_parse()

    def _build_trip_shadow(self, resolved_type, evidence_list):
        self.ensure_one()
        template_map = {"7": "bridge_table7_upload", "13": "bridge_table13_upload", "other": "bridge_table_upload"}
        trip_usi_map = {
            "7": "v://trip/bridge/pile/hole_inspection_upload@1.0.0",
            "13": "v://trip/bridge/pile/final_inspection_upload@1.0.0",
            "other": "v://trip/bridge/table/upload@1.0.0",
        }
        payload = self._sanitize_obj({
            "table_type": resolved_type,
            "pile_ref": self.pile_ref,
            "usi_path": self.usi_path,
            "engineering_name": self.engineering_name,
            "construction_unit": self.construction_unit,
            "supervision_unit": self.supervision_unit,
            "contract_no": self.contract_no,
            "bridge_name": self.bridge_name,
            "pier_name": self.pier_name,
            "pile_position": self.pile_position,
            "template_id": self.matched_template_id.id if self.matched_template_id else False,
            "template_code": self.matched_template_code or "",
            "template_name": self.matched_template_name or "",
            "signatures": {
                "inspector": self.inspector_signature_ref or "",
                "recorder": self.recorder_signature_ref or "",
                "reviewer": self.reviewer_signature_ref or "",
                "construction": self.construction_signature_ref or "",
                "supervisor": self.supervisor_signature_ref or "",
            },
        })
        now_code = fields.Datetime.now().strftime("%Y%m%d%H%M%S")
        vals = {
            "name": f"UPLOAD-{resolved_type}-{now_code}",
            "project_node": self.pile_id.project_node_id if self.pile_id else "",
            "work_id": self.pile_ref or "",
            "trip_template": template_map.get(resolved_type, "bridge_table_upload"),
            "trip_usi": trip_usi_map.get(resolved_type, "v://trip/bridge/table/upload@1.0.0"),
            "input_json": self._clean_text(json.dumps(payload, ensure_ascii=False)),
            "evidence_json": self._clean_text(json.dumps({"evidence": evidence_list}, ensure_ascii=False)),
            "x_status": "uploaded",
        }
        if evidence_list:
            vals["x_evidence_ids"] = self._clean_text(json.dumps([self._clean_text(v) for v in evidence_list], ensure_ascii=False))
        return self.env["coordos.trip.shadow"].create(vals)

    def _create_table7_record(self, evidence_list):
        if not self.pile_id:
            raise UserError("\u751f\u6210\u6865\u65bd7\u6570\u5b57\u8bb0\u5f55\u5fc5\u987b\u5148\u9009\u62e9\u5173\u8054\u6869\u57fa\u3002")
        return self.env["bridge.pile.hole.inspection"].create({
            "pile_id": self.pile_id.id,
            "pile_ref": self.pile_ref or self.pile_id._resolve_pile_ref(),
            "usi_path": self.usi_path,
            "usi_full_path": self.usi_full_path or self.usi_path,
            "engineering_name": self.engineering_name,
            "construction_unit": self.construction_unit,
            "supervision_unit": self.supervision_unit,
            "contract_no": self.contract_no,
            "page_info": "\u7b2c 1 \u9875  \u5171 1 \u9875",
            "bridge_name": self.bridge_name,
            "pier_name": self.pier_name,
            "pile_position": self.pile_position,
            "check_date": self.check_date or fields.Date.today(),
            "design_depth": self.design_depth,
            "actual_drilled_depth": self.actual_drilled_depth,
            "design_diameter": self.design_diameter,
            "actual_diameter": self.actual_diameter,
            "inclination_permille": self.inclination_permille,
            "hole_detector_passed": bool(self.hole_detector_passed),
            "evidence_refs": json.dumps(evidence_list, ensure_ascii=False),
            "inspector_signature_ref": self.inspector_signature_ref or "",
            "recorder_signature_ref": self.recorder_signature_ref or "",
            "reviewer_signature_ref": self.reviewer_signature_ref or "",
            "construction_signature_ref": self.construction_signature_ref or "",
            "supervisor_signature_ref": self.supervisor_signature_ref or "",
        })

    def _create_table13_record(self, evidence_list):
        if not self.pile_id:
            raise UserError("\u751f\u6210\u6865\u65bd13\u6570\u5b57\u8bb0\u5f55\u5fc5\u987b\u5148\u9009\u62e9\u5173\u8054\u6869\u57fa\u3002")
        return self.env["bridge.pile.final.inspection"].create({
            "pile_id": self.pile_id.id,
            "pile_ref": self.pile_ref or self.pile_id._resolve_pile_ref(),
            "check_date": self.check_date or fields.Date.today(),
            "design_top_elevation": self.design_top_elevation,
            "actual_top_elevation": self.actual_top_elevation,
            "design_x": self.design_x,
            "actual_x": self.actual_x,
            "design_y": self.design_y,
            "actual_y": self.actual_y,
            "design_strength": self.design_strength,
            "actual_strength": self.actual_strength,
            "integrity_class": self.integrity_class or "I",
            "evidence_refs": json.dumps(evidence_list, ensure_ascii=False),
            "inspector_signature_ref": self.inspector_signature_ref or "",
            "recorder_signature_ref": self.recorder_signature_ref or "",
            "reviewer_signature_ref": self.reviewer_signature_ref or "",
            "construction_signature_ref": self.construction_signature_ref or "",
            "supervisor_signature_ref": self.supervisor_signature_ref or "",
        })

    def _create_generic_record(self, evidence_list, trip_shadow):
        self.ensure_one()
        parsed_text = (self.parsed_data_json or "").strip()
        try:
            parsed_payload = json.loads(parsed_text) if parsed_text else {}
        except Exception:
            parsed_payload = {}
        if not isinstance(parsed_payload, dict):
            parsed_payload = {"raw": parsed_payload}

        generic_fields = parsed_payload.get("generic_fields")
        if not isinstance(generic_fields, dict):
            generic_fields = {}
        editable_payload = dict(generic_fields)
        for key in [
            "pile_ref",
            "check_date",
            "engineering_name",
            "construction_unit",
            "supervision_unit",
            "contract_no",
            "bridge_name",
            "pier_name",
            "pile_position",
        ]:
            value = parsed_payload.get(key)
            if value not in (None, ""):
                editable_payload[key] = value

        table_title = parsed_payload.get("table_title") or self.file_name or self.photo_name or "通用质检表"
        template_id = parsed_payload.get("template_id") or (self.matched_template_id.id if self.matched_template_id else False)
        template_code = parsed_payload.get("template_code") or self.matched_template_code or ""
        template_version = parsed_payload.get("template_version")
        try:
            template_version = int(template_version) if template_version not in (None, "") else 0
        except Exception:
            template_version = 0
        return self.env["coordos.quality.table.record"].create(
            {
                "table_title": self._clean_text(table_title),
                "table_type_code": self.resolved_table_type or self.table_type or "other",
                "quality_template_id": template_id or False,
                "quality_template_code": template_code,
                "quality_template_version": template_version,
                "source_file_name": self.file_name or self.photo_name or "",
                "trip_shadow_id": trip_shadow.id if trip_shadow else False,
                "pile_id": self.pile_id.id if self.pile_id else False,
                "pile_ref": self.pile_ref or "",
                "check_date": self.check_date or fields.Date.today(),
                "usi_path": self.usi_path or "",
                "usi_full_path": self.usi_full_path or self.usi_path or "",
                "engineering_name": self.engineering_name or "",
                "construction_unit": self.construction_unit or "",
                "supervision_unit": self.supervision_unit or "",
                "contract_no": self.contract_no or "",
                "bridge_name": self.bridge_name or "",
                "pier_name": self.pier_name or "",
                "pile_position": self.pile_position or "",
                "ocr_text": self.ocr_result or "",
                "parsed_data_json": json.dumps(parsed_payload, ensure_ascii=False),
                "editable_data_json": json.dumps(editable_payload, ensure_ascii=False),
                "evidence_refs": json.dumps(evidence_list, ensure_ascii=False),
                "inspector_signature_ref": self.inspector_signature_ref or "",
                "recorder_signature_ref": self.recorder_signature_ref or "",
                "reviewer_signature_ref": self.reviewer_signature_ref or "",
                "construction_signature_ref": self.construction_signature_ref or "",
                "supervisor_signature_ref": self.supervisor_signature_ref or "",
            }
        )

    @staticmethod
    def _parse_evidence_json(raw):
        text = (raw or "").strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return BridgeTableUploadWizard._split_refs(text)

    def _append_refs_to_generated_record(self, generated_record, resolved_type, refs_to_add):
        self.ensure_one()
        refs_to_add = [self._clean_text(v) for v in (refs_to_add or []) if self._clean_text(v)]
        if not generated_record:
            return
        if resolved_type == "7":
            base_refs = generated_record._evidence_refs_as_list()
        elif resolved_type == "13":
            base_refs = self._parse_evidence_json(generated_record.evidence_refs)
        else:
            base_refs = generated_record._split_refs(generated_record.evidence_refs)
        merged = self._merge_refs(base_refs, refs_to_add)
        generated_record.evidence_refs = json.dumps(merged, ensure_ascii=False)

    def _materialize_drawn_signatures(self, target_model, target_id):
        self.ensure_one()
        if self.strict_auto:
            return {}
        refs = {}
        draw_fields = [
            ("inspector_signature_draw", "inspector_signature_ref", "inspector_sign.png"),
            ("recorder_signature_draw", "recorder_signature_ref", "recorder_sign.png"),
            ("reviewer_signature_draw", "reviewer_signature_ref", "reviewer_sign.png"),
            ("construction_signature_draw", "construction_signature_ref", "construction_sign.png"),
            ("supervisor_signature_draw", "supervisor_signature_ref", "supervisor_sign.png"),
        ]
        for draw_field, ref_field, file_name in draw_fields:
            draw_data = getattr(self, draw_field)
            if not draw_data:
                continue
            att = self._create_attachment(target_model, target_id, draw_data, file_name, "image/png")
            if not att:
                continue
            ref = f"attachment://{att.id}"
            refs[ref_field] = ref
            setattr(self, ref_field, ref)
        return refs

    def action_upload_and_process(self):
        self.ensure_one()
        resolved_type, _parsed_payload = self._run_auto_parse()
        self._strict_validate_auto_pipeline(resolved_type)

        if not self.pile_id:
            self._try_resolve_pile_from_ref()
        if resolved_type in {"7", "13"} and not self.pile_id:
            raise UserError("\u6865\u65bd7/13\u6570\u5b57\u5316\u9700\u8981\u5148\u9009\u62e9\u5173\u8054\u6869\u57fa\u3002")

        evidence_list = self._split_refs(self.evidence_refs)
        if not evidence_list:
            evidence_list = [f"upload://{self.file_name or self.photo_name or 'bridge_table'}"]

        trip = self._build_trip_shadow(resolved_type, evidence_list)

        generated_record = None
        if resolved_type == "7":
            generated_record = self._create_table7_record(evidence_list)
        elif resolved_type == "13":
            generated_record = self._create_table13_record(evidence_list)
        else:
            generated_record = self._create_generic_record(evidence_list, trip)

        target_model = generated_record._name if generated_record else "coordos.trip.shadow"
        target_id = generated_record.id if generated_record else trip.id
        source_data = self.file or self.photo
        source_name = self.file_name or self.photo_name or f"bridge_table_{fields.Datetime.now()}.jpg"
        attachment = self._create_attachment(target_model, target_id, source_data, source_name, self._guess_mimetype(source_name))

        extra_refs = []
        if attachment:
            extra_refs.append(f"attachment://{attachment.id}")
            if generated_record and hasattr(generated_record, "source_attachment_id"):
                generated_record.source_attachment_id = attachment.id

        draw_refs = self._materialize_drawn_signatures(target_model, target_id)
        extra_refs.extend(draw_refs.values())

        if generated_record:
            signature_vals = {}
            for field_name in [
                "inspector_signature_ref",
                "recorder_signature_ref",
                "reviewer_signature_ref",
                "construction_signature_ref",
                "supervisor_signature_ref",
            ]:
                value = (draw_refs.get(field_name) or getattr(self, field_name) or "").strip()
                if value:
                    signature_vals[field_name] = value
            if signature_vals:
                generated_record.write(signature_vals)

        pdf_attachment = self._create_output_pdf_attachment(generated_record, resolved_type) if generated_record else None
        if pdf_attachment:
            extra_refs.append(f"attachment://{pdf_attachment.id}")

        if generated_record:
            self._append_refs_to_generated_record(generated_record, resolved_type, extra_refs)

        if generated_record and resolved_type == "7" and self.auto_submit_core:
            generated_record.action_submit_to_core()

        self.generated_trip_shadow_id = trip.id
        self.generated_model = target_model
        self.generated_res_id = target_id
        pdf_message = f", pdf=attachment://{pdf_attachment.id}" if pdf_attachment else ", pdf=none"
        self.generated_message = (
            f"OK: strict-auto pipeline completed (ocr->usi->record->pdf->trace), type={resolved_type}{pdf_message}."
        )
        self.status = "generated"

        return {
            "type": "ir.actions.act_window",
            "name": "Generated",
            "res_model": target_model,
            "view_mode": "form",
            "res_id": target_id,
            "target": "current",
        }
