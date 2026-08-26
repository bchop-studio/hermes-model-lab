"""T009 release packaging tests.

Covers the deterministic local release builder
(scripts/build_release.py):

1. Package inventory: the archive contains exactly the shippable file
   set under ONE stable root directory, nothing more.
2. Determinism: building twice produces byte-for-byte identical archives.
3. Safe extraction: every member path is relative, stays inside the
   archive root, has an explicit safe mode, and extraction never escapes
   the destination directory.
4. Internal-file exclusion: agent-only docs, internal state, caches,
   VCS data, credentials templates, and spikes never ship.
5. Extracted-product verification: the extracted tree passes Plugin
   Doctor in a disposable Hermes home, node syntax check on its Desktop
   entry, and backend route/health contract checks.
"""

import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_release as br  # noqa: E402


# ------------------------------------------------------------- inventory

def _member_paths(archive_path: Path) -> list[str]:
    with tarfile.open(archive_path, "r:gz") as tar:
        return tar.getnames()


def _expected_inventory(root: Path) -> list[str]:
    shippable = sorted(br.shippable_files(root))
    return [f"{br.RELEASE_ROOT_NAME}/{rel}" for rel in shippable]


def test_archive_has_one_stable_root_and_exact_inventory(tmp_path):
    out = br.build_archive(ROOT, tmp_path)
    with tarfile.open(out["archive"], "r:gz") as tar:
        members = tar.getmembers()
    dirs = {m.name for m in members if m.isdir()}
    files = {m.name for m in members if m.isreg()}
    # Every directory lives under exactly one root named after the release.
    assert all(d.startswith(br.RELEASE_ROOT_NAME) for d in dirs)
    assert br.RELEASE_ROOT_NAME in dirs
    assert files == set(_expected_inventory(ROOT))


# ----------------------------------------------------------- determinism

def test_two_builds_are_byte_identical(tmp_path):
    first = br.build_archive(ROOT, tmp_path / "a")
    second = br.build_archive(ROOT, tmp_path / "b")
    assert first["sha256"] == second["sha256"]
    assert first["size_bytes"] == second["size_bytes"]
    assert (
        (tmp_path / "a" / first["filename"]).read_bytes()
        == (tmp_path / "b" / second["filename"]).read_bytes()
    )


def test_members_have_fixed_metadata(tmp_path):
    out = br.build_archive(ROOT, tmp_path)
    with tarfile.open(out["archive"], "r:gz") as tar:
        for member in tar.getmembers():
            assert member.mtime == br.FIXED_MTIME
            assert member.uid == 0 and member.gid == 0
            assert member.uname == "" and member.gname == ""
            if member.isdir():
                assert member.mode == 0o755
            else:
                assert member.mode == 0o644


# ------------------------------------------------------- safe extraction

def test_all_member_paths_are_safe_relative(tmp_path):
    out = br.build_archive(ROOT, tmp_path)
    with tarfile.open(out["archive"], "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            assert not name.startswith("/")
            parts = Path(name).parts
            assert ".." not in parts
            assert not any(part.startswith("\\") for part in parts)
            assert member.isreg() or member.isdir()
            assert not member.issym() and not member.islnk()


# ---------------------------------------------- internal-file exclusion

@pytest.mark.parametrize(
    "internal",
    [
        "AGENTS.md",
        ".hermes.md",
        "PRD.md",
        ".env.example",
        "docs/BUILDLOG.md",
        "docs/taskchecklist.json",
    ],
)
def test_internal_files_never_ship(tmp_path, internal):
    out = br.build_archive(ROOT, tmp_path)
    forbidden = f"{br.RELEASE_ROOT_NAME}/{internal}"
    assert forbidden not in _member_paths(out["archive"])


def test_no_internal_directories_or_caches_ship(tmp_path):
    out = br.build_archive(ROOT, tmp_path)
    names = _member_paths(out["archive"])
    banned = {"__pycache__", ".git", ".dfg", "spikes", ".pytest_cache",
              ".ruff_cache", "node_modules", ".venv", "venv", ".gitignore"}
    for name in names:
        parts = {part.lower() for part in Path(name).parts}
        assert not (parts & banned), name


def test_no_secrets_or_env_files_ship(tmp_path):
    out = br.build_archive(ROOT, tmp_path)
    for name in _member_paths(out["archive"]):
        base = name.rsplit("/", 1)[-1]
        assert base != ".env"
        assert not base.startswith(".env.")


# ------------------------------------------------ T009 review repair RED

def _make_fixture_root(tmp_path: Path) -> Path:
    """A tiny isolated stand-in for the project tree."""
    root = tmp_path / "proj"
    for rel in (
        "README.md",
        "__init__.py",
        "desktop/plugin.js",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture {rel}\n", encoding="utf-8")
    return root


def test_artifacts_directory_is_not_shippable(tmp_path):
    """Prior release outputs under artifacts/ never enter the shippable set."""
    import verify_topology as vt

    root = _make_fixture_root(tmp_path)
    prior = root / "artifacts" / "build-a"
    prior.mkdir(parents=True)
    (prior / "hermes-model-lab-0.1.0.tar.gz").write_bytes(b"fake old tarball")
    (root / "artifacts" / "scratch.txt").write_text("junk", encoding="utf-8")

    shippable = vt.shippable_files(root)
    assert not any(p.startswith("artifacts/") for p in shippable)

    out = br.build_archive(root, tmp_path / "out")
    for name in _member_paths(out["archive"]):
        assert "/artifacts/" not in f"/{name}", name


def test_repeated_builds_from_populated_tree_stay_clean_and_identical(tmp_path):
    """Build into project-root/artifacts, build again WITHOUT deleting the
    prior output, and prove the exact inventory has no artifacts paths and
    the bytes stay identical."""
    root = _make_fixture_root(tmp_path)

    first = br.build_archive(root, root / "artifacts" / "build-a")
    second = br.build_archive(root, root / "artifacts" / "build-b")
    # The second build ran against a tree already containing the first
    # archive; it must be byte-identical and contain no artifacts paths.
    assert second["sha256"] == first["sha256"]
    assert (first["archive"]).read_bytes() == (second["archive"]).read_bytes()
    assert not any("artifacts" in rel for rel in second["inventory"])
    expected = sorted(
        f"{br.RELEASE_ROOT_NAME}/{rel}"
        for rel in ("README.md", "__init__.py", "desktop/plugin.js")
    )
    assert sorted(second["inventory"]) == expected


def test_contributing_has_no_stale_planning_status_claim():
    """The shipped CONTRIBUTING.md must reflect release-ready status."""
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "planning status" not in text.lower()
    out = br.build_archive(ROOT, Path("/tmp/model-lab-contrib-check-t009"))
    try:
        import tarfile as _tf

        with _tf.open(out["archive"], "r:gz") as tar:
            shipped = tar.extractfile(
                f"{br.RELEASE_ROOT_NAME}/CONTRIBUTING.md"
            ).read().decode("utf-8")
        assert "planning status" not in shipped.lower()
    finally:
        out["archive"].unlink(missing_ok=True)
        out["archive"].parent.rmdir()


# ---------------------------------------- extracted product verification

def test_extracted_tree_passes_product_verification(tmp_path):
    out = br.build_archive(ROOT, tmp_path)
    report = br.verify_extracted(
        out["archive"],
        work_root=tmp_path / "verify-work",
        source_root=ROOT,
    )
    assert report["extracted_file_count"] == len(_expected_inventory(ROOT))
    assert report["no_overwrite_install_ok"] is True
    assert report["parity_ok"] is True
    assert report["doctor_ok"] is True
    assert report["node_syntax_ok"] is True
    assert report["route_inventory_ok"] is True
    assert report["bare_router_health_ok"] is True
    assert report["cleanup_complete"] is True
