from pathlib import Path
from runpy import run_path

collect = run_path(str(Path(__file__).parents[1] / "scripts" / "collect_python_notices.py"))[
    "collect"
]


def distribution(root, name, version, license_content=None):
    metadata_dir = root / f"{name}-{version}.dist-info"
    metadata_dir.mkdir()
    (metadata_dir / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nLicense-Expression: MIT\n"
    )
    paths = [f"{metadata_dir.name}/METADATA"]
    if license_content is not None:
        license_file = metadata_dir / "licenses" / "LICENSE"
        license_file.parent.mkdir()
        license_file.write_bytes(license_content)
        paths.append(f"{metadata_dir.name}/licenses/LICENSE")
    (metadata_dir / "RECORD").write_text("".join(f"{path},,\n" for path in paths))


def test_collect_python_notices_preserves_bundled_text_and_flags_missing(tmp_path):
    distribution(tmp_path, "alpha", "1.0", b"Exact license text\n")
    distribution(tmp_path, "beta", "2.0")

    notices, inventory = collect(tmp_path)

    assert b"alpha 1.0\nDeclared license: MIT" in notices
    assert b"Exact license text\n" in notices
    assert b"beta 2.0\nDeclared license: MIT" in notices
    assert b"REVIEW REQUIRED: no bundled text license file found" in notices
    assert inventory == [
        {
            "name": "alpha",
            "version": "1.0",
            "declared_license": "MIT",
            "license_files": ["alpha-1.0.dist-info/licenses/LICENSE"],
            "review_required": False,
        },
        {
            "name": "beta",
            "version": "2.0",
            "declared_license": "MIT",
            "license_files": [],
            "review_required": True,
        },
    ]
