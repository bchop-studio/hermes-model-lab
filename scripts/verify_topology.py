#!/usr/bin/env python
"""Hermes Model Lab topology verification (T008).

Proves the plugin's three supported topologies without touching live
Hermes state or Windows OS settings:

1. Local disposable install — install/enable both plugin halves into a
   temporary HERMES_HOME, run Plugin Doctor on the installed copy, check
   exact source parity and Desktop JS syntax, inventory the backend route
   table, exercise a bare (unauthenticated) router client, then uninstall
   and prove nothing remains.
2. Windows-to-WSL path contract — resolve the real Windows username,
   derive the expected %LOCALAPPDATA%/hermes/desktop-plugins/<plugin-id>/
   path, prove folder-id equals plugin id, source parity logic, and that
   install/uninstall planning never overwrites an existing file. The
   backend half stays in the WSL Hermes home. Path translation and
   planning run against a temporary fake Windows root.
3. Remote/OAuth behavior contracts — static source proof that the Desktop
   SDK's ctx.rest is profile-aware, ctx.socket is optional/no-op on OAuth
   remotes, and Model Lab polls over REST only.

A read-only probe of the live WSL desktop bridge (service active, port
9119 listening, unauthenticated /api/plugins/... routes → 401) documents
the auth-gate semantics; a 401 does NOT prove the plugin is mounted,
because auth middleware runs before routing and an unknown plugin also
answers 401. No token is ever read or printed. Every disposable root is
removed before exit; failures are reported honestly in the JSON receipt
with a nonzero exit code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PureWindowsPath

PLUGIN_ID = "hermes-model-lab"
DESKTOP_ENTRY_NAME = "plugin.js"

# Route inventory every installed backend must expose (method, path).
REQUIRED_ROUTES = {("GET", "/health"), ("GET", "/models"), ("POST", "/complete")}

BRIDGE_PORT = 9119

# Desktop-side contract: Model Lab talks to its backend through ctx.rest
# only; ctx.socket is an accelerator it must not depend on.
REST_CALLS = {"loadHealth": "ctx.rest('/health')", "loadModels": "ctx.rest('/models')"}
FORBIDDEN_IN_DESKTOP = ("ctx.socket",)

WINDOWS_USERNAME_ALLOW = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_. ")


# ============================================================ small utils

def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# VCS/internal trees never packaged or compared for source parity.
# "artifacts" covers prior local release outputs (artifacts/build-*), so a
# build run against an already-populated tree can never embed its own or
# older archives; this replaces the builder's old single-archive exclusion.
NON_SHIPPED_DIRS = {"artifacts", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".venv", ".dfg", "spikes"}

# Repository-only and internal local files excluded from the installable
# package set (copy, parity, install plan, uninstall plan).
NON_SHIPPED_FILES = {
    ".hermes.md",
    "AGENTS.md",
    "PRD.md",
    ".env.example",
    ".gitignore",
    "cover.png",
    "docs/BUILDLOG.md",
    "docs/taskchecklist.json",
}


def is_shippable(relative_parts: tuple[str, ...]) -> bool:
    """One shared predicate: is this project-relative path shippable?

    *relative_parts* is a path split into its parts, e.g.
    ("desktop", "plugin.js"). Internal caches, VCS state, spikes, and the
    named local-only docs are excluded; anything else ships.
    """
    if not relative_parts:
        return False
    if any(part in NON_SHIPPED_DIRS for part in relative_parts):
        return False
    return "/".join(relative_parts) not in NON_SHIPPED_FILES


def shippable_files(root: Path) -> set[str]:
    """Project-relative POSIX paths of every shippable file under *root*."""
    src = Path(root)
    return {
        path.relative_to(src).as_posix()
        for path in sorted(src.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and is_shippable(path.relative_to(src).parts)
    }


def _sha_tree(root: Path) -> dict[str, str]:
    """Hash every shippable file; symlinks record their link targets.

    The file set comes from the shared shippable predicate, so internal
    local files and caches are excluded from both installation and parity.
    """
    manifest: dict[str, str] = {}
    src = Path(root)
    for rel in sorted(shippable_files(src)):
        path = src / rel
        if Path(path).is_symlink():
            manifest[rel] = f"symlink:{os.readlink(path)}"
        else:
            manifest[rel] = _hash_file(Path(path))
    return manifest


# ============================================== Windows path contract

def windows_home(username: str) -> str:
    """The Windows home for *username* (e.g. C:\\Users\\chris)."""
    return rf"C:\Users\{username}"


def windows_localappdata(username: str) -> str:
    return rf"{windows_home(username)}\AppData\Local"


def windows_desktop_plugin_path(username: str) -> PureWindowsPath:
    """Expected Windows Desktop plugin folder for this plugin.

    Built with PureWindowsPath so the string always uses Windows
    backslash separators, never a mixed-slash style.
    """
    return (
        PureWindowsPath(windows_localappdata(username))
        / "hermes"
        / "desktop-plugins"
        / PLUGIN_ID
        / DESKTOP_ENTRY_NAME
    )


def resolve_windows_user_root(fake_users_root: str | None = None) -> str | None:
    """Resolve the current Windows username from the WSL mount.

    Reads only directory NAMES under /mnt/c/Users (or a fake stand-in used
    by tests); never opens or stats anything inside a user profile, so
    unreadable profiles are skipped safely.
    """
    base = Path(fake_users_root or "/mnt/c/Users")
    if not base.is_dir():
        return None
    reserved = {
        "all users", "default", "default user", "public", "desktop.ini",
    }
    best: str | None = None
    try:
        names = sorted(entry.name for entry in base.iterdir())
    except OSError:
        return None
    for name in names:
        if name.lower() in reserved:
            continue
        if not set(name) <= WINDOWS_USERNAME_ALLOW:
            continue
        best = name
    return best


def backend_plugin_path(hermes_home: Path) -> Path:
    """The WSL-side backend install location."""
    return Path(hermes_home) / "plugins" / PLUGIN_ID


# ======================================= no-overwrite plan/install/remove

def plan_install(source_root: Path, dest_root: Path) -> dict:
    """Plan copying *source_root* into *dest_root* without overwriting."""
    files = []
    ok = True
    src = Path(source_root)
    for rel in sorted(shippable_files(src)):
        target = Path(dest_root) / rel
        conflict = target.exists()
        ok = ok and not conflict
        files.append({"path": rel, "conflict": conflict})
    return {"ok": ok, "files": files}


def plan_uninstall(source_root: Path, dest_root: Path) -> dict:
    """Plan removal of an exact known file set; refuse unknown folders.

    The destination folder must be exactly our plugin id and must contain
    only files whose names exist in the source tree, so a typo or a
    foreign plugin can never be deleted.
    """
    src = Path(source_root)
    dest = Path(dest_root)
    if dest.name != PLUGIN_ID:
        return {"ok": False, "reason": f"folder is not {PLUGIN_ID}", "files": []}
    source_names = shippable_files(src)
    unknown = []
    for path in sorted(dest.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(dest).as_posix()
            if rel not in source_names:
                unknown.append(rel)
    if unknown:
        return {"ok": False, "reason": "unknown files present", "files": unknown}
    return {"ok": True, "files": sorted(source_names)}


def _copytree_shippable(src: Path, dest: Path) -> None:
    """Copy only the shippable tree via the shared predicate."""
    src = Path(src)
    shutil.copytree(
        src, dest,
        ignore=lambda _dir, names: [
            name
            for name in names
            if not is_shippable((Path(_dir) / name).relative_to(src).parts)
        ],
    )


def _enable_plugin_in_config(home: Path, enable: bool) -> None:
    """Add/remove only this plugin from plugins.enabled via safe YAML.

    Uses yaml.safe_load/safe_dump so every other key and every other
    enabled plugin survives untouched. A missing plugins section is
    created; after a removal that leaves it empty, the section stays.
    """
    import yaml

    config = home / "config.yaml"
    data: dict = {}
    if config.exists():
        loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        data["plugins"] = plugins
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
        plugins["enabled"] = enabled
    if enable and PLUGIN_ID not in enabled:
        enabled.append(PLUGIN_ID)
        config.write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
    elif not enable:
        kept = [name for name in enabled if name != PLUGIN_ID]
        if len(kept) != len(enabled):
            if kept:
                plugins["enabled"] = kept
            else:
                plugins.pop("enabled", None)
                if not plugins:
                    data.pop("plugins", None)
            if data:
                config.write_text(
                    yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
                )
            else:
                config.unlink()


def install_backend(
    source_root: Path, hermes_home: Path, *, enable: bool = True
) -> dict:
    """Copy the backend half into <hermes_home>/plugins/<id>, no overwrite.

    Enabling edits the disposable home's config.yaml through safe YAML
    round-tripping so nothing else in the file can be corrupted.
    """
    src = Path(source_root)
    home = Path(hermes_home)
    dest = backend_plugin_path(home)
    plan = plan_install(src, dest)
    if not plan["ok"]:
        return {"ok": False, "reason": "install would overwrite existing files"}

    _copytree_shippable(src, dest)

    if enable:
        _enable_plugin_in_config(home, enable=True)

    parity = source_parity(src, dest)
    return {"ok": parity["ok"], "parity": parity, "installed": str(dest)}


def uninstall_backend(source_root: Path, hermes_home: Path) -> dict:
    """Remove exactly the installed file set plus the enablement line."""
    home = Path(hermes_home)
    dest = backend_plugin_path(home)
    plan = plan_uninstall(source_root, dest)
    if not plan["ok"]:
        return {"ok": False, **{k: v for k, v in plan.items() if k != "files"}}
    shutil.rmtree(dest)
    _enable_plugin_in_config(home, enable=False)
    return {"ok": True, "removed": str(dest)}


def source_parity(source_root: Path, installed_root: Path) -> dict:
    mismatches = []
    before = _sha_tree(Path(source_root))
    after = _sha_tree(Path(installed_root))
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            mismatches.append(key)
    return {"ok": not mismatches, "mismatches": mismatches}


# ==================================================== route inventory

def _router_models_payload() -> dict:
    import dashboard.plugin_api as api  # type: ignore[no-redef]

    return api._build_model_inventory()


def _import_router():
    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root))
    import dashboard.plugin_api as api

    return api


def route_inventory() -> list[tuple[str, str]]:
    api = _import_router()
    routes = []
    for route in api.router.routes:
        for method in getattr(route, "methods", ()) or ():
            routes.append((method, getattr(route, "path", "")))
    return routes


def bare_router_client():
    """A FastAPI TestClient holding ONLY the plugin router.

    This proves the router's own payloads work with no auth middleware in
    front — the shape the host server exposes behind its own gate.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api = _import_router()
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def check_route_inventory() -> dict:
    try:
        routes = route_inventory()
    except Exception as exc:  # noqa: BLE001 - receipt records class only
        return {"ok": False, "error_class": type(exc).__name__}
    shapes = {(method.upper(), path) for method, path in routes}
    missing = [f"{m} {p}" for m, p in REQUIRED_ROUTES if (m, p) not in shapes]
    extras = [f"{m} {p}" for m, p in shapes if (m, p) not in REQUIRED_ROUTES]
    return {"ok": not missing, "missing": missing, "extras": extras}


# ================================================== live bridge probe

def _bridge_port_listening(port: int = BRIDGE_PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _systemd_service_active(unit: str = "hermes-desktop-bridge") -> bool | None:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() == "active"


def probe_live_bridge(base_url: str) -> dict:
    """Read-only probe of the live bridge. Never reads or prints tokens."""
    reachable = False
    status_health = None
    status_other = None
    try:
        request = urllib.request.Request(f"{base_url}/api/dashboard/plugins", method="GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
        reachable = True
    except urllib.error.HTTPError as exc:
        reachable = True
        _ = exc.read()
    except (urllib.error.URLError, OSError):
        reachable = False
    if reachable:
        for attr, path in (
            ("status_health", f"/api/plugins/{PLUGIN_ID}/health"),
            ("status_other", "/api/plugins/some-other-plugin/health"),
        ):
            try:
                request = urllib.request.Request(base_url + path, method="GET")
                with urllib.request.urlopen(request, timeout=5) as response:
                    code = response.status
                    response.read()
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.read()
            except (urllib.error.URLError, OSError):
                code = None
            if attr == "status_health":
                status_health = code
            else:
                status_other = code
    service_active = _systemd_service_active()
    port_listening = _bridge_port_listening()
    # Gap 1: 401 on the plugin route is only an auth gate. The unknown
    # plugin route answers 401 too, so this proves nothing about mounting.
    return {
        "reachable": reachable,
        "service_active": service_active,
        "port_listening": port_listening,
        "unauth_plugin_health_status": status_health,
        "unauth_other_plugin_status": status_other,
        "auth_gate_401": bool(
            reachable and status_health == 401 and status_other == 401
        ),
    }


# ============================================ remote/OAuth contracts

def remote_contract_report(project_root: Path) -> dict:
    """Static proof of the Desktop SDK's remote behavior contracts.

    Reads two sources, both read-only:

    1. The shipped Hermes agent checkout's ``contrib/plugin.ts`` — the
       PluginContext contract documents ``ctx.rest`` as profile-aware and
       namespace-scoped, and ``ctx.socket`` as resolving to a no-op on
       OAuth remotes.
    2. Model Lab's own Desktop source — it must call only ``ctx.rest`` and
       never depend on ``ctx.socket``, so REST polling alone must be
       sufficient on any remote.

    No remote is contacted and nothing is written.
    """
    candidates = [
        Path(os.environ.get("HERMES_AGENT_ROOT", "")) / "apps/desktop/src/contrib/plugin.ts",
        Path("/home/bchop/.hermes/hermes-agent/apps/desktop/src/contrib/plugin.ts"),
    ]
    report: dict = {
        "rest_profile_aware": False,
        "socket_oauth_noop": False,
        "sdk_source": None,
    }
    for candidate in candidates:
        if candidate.is_file():
            report["sdk_source"] = str(candidate)
            break
    if report["sdk_source"] is None:
        return report
    text = Path(report["sdk_source"]).read_text(encoding="utf-8")
    rest_docs = ""
    socket_docs = ""
    for block in text.split("/**")[1:]:
        body, _, decl = block.partition("*/")
        if "\n  rest:" in decl or decl.lstrip().startswith("rest:"):
            rest_docs = body.lower()
        if "\n  socket:" in decl or decl.lstrip().startswith("socket:"):
            socket_docs = body.lower()
    report["rest_profile_aware"] = (
        "profile" in rest_docs and "namespace-scoped" in rest_docs
    )
    report["socket_oauth_noop"] = (
        "oauth" in socket_docs and "no-op" in socket_docs
    )

    desktop = desktop_source_contract(_project_root_of(Path(project_root))) if _has_desktop_source(project_root) else {"uses_ctx_rest": False, "uses_ctx_socket": False}
    report["desktop_rest_only"] = (
        desktop["uses_ctx_rest"] and not desktop["uses_ctx_socket"]
    )
    report["polling_sufficient_for_model_lab"] = (
        report["desktop_rest_only"] and report["socket_oauth_noop"]
    )
    return report


def _has_desktop_source(path: Path) -> bool:
    """True when a project root or desktop dir with plugin.js can be found."""
    try:
        _project_root_of(Path(path))
        return True
    except FileNotFoundError:
        return False


def desktop_source_contract(project_root: Path) -> dict:
    """Model Lab's own Desktop source polls over REST only."""
    js = Path(project_root) / "desktop" / DESKTOP_ENTRY_NAME
    text = js.read_text(encoding="utf-8")
    return {
        "uses_ctx_rest": all(call in text for call in REST_CALLS.values()),
        "uses_ctx_socket": any(bad in text for bad in FORBIDDEN_IN_DESKTOP),
    }


# =========================================== external tool wrappers

def _run_plugin_doctor(hermes_home: Path, plugin_dir: Path) -> dict:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(hermes_home)
    env.setdefault("NODE_NO_WARNINGS", "1")
    python = sys.executable
    try:
        proc = subprocess.run(
            [python, "-m", "hermes_cli.main", "plugins", "doctor", ".", "--ci"],
            capture_output=True, text=True, timeout=120, cwd=str(plugin_dir),
            env=env, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error_class": type(exc).__name__}
    output = (proc.stdout + proc.stderr).strip()
    tail = output[-400:] if len(output) > 400 else output
    # Never echo environment-derived values; the doctor output is tool text.
    return {"ok": proc.returncode == 0, "tail": tail, "exit_code": proc.returncode}


def _run_node_check(plugin_js: Path) -> dict:
    try:
        proc = subprocess.run(
            ["node", "--check", str(plugin_js)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error_class": type(exc).__name__}
    tail = (proc.stdout + proc.stderr).strip()
    tail = tail[-200:] if len(tail) > 200 else tail
    return {"ok": proc.returncode == 0, "tail": tail}


# ==================================================== orchestration

def run_verification(
    project_root: Path,
    *,
    keep_temp: bool = False,
) -> dict:
    root = Path(project_root).resolve()
    receipt: dict = {
        "local": {},
        "windows": {},
        "remote": {},
        "bridge": {},
        "cleanup_removed_paths": [],
    }
    created_roots: list[Path] = []

    def _temp_root(prefix: str) -> Path:
        nonlocal keep_temp
        path = Path(tempfile.mkdtemp(prefix=f"model-lab-{prefix}-"))
        created_roots.append(path)
        return path

    # ---- local disposable install --------------------------------------
    local = receipt["local"]
    hermes_home = _temp_root("home")
    install_result = install_backend(root, hermes_home)
    local["install_ok"] = install_result["ok"]
    local["parity_ok"] = install_result.get("parity", {}).get("ok", False)

    plugin_dir = backend_plugin_path(hermes_home)
    doctor = _run_plugin_doctor(hermes_home, plugin_dir)
    local["doctor_ok"] = doctor.get("ok", False)
    # Gap 6: check the installed copy, never the source tree.
    node = _run_node_check(plugin_dir / "desktop" / DESKTOP_ENTRY_NAME)
    local["node_syntax_ok"] = node.get("ok", False)

    routes = check_route_inventory()
    local["route_inventory_ok"] = routes["ok"]
    local["routes_missing"] = routes.get("missing", [])
    local["routes_extra"] = routes.get("extras", [])

    health_ok = models_ok = False
    models_sanitized_ok: bool | None = None
    try:
        client = bare_router_client()
        response = client.get("/health")
        health_ok = response.status_code == 200 and response.json().get("ok") is True
        bare_models = client.get("/models")
        models_ok = bare_models.status_code == 200
        if models_ok:
            import dashboard.plugin_api as api

            raw = api._build_model_inventory()
            _c, _l, rows, active = api._sanitize_model_inventory(raw)
            body = bare_models.json()
            expected_rows = sorted(row["slug"] for row in rows)
            got_rows = sorted(row["slug"] for row in body["providers"])
            models_sanitized_ok = (
                got_rows == expected_rows and body.get("active") == active
            )
            # Every provider the sanitizer dropped must be absent from /models.
            dropped = {
                str(r.get("slug") or "")
                for r in (raw.get("providers") or [])
                if r.get("authenticated") is False
            }
            served = {row["slug"] for row in body["providers"]}
            models_sanitized_ok = models_sanitized_ok and not (dropped & served)
    except Exception:  # noqa: BLE001 - receipt records failure honestly
        health_ok = False
        models_sanitized_ok = False
    local["bare_router_health_ok"] = health_ok
    local["bare_router_models_ok"] = models_ok
    local["bare_router_models_payload_sanitized_check_ran"] = (
        models_sanitized_ok is True
    )

    uninstall = uninstall_backend(root, hermes_home)
    local["uninstall_ok"] = uninstall["ok"]
    local["uninstall_clean"] = (
        uninstall["ok"] and not backend_plugin_path(hermes_home).exists()
    )

    # ---- windows-to-WSL path contract ----------------------------------
    windows = receipt["windows"]
    username = resolve_windows_user_root()
    windows["windows_username_resolved"] = username is not None
    expected = windows_desktop_plugin_path(username) if username else None
    windows["expected_desktop_path_pattern_ok"] = bool(expected) and all(
        part in str(expected) for part in ("AppData", "Local", "hermes", "desktop-plugins", PLUGIN_ID)
    )
    windows["folder_id_equals_plugin_id"] = bool(
        expected and expected.parts[-2] == PLUGIN_ID
    )

    fake_win = _temp_root("winroot")
    fake_users = fake_win / "Users" / "chris" / "AppData" / "Local"
    fake_users.mkdir(parents=True)
    resolved_fake = resolve_windows_user_root(str(fake_win / "Users"))
    windows["fake_root_translation_ok"] = resolved_fake == "chris"

    fake_dest = fake_win / "LocalAppData" / "hermes" / "desktop-plugins" / PLUGIN_ID
    fake_dest.mkdir(parents=True, exist_ok=True)
    (fake_dest / DESKTOP_ENTRY_NAME).write_text("existing\n", encoding="utf-8")
    blocked = plan_install(root / "desktop", fake_dest)
    windows["no_overwrite_ok"] = blocked["ok"] is False and any(
        entry["conflict"] for entry in blocked["files"]
    )
    foreign_dest = fake_win / "desktop-plugins-sim" / "not-our-plugin"
    shutil.copytree(root, foreign_dest)
    windows["uninstall_plan_ok"] = plan_uninstall(root, foreign_dest)["ok"] is False
    windows["backend_stays_in_wsl_home_ok"] = "mnt" not in backend_plugin_path(
        Path("/home/bchop/.hermes")
    ).parts
    windows["path_contract_ok"] = all(
        windows[key]
        for key in (
            "windows_username_resolved",
            "expected_desktop_path_pattern_ok",
            "folder_id_equals_plugin_id",
            "fake_root_translation_ok",
            "backend_stays_in_wsl_home_ok",
        )
    )

    # ---- remote contracts ----------------------------------------------
    remote = receipt["remote"]
    remote.update(remote_contract_report(root))
    desktop = desktop_source_contract(root)
    remote["rest_only_polling"] = desktop["uses_ctx_rest"] and not desktop["uses_ctx_socket"]

    # ---- live bridge probe (read-only) ----------------------------------
    receipt["bridge"] = probe_live_bridge(f"http://127.0.0.1:{BRIDGE_PORT}")
    bridge = receipt["bridge"]
    # Gap 1: a 401 only proves auth middleware is in front of the route
    # table; an unknown plugin gets 401 too. It does NOT prove this plugin
    # is mounted. mount_proof stays false/unverified until Hestia installs
    # on Windows and reads live mount evidence.
    bridge["mount_proof"] = False
    bridge["mount_proof_source"] = "unverified: requires live Windows install"
    bridge["semantics_documented"] = (
        "Loopback binds answer 401 without the session token cookie/bearer; "
        "auth middleware runs before routing, so an unknown plugin also "
        "answers 401. A 401 alone is an auth gate, not mount proof. The "
        "token is injected into the SPA HTML for the renderer and never "
        "exposed here. The plugin API is mounted at "
        "/api/plugins/hermes-model-lab/ only when the user plugin is in "
        "config.yaml plugins.enabled."
    )

    # ---- cleanup --------------------------------------------------------
    removed: list[str] = []
    for temp_root in reversed(created_roots):
        if keep_temp:
            continue
        if temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)
            if not temp_root.exists():
                removed.append(str(temp_root))
    receipt["cleanup_removed_paths"] = removed
    receipt["cleanup_complete"] = all(
        not Path(p).exists() for p in created_roots if not keep_temp
    ) if not keep_temp else True

    checks = [
        local["install_ok"],
        local["parity_ok"],
        local["doctor_ok"],
        local["node_syntax_ok"],
        local["route_inventory_ok"],
        local["bare_router_health_ok"],
        # Gap 6: the sanitizer proof is a required receipt check.
        local["bare_router_models_payload_sanitized_check_ran"],
        local["uninstall_clean"],
        windows["windows_username_resolved"],
        windows["expected_desktop_path_pattern_ok"],
        windows["folder_id_equals_plugin_id"],
        windows["fake_root_translation_ok"],
        windows["no_overwrite_ok"],
        windows["uninstall_plan_ok"],
        windows["backend_stays_in_wsl_home_ok"],
        remote["rest_profile_aware"],
        remote["socket_oauth_noop"],
        remote["rest_only_polling"],
    ]
    bridge_checks = [
        "reachable",
        "service_active",
        "port_listening",
        "auth_gate_401",
    ]
    # Gap 2: live bridge status gates the verdict on this machine.
    # mount_proof is deliberately NOT required yet.
    checks.extend(bool(bridge.get(key)) for key in bridge_checks)
    receipt["ok"] = all(checks) and receipt["cleanup_complete"]
    receipt["exit_code"] = 0 if receipt["ok"] else 1
    return receipt


def _project_root_of(path: Path) -> Path:
    """Return the project root for a root-or-desktop-dir argument."""
    if (path / "desktop" / DESKTOP_ENTRY_NAME).is_file():
        return path
    if path.name == "desktop" and (path / DESKTOP_ENTRY_NAME).is_file():
        return path.parent
    # Fallback: a directory whose parent holds desktop/plugin.js
    parent = path.parent
    if (parent / "desktop" / DESKTOP_ENTRY_NAME).is_file():
        return parent
    raise FileNotFoundError(f"cannot locate {DESKTOP_ENTRY_NAME} from {path}")


def _agent_root_candidates(_project_root: Path) -> Path:
    return Path("/home/bchop/.hermes/hermes-agent")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep disposable roots for inspection (never default)")
    args = parser.parse_args(argv)
    receipt = run_verification(Path(args.project_root), keep_temp=args.keep_temp)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return receipt["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
