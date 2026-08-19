"""Tests for scripts/validate_skill.py — the standard skill validator
(frontmatter/name/description checks, linked-resource checks,
mirror-hygiene checks, cited-error-code checks).

Two kinds of coverage:

1. The real `inis-run/` skill package as committed must validate clean
   (`test_real_skill_validates_clean`) — this is the actual release gate.
2. Each check is separately exercised against small, synthetic fixture
   skills built in `tmp_path`, so a broken check fails here even if the
   real skill happens not to trip it (and so a future regression in the
   real skill is caught by a specific, readable failure rather than one
   opaque "validation FAILED" dump).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_DIR / "scripts"))

import validate_skill  # noqa: E402

REAL_SKILL_DIR = PKG_DIR / "inis-run"
REPO_ROOT = PKG_DIR.parent.parent  # .../sdk/agent-skill -> repo root
ERRORS_MDX = REPO_ROOT / "docs-site" / "content" / "docs" / "api" / "errors.mdx"

MINIMAL_FRONTMATTER = (
    "---\n"
    "name: {name}\n"
    "description: {description}\n"
    "---\n"
)


def _write_minimal_skill(tmp_path: Path, *, name: str = "test-skill",
                          description: str = "Use this when testing inis.run skill validation logic in isolation.",
                          extra_frontmatter: str = "", body: str = "Do the thing.\n") -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    fm = MINIMAL_FRONTMATTER.format(name=name, description=description)
    if extra_frontmatter:
        fm = fm.replace("---\n", "---\n" + extra_frontmatter, 1)
    (skill_dir / "SKILL.md").write_text(fm + body, encoding="utf-8")
    return skill_dir


def test_real_skill_validates_clean() -> None:
    errors = validate_skill.validate(REAL_SKILL_DIR)
    assert errors == [], "\n".join(errors)


def test_real_skill_name_matches_directory() -> None:
    text = (REAL_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    frontmatter, _ = validate_skill.parse_frontmatter(text)
    assert frontmatter["name"] == "inis-run" == REAL_SKILL_DIR.name


def test_real_skill_body_under_500_lines() -> None:
    text = (REAL_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    _, body = validate_skill.parse_frontmatter(text)
    assert body.count("\n") < 500


def test_frontmatter_rejects_extra_keys(tmp_path: Path) -> None:
    skill_dir = _write_minimal_skill(tmp_path, extra_frontmatter="version: 1.0.0\n")
    errors = validate_skill.validate(skill_dir)
    assert any("extra keys" in e for e in errors), errors


def test_frontmatter_name_must_match_directory(tmp_path: Path) -> None:
    skill_dir = tmp_path / "actual-dir-name"
    skill_dir.mkdir()
    fm = MINIMAL_FRONTMATTER.format(
        name="different-name",
        description="Use this when testing inis.run skill validation logic in isolation.",
    )
    (skill_dir / "SKILL.md").write_text(fm + "Body.\n", encoding="utf-8")
    errors = validate_skill.validate(skill_dir)
    assert any("does not match its directory name" in e for e in errors), errors


def test_frontmatter_name_rejects_bad_slug(tmp_path: Path) -> None:
    skill_dir = tmp_path / "Bad_Name"
    skill_dir.mkdir()
    fm = MINIMAL_FRONTMATTER.format(
        name="Bad_Name",
        description="Use this when testing inis.run skill validation logic in isolation.",
    )
    (skill_dir / "SKILL.md").write_text(fm + "Body.\n", encoding="utf-8")
    errors = validate_skill.validate(skill_dir)
    assert any("lowercase alphanumeric" in e for e in errors), errors


def test_description_too_short_is_flagged(tmp_path: Path) -> None:
    skill_dir = _write_minimal_skill(tmp_path, description="too short")
    errors = validate_skill.validate(skill_dir)
    assert any("description is" in e for e in errors), errors


def test_description_must_name_inis_run(tmp_path: Path) -> None:
    skill_dir = _write_minimal_skill(
        tmp_path,
        description="Use this whenever the user asks about a generic sandbox integration.",
    )
    errors = validate_skill.validate(skill_dir)
    assert any("should name inis.run explicitly" in e for e in errors), errors


def test_missing_linked_resource_is_flagged(tmp_path: Path) -> None:
    skill_dir = _write_minimal_skill(
        tmp_path, body="See `reference/does-not-exist.md` for detail.\n"
    )
    errors = validate_skill.validate(skill_dir)
    assert any("does not exist" in e for e in errors), errors


def test_existing_linked_resource_passes(tmp_path: Path) -> None:
    skill_dir = _write_minimal_skill(
        tmp_path, body="See `reference/detail.md` for detail.\n"
    )
    (skill_dir / "reference").mkdir()
    (skill_dir / "reference" / "detail.md").write_text("detail\n", encoding="utf-8")
    errors = validate_skill.validate(skill_dir)
    assert not any("does not exist" in e for e in errors), errors


# These are the exact banned words, used to prove the detector catches
# them -- not a leak of anything. mirror-guard:allow-line
@pytest.mark.parametrize("term", ["Scaleway", "ClickHouse", "the Firecracker VM", "/etc/inis/inisd.env"])  # mirror-guard:allow-line
def test_banned_infra_terms_are_flagged(tmp_path: Path, term: str) -> None:
    skill_dir = _write_minimal_skill(tmp_path, body=f"Internal detail: {term}.\n")
    errors = validate_skill.validate(skill_dir)
    assert any("infrastructure jargon" in e for e in errors), errors


def test_banned_issue_reference_is_flagged(tmp_path: Path) -> None:
    # Fake issue number, chosen only to match the \d{2,6} pattern -- what's
    # under test is the detector, not any specific real issue.
    # mirror-guard:allow-line
    skill_dir = _write_minimal_skill(tmp_path, body="See #9999 for background.\n")  # mirror-guard:allow-line
    errors = validate_skill.validate(skill_dir)
    assert any("internal issue reference" in e for e in errors), errors


def test_banned_process_language_is_flagged(tmp_path: Path) -> None:
    skill_dir = _write_minimal_skill(tmp_path, body="This is pending founder go-ahead.\n")
    errors = validate_skill.validate(skill_dir)
    assert any("internal-process language" in e for e in errors), errors


def test_fabricated_error_code_is_flagged(tmp_path: Path) -> None:
    skill_dir = _write_minimal_skill(
        tmp_path,
        body="A 429 can also surface as `account_concurrency_exceeded`.\n",
    )
    errors = validate_skill.validate(skill_dir)
    assert any("fabricated error code" in e for e in errors), errors


def test_unrecognized_error_code_in_table_is_flagged(tmp_path: Path) -> None:
    skill_dir = _write_minimal_skill(
        tmp_path,
        body="| Code | HTTP |\n|---|---|\n| `totally_made_up_code` | 429 |\n",
    )
    errors = validate_skill.validate(skill_dir)
    assert any("not in the known taxonomy" in e for e in errors), errors


def test_real_error_code_in_table_passes(tmp_path: Path) -> None:
    skill_dir = _write_minimal_skill(
        tmp_path,
        body="| Code | HTTP |\n|---|---|\n| `session_not_live` | 409 |\n",
    )
    errors = validate_skill.validate(skill_dir)
    assert not any("not in the known taxonomy" in e for e in errors), errors


def test_known_error_codes_match_generated_table() -> None:
    """Cross-check validate_skill.KNOWN_ERROR_CODES against the real,
    generated errors table. Only runs inside the monorepo checkout --
    docs-site/ is not part of the published sdk/agent-skill mirror, so this
    guard can only exist here, not in the shipped package itself."""
    if not ERRORS_MDX.is_file():
        pytest.skip("docs-site/content/docs/api/errors.mdx not present -- "
                    "not running inside the monorepo checkout")

    text = ERRORS_MDX.read_text(encoding="utf-8")
    row_re = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*((?:`[a-z_]+`,?\s*)*)\s*\|", re.MULTILINE)
    generated_codes: set[str] = set()
    for m in row_re.finditer(text):
        generated_codes.add(m.group(1))
        generated_codes.update(re.findall(r"`([a-z_]+)`", m.group(2)))

    assert generated_codes, "did not find any codes in errors.mdx -- table format changed?"
    assert generated_codes == validate_skill.KNOWN_ERROR_CODES, (
        "validate_skill.KNOWN_ERROR_CODES has drifted from the generated errors "
        f"table.\nIn errors.mdx but not in the snapshot: {generated_codes - validate_skill.KNOWN_ERROR_CODES}\n"
        f"In the snapshot but not errors.mdx: {validate_skill.KNOWN_ERROR_CODES - generated_codes}"
    )
