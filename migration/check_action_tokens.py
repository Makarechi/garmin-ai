"""Exchange action tokens between the pinned Python and native database implementations."""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.action_tokens import (
    consume_action_token,
    issue_action_token,
    owner_action_signing_key,
)
from garmin_ai.models import Person

CONVERSATION = UUID(int=250)
REVISION = 17
EXPIRES = datetime.fromisoformat("2099-07-01T11:30:00.001000+05:30")
NOW = datetime.fromisoformat("2099-07-01T05:00:00+00:00")
CASES = ("confirm", "late", "rollback", "race", "corrupt")


def call(mode, path):
    if mode == "seed":
        engine = isolated_engine()
    else:
        url = make_url(os.environ["GA_TEST_DATABASE_URL"])
        if (
            url.get_backend_name() != "postgresql"
            or not (url.database or "").endswith("_test")
            or url.query
        ):
            raise ValueError("Expected a dedicated PostgreSQL fixture database")
        engine = create_engine(url, hide_parameters=True)

    try:
        with Session(engine) as session, session.begin():
            assert session.scalar(text("SELECT current_database()")).endswith("_test")
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == "c54f8a72d130"
            owner_id = session.scalar(select(Person.id))
            key = owner_action_signing_key(session, owner_id)
            context = {
                "owner_id": owner_id,
                "conversation_id": CONVERSATION,
                "revision": REVISION,
            }
            if mode == "seed":
                tokens = {
                    action: issue_action_token(
                        session, key, action_id=action, expires_at=EXPIRES, **context
                    )
                    for action in CASES
                }
                path.write_text(json.dumps(tokens), encoding="utf-8")
            else:
                tokens = json.loads(path.read_text(encoding="utf-8"))
                assert (
                    consume_action_token(session, key, tokens["rolled_back"], now=NOW, **context)
                    is None
                )
                assert (
                    consume_action_token(session, key, tokens["native"], now=NOW, **context)
                    == "native"
                )
                assert (
                    consume_action_token(session, key, tokens["native"], now=NOW, **context) is None
                )
                print(
                    "Pinned Python consumed the native action once and ignored the rolled-back issue"
                )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seed", "verify"))
    parser.add_argument("input_or_output", type=Path)
    args = parser.parse_args()
    call(args.mode, args.input_or_output)
