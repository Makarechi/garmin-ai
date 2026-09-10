"""Single-owner source binding. Fingerprints are identifiers, never credentials."""

import hashlib
import json
import re
import secrets
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import func, select, text

from garmin_ai.db import transaction, writer_guard
from garmin_ai.models import AppState, Base

BINDING_KEY = "account:garmin"


class AccountError(RuntimeError):
    pass


class AccountMismatch(AccountError):
    pass


class AccountEnrollmentRequired(AccountError):
    pass


def profile_fingerprint(profile):
    identity = profile.get("profileId") if isinstance(profile, dict) else None
    if isinstance(identity, bool) or not isinstance(identity, (int, str)):
        raise AccountError("Authenticated stable profile identity unavailable")
    text = str(identity)
    if len(text) > 19 or not text.isascii() or not text.isdecimal() or not 0 < int(text) < 2**63:
        raise AccountError("Authenticated stable profile identity unavailable")
    return hashlib.sha256(f"garmin:socialProfile:profileId:v1:{int(text)}".encode()).hexdigest()


def validate_fingerprint(fingerprint):
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise AccountError("Account fingerprint unavailable")


def check_retained_archive(archive_root, *, confirm_existing_owner=False):
    if archive_root is not None and archive_root.exists() and not confirm_existing_owner:
        if next(archive_root.rglob("*"), None) is not None:
            raise AccountEnrollmentRequired("Confirm ownership of retained raw storage locally")


def verify_file_probe(archive_root, report_path, fingerprint):
    validate_fingerprint(fingerprint)
    if next(archive_root.rglob("*"), None) is None:
        return
    try:
        report = json.loads(report_path.read_text())
        previous = report.get("account_fingerprint")
        validate_fingerprint(previous)
    except (OSError, ValueError, AttributeError, AccountError):
        raise AccountEnrollmentRequired(
            "Retained probe ownership requires local enrollment"
        ) from None
    if not secrets.compare_digest(previous, fingerprint):
        raise AccountMismatch("Retained probe belongs to another Garmin account")


def existing_account(session, fingerprint):
    validate_fingerprint(fingerprint)
    binding = session.get(AppState, BINDING_KEY, populate_existing=True)
    if binding:
        expected = binding.value.get("fingerprint")
        validate_fingerprint(expected)
        if not secrets.compare_digest(expected, fingerprint):
            raise AccountMismatch("Garmin account does not match this instance")
        return dict(binding.value)
    return None


def bind_account(session, fingerprint, *, confirm_existing_owner=False, archive_root=None):
    validate_fingerprint(fingerprint)
    writer_guard(session, enrollment=True)
    binding = existing_account(session, fingerprint)
    if binding is not None:
        return binding
    check_retained_archive(archive_root, confirm_existing_owner=confirm_existing_owner)
    # Operational jobs alone do not imply an established owner. Everything else,
    # including diary, audit and raw provenance, requires explicit legacy enrollment.
    populated = any(
        session.scalar(select(1).select_from(table).limit(1)) is not None
        for table in Base.metadata.sorted_tables
        if table.name not in {"app_state", "jobs"}
    )
    populated = (
        populated
        or session.scalar(
            select(AppState.key)
            .where(
                AppState.key.not_in(
                    {
                        "runtime:heartbeat",
                        "telegram:offset",
                        "proactive:enabled",
                        "proactive:generation",
                        "backup:last_success",
                    }
                ),
                ~AppState.key.startswith("outbox:auth:"),
                ~AppState.key.startswith("outbox:account-binding:"),
            )
            .limit(1)
        )
        is not None
    )
    if populated and not confirm_existing_owner:
        raise AccountEnrollmentRequired("Confirm the existing owner using local enrollment")
    value = {
        "instance_id": str(uuid4()),
        "fingerprint": fingerprint,
        "identity_contract": "socialProfile.profileId:v1",
    }
    session.add(AppState(key=BINDING_KEY, value=value))
    session.flush()
    return value


def ensure_account(
    engine, fingerprint, *, confirm_existing_owner=False, archive_root=None, before_commit=None
):
    with transaction(engine) as session:
        result = existing_account(session, fingerprint)
        if result is not None:
            if before_commit is not None:
                before_commit()
            return result
    # Release the shared transaction before acquiring the enrollment lock.
    # bind_account rechecks the binding under that exclusive lock.
    with transaction(engine, enrollment=True) as session:
        result = bind_account(
            session,
            fingerprint,
            confirm_existing_owner=confirm_existing_owner,
            archive_root=archive_root,
        )
        if before_commit is not None:
            before_commit()
        return result


def verify_setup_account(
    engine, fingerprint, *, confirm_existing_owner=False, archive_root=None, before_commit=None
):
    """For login/probe under standalone_files only; never authorizes canonical ingestion."""
    validate_fingerprint(fingerprint)
    with engine.connect() as connection:
        if connection.scalar(text("SELECT to_regclass('app_state')")) is None:
            check_retained_archive(archive_root, confirm_existing_owner=confirm_existing_owner)
            # An entirely unmigrated store has no owner to compare yet. A partial
            # schema is not evidence of an empty installation.
            if any(
                connection.scalar(select(func.to_regclass(table.name))) is not None
                for table in Base.metadata.sorted_tables
            ):
                raise AccountEnrollmentRequired("Migrate and verify the existing owner")
            if before_commit is not None:
                before_commit()
            return None
        if connection.scalar(text("SELECT 1 FROM app_state WHERE key='maintenance:erased'")):
            if before_commit is not None:
                before_commit()
            return None
    return ensure_account(
        engine,
        fingerprint,
        confirm_existing_owner=confirm_existing_owner,
        archive_root=archive_root,
        before_commit=before_commit,
    )


@contextmanager
def account_transaction(engine, fingerprint, *, archive_root=None):
    ensure_account(engine, fingerprint, archive_root=archive_root)
    with transaction(engine) as session:
        if existing_account(session, fingerprint) is None:
            raise AccountEnrollmentRequired("Owner binding disappeared; enroll locally")
        yield session
