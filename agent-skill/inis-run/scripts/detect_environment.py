#!/usr/bin/env python3
"""Detect a target project's language, package manager, and any agent
framework this skill has a native inis.run adapter for.

Deterministic and read-only: it inspects `pyproject.toml`,
`requirements*.txt`, `Pipfile`, `package.json`, and lockfiles already
present in the project directory. It makes no network call, installs
nothing, and never touches INIS_API_KEY or any other credential.

Usage:
    python3 detect_environment.py [project_dir]   # defaults to cwd

Exit code is always 0 — this is advisory, not a gate. See SKILL.md section
1 for the full routing table this script's recommendation is drawn from.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import tomllib  # Python >= 3.11
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    tomllib = None  # falls back to a regex scan below; the core SDK's own
    # supported range starts at Python 3.10 (sdk/python/pyproject.toml),
    # which predates the stdlib tomllib module, so this script has to keep
    # working without it rather than requiring a newer interpreter than the
    # SDK it's routing to.

# framework dependency name (lowercased, as it would appear in a manifest)
# -> (adapter id, install command, ecosystem)
_PYTHON_FRAMEWORK_ADAPTERS: dict[str, tuple[str, str]] = {
    "pydantic-ai": ("inis-pydantic-ai", "pip install inis-pydantic-ai"),
    "deepagents": ("langchain-inis", "pip install langchain-inis"),
    "openai-agents": ("inis-openai-agents", "pip install inis-openai-agents"),
    "strands-agents": ("inis-strands-agents", "pip install inis-strands-agents"),
    "agent-framework": ("inis-agent-framework", "pip install inis-agent-framework"),
    "agent-framework-core": ("inis-agent-framework", "pip install inis-agent-framework"),
    "google-adk": ("inis-google-adk", "pip install inis-google-adk"),
}

_NODE_FRAMEWORK_ADAPTERS: dict[str, tuple[str, str]] = {
    "ai": ("@inis-run/ai-sdk", "npm install @inis-run/ai-sdk @inis-run/sdk ai zod"),
    "@mastra/core": ("@inis-run/mastra", "npm install @inis-run/mastra"),
    "@openai/agents": ("@inis-run/openai-agents", "npm install @inis-run/openai-agents"),
    "@openai/agents-core": ("@inis-run/openai-agents", "npm install @inis-run/openai-agents"),
    "@strands-agents/sdk": ("@inis-run/strands-agents", "npm install @inis-run/strands-agents"),
}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _pyproject_dependency_names_via_tomllib(text: str) -> set[str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return set()
    names: set[str] = set()
    for dep in data.get("project", {}).get("dependencies", []) or []:
        names.add(_dep_base_name(dep))
    for group in (data.get("project", {}).get("optional-dependencies", {}) or {}).values():
        for dep in group:
            names.add(_dep_base_name(dep))
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
    for dep_name in poetry_deps:
        names.add(dep_name.lower())
    return names


def _pyproject_dependency_names_via_regex(text: str) -> set[str]:
    """Best-effort fallback for interpreters without stdlib `tomllib`
    (Python < 3.11). Not a real TOML parser -- just enough regex to pull
    dependency-looking strings out of `[project] dependencies = [...]`,
    `optional-dependencies` tables, and a `[tool.poetry.dependencies]`
    table, which covers every pyproject.toml shape this skill's own
    adapters use."""
    names: set[str] = set()
    for arr_match in re.finditer(r"dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL):
        for dep_match in re.finditer(r"""["']([^"']+)["']""", arr_match.group(1)):
            names.add(_dep_base_name(dep_match.group(1)))

    optional_match = re.search(
        r"\[project\.optional-dependencies\](.*?)(?:\n\[|\Z)", text, re.DOTALL
    )
    if optional_match:
        for arr_match in re.finditer(r"=\s*\[(.*?)\]", optional_match.group(1), re.DOTALL):
            for dep_match in re.finditer(r"""["']([^"']+)["']""", arr_match.group(1)):
                names.add(_dep_base_name(dep_match.group(1)))

    poetry_match = re.search(r"\[tool\.poetry\.dependencies\](.*?)(?:\n\[|\Z)", text, re.DOTALL)
    if poetry_match:
        for line_match in re.finditer(r"^([A-Za-z0-9_.\-]+)\s*=", poetry_match.group(1), re.MULTILINE):
            names.add(line_match.group(1).lower())

    return names


def _python_dependency_names(project_dir: Path) -> set[str]:
    names: set[str] = set()

    pyproject = project_dir / "pyproject.toml"
    if pyproject.is_file():
        text = _read_text(pyproject) or ""
        if tomllib is not None:
            names |= _pyproject_dependency_names_via_tomllib(text)
        else:
            names |= _pyproject_dependency_names_via_regex(text)

    for req_file in project_dir.glob("requirements*.txt"):
        text = _read_text(req_file) or ""
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            names.add(_dep_base_name(line))

    pipfile = project_dir / "Pipfile"
    if pipfile.is_file():
        text = _read_text(pipfile) or ""
        for match in re.finditer(r'^([A-Za-z0-9_.\-]+)\s*=', text, re.MULTILINE):
            names.add(match.group(1).lower())

    return names


def _dep_base_name(spec: str) -> str:
    # "pydantic-ai>=1.71,<3" / "pydantic_ai==2.0" -> "pydantic-ai"
    name = re.split(r"[<>=!~\[; ]", spec.strip(), maxsplit=1)[0]
    return name.strip().lower()


def _python_package_manager(project_dir: Path) -> str:
    if (project_dir / "uv.lock").is_file():
        return "uv"
    if (project_dir / "poetry.lock").is_file():
        return "poetry"
    if (project_dir / "Pipfile.lock").is_file():
        return "pipenv"
    return "pip"


def _node_dependency_names(project_dir: Path) -> set[str]:
    package_json = project_dir / "package.json"
    if not package_json.is_file():
        return set()
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    names: set[str] = set()
    for field in ("dependencies", "devDependencies", "peerDependencies"):
        names.update(k.lower() for k in (data.get(field) or {}).keys())
    return names


def _node_package_manager(project_dir: Path) -> str:
    if (project_dir / "bun.lockb").is_file() or (project_dir / "bun.lock").is_file():
        return "bun"
    if (project_dir / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (project_dir / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def detect(project_dir: Path) -> dict:
    result: dict = {"project_dir": str(project_dir), "matches": []}

    has_python = (project_dir / "pyproject.toml").is_file() or any(
        project_dir.glob("requirements*.txt")
    ) or (project_dir / "Pipfile").is_file()
    has_node = (project_dir / "package.json").is_file()

    if has_python:
        py_names = _python_dependency_names(project_dir)
        py_pm = _python_package_manager(project_dir)
        result["python"] = {"package_manager": py_pm}
        for dep_name, (adapter, install) in _PYTHON_FRAMEWORK_ADAPTERS.items():
            if dep_name in py_names:
                result["matches"].append(
                    {"ecosystem": "python", "framework": dep_name, "adapter": adapter, "install": install}
                )

    if has_node:
        node_names = _node_dependency_names(project_dir)
        node_pm = _node_package_manager(project_dir)
        result["node"] = {"package_manager": node_pm}
        for dep_name, (adapter, install) in _NODE_FRAMEWORK_ADAPTERS.items():
            if dep_name in node_names:
                result["matches"].append(
                    {"ecosystem": "node", "framework": dep_name, "adapter": adapter, "install": install}
                )

    if not result["matches"]:
        fallback = []
        if has_python:
            fallback.append({"ecosystem": "python", "adapter": "inis", "install": "pip install inis"})
        if has_node:
            fallback.append({"ecosystem": "node", "adapter": "@inis-run/sdk", "install": "npm install @inis-run/sdk"})
        if not fallback:
            fallback.append(
                {
                    "ecosystem": "unknown",
                    "adapter": None,
                    "install": None,
                    "note": "no pyproject.toml/requirements*.txt/Pipfile/package.json found — "
                    "if the harness speaks MCP directly, point it at https://mcp.inis.run/sse "
                    "instead of installing a package.",
                }
            )
        result["fallback"] = fallback

    return result


def format_report(result: dict) -> str:
    lines = [f"project: {result['project_dir']}"]
    if "python" in result:
        lines.append(f"python detected — package manager: {result['python']['package_manager']}")
    if "node" in result:
        lines.append(f"node detected — package manager: {result['node']['package_manager']}")

    if result["matches"]:
        lines.append("native adapter(s) found:")
        for m in result["matches"]:
            lines.append(f"  - {m['framework']} ({m['ecosystem']}) -> {m['install']}")
    else:
        lines.append("no native adapter dependency found — falling back:")
        for f in result.get("fallback", []):
            if f.get("install"):
                lines.append(f"  - {f['ecosystem']}: {f['install']}")
            else:
                lines.append(f"  - {f.get('note')}")

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    project_dir = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    result = detect(project_dir)
    print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
