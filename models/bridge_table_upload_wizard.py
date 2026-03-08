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

    status = fields.Selection(
        [("draft", "Draft"), ("parsed", "Parsed"), ("generated", "Generated")],
        string="Status",
        default="draft",
        readonly=True,
    )
    ocr_result = fields.Text("OCR Text", readonly=True)
    parsed_data_json = fields.Text("Parsed JSON", readonly=True)

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
        data = self._extract_fields_from_text(text)
        return self._clean_text(text), self._sanitize_obj(data)

    def _extract_fields_from_text(self, text):
        normalized = self._clean_text(text)
        normalized_no_space = self._normalize_for_match(normalized)
        data = {}

        pile_ref_match = re.search(r"v://[^\s,\]\[\"']+", normalized)
        if pile_ref_match:
            data["pile_ref"] = pile_ref_match.group(0)

        data["table_type"] = self._guess_table_type(normalized)
        data["check_date"] = self._extract_date(normalized)

        data["design_depth"] = self._extract_first_float(normalized_no_space, [r"(?:\u5e94\u94bb\u6df1\u5ea6|design_depth)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["actual_drilled_depth"] = self._extract_first_float(normalized_no_space, [r"(?:\u5b9e\u94bb\u6df1\u5ea6|actual_drilled_depth)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["design_diameter"] = self._extract_first_float(normalized_no_space, [r"(?:\u8bbe\u8ba1\u6869\u5f84|design_diameter)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["actual_diameter"] = self._extract_first_float(normalized_no_space, [r"(?:\u6210\u5b54\u76f4\u5f84|actual_diameter)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["inclination_permille"] = self._extract_first_float(normalized_no_space, [r"(?:\u503e\u659c\u5ea6|inclination_permille)[:：]?\s*(-?\d+(?:\.\d+)?)"])

        if re.search(r"(\u68c0\u5b54\u5668\u901a\u8fc7|hole_detector_passed[:：=]?true)", normalized_no_space, flags=re.IGNORECASE):
            data["hole_detector_passed"] = True
        elif re.search(r"(\u68c0\u5b54\u5668\u672a\u901a\u8fc7|hole_detector_passed[:：=]?false)", normalized_no_space, flags=re.IGNORECASE):
            data["hole_detector_passed"] = False

        data["design_top_elevation"] = self._extract_first_float(normalized_no_space, [r"(?:\u8bbe\u8ba1\u6869\u9876\u9ad8\u7a0b|design_top_elevation)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["actual_top_elevation"] = self._extract_first_float(normalized_no_space, [r"(?:\u5b9e\u6d4b\u6869\u9876\u9ad8\u7a0b|actual_top_elevation)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["design_x"] = self._extract_first_float(normalized_no_space, [r"(?:design_x)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["actual_x"] = self._extract_first_float(normalized_no_space, [r"(?:actual_x)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["design_y"] = self._extract_first_float(normalized_no_space, [r"(?:design_y)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["actual_y"] = self._extract_first_float(normalized_no_space, [r"(?:actual_y)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["design_strength"] = self._extract_first_float(normalized_no_space, [r"(?:\u8bbe\u8ba1\u5f3a\u5ea6|design_strength)[:：]?\s*(-?\d+(?:\.\d+)?)"])
        data["actual_strength"] = self._extract_first_float(normalized_no_space, [r"(?:\u5b9e\u6d4b\u5f3a\u5ea6|actual_strength)[:：]?\s*(-?\d+(?:\.\d+)?)"])

        integrity_match = re.search(r"(?:\u5b8c\u6574\u6027\u7b49\u7ea7|integrity_class)\s*[:：]?\s*([A-Za-z0-9\u4e00-\u9fa5IVX]+)", normalized)
        if integrity_match:
            data["integrity_class"] = integrity_match.group(1).strip()

        refs = re.findall(r"(?:photo|doc|report|v|attachment)://[^\s,\]\[\"']+", normalized, flags=re.IGNORECASE)
        if refs:
            data["evidence"] = refs

        sigs = re.findall(r"sig:[A-Za-z0-9_-]+", normalized)
        if sigs:
            data["inspector_signature_ref"] = sigs[0]
            if len(sigs) > 1:
                data["recorder_signature_ref"] = sigs[1]
            if len(sigs) > 2:
                data["reviewer_signature_ref"] = sigs[2]
            if len(sigs) > 3:
                data["construction_signature_ref"] = sigs[3]
            if len(sigs) > 4:
                data["supervisor_signature_ref"] = sigs[4]

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
        source_data = self.file or self.photo
        source_name = self.file_name or self.photo_name or "upload_image.jpg"
        if not source_data:
            raise UserError("\u8bf7\u5148\u4e0a\u4f20\u6587\u4ef6\u3002")

        suffix = os.path.splitext((source_name or "").lower())[1]
        if suffix == ".doc":
            raise UserError("\u6682\u4e0d\u652f\u6301 .doc \u81ea\u52a8\u89e3\u6790\uff0c\u8bf7\u8f6c\u4e3a .docx \u6216 PDF/\u56fe\u7247\u3002")

        file_data = base64.b64decode(source_data)
        text, data = self._ocr_and_extract_table(file_data, source_name or "")

        if self.table_type != "auto":
            resolved = self.table_type
        else:
            resolved = data.get("table_type") or "other"
            if resolved == "other":
                resolved = self._guess_table_type(f"{text}\n{source_name or ''}")

        self.resolved_table_type = resolved
        self.ocr_result = self._clean_text(text)[:50000]
        self.parsed_data_json = self._clean_text(json.dumps(self._sanitize_obj(data), ensure_ascii=False, indent=2))

        if data.get("pile_ref"):
            self.pile_ref = data["pile_ref"]
        self._try_resolve_pile_from_ref()
        if data.get("check_date"):
            self.check_date = data["check_date"]

        for key in [
            "design_depth", "actual_drilled_depth", "design_diameter", "actual_diameter", "inclination_permille",
            "hole_detector_passed", "design_top_elevation", "actual_top_elevation", "design_x", "actual_x",
            "design_y", "actual_y", "design_strength", "actual_strength", "integrity_class",
            "inspector_signature_ref", "recorder_signature_ref", "reviewer_signature_ref",
            "construction_signature_ref", "supervisor_signature_ref",
        ]:
            if key in data and data.get(key) not in (None, ""):
                setattr(self, key, data.get(key))

        if data.get("evidence"):
            self.evidence_refs = ",".join([self._clean_text(v) for v in data["evidence"] if self._clean_text(v)])

        if self.pile_id:
            if not self.design_depth:
                self.design_depth = self.pile_id.design_depth
            if not self.design_diameter:
                self.design_diameter = self.pile_id.design_diameter

        self._fill_header_from_usi()
        self.status = "parsed"
        return self._reopen_self()

    def _ensure_parsed(self):
        self.ensure_one()
        if self.status == "draft":
            self.action_parse_only()

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

    def _materialize_drawn_signatures(self, target_model, target_id):
        self.ensure_one()
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
        self._ensure_parsed()

        resolved_type = self.resolved_table_type or ("other" if self.table_type == "auto" else self.table_type)
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

        target_model = generated_record._name if generated_record else "coordos.trip.shadow"
        target_id = generated_record.id if generated_record else trip.id
        source_data = self.file or self.photo
        source_name = self.file_name or self.photo_name or f"bridge_table_{fields.Datetime.now()}.jpg"
        attachment = self._create_attachment(target_model, target_id, source_data, source_name, self._guess_mimetype(source_name))

        extra_refs = []
        if attachment:
            extra_refs.append(f"attachment://{attachment.id}")
        draw_refs = self._materialize_drawn_signatures(target_model, target_id)
        extra_refs.extend(draw_refs.values())

        if generated_record and resolved_type == "7":
            refs = generated_record._evidence_refs_as_list()
            refs.extend(extra_refs)
            generated_record.evidence_refs = json.dumps(refs, ensure_ascii=False)
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
        elif generated_record and resolved_type == "13":
            refs = self._parse_evidence_json(generated_record.evidence_refs)
            refs.extend(extra_refs)
            generated_record.evidence_refs = json.dumps(refs, ensure_ascii=False)
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

        if generated_record and resolved_type == "7" and self.auto_submit_core:
            generated_record.action_submit_to_core()

        self.generated_trip_shadow_id = trip.id
        self.generated_model = target_model
        self.generated_res_id = target_id
        self.generated_message = "OK: uploaded, parsed, addressed, generated. If execute-step asks trip_id, click Start Trip first."
        self.status = "generated"

        return {
            "type": "ir.actions.act_window",
            "name": "Generated",
            "res_model": target_model,
            "view_mode": "form",
            "res_id": target_id,
            "target": "current",
        }
