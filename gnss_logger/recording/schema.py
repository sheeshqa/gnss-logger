"""Stage 2 wire schemas and pure validation. Stage 1 schemas are independent."""

from copy import deepcopy
from datetime import date
from pathlib import PurePosixPath, PureWindowsPath
import math
import re

from . import CONTRACT_VERSION
from .contracts import ByteAccounting, DeviceBinding, State, TimeBinding, identifier, require_transition, utc


def obj(properties, required=None, extra=False):
    return dict(type="object", properties=properties,
                required=list(properties) if required is None else required, additionalProperties=extra)


def ref(name):
    return {"$ref": "#/$defs/" + name}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


UINT = dict(type="integer", minimum=0)
TEXT = dict(type="string", minLength=1)
UUID = dict(type="string", format="uuid-hex")
UTC = dict(type="string", format="utc")
SHA = dict(type="string", pattern="^[0-9a-f]{64}$")
ACCOUNTING = obj({**{k: UINT for k in ("received", "journal_written", "journal_synced", "pending", "unarchived", "framed", "opaque", "unclassified")},
                  "received_count_exact": dict(type="boolean")})
TIME = obj(dict(status=dict(enum=["UNDATED", "VALID", "DISCONTINUITY"]), receiver_utc=nullable(UTC),
                source=dict(enum=["NONE", "RAWX"]), leap_seconds_valid=dict(type="boolean"), generation=UINT))
BINDING = obj(dict(model=dict(enum=["ZED-F9P", "ZED-X20P"]), vid=dict(type="integer", minimum=0, maximum=65535),
                   pid=dict(type="integer", minimum=0, maximum=65535), usb_serial=nullable(TEXT), usb_location=nullable(TEXT)))
RANGE = obj(dict(journal_id=UUID, start=UINT, end=UINT))
ARTIFACT = obj(dict(artifact_id=UUID, connection_id=UUID, path=TEXT,
    kind=dict(enum=["PRIMARY", "DAILY", "UNDATED"]),
    state=dict(enum=["OPEN", "CLOSED_COMPLETE", "CLOSED_PARTIAL", "RECOVERED", "UNVERIFIED"]),
    bytes=UINT, sha256=nullable(SHA), utc_day=nullable(dict(type="string", format="date")),
    source_ranges=dict(type="array", items=RANGE)))
CONNECTION = obj(dict(connection_id=UUID, port=TEXT, opened_utc=UTC, closed_utc=nullable(UTC),
    identity_status=dict(enum=["UNIDENTIFIED", "MATCHED", "REJECTED"]),
    match_strength=dict(enum=["NONE", "USB_SERIAL", "USB_TOPOLOGY"]),
    observed_model=nullable(TEXT), firmware=nullable(TEXT), protocol=nullable(TEXT),
    vid=nullable(dict(type="integer", minimum=0, maximum=65535)),
    pid=nullable(dict(type="integer", minimum=0, maximum=65535)),
    usb_serial=nullable(TEXT), usb_location=nullable(TEXT)))
PARAMETERS = obj(dict(seconds=nullable(dict(type="number", exclusiveMinimum=0)), until_stopped=dict(type="boolean"),
    baud=dict(type="integer", minimum=1), expected_hz=dict(type="number", exclusiveMinimum=0), station=TEXT))
EVENT_DETAILS = {
    "STATE_CHANGED": obj(dict(before=dict(enum=[s.value for s in State]), after=dict(enum=[s.value for s in State]))),
    "CONNECTION_OPENED": obj(dict(port=TEXT)),
    "IDENTITY_CHECKED": obj(dict(identity_status=dict(enum=["MATCHED","REJECTED","UNIDENTIFIED"]),
        observed_model=nullable(TEXT), match_strength=dict(enum=["NONE","USB_SERIAL","USB_TOPOLOGY"]))),
    "CHECKPOINT": obj(dict(journal_id=UUID, synced_offset=UINT)),
    "GAP_OPENED": obj(dict(gap_id=UUID,last_data_utc=nullable(UTC),last_rawx_utc=nullable(UTC))),
    "GAP_CLOSED": obj(dict(gap_id=UUID,first_data_utc=UTC,first_rawx_utc=nullable(UTC),host_gap_ns=UINT,
        receiver_interval_s=nullable(dict(type="number",minimum=0)), uncertainty=dict(enum=["ESTIMATED","UNKNOWN"]))),
    "SOURCE_ERROR": obj(dict(component=TEXT,error_type=TEXT,message=dict(type="string"))),
    "STORAGE_ERROR": obj(dict(component=TEXT,error_type=TEXT,message=dict(type="string"))),
    "STOP_REQUESTED": obj(dict(reason=TEXT)),
    "STOPPED": obj(dict(exit_code=dict(type="integer",enum=[0,1,2,3,4,130]),accounting=ACCOUNTING)),
    "TIME_DISCONTINUITY": obj(dict(time_binding=TIME,previous_rawx_utc=nullable(UTC))),
    "RECOVERY": obj(dict(source=TEXT,destination=nullable(TEXT),received_count_exact=dict(type="boolean"))),
}

_PAYLOADS = {
    "manifest": dict(state=dict(enum=[s.value for s in State]), started_utc=UTC, ended_utc=nullable(UTC),
        elapsed_monotonic_ns=UINT, clock_discontinuities=UINT,
        read_only=dict(const=True), automatic_delete=dict(const=False), retention_policy=dict(enum=["keep_all"]),
        hardware_validation=dict(enum=["NOT TESTED"]), binding=BINDING, accounting=ACCOUNTING,
        parameters=PARAMETERS, output_directory=dict(type="string", format="absolute-path"),
        versions=obj(dict(gnss_logger=TEXT, python=TEXT, platform=TEXT, dependencies=obj({},required=[],extra=True))),
        connections=dict(type="array", items=CONNECTION),
        time_binding=TIME, artifacts=dict(type="array", items=ARTIFACT), stop_reason=nullable(TEXT),
        exit_code=nullable(dict(type="integer", enum=[0, 1, 2, 3, 4, 130])),
        integrity=dict(enum=["NOT_VERIFIED", "VERIFIED", "MISMATCH", "INDETERMINATE"]),
        extensions=obj({}, required=[], extra=True)),
    "event": dict(sequence=UINT, connection_id=nullable(UUID), utc=UTC, monotonic_ns=UINT,
        kind=dict(enum=["STATE_CHANGED", "CONNECTION_OPENED", "IDENTITY_CHECKED", "CHECKPOINT", "GAP_OPENED", "GAP_CLOSED", "SOURCE_ERROR", "STORAGE_ERROR", "STOP_REQUESTED", "STOPPED", "TIME_DISCONTINUITY", "RECOVERY"]),
        reason=TEXT, details=obj({}, required=[], extra=True)),
    "diagnostic": dict(utc=UTC, monotonic_ns=UINT, state=dict(enum=[s.value for s in State]),
        registration=dict(enum=["STARTING", "ACTIVE", "WAITING", "STOPPED_COMPLETE", "PARTIAL", "FAILED"]),
        phase_quality=dict(enum=["NOT_EVALUATED", "NO_DATA", "WARNINGS", "NO_INDICATORS_DETECTED"]),
        hardware_validation=dict(enum=["NOT TESTED"]), accounting=ACCOUNTING,
        last_data_utc=nullable(UTC), last_rawx_utc=nullable(UTC), last_sfrbx_utc=nullable(UTC),
        last_sync_utc=nullable(UTC), free_disk_bytes=nullable(UINT), queue_bytes=UINT,
        reconnect_attempts=UINT, gap_events=UINT, errors=dict(type="array", items=TEXT),
        extensions=obj({}, required=[], extra=True)),
    "checkpoint": dict(journal_id=UUID, utc=UTC, synced_offset=UINT,
        last_chunk_sequence=nullable(UINT), prefix_sha256=SHA, time_binding=TIME),
}


def get_schema(kind, version=1):
    if version == 2:
        from .schema_v2 import get_schema_v2
        return deepcopy(get_schema_v2(kind))
    if version != 1:
        raise ValueError('Unsupported schema version')
    if kind not in _PAYLOADS:
        raise ValueError("Unknown Stage 2 schema kind")
    properties = dict(schema_id=dict(const="gnss_logger.stage2." + kind),
        schema_version=dict(type="integer", const=1), contract_version=dict(const=CONTRACT_VERSION),
        run_id=UUID, attempt_id=UUID, **_PAYLOADS[kind])
    result = dict({"$schema": "https://json-schema.org/draft/2020-12/schema",
                   "$id": "urn:gnss-logger:stage2:" + kind + ":1"}, **obj(properties))
    if kind == "event":
        result["allOf"] = [{"if": {"properties": {"kind": {"const": name}}},
                             "then": {"properties": {"details": spec}}} for name,spec in EVENT_DETAILS.items()]
    return deepcopy(result)


def _json(value):
    """Reject non-JSON data and NaN even in extension payloads."""
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json(item)
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for item in value.values():
            _json(item)
        return
    raise ValueError("Expected finite JSON data with string keys")


def _validate(value, spec, path):
    # A deliberately limited validator for exactly the published schema vocabulary.
    if "anyOf" in spec:
        for branch in spec["anyOf"]:
            try:
                _validate(value, branch, path)
                return
            except ValueError:
                pass
        raise ValueError(path + ": no matching alternative")
    types = {"object": (dict,), "array": (list,), "integer": (int,), "number": (int, float),
             "string": (str,), "boolean": (bool,), "null": (type(None),)}
    if "type" in spec and type(value) not in types[spec["type"]]:
        raise ValueError(path + ": incorrect type")
    if "const" in spec and (type(value) is not type(spec["const"]) or value != spec["const"]):
        raise ValueError(path + ": incorrect constant")
    if "enum" in spec and not any(type(value) is type(item) and value == item for item in spec["enum"]):
        raise ValueError(path + ": unknown enum value")
    if type(value) in (int, float):
        if value < spec.get("minimum", value) or value > spec.get("maximum", value):
            raise ValueError(path + ": out of range")
        if "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]:
            raise ValueError(path + ": below exclusive minimum")
    if type(value) is str:
        if len(value) < spec.get("minLength", 0) or ("pattern" in spec and re.fullmatch(spec["pattern"], value) is None):
            raise ValueError(path + ": invalid string")
        if spec.get("format") == "uuid-hex":
            identifier(value)
        elif spec.get("format") == "utc":
            utc(value)
        elif spec.get("format") == "date":
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError(path + ": invalid UTC day")
        elif spec.get("format") == "absolute-path":
            if not (PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()):
                raise ValueError(path + ": absolute path required")
    if spec.get("type") == "object":
        fields = spec["properties"]
        if not set(spec["required"]) <= value.keys():
            raise ValueError(path + ": required field missing")
        if not spec["additionalProperties"] and value.keys() - fields.keys():
            raise ValueError(path + ": unknown field")
        for name in value.keys() & fields.keys():
            _validate(value[name], fields[name], path + "." + name)
    elif spec.get("type") == "array":
        for index, item in enumerate(value):
            _validate(item, spec["items"], f"{path}[{index}]")


def validate_record(record):
    """Validate wire structure plus cross-field byte/time/projection invariants."""
    _json(record)
    if not isinstance(record, dict):
        raise ValueError("Record must be an object")
    kind = record.get("schema_id", "").removeprefix("gnss_logger.stage2.") if isinstance(record.get("schema_id"), str) else ""
    version = record.get('schema_version')
    if type(version) is not int:
        raise ValueError('Integer schema version required')
    _validate(record, get_schema(kind, version), kind)
    if "accounting" in record:
        ByteAccounting(**record["accounting"])
    if "time_binding" in record:
        TimeBinding(**record["time_binding"])
    if kind == "manifest":
        DeviceBinding(**record["binding"])
        p = record["parameters"]
        if p["until_stopped"] != (p["seconds"] is None):
            raise ValueError("Choose one explicit duration mode")
        _manifest(record)
    if kind == "checkpoint" and (record["synced_offset"] == 0) != (record["last_chunk_sequence"] is None):
        raise ValueError("Empty checkpoints have no committed chunk sequence")
    if kind == "event":
        details = record["details"]
        if version == 2:
            from .schema_v2 import EVENT_DETAILS_V2
            spec = EVENT_DETAILS_V2.get(record['kind'], EVENT_DETAILS.get(record['kind']))
        else:
            spec = EVENT_DETAILS[record['kind']]
        _validate(details, spec, "event.details")
        if record["kind"] == "STATE_CHANGED":
            require_transition(State(details["before"]),State(details["after"]))
        if "accounting" in details:
            ByteAccounting(**details["accounting"])
        if "time_binding" in details:
            TimeBinding(**details["time_binding"])
    if version == 2:
        from .schema_v2 import invariants
        invariants(record)
    return record


def _manifest(record):
    terminal = record["state"] == "STOPPED"
    if terminal != (record["ended_utc"] is not None and record["stop_reason"] is not None and record["exit_code"] is not None):
        raise ValueError("Terminal manifest requires end time, reason and exit code")
    if not terminal and any(record[k] is not None for k in ("ended_utc", "stop_reason", "exit_code")):
        raise ValueError("Active manifest cannot claim final completion")
    if terminal and utc(record["ended_utc"]) < utc(record["started_utc"]) and not record["clock_discontinuities"]:
        raise ValueError("A backwards host clock must be reported explicitly")
    artifacts = record["artifacts"]
    connections = {c["connection_id"]: c for c in record["connections"]}
    if len(connections) != len(record["connections"]):
        raise ValueError("Duplicate connection identifier")
    expected = DeviceBinding(**record["binding"])
    for c in connections.values():
        if c["identity_status"] == "MATCHED":
            observed = dict(model=c["observed_model"], vid=c["vid"], pid=c["pid"],
                            usb_serial=c["usb_serial"], usb_location=c["usb_location"])
            strength = "USB_SERIAL" if expected.usb_serial is not None else "USB_TOPOLOGY"
            if not expected.matches(observed) or c["match_strength"] != strength:
                raise ValueError("Matched connection must satisfy the exact binding")
        elif c["match_strength"] != "NONE":
            raise ValueError("An unverified/rejected receiver cannot claim a match")
        if terminal and c["closed_utc"] is None:
            raise ValueError("Stopped attempts require closed connection intervals")
    by_id = {a["artifact_id"]: a for a in artifacts}
    if len(by_id) != len(artifacts) or len({a["path"] for a in artifacts}) != len(artifacts):
        raise ValueError("Duplicate artifact ID or path")
    ranges = {}
    for a in artifacts:
        connection = connections.get(a["connection_id"])
        if connection is None or (a["kind"] != "PRIMARY" and connection["identity_status"] != "MATCHED"):
            raise ValueError("Projections require an identified matching connection")
        if a["state"] in ("CLOSED_COMPLETE", "CLOSED_PARTIAL", "RECOVERED") and a["sha256"] is None:
            raise ValueError("Closed verified files require a computed SHA-256")
        if terminal and a["state"] == "OPEN":
            raise ValueError("Stopped attempts cannot claim an open file was finalized")
        if (a["kind"] == "DAILY") != (a["utc_day"] is not None):
            raise ValueError("Only daily files have a UTC day")
        if a["kind"] == "PRIMARY":
            if a["source_ranges"]:
                raise ValueError("Primary journal is original, not a projection")
            continue
        total = 0
        previous = {}
        for span in a["source_ranges"]:
            primary = by_id.get(span["journal_id"])
            if (not primary or primary["kind"] != "PRIMARY" or primary["connection_id"] != a["connection_id"] or
                    not 0 <= span["start"] < span["end"] <= primary["bytes"]):
                raise ValueError("Invalid source journal range")
            if span["start"] < previous.get(span["journal_id"],0):
                raise ValueError("Projection must preserve original byte order")
            previous[span["journal_id"]] = span["end"]
            ranges.setdefault(span["journal_id"], []).append((span["start"], span["end"]))
            total += span["end"] - span["start"]
        if total != a["bytes"]:
            raise ValueError("Derived size must equal the original byte ranges")
    for spans in ranges.values():
        ordered = sorted(spans)
        if any(right[0] < left[1] for left, right in zip(ordered, ordered[1:])):
            raise ValueError("A source frame cannot appear twice in the projection")
    ledger = record["accounting"]
    if sum(a["bytes"] for a in artifacts if a["kind"] == "PRIMARY") != ledger["journal_written"]:
        raise ValueError("Primary artifact sizes must match accounting")
    if sum(a["bytes"] for a in artifacts if a["kind"] != "PRIMARY") > ledger["framed"]:
        raise ValueError("Projection exceeds classified valid frame bytes")
    if terminal and ledger["pending"]:
        raise ValueError("Terminal accounting must classify remaining pending bytes")
    if terminal and record["exit_code"] == 0:
        if (ledger["unarchived"] or not ledger["received_count_exact"] or
                ledger["journal_synced"] != ledger["journal_written"] or record["integrity"] != "VERIFIED" or
                any(a["state"] != "CLOSED_COMPLETE" for a in artifacts)):
            raise ValueError("Successful completion requires verified, fully synced storage")
