"""Tests for Session.run_code / Session.restart_context, driven
against tests/interpreter_fake_guest.FakeInterpreterGuest — a faithful
in-process simulation of the guest side that runs the real kernel-script
cell-execution logic, not canned responses.
"""

from __future__ import annotations

import pytest

from inis.client import InterpreterResult, Session
from tests.interpreter_fake_guest import FakeInterpreterGuest


def make_session(**overrides) -> Session:
    kwargs = dict(base_url="https://api.inis.run", token="tok_abc")
    kwargs.update(overrides)
    session = Session(**kwargs)
    session.session_id = "sess_123"
    return session


class TestContextPersistence:
    def test_variables_survive_across_calls(self, fake_http):
        guest = FakeInterpreterGuest()
        fake_http(guest.handle)
        session = make_session()

        first = session.run_code("x = 41")
        second = session.run_code("x + 1")

        assert first == []
        assert len(second) == 1
        assert second[0].type == "text"
        assert second[0].stream == "result"
        assert second[0].text == "42"

    def test_first_call_installs_and_starts_the_kernel_once(self, fake_http):
        guest = FakeInterpreterGuest()
        transport = fake_http(guest.handle)
        session = make_session()

        session.run_code("a = 1")
        session.run_code("a")

        start_process_calls = [c for c in transport.calls if c.path.endswith("/processes")]
        assert len(start_process_calls) == 1, "kernel should only be (re)started once across calls"

    def test_json_result_for_dict_last_expression(self, fake_http):
        guest = FakeInterpreterGuest()
        fake_http(guest.handle)
        session = make_session()

        results = session.run_code('{"a": 1, "b": [1, 2, 3]}')

        assert results == [InterpreterResult(type="json", json={"a": 1, "b": [1, 2, 3]})]


class TestErrorPath:
    def test_uncaught_exception_returns_structured_error_and_keeps_context(self, fake_http):
        guest = FakeInterpreterGuest()
        fake_http(guest.handle)
        session = make_session()

        session.run_code("x = 41")
        results = session.run_code("raise ValueError('boom')")

        assert len(results) == 1
        err = results[0]
        assert err.type == "error"
        assert err.ename == "ValueError"
        assert err.evalue == "boom"
        assert "ValueError: boom" in err.traceback

        # context must still be usable afterwards
        after = session.run_code("x + 1")
        assert after[0].text == "42"


class TestKernelDeathRecovery:
    def test_killed_kernel_auto_restarts_with_fresh_context(self, fake_http):
        guest = FakeInterpreterGuest()
        fake_http(guest.handle)
        session = make_session()

        session.run_code("x = 41")
        assert guest.process_state == "running"

        session.kill_process("inis-kernel")
        assert guest.process_state == "exited"

        # x is gone (fresh context) but the call itself doesn't raise and
        # the interpreter is usable again immediately.
        results = session.run_code("x")
        assert results[0].type == "error"
        assert results[0].ename == "NameError"

        session.run_code("x = 5")
        again = session.run_code("x")
        assert again[0].text == "5"


class TestRestartContext:
    def test_restart_context_clears_globals(self, fake_http):
        guest = FakeInterpreterGuest()
        fake_http(guest.handle)
        session = make_session()

        session.run_code("x = 41")
        session.restart_context()
        results = session.run_code("x")

        assert results[0].type == "error"
        assert results[0].ename == "NameError"


class TestRichOutput:
    def test_dataframe_returns_table_result(self, fake_http):
        pytest.importorskip("pandas")
        guest = FakeInterpreterGuest()
        fake_http(guest.handle)
        session = make_session()

        results = session.run_code(
            "import pandas as pd\npd.DataFrame({'a': [1, 2], 'b': [3, 4]})"
        )

        tables = [r for r in results if r.type == "table"]
        assert len(tables) == 1
        assert tables[0].columns == ["a", "b"]
        assert tables[0].rows == [{"a": 1, "b": 3}, {"a": 2, "b": 4}]
        assert tables[0].row_count == 2
        assert tables[0].truncated is False

    def test_matplotlib_figure_returns_image_result_with_bytes(self, fake_http):
        pytest.importorskip("matplotlib")
        guest = FakeInterpreterGuest()
        fake_http(guest.handle)
        session = make_session()

        results = session.run_code(
            "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [1, 4, 9])"
        )

        images = [r for r in results if r.type == "image"]
        assert len(images) == 1
        assert images[0].format == "png"
        assert images[0].data is not None
        assert images[0].data[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(images[0].data) > 100
