"""Slack notifications + interactive Approve / Edit / Reject for HITL interrupts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import parse_qs

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from run_manager import TicketRun, build_decision, manager


def _log(message: str) -> None:
    """Console-safe logging (Windows cp1252 can't print arrows / some unicode)."""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", errors="replace").decode("ascii"), flush=True)


def slack_enabled() -> bool:
    return bool(os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_CHANNEL_ID"))


def _client() -> WebClient | None:
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        return None
    return WebClient(token=token)


def verify_slack_signature(
    *,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
) -> bool:
    signing_secret = os.getenv("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        # Dev convenience: allow unsigned payloads only when secret is unset.
        return True
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > 60 * 5:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        basestring,
        hashlib.sha256,
    ).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def _format_actions(actions: Any) -> str:
    try:
        return json.dumps(actions, indent=2, default=str)
    except TypeError:
        return str(actions)


def _approval_blocks(run: TicketRun) -> list[dict[str, Any]]:
    summary = run.redacted_summary or "(summary not ready yet)"
    actions_text = _format_actions(run.pending_actions)
    if len(actions_text) > 2500:
        actions_text = actions_text[:2500] + "\n... (truncated)"
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Refund needs approval", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Ticket*\n`{run.label}`"},
                {"type": "mrkdwn", "text": f"*Category*\n`{run.category or '—'}`"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Redacted summary*\n{summary}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Pending tool call*\n```{actions_text}```",
            },
        },
        {
            "type": "actions",
            "block_id": f"hitl:{run.thread_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "hitl_approve",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "value": run.thread_id,
                },
                {
                    "type": "button",
                    "action_id": "hitl_edit",
                    "text": {"type": "plain_text", "text": "Edit amount", "emoji": True},
                    "value": run.thread_id,
                },
                {
                    "type": "button",
                    "action_id": "hitl_reject",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                    "value": run.thread_id,
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"thread_id `{run.thread_id}` · approve here or in the dashboard",
                }
            ],
        },
    ]


def _slack_user_label(user: dict[str, Any] | None) -> str:
    """
    Label for the person who clicked.
    Includes <@U…> so Slack renders a mention; also keeps username for the dashboard.
    """
    if not user:
        return "someone in Slack"
    user_id = user.get("id")
    name = (
        user.get("username")
        or user.get("name")
        or user.get("real_name")
    )
    if user_id and name:
        return f"{name} (<@{user_id}>)"
    if user_id:
        return f"<@{user_id}>"
    return name or "someone in Slack"


def _decision_text(run: TicketRun, decision_summary: str | None = None) -> str | None:
    """Resolve decision text from explicit arg, run fields, or event history."""
    if decision_summary:
        return decision_summary
    if run.decision_summary:
        return run.decision_summary
    if run.decided_by:
        return f"Decided by {run.decided_by}"
    # Fallback: recover from events if fields were lost across a reload race.
    for event in reversed(run.events or []):
        if event.get("type") == "decision_actor" and event.get("summary"):
            if not run.decided_by and event.get("decided_by"):
                run.decided_by = event["decided_by"]
            if not run.decision_summary:
                run.decision_summary = event["summary"]
            return event["summary"]
    return None


def _result_blocks(run: TicketRun, decision_summary: str | None = None) -> list[dict[str, Any]]:
    outcome = run.refund_outcome or "(no refund action / skipped)"
    summary = _decision_text(run, decision_summary)
    status_line = {
        "completed": ":white_check_mark: Completed",
        "error": ":x: Error",
        "awaiting_approval": ":hourglass_flowing_sand: Awaiting approval",
        "running": ":gear: Running",
    }.get(run.status, run.status)
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Ticket update: {run.label}", "emoji": True},
        },
    ]
    # Decision first so it isn't easy to miss in the final message.
    if summary:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Decision*\n{summary}",
                },
            }
        )
    blocks.append(
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Status*\n{status_line}"},
                {"type": "mrkdwn", "text": f"*Category*\n`{run.category or '—'}`"},
            ],
        },
    )
    if run.redacted_summary:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Summary*\n{run.redacted_summary}"},
            }
        )
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Refund outcome*\n{outcome}"},
        }
    )
    if run.error:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Error*\n```{run.error}```"},
            }
        )
    return blocks


async def notify_awaiting_approval(run: TicketRun) -> None:
    if not slack_enabled():
        _log("Slack notify skipped: SLACK_BOT_TOKEN / SLACK_CHANNEL_ID not set")
        return
    client = _client()
    if client is None:
        return
    channel = os.getenv("SLACK_CHANNEL_ID", "")
    try:
        response = client.chat_postMessage(
            channel=channel,
            text=f"Refund approval needed for `{run.label}`",
            blocks=_approval_blocks(run),
        )
        run.slack_channel = response.get("channel", channel)
        run.slack_ts = response.get("ts")
        _log(f"Slack approval posted for {run.label} ts={run.slack_ts}")
    except SlackApiError as exc:
        err = exc.response.get("error", str(exc))
        run.error = f"Slack notify failed: {err}"
        _log(f"Slack notify failed for {run.label}: {err}")


async def notify_run_finished(run: TicketRun) -> None:
    if not slack_enabled():
        return
    client = _client()
    if client is None:
        return
    channel = run.slack_channel or os.getenv("SLACK_CHANNEL_ID", "")
    who = _decision_text(run)
    text = (
        f"Ticket `{run.label}` -> {run.status}"
        + (f" | {who}" if who else "")
    )
    _log(
        f"Slack finish notify {run.label} status={run.status} "
        f"decided_by={run.decided_by!r} summary={run.decision_summary!r} who={who!r}"
    )
    try:
        if run.slack_ts:
            client.chat_update(
                channel=channel,
                ts=run.slack_ts,
                text=text,
                blocks=_result_blocks(run, decision_summary=who),
            )
            _log(f"Slack message updated for {run.label} -> {run.status}")
        else:
            response = client.chat_postMessage(
                channel=channel,
                text=text,
                blocks=_result_blocks(run),
            )
            run.slack_channel = response.get("channel", channel)
            run.slack_ts = response.get("ts")
            _log(f"Slack completion posted for {run.label} ts={run.slack_ts}")
    except SlackApiError as exc:
        err = exc.response.get("error", str(exc))
        _log(f"Slack finish notify failed for {run.label}: {err}")
        if not run.error:
            run.error = f"Slack notify failed: {err}"


def _edit_modal(thread_id: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "hitl_edit_modal",
        "private_metadata": thread_id,
        "title": {"type": "plain_text", "text": "Edit refund"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "amount_block",
                "label": {"type": "plain_text", "text": "Corrected amount"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "amount",
                    "placeholder": {"type": "plain_text", "text": "e.g. $49.99"},
                },
            }
        ],
    }


def _reject_modal(thread_id: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "hitl_reject_modal",
        "private_metadata": thread_id,
        "title": {"type": "plain_text", "text": "Reject refund"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "reason_block",
                "label": {"type": "plain_text", "text": "Reason"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reason",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Why is this refund being rejected?",
                    },
                },
            }
        ],
    }


async def _mark_processing(run: TicketRun, decision_summary: str) -> None:
    if not run.slack_ts:
        return
    client = _client()
    if client is None:
        return
    channel = run.slack_channel or os.getenv("SLACK_CHANNEL_ID", "")
    try:
        client.chat_update(
            channel=channel,
            ts=run.slack_ts,
            text=f"Processing decision for `{run.label}`...",
            blocks=_result_blocks(run, decision_summary=decision_summary),
        )
    except SlackApiError:
        pass


async def _resume_later(thread_id: str, decision: dict[str, Any]) -> None:
    """Resume off the Slack request path (Slack requires a response in ~3s)."""
    try:
        await manager.resume(thread_id, decision)
    except Exception as exc:  # noqa: BLE001
        _log(f"Slack resume failed for {thread_id}: {exc}")
        run = manager.get_run(thread_id)
        if run is not None and not run.error:
            run.error = f"Resume failed: {exc}"


async def handle_slack_interaction(body: bytes) -> dict[str, Any] | None:
    """
    Handle Slack interactivity payloads.
    Returns an optional JSON response body for Slack (usually None / empty ack).
    """
    import asyncio

    form = parse_qs(body.decode("utf-8"))
    payload_raw = form.get("payload", [None])[0]
    if not payload_raw:
        return {"error": "missing payload"}
    payload = json.loads(payload_raw)
    payload_type = payload.get("type")
    actor = _slack_user_label(payload.get("user"))
    _log(f"Slack interaction type={payload_type} by={actor}")

    client = _client()

    if payload_type == "block_actions":
        actions = payload.get("actions") or []
        if not actions:
            return None
        action = actions[0]
        action_id = action.get("action_id")
        thread_id = action.get("value")
        trigger_id = payload.get("trigger_id")
        run = manager.get_run(thread_id) if thread_id else None
        _log(
            f"Slack action={action_id} thread_id={thread_id} "
            f"run_status={getattr(run, 'status', None)}"
        )

        if run is None or run.status != "awaiting_approval":
            # Acknowledge with an update via response_url if present.
            return None

        if action_id == "hitl_approve":
            summary = f"Approved by {actor}"
            manager.record_decision_actor(thread_id, actor=actor, summary=summary)
            await _mark_processing(run, summary)
            asyncio.create_task(
                _resume_later(thread_id, build_decision("approve"))
            )
            return None

        if action_id == "hitl_edit" and client and trigger_id:
            try:
                client.views_open(trigger_id=trigger_id, view=_edit_modal(thread_id))
            except SlackApiError as exc:
                _log(f"Slack views_open(edit) failed: {exc.response.get('error')}")
            return None

        if action_id == "hitl_reject" and client and trigger_id:
            try:
                client.views_open(trigger_id=trigger_id, view=_reject_modal(thread_id))
            except SlackApiError as exc:
                _log(f"Slack views_open(reject) failed: {exc.response.get('error')}")
            return None

        return None

    if payload_type == "view_submission":
        view = payload.get("view") or {}
        callback_id = view.get("callback_id")
        thread_id = view.get("private_metadata")
        values = view.get("state", {}).get("values", {})
        run = manager.get_run(thread_id) if thread_id else None
        _log(f"Slack view_submission={callback_id} thread_id={thread_id} by={actor}")

        if run is None or run.status != "awaiting_approval":
            return {
                "response_action": "errors",
                "errors": {
                    "amount_block": "Ticket is no longer awaiting approval.",
                    "reason_block": "Ticket is no longer awaiting approval.",
                },
            }

        if callback_id == "hitl_edit_modal":
            amount = (
                values.get("amount_block", {})
                .get("amount", {})
                .get("value", "")
                .strip()
            )
            if not amount:
                return {
                    "response_action": "errors",
                    "errors": {"amount_block": "Enter a corrected amount."},
                }
            summary = f"Edited amount -> `{amount}` by {actor}"
            manager.record_decision_actor(thread_id, actor=actor, summary=summary)
            await _mark_processing(run, summary)
            asyncio.create_task(
                _resume_later(
                    thread_id,
                    build_decision(
                        "edit",
                        amount=amount,
                        pending_actions=run.pending_actions,
                    ),
                )
            )
            return {"response_action": "clear"}

        if callback_id == "hitl_reject_modal":
            reason = (
                values.get("reason_block", {})
                .get("reason", {})
                .get("value", "")
                .strip()
            )
            if not reason:
                return {
                    "response_action": "errors",
                    "errors": {"reason_block": "Please provide a reason."},
                }
            summary = f"Rejected by {actor}: {reason}"
            manager.record_decision_actor(thread_id, actor=actor, summary=summary)
            await _mark_processing(run, summary)
            asyncio.create_task(
                _resume_later(
                    thread_id,
                    build_decision("reject", message=reason),
                )
            )
            return {"response_action": "clear"}

    return None
