"""Async twin of tests/test_session_interpreter.py: same scenarios against
AsyncSession.run_code / restart_context, same FakeInterpreterGuest.
"""

from __future__ import annotations

import pytest

from inis.async_client import AsyncSession
from inis.client import InterpreterResult
from tests.interpreter_fake_guest import FakeInterpreterGuest


def make_session(**overrides) -> AsyncSession:
    kwargs = dict(base_url="https://api.inis.run", token="tok_abc")
    kwargs.update(overrides)
    session = AsyncSession(**kwargs)
    session.session_id = "sess_123"
    return session


class TestContextPersistence:
    async def test_variables_survive_across_calls(self, fake_http_async):
        guest = FakeInterpreterGuest()
        fake_http_async(guest.handle)
        session = make_session()

        first = await session.run_code("x = 41")
        second = await session.run_code("x + 1")

        assert first == []
        assert second[0].type == "text"
        assert second[0].stream == "result"
        assert second[0].text == "42"

    async def test_first_call_installs_and_starts_the_kernel_once(self, fake_http_async):
        guest = FakeInterpreterGuest()
        transport = fake_http_async(guest.handle)
        session = make_session()

        await session.run_code("a = 1")
        await session.run_code("a")

        start_process_calls = [c for c in transport.calls if c.path.endswith("/processes")]
        assert len(start_process_calls) == 1

    async def test_json_result_for_dict_last_expression(self, fake_http_async):
        guest = FakeInterpreterGuest()
        fake_http_async(guest.handle)
        session = make_session()

        results = await session.run_code('{"a": 1, "b": [1, 2, 3]}')

        assert results == [InterpreterResult(type="json", json={"a": 1, "b": [1, 2, 3]})]


class TestErrorPath:
    async def test_uncaught_exception_returns_structured_error_and_keeps_context(
        self, fake_http_async
    ):
        guest = FakeInterpreterGuest()
        fake_http_async(guest.handle)
        session = make_session()

        await session.run_code("x = 41")
        results = await session.run_code("raise ValueError('boom')")

        err = results[0]
        assert err.type == "error"
        assert err.ename == "ValueError"
        assert err.evalue == "boom"
        assert "ValueError: boom" in err.traceback

        after = await session.run_code("x + 1")
        assert after[0].text == "42"


class TestKernelDeathRecovery:
    async def test_killed_kernel_auto_restarts_with_fresh_context(self, fake_http_async):
        guest = FakeInterpreterGuest()
        fake_http_async(guest.handle)
        session = make_session()

        await session.run_code("x = 41")
        await session.kill_process("inis-kernel")
        assert guest.process_state == "exited"

        results = await session.run_code("x")
        assert results[0].type == "error"
        assert results[0].ename == "NameError"

        await session.run_code("x = 5")
        again = await session.run_code("x")
        assert again[0].text == "5"


class TestRestartContext:
    async def test_restart_context_clears_globals(self, fake_http_async):
        guest = FakeInterpreterGuest()
        fake_http_async(guest.handle)
        session = make_session()

        await session.run_code("x = 41")
        await session.restart_context()
        results = await session.run_code("x")

        assert results[0].type == "error"
        assert results[0].ename == "NameError"


class TestRichOutput:
    async def test_dataframe_returns_table_result(self, fake_http_async):
        pytest.importorskip("pandas")
        guest = FakeInterpreterGuest()
        fake_http_async(guest.handle)
        session = make_session()

        results = await session.run_code(
            "import pandas as pd\npd.DataFrame({'a': [1, 2], 'b': [3, 4]})"
        )

        tables = [r for r in results if r.type == "table"]
        assert len(tables) == 1
        assert tables[0].columns == ["a", "b"]
        assert tables[0].row_count == 2

    async def test_matplotlib_figure_returns_image_result_with_bytes(self, fake_http_async):
        pytest.importorskip("matplotlib")
        guest = FakeInterpreterGuest()
        fake_http_async(guest.handle)
        session = make_session()

        results = await session.run_code(
            "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [1, 4, 9])"
        )

        images = [r for r in results if r.type == "image"]
        assert len(images) == 1
        assert images[0].data[:8] == b"\x89PNG\r\n\x1a\n"
