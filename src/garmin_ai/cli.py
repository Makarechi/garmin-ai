import argparse
import json
import logging
from datetime import date, datetime, timedelta
from getpass import getpass
from zoneinfo import ZoneInfo

from garminconnect import Garmin

from garmin_ai.archive import LocalArchive, atomic_private_write, private_directory
from garmin_ai.config import Settings
from garmin_ai.garmin import ENDPOINTS, GarminReader
from garmin_ai.probe import probe


def main():
    parser = argparse.ArgumentParser(description="Private Garmin health timeline")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Interactive Garmin login; secrets stay in your terminal")
    commands.add_parser("inventory", help="List supported read-only Garmin methods; no login")
    probe_parser = commands.add_parser("probe", help="Archive up to 31 days to inspect coverage")
    probe_parser.add_argument("--start", type=date.fromisoformat)
    probe_parser.add_argument("--end", type=date.fromisoformat)
    import_parser = commands.add_parser("import-probe", help="Import locally archived probe data")
    import_parser.add_argument("--report")
    args = parser.parse_args()
    # Upstream logs may contain identifying request parameters.
    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
    try:
        settings = Settings() if args.command != "inventory" else None
        if args.command == "inventory":
            for endpoint in ENDPOINTS:
                print(f"{endpoint.name}\t{endpoint.method}\t{endpoint.scope}")
            print("activities\tget_activities\tpage")
            print("activity_fit\tdownload_activity\tactivity")
        elif args.command == "login":
            token_dir = private_directory(settings.token_dir)
            client = Garmin(
                email=input("Garmin email: ").strip(),
                password=getpass("Garmin password: "),
                prompt_mfa=lambda: getpass("Garmin MFA code: ").strip(),
            )
            client.login()
            client.client.dump(str(token_dir.resolve()))
            print("Garmin login saved locally. Password is not stored by this application.")
        elif args.command == "probe":
            end = args.end or datetime.now(ZoneInfo(settings.timezone)).date()
            start = args.start or end - timedelta(days=13)
            if not 0 <= (end - start).days < 31:
                parser.error("Probe range must be 1–31 days")
            archive = LocalArchive(settings.data_dir / "raw")
            path = settings.data_dir / "coverage-report.json"
            result = probe(
                GarminReader.restore(settings.token_dir),
                archive,
                start,
                end,
                checkpoint=lambda report: atomic_private_write(
                    path, json.dumps(report, indent=2).encode()
                ),
            )
            atomic_private_write(path, json.dumps(result, indent=2).encode())
            print(f"Coverage report saved locally to {path}. Review before sharing.")
        elif args.command == "import-probe":
            from pathlib import Path

            from garmin_ai.db import make_engine
            from garmin_ai.sync import import_probe

            result = import_probe(
                make_engine(settings),
                LocalArchive(settings.data_dir / "raw"),
                settings,
                Path(args.report) if args.report else settings.data_dir / "coverage-report.json",
            )
            print(json.dumps(result))
            if result["errors"]:
                parser.exit(1, "Some archived data needs parser corrections.\n")
    except KeyboardInterrupt:
        parser.exit(130, "Cancelled.\n")
    except Exception as exc:
        parser.exit(1, f"Operation failed ({type(exc).__name__}). No sensitive details logged.\n")


if __name__ == "__main__":
    main()
