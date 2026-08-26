"""T007 state-isolation proof tests.

These tests prove three things:

1. The reusable verifier (scripts/verify_isolation.py) detects any change
   to protected Hermes state, any new session/message row, and any leak of
   the unique synthetic marker into message storage or log files.
2. Credential and auth material is never part of the snapshot manifests
   and never opened by the verifier.
3. The shipped plugin surface stays powerless: request schemas carry no
   path/file/tool/code fields, and shipped source contains no direct
   credential readers or filesystem/tool/code-execution actions.
"""

import ast
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import verify_isolation as vi  # noqa: E402


# ---------------------------------------------------------------- helpers

def _make_hermes_home(tmp_path: Path) -> Path:
    """A tiny fake Hermes home with the protected-state shape."""
    home = tmp_path / "hermes-home"
    (home / "memories").mkdir(parents=True)
    (home / "skills").mkdir()
    (home / "cron").mkdir()
    (home / "logs").mkdir()
    (home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (home / "memories" / "USER.md").write_text("before\n", encoding="utf-8")
    (home / "skills" / "demo.md").write_text("demo\n", encoding="utf-8")
    (home / "cron" / "jobs.json").write_text("[]\n", encoding="utf-8")
    (home / "auth.json").write_text('{"token": "host-secret"}', encoding="utf-8")
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    return home


# ------------------------------------------------- detection behaviors


def test_generated_marker_is_unique_per_run():
    first = vi.new_marker()
    second = vi.new_marker()

    assert first.startswith("MODEL_LAB_T007_")
    assert second.startswith("MODEL_LAB_T007_")
    assert first != second


def test_verifier_detects_changed_protected_file(tmp_path):
    home = _make_hermes_home(tmp_path)
    before = vi.snapshot_protected_state(home)
    (home / "config.yaml").write_text("model: changed\n", encoding="utf-8")
    after = vi.snapshot_protected_state(home)

    diffs = vi.diff_manifests(before, after)
    assert "config.yaml" in diffs


def test_verifier_detects_created_session_and_message(tmp_path):
    home = _make_hermes_home(tmp_path)
    before = vi.snapshot_session_counts(home)
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute("INSERT INTO sessions (id) VALUES ('leak-1')")
        conn.execute(
            "INSERT INTO messages (content) VALUES ('MODEL_LAB_LEAK_X')"
        )
        conn.commit()
        after = vi.snapshot_session_counts(home)
    finally:
        conn.close()

    assert before != after
    assert after["sessions"] == before["sessions"] + 1
    assert after["messages"] == before["messages"] + 1


def test_marker_scan_finds_leak_in_messages_and_logs(tmp_path):
    home = _make_hermes_home(tmp_path)
    marker = "MODEL_LAB_T007_DEADBEEF"

    assert vi.count_marker_in_messages(home, marker) == 0
    assert vi.count_marker_in_logs(home, marker) == 0

    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(
            "INSERT INTO messages (content) VALUES (?)", (f"saw {marker}",)
        )
        conn.commit()
    finally:
        conn.close()
    (home / "logs" / "agent.log").write_text(
        f"request used {marker}\n", encoding="utf-8"
    )

    assert vi.count_marker_in_messages(home, marker) == 1
    assert vi.count_marker_in_logs(home, marker) == 1


def test_project_source_manifest_excludes_git_caches_credentials(tmp_path):
    project = tmp_path / "proj"
    (project / "dashboard").mkdir(parents=True)
    (project / ".git").mkdir()
    (project / "__pycache__").mkdir()
    (project / "dashboard" / "plugin_api.py").write_text("x = 1\n")
    (project / ".env").write_text("SECRET=1", encoding="utf-8")
    (project / "auth.json").write_text("{}", encoding="utf-8")
    (project / ".git" / "HEAD").write_text("ref: x", encoding="utf-8")

    manifest = vi.project_source_manifest(project)

    assert "dashboard/plugin_api.py" in manifest
    assert not any(key.startswith(".git") for key in manifest)
    assert not any("__pycache__" in key for key in manifest)
    assert ".env" not in manifest
    assert "auth.json" not in manifest


def test_verifier_never_opens_credential_files(tmp_path, monkeypatch):
    home = _make_hermes_home(tmp_path)
    opened: list[str] = []
    real_open = Path.open

    def spy(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    vi.snapshot_protected_state(home)
    vi.project_source_manifest(ROOT)

    assert any("config.yaml" in path for path in opened), "open spy must be active"
    assert not any("auth.json" in path for path in opened)


def test_run_lab_request_reports_marker_and_clean_receipt(tmp_path, monkeypatch):
    """End-to-end verifier core against a fake LLM: the marker comes back,
    no protected state moves, and the receipt is clean."""
    home = _make_hermes_home(tmp_path)
    marker = "MODEL_LAB_T007_CAFEBABE"

    class FakeResult:
        text = f"{marker}"
        provider = "test-provider"
        model = "test-model"

        class usage:
            input_tokens = 1
            output_tokens = 1
            total_tokens = 2
            cache_read_tokens = 0
            cache_write_tokens = 0
            cost_usd = None

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            self.messages = messages
            return FakeResult()

    fake_llm = FakeLlm()
    monkeypatch.setattr(vi, "_load_llm", lambda: fake_llm)

    receipt = vi.run_verification(
        hermes_home=home,
        project_root=ROOT,
        prompt=f"Reply with exactly {marker}",
        marker=marker,
    )

    assert receipt["ok"] is True
    assert receipt["marker_returned"] is True
    assert receipt["marker_in_messages_after"] == 0
    assert receipt["marker_in_logs_after"] == 0
    assert receipt["changed_paths"] == []
    assert receipt["counts_changed"] == []


def test_run_lab_request_fails_closed_on_any_change(tmp_path, monkeypatch):
    home = _make_hermes_home(tmp_path)
    marker = "MODEL_LAB_T007_FAILCLOSE"

    class FakeResult:
        text = marker
        provider = "p"
        model = "m"

        class usage:
            input_tokens = 0
            output_tokens = 0
            total_tokens = 0
            cache_read_tokens = 0
            cache_write_tokens = 0
            cost_usd = None

    class LeakingLlm:
        async def acomplete(self, messages, **kwargs):
            # Simulate the host persisting the prompt into message storage.
            conn = sqlite3.connect(home / "state.db")
            try:
                conn.execute(
                    "INSERT INTO messages (content) VALUES (?)",
                    (f"leak {marker}",),
                )
                conn.commit()
            finally:
                conn.close()
            return FakeResult()

    monkeypatch.setattr(vi, "_load_llm", lambda: LeakingLlm())

    receipt = vi.run_verification(
        hermes_home=home,
        project_root=ROOT,
        prompt=f"Reply with exactly {marker}",
        marker=marker,
    )

    assert receipt["ok"] is False
    assert receipt["marker_in_messages_after"] == 1
    assert receipt["counts_changed"] == ["messages"]
    assert receipt["exit_code"] != 0


def test_run_lab_request_fails_closed_on_deleted_session_and_message(
    tmp_path, monkeypatch
):
    home = _make_hermes_home(tmp_path)
    marker = "MODEL_LAB_T007_DELETE"
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute("INSERT INTO sessions (id) VALUES ('existing')")
        conn.execute("INSERT INTO messages (content) VALUES ('ordinary message')")
        conn.commit()
    finally:
        conn.close()

    class FakeResult:
        text = marker
        provider = "p"
        model = "m"

    class DeletingLlm:
        async def acomplete(self, messages, **kwargs):
            conn = sqlite3.connect(home / "state.db")
            try:
                conn.execute("DELETE FROM messages")
                conn.execute("DELETE FROM sessions")
                conn.commit()
            finally:
                conn.close()
            return FakeResult()

    monkeypatch.setattr(vi, "_load_llm", lambda: DeletingLlm())

    receipt = vi.run_verification(
        hermes_home=home,
        project_root=ROOT,
        prompt=f"Reply with exactly {marker}",
        marker=marker,
    )

    assert receipt["ok"] is False
    assert receipt["counts_delta"] == {"messages": -1, "sessions": -1}
    assert receipt["counts_changed"] == ["messages", "sessions"]
    assert receipt["exit_code"] != 0


def test_run_lab_request_fails_when_marker_not_returned(tmp_path, monkeypatch):
    home = _make_hermes_home(tmp_path)

    class FakeResult:
        text = "something else entirely"
        provider = "p"
        model = "m"

        class usage:
            input_tokens = 1
            output_tokens = 1
            total_tokens = 2
            cache_read_tokens = 0
            cache_write_tokens = 0
            cost_usd = None

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            return FakeResult()

    monkeypatch.setattr(vi, "_load_llm", lambda: FakeLlm())

    receipt = vi.run_verification(
        hermes_home=home, project_root=ROOT, prompt="whatever MARKER_MISSING"
    )

    assert receipt["ok"] is False
    assert receipt["marker_returned"] is False


# ------------------------------------------------- static contracts

_PY_SOURCES = [
    ROOT / "__init__.py",
    ROOT / "dashboard" / "plugin_api.py",
]
_JS_SOURCES = [ROOT / "desktop" / "plugin.js"]


def test_completion_request_schema_has_no_dangerous_fields():
    sys.path.insert(0, str(ROOT / "dashboard"))
    spec_lines = (ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    match = re.search(
        r"class CompletionRequest\(BaseModel\):(.*?)\n(?:class |def |@|\Z)",
        spec_lines,
        re.S,
    )
    assert match, "CompletionRequest must exist"
    body = match.group(1)
    field_names = set(re.findall(r"^\s{4}(\w+)\s*:", body, re.M))
    dangerous = {
        name
        for name in field_names
        if re.search(r"path|file|tool|code|exec|shell|cmd|command", name)
    }
    assert field_names == {"prompt", "provider", "model"}
    assert not dangerous


_FORBIDDEN_CALLS = {"open", "exec", "eval", "__import__"}
_FORBIDDEN_MODULES = {"subprocess", "shutil", "socket", "ctypes", "pathlib"}


def test_python_backend_has_no_filesystem_or_exec_actions():
    for source in _PY_SOURCES:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in _FORBIDDEN_CALLS, (
                    f"{source.name}: forbidden call {node.func.id}"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in _FORBIDDEN_MODULES, (
                        f"{source.name}: forbidden import {alias.name}"
                    )
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in _FORBIDDEN_MODULES, (
                    f"{source.name}: forbidden import {node.module}"
                )
            if isinstance(node, ast.Attribute):
                lowered = node.attr.lower()
                assert not re.search(
                    r"apikey|api_key|credential|authtoken|password|secret",
                    lowered,
                ), f"{source.name}: suspicious attribute {node.attr}"


def test_desktop_source_has_no_fs_env_or_child_process_access():
    for source in _JS_SOURCES:
        text = source.read_text(encoding="utf-8")
        for pattern in (
            r"\brequire\s*\(\s*['\"](fs|child_process|os|path)",
            r"\bfrom\s+['\"](fs|child_process|node:fs)",
            r"\bprocess\.env\b",
            r"\beval\s*\(",
            r"\bFunction\s*\(",
            r"apiKey|api_key|credential|password",
        ):
            assert not re.search(pattern, text), (
                f"{source.name}: forbidden pattern {pattern}"
            )


def test_verifier_module_itself_stays_read_only():
    """The verifier must never write inside the Hermes home or project."""
    source = (SCRIPTS / "verify_isolation.py").read_text(encoding="utf-8")
    for pattern in (
        r"\.write_text\(",
        r"\.write_bytes\(",
        r"\.unlink\(",
        r"\.mkdir\(",
        r"\bopen\s*\(\s*[^)]*['\"][wax]",
        r"os\.remove|os\.rename|shutil",
    ):
        assert not re.search(pattern, source), f"verifier writes state: {pattern}"
