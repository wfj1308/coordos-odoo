import os
import logging
import re
from urllib.parse import quote, urlencode

import requests
from requests import RequestException

from odoo import models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class CoordosApiMixin(models.AbstractModel):
    _name = "coordos.api.mixin"
    _description = "CoordOS Core API 桥接"

    def _configured_core_base(self):
        base = self.env["ir.config_parameter"].sudo().get_param("coordos.core_base_url")
        if not base:
            base = os.getenv("COORDOS_CORE_URL", "http://coordos-core:8080")
        return base.rstrip("/")

    def _fallback_core_base(self):
        base = os.getenv("COORDOS_CORE_FALLBACK_URL", "http://coordos-core:8080")
        return base.rstrip("/")

    def _fallback_enabled(self):
        value = self.env["ir.config_parameter"].sudo().get_param("coordos.core_enable_fallback")
        if value is None:
            value = os.getenv("COORDOS_CORE_ENABLE_FALLBACK", "1")
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    def _core_verify_ssl(self):
        value = self.env["ir.config_parameter"].sudo().get_param("coordos.core_verify_ssl")
        if value is None:
            value = os.getenv("COORDOS_CORE_VERIFY_SSL", "1")
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    def _core_bases(self):
        primary = self._configured_core_base()
        bases = [primary]
        fallback = self._fallback_core_base()
        if self._fallback_enabled() and fallback and fallback not in bases:
            bases.append(fallback)
        return bases

    def _core_url(self, path=""):
        return f"{self._configured_core_base()}{path}"

    def _core_env(self):
        value = self.env["ir.config_parameter"].sudo().get_param("coordos.core_env")
        if value:
            return value
        return os.getenv("COORDOS_CORE_ENV", "dev")

    def _core_headers(self):
        headers = {"Content-Type": "application/json"}
        api_key = self.env["ir.config_parameter"].sudo().get_param("coordos.core_api_key")
        if not api_key:
            api_key = os.getenv("COORDOS_CORE_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @staticmethod
    def _as_dict(payload):
        if isinstance(payload, dict):
            return dict(payload)
        return {}

    @staticmethod
    def _first_present(payload, keys, default=None):
        if not isinstance(payload, dict):
            return default
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
        return default

    @staticmethod
    def _read_path(payload, dotted_path):
        cur = payload
        for part in (dotted_path or "").split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        if cur in (None, ""):
            return None
        return cur

    def _first_response_value(self, payload, paths):
        for path in paths:
            value = self._read_path(payload, path)
            if value not in (None, ""):
                return value
        return None

    def _require_payload_fields(self, endpoint, payload, required_fields):
        missing = [field for field in required_fields if payload.get(field) in (None, "")]
        if missing:
            raise UserError(f"CoordOS API 请求参数缺失 ({endpoint}): {', '.join(missing)}")

    def _require_response_fields(self, endpoint, response, required_paths):
        missing = [path for path in required_paths if self._read_path(response, path) in (None, "")]
        if missing:
            raise UserError(f"CoordOS API 返回缺失字段 ({endpoint}): {', '.join(missing)}")

    def current_org_code(self):
        user = self.env.user
        company = user.company_id
        candidates = [
            getattr(company, "x_org_code", None),
            getattr(company, "company_registry", None),
            getattr(company, "vat", None),
            getattr(company, "name", None),
        ]
        for value in candidates:
            if value:
                code = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value).strip()).strip("-").lower()
                if code:
                    return code[:64]

        login = (user.login or "").split("@", 1)[0]
        login_code = re.sub(r"[^A-Za-z0-9_-]+", "-", login.strip()).strip("-").lower()
        return login_code[:64] or "org-default"

    def _should_try_next_base(self, exc):
        if isinstance(exc, requests.exceptions.Timeout):
            return True
        if isinstance(exc, requests.exceptions.ConnectionError):
            return True
        if isinstance(exc, requests.exceptions.HTTPError):
            status = exc.response.status_code if exc.response is not None else None
            if status in {408, 429}:
                return True
            if status is not None and status >= 500:
                return True
        return False

    def core_request(self, method, path, payload=None, params=None, timeout=30):
        errors = []
        headers = self._core_headers()
        bases = self._core_bases()
        for index, base in enumerate(bases):
            url = f"{base}{path}"
            try:
                _logger.info("CoordOS API request: %s %s", method.upper(), url)
                response = requests.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    json=payload,
                    params=params,
                    timeout=timeout,
                    verify=self._core_verify_ssl(),
                )
                response.raise_for_status()
                _logger.info(
                    "CoordOS API response: %s %s -> %s",
                    method.upper(),
                    url,
                    response.status_code,
                )
                if not response.content:
                    return {}
                return response.json()
            except RequestException as exc:
                _logger.warning("CoordOS API failed: %s %s (%s)", method.upper(), url, exc)
                errors.append(f"{url}: {exc}")
                if index == len(bases) - 1:
                    break
                if not self._should_try_next_base(exc):
                    break
            except ValueError as exc:
                errors.append(f"{url}: invalid JSON response ({exc})")
                break
        raise UserError(f"CoordOS API 调用失败 ({method.upper()} {path}): {' | '.join(errors)}")

    def _try_post(self, paths, payload, params=None):
        errors = []
        for path in paths:
            try:
                return self.core_request("POST", path, payload=payload, params=params)
            except UserError as exc:
                message = str(exc)
                errors.append(message)
                # Auth/policy rejection is deterministic; do not append noisy fallback-path errors.
                if " 401 " in message or " 403 " in message:
                    raise UserError(message) from exc
        raise UserError(" | ".join(errors))

    def _try_get(self, paths, params=None):
        errors = []
        for path in paths:
            try:
                return self.core_request("GET", path, params=params)
            except UserError as exc:
                message = str(exc)
                errors.append(message)
                if " 401 " in message or " 403 " in message:
                    raise UserError(message) from exc
        raise UserError(" | ".join(errors))

    # Canonical wrappers
    def healthz(self):
        return self._try_get(["/healthz", "/api/healthz"])

    def register_spu(self, payload):
        data = self._as_dict(payload)
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        if metadata.get("name") and not data.get("name"):
            data["name"] = metadata.get("name")
        if not data.get("metadata") and data.get("name"):
            data["metadata"] = {"name": data["name"]}
        self._require_payload_fields("/spu/register", data, ["owner"])
        if not self._first_present(data, ["name"]) and not self._read_path(data, "metadata.name"):
            raise UserError("CoordOS API 请求参数缺失 (/spu/register): name 或 metadata.name")
        result = self._try_post(["/spu/register", "/api/spu/register"], data)
        if not self._first_response_value(result, ["spu.id", "data.spu.id"]):
            raise UserError("CoordOS API 返回缺失字段 (/spu/register): data.spu.id")
        return result

    def trip_admission(self, payload):
        raw = self._as_dict(payload)
        data = dict(raw)
        # Preferred strict contract
        if self._first_present(raw, ["trip_name"]) and self._first_present(raw, ["executor_spu"]):
            data["trip_name"] = self._first_present(raw, ["trip_name"])
            data["executor_spu"] = self._first_present(raw, ["executor_spu"])
            data["resources_utxo"] = raw.get("resources_utxo")
            data["project_node_id"] = self._first_present(raw, ["project_node_id", "projectNodeId"])
            data["context"] = raw.get("context")
            self._require_payload_fields(
                "/trip/admission", data, ["trip_name", "executor_spu", "resources_utxo", "project_node_id", "context"]
            )
            if not isinstance(data.get("resources_utxo"), list):
                raise UserError("CoordOS API 请求参数格式错误 (/trip/admission): resources_utxo 必须是数组")
            if not isinstance(data.get("context"), dict):
                raise UserError("CoordOS API 请求参数格式错误 (/trip/admission): context 必须是对象")
        else:
            # Legacy compatibility
            data["spu_ref"] = self._first_present(raw, ["spu_ref", "spuRef", "spu_id", "spuId"])
            data["executor_did"] = self._first_present(raw, ["executor_did", "executorDid", "executor"])
            self._require_payload_fields("/trip/admission", data, ["spu_ref", "executor_did"])
        result = self._try_post(["/trip/admission", "/api/trip/admission"], data)
        self._require_response_fields("/trip/admission", result, ["admission_id"])
        return result

    def trip_start(self, payload):
        raw = self._as_dict(payload)
        admission_id = self._first_present(raw, ["admission_id", "admissionId"])
        if admission_id:
            data = dict(raw)
            data["admission_id"] = admission_id
            result = self._try_post(["/trip/start", "/api/trip/start"], data)
            self._require_response_fields("/trip/start", result, ["trip_id"])
            return result

        # Legacy compatibility path (older clients without admission_id)
        try:
            result = self._try_post(["/trip/start", "/api/trip/start"], raw)
            if not self._first_response_value(result, ["trip_id", "tripId", "id"]):
                raise UserError("CoordOS API 返回缺失字段 (/trip/start): trip_id")
            return result
        except UserError:
            runtime_payload = {
                "work_id": raw.get("work_id") or raw.get("projectNode"),
                "tripUsi": raw.get("tripUsi") or "v://trip/engineering/pile@1.1.0",
                "input": raw.get("input", {}),
            }
            data = self.core_request(
                "POST",
                "/api/trip/execute",
                payload=runtime_payload,
                params={"env": self._core_env()},
            )
            if data.get("ok") and "status" not in data:
                data["status"] = "running"
            return data

    def trip_evidence(self, payload):
        raw = self._as_dict(payload)
        data = dict(raw)
        data["trip_id"] = self._first_present(raw, ["trip_id", "tripId", "id"])
        data["evidence"] = self._first_present(raw, ["evidence"], default=[])
        self._require_payload_fields("/trip/evidence", data, ["trip_id", "evidence"])
        if not isinstance(data.get("evidence"), list):
            raise UserError("CoordOS API 请求参数格式错误 (/trip/evidence): evidence 必须是数组")
        result = self._try_post(["/trip/evidence", "/api/trip/evidence"], data)
        self._require_response_fields("/trip/evidence", result, ["evidence_ids"])
        return result

    def trip_assert(self, payload):
        raw = self._as_dict(payload)
        data = dict(raw)
        data["trip_id"] = self._first_present(raw, ["trip_id", "tripId", "id"])
        data["evidence_ids"] = self._first_present(raw, ["evidence_ids", "evidenceIds"], default=[])
        self._require_payload_fields("/trip/assert", data, ["trip_id", "evidence_ids"])
        if not isinstance(data.get("evidence_ids"), list):
            raise UserError("CoordOS API 请求参数格式错误 (/trip/assert): evidence_ids 必须是数组")
        result = self._try_post(["/trip/assert", "/api/trip/assert"], data)
        if not self._first_response_value(result, ["verdict", "trip_id", "tripId"]):
            raise UserError("CoordOS API 返回缺失字段 (/trip/assert): verdict 或 trip_id")
        return result

    def trip_mint(self, payload):
        raw = self._as_dict(payload)
        data = dict(raw)
        data["trip_id"] = self._first_present(raw, ["trip_id", "tripId", "id"])
        self._require_payload_fields("/trip/mint", data, ["trip_id"])
        result = self._try_post(["/trip/mint", "/api/trip/mint"], data)
        if not self._first_response_value(result, ["product_utxo", "utxo_id"]):
            raise UserError("CoordOS API 返回缺失字段 (/trip/mint): product_utxo")
        return result

    def get_trip_list(self, params=None):
        try:
            return self._try_get(["/trip/list", "/api/trip/list"], params=params)
        except UserError:
            return self._try_post(["/trip/list", "/api/trip/list"], payload={}, params=params)

    def get_trip_status(self, trip_id):
        encoded_id = quote(str(trip_id), safe="")
        try:
            return self._try_get(
                [
                    f"/trip/{encoded_id}/status",
                    f"/api/trip/{encoded_id}/status",
                ]
            )
        except UserError:
            return self._try_post(
                ["/trip/status", "/api/trip/status"],
                payload={"trip_id": trip_id, "tripId": trip_id},
            )

    def get_spu_graph(self, spu_id):
        encoded_id = quote(str(spu_id), safe="")
        return self.core_request("GET", f"/spu/{encoded_id}/graph")

    def get_finance_balance(self, params=None):
        return self.core_request("GET", "/peg/finance/balance-sheet", params=params or {})

    # Compatibility wrappers for existing code
    def core_register_spu(self, payload):
        return self.register_spu(payload)

    def core_update_spu(self, payload):
        spu_id = payload.get("spu_id")
        paths = ["/spu/update", "/api/spu/update"]
        errors = []

        # Legacy update endpoints (POST)
        for path in paths:
            try:
                return self.core_request("POST", path, payload=payload)
            except UserError as exc:
                errors.append(str(exc))

        # REST-style update endpoints (PATCH/PUT/POST with encoded id)
        if spu_id:
            encoded_id = quote(str(spu_id), safe="")
            method_paths = [
                ("PATCH", f"/spu/{encoded_id}"),
                ("PUT", f"/spu/{encoded_id}"),
                ("POST", f"/spu/{encoded_id}"),
                ("PATCH", f"/api/spu/{encoded_id}"),
                ("PUT", f"/api/spu/{encoded_id}"),
                ("POST", f"/api/spu/{encoded_id}"),
            ]
            for method, path in method_paths:
                try:
                    return self.core_request(method, path, payload=payload)
                except UserError as exc:
                    errors.append(str(exc))

        raise UserError(" | ".join(errors))

    def core_trip_admission(self, payload):
        return self.trip_admission(payload)

    def core_trip_start(self, payload):
        return self.trip_start(payload)

    def core_trip_evidence(self, payload):
        return self.trip_evidence(payload)

    def core_trip_assert(self, payload):
        return self.trip_assert(payload)

    def core_trip_mint(self, payload):
        return self.trip_mint(payload)

    def core_graph_url(self, spu_id):
        query = urlencode({"spu_id": spu_id})
        return self._core_url(f"/spu/{spu_id}/graph?{query}")





