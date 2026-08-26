#!/usr/bin/env python
"""Hermes Model Lab state-isolation proof (T007).

Runs one real, bounded lab completion through the plugin's dashboard API
path and compares protected Hermes state before and after.

The verifier may read only declared protected state:
  - semantic session/message totals from the host SQLite store (read-only)
  - file hashes of config.yaml, memories/, skills/, cron/, sessions/
    metadata files, and the plugin project source tree
  - exact counts of one unique synthetic marker in message storage and
    *.log files

Credential/auth material (auth.json, .env, key/token/credential files) is
excluded from every manifest and is never opened. The script never writes.
It fails closed with exit code 1 on any change or any marker leak, and
prints a small JSON receipt containing only counts, booleans, hashes of
noncredential protected files, and changed path names.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

MARKER_PREFIX = "MODEL_LAB_T007_"
MARKER = f"{MARKER_PREFIX}ISOLATION_PROOF"

PROTECTED_FILES = ("config.yaml",)
PROTECTED_DIRS = ("memories", "skills", "cron")
SESSION_METADATA_DIR = "sessions"
LOG_DIR = "logs"
MESSAGE_DB = "state.db"
AMBIENT_WINDOW_SECONDS = 3.0

CREDENTIAL_BASENAMES = {"auth.json", ".env"}
CREDENTIAL_NAME_PATTERNS = (
    "*.pem",
    "*.key",
    "*token*",
    "*credential*",
    "*secret*",
    "*.env*",
)

PROJECT_EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".dfg",
    "docs",
    "scripts",
    "spikes",
}
PROJECT_EXCLUDED_NAMES = {
    ".env",
    ".env.example",
    "auth.json",
    ".hermes.md",
}

# Source files the shipped plugin depends on; the manifest covers these.
PROJECT_INCLUDED_GLOBS = ("*.py", "*.json", "*.yaml", "*.yml", "*.js", "*.mjs")


def _is_credential(path: Path) -> bool:
    name = path.name.lower()
    if name in CREDENTIAL_BASENAMES:
        return True
    import fnmatch

    return any(fnmatch.fnmatch(name, pattern) for pattern in CREDENTIAL_NAME_PATTERNS)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_protected_state(hermes_home: Path) -> dict[str, str]:
    """Hash config plus memory/skill/cron/session-metadata files."""
    home = Path(hermes_home)
    manifest: dict[str, str] = {}
    for relative in PROTECTED_FILES:
        target = home / relative
        if target.is_file() and not target.is_symlink() and not _is_credential(target):
            manifest[relative] = _hash_file(target)
    for dirname in (*PROTECTED_DIRS, SESSION_METADATA_DIR):
        base = home / dirname
        if not base.is_dir() or base.is_symlink():
            continue
        allowed_suffixes = {".json", ".md", ".yaml", ".yml", ".txt"}
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink() or _is_credential(path):
                continue
            if path.suffix.lower() not in allowed_suffixes:
                continue
            manifest[path.relative_to(home).as_posix()] = _hash_file(path)
    return manifest


def project_source_manifest(project_root: Path) -> dict[str, str]:
    """Deterministic hash manifest of the shipped project source tree."""
    root = Path(project_root)
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relparts = path.relative_to(root).parts
        if any(part in PROJECT_EXCLUDED_DIRS for part in relparts[:-1]):
            continue
        if path.name in PROJECT_EXCLUDED_NAMES or _is_credential(path):
            continue
        if not any(path.match(glob) for glob in PROJECT_INCLUDED_GLOBS):
            continue
        manifest[path.relative_to(root).as_posix()] = _hash_file(path)
    return manifest


def diff_manifests(
    before: dict[str, str], after: dict[str, str]
) -> list[str]:
    changed: list[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


def snapshot_session_counts(hermes_home: Path) -> dict[str, int]:
    """Semantic totals from the host store, opened read-only."""
    db = Path(hermes_home) / MESSAGE_DB
    counts = {"sessions": 0, "messages": 0}
    if not db.exists():
        return counts
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table in ("sessions", "messages"):
            if table in tables:
                counts[table] = int(
                    conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                )
    finally:
        conn.close()
    return counts


def count_marker_in_messages(hermes_home: Path, marker: str) -> int:
    db = Path(hermes_home) / MESSAGE_DB
    if not db.exists():
        return 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "messages" not in tables:
            return 0
        return int(
            conn.execute(
                "SELECT count(*) FROM messages WHERE content LIKE ?",
                (f"%{marker}%",),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def count_marker_in_logs(hermes_home: Path, marker: str) -> int:
    total = 0
    log_dir = Path(hermes_home) / LOG_DIR
    if not log_dir.is_dir():
        return 0
    for path in sorted(log_dir.rglob("*.log")):
        try:
            with path.open("rb") as handle:
                total += handle.read().count(marker.encode("utf-8"))
        except OSError:
            continue
    return total


def _load_llm():
    """Create the same host-owned plugin LLM facade the backend uses.

    Imported lazily so unit tests can substitute a fake without touching
    real Hermes state. This mirrors dashboard/plugin_api.py exactly.
    """
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    manifest = PluginManifest(name="hermes-model-lab", key="hermes-model-lab")
    return PluginContext(manifest, get_plugin_manager()).llm


def new_marker() -> str:
    """One collision-resistant synthetic marker per verifier run."""
    return f"{MARKER_PREFIX}{uuid.uuid4().hex.upper()}"


def run_verification(
    hermes_home: Path,
    project_root: Path,
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    marker: str | None = None,
) -> dict:
    """One bounded completion plus full before/after proof."""
    home = Path(hermes_home)
    marker = marker or new_marker()
    prompt_with_marker = f"{prompt}\n\nInclude this exact token in your reply: {marker}"

    receipt: dict = {"marker": marker}

    # Measure the ambient message-write rate on a quiet live host first, so
    # growth during the request can be attributed honestly. On a quiet host
    # this window sees zero writes; any message growth during the request
    # itself then fails closed.
    ambient_before = snapshot_session_counts(home)
    time.sleep(AMBIENT_WINDOW_SECONDS)
    ambient_after = snapshot_session_counts(home)
    receipt["ambient_messages_during_window"] = (
        ambient_after["messages"] - ambient_before["messages"]
    )

    files_before = snapshot_protected_state(home)
    source_before = project_source_manifest(project_root)
    counts_before = snapshot_session_counts(home)
    marker_msgs_before = count_marker_in_messages(home, marker)
    marker_logs_before = count_marker_in_logs(home, marker)

    llm = _load_llm()
    call_kwargs: dict = {"max_tokens": 64, "timeout": 60.0, "purpose": "model-lab-verify"}
    if provider and model:
        call_kwargs["provider"] = provider
        call_kwargs["model"] = model
    started = time.monotonic()
    result = None
    error = None

    async def _bounded_call() -> object:
        return await asyncio.wait_for(
            llm.acomplete(
                [{"role": "user", "content": prompt_with_marker}], **call_kwargs
            ),
            timeout=65.0,
        )

    try:
        result = asyncio.run(_bounded_call())
    except Exception as exc:  # noqa: BLE001 - receipt records class only
        error = type(exc).__name__
    elapsed_ms = int((time.monotonic() - started) * 1000)

    text = getattr(result, "text", "") or ""
    receipt["request_ok"] = error is None
    receipt["error_class"] = error
    receipt["marker_returned"] = marker in text
    receipt["elapsed_ms"] = elapsed_ms
    receipt["provider"] = getattr(result, "provider", None)
    receipt["model"] = getattr(result, "model", None)

    files_after = snapshot_protected_state(home)
    source_after = project_source_manifest(project_root)
    counts_after = snapshot_session_counts(home)
    marker_msgs_after = count_marker_in_messages(home, marker)
    marker_logs_after = count_marker_in_logs(home, marker)

    receipt["marker_in_messages_after"] = marker_msgs_after
    receipt["marker_in_logs_after"] = marker_logs_after
    receipt["changed_paths"] = sorted(
        set(diff_manifests(files_before, files_after))
        | set(diff_manifests(source_before, source_after))
    )
    counts_delta = {
        key: counts_after[key] - counts_before[key] for key in sorted(counts_after)
    }
    receipt["counts_delta"] = counts_delta
    receipt["counts_changed"] = [
        key for key, delta in counts_delta.items() if delta != 0
    ]

    ok = (
        receipt["request_ok"]
        and receipt["marker_returned"]
        and marker_msgs_before == 0
        and marker_logs_before == 0
        and marker_msgs_after == 0
        and marker_logs_after == 0
        and not receipt["changed_paths"]
        and not receipt["counts_changed"]
    )
    receipt["ok"] = ok
    receipt["exit_code"] = 0 if ok else 1
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args(argv)

    receipt = run_verification(
        hermes_home=Path(args.hermes_home),
        project_root=Path(args.project_root),
        prompt="Reply with a single short sentence confirming you received a test message.",
        provider=args.provider,
        model=args.model,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return receipt["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
