"""Compare native PostgreSQL schema transitions with the pinned Alembic revisions."""

import argparse
import os
import subprocess
from contextlib import ExitStack
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

BEFORE_REVISIONS = "b7c4e1a92d60"
BEFORE_FENCE = "c8f51d3a7e20"
CURRENT = "c54f8a72d130"

CATALOG = (
    """
    SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'
    ORDER BY tablename
    """,
    """
    SELECT class.relname, row_number() OVER (PARTITION BY class.relname ORDER BY col.attnum), col.attname,
           format_type(col.atttypid,col.atttypmod), col.attnotnull,
           pg_get_expr(defaults.adbin,defaults.adrelid)
    FROM pg_attribute col JOIN pg_class class ON class.oid=col.attrelid
    JOIN pg_namespace ns ON ns.oid=class.relnamespace
    LEFT JOIN pg_attrdef defaults ON defaults.adrelid=class.oid AND defaults.adnum=col.attnum
    WHERE ns.nspname='public' AND class.relkind IN ('r','p')
      AND col.attnum>0 AND NOT col.attisdropped
    ORDER BY class.relname,col.attnum
    """,
    """
    SELECT class.relname, cons.conname, pg_get_constraintdef(cons.oid)
    FROM pg_constraint cons JOIN pg_class class ON class.oid=cons.conrelid
    JOIN pg_namespace ns ON ns.oid=class.relnamespace
    WHERE ns.nspname='public'
    ORDER BY class.relname,cons.conname
    """,
    """
    SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public'
    ORDER BY tablename,indexname
    """,
    """
    SELECT class.relname, trigger.tgname, pg_get_triggerdef(trigger.oid)
    FROM pg_trigger trigger JOIN pg_class class ON class.oid=trigger.tgrelid
    JOIN pg_namespace ns ON ns.oid=class.relnamespace
    WHERE ns.nspname='public' AND NOT trigger.tgisinternal
    ORDER BY class.relname,trigger.tgname
    """,
    """
    SELECT hypertable_schema,hypertable_name,column_name,dimension_type,time_interval
    FROM timescaledb_information.dimensions
    ORDER BY hypertable_schema,hypertable_name,column_name
    """,
)


def engine_for(stack, url):
    assert url.get_backend_name() == "postgresql"
    assert url.database and url.database.endswith("_test") and not url.query
    engine = create_engine(url, hide_parameters=True)
    stack.callback(engine.dispose)
    assert not inspect(engine).get_table_names(schema="public")
    return engine


def upgrade(engine, revision):
    with engine.begin() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def compare_catalog(reference, native):
    with reference.connect() as left, native.connect() as right:
        for index, query in enumerate(CATALOG):
            assert left.execute(text(query)).all() == right.execute(text(query)).all(), index
        assert left.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT
        assert right.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT
        people = text("SELECT singleton,locale,timezone,units FROM people")
        assert left.execute(people).all() == right.execute(people).all()
        fields = """
        ts,metric,source,local_date,value,unit,metric_definition_version_id,
        source_ref,quality,details,ingested_at
        """
        for table in ("measurements", "measurement_revisions"):
            statement = text(f"SELECT {fields} FROM {table} ORDER BY ts, metric, source")
            assert left.execute(statement).all() == right.execute(statement).all()


def run_native(binary, engine, case):
    subprocess.run(
        [
            binary,
            "--ignored",
            "--exact",
            "migrates_only_a_supported_disposable_schema",
            "--nocapture",
        ],
        env={
            "GA_TEST_DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "GA_TEST_SCHEMA_CASE": case,
        },
        check=True,
        timeout=20,
    )


def seed_measurement(engine):
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO measurements(ts,metric,source,local_date,value,unit,quality,details,ingested_at)
            VALUES ('2026-10-21T00:11:22.333444Z','synthetic_metric','synthetic','2026-10-21',
                    17.5,'test','observed',jsonb_build_object('synthetic',true),'2026-10-22T00:01:02Z')
            """)
        )


def compare_case(binary, reference, native, case, revision):
    if revision:
        for target in [reference, native]:
            upgrade(target, BEFORE_REVISIONS)
            seed_measurement(target)
            if revision == BEFORE_FENCE:
                upgrade(target, BEFORE_FENCE)
    if case == "before_revisions":
        with native.begin() as connection:
            connection.execute(text("ALTER TABLE outbox_messages ADD COLUMN memory_fence jsonb"))
        run_native(binary, native, "rollback")
        with native.begin() as connection:
            connection.execute(text("ALTER TABLE outbox_messages DROP COLUMN memory_fence"))
    upgrade(reference, CURRENT)
    run_native(binary, native, case)
    compare_catalog(reference, native)
    if revision:
        with native.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM measurement_revisions")) == 1
    print(f"Matched Alembic schema and data for {case}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("name_prefix")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    opening = make_url(os.environ["GA_TEST_DATABASE_URL"])
    assert opening.database and opening.database.endswith("_test")
    with ExitStack() as stack:
        for category, revision in (
            ("fresh", None),
            ("before_revisions", BEFORE_REVISIONS),
            ("before_fence", BEFORE_FENCE),
        ):
            reference, native = [
                engine_for(
                    stack, opening.set(database=f"{args.name_prefix}_{category}_{kind}_test")
                )
                for kind in ("py", "rs")
            ]
            compare_case(binary, reference, native, category, revision)
        unknown = engine_for(stack, opening.set(database=f"{args.name_prefix}_unknown_test"))
        upgrade(unknown, CURRENT)
        with unknown.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num='future-fixture'"))
        run_native(binary, unknown, "unknown")
        with unknown.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "future-fixture"
            )
        occupied = engine_for(stack, opening.set(database=f"{args.name_prefix}_occupied_test"))
        with occupied.begin() as connection:
            connection.execute(text("CREATE TABLE unrelated_fixture(id integer PRIMARY KEY)"))
            connection.execute(text("INSERT INTO unrelated_fixture VALUES (17)"))
        run_native(binary, occupied, "occupied")
        with occupied.connect() as connection:
            assert connection.scalar(text("SELECT id FROM unrelated_fixture")) == 17
    print(
        "Native schema refused unknown or occupied targets and rolled back an interrupted upgrade"
    )


if __name__ == "__main__":
    main()
