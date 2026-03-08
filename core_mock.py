from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from urllib.parse import parse_qs, unquote, urlparse


SPUS = {}
ADMISSIONS = {}
TRIPS = {}
LEDGERS = {}
PROJECT_TREES = {}
BRIDGE_INSPECTIONS = {}
PILE_TABLE7_STATE = {}
COUNTERS = {
    "admission": 0,
    "trip": 0,
    "evidence": 0,
    "dispatch": 0,
    "step": 0,
}

TEMPLATES = [
    {
        "template_id": "smu.kind:highway:403-1-1:v1",
        "item_code": "403-1-1",
        "name": "钢筋加工模板",
        "path": "v://templates/smu-kind/highway/403-1-1/v1.yaml",
        "fields": [
            {"key": "bar_grade", "label": "钢筋级别", "type": "string", "default": "HRB400"},
            {"key": "diameter_mm", "label": "直径(mm)", "type": "integer", "default": 25},
            {"key": "weight_t", "label": "重量(t)", "type": "number", "default": 1.0},
        ],
    },
    {
        "template_id": "smu.kind:highway:404-2-1:v1",
        "item_code": "404-2-1",
        "name": "桩基成孔模板",
        "path": "v://templates/smu-kind/highway/404-2-1/v1.yaml",
        "fields": [
            {"key": "design_depth", "label": "应钻深度(m)", "type": "number", "default": 20},
            {"key": "design_diameter", "label": "设计桩径(m)", "type": "number", "default": 1.2},
            {"key": "inclination_permille", "label": "倾斜度(‰)", "type": "number", "default": 30},
        ],
    },
]

BRIDGE_INSPECTION_TEMPLATES = {
    "7": {
        "table_no": "7",
        "code": "bridge_table_7",
        "name": "桥施7表-桩基成孔检查",
        "required_measurements": [
            "design_depth",
            "actual_drilled_depth",
            "design_diameter",
            "actual_diameter",
            "inclination_permille",
            "hole_detector_passed",
        ],
    },
    "13": {
        "table_no": "13",
        "code": "bridge_table_13",
        "name": "桥施13表-桩基成桩检查",
        "required_measurements": [
            "design_top_elevation",
            "actual_top_elevation",
            "design_x",
            "actual_x",
            "design_y",
            "actual_y",
            "design_strength",
            "actual_strength",
            "integrity_class",
        ],
    },
}


def _json(handler, data, code=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler, reason, code=400):
    _json(handler, {"status": "error", "reason": reason}, code)


def _next_id(prefix):
    COUNTERS[prefix] += 1
    return f"{prefix}:{COUNTERS[prefix]:04d}"


def _first(payload, keys, default=None):
    if not isinstance(payload, dict):
        return default
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return default


def _normalized_parts(path):
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == "api":
        parts = parts[1:]
    return parts


def _require_keys(payload, keys):
    missing = [key for key in keys if payload.get(key) in (None, "")]
    return missing


def _trip_view(trip):
    return {
        "trip_id": trip["id"],
        "status": trip["status"],
        "project_node_id": trip.get("project_node_id"),
        "tripTemplateCode": trip.get("trip_template_code", "pile_construction"),
        "dispatchPlanId": trip.get("dispatch_plan_id"),
        "processLog": trip.get("process_log", []),
        "prc_hash": trip.get("prc_hash"),
        "product_utxo": trip.get("product_utxo"),
        "trip": {
            "id": trip["id"],
            "status": trip["status"],
            "project_node_id": trip.get("project_node_id"),
            "trip_template_code": trip.get("trip_template_code", "pile_construction"),
            "processLog": trip.get("process_log", []),
        },
    }


def _build_default_ledger(trip):
    quantity = float(trip.get("quantity") or 0)
    unit_price = float(trip.get("unit_price") or 0)
    amount = quantity * unit_price
    line = {
        "line_no": 1,
        "trip_id": trip["id"],
        "description": "trip_certification",
        "quantity": quantity,
        "unit_price": unit_price,
        "amount": amount,
    }
    return {
        "status": "ok",
        "trip_id": trip["id"],
        "assets": amount,
        "liabilities": 0,
        "equity": amount,
        "lines": [line],
        "data": {"trip_id": trip["id"], "lines": [line], "total_amount": amount},
    }


def _template_payload(template):
    schema = {"fields": template.get("fields", [])}
    return {
        "template_id": template["template_id"],
        "item_code": template["item_code"],
        "name": template["name"],
        "path": template["path"],
        "schema": schema,
        "fields": template.get("fields", []),
    }


def _templates_response(items):
    payload_items = [_template_payload(item) for item in items]
    template = payload_items[0] if payload_items else None
    return {
        "status": "ok",
        "count": len(payload_items),
        "items": payload_items,
        "template": template,
        "data": {"count": len(payload_items), "items": payload_items, "template": template},
    }


def _material_stats(tree_state):
    stats = {}
    for rec in tree_state.get("instances", {}).values():
        item_code = rec.get("item_code")
        if not item_code:
            continue
        bucket = stats.setdefault(item_code, {"item_code": item_code, "total": 0.0, "remaining": 0.0, "unit": rec.get("unit", "")})
        bucket["total"] += float(rec.get("total") or 0)
        bucket["remaining"] += float(rec.get("remaining") or 0)
    lines = list(stats.values())
    return {"status": "ok", "project_usi": tree_state["project_usi"], "data": {"lines": lines, "count": len(lines)}, "lines": lines}


def _build_tree_state(project_usi, structure):
    state = {
        "project_usi": project_usi,
        "structure": structure,
        "locations": {},
        "instances": {},
        "traces": {},
    }

    def _add_location(key, payload):
        state["locations"][key] = payload

    bridges = structure.get("bridges", []) if isinstance(structure, dict) else []
    if isinstance(bridges, list):
        for bridge in bridges:
            if not isinstance(bridge, dict):
                continue
            bridge_id = str(bridge.get("id") or "").strip()
            if not bridge_id:
                continue
            _add_location(bridge_id, bridge)
            piers = bridge.get("piers", [])
            if not isinstance(piers, list):
                continue
            for pier in piers:
                if not isinstance(pier, dict):
                    continue
                pier_id = str(pier.get("id") or "").strip()
                if not pier_id:
                    continue
                loc_key = f"{bridge_id}/{pier_id}"
                _add_location(loc_key, pier)
                instances = pier.get("instances", [])
                if not isinstance(instances, list):
                    continue
                for idx, inst in enumerate(instances, start=1):
                    if isinstance(inst, str):
                        inst_ref = inst
                        item_code = ""
                        total = 0.0
                        remaining = 0.0
                        unit = ""
                    elif isinstance(inst, dict):
                        item_code = str(inst.get("item_code") or "").strip()
                        inst_ref = str(inst.get("instance_ref") or "").strip()
                        if not inst_ref:
                            inst_ref = f"{project_usi}/smu/{item_code or 'unknown'}/{idx:03d}"
                        total = float(inst.get("total") or 0)
                        remaining = float(inst.get("remaining") or 0)
                        unit = str(inst.get("unit") or "")
                    else:
                        continue
                    state["instances"][inst_ref] = {
                        "instance_ref": inst_ref,
                        "location": loc_key,
                        "item_code": item_code,
                        "total": total,
                        "remaining": remaining,
                        "unit": unit,
                    }
                    state["traces"][inst_ref] = {
                        "project_usi": project_usi,
                        "path": [project_usi, bridge_id, pier_id, inst_ref],
                        "location": loc_key,
                    }

    roadbed = structure.get("roadbed", {}) if isinstance(structure, dict) else {}
    sections = roadbed.get("sections", []) if isinstance(roadbed, dict) else []
    if isinstance(sections, list):
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            sid = str(sec.get("id") or "").strip()
            if not sid:
                continue
            loc_key = f"roadbed/{sid}"
            _add_location(loc_key, sec)
            instances = sec.get("instances", [])
            if not isinstance(instances, list):
                continue
            for idx, inst in enumerate(instances, start=1):
                if isinstance(inst, str):
                    inst_ref = inst
                    item_code = ""
                    total = 0.0
                    remaining = 0.0
                    unit = ""
                elif isinstance(inst, dict):
                    item_code = str(inst.get("item_code") or "").strip()
                    inst_ref = str(inst.get("instance_ref") or "").strip()
                    if not inst_ref:
                        inst_ref = f"{project_usi}/smu/{item_code or 'unknown'}/{idx:03d}"
                    total = float(inst.get("total") or 0)
                    remaining = float(inst.get("remaining") or 0)
                    unit = str(inst.get("unit") or "")
                else:
                    continue
                state["instances"][inst_ref] = {
                    "instance_ref": inst_ref,
                    "location": loc_key,
                    "item_code": item_code,
                    "total": total,
                    "remaining": remaining,
                    "unit": unit,
                }
                state["traces"][inst_ref] = {
                    "project_usi": project_usi,
                    "path": [project_usi, "roadbed", sid, inst_ref],
                    "location": loc_key,
                }

    return state


def _to_float(value):
    try:
        return float(value)
    except Exception:
        return None


def _validate_bridge_base_payload(payload):
    if not isinstance(payload, dict):
        return "payload must be object"
    pile_ref = payload.get("pile_ref")
    if not isinstance(pile_ref, str) or not pile_ref.strip():
        return "missing required field: pile_ref"
    measurements = payload.get("measurements")
    if not isinstance(measurements, dict):
        return "missing required field: measurements(object)"
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return "missing required field: evidence(list)"
    signatures = payload.get("signatures")
    if signatures is not None and not isinstance(signatures, dict):
        return "invalid field type: signatures must be object"
    return ""


def _table7_verdict(measurements):
    required = BRIDGE_INSPECTION_TEMPLATES["7"]["required_measurements"]
    for key in required:
        if key not in measurements:
            return None, f"missing required measurement: {key}"
    design_depth = _to_float(measurements.get("design_depth"))
    actual_depth = _to_float(measurements.get("actual_drilled_depth"))
    design_d = _to_float(measurements.get("design_diameter"))
    actual_d = _to_float(measurements.get("actual_diameter"))
    incline = _to_float(measurements.get("inclination_permille"))
    hole_passed = measurements.get("hole_detector_passed")
    if None in (design_depth, actual_depth, design_d, actual_d, incline):
        return None, "invalid measurement types for table7"
    if not isinstance(hole_passed, bool):
        return None, "invalid measurement type: hole_detector_passed must be bool"
    qualified = (
        abs(actual_depth - design_depth) <= 0.2
        and abs(actual_d - design_d) <= 0.1
        and incline <= 30
        and hole_passed
    )
    return ("qualified" if qualified else "rejected"), ""


def _table13_verdict(measurements):
    required = BRIDGE_INSPECTION_TEMPLATES["13"]["required_measurements"]
    for key in required:
        if key not in measurements:
            return None, f"missing required measurement: {key}"
    values = {k: measurements.get(k) for k in required}
    nums = {
        key: _to_float(values.get(key))
        for key in [
            "design_top_elevation",
            "actual_top_elevation",
            "design_x",
            "actual_x",
            "design_y",
            "actual_y",
            "design_strength",
            "actual_strength",
        ]
    }
    if any(v is None for v in nums.values()):
        return None, "invalid numeric measurement types for table13"
    integrity = str(values.get("integrity_class") or "").strip()
    if not integrity:
        return None, "invalid measurement: integrity_class required"
    qualified = (
        abs(nums["actual_top_elevation"] - nums["design_top_elevation"]) <= 0.05
        and abs(nums["actual_x"] - nums["design_x"]) <= 0.05
        and abs(nums["actual_y"] - nums["design_y"]) <= 0.05
        and nums["actual_strength"] >= nums["design_strength"]
        and integrity in {"I", "II"}
    )
    return ("qualified" if qualified else "rejected"), ""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        parts = _normalized_parts(path)
        query = parse_qs(parsed.query or "")

        if parts == ["healthz"]:
            _json(self, {"status": "ok"})
            return

        if len(parts) == 3 and parts[0] == "spu" and parts[2] == "graph":
            spu_id = unquote(parts[1])
            trip_nodes = [
                {"id": trip_id, "kind": "trip", "status": trip.get("status")}
                for trip_id, trip in TRIPS.items()
                if trip.get("executor_spu") == spu_id
            ]
            _json(
                self,
                {
                    "spu_id": spu_id,
                    "nodes": [{"id": spu_id, "kind": "spu"}] + trip_nodes,
                    "edges": [{"from": spu_id, "to": node["id"], "kind": "owns_trip"} for node in trip_nodes],
                    "trace": f"trace:{spu_id}",
                },
            )
            return

        if parts == ["peg", "finance", "balance-sheet"]:
            _json(
                self,
                {
                    "assets": 1000,
                    "liabilities": 300,
                    "equity": 700,
                    "by_account": [{"code": "1001", "balance": 1000}],
                    "lines": [{"label": "Mock Assets", "amount": 1000}],
                    "trace": "trace:finance:mock",
                },
            )
            return

        if parts == ["templates", "smu-kind"]:
            item_code = unquote((query.get("item_code") or [""])[0]).strip()
            template_id = unquote((query.get("template_id") or [""])[0]).strip()
            if item_code and template_id:
                _error(self, "item_code 与 template_id 不能同时传入")
                return
            items = TEMPLATES
            if item_code:
                items = [t for t in TEMPLATES if t.get("item_code") == item_code]
            if template_id:
                items = [t for t in TEMPLATES if t.get("template_id") == template_id]
            _json(self, _templates_response(items))
            return

        if parts == ["templates", "bridge", "inspection"]:
            templates = list(BRIDGE_INSPECTION_TEMPLATES.values())
            _json(
                self,
                {
                    "status": "ok",
                    "templates": templates,
                    "data": {"templates": templates, "count": len(templates)},
                },
            )
            return

        if len(parts) == 4 and parts[0] == "templates" and parts[1] == "bridge" and parts[2] == "inspection":
            table_no = str(parts[3])
            template = BRIDGE_INSPECTION_TEMPLATES.get(table_no)
            if not template:
                _error(self, f"unsupported bridge inspection table: {table_no}", 404)
                return
            _json(self, {"status": "ok", "table_no": table_no, "template": template, "data": {"template": template}})
            return

        if len(parts) == 3 and parts[0] == "templates" and parts[1] == "smu-kind":
            item_code = unquote(parts[2]).strip()
            items = [t for t in TEMPLATES if t.get("item_code") == item_code]
            _json(self, _templates_response(items))
            return

        if len(parts) == 4 and parts[0] == "templates" and parts[1] == "smu-kind" and parts[2] == "by-template":
            template_id = unquote(parts[3]).strip()
            items = [t for t in TEMPLATES if t.get("template_id") == template_id]
            _json(self, _templates_response(items))
            return

        if parts == ["trip", "list"]:
            _json(self, {"trips": [_trip_view(trip) for trip in TRIPS.values()]})
            return

        if len(parts) == 3 and parts[0] == "trip" and parts[2] == "status":
            trip_id = unquote(parts[1])
            trip = TRIPS.get(trip_id)
            if not trip:
                _error(self, f"trip not found: {trip_id}", 404)
                return
            _json(self, _trip_view(trip))
            return

        if len(parts) == 2 and parts[0] == "trip":
            trip_id = unquote(parts[1])
            trip = TRIPS.get(trip_id)
            if not trip:
                _error(self, f"trip not found: {trip_id}", 404)
                return
            data = _trip_view(trip)
            _json(self, {"status": "ok", "data": data, **data})
            return

        if len(parts) == 3 and parts[0] == "ledger" and parts[1] == "by-trip":
            trip_id = unquote(parts[2])
            trip = TRIPS.get(trip_id)
            if not trip:
                _error(self, f"trip not found: {trip_id}", 404)
                return
            ledger = LEDGERS.get(trip_id) or _build_default_ledger(trip)
            _json(self, ledger)
            return

        if len(parts) == 2 and parts[0] == "project-node":
            project_node_id = unquote(parts[1])
            segments = [item for item in project_node_id.split("/") if item]
            _json(
                self,
                {
                    "status": "ok",
                    "project_node_id": project_node_id,
                    "data": {
                        "project_node_id": project_node_id,
                        "kind": "project-node",
                        "name": segments[-1] if segments else "node",
                        "segments": segments,
                    },
                },
            )
            return

        if len(parts) == 2 and parts[0] == "project-tree":
            project_usi = unquote(parts[1]).strip()
            state = PROJECT_TREES.get(project_usi)
            if not state:
                _error(self, f"project tree not found: {project_usi}", 404)
                return
            payload = {"status": "ok", "project_usi": project_usi, "data": {"project_usi": project_usi, "structure": state["structure"]}}
            _json(self, payload)
            return

        if len(parts) == 4 and parts[0] == "bridge" and parts[1] == "pile" and parts[2] == "inspection":
            trip_id = unquote(parts[3]).strip()
            record = BRIDGE_INSPECTIONS.get(trip_id)
            if not record:
                _error(self, f"bridge inspection not found: {trip_id}", 404)
                return
            _json(
                self,
                {
                    "status": "ok",
                    "trip_id": trip_id,
                    "inspection": record,
                    "data": record,
                },
            )
            return

        if len(parts) == 4 and parts[0] == "project-tree" and parts[2] == "location":
            project_usi = unquote(parts[1]).strip()
            loc = unquote(parts[3]).strip()
            state = PROJECT_TREES.get(project_usi)
            if not state:
                _error(self, f"project tree not found: {project_usi}", 404)
                return
            node = state["locations"].get(loc)
            if not node:
                _error(self, f"location not found: {loc}", 404)
                return
            _json(self, {"status": "ok", "project_usi": project_usi, "location": loc, "data": {"location": loc, "node": node}})
            return

        if len(parts) == 4 and parts[0] == "project-tree" and parts[2] == "instance":
            project_usi = unquote(parts[1]).strip()
            inst = unquote(parts[3]).strip()
            state = PROJECT_TREES.get(project_usi)
            if not state:
                _error(self, f"project tree not found: {project_usi}", 404)
                return
            item = state["instances"].get(inst)
            if not item:
                _error(self, f"instance not found: {inst}", 404)
                return
            _json(self, {"status": "ok", "project_usi": project_usi, "instance_ref": inst, "data": item})
            return

        if len(parts) == 4 and parts[0] == "project-tree" and parts[2] == "trace":
            project_usi = unquote(parts[1]).strip()
            inst = unquote(parts[3]).strip()
            state = PROJECT_TREES.get(project_usi)
            if not state:
                _error(self, f"project tree not found: {project_usi}", 404)
                return
            trace = state["traces"].get(inst)
            if not trace:
                _error(self, f"trace not found for instance: {inst}", 404)
                return
            _json(self, {"status": "ok", "project_usi": project_usi, "instance_ref": inst, "data": trace})
            return

        if len(parts) == 4 and parts[0] == "project-tree" and parts[2] == "stats" and parts[3] == "material-remaining":
            project_usi = unquote(parts[1]).strip()
            state = PROJECT_TREES.get(project_usi)
            if not state:
                _error(self, f"project tree not found: {project_usi}", 404)
                return
            _json(self, _material_stats(state))
            return

        _error(self, f"path not found: {path}", 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        parts = _normalized_parts(path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except Exception:
            payload = {}

        if parts == ["spu", "register"]:
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            name = _first(payload, ["name"], default=metadata.get("name"))
            owner = _first(payload, ["owner"])
            category = _first(payload, ["category"], default="unknown")
            if not name:
                _error(self, "missing required field: name or metadata.name")
                return
            if not owner:
                _error(self, "missing required field: owner")
                return
            spu_id = f"v://spu/{category}/{name}"
            spu = {
                "id": spu_id,
                "name": name,
                "owner": owner,
                "category": category,
                "status": "verified",
            }
            SPUS[spu_id] = spu
            utxo = {"id": f"utxo:spu:{name}"}
            _json(self, {"status": "success", "data": {"spu": spu, "utxo": utxo}, "spu": spu, "utxo": utxo})
            return

        if parts == ["project-tree", "generate"]:
            if not isinstance(payload, dict):
                _error(self, "payload must be object")
                return
            project_usi = str(payload.get("project_usi") or "").strip()
            structure = payload.get("structure")
            if not project_usi:
                _error(self, "missing required field: project_usi")
                return
            if not isinstance(structure, dict):
                _error(self, "missing required field: structure(object)")
                return
            state = _build_tree_state(project_usi, structure)
            PROJECT_TREES[project_usi] = state
            _json(
                self,
                {
                    "status": "ok",
                    "project_usi": project_usi,
                    "data": {
                        "project_usi": project_usi,
                        "locations": len(state["locations"]),
                        "instances": len(state["instances"]),
                    },
                },
            )
            return

        if parts == ["spu", "update"]:
            _json(
                self,
                {
                    "status": "success",
                    "spu": {"id": payload.get("spu_id") or "spu:updated", "status": "verified"},
                    "utxo": {"id": "utxo:spu:002"},
                },
            )
            return

        if parts == ["bridge", "pile", "hole-inspection", "submit"]:
            err = _validate_bridge_base_payload(payload)
            if err:
                _error(self, err)
                return
            measurements = payload.get("measurements", {})
            verdict, err = _table7_verdict(measurements)
            if err:
                _error(self, err)
                return
            trip_id = _next_id("trip")
            pile_ref = str(payload.get("pile_ref")).strip()
            record = {
                "table_no": "7",
                "trip_id": trip_id,
                "pile_ref": pile_ref,
                "verdict": verdict,
                "status": verdict,
                "pdf_ref": f"v://doc/bridge/table7/{trip_id}.pdf",
                "measurements": measurements,
                "evidence": payload.get("evidence", []),
                "signatures": payload.get("signatures") if isinstance(payload.get("signatures"), dict) else {},
            }
            BRIDGE_INSPECTIONS[trip_id] = record
            PILE_TABLE7_STATE[pile_ref] = {"trip_id": trip_id, "verdict": verdict}
            _json(
                self,
                {
                    "status": "ok",
                    "inspection": record,
                    "data": record,
                },
            )
            return

        if parts == ["bridge", "pile", "final-inspection", "submit"]:
            err = _validate_bridge_base_payload(payload)
            if err:
                _error(self, err)
                return
            pile_ref = str(payload.get("pile_ref")).strip()
            pre = PILE_TABLE7_STATE.get(pile_ref)
            if not pre or pre.get("verdict") != "qualified":
                _error(self, "table13 prerequisite failed: table7 must be qualified for same pile_ref", 409)
                return
            measurements = payload.get("measurements", {})
            verdict, err = _table13_verdict(measurements)
            if err:
                _error(self, err)
                return
            trip_id = _next_id("trip")
            record = {
                "table_no": "13",
                "trip_id": trip_id,
                "pile_ref": pile_ref,
                "table7_trip_id": pre.get("trip_id"),
                "verdict": verdict,
                "status": verdict,
                "pdf_ref": f"v://doc/bridge/table13/{trip_id}.pdf",
                "measurements": measurements,
                "evidence": payload.get("evidence", []),
                "signatures": payload.get("signatures") if isinstance(payload.get("signatures"), dict) else {},
            }
            BRIDGE_INSPECTIONS[trip_id] = record
            _json(
                self,
                {
                    "status": "ok",
                    "inspection": record,
                    "data": record,
                },
            )
            return

        if parts == ["dispatch", "launch-trip"]:
            required = [
                "trip_name",
                "executor_spu",
                "resources_utxo",
                "project_node_id",
                "context",
                "energy_consumed",
            ]
            missing = _require_keys(payload, required)
            if missing:
                _error(self, f"missing required fields: {', '.join(missing)}")
                return
            if not isinstance(payload.get("resources_utxo"), list):
                _error(self, "invalid field type: resources_utxo must be list")
                return
            if not isinstance(payload.get("context"), dict):
                _error(self, "invalid field type: context must be object")
                return
            if not isinstance(payload.get("energy_consumed"), int):
                _error(self, "invalid field type: energy_consumed must be integer")
                return

            admission_id = _next_id("admission")
            dispatch_plan_id = _next_id("dispatch")
            trip_id = _next_id("trip")
            ADMISSIONS[admission_id] = {
                "admission_id": admission_id,
                "trip_name": payload["trip_name"],
                "executor_spu": payload["executor_spu"],
                "resources_utxo": payload["resources_utxo"],
                "project_node_id": payload["project_node_id"],
                "context": payload["context"],
            }
            TRIPS[trip_id] = {
                "id": trip_id,
                "admission_id": admission_id,
                "dispatch_plan_id": dispatch_plan_id,
                "status": "running",
                "trip_name": payload["trip_name"],
                "executor_spu": payload["executor_spu"],
                "resources_utxo": payload["resources_utxo"],
                "project_node_id": payload["project_node_id"],
                "context": payload["context"],
                "energy_consumed": payload["energy_consumed"],
                "trip_template_code": "pile_construction",
                "process_log": [],
                "evidence_ids": [],
                "prc_hash": None,
                "product_utxo": None,
                "quantity": 0,
                "unit_price": 0,
            }
            _json(
                self,
                {
                    "status": "running",
                    "data": {
                        "trip_id": trip_id,
                        "admission_id": admission_id,
                        "dispatch_plan_id": dispatch_plan_id,
                        "status": "running",
                    },
                    "trip_id": trip_id,
                    "admission_id": admission_id,
                    "dispatchPlanId": dispatch_plan_id,
                },
            )
            return

        if parts == ["trip", "admission"]:
            # Compatibility with older chain
            trip_name = _first(payload, ["trip_name"])
            executor_spu = _first(payload, ["executor_spu"])
            resources_utxo = payload.get("resources_utxo")
            project_node_id = _first(payload, ["project_node_id", "projectNodeId"])
            context = payload.get("context")

            legacy_spu_ref = _first(payload, ["spu_ref", "spuRef", "spu_id"])
            legacy_executor = _first(payload, ["executor_did", "executorDid", "executor"])
            if trip_name and executor_spu and isinstance(resources_utxo, list) and project_node_id and isinstance(context, dict):
                pass
            elif legacy_spu_ref and legacy_executor:
                trip_name = trip_name or "legacy_trip"
                executor_spu = executor_spu or legacy_spu_ref
                resources_utxo = resources_utxo if isinstance(resources_utxo, list) else []
                project_node_id = project_node_id or _first(payload, ["project_node", "projectNode"], default="v://project-node/legacy")
                context = context if isinstance(context, dict) else {}
            else:
                _error(
                    self,
                    "missing required fields: trip_name, executor_spu, resources_utxo(list), project_node_id, context(object)",
                )
                return

            admission_id = _next_id("admission")
            ADMISSIONS[admission_id] = {
                "admission_id": admission_id,
                "trip_name": trip_name,
                "executor_spu": executor_spu,
                "resources_utxo": resources_utxo,
                "project_node_id": project_node_id,
                "context": context,
            }
            _json(self, {"status": "admitted", "admission_id": admission_id})
            return

        if parts == ["trip", "start"] or parts == ["trip", "execute"]:
            admission_id = _first(payload, ["admission_id", "admissionId"])
            if not admission_id:
                _error(self, "missing required field: admission_id")
                return
            admission = ADMISSIONS.get(admission_id)
            if not admission:
                _error(self, f"admission not found: {admission_id}", 404)
                return

            trip_id = _first(payload, ["trip_id", "tripId"], default=_next_id("trip"))
            trip = TRIPS.get(trip_id) or {
                "id": trip_id,
                "dispatch_plan_id": _next_id("dispatch"),
                "trip_template_code": "pile_construction",
                "process_log": [],
                "evidence_ids": [],
                "prc_hash": None,
                "product_utxo": None,
                "quantity": 0,
                "unit_price": 0,
            }
            trip.update(
                {
                    "admission_id": admission_id,
                    "status": "running",
                    "trip_name": admission.get("trip_name") or "trip",
                    "executor_spu": admission.get("executor_spu"),
                    "resources_utxo": admission.get("resources_utxo", []),
                    "project_node_id": admission.get("project_node_id"),
                    "context": admission.get("context", {}),
                }
            )
            TRIPS[trip_id] = trip
            _json(self, {"ok": True, "status": "running", "trip_id": trip_id, "admission_id": admission_id})
            return

        if len(parts) == 3 and parts[0] == "trip" and parts[2] == "execute-step":
            trip_id = unquote(parts[1])
            trip = TRIPS.get(trip_id)
            if not trip:
                _error(self, f"trip not found: {trip_id}", 404)
                return
            step = payload.get("step")
            metrics = payload.get("metrics")
            if not step:
                _error(self, "missing required field: step")
                return
            if not isinstance(metrics, dict):
                _error(self, "invalid field type: metrics must be object")
                return
            COUNTERS["step"] += 1
            log_item = {
                "stepCode": step,
                "startedAt": metrics.get("startedAt", ""),
                "endedAt": metrics.get("endedAt", ""),
                "metrics": metrics,
            }
            trip["process_log"] = trip.get("process_log", []) + [log_item]
            trip["status"] = "step_recorded"
            TRIPS[trip_id] = trip
            _json(
                self,
                {
                    "status": "step_recorded",
                    "trip_id": trip_id,
                    "data": {"trip_id": trip_id, "status": "step_recorded", "processLog": trip["process_log"]},
                },
            )
            return

        if len(parts) == 3 and parts[0] == "trip" and parts[2] == "evidence":
            trip_id = unquote(parts[1])
            trip = TRIPS.get(trip_id)
            if not trip:
                _error(self, f"trip not found: {trip_id}", 404)
                return
            evidence = payload.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                _error(self, "missing required field: evidence(list)")
                return
            evidence_ids = []
            for _item in evidence:
                COUNTERS["evidence"] += 1
                evidence_ids.append(f"evidence:{trip_id}:{COUNTERS['evidence']:04d}")
            trip["evidence_ids"] = evidence_ids
            trip["status"] = "evidence_added"
            TRIPS[trip_id] = trip
            _json(
                self,
                {
                    "status": "evidence_added",
                    "trip_id": trip_id,
                    "evidence_ids": evidence_ids,
                    "data": {"trip_id": trip_id, "evidence_ids": evidence_ids},
                },
            )
            return

        if len(parts) == 3 and parts[0] == "trip" and parts[2] == "certify":
            trip_id = unquote(parts[1])
            trip = TRIPS.get(trip_id)
            if not trip:
                _error(self, f"trip not found: {trip_id}", 404)
                return
            evidence_ids = payload.get("evidence_ids")
            quantity = payload.get("quantity")
            unit_price = payload.get("unit_price")
            if not isinstance(evidence_ids, list) or not evidence_ids:
                _error(self, "missing required field: evidence_ids(list)")
                return
            if not isinstance(quantity, (int, float)):
                _error(self, "invalid field type: quantity must be number")
                return
            if not isinstance(unit_price, (int, float)):
                _error(self, "invalid field type: unit_price must be number")
                return
            trip["status"] = "certified"
            trip["prc_hash"] = f"prc:{trip_id}:001"
            trip["product_utxo"] = f"utxo:{trip_id}:product:001"
            trip["quantity"] = float(quantity)
            trip["unit_price"] = float(unit_price)
            trip["evidence_ids"] = evidence_ids
            TRIPS[trip_id] = trip
            LEDGERS[trip_id] = _build_default_ledger(trip)
            _json(
                self,
                {
                    "status": "certified",
                    "trip_id": trip_id,
                    "prc_hash": trip["prc_hash"],
                    "product_utxo": trip["product_utxo"],
                    "verdict": "qualified",
                    "data": {
                        "trip_id": trip_id,
                        "prc_hash": trip["prc_hash"],
                        "product_utxo": trip["product_utxo"],
                        "verdict": "qualified",
                    },
                },
            )
            return

        if parts == ["trip", "evidence"]:
            trip_id = _first(payload, ["trip_id", "tripId"])
            evidence = payload.get("evidence")
            if not trip_id:
                _error(self, "missing required field: trip_id")
                return
            if not isinstance(evidence, list) or not evidence:
                _error(self, "missing required field: evidence(list)")
                return
            trip = TRIPS.get(trip_id)
            if not trip:
                _error(self, f"trip not found: {trip_id}", 404)
                return
            evidence_ids = []
            for _item in evidence:
                COUNTERS["evidence"] += 1
                evidence_ids.append(f"evidence:{trip_id}:{COUNTERS['evidence']:04d}")
            trip["evidence_ids"] = evidence_ids
            trip["status"] = "evidence_added"
            TRIPS[trip_id] = trip
            _json(self, {"status": "evidence_added", "trip_id": trip_id, "evidence_ids": evidence_ids})
            return

        if parts == ["trip", "assert"]:
            trip_id = _first(payload, ["trip_id", "tripId"])
            evidence_ids = _first(payload, ["evidence_ids", "evidenceIds"], default=[])
            if not trip_id:
                _error(self, "missing required field: trip_id")
                return
            if not isinstance(evidence_ids, list) or not evidence_ids:
                _error(self, "missing required field: evidence_ids(list)")
                return
            trip = TRIPS.get(trip_id)
            if not trip:
                _error(self, f"trip not found: {trip_id}", 404)
                return
            trip["status"] = "asserted"
            trip["prc_hash"] = f"prc:{trip_id}:001"
            TRIPS[trip_id] = trip
            _json(self, {"status": "asserted", "trip_id": trip_id, "prc_hash": trip["prc_hash"], "verdict": "qualified"})
            return

        if parts == ["trip", "mint"]:
            trip_id = _first(payload, ["trip_id", "tripId"])
            if not trip_id:
                _error(self, "missing required field: trip_id")
                return
            trip = TRIPS.get(trip_id)
            if not trip:
                _error(self, f"trip not found: {trip_id}", 404)
                return
            trip["status"] = "minted"
            trip["product_utxo"] = f"utxo:{trip_id}:product:001"
            TRIPS[trip_id] = trip
            _json(
                self,
                {
                    "status": "minted",
                    "trip_id": trip_id,
                    "product_utxo": trip["product_utxo"],
                    "utxo_id": trip["product_utxo"],
                },
            )
            return

        _error(self, f"path not found: {path}", 404)

    def log_message(self, *args):
        return


def run(host="0.0.0.0", port=8080):
    HTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    run()
