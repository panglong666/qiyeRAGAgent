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
  $("#leaveCard").classList.toggle("hidden", !user.permissions.includes("leave.review"));
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
      <div>${escapeHtml(text)}</div>
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
    renderList("#ticketsList", tickets, (item) => `<strong>#${item.id} ${escapeHtml(item.subject)}</strong><small>${escapeHtml(item.status)} · ${formatTime(item.created_at)}</small>`);
  } catch (error) { $("#ticketsList").textContent = error.message; }
  if (currentUser.permissions.includes("human_case.manage")) loadHumanCases();
  if (currentUser.permissions.includes("leave.review")) loadLeaveRequests();
}

async function loadHumanCases() {
  const items = await api("/api/human-cases");
  renderList("#humanCasesList", items, (item) => `
    <strong>#${item.id} ${escapeHtml(item.creator)} · ${escapeHtml(item.reason)}</strong>
    <small>${escapeHtml(item.question)} · ${escapeHtml(item.status)}</small>
    ${item.status === "pending" ? `<div class="list-actions"><button onclick="resolveCase(${item.id})">填写处理结果</button></div>` : ""}`);
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
  renderList("#leaveList", items, (item) => `
    <strong>#${item.id} ${escapeHtml(item.creator)} · ${escapeHtml(item.leave_type)}</strong>
    <small>${escapeHtml(item.start_date)} 至 ${escapeHtml(item.end_date)} · ${escapeHtml(item.status)}</small>
    ${item.status === "pending_human_approval" ? `<div class="list-actions"><button onclick="reviewLeave(${item.id}, 'approved')">批准</button><button onclick="reviewLeave(${item.id}, 'rejected')">拒绝</button></div>` : ""}`);
}

async function reviewLeave(id, decision) {
  const note = prompt("审批备注（可留空）：") || "";
  await api(`/api/leave-requests/${id}/review`, { method: "POST", body: JSON.stringify({ decision, note }) });
  loadLeaveRequests();
}
window.reviewLeave = reviewLeave;

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

