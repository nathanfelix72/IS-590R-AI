"""In-memory run tracker + progress fan-out for the dashboard and Slack."""

from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from langgraph.types import Command

from middlewareHITLMAS import SAMPLE_MESSAGES, support_graph


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TicketRun:
    thread_id: str
    label: str
    raw_message: str
    status: str = "queued"  # queued | running | awaiting_approval | completed | error
    category: str = ""
    redacted_summary: str = ""
    refund_outcome: str = ""
    pending_actions: Any = None
    error: str | None = None
    decided_by: str | None = None  # e.g. Slack display / <@U…> / "Dashboard"
    decision_summary: str | None = None
    slack_channel: str | None = None
    slack_ts: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "label": self.label,
            "raw_message": self.raw_message,
            "status": self.status,
            "category": self.category,
            "redacted_summary": self.redacted_summary,
            "refund_outcome": self.refund_outcome,
            "pending_actions": self.pending_actions,
            "error": self.error,
            "decided_by": self.decided_by,
            "decision_summary": self.decision_summary,
            "slack_channel": self.slack_channel,
            "slack_ts": self.slack_ts,
            "events": list(self.events),
            "updated_at": self.updated_at,
        }


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, TicketRun] = {}
        self._subscribers: list[asyncio.Queue] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._on_awaiting_approval: Callable[[TicketRun], Any] | None = None
        self._on_run_finished: Callable[[TicketRun], Any] | None = None

    def set_hooks(
        self,
        *,
        on_awaiting_approval: Callable[[TicketRun], Any] | None = None,
        on_run_finished: Callable[[TicketRun], Any] | None = None,
    ) -> None:
        self._on_awaiting_approval = on_awaiting_approval
        self._on_run_finished = on_run_finished

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def list_runs(self) -> list[dict[str, Any]]:
        return [run.to_dict() for run in self.runs.values()]

    def get_run(self, thread_id: str) -> TicketRun | None:
        return self.runs.get(thread_id)

    def clear_runs(self) -> int:
        """Drop all ticket state (dashboard reset). Does not affect LangGraph disk."""
        count = len(self.runs)
        self.runs.clear()
        self._locks.clear()
        message = {"type": "snapshot", "runs": []}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass
        return count

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        if thread_id not in self._locks:
            self._locks[thread_id] = asyncio.Lock()
        return self._locks[thread_id]

    def _emit(self, run: TicketRun, event_type: str, **payload: Any) -> None:
        event = {
            "type": event_type,
            "thread_id": run.thread_id,
            "at": _utc_now(),
            **payload,
        }
        run.events.append(event)
        run.updated_at = event["at"]
        message = {"type": "run_update", "run": run.to_dict(), "event": event}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def _sync_fields_from_state(self, run: TicketRun, values: dict[str, Any]) -> None:
        run.redacted_summary = values.get("redacted_summary") or run.redacted_summary
        run.category = values.get("category") or run.category
        run.refund_outcome = values.get("refund_outcome") or run.refund_outcome

    def _extract_interrupt(self, config: dict[str, Any]) -> Any | None:
        snapshot = support_graph.get_state(config)
        interrupts = list(getattr(snapshot, "interrupts", ()) or ())
        if not interrupts:
            for task in snapshot.tasks:
                task_interrupts = getattr(task, "interrupts", None) or ()
                if task_interrupts:
                    interrupts.extend(task_interrupts)
        if not interrupts:
            return None
        value = interrupts[0].value
        if isinstance(value, dict):
            return value.get("action_requests", value)
        return value

    async def _maybe_await(self, result: Any) -> None:
        if asyncio.iscoroutine(result):
            await result

    async def start_sample(self, label: str) -> TicketRun:
        if label not in SAMPLE_MESSAGES:
            raise KeyError(f"Unknown sample label: {label}")
        # Unique thread ids so re-running a sample doesn't collide with old checkpoints.
        thread_id = f"{label}-{uuid.uuid4().hex[:8]}"
        return await self.start_run(
            label, SAMPLE_MESSAGES[label], thread_id=thread_id
        )

    async def start_all_samples(self) -> list[TicketRun]:
        """Queue every SAMPLE_MESSAGES ticket (each gets its own thread)."""
        runs: list[TicketRun] = []
        for label in SAMPLE_MESSAGES:
            runs.append(await self.start_sample(label))
        return runs

    async def start_run(
        self,
        label: str,
        raw_message: str,
        thread_id: str | None = None,
    ) -> TicketRun:
        tid = thread_id or f"{label}-{uuid.uuid4().hex[:8]}"
        if tid in self.runs and self.runs[tid].status in {"running", "awaiting_approval"}:
            raise RuntimeError(f"Run '{tid}' is already active")

        run = TicketRun(thread_id=tid, label=label, raw_message=raw_message)
        self.runs[tid] = run
        self._emit(run, "queued", message="Ticket queued")
        asyncio.create_task(self._execute(tid, initial=True))
        return run

    def record_decision_actor(
        self,
        thread_id: str,
        *,
        actor: str,
        summary: str,
    ) -> TicketRun:
        run = self.runs.get(thread_id)
        if run is None:
            raise KeyError(f"Unknown thread_id: {thread_id}")
        run.decided_by = actor
        run.decision_summary = summary
        self._emit(run, "decision_actor", decided_by=actor, summary=summary)
        return run

    async def resume(self, thread_id: str, decision: dict[str, Any]) -> TicketRun:
        run = self.runs.get(thread_id)
        if run is None:
            raise KeyError(f"Unknown thread_id: {thread_id}")
        if run.status != "awaiting_approval":
            raise RuntimeError(f"Run '{thread_id}' is not awaiting approval")
        run.pending_actions = None
        run.status = "running"
        self._emit(run, "decision", decision=decision, decided_by=run.decided_by)
        asyncio.create_task(self._execute(thread_id, initial=False, decision=decision))
        return run

    async def _execute(
        self,
        thread_id: str,
        *,
        initial: bool,
        decision: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock_for(thread_id):
            run = self.runs[thread_id]
            run.status = "running"
            run.error = None
            self._emit(run, "running", message="Graph started" if initial else "Resuming after decision")

            config = {"configurable": {"thread_id": thread_id}}
            if initial:
                input_value: Any = {
                    "raw_message": run.raw_message,
                    "redacted_summary": "",
                    "category": "",
                    "refund_outcome": "",
                }
            else:
                input_value = Command(resume={"decisions": [decision]})

            try:
                events = await asyncio.to_thread(
                    lambda: list(self._collect_stream(input_value, config))
                )
                for kind, payload, values in events:
                    self._sync_fields_from_state(run, values)
                    if kind == "node":
                        self._emit(
                            run,
                            "node_complete",
                            node=payload,
                            data=copy.deepcopy(values),
                        )
                    elif kind == "interrupt":
                        run.status = "awaiting_approval"
                        run.pending_actions = payload
                        self._emit(run, "awaiting_approval", actions=payload)
                        if self._on_awaiting_approval:
                            await self._maybe_await(self._on_awaiting_approval(run))
                        return
                    elif kind == "completed":
                        run.status = "completed"
                        run.pending_actions = None
                        self._emit(run, "completed", result=copy.deepcopy(values))
                        if self._on_run_finished:
                            await self._maybe_await(self._on_run_finished(run))
                        return
            except Exception as exc:  # noqa: BLE001 - surface to dashboard
                run.status = "error"
                run.error = str(exc)
                self._emit(run, "error", message=str(exc))
                if self._on_run_finished:
                    await self._maybe_await(self._on_run_finished(run))

    def _collect_stream(
        self, input_value: Any, config: dict[str, Any]
    ) -> list[tuple[str, Any, dict[str, Any]]]:
        collected: list[tuple[str, Any, dict[str, Any]]] = []
        last_values: dict[str, Any] = {}
        for update in support_graph.stream(input_value, config=config, stream_mode="updates"):
            for node_name, node_update in update.items():
                if isinstance(node_update, dict):
                    last_values = {**last_values, **node_update}
                    collected.append(("node", node_name, dict(last_values)))
        state_values = support_graph.get_state(config).values
        if state_values:
            last_values = dict(state_values)
        interrupt = self._extract_interrupt(config)
        if interrupt is not None:
            collected.append(("interrupt", interrupt, last_values))
        else:
            collected.append(("completed", None, last_values))
        return collected


def build_decision(
    decision_type: str,
    *,
    amount: str | None = None,
    message: str | None = None,
    pending_actions: Any = None,
) -> dict[str, Any]:
    """Build a HITL resume decision, preserving original tool args on edit."""
    if decision_type == "approve":
        return {"type": "approve"}
    if decision_type == "reject":
        return {"type": "reject", "message": message or "Rejected"}
    if decision_type != "edit":
        raise ValueError(f"Unknown decision type: {decision_type}")
    if not amount:
        raise ValueError("Edit requires amount")
    args: dict[str, Any] = {}
    if isinstance(pending_actions, list) and pending_actions:
        first = pending_actions[0]
        if isinstance(first, dict):
            args = dict(first.get("args") or {})
    args["amount"] = amount
    return {
        "type": "edit",
        "edited_action": {
            "name": "issue_refund",
            "args": args,
        },
    }


manager = RunManager()
