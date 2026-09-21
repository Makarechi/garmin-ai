import secrets
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from garmin_ai.access import permits, permits_tool
from garmin_ai.calendar_context import CalendarBatch
from garmin_ai.config import Settings
from garmin_ai.db import SCHEMA_REVISION, MaintenanceMode, make_engine, transaction
from garmin_ai.definitions import (
    CustomEntryInput,
    DefinitionActivation,
    DefinitionRevision,
    DefinitionSpec,
    activate_definition,
    create_custom_event,
    create_definition_draft,
    definition_state,
    ensure_system_definitions,
    list_definitions,
    propose_definition_revision,
    retire_definition,
    update_custom_event,
    version_state,
)
from garmin_ai.events import (
    Conflict,
    EventInput,
    create_event,
    delete_event,
    event_query_allowed,
    serialize_event,
    update_event,
)
from garmin_ai.hypotheses import HypothesisSpec
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import Event
from garmin_ai.natural_language import NaturalLanguageRequest, process_tracker_text
from garmin_ai.onboarding import OnboardingPlan, apply_onboarding, onboarding_status
from garmin_ai.personal_goals import GoalSelection, preferences, select_goals
from garmin_ai.scenario_packs import (
    PackSelection,
    configure_scenario_pack,
    ensure_scenario_packs,
    list_scenario_packs,
)
from garmin_ai.tools import TOOLS, ReplayUnavailable, call_tool
from garmin_ai.tracker_forms import (
    FormSubmission,
    FormValidationError,
    TrackerConfirmation,
    TrackerSetupDraft,
    action_for_event,
    available_actions,
    confirm_tracker,
    form_for_action,
    preview_tracker,
    submit_form,
)
from garmin_ai.wearable import WearableBatch, accept_batch


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict = Field(default_factory=dict)


class EditRequest(BaseModel):
    revision: int = Field(ge=1)
    event: EventInput


class CustomEditRequest(BaseModel):
    revision: int = Field(ge=1)
    entry: CustomEntryInput


def create_app(settings: Settings | None = None, engine=None):
    settings = settings or Settings()
    engine = engine or make_engine(settings)
    from garmin_ai.accounts import AccountError, apply_instance_settings

    settings_initialized = False
    try:
        with transaction(engine) as session:
            apply_instance_settings(session, settings)
            ensure_system_definitions(session, backfill=True)
            ensure_system_metric_definitions(session, backfill=True)
            from garmin_ai.canonical_events import backfill_canonical_events

            backfill_canonical_events(session)
            ensure_scenario_packs(session)
        settings_initialized = True
    except (AccountError, MaintenanceMode, SQLAlchemyError):
        # Liveness and readiness remain available while storage is fenced or awaiting migration.
        pass
    app = FastAPI(title="Garmin AI", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.settings = settings
    app.state.settings_initialized = settings_initialized
    from garmin_ai.dashboard import install_dashboard

    install_dashboard(app)

    def initialize_session(session):
        # Keep identity validation under the same storage lock and transaction as the request.
        # A restore or erase/resume cycle therefore cannot be inserted between validation and
        # the actual database access.
        apply_instance_settings(session, settings)
        ensure_system_definitions(session, backfill=True)
        ensure_system_metric_definitions(session, backfill=True)
        from garmin_ai.canonical_events import backfill_canonical_events

        backfill_canonical_events(session)
        ensure_scenario_packs(session)
        app.state.settings_initialized = True

    def authorize(authorization: str | None = Header(default=None)):
        candidates = [(settings.api_key.get_secret_value(), {"admin"})] + [
            (token.key.get_secret_value(), token.scopes) for token in settings.api_tokens
        ]
        granted = None
        for key, scopes in candidates:
            if (
                len(key) >= 32
                and not key.startswith("replace-with-")
                and secrets.compare_digest(
                    (authorization or "").encode("utf-8"), ("Bearer " + key).encode("utf-8")
                )
            ):
                granted = frozenset(scopes)
        if granted is None:
            raise HTTPException(401, "Authentication required")
        return granted

    def wearable_identity(authorization: str | None = Header(default=None)):
        for token in settings.api_tokens:
            if token.scopes == {"write:wearable"} and secrets.compare_digest(
                (authorization or "").encode("utf-8"),
                ("Bearer " + token.key.get_secret_value()).encode("utf-8"),
            ):
                return token.wearable_device_id
        raise HTTPException(401, "Wearable authentication required")

    def require(*required):
        def check(granted=Depends(authorize)):
            if not permits(granted, set(required)):
                raise HTTPException(403, "Insufficient scope")

        return check

    def db():
        try:
            with transaction(engine) as session:
                initialize_session(session)
                session.info["timezone"] = settings.timezone
                yield session
        except (AccountError, MaintenanceMode, SQLAlchemyError):
            raise HTTPException(503, "Database unavailable or identity is not ready") from None

    @app.exception_handler(MaintenanceMode)
    async def maintenance_handler(request: Request, exc: MaintenanceMode):
        return JSONResponse(status_code=503, content={"detail": "Storage disabled after erasure"})

    @app.exception_handler(Conflict)
    async def conflict_handler(request: Request, exc: Conflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(LookupError)
    async def missing_handler(request: Request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": "Record not found"})

    @app.exception_handler(FormValidationError)
    async def form_validation_handler(request: Request, exc: FormValidationError):
        return JSONResponse(
            status_code=422,
            content={"detail": "Form validation failed", "errors": exc.errors},
        )

    @app.exception_handler(ValueError)
    async def invalid_handler(request: Request, exc: ValueError):
        # Validation exceptions may contain the original personal message.
        return JSONResponse(status_code=422, content={"detail": "Invalid arguments"})

    @app.exception_handler(PermissionError)
    async def permission_handler(request: Request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"detail": "Operation not allowed"})

    @app.exception_handler(ReplayUnavailable)
    async def replay_handler(request: Request, exc: ReplayUnavailable):
        return JSONResponse(
            status_code=503, content={"detail": str(exc)}, headers={"Retry-After": "60"}
        )

    @app.post("/context/calendar/import", dependencies=[Depends(require("admin"))])
    def import_calendar(request: CalendarBatch, session=Depends(db)):
        from garmin_ai.calendar_context import import_batch

        return import_batch(session, settings, request)

    @app.get("/context/calendar", dependencies=[Depends(require("admin"))])
    def calendar_plans(start: AwareDatetime, end: AwareDatetime, session=Depends(db)):
        from garmin_ai.calendar_context import plans

        return plans(session, settings, start, end)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)
    ):
        from garmin_ai.telegram import save_update

        secret = settings.telegram_webhook_secret.get_secret_value()
        if (
            len(secret) < 16
            or not x_telegram_bot_api_secret_token
            or not secrets.compare_digest(
                secret.encode("utf-8"), x_telegram_bot_api_secret_token.encode("utf-8")
            )
        ):
            raise HTTPException(403, "Invalid webhook secret")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise HTTPException(413, "Update too large")
        import json

        update = json.loads(body)
        try:
            with transaction(engine) as session:
                initialize_session(session)
                if not session.scalar(text("SELECT pg_try_advisory_xact_lock(72104623)")):
                    raise HTTPException(503, "Telegram ingestion busy; retry delivery")
                accepted = save_update(
                    session,
                    update,
                    settings.telegram_user_id,
                    dispatcher_version=settings.telegram_dispatcher_version,
                )
        except (AccountError, MaintenanceMode, SQLAlchemyError):
            raise HTTPException(503, "Database unavailable or identity is not ready") from None
        return {"ok": True, "accepted": accepted}

    @app.get("/health/ready")
    def ready():
        try:
            with transaction(engine) as session:
                revision = session.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != SCHEMA_REVISION:
                    raise HTTPException(503, "Database migration required")
                initialize_session(session)
            return {"status": "ready"}
        except MaintenanceMode:
            raise HTTPException(503, "Storage disabled after erasure") from None
        except (AccountError, SQLAlchemyError):
            raise HTTPException(503, "Database unavailable or not migrated") from None

    @app.get("/metrics", dependencies=[Depends(require("admin"))], response_class=PlainTextResponse)
    def metrics(session=Depends(db)):
        from garmin_ai.observability import prometheus

        return prometheus(session)

    @app.get("/operations", dependencies=[Depends(require("admin"))])
    def operations(session=Depends(db)):
        from garmin_ai.observability import snapshot

        return snapshot(session)

    @app.get("/tools", dependencies=[Depends(authorize)])
    def list_tools(granted=Depends(authorize)):
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.arguments.model_json_schema(),
            }
            for t in TOOLS.values()
            if permits_tool(granted, t.name)
        ]

    @app.get("/scenario-packs", dependencies=[Depends(require("read:diary"))])
    def scenario_packs(session=Depends(db)):
        return {"packs": list_scenario_packs(session)}

    @app.put(
        "/scenario-packs/{key}",
        dependencies=[Depends(require("read:diary", "write:diary"))],
    )
    def update_scenario_pack(key: str, body: PackSelection, session=Depends(db)):
        return configure_scenario_pack(session, key, body)

    @app.get("/onboarding", dependencies=[Depends(require("admin"))])
    def get_onboarding(session=Depends(db)):
        return onboarding_status(session, settings)

    @app.put("/onboarding", dependencies=[Depends(require("admin"))])
    def update_onboarding(body: OnboardingPlan, session=Depends(db)):
        return apply_onboarding(session, body)

    @app.post("/tracker-setups/preview", dependencies=[Depends(require("manage:definitions"))])
    def preview_tracker_setup(body: TrackerSetupDraft, session=Depends(db)):
        return preview_tracker(session, body)

    @app.post("/tracker-setups", dependencies=[Depends(require("manage:definitions"))])
    def create_tracker(body: TrackerConfirmation, session=Depends(db)):
        return confirm_tracker(session, body, actor="api")

    @app.get("/actions", dependencies=[Depends(require("read:diary"))])
    def actions(
        locale: str = Query(default="en", pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$"),
        session=Depends(db),
    ):
        return {
            "actions": [
                row.model_dump(mode="json") for row in available_actions(session, locale=locale)
            ]
        }

    @app.get("/actions/events/{event_id}", dependencies=[Depends(require("read:diary"))])
    def event_action(
        event_id: UUID,
        locale: str = Query(default="en", pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$"),
        session=Depends(db),
    ):
        return action_for_event(session, event_id, locale=locale).model_dump(mode="json")

    @app.get("/forms/{action_id}", dependencies=[Depends(require("read:diary"))])
    def generated_form(
        action_id: str,
        locale: str = Query(default="en", pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$"),
        session=Depends(db),
    ):
        return form_for_action(session, action_id, locale=locale).model_dump(mode="json")

    @app.post(
        "/forms/{action_id}/submit",
        dependencies=[Depends(require("read:diary", "write:diary"))],
    )
    def submit_generated_form(action_id: str, body: FormSubmission, session=Depends(db)):
        return serialize_event(submit_form(session, action_id, body, actor="api"))

    @app.post("/natural-language/trackers", dependencies=[Depends(authorize)])
    def natural_language_tracker(
        body: NaturalLanguageRequest,
        session=Depends(db),
        granted=Depends(authorize),
    ):
        from garmin_ai.integrations import configured_instance
        from garmin_ai.llm import GeminiProvider, ProviderUnavailable
        from garmin_ai.provider_gate import ProviderGate

        provider = None
        model_instance = configured_instance(settings, "model", "gemini")
        if model_instance is not None or not settings.integrations:
            try:
                provider = GeminiProvider(
                    settings,
                    instance_id=(
                        model_instance.id if model_instance is not None else "model:gemini:primary"
                    ),
                )
                provider.request_gate = ProviderGate(engine, settings)
            except ProviderUnavailable:
                pass
        try:
            return process_tracker_text(
                session,
                provider,
                body,
                granted=granted,
                actor="api",
                now=datetime.now(UTC),
                timezone=settings.timezone,
                locale=settings.locale,
            )
        finally:
            if provider is not None:
                provider.close()

    @app.post("/tools/{name}", dependencies=[Depends(authorize)])
    def run_tool(name: str, body: ToolRequest, session=Depends(db), granted=Depends(authorize)):
        if not permits_tool(granted, name):
            raise HTTPException(403, "Insufficient scope")
        return call_tool(session, name, body.arguments)

    @app.get("/preferences/goals", dependencies=[Depends(require("read:diary"))])
    def get_goals(session=Depends(db)):
        return preferences(session)

    @app.put("/preferences/goals", dependencies=[Depends(require("read:diary", "write:diary"))])
    def put_goals(body: GoalSelection, session=Depends(db)):
        return select_goals(session, body)

    @app.get("/definitions", dependencies=[Depends(require("read:diary"))])
    def definitions(session=Depends(db)):
        return list_definitions(session)

    @app.post("/definitions", dependencies=[Depends(require("manage:definitions"))])
    def new_definition(body: DefinitionSpec, session=Depends(db)):
        return definition_state(
            create_definition_draft(session, body, actor="api", authorized=True)
        )

    @app.put("/definitions/{definition_id}", dependencies=[Depends(require("manage:definitions"))])
    def propose_definition(definition_id: UUID, body: DefinitionRevision, session=Depends(db)):
        return definition_state(
            propose_definition_revision(
                session,
                definition_id,
                body.revision,
                body.spec,
                actor="api",
                authorized=True,
            )
        )

    @app.post(
        "/definitions/{definition_id}/activate",
        dependencies=[Depends(require("manage:definitions"))],
    )
    def activate_user_definition(
        definition_id: UUID, body: DefinitionActivation, session=Depends(db)
    ):
        return version_state(
            activate_definition(session, definition_id, body.revision, actor="api", authorized=True)
        )

    @app.post(
        "/definitions/{definition_id}/retire",
        dependencies=[Depends(require("manage:definitions"))],
    )
    def retire_user_definition(
        definition_id: UUID, body: DefinitionActivation, session=Depends(db)
    ):
        return definition_state(
            retire_definition(session, definition_id, body.revision, authorized=True)
        )

    @app.get("/exports/diary", dependencies=[Depends(require("read:diary"))])
    def diary_export(
        start: AwareDatetime,
        end: AwareDatetime,
        timezone: str | None = None,
        format: Literal["json", "csv"] = "json",
        session=Depends(db),
    ):
        from garmin_ai.diary_export import as_csv, export_diary

        data = export_diary(session, start, end, timezone or settings.timezone)
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="garmin-diary.{format}"',
        }
        if format == "csv":
            return Response(as_csv(data), media_type="text/csv", headers=headers)
        return JSONResponse(data, headers=headers)

    @app.post("/wearable/marks")
    def wearable_marks(body: WearableBatch, device_id=Depends(wearable_identity)):
        # Commit before constructing the ACK response, not in dependency teardown.
        try:
            with transaction(engine) as session:
                initialize_session(session)
                result = accept_batch(session, device_id, body)
        except (AccountError, MaintenanceMode, SQLAlchemyError):
            raise HTTPException(503, "Database unavailable or identity is not ready") from None
        return result

    @app.post("/events", dependencies=[Depends(require("read:diary", "write:diary"))])
    def new_event(
        body: EventInput,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=200),
        session=Depends(db),
    ):
        return serialize_event(
            create_event(session, body, actor="api", idempotency_key=idempotency_key)
        )

    @app.post("/entries", dependencies=[Depends(require("read:diary", "write:diary"))])
    def new_custom_entry(
        body: CustomEntryInput,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=200),
        session=Depends(db),
    ):
        return serialize_event(
            create_custom_event(session, body, actor="api", idempotency_key=idempotency_key)
        )

    @app.put("/entries/{event_id}", dependencies=[Depends(require("read:diary", "write:diary"))])
    def edit_custom_entry(event_id: UUID, body: CustomEditRequest, session=Depends(db)):
        return serialize_event(
            update_custom_event(session, event_id, body.entry, revision=body.revision, actor="api")
        )

    @app.get("/events/{event_id}", dependencies=[Depends(require("read:diary"))])
    def get_event(event_id: UUID, session=Depends(db)):
        row = session.scalar(
            select(Event).where(
                Event.id == event_id,
                Event.deleted.is_(False),
                event_query_allowed(),
            )
        )
        if not row:
            raise LookupError("Event not found")
        return serialize_event(row)

    @app.put("/events/{event_id}", dependencies=[Depends(require("read:diary", "write:diary"))])
    def edit_event(event_id: UUID, body: EditRequest, session=Depends(db)):
        return serialize_event(
            update_event(session, event_id, body.event, revision=body.revision, actor="api")
        )

    @app.delete("/events/{event_id}", dependencies=[Depends(require("read:diary", "write:diary"))])
    def remove_event(event_id: UUID, revision: int = Query(ge=1), session=Depends(db)):
        return serialize_event(delete_event(session, event_id, revision=revision, actor="api"))

    @app.post(
        "/hypotheses", dependencies=[Depends(require("read:health", "read:diary", "write:diary"))]
    )
    def register_hypothesis(spec: HypothesisSpec, session=Depends(db)):
        from garmin_ai.hypotheses import register

        return register(session, spec)

    @app.get("/hypotheses/{identity}", dependencies=[Depends(require("read:health", "read:diary"))])
    def get_hypothesis(identity: UUID, response: Response, session=Depends(db)):
        from garmin_ai.hypotheses import fetch

        response.headers["Cache-Control"] = "no-store"
        return fetch(session, identity).value

    @app.post(
        "/hypotheses/{identity}/recheck",
        dependencies=[Depends(require("read:health", "read:diary", "write:diary"))],
    )
    def recheck_hypothesis(identity: UUID, session=Depends(db)):
        from garmin_ai.hypotheses import recheck

        return recheck(session, identity)

    @app.post(
        "/hypotheses/{identity}/stop",
        dependencies=[Depends(require("read:health", "read:diary", "write:diary"))],
    )
    def stop_hypothesis(identity: UUID, session=Depends(db)):
        from garmin_ai.hypotheses import stop

        return stop(session, identity)

    return app
