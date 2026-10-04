// 字幕节奏与镜头边界校正台 —— 原生 JS 前端
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  project: null,
};

// ---------- 工具 ----------
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", isErr);
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

function fmtMs(ms) {
  if (ms == null) return "—";
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  const x = ms % 1000;
  return [h, m, s].map(v => String(v).padStart(2, "0")).join(":") +
         "," + String(x).padStart(3, "0");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : {"Content-Type": "application/json"},
    ...opts,
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

// ---------- 项目列表 ----------
async function loadProjects(selectId) {
  const rows = await api("/api/projects");
  const sel = $("#projectSelect");
  sel.innerHTML = "";
  const opt0 = document.createElement("option");
  opt0.value = ""; opt0.textContent = "— 选择历史项目 —";
  sel.appendChild(opt0);
  for (const r of rows) {
    const o = document.createElement("option");
    o.value = r.id;
    o.textContent = `#${r.id} ${r.name}（${r.cue_count} 条，采纳 ${r.adopted_count}）`;
    sel.appendChild(o);
  }
}

// ---------- 渲染 ----------
function render() {
  const p = state.project;
  if (!p) {
    $("#workspace").classList.add("hidden");
    $("#importPanel").classList.remove("hidden");
    return;
  }
  $("#importPanel").classList.add("hidden");
  $("#workspace").classList.remove("hidden");
  $("#projectTitle").textContent = `#${p.id} ${p.name}`;
  const sf = $("#settingsForm");
  sf.cps_limit.value = p.cps_limit;
  sf.min_gap_ms.value = p.min_gap_ms;
  sf.tolerance_ms.value = p.tolerance_ms;

  const st = p.analysis.stats;
  $("#stats").innerHTML = [
    ["条目数", st.cue_count, ""],
    ["镜头数", st.shot_count, ""],
    ["间隔/重叠问题", st.gap_violations, st.gap_violations ? "bad" : "good"],
    ["CPS 超限", st.cps_violations, st.cps_violations ? "bad" : "good"],
    ["跨镜头条目", st.cross_shot_count, st.cross_shot_count ? "bad" : "good"],
    ["可提案条目", st.proposal_count, "good"],
    ["需移动条目", st.proposal_moved_count, "warn"],
    ["冲突条目", st.conflict_cue_count, st.conflict_cue_count ? "bad" : "good"],
    ["总移动量", st.total_shift_ms + " ms", ""],
  ].map(([k, v, cls]) => `<span class="stat ${cls}">${k} <b>${v}</b></span>`).join("");

  const conf = $("#conflicts");
  conf.innerHTML = "";
  for (const ch of p.analysis.chains) {
    const div = document.createElement("div");
    div.className = "chain";
    div.innerHTML = `<span class="tag">冲突链 ${ch.cue_ids.join(" → ")}</span>${esc(ch.reason)}`;
    conf.appendChild(div);
  }
  for (const it of p.analysis.intrinsic) {
    const div = document.createElement("div");
    div.className = "chain intrinsic";
    div.innerHTML = `<span class="tag">内在冲突 · ${esc(it.cue_id)}</span>${esc(it.reason)}`;
    conf.appendChild(div);
  }

  const tb = $("#cueTable tbody");
  tb.innerHTML = "";
  for (const c of p.analysis.cues) {
    const tr = document.createElement("tr");
    const flags = [];
    if (c.overlap_with) flags.push(`<span class="flag gap" title="${esc(c.overlap_with)}">间隔</span>`);
    if (!c.cps_ok) flags.push(`<span class="flag" title="CPS ${c.cps} 超限">CPS ${c.cps}</span>`);
    if (c.cross_shot) flags.push(`<span class="flag cross">跨镜头</span>`);

    const propTxt = c.proposal_start_ms == null ? "—（无解）"
      : `${c.proposal_start_ms} → ${c.proposal_end_ms}`;
    let deltaCls = "delta-zero", dTxt = "—";
    if (c.proposal_start_ms != null) {
      const d = c.proposal_start_ms - c.start_ms;
      dTxt = (d > 0 ? "+" : "") + d + " ms";
      deltaCls = d === 0 ? "delta-zero" : "delta-pos";
    }
    let status;
    if (c.adopted) {
      status = `<span class="pill adopted">已采纳</span>`;
    } else if (c.proposal_start_ms != null) {
      status = `<span class="pill proposed">待采纳</span>`;
    } else {
      status = `<span class="pill conflict">冲突</span>`;
    }
    const canAdopt = c.proposal_start_ms != null;
    const actionBtns = `
      <div class="row-actions">
        ${canAdopt ? `<button class="mini primary" data-act="adopt" data-uid="${c.uid}">采纳</button>` : ""}
        ${c.adopted ? `<button class="mini" data-act="revert" data-uid="${c.uid}">撤销</button>` : ""}
      </div>`;
    const startShown = c.adopted_start_ms ?? c.start_ms;
    const endShown = c.adopted_end_ms ?? c.end_ms;
    tr.className = c.proposal_start_ms == null ? "row-bad" : "";
    tr.innerHTML = `
      <td>${c.ord}</td>
      <td>${esc(c.cue_id)}</td>
      <td class="text-cell">${esc(c.text)}<div class="hash">sha256:${c.raw_hash.slice(0, 16)}…</div></td>
      <td>${startShown} → ${endShown}</td>
      <td>${c.duration_ms}</td>
      <td>${c.graphemes}</td>
      <td class="${c.cps_ok ? "cps-ok" : "cps-bad"}">${c.cps === Infinity ? "∞" : c.cps}</td>
      <td>${flags.join("") || "—"}</td>
      <td>${propTxt}</td>
      <td class="${deltaCls}">${dTxt}</td>
      <td>${status}${actionBtns}</td>`;
    tb.appendChild(tr);
  }

  const base = `/api/projects/${p.id}/export`;
  $("#exportJson").href = base + "?fmt=json";
  $("#exportZip").href = base + "?fmt=zip";
  $("#exportSrt").href = base + "?fmt=srt";
  $("#projectSelect").value = String(p.id);
}

// 采纳可能因另一标签页并发改参数而被拒（旧提案失效）：重新拉取详情，
// 让表格与统计条回到服务端的最新一致状态。
async function refreshAfterConflict(pid) {
  try {
    state.project = await api(`/api/projects/${pid}`);
    render();
    loadProjects();
  } catch (_) {}
}

// ---------- 事件 ----------
$("#importForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const fd = new FormData();
  fd.append("name", f.name.value.trim() || "未命名项目");
  fd.append("cps_limit", f.cps_limit.value);
  fd.append("min_gap_ms", f.min_gap_ms.value);
  fd.append("tolerance_ms", f.tolerance_ms.value);
  fd.append("subtitle", f.subtitle.files[0]);
  if (f.shots_file.files[0]) fd.append("shots_file", f.shots_file.files[0]);
  try {
    state.project = await api("/api/projects", { method: "POST", body: fd });
    toast("导入并分析完成");
    await loadProjects();
    render();
  } catch (e) { toast(e.message, true); }
});

$("#settingsForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!state.project) return;
  const f = ev.target;
  try {
    state.project = await api(`/api/projects/${state.project.id}/settings`, {
      method: "PUT",
      body: JSON.stringify({
        cps_limit: parseFloat(f.cps_limit.value),
        min_gap_ms: parseInt(f.min_gap_ms.value, 10),
        tolerance_ms: parseInt(f.tolerance_ms.value, 10),
      }),
    });
    toast("已按新参数重新求解");
    render();
    loadProjects();
  } catch (e) { toast(e.message, true); }
});

$("#cueTable").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn || !state.project) return;
  const uid = parseInt(btn.dataset.uid, 10);
  const c = state.project.analysis.cues.find(x => x.uid === uid);
  const pid = state.project.id;
  try {
    if (btn.dataset.act === "adopt") {
      state.project = await api(`/api/projects/${pid}/adopt`, {
        method: "POST",
        body: JSON.stringify({
          cue_uid: uid,
          new_start_ms: c.proposal_start_ms,
          new_end_ms: c.proposal_end_ms,
        }),
      });
      toast(`已采纳 cue ${c.cue_id}`);
    } else {
      state.project = await api(`/api/projects/${pid}/adopt/${uid}`,
                                { method: "DELETE" });
      toast(`已撤销 cue ${c.cue_id}`);
    }
    render();
    loadProjects();
  } catch (e) {
    toast(e.message + "（参数可能已在别处更新，已刷新）", true);
    refreshAfterConflict(pid);
  }
});

$("#adoptAllBtn").addEventListener("click", async () => {
  if (!state.project) return;
  const todo = state.project.analysis.cues.filter(
    c => c.proposal_start_ms != null && !c.adopted);
  if (!todo.length) return toast("没有可采纳的提案");
  try {
    let p = state.project;
    for (const c of todo) {
      p = await api(`/api/projects/${p.id}/adopt`, {
        method: "POST",
        body: JSON.stringify({
          cue_uid: c.uid,
          new_start_ms: c.proposal_start_ms,
          new_end_ms: c.proposal_end_ms,
        }),
      });
    }
    state.project = p;
    toast(`已批量采纳 ${todo.length} 条`);
    render();
    loadProjects();
  } catch (e) {
    toast(e.message + "（参数可能已在别处更新，已刷新）", true);
    refreshAfterConflict(state.project.id);
  }
});

$("#deleteProjectBtn").addEventListener("click", async () => {
  if (!state.project) return;
  if (!confirm(`确定删除项目「${state.project.name}」？该操作不可恢复。`)) return;
  try {
    await api(`/api/projects/${state.project.id}`, { method: "DELETE" });
    state.project = null;
    toast("项目已删除");
    await loadProjects();
    render();
  } catch (e) { toast(e.message, true); }
});

$("#projectSelect").addEventListener("change", async (ev) => {
  const id = ev.target.value;
  if (!id) return;
  try {
    state.project = await api(`/api/projects/${id}`);
    render();
  } catch (e) { toast(e.message, true); }
});

$("#newProjectBtn").addEventListener("click", () => {
  state.project = null;
  $("#projectSelect").value = "";
  render();
});

// 初始化
loadProjects().catch(() => {});
