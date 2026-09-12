import secrets
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
from garmin_ai.events import (
    Conflict,
    EventInput,
    create_event,
    delete_event,
    serialize,
    update_event,
)
from garmin_ai.hypotheses import HypothesisSpec
from garmin_ai.models import Event
from garmin_ai.personal_goals import GoalSelection, preferences, select_goals
from garmin_ai.tools import TOOLS, call_tool
from garmin_ai.wearable import WearableBatch, accept_batch


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict = Field(default_factory=dict)


class EditRequest(BaseModel):
    revision: int = Field(ge=1)
    event: EventInput


def create_app(settings: Settings | None = None, engine=None):
    settings = settings or Settings()
    engine = engine or make_engine(settings)
    app = FastAPI(title="Garmin AI", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.settings = settings
    from garmin_ai.dashboard import install_dashboard

    install_dashboard(app)

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
        with transaction(engine) as session:
            session.info["timezone"] = settings.timezone
            yield session

    @app.exception_handler(MaintenanceMode)
    async def maintenance_handler(request: Request, exc: MaintenanceMode):
        return JSONResponse(status_code=503, content={"detail": "Storage disabled after erasure"})

    @app.exception_handler(Conflict)
    async def conflict_handler(request: Request, exc: Conflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(LookupError)
    async def missing_handler(request: Request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": "Record not found"})

    @app.exception_handler(ValueError)
    async def invalid_handler(request: Request, exc: ValueError):
        # Validation exceptions may contain the original personal message.
        return JSONResponse(status_code=422, content={"detail": "Invalid arguments"})

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
        with transaction(engine) as session:
            if not session.scalar(text("SELECT pg_try_advisory_xact_lock(72104623)")):
                raise HTTPException(503, "Telegram ingestion busy; retry delivery")
            accepted = save_update(session, update, settings.telegram_user_id)
        return {"ok": True, "accepted": accepted}

    @app.get("/health/ready")
    def ready():
        try:
            with engine.connect() as conn:
                revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != SCHEMA_REVISION:
                    raise HTTPException(503, "Database migration required")
                if conn.scalar(text("SELECT 1 FROM app_state WHERE key='maintenance:erased'")):
                    raise HTTPException(503, "Storage disabled after erasure")
            return {"status": "ready"}
        except SQLAlchemyError:
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
        with transaction(engine) as session:
            result = accept_batch(session, device_id, body)
        return result

    @app.post("/events", dependencies=[Depends(require("read:diary", "write:diary"))])
    def new_event(
        body: EventInput,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=200),
        session=Depends(db),
    ):
        return serialize(create_event(session, body, actor="api", idempotency_key=idempotency_key))

    @app.get("/events/{event_id}", dependencies=[Depends(require("read:diary"))])
    def get_event(event_id: UUID, session=Depends(db)):
        row = session.scalar(select(Event).where(Event.id == event_id, Event.deleted.is_(False)))
        if not row:
            raise LookupError("Event not found")
        return serialize(row)

    @app.put("/events/{event_id}", dependencies=[Depends(require("read:diary", "write:diary"))])
    def edit_event(event_id: UUID, body: EditRequest, session=Depends(db)):
        return serialize(
            update_event(session, event_id, body.event, revision=body.revision, actor="api")
        )

    @app.delete("/events/{event_id}", dependencies=[Depends(require("read:diary", "write:diary"))])
    def remove_event(event_id: UUID, revision: int = Query(ge=1), session=Depends(db)):
        return serialize(delete_event(session, event_id, revision=revision, actor="api"))

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
