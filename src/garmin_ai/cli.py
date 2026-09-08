import argparse
import json
import logging
from datetime import date, datetime, timedelta
from getpass import getpass
from pathlib import Path
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
    commands.add_parser("mcp", help="Run local database MCP server over stdio")
    commands.add_parser("worker", help="Run Garmin synchronization and Telegram")
    commands.add_parser(
        "resume-storage", help="Re-enable an erased store after explicit local setup"
    )
    commands.add_parser("migrate", help="Upgrade the database schema")
    serve_parser = commands.add_parser("serve", help="Run the authenticated local HTTP API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8080)
    for name in ("backup", "export", "restore-db"):
        operation = commands.add_parser(name)
        operation.add_argument("path", type=Path)
    unpack = commands.add_parser("unpack-backup")
    unpack.add_argument("source", type=Path)
    unpack.add_argument("destination", type=Path)
    erase = commands.add_parser("erase-all")
    erase.add_argument("--confirm", required=True)
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
            from garmin_ai.db import exclusive_ingestion, make_engine

            engine = make_engine(settings)
            try:
                with exclusive_ingestion(engine):
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
            finally:
                engine.dispose()
        elif args.command == "mcp":
            from garmin_ai.mcp_server import main as mcp_main

            mcp_main()
        elif args.command == "worker":
            from garmin_ai.runtime import main as worker_main

            worker_main()
        elif args.command == "serve":
            import uvicorn

            from garmin_ai.api import create_app

            uvicorn.run(
                create_app(settings),
                host=args.host,
                port=args.port,
                access_log=False,
                log_level="warning",
            )
        elif args.command == "resume-storage":
            from sqlalchemy import text

            from garmin_ai.db import make_engine

            engine = make_engine(settings)
            try:
                with engine.begin() as conn:
                    conn.execute(text("SELECT pg_advisory_xact_lock(72104622)"))
                    conn.execute(text("DELETE FROM app_state WHERE key='maintenance:erased'"))
            finally:
                engine.dispose()
            print("Storage re-enabled.")
        elif args.command == "migrate":
            from alembic import command
            from alembic.config import Config

            config = Config()
            config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
            command.upgrade(config, "head")
            print("Database schema upgraded.")
        elif args.command in {"backup", "export", "restore-db", "unpack-backup", "erase-all"}:
            from garmin_ai import operations
            from garmin_ai.db import make_engine

            if args.command == "unpack-backup":
                operations.unpack_backup(settings, args.source, args.destination)
                print("Backup unpacked.")
                return
            engine = make_engine(settings)
            try:
                if args.command == "backup":
                    result = operations.create_backup(engine, settings, args.path)
                elif args.command == "export":
                    result = operations.export_database(engine, args.path)
                elif args.command == "restore-db":
                    result = operations.restore_database(engine, args.path)
                elif args.command == "erase-all":
                    result = operations.erase_all(engine, settings, args.confirm)
                else:
                    operations.unpack_backup(settings, args.source, args.destination)
                    result = {"unpacked": True}
                print(json.dumps(result))
            finally:
                engine.dispose()
        elif args.command == "import-probe":
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
