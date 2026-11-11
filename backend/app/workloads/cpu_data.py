"""Internal CPU/data workloads: checksum, compression, json_transform (spec §44).

These are pure and deterministic so the control plane can verify any result by
recomputation (spec §67 DETERMINISTIC_RECOMPUTE / HASH). The exact same
algorithms are implemented on Android in ``CpuDataExecutor.kt``; results contain
no floats so canonical JSON hashing is identical in Kotlin and Python.

Input generation (shared spec, keep in sync with Kotlin):
* ``det_stream(seed, n)``: concatenation of SHA-256(seed_utf8 || uint32_be(i)) for
  i = 0, 1, ... truncated to n bytes (incompressible, for checksum).
* ``det_text(seed, n)``: concatenation of lines ``"farmos-{seed}-{i % 97}\\n"`` for
  i = 0, 1, ... truncated to n bytes (compressible, for compression).
"""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from typing import Any

CPU_DATA_WORKLOADS = {"checksum", "compression", "json_transform"}

MAX_CHECKSUM_BYTES = 8 * 1024 * 1024
MAX_COMPRESSION_BYTES = 1024 * 1024
MAX_JSON_DOC_BYTES = 256 * 1024


class WorkloadError(ValueError):
    pass


# ── canonical JSON (shared with result hashing) ─────────────────────────────

def normalize_numbers(value: Any) -> Any:
    """Render integral floats as ints so Kotlin/org.json and Python agree."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    if isinstance(value, dict):
        return {str(k): normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_numbers(v) for v in value]
    return value


def canonical(value: Any) -> str:
    return json.dumps(normalize_numbers(value), sort_keys=True, separators=(",", ":"))


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ── deterministic inputs ─────────────────────────────────────────────────────

def det_stream(seed: str, size: int) -> bytes:
    out = bytearray()
    seed_b = seed.encode("utf-8")
    i = 0
    while len(out) < size:
        out += hashlib.sha256(seed_b + i.to_bytes(4, "big")).digest()
        i += 1
    return bytes(out[:size])


def det_text(seed: str, size: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < size:
        out += f"farmos-{seed}-{i % 97}\n".encode("utf-8")
        i += 1
    return bytes(out[:size])


def _int(payload: dict[str, Any], key: str, lo: int, hi: int) -> int:
    v = payload.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
        raise WorkloadError(f"{key} must be an integer in [{lo}, {hi}]")
    return v


def _seed(payload: dict[str, Any]) -> str:
    s = payload.get("seed")
    if not isinstance(s, str) or not (1 <= len(s) <= 128):
        raise WorkloadError("seed must be a 1-128 char string")
    return s


# ── workloads ────────────────────────────────────────────────────────────────

def run_checksum(payload: dict[str, Any]) -> dict[str, Any]:
    seed = _seed(payload)
    size = _int(payload, "size_bytes", 1, MAX_CHECKSUM_BYTES)
    rounds = payload.get("rounds", 1)
    if not isinstance(rounds, int) or not (1 <= rounds <= 16):
        raise WorkloadError("rounds must be an integer in [1, 16]")
    data = det_stream(seed, size)
    digest = hashlib.sha256(data).digest()
    for _ in range(rounds - 1):
        digest = hashlib.sha256(digest).digest()
    return {
        "status": "COMPLETED",
        "workload": "checksum",
        "algorithm": "sha256",
        "size_bytes": size,
        "rounds": rounds,
        "output_sha256": digest.hex(),
    }


def run_compression(payload: dict[str, Any], level: int | None = None) -> dict[str, Any]:
    seed = _seed(payload)
    size = _int(payload, "size_bytes", 1, MAX_COMPRESSION_BYTES)
    lvl = level if level is not None else payload.get("level", 6)
    if not isinstance(lvl, int) or not (1 <= lvl <= 9):
        raise WorkloadError("level must be an integer in [1, 9]")
    data = det_text(seed, size)
    compressed = zlib.compress(data, lvl)
    return {
        "status": "COMPLETED",
        "workload": "compression",
        "algorithm": "zlib",
        "input_size": size,
        "input_sha256": sha256_hex(data),
        "compressed_size": len(compressed),
        "compressed_b64": base64.b64encode(compressed).decode("ascii"),
    }


def _apply_op(doc: Any, op: dict[str, Any]) -> Any:
    kind = op.get("op")
    if kind == "select":
        fields = op.get("fields")
        if not isinstance(doc, list) or not isinstance(fields, list):
            raise WorkloadError("select needs a list document and fields list")
        return [{f: r.get(f) for f in fields} for r in doc if isinstance(r, dict)]
    if kind == "filter_eq":
        if not isinstance(doc, list):
            raise WorkloadError("filter_eq needs a list document")
        return [r for r in doc if isinstance(r, dict) and r.get(op.get("field")) == op.get("value")]
    if kind == "sort_by":
        if not isinstance(doc, list):
            raise WorkloadError("sort_by needs a list document")
        field = op.get("field")
        return sorted(doc, key=lambda r: canonical(r.get(field) if isinstance(r, dict) else None))
    if kind == "count":
        if not isinstance(doc, list):
            raise WorkloadError("count needs a list document")
        return len(doc)
    if kind == "sum":
        if not isinstance(doc, list):
            raise WorkloadError("sum needs a list document")
        total = 0
        for r in doc:
            v = r.get(op.get("field")) if isinstance(r, dict) else None
            if v is None:
                continue
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            if not isinstance(v, int) or isinstance(v, bool):
                raise WorkloadError("sum supports integer fields only (deterministic)")
            total += v
        return total
    raise WorkloadError(f"unsupported op {kind!r}")


def run_json_transform(payload: dict[str, Any]) -> dict[str, Any]:
    doc = payload.get("document")
    ops = payload.get("ops", [])
    if doc is None or not isinstance(ops, list) or len(ops) > 16:
        raise WorkloadError("json_transform needs document and <=16 ops")
    if len(canonical(doc)) > MAX_JSON_DOC_BYTES:
        raise WorkloadError("document too large")
    out = normalize_numbers(doc)
    for op in ops:
        if not isinstance(op, dict):
            raise WorkloadError("each op must be an object")
        out = _apply_op(out, op)
    return {
        "status": "COMPLETED",
        "workload": "json_transform",
        "ops_applied": len(ops),
        "output": out,
        "output_sha256": sha256_hex(canonical(out)),
    }


RUNNERS = {
    "checksum": run_checksum,
    "compression": run_compression,
    "json_transform": run_json_transform,
}


def execute(workload_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a CPU/data workload, returning a FAILED result (never raising) on bad input."""
    fn = RUNNERS.get(workload_type)
    if fn is None:
        return {"status": "FAILED", "workload": workload_type, "error": "unsupported workload"}
    try:
        return fn(payload or {})
    except WorkloadError as exc:
        return {"status": "FAILED", "workload": workload_type, "error": str(exc)}


def verify_compression(payload: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str]:
    """Round-trip check that is independent of the zlib implementation/version."""
    try:
        seed = _seed(payload)
        size = _int(payload, "size_bytes", 1, MAX_COMPRESSION_BYTES)
    except WorkloadError as exc:
        return False, f"bad payload: {exc}"
    expected_sha = sha256_hex(det_text(seed, size))
    if result.get("input_sha256") != expected_sha:
        return False, "input_sha256 mismatch"
    try:
        blob = base64.b64decode(result.get("compressed_b64") or "", validate=True)
        restored = zlib.decompressobj().decompress(blob, size + 1)
    except (ValueError, zlib.error) as exc:
        return False, f"undecodable compressed payload: {exc}"
    if sha256_hex(restored) != expected_sha:
        return False, "decompressed output does not match input"
    if result.get("compressed_size") != len(blob):
        return False, "compressed_size mismatch"
    if len(blob) >= size and size > 256:
        return False, "no compression achieved"
    return True, "roundtrip ok"


def validate_payload(workload_type: str, payload: dict[str, Any]) -> None:
    """Cheap parameter validation at job creation (raises WorkloadError)."""
    if workload_type == "checksum":
        _seed(payload)
        _int(payload, "size_bytes", 1, MAX_CHECKSUM_BYTES)
        r = payload.get("rounds", 1)
        if not isinstance(r, int) or not (1 <= r <= 16):
            raise WorkloadError("rounds must be an integer in [1, 16]")
    elif workload_type == "compression":
        _seed(payload)
        _int(payload, "size_bytes", 1, MAX_COMPRESSION_BYTES)
    elif workload_type == "json_transform":
        run_json_transform(payload)
