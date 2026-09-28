"""Contract tests for the repository's own GitHub Actions workflows.

The quality workflow is the only automated gate this repository has, so the
properties that make it trustworthy are pinned here rather than left to
review:

1. every `uses:` reference is an immutable commit SHA, never a floating tag;
2. the workflow token stays least-privilege;
3. the workflow runs this repository's real check commands, and the hermetic
   Python step names only tests that exist.
"""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

if not WORKFLOWS.is_dir():
    pytest.skip(
        "workflow files are repository-only and are not part of the release archive",
        allow_module_level=True,
    )

PINNED_USES = re.compile(r"^\s*(?:- )?uses:\s*(\S+)\s*(#.*)?$", re.MULTILINE)
SHA_PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w.-]+)*@[0-9a-f]{40}$")


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def test_both_baseline_workflows_exist():
    names = {path.name for path in _workflow_files()}
    assert names == {"ci.yml", "codeql.yml"}


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path):
    text = path.read_text(encoding="utf-8")
    uses = [match.group(1) for match in PINNED_USES.finditer(text)]
    assert uses, f"{path.name} should call at least one action"
    for reference in uses:
        assert SHA_PINNED.match(reference), f"{path.name} pins {reference} to a tag, not a SHA"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_token_stays_read_only(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    permissions = data["permissions"]
    assert permissions["contents"] == "read"
    # Only CodeQL is allowed to write, and only security events.
    extra = {key for key in permissions if key != "contents"}
    assert extra <= {"security-events"}, f"{path.name} grants {sorted(extra)}"
    for job in data["jobs"].values():
        job_permissions = job.get("permissions")
        if job_permissions:
            assert job_permissions["contents"] == "read"
            assert set(job_permissions) <= {"contents", "security-events"}


def test_ci_runs_the_repository_check_commands():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "ruff check __init__.py dashboard/plugin_api.py scripts tests" in text
    assert "pytest tests -q" in text
    assert "node --check desktop/plugin.js" in text
    assert "node --experimental-vm-modules tests/desktop_plugin_contract.mjs" in text
    assert "pip install --disable-pip-version-check -r requirements-dev.txt" in text


def test_ruff_rules_are_explicit_and_stable():
    """A Ruff upgrade must not silently replace the repository's lint policy."""
    path = ROOT / "ruff.toml"
    assert path.is_file()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["lint"]["select"] == ["E4", "E7", "E9", "F"]


def test_ci_pytest_targets_exist_on_disk():
    """Every --deselect nodeid must name a real test, not a stale one."""
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    # Shell line continuations mean a nodeid can carry a trailing backslash.
    nodeids = [
        match.rstrip("\\")
        for match in re.findall(r"--deselect (\S+::\S+)", text)
    ]
    assert nodeids, "the hermetic step should state which tests need a host"
    for nodeid in nodeids:
        rel, _, name = nodeid.partition("::")
        assert rel.endswith(".py"), nodeid
        module = ROOT / rel
        assert module.is_file(), f"{nodeid} names a missing file"
        assert f"def {name}(" in module.read_text(encoding="utf-8"), (
            f"{nodeid} names a missing test"
        )


def test_codeql_scans_the_shipped_languages():
    data = yaml.safe_load((WORKFLOWS / "codeql.yml").read_text(encoding="utf-8"))
    matrix = data["jobs"]["analyze"]["strategy"]["matrix"]["include"]
    languages = {entry["language"] for entry in matrix}
    assert languages == {"python", "javascript-typescript"}
    assert {entry["build-mode"] for entry in matrix} == {"none"}


def test_dependabot_groups_python_and_github_actions_weekly():
    path = ROOT / ".github" / "dependabot.yml"
    assert path.is_file()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert data["multi-ecosystem-groups"] == {
        "weekly-dependencies": {
            "schedule": {
                "interval": "weekly",
                "day": "monday",
                "time": "10:00",
                "timezone": "America/New_York",
            }
        }
    }
    updates = data["updates"]
    assert {entry["package-ecosystem"] for entry in updates} == {
        "github-actions",
        "pip",
    }
    assert all(entry["directory"] == "/" for entry in updates)
    assert all(entry["patterns"] == ["*"] for entry in updates)
    assert all(
        entry["multi-ecosystem-group"] == "weekly-dependencies" for entry in updates
    )
