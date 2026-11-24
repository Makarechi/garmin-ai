"""Run the frozen recovery fixture against a candidate's explicitly migrated schema.

The archived Python probe intentionally provisions its own historical Alembic head.
Keep that fixture unchanged, then upgrade only the native candidate's database at
the provisioning boundary, before native API, backup or restoration commands run.
Python backup creation continues to invoke the archived probe directly.
"""

import argparse
import os
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

FROZEN_PYTHON_COMMIT = "9376d081620740799362282ac9faf2f1a1baaffa"


def load_frozen_probe(reference):
    revision = subprocess.check_output(
        ["git", "-C", str(reference), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != FROZEN_PYTHON_COMMIT:
        raise ValueError("The off-host fixture must use the pinned Python reference")
    sys.path[:0] = [str(reference / "src"), str(reference / "migration")]
    path = reference / "tests/evals/offhost_restore_probe.py"
    spec = spec_from_file_location("native_offhost_reference_probe", path)
    if spec is None or spec.loader is None:
        raise ValueError("The frozen off-host probe could not be loaded")
    probe = module_from_spec(spec)
    spec.loader.exec_module(probe)
    return probe, path


def install_native_migration(probe, binary):
    provision = probe.engine_for_empty_test_database

    def provision_native_database():
        # The frozen function first validates localhost, the _test database name,
        # absence of tables, and the historical fixture's empty row inventory.
        engine = provision()
        try:
            subprocess.run(
                [binary, "migrate"],
                env={"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)},
                check=True,
                timeout=60,
            )
        except BaseException:
            engine.dispose()
            raise
        return engine

    probe.engine_for_empty_test_database = provision_native_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--native-binary", type=Path, required=True)
    args, probe_args = parser.parse_known_args()
    reference = args.reference.resolve(strict=True)
    binary = args.native_binary.resolve(strict=True)
    if not os.access(binary, os.X_OK):
        parser.error("The candidate native binary must be executable")
    probe, path = load_frozen_probe(reference)
    install_native_migration(probe, binary)
    sys.argv = [str(path), *probe_args, "--native-binary", str(binary)]
    probe.main()


if __name__ == "__main__":
    main()
