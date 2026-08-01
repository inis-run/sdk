"""Tests for inis-run/scripts/detect_environment.py — the deterministic,
no-network project detector SKILL.md section 1 tells an agent to run
before picking a package. Exercises real file-tree fixtures under
tmp_path; makes no network call and never touches INIS_API_KEY (there is
no credential concept in this script at all)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_DIR / "inis-run" / "scripts"))

import detect_environment as de  # noqa: E402


def test_no_manifest_files_falls_back_to_unknown(tmp_path: Path) -> None:
    result = de.detect(tmp_path)
    assert result["matches"] == []
    assert result["fallback"][0]["ecosystem"] == "unknown"
    assert "mcp.inis.run" in result["fallback"][0]["note"]


def test_plain_python_project_falls_back_to_core_sdk(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["requests>=2"]\n', encoding="utf-8"
    )
    result = de.detect(tmp_path)
    assert result["matches"] == []
    assert result["python"]["package_manager"] == "pip"
    assert {"ecosystem": "python", "adapter": "inis", "install": "pip install inis"} in result["fallback"]


def test_pydantic_ai_dependency_is_detected(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["pydantic-ai>=1.71,<3", "httpx"]\n',
        encoding="utf-8",
    )
    result = de.detect(tmp_path)
    assert {
        "ecosystem": "python",
        "framework": "pydantic-ai",
        "adapter": "inis-pydantic-ai",
        "install": "pip install inis-pydantic-ai",
    } in result["matches"]


def test_poetry_lock_selects_poetry_package_manager(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    result = de.detect(tmp_path)
    assert result["python"]["package_manager"] == "poetry"


def test_uv_lock_selects_uv_package_manager(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    result = de.detect(tmp_path)
    assert result["python"]["package_manager"] == "uv"


def test_requirements_txt_detects_deepagents(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("deepagents==0.7.1\nhttpx\n", encoding="utf-8")
    result = de.detect(tmp_path)
    assert {
        "ecosystem": "python",
        "framework": "deepagents",
        "adapter": "langchain-inis",
        "install": "pip install langchain-inis",
    } in result["matches"]


def test_mastra_dependency_is_detected(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"@mastra/core": "^1.55.0"}}),
        encoding="utf-8",
    )
    result = de.detect(tmp_path)
    assert {
        "ecosystem": "node",
        "framework": "@mastra/core",
        "adapter": "@inis-run/mastra",
        "install": "npm install @inis-run/mastra",
    } in result["matches"]


def test_plain_node_project_falls_back_to_core_sdk(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"express": "^4"}}), encoding="utf-8"
    )
    result = de.detect(tmp_path)
    assert result["matches"] == []
    assert {"ecosystem": "node", "adapter": "@inis-run/sdk", "install": "npm install @inis-run/sdk"} in result["fallback"]


def test_pnpm_lock_selects_pnpm_package_manager(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    result = de.detect(tmp_path)
    assert result["node"]["package_manager"] == "pnpm"


def test_openai_agents_js_dependency_is_detected(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "devDependencies": {"@openai/agents-core": "^0.14.1"}}),
        encoding="utf-8",
    )
    result = de.detect(tmp_path)
    frameworks = {m["framework"] for m in result["matches"]}
    assert "@openai/agents-core" in frameworks


def test_mixed_python_and_node_project_reports_both(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["google-adk"]\n', encoding="utf-8"
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "y", "dependencies": {"@strands-agents/sdk": "^1"}}),
        encoding="utf-8",
    )
    result = de.detect(tmp_path)
    frameworks = {m["framework"] for m in result["matches"]}
    assert frameworks == {"google-adk", "@strands-agents/sdk"}


def test_format_report_does_not_raise(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    result = de.detect(tmp_path)
    report = de.format_report(result)
    assert str(tmp_path) in report


def test_main_exits_zero(tmp_path: Path, capsys) -> None:
    rc = de.main(["detect_environment.py", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(tmp_path) in out


def test_regex_fallback_matches_tomllib_for_pep621_dependencies() -> None:
    """Python < 3.11 has no stdlib tomllib; detect_environment.py falls
    back to a regex scan (see _pyproject_dependency_names_via_regex) so it
    keeps working on the same Python range the core SDK itself supports
    (>=3.10). Prove the two extraction paths agree on a real shape."""
    text = (
        '[project]\nname = "x"\n'
        'dependencies = ["pydantic-ai>=1.71,<3", "httpx>=0.27"]\n'
        "\n[project.optional-dependencies]\n"
        'dev = ["pytest", "mypy"]\n'
    )
    assert de._pyproject_dependency_names_via_regex(text) == {
        "pydantic-ai", "httpx", "pytest", "mypy",
    }
    if de.tomllib is not None:
        assert (
            de._pyproject_dependency_names_via_tomllib(text)
            == de._pyproject_dependency_names_via_regex(text)
        )


def test_regex_fallback_matches_tomllib_for_poetry_dependencies() -> None:
    text = (
        "[tool.poetry.dependencies]\n"
        'python = "^3.10"\n'
        'deepagents = "^0.7.1"\n'
        "\n[tool.poetry.group.dev.dependencies]\n"
        'pytest = "*"\n'
    )
    fallback = de._pyproject_dependency_names_via_regex(text)
    assert "deepagents" in fallback
    if de.tomllib is not None:
        assert de._pyproject_dependency_names_via_tomllib(text) == fallback
