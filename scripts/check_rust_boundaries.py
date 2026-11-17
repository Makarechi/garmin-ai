"""Verify the Rust domain's production dependency graph from Cargo metadata."""

import json
import sys

DOMAIN_DEPENDENCIES = {"jiter", "serde_json", "sha2", "zmij"}
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"


def production_dependencies(node):
    for dependency in node["deps"]:
        if any(kind["kind"] != "dev" for kind in dependency["dep_kinds"]):
            yield dependency["pkg"]


def check(metadata):
    packages = {package["id"]: package for package in metadata["packages"]}
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    (domain,) = [
        package_id
        for package_id in metadata["workspace_members"]
        if packages[package_id]["name"] == "ga-domain"
    ]
    direct = set(production_dependencies(nodes[domain]))
    for package_id in direct:
        package = packages[package_id]
        if package["name"] not in DOMAIN_DEPENDENCIES:
            raise ValueError(f"Unapproved domain dependency: {package['name']}")

    pending = list(direct)
    visited = set()
    while pending:
        package_id = pending.pop()
        if package_id in visited:
            continue
        visited.add(package_id)
        package = packages[package_id]
        if package["source"] != CRATES_IO:
            raise ValueError(f"Domain reaches a non-registry package: {package['name']}")
        node = nodes[package_id]
        if package["name"] == "jiter" and "python" in node["features"]:
            raise ValueError("The domain JSON parser must build without Python integration")
        pending.extend(production_dependencies(node))
    print("The Rust domain dependency graph meets the boundary rules.")


if __name__ == "__main__":
    try:
        check(json.load(sys.stdin))
    except ValueError as error:
        sys.exit(str(error))
