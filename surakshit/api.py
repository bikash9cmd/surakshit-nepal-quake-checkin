"""Local HTTP API (FastAPI) with the same routes as the AWS deployment.

On AWS, POST /checkin never touches a Lambda: API Gateway writes the body straight to SQS and
returns 202 (infra/template.yaml, CheckInRoute). Here a background task plays the writer.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from surakshit import reads
from surakshit.local import LocalSystem

WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


def create_app(system: LocalSystem | None = None) -> FastAPI:
    sys_ = system or LocalSystem()
    app = FastAPI(title="Surakshit", description="Earthquake check-ins for Nepal (local mode)")

    def cached(status: int, body: dict, kind: str) -> JSONResponse:
        return JSONResponse(body, status_code=status,
                            headers={"Cache-Control": f"public, max-age={reads.CACHE_SECONDS[kind]}"})

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB)

    @app.post("/checkin", status_code=202)
    async def checkin(request: Request, tasks: BackgroundTasks):
        body = (await request.body()).decode("utf-8", "replace")
        rid = sys_.submit_web(body)
        tasks.add_task(sys_.drain)
        return {"accepted": True, "id": rid}

    @app.get("/status")
    def status(phone: str):
        return cached(*reads.status_view(sys_.store, sys_.pepper, phone), "status")

    @app.get("/events")
    def events():
        return cached(*reads.events_view(sys_.store), "events")

    @app.get("/summary/{event_id}")
    def summary(event_id: str):
        return cached(*reads.summary_view(sys_.store, event_id), "summary")

    @app.post("/sms")
    def sms(payload: dict):
        """Simulates an inbound SMS. Returns the replies that would be sent back."""
        sender = str(payload.get("from", ""))
        sys_.submit_sms(sender, str(payload.get("text", "")))
        before = len(sys_.outbox)
        sys_.drain()
        return {"outbound": [o.__dict__ for o in sys_.outbox[before:]]}

    @app.get("/health")
    def health():
        return {"ok": True, "queued": len(sys_.queue), "events": len(sys_.store.events)}

    app.state.system = sys_
    return app
