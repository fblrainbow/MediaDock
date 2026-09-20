"""MediaDock release layer (Stage-010).

Single source of truth for the delivered version, plus the static half of the
release check: required files, userscript version, changelog and
`config.example.json` parity.

`tests/release_check.py` adds the runtime half (real HTTP, restart, rollback
drill); this module stays stdlib-only and never starts a server.

Version contract (Stage-010.md 5.3):

    APP_VERSION        delivered application version
    API_VERSION        HTTP contract generation
    USERSCRIPT_VERSION `MediaDock.js` @version this release expects
    RELEASE_DATE       date of the changelog entry named APP_VERSION

Adding a new required document means editing `REQUIRED_DOCS`; the release
check fails until the file exists, which is the point.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_VERSION = "1.0.1"
API_VERSION = "1"
USERSCRIPT_VERSION = "5.2"
RELEASE_DATE = "2026-09-20"
RELEASE_NAME = "MVP 交付"

USERSCRIPT_FILE = "MediaDock.js"
CHANGELOG_FILE = "docs/release-notes.md"
CONFIG_EXAMPLE_FILE = "config.example.json"

# Files a release must ship; missing entries fail the release check.
REQUIRED_DOCS: Sequence[str] = (
    "README.md",
    "requirements.txt",
    "config.example.json",
    "MediaDock.js",
    "docs/install.md",
    "docs/configuration.md",
    "docs/userscript.md",
    "docs/release-notes.md",
    "docs/known-limitations.md",
    "docs/upgrade-rollback.md",
    "docs/release-checklist.md",
)

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_USERSCRIPT_VERSION_RE = re.compile(r"^//\s*@version\s+(\S+)\s*$", re.M)
# `## [1.0.0] - 2026-09-20` or `## 1.0.0` (brackets optional)
_CHANGELOG_RE = re.compile(r"^##\s*\[?(\d+\.\d+\.\d+)\]?", re.M)
_REQUIRED_CONFIG_KEYS = ("host", "port")


def is_valid_version(text: Any) -> bool:
    """True for `X.Y.Z` with numeric parts only."""
    return bool(VERSION_RE.match(str(text or "").strip()))


def version_tuple(text: str) -> tuple:
    """`"1.2.3"` -> `(1, 2, 3)`; invalid input -> `()` (never raises)."""
    if not is_valid_version(text):
        return ()
    return tuple(int(part) for part in str(text).split("."))


def read_text(path: str) -> str:
    """Best-effort UTF-8 read; missing/unreadable file reads as `""`."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return ""


def parse_userscript_version(source: str) -> str:
    """`@version` from a userscript header; `""` when absent."""
    match = _USERSCRIPT_VERSION_RE.search(source or "")
    return match.group(1) if match else ""


def userscript_version(path: Optional[str] = None) -> str:
    """Version declared by `MediaDock.js` (repo-relative by default)."""
    return parse_userscript_version(
        read_text(path or os.path.join(BASE_DIR, USERSCRIPT_FILE)))


def changelog_versions(text: str) -> List[str]:
    """All released versions in a changelog, in file order."""
    return _CHANGELOG_RE.findall(text or "")


def latest_changelog_version(text: str) -> str:
    """First `## x.y.z` heading, or `""` when the changelog is empty."""
    versions = changelog_versions(text)
    return versions[0] if versions else ""


def changelog_latest(path: Optional[str] = None) -> str:
    """Latest version from the changelog file."""
    return latest_changelog_version(
        read_text(path or os.path.join(BASE_DIR, CHANGELOG_FILE)))


def missing_docs(base_dir: Optional[str] = None,
                 docs: Sequence[str] = REQUIRED_DOCS) -> List[str]:
    """Required files that are absent (repo-relative paths, sorted)."""
    root = base_dir or BASE_DIR
    return sorted(name for name in docs
                  if not os.path.isfile(os.path.join(root, name)))


def config_key_report(example_path: str,
                      defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Compare `config.example.json` keys with `core_config.DEFAULTS` keys."""
    import json

    expected = set(defaults)
    try:
        with open(example_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return {"ok": False, "missing": sorted(expected), "extra": [],
                "reason": f"cannot read {os.path.basename(example_path)}: {exc}"}
    if not isinstance(data, dict):
        return {"ok": False, "missing": sorted(expected), "extra": [],
                "reason": f"{os.path.basename(example_path)} must be a JSON object"}
    actual = set(data)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    reason = ""
    if missing:
        reason = "missing key(s): " + ", ".join(missing)
    elif extra:
        reason = "unknown key(s): " + ", ".join(extra)
    return {"ok": not missing and not extra, "missing": missing, "extra": extra,
            "reason": reason}


def _load_defaults() -> Dict[str, Any]:
    """`core_config.DEFAULTS`; empty when the module cannot be imported."""
    try:
        from core_config import DEFAULTS
        return dict(DEFAULTS)
    except Exception:  # noqa: BLE001 - a broken config layer is reported, not raised
        return {}


def release_info(schema_version: Optional[int] = None) -> Dict[str, Any]:
    """`/health.version` payload (Stage-010.md 5.4)."""
    try:
        schema = int(schema_version or 0)
    except (TypeError, ValueError):
        schema = 0
    return {"app": APP_VERSION, "api": API_VERSION,
            "userscript": USERSCRIPT_VERSION, "schema": schema}


def static_checks(base_dir: Optional[str] = None,
                  defaults: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Static release checks in a stable order; each item is `{name, ok, detail}`."""
    root = base_dir or BASE_DIR
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    missing = missing_docs(root)
    add("required_files", not missing,
        "all present" if not missing else "missing: " + ", ".join(missing))

    add("app_version_shape", is_valid_version(APP_VERSION),
        f"APP_VERSION={APP_VERSION}")

    script_version = userscript_version(os.path.join(root, USERSCRIPT_FILE))
    add("userscript_version", script_version == USERSCRIPT_VERSION,
        f"file={script_version or '(none)'} expected={USERSCRIPT_VERSION}")

    latest = changelog_latest(os.path.join(root, CHANGELOG_FILE))
    add("changelog_version", latest == APP_VERSION,
        f"changelog={latest or '(none)'} APP_VERSION={APP_VERSION}")

    readme = read_text(os.path.join(root, "README.md"))
    add("readme_version", APP_VERSION in readme,
        "README.md states " + APP_VERSION if APP_VERSION in readme
        else "README.md does not mention " + APP_VERSION)

    report = config_key_report(os.path.join(root, CONFIG_EXAMPLE_FILE),
                               defaults if defaults is not None else _load_defaults())
    add("config_example_keys", report["ok"], report["reason"] or "keys match")

    return checks


def failed_names(checks: Sequence[Dict[str, Any]]) -> List[str]:
    """Names of failed checks, in order."""
    return [item["name"] for item in checks if not item.get("ok")]


def format_report(checks: Sequence[Dict[str, Any]]) -> str:
    """Human-readable `OK`/`FAIL` report for a check list."""
    lines = []
    for item in checks:
        mark = "OK  " if item.get("ok") else "FAIL"
        detail = item.get("detail") or ""
        lines.append(f"[{mark}] {item['name']}{' - ' + detail if detail else ''}")
    return "\n".join(lines)

