import secrets
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from garmin_ai.config import Settings
from garmin_ai.db import MaintenanceMode, make_engine, transaction
from garmin_ai.events import (
    Conflict,
    EventInput,
    create_event,
    delete_event,
    serialize,
    update_event,
)
from garmin_ai.models import Event
from garmin_ai.tools import TOOLS, call_tool


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

    def authorize(authorization: str | None = Header(default=None)):
        key = settings.api_key.get_secret_value()
        if (
            len(key) < 32
            or not authorization
            or not secrets.compare_digest(
                authorization.encode("utf-8"), ("Bearer " + key).encode("utf-8")
            )
        ):
            raise HTTPException(401, "Authentication required")

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
            or not secrets.compare_digest(secret, x_telegram_bot_api_secret_token)
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
            accepted = save_update(session, update, settings.telegram_user_id)
        return {"ok": True, "accepted": accepted}

    @app.get("/health/ready")
    def ready():
        try:
            with engine.connect() as conn:
                revision = conn.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != "bfccd06bf1c6":
                    raise HTTPException(503, "Database migration required")
                if conn.scalar(text("SELECT 1 FROM app_state WHERE key='maintenance:erased'")):
                    raise HTTPException(503, "Storage disabled after erasure")
            return {"status": "ready"}
        except SQLAlchemyError:
            raise HTTPException(503, "Database unavailable or not migrated") from None

    @app.get("/metrics", dependencies=[Depends(authorize)], response_class=PlainTextResponse)
    def metrics(session=Depends(db)):
        from garmin_ai.observability import prometheus

        return prometheus(session)

    @app.get("/operations", dependencies=[Depends(authorize)])
    def operations(session=Depends(db)):
        from garmin_ai.observability import snapshot

        return snapshot(session)

    @app.get("/tools", dependencies=[Depends(authorize)])
    def list_tools():
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.arguments.model_json_schema(),
            }
            for t in TOOLS.values()
        ]

    @app.post("/tools/{name}", dependencies=[Depends(authorize)])
    def run_tool(name: str, body: ToolRequest, session=Depends(db)):
        return call_tool(session, name, body.arguments)

    @app.post("/events", dependencies=[Depends(authorize)])
    def new_event(
        body: EventInput,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=200),
        session=Depends(db),
    ):
        return serialize(create_event(session, body, actor="api", idempotency_key=idempotency_key))

    @app.get("/events/{event_id}", dependencies=[Depends(authorize)])
    def get_event(event_id: UUID, session=Depends(db)):
        row = session.scalar(select(Event).where(Event.id == event_id, Event.deleted.is_(False)))
        if not row:
            raise LookupError("Event not found")
        return serialize(row)

    @app.put("/events/{event_id}", dependencies=[Depends(authorize)])
    def edit_event(event_id: UUID, body: EditRequest, session=Depends(db)):
        return serialize(
            update_event(session, event_id, body.event, revision=body.revision, actor="api")
        )

    @app.delete("/events/{event_id}", dependencies=[Depends(authorize)])
    def remove_event(event_id: UUID, revision: int, session=Depends(db)):
        return serialize(delete_event(session, event_id, revision=revision, actor="api"))

    return app
