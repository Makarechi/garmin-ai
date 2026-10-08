"""Collect bundled Python distribution license files from an installed environment.

This is an inventory for review, not a license compatibility decision. Base
image, operating-system packages, and the separate database image are outside
its scope.
"""

import argparse
import importlib.metadata as metadata
import json
import sysconfig
from pathlib import Path


def _license_path(path: Path) -> bool:
    parts = path.parts
    if any(part.endswith(".dist-info") for part in parts[:-1]):
        if "licenses" in parts:
            return True
        return parts[-1].upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))
    return False


def collect(site_packages: Path) -> tuple[bytes, list[dict]]:
    root = site_packages.resolve()
    distributions = sorted(
        metadata.distributions(path=[str(root)]),
        key=lambda dist: (dist.metadata["Name"].lower(), dist.version),
    )
    if not distributions:
        raise ValueError("No installed Python distributions found")
    sections = [
        b"Python distribution license files bundled with this application image\n",
        b"Generated from the installed environment. Review other image layers separately.\n",
    ]
    inventory = []
    for dist in distributions:
        name = dist.metadata["Name"]
        version = dist.version
        declared = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        files = []
        for entry in sorted(dist.files or (), key=str):
            relative = Path(str(entry))
            if not _license_path(relative):
                continue
            source = (root / relative).resolve()
            if not source.is_relative_to(root) or not source.is_file():
                continue
            content = source.read_bytes()
            if not content or b"\0" in content:
                continue
            files.append((str(relative), content))
        inventory.append(
            {
                "name": name,
                "version": version,
                "declared_license": declared,
                "license_files": [path for path, _ in files],
                "review_required": not bool(files),
            }
        )
        sections.extend(
            [
                b"\n" + b"=" * 80 + b"\n",
                f"{name} {version}\nDeclared license: {declared or 'not specified'}\n".encode(),
            ]
        )
        if not files:
            sections.append(b"REVIEW REQUIRED: no bundled text license file found.\n")
        for path, content in files:
            sections.extend(
                [
                    f"\n--- {path} ---\n".encode(),
                    content,
                    b"\n" if not content.endswith(b"\n") else b"",
                ]
            )
    return b"".join(sections), inventory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notices", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    args = parser.parse_args()
    text, inventory = collect(Path(sysconfig.get_paths()["purelib"]))
    args.notices.write_bytes(text)
    args.inventory.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    print(
        f"Collected {len(inventory)} distributions; {sum(row['review_required'] for row in inventory)} require review"
    )


if __name__ == "__main__":
    main()
