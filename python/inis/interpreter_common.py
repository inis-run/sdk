"""Shared, transport-agnostic pieces of the code-interpreter layer:
path/protocol constants, the request/result envelope helpers, and the
InterpreterResult type. inis.client.Session and inis.async_client.AsyncSession
both build on this — the only difference between them is which I/O primitives
(exec/write_file/read_file/...) drive the protocol, sync vs. async.

See sdk/python/inis/_kernel_script.py for the guest-side half of this
protocol and the design rationale (no guest-agent or template changes; a
small stdlib-only script uploaded and run as a background process).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KERNEL_SOURCE: str = Path(__file__).with_name("_kernel_script.py").read_text()
"""The exact source of the guest-side kernel script, read from disk at
import time so the single copy in _kernel_script.py is always what ships —
nothing to keep in sync by hand on the Python side. (The TypeScript SDK
carries a generated copy — see scripts/gen-kernel-ts.py.)"""

KERNEL_VERSION: str = hashlib.sha256(KERNEL_SOURCE.encode("utf-8")).hexdigest()[:16]
"""Content hash of KERNEL_SOURCE. Session/AsyncSession write this alongside
the script as a version marker in the guest; a mismatch (older marker, or
none at all) means "re-upload and (re)start" — see _ensure_kernel()."""

KERNEL_NAME = "inis-kernel"
ROOT_DIR = "/workspace/.inis_kernel"
KERNEL_PATH = f"{ROOT_DIR}/kernel.py"
VERSION_PATH = f"{ROOT_DIR}/version"
REQUESTS_DIR = f"{ROOT_DIR}/requests"
RESULTS_DIR = f"{ROOT_DIR}/results"
ARTIFACTS_DIR = f"{ROOT_DIR}/artifacts"

DEFAULT_TIMEOUT_MS = 30_000
POLL_INTERVAL_S = 0.05
_EXEC_TIMEOUT_BUFFER_MS = 10_000
TIMEOUT_MARKER = "__inis_timeout__"


@dataclass
class InterpreterResult:
    """One typed piece of run_code() output.

    type is one of:
      "text"  — stdout/stderr captured during the cell, or the repr() of its
                last expression (stream distinguishes which: "stdout",
                "stderr", or "result").
      "image" — a matplotlib figure captured on show/implicit display.
                format is "png"; data holds the decoded image bytes.
      "table" — a pandas DataFrame/Series last expression, as columns +
                rows (capped at 1000 rows — see row_count/truncated for the
                untruncated count).
      "json"  — a dict/list/tuple last expression that isn't a DataFrame.
      "error" — an uncaught exception: ename/evalue/traceback. The
                interpreter context is untouched and stays usable.
    """

    type: str
    text: str | None = None
    stream: str | None = None
    format: str | None = None
    data: bytes | None = None
    size: int | None = None
    columns: list[str] | None = None
    rows: list[dict[str, Any]] | None = None
    row_count: int | None = None
    truncated: bool | None = None
    json: Any | None = None
    ename: str | None = None
    evalue: str | None = None
    traceback: str | None = None
    path: str | None = None  # guest-side artifact path (type=="image")


def new_request_id() -> str:
    return uuid.uuid4().hex


def mkdirs_command() -> str:
    return f"mkdir -p '{REQUESTS_DIR}' '{RESULTS_DIR}' '{ARTIFACTS_DIR}'"


def request_paths(req_id: str) -> tuple[str, str]:
    """(tmp_path, final_path) for a request file — the SDK writes the
    content to tmp_path first, then a single exec() both renames it into
    final_path and waits for the matching result, so the kernel's polling
    loop never observes a partially-written request."""
    return f"{REQUESTS_DIR}/.{req_id}.tmp", f"{REQUESTS_DIR}/{req_id}.json"


def result_path(req_id: str) -> str:
    return f"{RESULTS_DIR}/{req_id}.json"


def encode_request(req_id: str, code: str) -> str:
    return json.dumps({"id": req_id, "code": code})


def exec_timeout_ms(timeout_ms: int) -> int:
    """Timeout to pass to exec() itself: comfortably above the in-guest
    wait loop's own timeout so that loop — not the HTTP transport — is what
    fires first and returns the graceful TIMEOUT_MARKER."""
    return timeout_ms + _EXEC_TIMEOUT_BUFFER_MS


def wait_command(req_id: str, tmp_path: str, final_path: str, timeout_ms: int) -> str:
    """Shell command run via exec(): atomically renames the request file
    into place, then polls in-guest (no extra HTTP round trips) for the
    matching result file, printing it once it appears. Gives up after
    timeout_ms and prints TIMEOUT_MARKER instead of hanging forever."""
    res_path = result_path(req_id)
    iterations = max(1, int(timeout_ms / (POLL_INTERVAL_S * 1000)) + 1)
    return (
        f"mv '{tmp_path}' '{final_path}' && "
        f"i=0; while [ ! -f '{res_path}' ]; do "
        f"i=$((i+1)); if [ $i -ge {iterations} ]; then break; fi; "
        f"sleep {POLL_INTERVAL_S}; done; "
        f"if [ -f '{res_path}' ]; then cat '{res_path}'; rm -f '{res_path}'; "
        f'else echo \'{{"{TIMEOUT_MARKER}": true}}\'; fi'
    )


def parse_envelope(stdout: str, req_id: str) -> list[dict[str, Any]] | None:
    """Parse the wait_command's stdout. Returns the raw result-item dicts on
    success, or None if the kernel didn't answer in time (TIMEOUT_MARKER) or
    the output wasn't a well-formed envelope for this request — both mean
    "go check whether the kernel is still alive"."""
    stdout = (stdout or "").strip()
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get(TIMEOUT_MARKER):
        return None
    if payload.get("id") != req_id:
        return None
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    return results


def raw_to_result(raw: dict[str, Any]) -> InterpreterResult:
    return InterpreterResult(
        type=raw.get("type", "text"),
        text=raw.get("text"),
        stream=raw.get("stream"),
        format=raw.get("format"),
        size=raw.get("size"),
        columns=raw.get("columns"),
        rows=raw.get("rows"),
        row_count=raw.get("row_count"),
        truncated=raw.get("truncated"),
        json=raw.get("json"),
        ename=raw.get("ename"),
        evalue=raw.get("evalue"),
        traceback=raw.get("traceback"),
        path=raw.get("path"),
    )


def timeout_result(timeout_ms: int) -> InterpreterResult:
    return InterpreterResult(
        type="error",
        ename="TimeoutError",
        evalue=f"run_code timed out after {timeout_ms}ms (context left intact)",
    )
