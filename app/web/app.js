(() => {
  const APP_NAME = "app";
  const state = {
    userId: "elena_gold",
    sessionId: null,
    sending: false,
  };

  const els = {
    userSelect: document.getElementById("userSelect"),
    newSessionBtn: document.getElementById("newSessionBtn"),
    healthBadge: document.getElementById("healthBadge"),
    disruptionList: document.getElementById("disruptionList"),
    approvalList: document.getElementById("approvalList"),
    pendingCount: document.getElementById("pendingCount"),
    auditList: document.getElementById("auditList"),
    chatMessages: document.getElementById("chatMessages"),
    sessionMeta: document.getElementById("sessionMeta"),
    chatForm: document.getElementById("chatForm"),
    promptInput: document.getElementById("promptInput"),
    sendBtn: document.getElementById("sendBtn"),
    refreshQueueBtn: document.getElementById("refreshQueueBtn"),
    refreshAuditBtn: document.getElementById("refreshAuditBtn"),
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  async function ensureSession(forceNew = false) {
    if (state.sessionId && !forceNew) return state.sessionId;
    const resp = await fetch(
      `/apps/${APP_NAME}/users/${encodeURIComponent(state.userId)}/sessions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      }
    );
    if (!resp.ok) {
      throw new Error(`Failed creating session (${resp.status})`);
    }
    const data = await resp.json();
    state.sessionId = data.id;
    els.sessionMeta.textContent = `User: ${state.userId} | Session: ${state.sessionId}`;
    return state.sessionId;
  }

  async function loadHealth() {
    try {
      const resp = await fetch("/api/status");
      if (!resp.ok) throw new Error("unhealthy");
      const data = await resp.json();
      els.healthBadge.textContent = `${data.flash_model} + ${data.pro_model}`;
      els.healthBadge.className = "pill pill-ok";
    } catch {
      els.healthBadge.textContent = "Runtime unreachable";
      els.healthBadge.className = "pill pill-danger";
    }
  }

  async function loadDisruptions() {
    const resp = await fetch("/api/disruptions");
    if (!resp.ok) return;
    const items = await resp.json();
    els.disruptionList.replaceChildren();
    for (const item of items) {
      const card = el("div", "card");
      const title = el("div", "card-title");
      title.appendChild(el("span", "", `${item.pnr} · ${item.passenger_name}`));
      const pillClass =
        item.rebooking_status === "REBOOKED"
          ? "pill pill-ok"
          : item.rebooking_status === "ON_SCHEDULE"
            ? "pill pill-info"
            : "pill pill-warn";
      title.appendChild(el("span", pillClass, item.rebooking_status));
      card.appendChild(title);

      card.appendChild(
        el(
          "div",
          "card-meta",
          `${item.origin} → ${item.destination} | ${item.loyalty_tier} | Flights: ${item.segment_flights.join(", ")}`
        )
      );
      card.appendChild(el("div", "", item.disruption_summary));

      if (item.vouchers && item.vouchers.length > 0) {
        const vTexts = item.vouchers.map(
          (v) => `${v.voucher_id} ($${v.amount_usd} ${v.voucher_type})`
        );
        card.appendChild(el("div", "card-meta", `Vouchers: ${vTexts.join(", ")}`));
      }

      const btn = el("button", "btn btn-secondary btn-sm", "Load into Console");
      btn.type = "button";
      btn.style.marginTop = "8px";
      btn.addEventListener("click", () => {
        els.promptInput.value = `Inspect PNR ${item.pnr} (${item.passenger_name}), check regulatory compensation eligibility, and summarize recovery options.`;
        els.promptInput.focus();
      });
      card.appendChild(btn);
      els.disruptionList.appendChild(card);
    }
  }

  async function loadApprovals() {
    const resp = await fetch("/api/approvals");
    if (!resp.ok) return;
    const items = await resp.json();
    const pending = items.filter((a) => a.status === "PENDING");
    els.pendingCount.textContent = `${pending.length} Pending`;
    els.pendingCount.className = pending.length ? "pill pill-warn" : "pill pill-ok";

    els.approvalList.replaceChildren();
    if (items.length === 0) {
      els.approvalList.appendChild(
        el("div", "card-meta", "No pending HITL supervisor approvals yet.")
      );
      return;
    }

    for (const apr of items.slice(0, 8)) {
      const card = el("div", "card");
      const title = el("div", "card-title");
      title.appendChild(el("span", "", `${apr.approval_id} · ${apr.pnr}`));
      const badgeCls =
        apr.status === "PENDING"
          ? "pill pill-warn"
          : apr.status === "REJECTED"
            ? "pill pill-danger"
            : "pill pill-ok";
      title.appendChild(el("span", badgeCls, apr.status));
      card.appendChild(title);
      card.appendChild(el("div", "", apr.summary));
      card.appendChild(
        el("div", "card-meta", `Action: ${apr.action_type} | Exposure: $${apr.estimated_cost_usd}`)
      );

      if (apr.status === "PENDING") {
        const row = el("div", "btn-row");
        const approveBtn = el("button", "btn btn-ok btn-sm", "Approve & Execute");
        approveBtn.type = "button";
        approveBtn.addEventListener("click", () => decideApproval(apr, true));
        const rejectBtn = el("button", "btn btn-danger btn-sm", "Reject");
        rejectBtn.type = "button";
        rejectBtn.addEventListener("click", () => decideApproval(apr, false));
        row.appendChild(approveBtn);
        row.appendChild(rejectBtn);
        card.appendChild(row);
      }
      els.approvalList.appendChild(card);
    }
  }

  async function decideApproval(apr, approved) {
    const resp = await fetch(`/api/approvals/${encodeURIComponent(apr.approval_id)}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approved,
        reviewer: "ops_supervisor",
        decision_reason: approved
          ? "Approved from SkyCare HITL Operations Drawer"
          : "Denied from SkyCare HITL Operations Drawer",
      }),
    });
    if (resp.ok) {
      await loadApprovals();
      await loadAudit();
      const followUp = approved
        ? `Supervisor approved approval_id ${apr.approval_id} for PNR ${apr.pnr}. Please complete the pending ${apr.action_type.toLowerCase()} now.`
        : `Supervisor rejected approval_id ${apr.approval_id} for PNR ${apr.pnr}. Please verify status and inform the passenger politely.`;
      await sendPrompt(followUp);
    }
  }

  async function loadAudit() {
    const resp = await fetch("/api/audit?limit=30");
    if (!resp.ok) return;
    const events = await resp.json();
    els.auditList.replaceChildren();
    for (const ev of events) {
      const row = el("div", "audit-row");
      const head = el("div", "audit-row-head");
      const modelLabel = ev.model ? ` (${ev.model})` : "";
      head.appendChild(el("span", "", `${ev.event_type} · ${ev.actor}${modelLabel}`));
      const outcomeClass =
        ev.outcome === "blocked_policy" || ev.outcome === "rejected" || ev.outcome === "error"
          ? "pill pill-danger"
          : ev.outcome === "awaiting_approval" || ev.outcome === "redacted"
            ? "pill pill-warn"
            : "pill pill-ok";
      head.appendChild(el("span", outcomeClass, ev.outcome));
      row.appendChild(head);
      row.appendChild(el("div", "card-meta", `Intent: ${ev.intent}`));
      row.appendChild(el("div", "", `Action: ${ev.intended_action}`));
      if (ev.outcome_detail) {
        row.appendChild(el("div", "card-meta", ev.outcome_detail));
      }
      els.auditList.appendChild(row);
    }
  }

  function appendMessage(role, author, text, toolsUsed = []) {
    const box = el("div", `msg ${role === "user" ? "msg-user" : "msg-agent"}`);
    const header = el("div", "msg-header");
    header.appendChild(el("strong", "", role === "user" ? state.userId : author));
    if (role !== "user") {
      const isPro = author === "recovery_planner_agent";
      header.appendChild(
        el(
          "span",
          isPro ? "badge badge-pro" : "badge badge-flash",
          isPro ? "Pro 3.1" : "Flash 3.8"
        )
      );
    }
    box.appendChild(header);
    const body = el("div", "", text);
    box.appendChild(body);
    if (toolsUsed.length > 0) {
      const pills = el("div", "tool-pills");
      for (const t of toolsUsed) {
        pills.appendChild(el("span", "pill pill-info", `tool: ${t}`));
      }
      box.appendChild(pills);
    }
    els.chatMessages.appendChild(box);
    els.chatMessages.scrollTop = els.chatMessages.scrollHeight;
    return { body, header, box };
  }

  async function sendPrompt(promptText) {
    if (!promptText.trim() || state.sending) return;
    state.sending = true;
    els.sendBtn.disabled = true;

    try {
      const sessionId = await ensureSession();
      appendMessage("user", state.userId, promptText);
      const pendingNode = appendMessage("model", "skycare_concierge", "Thinking...");

      const resp = await fetch("/run_sse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          app_name: APP_NAME,
          user_id: state.userId,
          session_id: sessionId,
          new_message: {
            role: "user",
            parts: [{ text: promptText }],
          },
          streaming: true,
        }),
      });
      if (!resp.ok || !resp.body) {
        pendingNode.body.textContent = `Request failed (${resp.status}).`;
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let activeAuthor = "skycare_concierge";
      let fullText = "";
      const toolsSet = new Set();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const rawLine of lines) {
          const line = rawLine.trim();
          if (!line.startsWith("data:")) continue;
          const payloadStr = line.slice(5).trim();
          if (!payloadStr || payloadStr === "[DONE]") continue;
          let event;
          try {
            event = JSON.parse(payloadStr);
          } catch {
            continue;
          }
          if (event.author) activeAuthor = event.author;
          const parts = event.content && Array.isArray(event.content.parts)
            ? event.content.parts
            : [];
          for (const part of parts) {
            if (part.functionCall && part.functionCall.name) {
              toolsSet.add(part.functionCall.name);
            }
            if (part.text) {
              if (event.partial) {
                fullText += part.text;
              } else {
                fullText = part.text;
              }
            }
          }
          if (fullText) {
            pendingNode.body.textContent = fullText;
            const isPro = activeAuthor === "recovery_planner_agent";
            pendingNode.header.replaceChildren(
              el("strong", "", activeAuthor),
              el("span", isPro ? "badge badge-pro" : "badge badge-flash", isPro ? "Pro 3.1" : "Flash 3.8")
            );
            els.chatMessages.scrollTop = els.chatMessages.scrollHeight;
          }
        }
      }

      if (!fullText) {
        pendingNode.body.textContent = "Completed without text output.";
      }
      if (toolsSet.size > 0) {
        const pills = el("div", "tool-pills");
        for (const t of toolsSet) {
          pills.appendChild(el("span", "pill pill-info", `tool: ${t}`));
        }
        pendingNode.box.appendChild(pills);
      }
    } catch (err) {
      appendMessage("model", "system", `Error: ${err.message}`);
    } finally {
      state.sending = false;
      els.sendBtn.disabled = false;
      await Promise.all([loadDisruptions(), loadApprovals(), loadAudit()]);
    }
  }

  els.userSelect.addEventListener("change", async () => {
    state.userId = els.userSelect.value;
    state.sessionId = null;
    els.chatMessages.replaceChildren();
    await ensureSession(true);
  });

  els.newSessionBtn.addEventListener("click", async () => {
    state.sessionId = null;
    els.chatMessages.replaceChildren();
    await ensureSession(true);
  });

  els.chatForm.addEventListener("submit", async (evt) => {
    evt.preventDefault();
    const text = els.promptInput.value.trim();
    if (!text) return;
    els.promptInput.value = "";
    await sendPrompt(text);
  });

  els.promptInput.addEventListener("keydown", (evt) => {
    if (evt.key === "Enter" && !evt.shiftKey) {
      evt.preventDefault();
      els.chatForm.requestSubmit();
    }
  });

  for (const chip of document.querySelectorAll(".chip")) {
    chip.addEventListener("click", () => {
      const prompt = chip.getAttribute("data-prompt") || "";
      els.promptInput.value = prompt;
      els.promptInput.focus();
    });
  }

  els.refreshQueueBtn.addEventListener("click", loadDisruptions);
  els.refreshAuditBtn.addEventListener("click", loadAudit);

  async function init() {
    await Promise.all([loadHealth(), loadDisruptions(), loadApprovals(), loadAudit()]);
    try {
      await ensureSession();
    } catch {
      els.sessionMeta.textContent = "Session will initialize on first message.";
    }
  }

  init();
})();
