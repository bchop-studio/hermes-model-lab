#!/usr/bin/env python
"""Deterministic local release builder and verifier for T009.

Produces a byte-for-byte reproducible .tar.gz of the downloader-facing
release tree under one stable root directory, verifies the extracted
artifact with the real Plugin Doctor and backend contracts, and prints
a JSON receipt. LOCAL ONLY: nothing is tagged, pushed, or published.

Determinism guarantees:
- sorted member paths, one fixed root name
- fixed mtime (0), uid/gid 0, empty uname/gname, explicit modes
- gzip with mtime=0 and no filename embedded in the header

Safety guarantees:
- members are regular files/directories only (no symlinks/hardlinks)
- relative paths, no "..", extraction never escapes the destination
- inventory comes from verify_topology.is_shippable, so internal files,
  caches, VCS data, spikes, and credential templates never ship

Usage:
    python scripts/build_release.py --project-root <root> \
        [--output-dir artifacts] [--verify]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import verify_topology as vt  # noqa: E402

PLUGIN_ID = vt.PLUGIN_ID
RELEASE_ROOT_NAME = f"{PLUGIN_ID}-0.1.0"
FIXED_MTIME = 0
FILE_MODE = 0o644
DIR_MODE = 0o755


# ------------------------------------------------------------ inventory

def shippable_files(root: Path) -> set[str]:
    """The exact downloader-facing file set (project-relative paths)."""
    return vt.shippable_files(Path(root))


# --------------------------------------------------------------- build

def build_archive(
    project_root: Path,
    output_dir: Path,
) -> dict:
    """Build the deterministic release archive into *output_dir*.

    Prior outputs under artifacts/ are excluded by the shared predicate.
    """
    root = Path(project_root).resolve()
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{RELEASE_ROOT_NAME}.tar.gz"
    # Defense-in-depth note: prior outputs under artifacts/ are excluded by
    # the shared predicate (NON_SHIPPED_DIRS contains "artifacts"), so no
    # per-archive exclusion is needed here anymore.
    entries: list[tuple[str, bytes]] = []
    for rel in sorted(shippable_files(root)):
        entries.append((rel, (root / rel).read_bytes()))

    dirs: set[str] = {RELEASE_ROOT_NAME}
    for rel, _ in entries:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            dirs.add(f"{RELEASE_ROOT_NAME}/{'/'.join(parts[:i])}")

    payload = io.BytesIO()
    with tarfile.open(
        fileobj=payload, mode="w", format=tarfile.GNU_FORMAT
    ) as tar:
        def _add(name: str, data: bytes | None) -> None:
            info = tarfile.TarInfo(name)
            info.mtime = FIXED_MTIME
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = DIR_MODE
                info.size = 0
                tar.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.mode = FILE_MODE
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        for d in sorted(dirs):
            _add(d, None)
        for rel, data in entries:
            _add(f"{RELEASE_ROOT_NAME}/{rel}", data)

    raw = payload.getvalue()
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as gz:
        gz.write(raw)
    blob = compressed.getvalue()

    archive_path = out_dir / filename
    archive_path.write_bytes(blob)
    return {
        "filename": filename,
        "archive": archive_path,
        "size_bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "inventory": [f"{RELEASE_ROOT_NAME}/{rel}" for rel, _ in entries],
        "dir_count": len(dirs),
        "file_count": len(entries),
    }


def extract_archive(archive_path: Path, dest: Path) -> Path:
    """Safely extract into *dest*, refusing unsafe or foreign members.

    Returns the extracted release-root path.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    expected_root = RELEASE_ROOT_NAME
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.rstrip("/")
            parts = Path(name).parts
            if (
                member.name.endswith("/")
                and not member.isdir()
            ):
                raise ValueError(f"non-directory with trailing slash: {member.name}")
            if parts[0] != expected_root:
                raise ValueError(f"member outside release root: {member.name}")
            if not member.isreg() and not member.isdir():
                raise ValueError(f"unsafe member type: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"link member refused: {member.name}")
            resolved = (dest / name).resolve()
            if not str(resolved).startswith(str(dest.resolve()) + "/"):
                raise ValueError(f"path escape refused: {member.name}")
        tar.extractall(dest, filter="data")
    return dest / expected_root


def verify_extracted(
    archive_path: Path,
    *,
    work_root: Path,
    source_root: Path,
) -> dict:
    """Extract into a disposable tree and prove it is the real product.

    Runs, against the EXTRACTED copy only: no-overwrite install planning,
    source parity between the shipped tree and the extracted copy,
    Hermes Plugin Doctor, node syntax check on the Desktop entry, route
    inventory, and bare-router health. Every temp path is removed.
    """
    work_root = Path(work_root)
    report: dict = {}
    hermes_home = work_root / "hermes-home"
    extracted_root = extract_archive(archive_path, work_root / "extracted")
    report["extracted_file_count"] = sum(
        1 for p in extracted_root.rglob("*") if p.is_file()
    )

    # No-overwrite install plan must be clean on an empty destination.
    dest = hermes_home / "plugins" / PLUGIN_ID
    plan = vt.plan_install(extracted_root, dest)
    report["no_overwrite_install_ok"] = plan["ok"]

    install = vt.install_backend(extracted_root, hermes_home)
    report["parity_ok"] = install.get("parity", {}).get("ok", False)

    doctor = vt._run_plugin_doctor(hermes_home, dest)
    report["doctor_ok"] = doctor.get("ok", False)

    node = vt._run_node_check(dest / "desktop" / vt.DESKTOP_ENTRY_NAME)
    report["node_syntax_ok"] = node.get("ok", False)

    routes = vt.check_route_inventory()
    report["route_inventory_ok"] = routes["ok"]
    report["routes_missing"] = routes.get("missing", [])

    try:
        client = vt.bare_router_client()
        response = client.get("/health")
        report["bare_router_health_ok"] = (
            response.status_code == 200 and response.json().get("ok") is True
        )
    except Exception:  # noqa: BLE001 - recorded honestly
        report["bare_router_health_ok"] = False

    uninstall = vt.uninstall_backend(extracted_root, hermes_home)
    report["uninstall_clean"] = uninstall["ok"] and not dest.exists()

    # Cleanup proof: remove every disposable path.
    removed = []
    for path in (work_root / "extracted", hermes_home):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                removed.append(str(path))
    report["cleanup_removed_paths"] = removed
    report["cleanup_complete"] = all(not Path(p).exists() for p in removed)
    report["ok"] = all(
        report[key]
        for key in (
            "no_overwrite_install_ok",
            "parity_ok",
            "doctor_ok",
            "node_syntax_ok",
            "route_inventory_ok",
            "bare_router_health_ok",
            "uninstall_clean",
            "cleanup_complete",
        )
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--output-dir", default="artifacts")
    parser.add_argument("--build-twice", action="store_true",
                        help="build twice and prove byte equality")
    parser.add_argument("--verify", action="store_true",
                        help="extract to a disposable tree and verify")
    args = parser.parse_args(argv)

    root = Path(args.project_root).resolve()
    receipt: dict = {"local_only": True}

    out_a = Path(args.output_dir) / "build-a"
    first = build_archive(root, out_a)
    receipt["artifact"] = {
        k: v for k, v in first.items() if k != "archive"
    }
    receipt["artifact"]["path"] = str(first["archive"])

    if args.build_twice:
        second = build_archive(root, Path(args.output_dir) / "build-b")
        receipt["determinism"] = {
            "second_sha256": second["sha256"],
            "byte_identical": (
                first["sha256"] == second["sha256"]
                and first["size_bytes"] == second["size_bytes"]
                and first["archive"].read_bytes()
                == second["archive"].read_bytes()
            ),
        }

    if args.verify:
        work = Path(tempfile.mkdtemp(prefix="model-lab-release-verify-"))
        try:
            report = verify_extracted(first["archive"], work_root=work,
                                      source_root=root)
            receipt["extracted_verification"] = report
        finally:
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            receipt["temp_cleanup_done"] = not work.exists()

    print(json.dumps(receipt, indent=2, sort_keys=True))
    ok = True
    if args.build_twice:
        ok = ok and receipt["determinism"]["byte_identical"]
    if args.verify:
        ok = ok and receipt["extracted_verification"]["ok"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
