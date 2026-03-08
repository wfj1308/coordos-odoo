import logging
import os
from urllib.parse import quote

import requests


_logger = logging.getLogger(__name__)

DEFAULT_CORE_URL = os.getenv("COORDOS_CORE_URL", "http://coordos-core:8080").rstrip("/")
DEFAULT_TIMEOUT = int(os.getenv("COORDOS_CORE_TIMEOUT", "12"))
DISPATCH_REQUIRED_KEYS = {
    "trip_name",
    "executor_spu",
    "resources_utxo",
    "project_node_id",
    "context",
    "energy_consumed",
}


def _post_json(core_url, path, payload, timeout):
    url = f"{core_url}{path}"
    _logger.info("CoordOS Bridge request: POST %s payload=%s", url, payload)
    resp = requests.post(url, json=payload, timeout=timeout)
    _logger.info("CoordOS Bridge response: POST %s -> %s", url, resp.status_code)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _get_json(core_url, path, timeout):
    url = f"{core_url}{path}"
    _logger.info("CoordOS Bridge request: GET %s", url)
    resp = requests.get(url, timeout=timeout)
    _logger.info("CoordOS Bridge response: GET %s -> %s", url, resp.status_code)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _get_json_with_params(core_url, path, params, timeout):
    url = f"{core_url}{path}"
    _logger.info("CoordOS Bridge request: GET %s params=%s", url, params)
    resp = requests.get(url, params=params, timeout=timeout)
    _logger.info("CoordOS Bridge response: GET %s -> %s", url, resp.status_code)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _http_error_text(exc):
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        data = response.json()
        if isinstance(data, dict):
            return data.get("reason") or data.get("message") or response.text or str(exc)
    except ValueError:
        pass
    return response.text or str(exc)


def _extract_trip_status(data):
    trip_id = (
        data.get("tripId")
        or data.get("trip_id")
        or data.get("id")
        or (data.get("trip") or {}).get("id")
        or (data.get("data") or {}).get("trip_id")
    )
    status = (
        data.get("status")
        or data.get("state")
        or (data.get("trip") or {}).get("status")
        or (data.get("data") or {}).get("status")
        or "unknown"
    )
    return trip_id, status


def _extract_dispatch_plan_id(data):
    return (
        data.get("dispatchPlanId")
        or data.get("dispatch_plan_id")
        or (data.get("dispatchPlan") or {}).get("id")
        or (data.get("dispatch") or {}).get("plan_id")
        or (data.get("data") or {}).get("dispatchPlanId")
        or (data.get("data") or {}).get("dispatch_plan_id")
    )


def _extract_process_log(data):
    trip_block = data.get("trip") if isinstance(data.get("trip"), dict) else {}
    data_block = data.get("data") if isinstance(data.get("data"), dict) else {}
    candidates = [
        data.get("processLog"),
        data.get("process_log"),
        data.get("steps"),
        data.get("process"),
        trip_block.get("processLog"),
        trip_block.get("process_log"),
        trip_block.get("steps"),
        data_block.get("processLog"),
        data_block.get("process_log"),
        data_block.get("steps"),
    ]
    raw_list = next((value for value in candidates if isinstance(value, list)), [])
    normalized = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        metrics = item.get("metrics")
        if metrics is None:
            metrics = {}
        elif not isinstance(metrics, dict):
            metrics = {"value": metrics}
        normalized.append(
            {
                "stepCode": item.get("stepCode") or item.get("step_code") or item.get("step") or item.get("code"),
                "startedAt": item.get("startedAt") or item.get("started_at") or item.get("startAt"),
                "endedAt": item.get("endedAt") or item.get("ended_at") or item.get("endAt"),
                "metrics": metrics,
            }
        )
    return normalized


def launch_trip(payload, core_url=None, timeout=None):
    """
    Odoo -> Bridge -> Core:
    POST /dispatch/launch-trip
    """
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    payload_keys = set(payload.keys())
    if payload_keys != DISPATCH_REQUIRED_KEYS:
        raise RuntimeError(
            f"payload 字段不符合约定，必须是 {sorted(DISPATCH_REQUIRED_KEYS)}，实际是 {sorted(payload_keys)}"
        )
    if not isinstance(payload.get("resources_utxo"), list):
        raise RuntimeError("payload.resources_utxo 必须是数组")
    if not isinstance(payload.get("context"), dict):
        raise RuntimeError("payload.context 必须是对象")
    if not isinstance(payload.get("energy_consumed"), int):
        raise RuntimeError("payload.energy_consumed 必须是整数")
    try:
        data = _post_json(core_url, "/dispatch/launch-trip", payload, timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/dispatch/launch-trip") from exc
    except requests.HTTPError as exc:
        body = _http_error_text(exc)
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {body or str(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc

    trip_id, status = _extract_trip_status(data)
    dispatch_plan_id = _extract_dispatch_plan_id(data)
    return {
        "raw": data,
        "tripId": trip_id,
        "status": status,
        "dispatchPlanId": dispatch_plan_id,
    }


def _extract_trip_detail(data):
    trip_block = data.get("trip") if isinstance(data.get("trip"), dict) else {}
    trip_id = (
        data.get("tripId")
        or data.get("trip_id")
        or trip_block.get("trip_id")
        or trip_block.get("tripId")
        or data.get("id")
    )
    status = data.get("status") or trip_block.get("status") or data.get("state") or "unknown"
    project_node = (
        data.get("projectNodeId")
        or data.get("project_node_id")
        or trip_block.get("project_node_id")
        or trip_block.get("projectNodeId")
        or data.get("projectNode")
    )
    trip_template = (
        data.get("tripTemplateCode")
        or data.get("trip_template_code")
        or trip_block.get("trip_template_code")
        or trip_block.get("tripTemplateCode")
        or data.get("tripTemplate")
    )
    dispatch_plan_id = _extract_dispatch_plan_id(data)
    process_log = _extract_process_log(data)
    return {
        "raw": data,
        "tripId": trip_id,
        "status": status,
        "projectNode": project_node,
        "tripTemplate": trip_template,
        "dispatchPlanId": dispatch_plan_id,
        "processLog": process_log,
    }


def get_trip_detail(trip_id, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    encoded_id = quote(str(trip_id), safe="")
    try:
        data = _get_json(core_url, f"/trip/{encoded_id}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/trip/{encoded_id}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc
    return _extract_trip_detail(data)


def execute_trip_step(trip_id, payload, core_url=None, timeout=None):
    """
    Odoo -> Bridge -> Core:
    POST /trip/{tripId}/execute-step
    """
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not trip_id:
        raise RuntimeError("trip_id 不能为空")
    if not isinstance(payload, dict):
        raise RuntimeError("payload 必须是 JSON 对象")
    step = payload.get("step")
    metrics = payload.get("metrics")
    if not step:
        raise RuntimeError("payload.step 必填")
    if metrics is None:
        metrics = {}
    if not isinstance(metrics, dict):
        raise RuntimeError("payload.metrics 必须是 JSON 对象")

    strict_payload = {
        "step": step,
        "metrics": metrics,
    }
    encoded_id = quote(str(trip_id), safe="")
    try:
        data = _post_json(core_url, f"/trip/{encoded_id}/execute-step", strict_payload, timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/trip/{encoded_id}/execute-step") from exc
    except requests.HTTPError as exc:
        body = _http_error_text(exc)
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {body or str(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc

    result_trip_id, result_status = _extract_trip_status(data)
    return {
        "raw": data,
        "tripId": result_trip_id or trip_id,
        "status": result_status,
    }


def add_trip_evidence(trip_id, payload, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not trip_id:
        raise RuntimeError("trip_id 不能为空")
    if not isinstance(payload, dict):
        raise RuntimeError("payload 必须是 JSON 对象")
    if "evidence" not in payload:
        raise RuntimeError("payload.evidence 必填")
    if not isinstance(payload.get("evidence"), list):
        raise RuntimeError("payload.evidence 必须是数组")
    if not payload.get("evidence"):
        raise RuntimeError("payload.evidence 不能为空数组")
    encoded_id = quote(str(trip_id), safe="")
    try:
        return _post_json(core_url, f"/trip/{encoded_id}/evidence", payload, timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/trip/{encoded_id}/evidence") from exc
    except requests.HTTPError as exc:
        body = _http_error_text(exc)
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {body or str(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def certify_trip(trip_id, payload, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not trip_id:
        raise RuntimeError("trip_id 不能为空")
    if not isinstance(payload, dict):
        raise RuntimeError("payload 必须是 JSON 对象")
    if "evidence_ids" not in payload:
        raise RuntimeError("payload.evidence_ids 必填")
    if not isinstance(payload.get("evidence_ids"), list):
        raise RuntimeError("payload.evidence_ids 必须是数组")
    if not payload.get("evidence_ids"):
        raise RuntimeError("payload.evidence_ids 不能为空数组")
    if "quantity" not in payload:
        raise RuntimeError("payload.quantity 必填")
    if "unit_price" not in payload:
        raise RuntimeError("payload.unit_price 必填")
    if not isinstance(payload.get("quantity"), (int, float)):
        raise RuntimeError("payload.quantity 必须是数字")
    if not isinstance(payload.get("unit_price"), (int, float)):
        raise RuntimeError("payload.unit_price 必须是数字")
    encoded_id = quote(str(trip_id), safe="")
    try:
        return _post_json(core_url, f"/trip/{encoded_id}/certify", payload, timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/trip/{encoded_id}/certify") from exc
    except requests.HTTPError as exc:
        body = _http_error_text(exc)
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {body or str(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_ledger_by_trip(trip_id, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not trip_id:
        raise RuntimeError("trip_id 不能为空")
    encoded_id = quote(str(trip_id), safe="")
    try:
        return _get_json(core_url, f"/ledger/by-trip/{encoded_id}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/ledger/by-trip/{encoded_id}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_project_node(project_node_id, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not project_node_id:
        raise RuntimeError("project_node_id 不能为空")
    encoded_id = quote(str(project_node_id), safe="")
    try:
        return _get_json(core_url, f"/project-node/{encoded_id}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/project-node/{encoded_id}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def list_smu_kind_templates(item_code=None, template_id=None, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    params = {}
    if item_code:
        params["item_code"] = item_code
    if template_id:
        params["template_id"] = template_id
    try:
        return _get_json_with_params(core_url, "/templates/smu-kind", params=params, timeout=timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/templates/smu-kind") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_smu_kind_template_by_item(item_code, core_url=None, timeout=None):
    if not item_code:
        raise RuntimeError("item_code 不能为空")
    return list_smu_kind_templates(item_code=item_code, core_url=core_url, timeout=timeout)


def get_smu_kind_template_by_id(template_id, core_url=None, timeout=None):
    if not template_id:
        raise RuntimeError("template_id 不能为空")
    return list_smu_kind_templates(template_id=template_id, core_url=core_url, timeout=timeout)


def get_smu_kind_template_by_path_item(item_code, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not item_code:
        raise RuntimeError("item_code 不能为空")
    encoded_code = quote(str(item_code), safe="")
    try:
        return _get_json(core_url, f"/templates/smu-kind/{encoded_code}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/templates/smu-kind/{encoded_code}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_smu_kind_template_by_path_template(template_id, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not template_id:
        raise RuntimeError("template_id 不能为空")
    encoded_tpl = quote(str(template_id), safe="")
    try:
        return _get_json(core_url, f"/templates/smu-kind/by-template/{encoded_tpl}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/templates/smu-kind/by-template/{encoded_tpl}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def generate_project_tree(payload, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not isinstance(payload, dict):
        raise RuntimeError("payload 必须是 JSON 对象")
    project_usi = payload.get("project_usi")
    structure = payload.get("structure")
    if not isinstance(project_usi, str) or not project_usi.strip():
        raise RuntimeError("payload.project_usi 必填且必须是字符串")
    if not isinstance(structure, dict):
        raise RuntimeError("payload.structure 必填且必须是对象")
    try:
        return _post_json(core_url, "/project-tree/generate", payload, timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/project-tree/generate") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_project_tree(project_usi, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not project_usi:
        raise RuntimeError("project_usi 不能为空")
    encoded_id = quote(str(project_usi), safe="")
    try:
        return _get_json(core_url, f"/project-tree/{encoded_id}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/project-tree/{encoded_id}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_project_tree_location(project_usi, location, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not project_usi or not location:
        raise RuntimeError("project_usi/location 不能为空")
    encoded_id = quote(str(project_usi), safe="")
    encoded_loc = quote(str(location), safe="")
    try:
        return _get_json(core_url, f"/project-tree/{encoded_id}/location/{encoded_loc}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/project-tree/{encoded_id}/location/{encoded_loc}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_project_tree_material_stats(project_usi, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not project_usi:
        raise RuntimeError("project_usi 不能为空")
    encoded_id = quote(str(project_usi), safe="")
    try:
        return _get_json(core_url, f"/project-tree/{encoded_id}/stats/material-remaining", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/project-tree/{encoded_id}/stats/material-remaining") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_project_tree_instance(project_usi, instance_ref, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not project_usi or not instance_ref:
        raise RuntimeError("project_usi/instance_ref 不能为空")
    encoded_id = quote(str(project_usi), safe="")
    encoded_ref = quote(str(instance_ref), safe="")
    try:
        return _get_json(core_url, f"/project-tree/{encoded_id}/instance/{encoded_ref}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/project-tree/{encoded_id}/instance/{encoded_ref}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_project_tree_trace(project_usi, instance_ref, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not project_usi or not instance_ref:
        raise RuntimeError("project_usi/instance_ref 不能为空")
    encoded_id = quote(str(project_usi), safe="")
    encoded_ref = quote(str(instance_ref), safe="")
    try:
        return _get_json(core_url, f"/project-tree/{encoded_id}/trace/{encoded_ref}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/project-tree/{encoded_id}/trace/{encoded_ref}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_bridge_inspection_templates(core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    try:
        return _get_json(core_url, "/templates/bridge/inspection", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/templates/bridge/inspection") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_bridge_inspection_template(table_no, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if str(table_no) not in {"7", "13"}:
        raise RuntimeError("table_no 仅支持 7 或 13")
    try:
        return _get_json(core_url, f"/templates/bridge/inspection/{table_no}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/templates/bridge/inspection/{table_no}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def submit_bridge_table7(payload, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not isinstance(payload, dict):
        raise RuntimeError("payload 必须是 JSON 对象")
    if not isinstance(payload.get("pile_ref"), str) or not payload.get("pile_ref", "").strip():
        raise RuntimeError("payload.pile_ref 必填且必须是字符串")
    measurements = payload.get("measurements")
    if not isinstance(measurements, dict):
        raise RuntimeError("payload.measurements 必填且必须是对象")
    required = [
        "design_depth",
        "actual_drilled_depth",
        "design_diameter",
        "actual_diameter",
        "inclination_permille",
        "hole_detector_passed",
    ]
    missing = [k for k in required if k not in measurements]
    if missing:
        raise RuntimeError(f"payload.measurements 缺少字段: {', '.join(missing)}")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise RuntimeError("payload.evidence 必填且必须是非空数组")
    signatures = payload.get("signatures")
    if not isinstance(signatures, dict):
        raise RuntimeError("payload.signatures 必填且必须是对象")
    for key in ("inspector", "reviewer"):
        if key not in signatures:
            raise RuntimeError(f"payload.signatures 缺少字段: {key}")
    try:
        return _post_json(core_url, "/bridge/pile/hole-inspection/submit", payload, timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/bridge/pile/hole-inspection/submit") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def submit_bridge_table13(payload, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not isinstance(payload, dict):
        raise RuntimeError("payload 必须是 JSON 对象")
    if not isinstance(payload.get("pile_ref"), str) or not payload.get("pile_ref", "").strip():
        raise RuntimeError("payload.pile_ref 必填且必须是字符串")
    measurements = payload.get("measurements")
    if not isinstance(measurements, dict):
        raise RuntimeError("payload.measurements 必填且必须是对象")
    required = [
        "design_top_elevation",
        "actual_top_elevation",
        "design_x",
        "actual_x",
        "design_y",
        "actual_y",
        "design_strength",
        "actual_strength",
        "integrity_class",
    ]
    missing = [k for k in required if k not in measurements]
    if missing:
        raise RuntimeError(f"payload.measurements 缺少字段: {', '.join(missing)}")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise RuntimeError("payload.evidence 必填且必须是非空数组")
    signatures = payload.get("signatures")
    if signatures is not None and not isinstance(signatures, dict):
        raise RuntimeError("payload.signatures 如提供必须是对象")
    try:
        return _post_json(core_url, "/bridge/pile/final-inspection/submit", payload, timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/bridge/pile/final-inspection/submit") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc


def get_bridge_pile_inspection(trip_id, core_url=None, timeout=None):
    core_url = (core_url or DEFAULT_CORE_URL).rstrip("/")
    timeout = timeout or DEFAULT_TIMEOUT
    if not trip_id:
        raise RuntimeError("trip_id 不能为空")
    encoded_id = quote(str(trip_id), safe="")
    try:
        return _get_json(core_url, f"/bridge/pile/inspection/{encoded_id}", timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"请求超时: {core_url}/bridge/pile/inspection/{encoded_id}") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"HTTP {status}: {_http_error_text(exc)}") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc
