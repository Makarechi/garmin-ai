"""Collect the license files shipped with the selected Rust dependencies."""

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

LICENSE_NAME = re.compile(r"^(license|licence|copying|notice|copyright)([._-]|$)", re.I)


def selected_packages(metadata, tree):
    candidates = defaultdict(list)
    for package in metadata["packages"]:
        candidates[package["name"], package["version"]].append(package)
    workspace = set(metadata["workspace_members"])
    selected = {}
    for line in tree.splitlines():
        if not line.strip():
            continue
        name, version, *_ = line.split(maxsplit=2)
        key = name, version.removeprefix("v")
        matches = candidates[key]
        if len(matches) != 1:
            raise ValueError(f"Expected one Cargo package for {name} {version}")
        package = matches[0]
        if package["id"] not in workspace:
            selected[key] = package
    if not selected:
        raise ValueError("No third-party Rust dependencies found")
    return [selected[key] for key in sorted(selected)]


def bundled_files(package):
    root = Path(package["manifest_path"]).resolve().parent
    declared = package.get("license_file")
    referenced = (root / declared).resolve() if declared else None
    paths = {
        path.resolve()
        for path in root.rglob("*")
        if LICENSE_NAME.match(path.name)
        or any(part.casefold() == "licenses" for part in path.relative_to(root).parts[:-1])
    }
    if referenced is not None:
        paths.add(referenced)
    files = {}
    for path in sorted(paths):
        if not path.is_relative_to(root) or not path.is_file():
            continue
        content = path.read_bytes()
        if content and b"\0" not in content:
            files[path.relative_to(root).as_posix()] = content
    declared_missing = referenced is not None and referenced not in {root / path for path in files}
    return files, declared_missing


def load_supplements(directory):
    root = directory.resolve()
    supplements = {}
    for entry in json.loads((root / "manifest.json").read_text()):
        files = {}
        for relative, digest in sorted(entry["files"].items()):
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                raise ValueError("Supplemental notice escapes its source directory")
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError(f"Supplemental notice checksum differs: {relative}")
            files[f"upstream/{relative}"] = content
        for name, version in entry["packages"].items():
            key = name, version
            if key in supplements:
                raise ValueError(f"Duplicate supplemental notices for {name} {version}")
            supplements[key] = entry["source"], files
    return supplements


def collect(metadata, tree, supplements):
    sections = [
        b"Rust dependency license files bundled with this application image\n",
        b"Generated from the locked application dependency tree for the image platform.\n",
    ]
    inventory = []
    for package in selected_packages(metadata, tree):
        name, version = package["name"], package["version"]
        declared = package.get("license") or ""
        files, declared_missing = bundled_files(package)
        upstream = {}
        if supplement := supplements.get((name, version)):
            source, supplemental_files = supplement
            files.update(supplemental_files)
            upstream["upstream_source"] = source
        review_required = not files or declared_missing
        inventory.append(
            {
                "name": name,
                "version": version,
                "declared_license": declared,
                "license_files": list(files),
                "review_required": review_required,
                **upstream,
            }
        )
        sections.extend(
            [
                b"\n" + b"=" * 80 + b"\n",
                f"{name} {version}\nDeclared license: {declared or 'not specified'}\n".encode(),
            ]
        )
        if upstream:
            sections.append(f"Upstream notices: {upstream['upstream_source']}\n".encode())
        if review_required:
            sections.append(b"REVIEW REQUIRED: one or more license texts are missing.\n")
        for path, content in files.items():
            sections.extend(
                [
                    f"\n--- {path} ---\n".encode(),
                    content,
                    b"\n" if not content.endswith(b"\n") else b"",
                ]
            )
    return b"".join(sections), inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--supplements", type=Path, required=True)
    parser.add_argument("--notices", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args()
    notices, inventory = collect(
        json.loads(args.metadata.read_text()),
        args.tree.read_text(),
        load_supplements(args.supplements),
    )
    args.notices.write_bytes(notices)
    args.inventory.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
