"""Public dashboard shell; all personal data still uses authenticated API routes."""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

ASSETS = Path(__file__).with_name("static") / "dashboard"
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
}


def install_dashboard(app):
    @app.middleware("http")
    async def prevent_private_cache(request, call_next):
        response = await call_next(request)
        if request.headers.get("authorization"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return FileResponse(ASSETS / "index.html", headers=HEADERS)

    @app.get("/dashboard-assets/{asset}", include_in_schema=False)
    def dashboard_asset(asset: str):
        if asset not in {"app.js", "styles.css"}:
            raise HTTPException(404)
        return FileResponse(ASSETS / asset, headers=HEADERS)
