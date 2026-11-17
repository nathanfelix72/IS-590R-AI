"""
Dashboard + Slack HITL for lab6 support workflow.

Run:
  uvicorn app:app --reload --port 8000

Slack interactivity needs a public URL (ngrok) pointed at /slack/interactions.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

# Load before LangChain / LangSmith imports (same pattern as lab5).
LAB_DIR = Path(__file__).resolve().parent
load_dotenv(LAB_DIR / ".env")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from middlewareHITLMAS import SAMPLE_MESSAGES, langsmith_status
from run_manager import build_decision, manager
from slack_hitl import (
    handle_slack_interaction,
    notify_awaiting_approval,
    notify_run_finished,
    slack_enabled,
    verify_slack_signature,
)

STATIC_DIR = LAB_DIR / "static"

app = FastAPI(title="Lab6 HITL Dashboard", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

manager.set_hooks(
    on_awaiting_approval=notify_awaiting_approval,
    on_run_finished=notify_run_finished,
)

_tracing, _project = langsmith_status()
print(f"LangSmith tracing={'on' if _tracing else 'OFF'} project={_project}")


class StartRunRequest(BaseModel):
    label: str | None = None
    raw_message: str | None = None
    thread_id: str | None = None


class DecisionRequest(BaseModel):
    type: Literal["approve", "edit", "reject"]
    message: str | None = None
    amount: str | None = None
    edited_action: dict[str, Any] | None = None


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
async def health() -> dict[str, Any]:
    tracing, project = langsmith_status()
    return {
        "ok": True,
        "slack_enabled": slack_enabled(),
        "langsmith_tracing": tracing,
        "langsmith_project": project,
        "samples": list(SAMPLE_MESSAGES.keys()),
    }


@app.get("/api/samples")
async def samples() -> dict[str, str]:
    return SAMPLE_MESSAGES


@app.get("/api/runs")
async def list_runs() -> list[dict[str, Any]]:
    return manager.list_runs()


@app.delete("/api/runs")
async def clear_runs() -> dict[str, Any]:
    cleared = manager.clear_runs()
    return {"ok": True, "cleared": cleared}


@app.post("/api/runs/batch")
async def start_all_samples() -> list[dict[str, Any]]:
    runs = await manager.start_all_samples()
    return [run.to_dict() for run in runs]


@app.get("/api/runs/{thread_id}")
async def get_run(thread_id: str) -> dict[str, Any]:
    run = manager.get_run(thread_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run.to_dict()


@app.post("/api/runs")
async def start_run(body: StartRunRequest) -> dict[str, Any]:
    try:
        if body.label:
            run = await manager.start_sample(body.label)
        elif body.raw_message:
            label = body.thread_id or f"custom-{len(manager.runs) + 1}"
            run = await manager.start_run(
                label=label,
                raw_message=body.raw_message,
                thread_id=body.thread_id or label,
            )
        else:
            raise HTTPException(
                status_code=400,
                detail="Provide either 'label' (sample) or 'raw_message'",
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run.to_dict()


@app.post("/api/runs/{thread_id}/decision")
async def decide(thread_id: str, body: DecisionRequest) -> dict[str, Any]:
    run = manager.get_run(thread_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    amount = body.amount
    if body.edited_action and isinstance(body.edited_action.get("args"), dict):
        amount = body.edited_action["args"].get("amount", amount)
    try:
        decision = build_decision(
            body.type,
            amount=amount,
            message=body.message or "Rejected from dashboard",
            pending_actions=run.pending_actions,
        )
        if body.type == "approve":
            summary = "Approved from dashboard"
        elif body.type == "edit":
            summary = f"Edited amount -> `{amount}` from dashboard"
        else:
            summary = f"Rejected from dashboard: {body.message or 'Rejected from dashboard'}"
        manager.record_decision_actor(
            thread_id, actor="Dashboard", summary=summary
        )
        run = await manager.resume(thread_id, decision)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run.to_dict()


@app.get("/api/events")
async def event_stream(request: Request) -> StreamingResponse:
    queue = manager.subscribe()

    async def gen():
        # Snapshot so the UI can catch up immediately.
        snapshot = json.dumps({"type": "snapshot", "runs": manager.list_runs()})
        yield f"data: {snapshot}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(message, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/slack/interactions")
async def slack_interactions(request: Request) -> JSONResponse:
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")
    if not verify_slack_signature(body=body, timestamp=timestamp, signature=signature):
        print("Slack interaction rejected: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    print("Slack interaction request received", flush=True)
    result = await handle_slack_interaction(body)
    if result is None:
        return JSONResponse({"ok": True})
    return JSONResponse(result)


@app.post("/slack/events")
async def slack_events(request: Request) -> JSONResponse:
    """Optional Events API URL — handles url_verification challenges."""
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")
    if not verify_slack_signature(body=body, timestamp=timestamp, signature=signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")
    payload = await request.json()
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})
    return JSONResponse({"ok": True})
