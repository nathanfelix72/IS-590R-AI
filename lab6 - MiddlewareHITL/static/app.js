const tableBody = document.getElementById("tableBody");
const emptyRow = document.getElementById("emptyRow");
const sampleSelect = document.getElementById("sampleSelect");
const connection = document.getElementById("connection");
const slackMeta = document.getElementById("slackMeta");
const summaryBar = document.getElementById("summaryBar");
const detail = document.getElementById("detail");
const editDialog = document.getElementById("editDialog");
const rejectDialog = document.getElementById("rejectDialog");
const runAllBtn = document.getElementById("runAll");
const runSampleBtn = document.getElementById("runSample");
const runCustomBtn = document.getElementById("runCustom");

const runs = new Map();
let selectedThreadId = null;
let pendingThreadId = null;
let sampleLabels = [];

const STEPS = [
  { id: "sanitize_intake", short: "Sanitize" },
  { id: "triage", short: "Triage" },
  { id: "draft_refund_action", short: "Refund" },
];

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(
      typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : res.statusText
    );
  }
  return data;
}

function setLoading(btn, on) {
  btn.disabled = on;
  btn.classList.toggle("loading", on);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function doneNodes(run) {
  return new Set(
    (run.events || [])
      .filter((e) => e.type === "node_complete")
      .map((e) => e.node)
  );
}

function stepState(run, stepId) {
  const done = doneNodes(run);
  if (done.has(stepId)) return "done";

  if (run.status === "error") {
    const order = STEPS.map((s) => s.id);
    const next = order.find((id) => !done.has(id));
    return next === stepId ? "error" : "pending";
  }

  if (run.status === "awaiting_approval" && stepId === "draft_refund_action") {
    return "active";
  }

  if (run.status === "completed") {
    if (done.has(stepId)) return "done";
    // Non-billing tickets never enter refund HITL.
    return "skipped";
  }

  if (run.status === "running" || run.status === "queued") {
    const order = STEPS.map((s) => s.id);
    // After a decision, sanitize/triage are done; refund is active again.
    if (done.has("sanitize_intake") && done.has("triage") && !done.has("draft_refund_action")) {
      if (stepId === "draft_refund_action") return "active";
      return done.has(stepId) ? "done" : "pending";
    }
    const next = order.find((id) => !done.has(id));
    if (next === stepId) return "active";
    return "pending";
  }

  return "pending";
}

function stepChip(run, step) {
  const state = stepState(run, step.id);
  const mark =
    state === "active"
      ? `<span class="mini-spinner" title="In progress"></span>`
      : state === "done"
        ? "✓"
        : state === "skipped"
          ? "–"
          : state === "error"
            ? "!"
            : "·";
  const label =
    state === "active"
      ? "working…"
      : state === "done"
        ? "done"
        : state === "skipped"
          ? "skipped"
          : state === "error"
            ? "failed"
            : "waiting";
  return `<span class="step-chip ${state}" title="${escapeHtml(step.id)}: ${label}">
    <span class="mark">${mark}</span>${escapeHtml(step.short)}
  </span>`;
}

function statusBadge(status) {
  const busy = status === "running" || status === "queued";
  return `<span class="badge ${escapeHtml(status || "")}">
    ${busy ? '<span class="dot"></span>' : ""}
    ${escapeHtml((status || "unknown").replaceAll("_", " "))}
  </span>`;
}

function currentStepLabel(run) {
  if (run.status === "awaiting_approval") return "Waiting for human approval";
  if (run.status === "completed") return "Finished";
  if (run.status === "error") return run.error || "Error";
  if (run.status === "queued") return "Queued…";
  for (const step of STEPS) {
    if (stepState(run, step.id) === "active") {
      return `Running ${step.short}…`;
    }
  }
  return "Running…";
}

function updateSummary() {
  const list = [...runs.values()];
  const counts = {
    running: list.filter((r) => r.status === "running" || r.status === "queued").length,
    awaiting: list.filter((r) => r.status === "awaiting_approval").length,
    completed: list.filter((r) => r.status === "completed").length,
    error: list.filter((r) => r.status === "error").length,
  };
  summaryBar.hidden = list.length === 0;
  summaryBar.querySelector('[data-k="running"] strong').textContent = counts.running;
  summaryBar.querySelector('[data-k="awaiting"] strong').textContent = counts.awaiting;
  summaryBar.querySelector('[data-k="completed"] strong').textContent = counts.completed;
  summaryBar.querySelector('[data-k="error"] strong').textContent = counts.error;
}

function renderDetail(run) {
  if (!run) {
    detail.hidden = true;
    return;
  }
  detail.hidden = false;
  document.getElementById("detailTitle").textContent = run.label;
  document.getElementById("detailStatus").innerHTML =
    `${statusBadge(run.status)} <span class="hint" style="margin-left:0.6rem">${escapeHtml(currentStepLabel(run))}</span>`;
  const decisionWrap = document.getElementById("detailDecisionWrap");
  if (run.decision_summary || run.decided_by) {
    decisionWrap.hidden = false;
    const text = (run.decision_summary || `Decided by ${run.decided_by}`).replace(
      /<@([^>]+)>/g,
      "@$1"
    );
    document.getElementById("detailDecision").textContent = text;
  } else {
    decisionWrap.hidden = true;
  }
  document.getElementById("detailSummary").textContent = run.redacted_summary || "—";
  document.getElementById("detailOutcome").textContent =
    run.refund_outcome || "(none yet / skipped)";

  const pendingWrap = document.getElementById("detailPendingWrap");
  const actionsEl = document.getElementById("detailActions");
  if (run.pending_actions) {
    pendingWrap.hidden = false;
    document.getElementById("detailPending").textContent = JSON.stringify(
      run.pending_actions,
      null,
      2
    );
    if (run.status === "awaiting_approval") {
      actionsEl.innerHTML = `
        <button class="btn primary" data-act="approve">Approve</button>
        <button class="btn" data-act="edit">Edit amount</button>
        <button class="btn danger" data-act="reject">Reject</button>
      `;
      actionsEl.querySelectorAll("[data-act]").forEach((btn) => {
        btn.addEventListener("click", () => onAction(run.thread_id, btn.dataset.act));
      });
    } else {
      actionsEl.innerHTML = "";
    }
  } else {
    pendingWrap.hidden = true;
    actionsEl.innerHTML = "";
  }

  const errorWrap = document.getElementById("detailErrorWrap");
  if (run.error) {
    errorWrap.hidden = false;
    document.getElementById("detailError").textContent = run.error;
  } else {
    errorWrap.hidden = true;
  }

  document.getElementById("detailTimeline").innerHTML = (run.events || [])
    .slice()
    .reverse()
    .slice(0, 12)
    .map((e) => {
      const detailText =
        e.type === "node_complete"
          ? e.node
          : e.type === "decision"
            ? JSON.stringify(e.decision)
            : e.message || "";
      return `<li>${escapeHtml(e.at?.slice(11, 19) || "")} · ${escapeHtml(e.type)}${
        detailText ? ` · ${escapeHtml(detailText)}` : ""
      }</li>`;
    })
    .join("");
}

function renderTable() {
  const list = [...runs.values()].sort((a, b) =>
    (b.updated_at || "").localeCompare(a.updated_at || "")
  );

  tableBody.innerHTML = "";
  if (!list.length) {
    tableBody.appendChild(emptyRow);
    emptyRow.hidden = false;
    updateSummary();
    renderDetail(null);
    return;
  }
  emptyRow.hidden = true;

  for (const run of list) {
    const tr = document.createElement("tr");
    const busy = run.status === "running" || run.status === "queued";
    if (busy) tr.classList.add("is-busy");
    if (run.thread_id === selectedThreadId) tr.classList.add("selected");

    tr.innerHTML = `
      <td class="ticket-cell">
        ${escapeHtml(run.label)}
        <span class="sub">${escapeHtml(currentStepLabel(run))}</span>
      </td>
      <td>${statusBadge(run.status)}</td>
      <td>${stepChip(run, STEPS[0])}</td>
      <td>${stepChip(run, STEPS[1])}</td>
      <td>${stepChip(run, STEPS[2])}</td>
      <td class="cat">${escapeHtml(run.category || "—")}</td>
      <td><button type="button" class="linkish" data-open>Details</button></td>
    `;

    tr.addEventListener("click", (ev) => {
      if (ev.target.closest("button")) return;
      selectedThreadId = run.thread_id;
      renderTable();
      renderDetail(run);
      detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    tr.querySelector("[data-open]").addEventListener("click", (ev) => {
      ev.stopPropagation();
      selectedThreadId = run.thread_id;
      renderTable();
      renderDetail(run);
      detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });

    tableBody.appendChild(tr);
  }

  updateSummary();
  if (selectedThreadId && runs.has(selectedThreadId)) {
    renderDetail(runs.get(selectedThreadId));
  }
}

function upsertRun(run) {
  runs.set(run.thread_id, run);
  renderTable();
}

async function onAction(threadId, act) {
  pendingThreadId = threadId;
  try {
    if (act === "approve") {
      await api(`/api/runs/${threadId}/decision`, {
        method: "POST",
        body: JSON.stringify({ type: "approve" }),
      });
    } else if (act === "edit") {
      document.getElementById("editAmount").value = "";
      editDialog.showModal();
    } else if (act === "reject") {
      document.getElementById("rejectReason").value = "";
      rejectDialog.showModal();
    }
  } catch (err) {
    alert(err.message);
  }
}

document.getElementById("closeDetail").addEventListener("click", () => {
  selectedThreadId = null;
  detail.hidden = true;
  renderTable();
});

document.getElementById("editForm").addEventListener("close", async () => {
  if (editDialog.returnValue !== "confirm" || !pendingThreadId) return;
  const amount = document.getElementById("editAmount").value.trim();
  if (!amount) return;
  try {
    await api(`/api/runs/${pendingThreadId}/decision`, {
      method: "POST",
      body: JSON.stringify({ type: "edit", amount }),
    });
  } catch (err) {
    alert(err.message);
  }
});

document.getElementById("rejectForm").addEventListener("close", async () => {
  if (rejectDialog.returnValue !== "confirm" || !pendingThreadId) return;
  const message = document.getElementById("rejectReason").value.trim();
  try {
    await api(`/api/runs/${pendingThreadId}/decision`, {
      method: "POST",
      body: JSON.stringify({ type: "reject", message }),
    });
  } catch (err) {
    alert(err.message);
  }
});

runSampleBtn.addEventListener("click", async () => {
  const label = sampleSelect.value;
  setLoading(runSampleBtn, true);
  try {
    const run = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ label }),
    });
    selectedThreadId = run.thread_id;
    upsertRun(run);
  } catch (err) {
    alert(err.message);
  } finally {
    setLoading(runSampleBtn, false);
  }
});

runAllBtn.addEventListener("click", async () => {
  setLoading(runAllBtn, true);
  document.getElementById("batchHint").textContent = "Starting all samples…";
  try {
    const batch = await api("/api/runs/batch", { method: "POST" });
    batch.forEach(upsertRun);
    if (batch[0]) selectedThreadId = batch[0].thread_id;
    document.getElementById("batchHint").textContent =
      `Queued ${batch.length} tickets — watch the table for step progress`;
  } catch (err) {
    alert(err.message);
    document.getElementById("batchHint").textContent = "Starts all sample tickets at once";
  } finally {
    setLoading(runAllBtn, false);
  }
});

document.getElementById("clearBoard").addEventListener("click", async () => {
  if (!confirm("Clear all tickets from the dashboard?")) return;
  try {
    await api("/api/runs", { method: "DELETE" });
    runs.clear();
    selectedThreadId = null;
    renderTable();
  } catch (err) {
    alert(err.message);
  }
});

runCustomBtn.addEventListener("click", async () => {
  const raw_message = document.getElementById("customMessage").value.trim();
  if (!raw_message) return;
  setLoading(runCustomBtn, true);
  try {
    const run = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ raw_message }),
    });
    selectedThreadId = run.thread_id;
    upsertRun(run);
  } catch (err) {
    alert(err.message);
  } finally {
    setLoading(runCustomBtn, false);
  }
});

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onopen = () => {
    connection.textContent = "Live";
    connection.className = "status-pill live";
  };
  es.onerror = () => {
    connection.textContent = "Reconnecting…";
    connection.className = "status-pill dead";
  };
  es.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.type === "snapshot") {
      runs.clear();
      (data.runs || []).forEach((run) => runs.set(run.thread_id, run));
      renderTable();
      return;
    }
    if (data.type === "run_update" && data.run) {
      upsertRun(data.run);
    }
  };
}

async function boot() {
  const health = await api("/api/health");
  const slackBit = health.slack_enabled
    ? "Slack: enabled"
    : "Slack: not configured";
  const lsBit = health.langsmith_tracing
    ? `LangSmith: on (${health.langsmith_project || "default"})`
    : "LangSmith: OFF";
  slackMeta.textContent = `${slackBit} · ${lsBit}`;
  sampleLabels = health.samples || [];
  sampleLabels.forEach((label) => {
    const opt = document.createElement("option");
    opt.value = label;
    opt.textContent = label;
    sampleSelect.appendChild(opt);
  });
  document.getElementById("batchHint").textContent =
    `Starts all ${sampleLabels.length || 5} sample tickets at once`;
  const existing = await api("/api/runs");
  existing.forEach((run) => runs.set(run.thread_id, run));
  renderTable();
  connectEvents();
}

boot().catch((err) => {
  connection.textContent = "Failed to load";
  connection.className = "status-pill dead";
  console.error(err);
});
