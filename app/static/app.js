const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let currentUser = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 401 && path !== "/api/login") showLogin();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function showLogin() {
  $("#loginView").classList.remove("hidden");
  $("#appView").classList.add("hidden");
  currentUser = null;
}

function showApp(user) {
  currentUser = user;
  $("#loginView").classList.add("hidden");
  $("#appView").classList.remove("hidden");
  $("#displayName").textContent = user.display_name;
  $("#avatar").textContent = user.display_name.slice(0, 1);
  $("#roleText").textContent = `${roleName(user.role)} · ${user.department}`;
  $("#metricRole").textContent = roleName(user.role);
  $("#metricTools").textContent = user.tools.length;
  $("#auditNav").classList.toggle("hidden", !user.permissions.includes("audit.read"));
  $("#humanCasesCard").classList.toggle("hidden", !user.permissions.includes("human_case.manage"));
  const canReviewLeave = user.permissions.includes("leave.review");
  const canRequestLeave = user.permissions.includes("leave.request");
  // 审批人看整条队列；申请人看自己的申请与审批凭证（闭环最后一环）。
  $("#leaveCard").classList.toggle("hidden", !(canReviewLeave || canRequestLeave));
  $("#leaveCardTitle").textContent = canReviewLeave ? "请假人工审批" : "我的请假申请";
  $("#leaveCardTag").textContent = canReviewLeave ? "Human-in-the-loop" : "申请凭证";
  $("#adminCard").classList.toggle("hidden", !user.permissions.includes("kb.manage"));
  loadHealth();
}

function roleName(role) {
  return ({ employee: "普通员工", hr: "人力专员", auditor: "审计员", admin: "系统管理员" })[role] || role;
}

$("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#loginError").textContent = "";
  try {
    const user = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username: $("#username").value, password: $("#password").value }),
    });
    showApp(user);
  } catch (error) { $("#loginError").textContent = error.message; }
});

$$('[data-user]').forEach((button) => button.addEventListener("click", () => {
  $("#username").value = button.dataset.user;
  $("#password").value = "";
  $("#password").focus();
}));

$("#logoutBtn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } finally { showLogin(); }
});

$$('.nav-item').forEach((button) => button.addEventListener("click", () => {
  $$('.nav-item').forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  $$('.panel').forEach((panel) => panel.classList.add("hidden"));
  $(`#${button.dataset.panel}`).classList.remove("hidden");
  $("#panelTitle").textContent = button.textContent.trim();
  if (button.dataset.panel === "workbenchPanel") loadWorkbench();
  if (button.dataset.panel === "auditPanel") loadAudit();
}));

$("#chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#messageInput");
  const message = input.value.trim();
  if (!message) return;
  addMessage("user", message);
  input.value = "";
  $("#sendBtn").disabled = true;
  const loading = addMessage("assistant", "正在检索制度并执行安全检查…", { loading: true });
  try {
    const result = await api("/api/chat", { method: "POST", body: JSON.stringify({ message }) });
    loading.remove();
    addMessage("assistant", result.answer, result);
  } catch (error) {
    loading.remove();
    addMessage("assistant", `请求未完成：${error.message}`);
  } finally { $("#sendBtn").disabled = false; input.focus(); }
});

$("#messageInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#chatForm").requestSubmit(); }
});

$$('.quick-prompts button').forEach((button) => button.addEventListener("click", () => {
  $("#messageInput").value = button.textContent;
  $("#chatForm").requestSubmit();
}));

function addMessage(role, text, data = {}) {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;
  const citations = (data.citations || []).map((citation, index) => `
    <div class="citation"><strong>[${index + 1}] ${escapeHtml(citation.section)}</strong>${escapeHtml(citation.quote)}</div>`).join("");
  const trace = (data.steps || []).map((step) => `<li>${escapeHtml(step.phase.toUpperCase())} · ${escapeHtml(step.detail)}</li>`).join("");
  wrapper.innerHTML = `
    <div class="message-icon">${role === "user" ? "我" : "AI"}</div>
    <div class="bubble"><p class="message-meta">${role === "user" ? escapeHtml(currentUser?.display_name || "用户") : "企业制度助手"}</p>
      <div class="bubble-body">${escapeHtml(text)}</div>
      ${citations ? `<div class="citations">${citations}</div>` : ""}
      ${data.escalated ? `<span class="escalated">已启动人工兜底</span>` : ""}
      ${trace ? `<details class="trace"><summary>查看 Agent 执行轨迹</summary><ol>${trace}</ol></details>` : ""}
    </div>`;
  $("#messages").appendChild(wrapper);
  wrapper.scrollIntoView({ behavior: "smooth", block: "end" });
  return wrapper;
}

async function loadHealth() {
  try {
    const data = await api("/api/health");
    $("#kbStatus").textContent = `${data.knowledge_chunks} 个知识块`;
    $("#metricModel").textContent = data.llm_enabled ? "LLM 增强" : "本地可信模式";
  } catch { $("#kbStatus").textContent = "异常"; }
}

async function loadWorkbench() {
  await loadHealth();
  try {
    const tickets = await api("/api/tickets");
    renderList("#ticketsList", tickets, (item) => record(
      item.id,
      item.subject,
      item.status,
      detailRow("提交人", item.creator)
        + detailRow("分类", item.category)
        + detailRow("内容", item.description)
        + detailRow("提交时间", formatTime(item.created_at)),
    ));
  } catch (error) { $("#ticketsList").textContent = error.message; }
  if (currentUser.permissions.includes("human_case.manage")) loadHumanCases();
  if (currentUser.permissions.includes("leave.review")
    || currentUser.permissions.includes("leave.request")) loadLeaveRequests();
}

async function loadHumanCases() {
  const items = await api("/api/human-cases");
  renderList("#humanCasesList", items, (item) => record(
    item.id,
    `${item.creator} · ${item.reason}`,
    item.status,
    detailRow("问题", item.question)
      + detailRow("提交时间", formatTime(item.created_at))
      + detailRow("处理人", item.resolver)
      + detailRow("处理时间", formatTime(item.resolved_at))
      + detailRow("处理结果", item.resolution),
    item.status === "pending"
      ? `<div class="list-actions"><button onclick="resolveCase(${item.id})">填写处理结果</button></div>`
      : "",
  ));
}

async function resolveCase(id) {
  const resolution = prompt("请输入人工处理结果：");
  if (!resolution) return;
  await api(`/api/human-cases/${id}/resolve`, { method: "POST", body: JSON.stringify({ resolution }) });
  loadHumanCases();
}
window.resolveCase = resolveCase;

async function loadLeaveRequests() {
  const items = await api("/api/leave-requests");
  renderList("#leaveList", items, (item) => record(
    item.id,
    `${item.creator} · ${item.leave_type}`,
    item.status,
    detailRow("起止日期", `${item.start_date} 至 ${item.end_date}`)
      + detailRow("事由", item.reason)
      + detailRow("提交时间", formatTime(item.created_at))
      + detailRow("审批人", item.reviewer)
      + detailRow("审批时间", formatTime(item.reviewed_at))
      + detailRow("审批意见", item.review_note),
    item.status === "pending_human_approval"
      ? leaveActions(item)
      : "",
  ));
}

// 审批按钮只给有审批权的人渲染：申请人看自己的单时不该出现"批准/拒绝"。
function leaveActions(item) {
  if (item.status !== "pending_human_approval") return "";
  if (!currentUser || !currentUser.permissions.includes("leave.review")) return "";
  return `<div class="list-actions">`
    + `<button onclick="reviewLeave(${item.id}, 'approved')">批准</button>`
    + `<button onclick="reviewLeave(${item.id}, 'rejected')">拒绝</button></div>`;
}

async function reviewLeave(id, decision) {
  const note = prompt("审批备注（可留空）：") || "";
  await api(`/api/leave-requests/${id}/review`, { method: "POST", body: JSON.stringify({ decision, note }) });
  loadLeaveRequests();
}
window.reviewLeave = reviewLeave;

const STATUS_LABELS = {
  open: "待处理", pending: "待处理", pending_human_approval: "待人工审批",
  approved: "已批准", rejected: "已拒绝", resolved: "已处理",
};

function statusLabel(status) { return STATUS_LABELS[status] || status; }

function detailRow(label, value) {
  return value ? `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>` : "";
}

// 列表项统一渲染成可展开记录：摘要一行，点开看完整字段（含审批凭证）。
function record(id, title, status, rows, actions = "") {
  return `<details class="record">
      <summary><strong>#${id} ${escapeHtml(title)}</strong><span class="status-chip">${escapeHtml(statusLabel(status))}</span></summary>
      <dl class="record-fields">${rows}</dl>
      ${actions}
    </details>`;
}

function renderList(selector, items, renderer) {
  $(selector).innerHTML = items.length ? items.map((item) => `<div class="list-item">${renderer(item)}</div>`).join("") : "暂无数据";
}

$("#refreshWorkbench").addEventListener("click", loadWorkbench);
$("#refreshAudit").addEventListener("click", loadAudit);

async function loadAudit() {
  try {
    const data = await api("/api/audit-logs");
    $("#chainBanner").textContent = data.chain_valid ? "✓ 审计哈希链校验通过，未发现历史记录篡改" : "⚠ 审计链校验失败，请立即检查数据库";
    $("#chainBanner").classList.toggle("invalid", !data.chain_valid);
    $("#auditBody").innerHTML = data.items.map((item) => `<tr><td>${formatTime(item.occurred_at)}</td><td>${escapeHtml(item.username)}</td><td>${escapeHtml(item.action)}</td><td>${escapeHtml(item.resource)}</td><td>${escapeHtml(item.outcome)}</td></tr>`).join("");
  } catch (error) { $("#chainBanner").textContent = error.message; }
}

$("#reindexBtn").addEventListener("click", async () => {
  $("#reindexResult").textContent = "正在重建…";
  try {
    const data = await api("/api/admin/reindex", { method: "POST" });
    $("#reindexResult").textContent = `完成：${data.chunk_count} 个知识块，版本 ${data.document_hash.slice(0, 12)}`;
    loadHealth();
  } catch (error) { $("#reindexResult").textContent = error.message; }
});

function formatTime(value) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]); }

(async function bootstrap() {
  try { showApp(await api("/api/me")); } catch { showLogin(); }
})();

