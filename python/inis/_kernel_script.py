# inis.run code-interpreter kernel.
#
# Ships inside the inis SDK packages (Python + TypeScript — see
# sdk/typescript/src/kernel_script.ts, generated from this file by
# scripts/gen-kernel-ts.py, which must be re-run after any edit here) and is
# uploaded into a session's filesystem on the first run_code() call, then
# started as a long-lived background process via start_process(). It has no
# dependency on the inis SDK itself and no guest-agent changes are needed:
# stdlib only for the core loop. matplotlib/pandas are used opportunistically
# if already importable in the target environment (e.g. the official
# "data-science" template) to produce richer results, but their absence
# never breaks plain code execution.
#
# Protocol: polls a requests directory for atomically renamed *.json request
# files ({"id", "code"}) and executes each against a persistent globals()
# dict shared across calls (the "context"), then atomically writes a *.json
# result envelope ({"id", "results": [...]}) to a results directory. The SDK
# writes request files via write-then-rename (write a temp file, then a
# separate exec() renames it into place) so this loop never observes a
# partially-written request; it does the same on the way out (write a .tmp
# file, then os.rename) so the SDK's shell-side poll loop never observes a
# partially-written result.
#
# Each result item has a "type" of "text", "image", "table", "json", or
# "error". restart_context() in the SDK kills this process outright (the
# simplest way to guarantee a fully fresh context) rather than messaging it,
# so this script has no reset protocol of its own.
import ast
import contextlib
import io
import json
import os
import sys
import time
import traceback

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/workspace/.inis_kernel"
REQ_DIR = os.path.join(ROOT, "requests")
RES_DIR = os.path.join(ROOT, "results")
ART_DIR = os.path.join(ROOT, "artifacts")

# These bound how much of a rich result (a DataFrame's rows, or
# a value's repr()) the kernel puts in one result envelope, distinct from
# the transport's own limits. The envelope is read back over the same
# buffered-exec channel as any other run_code() stdout, so it inherits that
# path's cap (protocol.MaxStdoutBytes, 4 MiB — see internal/protocol/
# protocol.go) as the real ceiling; these were previously 200/20000 with no
# stated relationship to that budget. Raised 5x — still comfortably inside
# the 4 MiB budget for realistic tables/reprs, while an agent asking for the
# last value no longer needs a second call just to see past row 200.
# truncated/row_count are still reported so a caller that needs more than
# this can tell and can re-query with an explicit .head()/slice instead of
# silently getting a partial view.
MAX_TABLE_ROWS = 1000
MAX_REPR_LEN = 100000

_globals = {"__name__": "__main__"}


def _ensure_dirs():
    for d in (REQ_DIR, RES_DIR, ART_DIR):
        os.makedirs(d, exist_ok=True)


def _try_matplotlib():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except Exception:
        return None


def _try_pandas():
    try:
        import pandas as pd

        return pd
    except Exception:
        return None


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except Exception:
        return repr(value)


def _table_result(df):
    try:
        truncated = len(df) > MAX_TABLE_ROWS
        view = df.head(MAX_TABLE_ROWS)
        rows = json.loads(view.to_json(orient="records", date_format="iso"))
        return {
            "type": "table",
            "columns": [str(c) for c in view.columns],
            "rows": rows,
            "row_count": int(len(df)),
            "truncated": truncated,
        }
    except Exception:
        return None


def capture_figures(req_id, plt):
    """Save every open matplotlib figure with at least one axes to ART_DIR
    as a PNG, close it, and return one "image" result dict per figure."""
    results = []
    if plt is None:
        return results
    for i, num in enumerate(list(plt.get_fignums())):
        fig = plt.figure(num)
        try:
            if not fig.axes:
                continue
            path = os.path.join(ART_DIR, "%s_%d.png" % (req_id, i))
            fig.savefig(path, format="png", bbox_inches="tight")
            results.append(
                {
                    "type": "image",
                    "format": "png",
                    "path": path,
                    "size": os.path.getsize(path),
                }
            )
        finally:
            plt.close(fig)
    return results


def run_cell(code, req_id, globals_dict=None):
    """Execute one cell of code against globals_dict (defaults to the
    persistent module-level context), returning a list of result dicts.
    Pure function of (code, globals_dict) plus the optional-dependency
    probes above — no filesystem polling, so it's directly unit-testable."""
    g = _globals if globals_dict is None else globals_dict
    results = []
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    plt = _try_matplotlib()
    pd = _try_pandas()
    last_value = None
    has_last_value = False

    try:
        parsed = ast.parse(code, mode="exec")
        last_expr = None
        if parsed.body and isinstance(parsed.body[-1], ast.Expr):
            last_expr = parsed.body.pop()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            if parsed.body:
                exec(compile(parsed, "<inis-run_code>", "exec"), g)
            if last_expr is not None:
                value = eval(
                    compile(ast.Expression(last_expr.value), "<inis-run_code>", "eval"),
                    g,
                )
                if value is not None:
                    last_value = value
                    has_last_value = True
    except Exception as exc:
        # Deliberately not BaseException: SystemExit/KeyboardInterrupt (e.g.
        # user code calling exit()) should kill this process rather than be
        # reported as a catchable "error" result — the same call-exit-to-
        # restart-the-kernel convention Jupyter uses. main()'s caller lets
        # SystemExit propagate; the next run_code() transparently restarts
        # the (now-dead) kernel with a fresh context, same as any other
        # kernel death.
        tb = traceback.format_exc()
        out, err = stdout_buf.getvalue(), stderr_buf.getvalue()
        if out:
            results.append({"type": "text", "stream": "stdout", "text": out})
        if err:
            results.append({"type": "text", "stream": "stderr", "text": err})
        results.append(
            {
                "type": "error",
                "ename": type(exc).__name__,
                "evalue": str(exc),
                "traceback": tb,
            }
        )
        results.extend(capture_figures(req_id, plt))
        return results

    out, err = stdout_buf.getvalue(), stderr_buf.getvalue()
    if out:
        results.append({"type": "text", "stream": "stdout", "text": out})
    if err:
        results.append({"type": "text", "stream": "stderr", "text": err})

    results.extend(capture_figures(req_id, plt))

    if has_last_value:
        handled = False
        if pd is not None:
            try:
                if isinstance(last_value, pd.DataFrame):
                    t = _table_result(last_value)
                    if t is not None:
                        results.append(t)
                        handled = True
                elif isinstance(last_value, pd.Series):
                    t = _table_result(last_value.to_frame())
                    if t is not None:
                        results.append(t)
                        handled = True
            except Exception:
                pass
        if not handled:
            if isinstance(last_value, (dict, list, tuple)):
                results.append({"type": "json", "json": _json_safe(last_value)})
            else:
                r = repr(last_value)
                if len(r) > MAX_REPR_LEN:
                    r = r[:MAX_REPR_LEN] + "... (truncated)"
                results.append({"type": "text", "stream": "result", "text": r})

    return results


def _write_result(req_id, results):
    payload = {"id": req_id, "results": results}
    tmp_path = os.path.join(RES_DIR, ".%s.tmp" % req_id)
    final_path = os.path.join(RES_DIR, "%s.json" % req_id)
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
    os.rename(tmp_path, final_path)


def main():
    _ensure_dirs()
    seen = set()
    while True:
        try:
            names = sorted(os.listdir(REQ_DIR))
        except FileNotFoundError:
            names = []
        for name in names:
            if not name.endswith(".json") or name.startswith(".") or name in seen:
                continue
            seen.add(name)
            path = os.path.join(REQ_DIR, name)
            try:
                with open(path, "r") as f:
                    req = json.load(f)
            except Exception:
                continue
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass

            req_id = req.get("id") or name[:-5]
            code = req.get("code", "")
            try:
                results = run_cell(code, req_id)
            except SystemExit:
                raise
            except Exception:
                results = [
                    {
                        "type": "error",
                        "ename": "KernelError",
                        "evalue": "kernel failed to execute request",
                        "traceback": traceback.format_exc(),
                    }
                ]
            _write_result(req_id, results)
        time.sleep(0.05)


if __name__ == "__main__":
    main()
