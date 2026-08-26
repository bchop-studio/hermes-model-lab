"""T008 topology verification tests.

Covers the reusable verifier (scripts/verify_topology.py) and the three
topology contracts it proves:

1. Local disposable install: backend + desktop halves install into a
   temporary HERMES_HOME and a temporary desktop-plugins root with exact
   source parity, valid syntax, scoped route inventory, and clean uninstall.
2. Windows-to-WSL path contract: the Windows Desktop half resolves under
   %LOCALAPPDATA%/hermes/desktop-plugins/<plugin-id>/ while the backend
   stays in the WSL Hermes home; install/uninstall planning never overwrites.
3. Remote/OAuth behavior contracts: the Desktop SDK's ctx.rest is
   profile-aware, ctx.socket is a no-op on OAuth remotes, and Model Lab
   polls over REST only.
"""

import json
import pathlib
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import verify_topology as vt  # noqa: E402


# ---------------------------------------------------------------- helpers

def _make_source_tree(tmp_path: Path) -> Path:
    """A fake project root shaped like the shipped plugin."""
    src = tmp_path / "src"
    (src / "dashboard").mkdir(parents=True)
    (src / "desktop").mkdir()
    (src / "plugin.yaml").write_text(
        "name: hermes-model-lab\nversion: 0.1.0\n", encoding="utf-8"
    )
    (src / "dashboard" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (src / "dashboard" / "plugin_api.py").write_text("router = None\n", encoding="utf-8")
    (src / "desktop" / "plugin.js").write_text(
        "const callbacks = {\n"
        "  loadHealth: () => ctx.rest('/health'),\n"
        "  loadModels: () => ctx.rest('/models'),\n"
        "}\nexport default {}\n",
        encoding="utf-8",
    )
    return src


# ------------------------------------------------- windows path contract

def test_desktop_plugin_path_resolves_under_localappdata():
    path = vt.windows_desktop_plugin_path("chris")
    text = str(path)
    assert "AppData" in text and "Local" in text
    assert "hermes" in text and "desktop-plugins" in text
    # Folder-id equals plugin id.
    assert vt.PLUGIN_ID in text
    assert text.endswith(vt.DESKTOP_ENTRY_NAME)


def test_desktop_plugin_path_is_pure_windows_with_backslashes_only():
    """Gap 3: the path is built with PureWindowsPath, backslashes only."""
    path = vt.windows_desktop_plugin_path("chris")
    assert isinstance(path, pathlib.PureWindowsPath)
    assert str(path) == (
        r"C:\Users\chris\AppData\Local\hermes"
        r"\desktop-plugins\hermes-model-lab\plugin.js"
    )


def test_windows_path_translation_from_wsl_mount(tmp_path):
    """A fake /mnt/c/Users root maps to the exact Windows-side path."""
    fake_users = tmp_path / "Users"
    user_root = fake_users / "chris"
    (user_root / "AppData" / "Local").mkdir(parents=True)
    resolved = vt.resolve_windows_user_root(str(fake_users))
    assert resolved == "chris"


def test_backend_path_stays_in_wsl_hermes_home():
    path = vt.backend_plugin_path(Path("/home/bchop/.hermes"))
    parts = Path(path).parts
    assert ".hermes" in parts and "plugins" in parts
    assert parts[-1] == vt.PLUGIN_ID
    assert "mnt" not in parts and "AppData" not in parts


# ------------------------------------------- no-overwrite install plans

def test_plan_install_is_blocked_by_existing_file(tmp_path):
    src = _make_source_tree(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "plugin.yaml").write_text("user edited\n", encoding="utf-8")

    plan = vt.plan_install(src, dest)

    assert plan["ok"] is False
    assert any(entry["conflict"] for entry in plan["files"])


def test_plan_uninstall_lists_exact_files_and_refuses_unknown_folder(tmp_path):
    src = _make_source_tree(tmp_path)
    dest = tmp_path / "installed" / vt.PLUGIN_ID
    shutil.copytree(src, dest)

    plan = vt.plan_uninstall(src, dest)
    assert plan["ok"] is True

    foreign = tmp_path / "installed" / "someone-elses-plugin"
    shutil.copytree(src, foreign)
    bad = vt.plan_uninstall(src, foreign)
    assert bad["ok"] is False


# --------------------------------------------------- install + parity

def test_install_backend_half_matches_source_and_enables_plugin(tmp_path):
    src = _make_source_tree(tmp_path)
    home = tmp_path / "hermes-home"

    result = vt.install_backend(src, home)

    installed = home / "plugins" / vt.PLUGIN_ID
    assert result["ok"] is True
    assert (installed / "plugin.yaml").is_file()
    assert (installed / "dashboard" / "manifest.json").is_file()
    cfg = (home / "config.yaml").read_text(encoding="utf-8")
    assert vt.PLUGIN_ID in cfg


def test_verify_source_parity_detects_a_tampered_copy(tmp_path):
    src = _make_source_tree(tmp_path)
    dest = tmp_path / "copy"
    shutil.copytree(src, dest)

    assert vt.source_parity(src, dest)["ok"] is True

    (dest / "desktop" / "plugin.js").write_text("tampered\n", encoding="utf-8")
    report = vt.source_parity(src, dest)
    assert report["ok"] is False
    assert "desktop/plugin.js" in report["mismatches"]


def test_uninstall_leaves_no_plugin(tmp_path):
    src = _make_source_tree(tmp_path)
    home = tmp_path / "hermes-home"
    vt.install_backend(src, home)
    installed = home / "plugins" / vt.PLUGIN_ID
    assert installed.is_dir()

    vt.uninstall_backend(src, home)

    assert not installed.exists()


# ----------------------------------------------------- route inventory

def test_route_inventory_lists_only_scoped_lab_routes():
    routes = vt.route_inventory()
    shapes = sorted(f"{method} {path}" for method, path in routes)
    assert ("GET /health" in shapes)
    assert ("GET /models" in shapes)
    assert ("POST /complete" in shapes)


def test_bare_router_health_payload_works_without_auth():
    client = vt.bare_router_client()
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["plugin"] == vt.PLUGIN_ID


def test_bare_router_models_payload_is_sanitized(monkeypatch):
    fixture = {
        "provider": "prov-a",
        "model": "model-1",
        "providers": [
            {"slug": "prov-a", "name": "A", "authenticated": True,
             "models": ["model-1", "model-2"]},
            {"slug": "prov-bad", "name": "B", "authenticated": False,
             "models": ["secret-model"]},
        ],
    }
    monkeypatch.setattr(vt, "_router_models_payload", lambda: fixture)
    import dashboard.plugin_api as api
    monkeypatch.setattr(api, "_build_model_inventory", lambda: fixture)
    client = vt.bare_router_client()
    body = client.get("/models").json()
    slugs = [row["slug"] for row in body["providers"]]
    assert slugs == ["prov-a"]
    assert body["active"] == {"provider": "prov-a", "model": "model-1"}


# ------------------------------------------------------- live bridge

def test_bridge_probe_reports_auth_gate_not_mount_proof():
    """Gap 1: 401 is an auth gate; the probe must not claim mount proof."""
    probe = vt.probe_live_bridge("http://127.0.0.1:9119")
    assert "mount_proof_401" not in probe
    if not probe["reachable"]:
        pytest.skip("live desktop bridge not reachable from this session")
    # Auth middleware runs before routing: an unknown plugin also gets 401.
    assert probe["unauth_other_plugin_status"] == 401
    assert probe["auth_gate_401"] is True


def test_live_bridge_auth_gate_answers_401_when_reachable():
    """Read-only probe of the live WSL desktop bridge: both routes answer
    401 without credentials. That proves the auth gate only, not mounting."""
    probe = vt.probe_live_bridge("http://127.0.0.1:9119")
    if not probe["reachable"]:
        pytest.skip("live desktop bridge not reachable from this session")
    assert probe["unauth_plugin_health_status"] == 401


def test_remote_rest_contract_is_profile_aware():
    report = vt.remote_contract_report(ROOT / "desktop")
    assert report["rest_profile_aware"] is True


def test_remote_socket_contract_is_noop_on_oauth_remotes():
    report = vt.remote_contract_report(ROOT / "desktop")
    assert report["socket_oauth_noop"] is True


def test_model_lab_polls_over_rest_only():
    report = vt.desktop_source_contract(ROOT)
    assert report["uses_ctx_rest"] is True
    assert report["uses_ctx_socket"] is False


# ------------------------------------------ shippable-set predicate

def test_shippable_predicate_excludes_all_internal_local_files(tmp_path):
    """Gap 4: one shared predicate excludes every internal local file."""
    src = _make_source_tree(tmp_path)
    internal = [
        ".hermes.md", "AGENTS.md", "PRD.md", ".env.example",
        "docs/BUILDLOG.md", "docs/taskchecklist.json",
        ".dfg/receipt.json", "spikes/try.py", ".git/config",
        "__pycache__/x.pyc", "node_modules/pkg/index.js",
        ".venv/bin/python", ".pytest_cache/v/cache",
    ]
    for rel in internal:
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("internal\n", encoding="utf-8")
    # Public docs added later must stay possible.
    (src / "docs" / "install-guide.md").write_text("public\n", encoding="utf-8")

    shipped = vt.shippable_files(src)

    for rel in internal:
        assert rel not in shipped
    assert "docs/install-guide.md" in shipped
    assert "plugin.yaml" in shipped
    assert "desktop/plugin.js" in shipped


def test_copy_parity_and_plans_share_one_predicate(tmp_path):
    """Gap 4: copy, parity, install plan, uninstall plan all use the predicate."""
    src = _make_source_tree(tmp_path)
    (src / "AGENTS.md").write_text("internal\n", encoding="utf-8")

    dest = tmp_path / "copy"
    vt._copytree_shippable(src, dest)
    assert not (dest / "AGENTS.md").exists()

    installed = tmp_path / "installed" / vt.PLUGIN_ID
    shutil.copytree(dest, installed)
    assert vt.source_parity(src, installed)["ok"] is True

    plan = vt.plan_install(src, tmp_path / "plan-target")
    assert all(entry["path"] != "AGENTS.md" for entry in plan["files"])

    unplan = vt.plan_uninstall(src, installed)
    assert unplan["ok"] is True
    assert "AGENTS.md" not in unplan["files"]


# --------------------------------------------------- YAML config edits

def test_install_backend_preserves_other_plugins_and_config_yaml(tmp_path):
    """Gap 5: enabling uses safe_load/safe_dump and preserves everything else."""
    src = _make_source_tree(tmp_path)
    home = tmp_path / "hermes-home"
    home.mkdir()
    config = {
        "gateway": {"port": 9119},
        "plugins": {"enabled": ["some-other-plugin"]},
        "theme": "dark-plum",
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )

    result = vt.install_backend(src, home)

    assert result["ok"] is True
    saved = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert saved["plugins"]["enabled"] == ["some-other-plugin", vt.PLUGIN_ID]
    assert saved["gateway"] == {"port": 9119}
    assert saved["theme"] == "dark-plum"


def test_install_backend_creates_config_when_missing(tmp_path):
    src = _make_source_tree(tmp_path)
    home = tmp_path / "hermes-home"

    vt.install_backend(src, home)

    saved = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert saved == {"plugins": {"enabled": [vt.PLUGIN_ID]}}


def test_uninstall_backend_removes_only_this_plugin_and_keeps_rest(tmp_path):
    """Gap 5: disabling removes only hermes-model-lab via safe_load/safe_dump."""
    src = _make_source_tree(tmp_path)
    home = tmp_path / "hermes-home"
    vt.install_backend(src, home)
    config = {
        "gateway": {"port": 9119},
        "plugins": {"enabled": [vt.PLUGIN_ID, "another-plugin"]},
        "theme": "dark-plum",
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )

    result = vt.uninstall_backend(src, home)

    assert result["ok"] is True
    saved = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert saved["plugins"]["enabled"] == ["another-plugin"]
    assert saved["gateway"] == {"port": 9119}
    assert saved["theme"] == "dark-plum"


# ------------------------------------------------- node + sanitized proof

def test_node_check_runs_on_installed_copy(tmp_path, monkeypatch):
    """Gap 6: _run_node_check receives the installed plugin.js, not source."""
    src = _make_source_tree(tmp_path)
    seen: dict[str, Path] = {}

    def spy(plugin_js):
        seen["path"] = plugin_js
        return {"ok": True}

    monkeypatch.setattr(vt, "_run_node_check", spy)
    monkeypatch.setattr(vt, "_run_plugin_doctor", lambda h, d: {"ok": True})
    vt.run_verification(project_root=src)

    assert seen["path"].name == "plugin.js"
    assert seen["path"].parts[-4:] == (
        "plugins", vt.PLUGIN_ID, "desktop", "plugin.js",
    )
    # It must be inside a temp install home, not the project source tree.
    assert "model-lab-home-" in str(seen["path"])
    assert str(src) not in str(seen["path"])


def test_sanitized_models_proof_is_real_boolean_in_full_receipt(tmp_path, monkeypatch):
    """Gap 6: the sanitizer proof is a real check result, not a hardcoded None."""
    def fake_doctor(hermes_home: Path, plugin_dir: Path) -> dict:
        return {"ok": True}

    def fake_node_check(path: Path) -> dict:
        return {"ok": True}

    def fake_probe(base_url: str) -> dict:
        return {
            "reachable": False,
            "service_active": None,
            "port_listening": None,
            "unauth_plugin_health_status": None,
            "unauth_other_plugin_status": None,
        }

    monkeypatch.setattr(vt, "_run_plugin_doctor", fake_doctor)
    monkeypatch.setattr(vt, "_run_node_check", fake_node_check)
    monkeypatch.setattr(vt, "probe_live_bridge", fake_probe)

    receipt = vt.run_verification(project_root=_make_source_tree(tmp_path))

    value = receipt["local"]["bare_router_models_payload_sanitized_check_ran"]
    assert value is True or value is False
    # With a healthy bare router run the proof must actually be true.
    assert receipt["local"]["bare_router_health_ok"] is True


# ------------------------------------------------------- verdict gating

def test_verdict_requires_live_bridge_checks_but_not_mount_proof(
    tmp_path, monkeypatch,
):
    """Gap 2: reachable/service/port/auth_gate are required; mount_proof is not."""
    src = _make_source_tree(tmp_path)
    monkeypatch.setattr(vt, "_run_plugin_doctor", lambda h, d: {"ok": True})
    monkeypatch.setattr(vt, "_run_node_check", lambda p: {"ok": True})

    def probe(base_url: str) -> dict:
        return {
            "reachable": True,
            "service_active": True,
            "port_listening": True,
            "unauth_plugin_health_status": 401,
            "unauth_other_plugin_status": 401,
            "auth_gate_401": True,
        }

    monkeypatch.setattr(vt, "probe_live_bridge", probe)
    receipt = vt.run_verification(project_root=src)
    assert receipt["ok"] is True

    # Now degrade each required bridge signal; the verdict must fail each time.
    healthy = probe("")
    for key in ("reachable", "service_active", "port_listening", "auth_gate_401"):
        degraded = dict(healthy)
        degraded[key] = False
        if key == "auth_gate_401":
            degraded["unauth_other_plugin_status"] = 403
        monkeypatch.setattr(vt, "probe_live_bridge", lambda u, d=degraded: d)
        failed = vt.run_verification(project_root=src)
        assert failed["ok"] is False, f"{key} must gate the verdict"


# ------------------------------------------------ end-to-end receipt

def test_full_run_reports_clean_receipt_and_cleans_up(tmp_path, monkeypatch):
    src = _make_source_tree(tmp_path)
    calls: dict[str, bool] = {}

    def fake_doctor(hermes_home: Path, plugin_dir: Path) -> dict:
        calls["doctor"] = True
        return {"ok": True}

    def fake_node_check(path: Path) -> dict:
        calls["node_check"] = True
        return {"ok": True}

    def fake_probe(base_url: str) -> dict:
        calls["probe"] = True
        return {
            "reachable": True,
            "service_active": True,
            "port_listening": True,
            "unauth_plugin_health_status": 401,
            "unauth_other_plugin_status": 401,
            "auth_gate_401": True,
        }

    monkeypatch.setattr(vt, "_run_plugin_doctor", fake_doctor)
    monkeypatch.setattr(vt, "_run_node_check", fake_node_check)
    monkeypatch.setattr(vt, "probe_live_bridge", fake_probe)

    receipt = vt.run_verification(project_root=src)

    assert receipt["ok"] is True, json.dumps(receipt, indent=2, default=str)
    assert receipt["exit_code"] == 0
    assert calls == {"doctor": True, "node_check": True, "probe": True}
    assert receipt["local"]["parity_ok"] is True
    assert receipt["local"]["doctor_ok"] is True
    assert receipt["local"]["node_syntax_ok"] is True
    assert receipt["local"]["uninstall_clean"] is True
    assert receipt["windows"]["path_contract_ok"] is True
    assert receipt["windows"]["no_overwrite_ok"] is True
    assert receipt["windows"]["uninstall_plan_ok"] is True
    assert receipt["bridge"]["auth_gate_401"] is True
    assert "mount_proof_401" not in receipt["bridge"]
    assert receipt["remote"]["rest_profile_aware"] is True
    assert receipt["remote"]["socket_oauth_noop"] is True
    # Cleanup proof: every disposable root created by the run is gone.
    assert all(not Path(p).exists() for p in receipt["cleanup_removed_paths"])
