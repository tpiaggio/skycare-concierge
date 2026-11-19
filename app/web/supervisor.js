// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

(() => {
  const POLL_INTERVAL_MS = 2000;
  const {
    resolveInitialLang,
    saveLang,
    el,
    formatUsd,
    formatTimestamp,
    applyI18nAttributes,
  } = window.SkyCareCommon;

  const I18N = {
    en: {
      locale: "en-US",
      pageTitle: "SkyCare Supervisor Desk · Airline Operations",
      brandTitle: "SkyCare Supervisor Desk",
      subtitle: "Airline Escalation Queue",
      pendingZero: "0 Pending",
      pendingCount: (n) => `${n} Pending`,
      resetDemo: "Reset sample flights",
      resetToast: "Sample flights reset and pending approvals cleared.",
      resetFailed: "Unable to reset sample flights right now.",
      escalationsHeading: "Escalated Conversations",
      resolvedHeading: "Recent Decisions",
      safeguardsHeading: "System Safeguards",
      emptyTitle: "No pending escalations",
      emptyBody:
        "Passenger requests exceeding the automatic cap or requiring ticket reissue appear here automatically.",
      approveBtn: "Approve",
      denyBtn: "Deny",
      approvingBtn: "Approving...",
      denyingBtn: "Denying...",
      decisionFailed: (status) => `Unable to record decision (${status}).`,
      approveReason: "Approved from SkyCare Supervisor Desk",
      denyReason: "Denied from SkyCare Supervisor Desk",
      refLabel: (id) => `Ref ${id}`,
      amountLabel: (amt) => `Amount: ${amt}`,
      actionTypes: {
        ISSUE_VOUCHER: "Hotel / Care Voucher",
        REBOOK_PNR: "PNR Ticket Reissue",
      },
      statuses: {
        PENDING: "PENDING",
        APPROVED: "APPROVED",
        CONSUMED: "COMPLETED",
        REJECTED: "DENIED",
      },
      capLabel: "Voucher Auto-Approval Cap",
      capDetail: (amt) =>
        `Auto-issues vouchers up to ${amt}; higher requests route here for review.`,
      piiLabel: "PII Redaction Shield",
      piiIdleDetail: "Masks payment cards, passports, emails, and phones in audit logs.",
      piiActiveDetail: (count, cats) =>
        cats
          ? `Masked sensitive fields ${count} time(s) (${cats}).`
          : `Masked sensitive fields ${count} time(s) cleanly.`,
      guardrailLabel: "Prompt Injection Guardrail",
      guardrailIdleDetail: "Screens override and policy-bypass attempts before tool execution.",
      guardrailBlockedDetail: (count, reason) =>
        reason
          ? `Blocked ${count} policy-bypass attempt(s): ${reason}`
          : `Blocked ${count} policy-bypass attempt(s).`,
      statusActive: "Active",
      piiMaskedPill: (n) => `Masked ✓ (${n})`,
      guardrailBlockedPill: (n) => `Blocked ✓ (${n})`,
      auditSummaryDefault: "Sanitized audit log (0)",
      auditSummaryCount: (n) => `Sanitized audit log (${n})`,
    },
    es: {
      locale: "es-419",
      pageTitle: "Mesa de Supervisor SkyCare · Operaciones",
      brandTitle: "Mesa de Supervisor SkyCare",
      subtitle: "Cola de Escalaciones",
      pendingZero: "0 Pendientes",
      pendingCount: (n) => `${n} Pendientes`,
      resetDemo: "Restaurar vuelos de demo",
      resetToast: "Vuelos de muestra restaurados y aprobaciones reiniciadas.",
      resetFailed: "No se pudieron restaurar los vuelos de muestra.",
      escalationsHeading: "Conversaciones Escaladas",
      resolvedHeading: "Decisiones Recientes",
      safeguardsHeading: "Controles del Sistema",
      emptyTitle: "Sin escalaciones pendientes",
      emptyBody:
        "Las solicitudes que superen el tope automático o requieran emisión de boleto aparecerán aquí automáticamente.",
      approveBtn: "Aprobar",
      denyBtn: "Denegar",
      approvingBtn: "Aprobando...",
      denyingBtn: "Denegando...",
      decisionFailed: (status) => `No se pudo registrar la decisión (${status}).`,
      approveReason: "Aprobado desde la Mesa de Supervisor SkyCare",
      denyReason: "Denegado desde la Mesa de Supervisor SkyCare",
      refLabel: (id) => `Ref ${id}`,
      amountLabel: (amt) => `Monto: ${amt}`,
      actionTypes: {
        ISSUE_VOUCHER: "Voucher de Hotel / Asistencia",
        REBOOK_PNR: "Emisión de Boleto PNR",
      },
      statuses: {
        PENDING: "PENDIENTE",
        APPROVED: "APROBADO",
        CONSUMED: "COMPLETADO",
        REJECTED: "DENEGADO",
      },
      capLabel: "Tope Automático de Vouchers",
      capDetail: (amt) =>
        `Auto-emite vouchers hasta ${amt}; montos mayores pasan a revisión aquí.`,
      piiLabel: "Escudo de Privacidad PII",
      piiIdleDetail: "Oculta tarjetas, pasaportes, correos y teléfonos en los registros.",
      piiActiveDetail: (count, cats) =>
        cats
          ? `Datos sensibles ocultados ${count} vez/veces (${cats}).`
          : `Datos sensibles ocultados ${count} vez/veces sin fugas.`,
      guardrailLabel: "Escudo Anti-Inyección",
      guardrailIdleDetail: "Bloquea intentos de anular reglas antes de ejecutar herramientas.",
      guardrailBlockedDetail: (count, reason) =>
        reason
          ? `${count} intento(s) de anulación bloqueado(s): ${reason}`
          : `${count} intento(s) de anulación bloqueado(s).`,
      statusActive: "Activo",
      piiMaskedPill: (n) => `Oculto ✓ (${n})`,
      guardrailBlockedPill: (n) => `Bloqueado ✓ (${n})`,
      auditSummaryDefault: "Registro de auditoría sanitizado (0)",
      auditSummaryCount: (n) => `Registro de auditoría sanitizado (${n})`,
    },
  };

  const state = {
    lang: resolveInitialLang(),
    voucherLimitUsd: 150,
    approvals: [],
    auditEvents: [],
    pnrMeta: new Map(),
    inFlightIds: new Set(),
    toastTimer: null,
  };

  const els = {
    langSelect: document.getElementById("langSelect"),
    pendingBadge: document.getElementById("pendingBadge"),
    resetDemoBtn: document.getElementById("resetDemoBtn"),
    resetToast: document.getElementById("resetToast"),
    pendingList: document.getElementById("pendingList"),
    resolvedSection: document.getElementById("resolvedSection"),
    resolvedList: document.getElementById("resolvedList"),
    capDetail: document.getElementById("capDetail"),
    capPill: document.getElementById("capPill"),
    piiDetail: document.getElementById("piiDetail"),
    piiPill: document.getElementById("piiPill"),
    guardrailDetail: document.getElementById("guardrailDetail"),
    guardrailPill: document.getElementById("guardrailPill"),
    auditSummary: document.getElementById("auditSummary"),
    auditList: document.getElementById("auditList"),
  };

  function t() {
    return I18N[state.lang] || I18N.en;
  }

  function showToast(message) {
    if (!els.resetToast) return;
    els.resetToast.textContent = message;
    els.resetToast.hidden = false;
    if (state.toastTimer !== null) {
      window.clearTimeout(state.toastTimer);
    }
    state.toastTimer = window.setTimeout(() => {
      els.resetToast.hidden = true;
    }, 4500);
  }

  function statusPillClass(status) {
    if (status === "APPROVED" || status === "CONSUMED") return "pill pill-mint";
    if (status === "REJECTED") return "pill pill-rose";
    return "pill pill-champagne";
  }

  function buildEscalationCard(apr) {
    const dict = t();
    const isPending = apr.status === "PENDING";
    const meta = state.pnrMeta.get(apr.pnr);
    const card = el(
      "div",
      isPending ? "escalation-card" : "escalation-card is-resolved"
    );

    const top = el("div", "escalation-top");
    const left = el("div", "");
    const passengerPart = meta
      ? `${apr.pnr} · ${meta.passenger_name} (${meta.loyalty_tier})`
      : apr.pnr;
    left.appendChild(el("div", "escalation-title", passengerPart));

    const routeParts = [];
    if (meta) routeParts.push(`${meta.origin} → ${meta.destination}`);
    const actionLabel = dict.actionTypes[apr.action_type] || apr.action_type;
    routeParts.push(actionLabel);
    left.appendChild(el("div", "escalation-route", routeParts.join(" · ")));
    top.appendChild(left);

    const statusLabel = dict.statuses[apr.status] || apr.status;
    top.appendChild(el("span", statusPillClass(apr.status), statusLabel));
    card.appendChild(top);

    card.appendChild(el("div", "escalation-summary", apr.summary));

    const metaRow = el("div", "escalation-meta");
    metaRow.appendChild(el("span", "", dict.refLabel(apr.approval_id)));
    if (Number(apr.estimated_cost_usd) > 0) {
      metaRow.appendChild(
        el("span", "", dict.amountLabel(formatUsd(apr.estimated_cost_usd, dict.locale)))
      );
    }
    const ts = formatTimestamp(apr.resolved_at || apr.created_at, dict.locale);
    if (ts) metaRow.appendChild(el("span", "", ts));
    card.appendChild(metaRow);

    if (isPending) {
      const actions = el("div", "escalation-actions");
      const busy = state.inFlightIds.has(apr.approval_id);

      const approveBtn = el(
        "button",
        "btn btn-sage btn-sm",
        busy ? dict.approvingBtn : dict.approveBtn
      );
      approveBtn.type = "button";
      approveBtn.disabled = busy;
      approveBtn.addEventListener("click", () => decideApproval(apr, true));

      const denyBtn = el(
        "button",
        "btn btn-outline btn-sm",
        busy ? dict.denyingBtn : dict.denyBtn
      );
      denyBtn.type = "button";
      denyBtn.disabled = busy;
      denyBtn.addEventListener("click", () => decideApproval(apr, false));

      actions.appendChild(approveBtn);
      actions.appendChild(denyBtn);
      card.appendChild(actions);
    }

    return card;
  }

  function renderApprovals() {
    const dict = t();
    const pending = state.approvals.filter((a) => a.status === "PENDING");
    const resolved = state.approvals.filter((a) => a.status !== "PENDING");

    if (els.pendingBadge) {
      els.pendingBadge.textContent = dict.pendingCount(pending.length);
      els.pendingBadge.className =
        pending.length > 0 ? "pill pill-champagne" : "pill pill-mint";
    }

    els.pendingList.replaceChildren();
    if (pending.length === 0) {
      const empty = el("div", "empty-card");
      empty.appendChild(el("strong", "", dict.emptyTitle));
      empty.appendChild(el("p", "", dict.emptyBody));
      els.pendingList.appendChild(empty);
    } else {
      for (const apr of pending) {
        els.pendingList.appendChild(buildEscalationCard(apr));
      }
    }

    if (els.resolvedSection && els.resolvedList) {
      els.resolvedList.replaceChildren();
      if (resolved.length === 0) {
        els.resolvedSection.hidden = true;
      } else {
        els.resolvedSection.hidden = false;
        for (const apr of resolved.slice(0, 8)) {
          els.resolvedList.appendChild(buildEscalationCard(apr));
        }
      }
    }
  }

  function renderSafeguards() {
    const dict = t();
    const capFormatted = formatUsd(state.voucherLimitUsd, dict.locale);
    if (els.capPill) els.capPill.textContent = capFormatted;
    if (els.capDetail) els.capDetail.textContent = dict.capDetail(capFormatted);

    const piiEvents = state.auditEvents.filter(
      (ev) => ev.event_type === "pii_redaction" || ev.outcome === "redacted"
    );
    if (els.piiPill && els.piiDetail) {
      if (piiEvents.length > 0) {
        els.piiPill.className = "pill pill-mint";
        els.piiPill.textContent = dict.piiMaskedPill(piiEvents.length);
        const latestDetail = String(piiEvents[0].outcome_detail || "").replace(
          /^categories=/,
          ""
        );
        els.piiDetail.textContent = dict.piiActiveDetail(piiEvents.length, latestDetail);
      } else {
        els.piiPill.className = "pill pill-neutral";
        els.piiPill.textContent = dict.statusActive;
        els.piiDetail.textContent = dict.piiIdleDetail;
      }
    }

    const blockedEvents = state.auditEvents.filter(
      (ev) => ev.event_type === "policy_guardrail" || ev.outcome === "blocked_policy"
    );
    if (els.guardrailPill && els.guardrailDetail) {
      if (blockedEvents.length > 0) {
        els.guardrailPill.className = "pill pill-mint";
        els.guardrailPill.textContent = dict.guardrailBlockedPill(blockedEvents.length);
        const reason = String(blockedEvents[0].outcome_detail || "").split(":")[0];
        els.guardrailDetail.textContent = dict.guardrailBlockedDetail(
          blockedEvents.length,
          reason
        );
      } else {
        els.guardrailPill.className = "pill pill-neutral";
        els.guardrailPill.textContent = dict.statusActive;
        els.guardrailDetail.textContent = dict.guardrailIdleDetail;
      }
    }

    if (els.auditSummary && els.auditList) {
      els.auditSummary.textContent = dict.auditSummaryCount(state.auditEvents.length);
      els.auditList.replaceChildren();
      for (const ev of state.auditEvents.slice(0, 15)) {
        const row = el("div", "audit-item");
        const head = el("div", "audit-item-head");
        const ts = formatTimestamp(ev.timestamp, dict.locale);
        head.appendChild(el("span", "", ts ? `${ev.actor} · ${ts}` : ev.actor));
        const pillCls =
          ev.outcome === "blocked_policy" || ev.outcome === "rejected" || ev.outcome === "error"
            ? "pill pill-rose"
            : ev.outcome === "awaiting_approval" || ev.outcome === "redacted"
              ? "pill pill-champagne"
              : "pill pill-mint";
        head.appendChild(el("span", pillCls, ev.outcome));
        row.appendChild(head);
        row.appendChild(el("div", "", ev.intended_action));
        if (ev.outcome_detail) {
          row.appendChild(el("div", "audit-item-detail", ev.outcome_detail));
        }
        els.auditList.appendChild(row);
      }
    }
  }

  function applyStaticTranslations() {
    const dict = t();
    document.documentElement.lang = state.lang;
    document.title = dict.pageTitle;
    if (els.langSelect) els.langSelect.value = state.lang;
    applyI18nAttributes(dict);
    renderApprovals();
    renderSafeguards();
  }

  async function refreshAll() {
    try {
      const [approvalsResp, auditResp, disruptionsResp] = await Promise.all([
        fetch("/api/approvals"),
        fetch("/api/audit?limit=40"),
        fetch("/api/disruptions"),
      ]);
      if (disruptionsResp.ok) {
        const disruptions = await disruptionsResp.json();
        const nextMap = new Map();
        for (const item of disruptions) {
          nextMap.set(item.pnr, item);
        }
        state.pnrMeta = nextMap;
      }
      if (approvalsResp.ok) {
        state.approvals = await approvalsResp.json();
      }
      if (auditResp.ok) {
        state.auditEvents = await auditResp.json();
      }
      renderApprovals();
      renderSafeguards();
    } catch {
      // Retain last known view across transient polling errors.
    }
  }

  async function loadStatus() {
    try {
      const resp = await fetch("/api/status");
      if (!resp.ok) return;
      const data = await resp.json();
      if (typeof data.voucher_auto_approve_limit_usd === "number") {
        state.voucherLimitUsd = data.voucher_auto_approve_limit_usd;
      }
      renderSafeguards();
    } catch {
      // Keep default cap if status check fails.
    }
  }

  async function decideApproval(apr, approved) {
    const dict = t();
    if (state.inFlightIds.has(apr.approval_id)) return;
    state.inFlightIds.add(apr.approval_id);
    renderApprovals();

    try {
      const resp = await fetch(
        `/api/approvals/${encodeURIComponent(apr.approval_id)}/decision`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            approved,
            reviewer: "ops_supervisor",
            decision_reason: approved ? dict.approveReason : dict.denyReason,
          }),
        }
      );
      if (!resp.ok && resp.status !== 409) {
        showToast(dict.decisionFailed(resp.status));
      }
    } catch {
      showToast(dict.decisionFailed("network"));
    } finally {
      state.inFlightIds.delete(apr.approval_id);
      await refreshAll();
    }
  }

  async function resetDemoData() {
    const dict = t();
    if (els.resetDemoBtn) els.resetDemoBtn.disabled = true;
    try {
      const resp = await fetch("/api/demo/reset", { method: "POST" });
      if (!resp.ok) {
        showToast(dict.resetFailed);
        return;
      }
      await refreshAll();
      showToast(dict.resetToast);
    } catch {
      showToast(dict.resetFailed);
    } finally {
      if (els.resetDemoBtn) els.resetDemoBtn.disabled = false;
    }
  }

  if (els.langSelect) {
    els.langSelect.addEventListener("change", () => {
      state.lang = saveLang(els.langSelect.value);
      applyStaticTranslations();
    });
  }

  if (els.resetDemoBtn) {
    els.resetDemoBtn.addEventListener("click", resetDemoData);
  }

  async function init() {
    applyStaticTranslations();
    await Promise.all([loadStatus(), refreshAll()]);
    window.setInterval(refreshAll, POLL_INTERVAL_MS);
  }

  init();
})();
