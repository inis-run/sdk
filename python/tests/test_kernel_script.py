"""Direct unit tests of inis._kernel_script — the guest-side code-
interpreter kernel — exercising run_cell() against an in-memory
globals dict. No filesystem polling, no HTTP: this is plain Python tested
like any other module.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from inis import _kernel_script as kernel


@pytest.fixture(autouse=True)
def _isolated_art_dir(monkeypatch):
    monkeypatch.setattr(kernel, "ART_DIR", tempfile.mkdtemp())


def fresh_globals() -> dict:
    return {"__name__": "__main__"}


class TestPersistence:
    def test_assignment_produces_no_result(self):
        g = fresh_globals()
        assert kernel.run_cell("x = 41", "r1", g) == []

    def test_later_cell_sees_earlier_assignment(self):
        g = fresh_globals()
        kernel.run_cell("x = 41", "r1", g)
        results = kernel.run_cell("x + 1", "r2", g)
        assert results == [{"type": "text", "stream": "result", "text": "42"}]

    def test_function_and_class_definitions_persist(self):
        g = fresh_globals()
        kernel.run_cell("def double(n):\n    return n * 2", "r1", g)
        kernel.run_cell("class Box:\n    def __init__(self, v):\n        self.v = v", "r2", g)
        results = kernel.run_cell("double(Box(21).v)", "r3", g)
        assert results == [{"type": "text", "stream": "result", "text": "42"}]


class TestStdoutStderr:
    def test_print_is_captured_as_stdout_text_result(self):
        g = fresh_globals()
        results = kernel.run_cell("print('hello')", "r1", g)
        assert results == [{"type": "text", "stream": "stdout", "text": "hello\n"}]

    def test_stderr_writes_are_captured_separately(self):
        g = fresh_globals()
        results = kernel.run_cell(
            "import sys\nsys.stderr.write('warn\\n')", "r1", g
        )
        assert {"type": "text", "stream": "stderr", "text": "warn\n"} in results


class TestLastExpression:
    def test_repr_of_last_expression(self):
        g = fresh_globals()
        assert kernel.run_cell("[1, 2, 3][0]", "r1", g) == [
            {"type": "text", "stream": "result", "text": "1"}
        ]

    def test_none_last_expression_produces_no_result(self):
        g = fresh_globals()
        assert kernel.run_cell("print('x'); None", "r1", g) == [
            {"type": "text", "stream": "stdout", "text": "x\n"}
        ]

    def test_dict_last_expression_is_json_result(self):
        g = fresh_globals()
        results = kernel.run_cell("{'a': 1, 'b': [1, 2]}", "r1", g)
        assert results == [{"type": "json", "json": {"a": 1, "b": [1, 2]}}]

    def test_long_repr_is_truncated(self):
        g = fresh_globals()
        n = kernel.MAX_REPR_LEN + 10000
        results = kernel.run_cell(f"'x' * {n}", "r1", g)
        assert len(results) == 1
        assert results[0]["type"] == "text"
        assert results[0]["text"].endswith("... (truncated)")
        assert len(results[0]["text"]) < n


class TestErrors:
    def test_uncaught_exception_is_structured_error_with_traceback(self):
        g = fresh_globals()
        results = kernel.run_cell("raise ValueError('boom')", "r1", g)
        assert len(results) == 1
        err = results[0]
        assert err["type"] == "error"
        assert err["ename"] == "ValueError"
        assert err["evalue"] == "boom"
        assert "ValueError: boom" in err["traceback"]

    def test_system_exit_propagates_instead_of_becoming_an_error_result(self):
        # exit()/sys.exit() should kill the kernel process (main() lets
        # SystemExit propagate out of its loop) so the next run_code()
        # transparently restarts it — the same convention Jupyter uses —
        # rather than being swallowed as a catchable "error" result.
        g = fresh_globals()
        with pytest.raises(SystemExit):
            kernel.run_cell("import sys\nsys.exit(1)", "r1", g)

    def test_syntax_error_is_structured_error_not_a_crash(self):
        g = fresh_globals()
        results = kernel.run_cell("def broken(:", "r1", g)
        assert results[0]["type"] == "error"
        assert results[0]["ename"] == "SyntaxError"

    def test_context_survives_a_prior_error(self):
        g = fresh_globals()
        kernel.run_cell("x = 41", "r1", g)
        kernel.run_cell("raise ValueError('boom')", "r2", g)
        results = kernel.run_cell("x + 1", "r3", g)
        assert results == [{"type": "text", "stream": "result", "text": "42"}]

    def test_stdout_before_the_exception_is_still_captured(self):
        g = fresh_globals()
        results = kernel.run_cell("print('before')\nraise ValueError('boom')", "r1", g)
        assert {"type": "text", "stream": "stdout", "text": "before\n"} in results
        assert any(r["type"] == "error" for r in results)


class TestRichOutput:
    def test_dataframe_becomes_capped_table_result(self):
        pd = pytest.importorskip("pandas")
        g = fresh_globals()
        n = kernel.MAX_TABLE_ROWS + 500
        results = kernel.run_cell(
            f"import pandas as pd\npd.DataFrame({{'a': range({n}), 'b': range({n})}})", "r1", g
        )
        tables = [r for r in results if r["type"] == "table"]
        assert len(tables) == 1
        assert tables[0]["columns"] == ["a", "b"]
        assert tables[0]["row_count"] == n
        assert tables[0]["truncated"] is True
        assert len(tables[0]["rows"]) == kernel.MAX_TABLE_ROWS

    def test_series_becomes_table_result(self):
        pytest.importorskip("pandas")
        g = fresh_globals()
        results = kernel.run_cell(
            "import pandas as pd\npd.Series([1, 2, 3], name='v')", "r1", g
        )
        tables = [r for r in results if r["type"] == "table"]
        assert len(tables) == 1
        assert tables[0]["row_count"] == 3

    def test_matplotlib_figure_saved_as_png_artifact(self):
        pytest.importorskip("matplotlib")
        g = fresh_globals()
        results = kernel.run_cell(
            "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [1, 4, 9])", "r1", g
        )
        images = [r for r in results if r["type"] == "image"]
        assert len(images) == 1
        assert images[0]["format"] == "png"
        assert os.path.exists(images[0]["path"])
        with open(images[0]["path"], "rb") as f:
            assert f.read(8) == b"\x89PNG\r\n\x1a\n"

    def test_figure_with_no_axes_is_not_captured(self):
        pytest.importorskip("matplotlib")
        g = fresh_globals()
        results = kernel.run_cell("import matplotlib.pyplot as plt\nplt.figure()", "r1", g)
        assert not [r for r in results if r["type"] == "image"]

    def test_matplotlib_absent_falls_back_gracefully(self, monkeypatch):
        monkeypatch.setattr(kernel, "_try_matplotlib", lambda: None)
        g = fresh_globals()
        results = kernel.run_cell("x = 1 + 1\nx", "r1", g)
        assert results == [{"type": "text", "stream": "result", "text": "2"}]
