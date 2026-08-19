#!/usr/bin/env python3
"""Standard validator for the `inis-run` coding-agent skill package
(`sdk/agent-skill/inis-run/`).

Checks, in order:

  1. `SKILL.md` exists and its YAML frontmatter has exactly the keys
     `name` and `description` — a standard SKILL.md carries only those
     two fields.
  2. `name` is a lowercase-hyphen slug and matches the containing
     directory name (so directory-keyed skill loaders find it).
  3. `description` is non-empty, mentions "inis.run", and is short enough
     to be a loader-matched trigger string rather than a restated body.
  4. The imperative body (everything after the frontmatter) stays under
     500 lines — the body should stay lean; move detail to reference/*.md.
  5. Every file path this skill's own Markdown references in backticks or
     as a relative link actually exists under the skill directory.
  6. No internal-only content: issue numbers, infra hostnames/jargon, or
     internal-process language ("pending founder", "waiting on
     approval", ...) — this is public integration guidance and must read
     that way; a prior public adapter README shipped process language like
     this and had to be cleaned up by hand after the fact.
  7. Every error `code` cited in a Markdown table row matches the real,
     current error taxonomy (`KNOWN_ERROR_CODES` below) — this is a direct
     guard against inventing a plausible-sounding code that exists nowhere
     in the API (a mistake that has shipped in this product's public
     material before). `tests/test_skill_structure.py` additionally
     cross-checks `KNOWN_ERROR_CODES` itself against
     `docs-site/content/docs/api/errors.mdx` when run inside the
     monorepo checkout, so this snapshot can't silently drift from the
     generated table either.

Usage:
    python3 validate_skill.py [path/to/inis-run]   # defaults to the
                                                     # sibling inis-run/
                                                     # directory

Exit 0 and prints "OK" with no findings; exit 1 and prints every finding
otherwise (not just the first).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SKILL_DIR = SCRIPT_DIR.parent / "inis-run"

MAX_BODY_LINES = 500
MAX_DESCRIPTION_LEN = 1024
MIN_DESCRIPTION_LEN = 40
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Snapshot of docs-site/content/docs/api/errors.mdx's generated table
# (primary code + "also seen as" aliases). Keep this in sync by hand when
# that table changes -- the monorepo-only test in
# tests/test_skill_structure.py cross-checks it against the real generated
# file and fails loudly on drift; this script alone (as shipped in the
# public mirror, where errors.mdx does not exist) cannot do that live
# cross-check, so it is the thing that must stay accurate.
KNOWN_ERROR_CODES = {
    "validation", "bad_request",
    "payload_too_large",
    "unauthenticated", "unauthorized",
    "payment_required",
    "forbidden",
    "not_found",
    "method_not_allowed",
    "conflict",
    "org_has_other_members",
    "idempotency_key_in_progress",
    "session_not_live",
    "session_ended",
    "session_node_lost",
    "rate_limited", "concurrency_limit_exceeded", "rate_limit_exceeded",
    "account_concurrency_exceeded", "key_concurrency_exceeded",
    "bad_gateway",
    "session_unavailable",
    "session_node_unavailable",
    "template_version_unavailable",
    "archive_unavailable",
    "size_unavailable",
    "unavailable", "service_unavailable",
    "internal", "internal_error",
    "error",
}

# Case-insensitive. Anything matching these has no business in a public
# package -- see scripts/ci/guard-sdk-mirror-leaks.sh, which this mirrors
# for the same reason at author-time instead of only at CI/publish-time.
# mirror-guard:allow-line -- this IS that guard's own denylist definition,
# not a leak: it has to name the banned words to ban them.
BANNED_INFRA_TERMS = re.compile(
    r"\b(firecracker|kvm|microvm|jailer|vsock|ansible|scaleway|clickhouse|"  # mirror-guard:allow-line
    r"signoz|ubicloud|control-1|control-2|exec-eu|ams1)\b|/etc/inis\b",  # mirror-guard:allow-line
    re.IGNORECASE,
)
BANNED_ISSUE_REF = re.compile(r"(^|[^#\w])#\d{2,6}([^\d]|$)")
BANNED_PROCESS_PHRASES = [
    "pending founder",
    "founder go-ahead",
    "waiting on approval",
    "claimed by",
    "🚧",
]
# A specific, plausible-sounding error code that has never existed in this
# API but has shipped in this product's public material before -- never
# allowed to reappear, even inside a "don't do this" example, without
# triggering review.
BANNED_FABRICATED_CODES = ["account_concurrency_exceeded"]

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
BACKTICK_PATH_RE = re.compile(r"`([A-Za-z0-9_./-]+\.[a-zA-Z0-9]+)`")
TABLE_CODE_RE = re.compile(r"^\|\s*`([a-z][a-z_]*)`")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with a `---` YAML frontmatter fence")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("SKILL.md frontmatter is not terminated with a closing `---`")
    fm_text = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    frontmatter = yaml.safe_load(fm_text) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError("SKILL.md frontmatter did not parse to a mapping")
    return frontmatter, body


def _iter_markdown_files(skill_dir: Path):
    yield skill_dir / "SKILL.md"
    yield from sorted((skill_dir / "reference").glob("*.md"))


def check_frontmatter(skill_dir: Path, errors: list[str]) -> tuple[dict, str] | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        errors.append(f"missing {skill_md}")
        return None

    text = skill_md.read_text(encoding="utf-8")
    try:
        frontmatter, body = parse_frontmatter(text)
    except ValueError as e:
        errors.append(f"SKILL.md frontmatter: {e}")
        return None

    extra_keys = set(frontmatter) - {"name", "description"}
    missing_keys = {"name", "description"} - set(frontmatter)
    if extra_keys:
        errors.append(
            f"SKILL.md frontmatter has extra keys {sorted(extra_keys)} -- only "
            "`name` and `description` are allowed"
        )
    if missing_keys:
        errors.append(f"SKILL.md frontmatter is missing required keys {sorted(missing_keys)}")

    name = frontmatter.get("name")
    if isinstance(name, str):
        if not NAME_RE.match(name):
            errors.append(
                f"SKILL.md name {name!r} must be lowercase alphanumeric segments "
                "joined by single hyphens (^[a-z0-9]+(-[a-z0-9]+)*$)"
            )
        if name != skill_dir.name:
            errors.append(
                f"SKILL.md name {name!r} does not match its directory name "
                f"{skill_dir.name!r} -- directory-keyed skill loaders resolve by name"
            )
    elif "name" in frontmatter:
        errors.append("SKILL.md name must be a string")

    description = frontmatter.get("description")
    if isinstance(description, str):
        if not (MIN_DESCRIPTION_LEN <= len(description) <= MAX_DESCRIPTION_LEN):
            errors.append(
                f"SKILL.md description is {len(description)} chars -- expected "
                f"{MIN_DESCRIPTION_LEN}-{MAX_DESCRIPTION_LEN} (concrete enough to "
                "match on, short enough to stay a trigger string)"
            )
        if "inis.run" not in description:
            errors.append("SKILL.md description should name inis.run explicitly")
    elif "description" in frontmatter:
        errors.append("SKILL.md description must be a string")

    body_lines = body.count("\n") + (1 if body and not body.endswith("\n") else 0)
    if body_lines > MAX_BODY_LINES:
        errors.append(
            f"SKILL.md imperative body is {body_lines} lines -- keep it under "
            f"{MAX_BODY_LINES}, move reference material to reference/*.md instead"
        )

    return frontmatter, body


def check_linked_resources(skill_dir: Path, errors: list[str]) -> None:
    for md_file in _iter_markdown_files(skill_dir):
        if not md_file.is_file():
            continue
        text = md_file.read_text(encoding="utf-8")
        candidates: set[str] = set()
        for m in LINK_RE.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            candidates.add(target)
        for m in BACKTICK_PATH_RE.finditer(text):
            target = m.group(1)
            if "://" in target:
                continue
            if not (target.startswith("reference/") or target.startswith("scripts/")
                    or target in ("SKILL.md",)):
                continue
            candidates.add(target)

        for target in candidates:
            target = target.split("#", 1)[0]
            resolved = (skill_dir / target).resolve()
            if not str(resolved).startswith(str(skill_dir.resolve())):
                errors.append(f"{md_file}: linked path {target!r} escapes the skill directory")
                continue
            if not resolved.is_file():
                errors.append(f"{md_file}: linked path {target!r} does not exist")


def check_no_internal_content(skill_dir: Path, errors: list[str]) -> None:
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if BANNED_ISSUE_REF.search(text):
            errors.append(f"{path}: looks like it contains an internal issue reference (#NNNN)")
        if BANNED_INFRA_TERMS.search(text):
            errors.append(f"{path}: contains banned internal infrastructure jargon")
        lowered = text.lower()
        for phrase in BANNED_PROCESS_PHRASES:
            if phrase.lower() in lowered:
                errors.append(f"{path}: contains internal-process language ({phrase!r})")
        for code in BANNED_FABRICATED_CODES:
            if code in text:
                errors.append(f"{path}: cites the known-fabricated error code {code!r}")


def check_error_codes(skill_dir: Path, errors: list[str]) -> None:
    for md_file in _iter_markdown_files(skill_dir):
        if not md_file.is_file():
            continue
        for line in md_file.read_text(encoding="utf-8").splitlines():
            m = TABLE_CODE_RE.match(line.strip())
            if not m:
                continue
            code = m.group(1)
            if code not in KNOWN_ERROR_CODES:
                errors.append(
                    f"{md_file}: cites error code `{code}` which is not in the known "
                    "taxonomy (KNOWN_ERROR_CODES in this script) -- verify it against "
                    "docs-site/content/docs/api/errors.mdx / api/openapi.yaml before "
                    "citing it"
                )


def validate(skill_dir: Path) -> list[str]:
    errors: list[str] = []
    if not skill_dir.is_dir():
        return [f"{skill_dir} is not a directory"]
    check_frontmatter(skill_dir, errors)
    check_linked_resources(skill_dir, errors)
    check_no_internal_content(skill_dir, errors)
    check_error_codes(skill_dir, errors)
    return errors


def main(argv: list[str]) -> int:
    skill_dir = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_SKILL_DIR
    errors = validate(skill_dir)
    if errors:
        print(f"skill validation FAILED ({len(errors)} finding(s)):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"skill validation OK: {skill_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
