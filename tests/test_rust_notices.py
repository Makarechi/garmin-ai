from pathlib import Path
from runpy import run_path

import pytest

collect = run_path(str(Path(__file__).parents[1] / "scripts" / "collect_rust_notices.py"))[
    "collect"
]


def package(root, name, version, license_file=None):
    directory = root / f"{name}-{version}"
    directory.mkdir()
    manifest = directory / "Cargo.toml"
    manifest.touch()
    return {
        "id": f"{name}@{version}",
        "name": name,
        "version": version,
        "license": "MIT",
        "license_file": license_file,
        "manifest_path": str(manifest),
    }


def test_only_selected_dependencies_and_their_exact_license_bytes_are_collected(tmp_path):
    app = package(tmp_path, "ga-app", "0.1.0")
    before = package(tmp_path, "example", "1.0.0", "copyright-text")
    after = package(tmp_path, "example", "2.0.0", "missing-declared")
    unused = package(tmp_path, "unused", "1.0.0")
    first = Path(before["manifest_path"]).parent
    second = Path(after["manifest_path"]).parent
    (first / "copyright-text").write_bytes(b"Copyright notice\r\n")
    (first / "third-party").mkdir()
    (first / "third-party" / "NOTICE.txt").write_bytes(b"Vendored notice")
    (second / "LICENSE-MIT").write_bytes(b"Another license\n")
    (first / "LICENSE-external").symlink_to(second / "LICENSE-MIT")
    metadata = {"packages": [unused, after, before, app], "workspace_members": [app["id"]]}

    notices, inventory = collect(
        metadata,
        "ga-app v0.1.0 (/build/crates/ga-app)\nexample v2.0.0\nexample v1.0.0\nexample v2.0.0",
    )

    assert b"Copyright notice\r\n" in notices
    assert b"Vendored notice\n" in notices
    assert b"unused" not in notices
    assert b"REVIEW REQUIRED" in notices
    assert inventory == [
        {
            "name": "example",
            "version": "1.0.0",
            "declared_license": "MIT",
            "license_files": ["copyright-text", "third-party/NOTICE.txt"],
            "review_required": False,
        },
        {
            "name": "example",
            "version": "2.0.0",
            "declared_license": "MIT",
            "license_files": ["LICENSE-MIT"],
            "review_required": True,
        },
    ]


def test_unknown_tree_entry_cannot_be_silently_omitted(tmp_path):
    known = package(tmp_path, "known", "1.0.0")
    metadata = {"packages": [known], "workspace_members": []}

    with pytest.raises(ValueError, match="Expected one Cargo package for missing"):
        collect(metadata, "known v1.0.0\nmissing v1.0.0")
