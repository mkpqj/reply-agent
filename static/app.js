const state = {
  conversations: [],
  selectedConversationId: null,
  latestSimulation: null,
  chatMessages: [],
  selectedFollowUpTaskId: null,
  followUpTasks: [],
};

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatDate(value) {
  if (!value) return "-";
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    throw new Error(payload?.detail || `Request failed: ${response.status}`);
  }
  return payload;
}

function getQueueBody() {
  return document.getElementById("queueTableBody");
}

function getFollowUpHint() {
  return document.getElementById("followUpHint");
}

function getFollowUpDetailRoot() {
  return document.getElementById("followUpDetail");
}

function setFollowUpHint(message = "", tone = "info") {
  const hint = getFollowUpHint();
  hint.textContent = message;
  hint.dataset.tone = tone;
}

function renderMetrics(metrics) {
  const cards = [
    ["总会话", metrics.total_conversations, "累计接入的会话数量"],
    ["待人工会话", metrics.pending_review_conversations, "当前需要人工跟进的会话"],
    ["开放任务", metrics.open_follow_up_tasks, "仍未处理的待跟进任务"],
    ["高风险会话", metrics.high_risk_conversations, "被标记为高风险的会话"],
    ["自动通过", metrics.auto_reply_count, "质检自动通过次数"],
    ["已发送回复", metrics.sent_reply_count, "自动发送成功数量"],
    ["已拦截回复", metrics.blocked_reply_count, "被质检策略拦截数量"],
    ["已领取任务", metrics.claimed_follow_up_tasks, "人工已接手任务"],
  ];

  const grid = document.getElementById("metricsGrid");
  grid.innerHTML = cards
    .map(
      ([label, value, hint], index) => `
        <article class="metric-card" style="transform: rotate(${index % 2 === 0 ? "-0.8deg" : "0.6deg"});">
          <span>${label}</span>
          <strong>${value}</strong>
          <em>${hint}</em>
        </article>
      `
    )
    .join("");
}

function renderQueue(tasks) {
  state.followUpTasks = tasks;
  const body = getQueueBody();
  if (!tasks.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty-state">当前没有待跟进任务。</td></tr>';
    return;
  }

  body.innerHTML = tasks
    .map(
      (task) => `
        <tr class="queue-row ${state.selectedFollowUpTaskId === task.id ? "active" : ""}" data-task-id="${escapeHtml(task.id)}">
          <td>${escapeHtml(task.id)}</td>
          <td>${escapeHtml(task.priority)}</td>
          <td>${escapeHtml(task.status)}</td>
          <td>${escapeHtml(task.reason)}</td>
          <td>${escapeHtml(task.message_content || "暂无客户消息")}</td>
          <td>${formatDate(task.due_at)}</td>
          <td><button type="button" class="queue-inline-button" data-task-id="${escapeHtml(task.id)}">查看详情</button></td>
        </tr>
      `
    )
    .join("");
}

function renderKnowledgeBase(documents) {
  const body = document.getElementById("kbTableBody");
  if (!documents.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty-state">当前没有知识条目。</td></tr>';
    return;
  }

  body.innerHTML = documents
    .slice(0, 20)
    .map(
      (item) => `
        <tr>
          <td>${escapeHtml(item.id)}</td>
          <td>${escapeHtml(item.kb_type)}</td>
          <td>${escapeHtml(item.product_id || "-")}</td>
          <td>${escapeHtml((item.intent_scope || []).join(", "))}</td>
          <td>${escapeHtml(item.title)}</td>
        </tr>
      `
    )
    .join("");
}

function renderConversations(conversations) {
  state.conversations = conversations;
  const container = document.getElementById("conversationList");
  if (!conversations.length) {
    container.innerHTML = '<p class="empty-state">当前筛选条件下没有会话。</p>';
    return;
  }

  container.innerHTML = conversations
    .map(
      (item) => `
        <article class="conversation-card ${state.selectedConversationId === item.id ? "active" : ""}" data-id="${item.id}">
          <div class="conversation-meta">
            <span class="meta-pill">${escapeHtml(item.status)}</span>
            <span class="meta-pill">${escapeHtml(item.risk_level || "-")}</span>
            <span class="meta-pill">${escapeHtml(item.current_intent || "未识别")}</span>
          </div>
          <h3>${escapeHtml(item.user_id)}</h3>
          <div class="tag-row">
            ${(item.active_tags || []).map((tag) => `<span class="tag-pill">${escapeHtml(tag)}</span>`).join("")}
          </div>
          <p class="conversation-preview">${escapeHtml(item.latest_message || "暂无消息")}</p>
          <p class="conversation-preview">最近更新时间：${formatDate(item.last_message_at)}</p>
        </article>
      `
    )
    .join("");

  container.querySelectorAll(".conversation-card").forEach((card) => {
    card.addEventListener("click", () => {
      state.selectedConversationId = card.dataset.id;
      renderConversations(state.conversations);
      loadConversationDetail(card.dataset.id);
    });
  });

  if (!state.selectedConversationId && conversations[0]) {
    state.selectedConversationId = conversations[0].id;
    renderConversations(state.conversations);
    loadConversationDetail(state.selectedConversationId);
  }
}

function renderConversationDetail(detail) {
  const root = document.getElementById("conversationDetail");
  document.getElementById("deleteConversationButton").disabled = false;
  const latestReply = detail.replies[detail.replies.length - 1];
  const latestQc = detail.quality_checks[detail.quality_checks.length - 1];

  root.innerHTML = `
    <div class="detail-block">
      <h3>会话概览</h3>
      <div class="conversation-meta">
        <span class="meta-pill">${escapeHtml(detail.conversation.status)}</span>
        <span class="meta-pill">${escapeHtml(detail.conversation.risk_level || "-")}</span>
        <span class="meta-pill">${escapeHtml(detail.conversation.current_intent || "未识别")}</span>
      </div>
      <div class="tag-row" style="margin-top:10px;">
        ${detail.tags.map((tag) => `<span class="tag-pill">${escapeHtml(tag.tag_code)}</span>`).join("")}
      </div>
    </div>
    <div class="detail-block">
      <h3>消息流</h3>
      ${detail.messages
        .map(
          (message) => `
            <div class="message-bubble ${message.sender_type === "user" ? "" : "agent"}">
              <strong>${message.sender_type === "user" ? "用户" : "系统"}</strong><br />
              ${escapeHtml(message.content)}
            </div>
          `
        )
        .join("")}
    </div>
    <div class="detail-block">
      <h3>最新回复与质检</h3>
      <div class="message-bubble agent">${escapeHtml(latestReply?.final_reply || latestReply?.draft_reply || "暂无回复记录")}</div>
      <p class="conversation-preview">发送状态：${escapeHtml(latestReply?.reply_status || "-")}</p>
      <p class="conversation-preview">质检模式：${escapeHtml(latestQc?.review_mode || "-")}</p>
      <p class="conversation-preview">质检建议：${escapeHtml(latestQc?.suggestion || "-")}</p>
    </div>
    <div class="detail-block">
      <h3>跟进任务</h3>
      ${
        detail.follow_up_tasks.length
          ? detail.follow_up_tasks
              .map(
                (task) => `
                  <div class="message-bubble">
                    <strong>${escapeHtml(task.priority)} / ${escapeHtml(task.status)}</strong><br />
                    原因：${escapeHtml(task.reason)}<br />
                    截止：${formatDate(task.due_at)}
                  </div>
                `
              )
              .join("")
          : '<p class="empty-state">当前没有跟进任务。</p>'
      }
    </div>
  `;
}

function buildChatMessagesFromDetail(detail) {
  const messages = detail?.messages || [];
  const replies = (detail?.replies || []).filter((reply) => {
    const content = (reply.final_reply || reply.draft_reply || "").trim();
    return content && content !== "??????";
  });
  const repliesByMessageId = replies.reduce((accumulator, reply) => {
    const bucket = accumulator.get(reply.message_id) || [];
    bucket.push(reply);
    accumulator.set(reply.message_id, bucket);
    return accumulator;
  }, new Map());

  repliesByMessageId.forEach((bucket) => {
    bucket.sort((left, right) => new Date(left.created_at) - new Date(right.created_at));
  });

  const chatMessages = [];

  messages.forEach((message) => {
    chatMessages.push({
      role: message.sender_type === "user" ? "user" : "agent",
      content: message.content,
      meta: message.sender_type === "user" ? "模拟客户" : "系统消息",
      createdAt: message.created_at,
    });

    const relatedReplies = repliesByMessageId.get(message.id) || [];
    relatedReplies.forEach((reply) => {
      chatMessages.push({
        role: "agent",
        content: reply.final_reply || reply.draft_reply,
        meta: `Agent · ${reply.reply_status}`,
        createdAt: reply.created_at,
      });
    });
  });

  chatMessages.sort((left, right) => {
    const leftTime = left.createdAt ? new Date(left.createdAt).getTime() : 0;
    const rightTime = right.createdAt ? new Date(right.createdAt).getTime() : 0;
    return leftTime - rightTime;
  });

  return chatMessages;
}

function renderChatBoard(detail, simulationResult = null) {
  const root = document.getElementById("chatBoard");
  if (detail) {
    state.chatMessages = buildChatMessagesFromDetail(detail);
  } else if (!state.chatMessages.length && simulationResult?.reply?.draft_reply) {
    state.chatMessages = [
      {
        role: "agent",
        content: simulationResult.final_reply || simulationResult.reply.draft_reply,
        meta: "Agent",
      },
    ];
  }

  const rows = state.chatMessages.map(
    (item) => `
      <div class="chat-row ${item.role}">
        <div class="chat-message-group ${item.role}">
          <div class="chat-bubble ${item.role} ${item.pending ? "pending" : ""}">${escapeHtml(item.content)}</div>
          <div class="chat-meta">${escapeHtml(item.meta || (item.role === "user" ? "模拟客户" : "Agent"))}</div>
        </div>
      </div>
    `
  );

  root.innerHTML = rows.length
    ? rows.join("")
    : `
      <div class="chat-empty">
        <p>左侧代表模拟客户，右侧代表 Agent。</p>
        <p>发送一条消息后，这里会像真实聊天窗口一样显示对话气泡。</p>
      </div>
    `;
  root.scrollTop = root.scrollHeight;
}

function renderSimulatorResult(result) {
  const root = document.getElementById("simulatorResult");
  state.latestSimulation = result;
  root.innerHTML = `
    <div class="sim-result-grid">
      <div class="sim-result-block">
        <h3>识别结果</h3>
        <div class="conversation-meta">
          <span class="meta-pill">${escapeHtml(result.intent_result.intent)}</span>
          <span class="meta-pill">置信度 ${escapeHtml(result.intent_result.confidence)}</span>
          <span class="meta-pill">${escapeHtml(result.action)}</span>
        </div>
        <p class="conversation-preview">${escapeHtml(result.intent_result.signals.join(" / "))}</p>
      </div>
      <div class="sim-result-block">
        <h3>Agent 回复</h3>
        <div class="message-bubble agent">${escapeHtml(result.reply.draft_reply)}</div>
        <p class="conversation-preview">最终发送：${escapeHtml(result.final_reply || "未自动发送，进入待处理队列")}</p>
      </div>
      <div class="sim-result-block">
        <h3>质检与标签</h3>
        <p class="conversation-preview">质检模式：${escapeHtml(result.quality_check.review_mode)}</p>
        <p class="conversation-preview">质检建议：${escapeHtml(result.quality_check.suggestion)}</p>
        <div class="tag-row">
          ${result.tags.map((tag) => `<span class="tag-pill">${escapeHtml(tag)}</span>`).join("")}
        </div>
      </div>
    </div>
  `;
}

function resetConversationPanels() {
  document.getElementById("conversationDetail").innerHTML =
    '<p class="empty-state">选择左侧会话后，这里会显示消息、回复、质检和跟进信息。</p>';
  document.getElementById("deleteConversationButton").disabled = true;
  document.getElementById("chatConversationStatus").textContent = "新会话";
  state.chatMessages = [];
  renderChatBoard(null, null);
}

function resetFollowUpDetail(options = {}) {
  const { keepSelection = false, message = "", tone = "info" } = options;
  if (!keepSelection) {
    state.selectedFollowUpTaskId = null;
  }
  getFollowUpDetailRoot().innerHTML =
    '<p class="empty-state">点击左侧待处理队列中的任务，这里会显示该任务的基础信息和客户消息，并可标记为已处理。</p>';
  setFollowUpHint(message, tone);
}

function scrollFollowUpPaneIntoView() {
  const pane = document.querySelector(".queue-detail-pane");
  if (!pane) return;
  const rect = pane.getBoundingClientRect();
  const offScreen = rect.top < 0 || rect.bottom > window.innerHeight;
  if (window.matchMedia("(max-width: 1100px)").matches || offScreen) {
    pane.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

async function loadMetrics() {
  const metrics = await fetchJson("/api/dashboard/metrics");
  renderMetrics(metrics);
}

async function markFollowUpTaskResolved(taskId) {
  const confirmed = window.confirm("确定将该任务标记为已处理吗？标记后它将从待处理队列中移除。");
  if (!confirmed) return;

  await fetchJson(`/api/follow-up/tasks/${taskId}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resolution_note: "人工标记为已处理" }),
  });

  state.selectedFollowUpTaskId = null;
  await Promise.all([loadMetrics(), loadConversations()]);
  await loadQueue({ preserveHint: true });
  setFollowUpHint("该任务已标记为已处理，并已从待处理队列移除。", "success");
}

async function loadFollowUpDetail(taskId, options = {}) {
  const { scrollIntoView = true, preserveHint = false } = options;
  try {
    state.selectedFollowUpTaskId = taskId;
    renderQueue(state.followUpTasks);
    getFollowUpDetailRoot().innerHTML = '<p class="empty-state">正在加载任务详情...</p>';
    if (!preserveHint) {
      setFollowUpHint("正在加载任务详情...", "info");
    }
    if (scrollIntoView) {
      scrollFollowUpPaneIntoView();
    }

    const detail = await fetchJson(`/api/follow-up/tasks/${taskId}`);
    getFollowUpDetailRoot().innerHTML = `
      <div class="detail-block">
        <h3>任务概览</h3>
        <div class="conversation-meta">
          <span class="meta-pill">${escapeHtml(detail.task.priority)}</span>
          <span class="meta-pill">${escapeHtml(detail.task.status)}</span>
          <span class="meta-pill">${escapeHtml(detail.conversation?.current_intent || "未识别")}</span>
        </div>
        <p class="conversation-preview">进入待跟进原因：${escapeHtml(detail.task.reason)}</p>
        <p class="conversation-preview">截止时间：${formatDate(detail.task.due_at)}</p>
        <p class="conversation-preview">会话 ID：${escapeHtml(detail.conversation?.id || "-")}</p>
        <p class="conversation-preview">用户 ID：${escapeHtml(detail.conversation?.user_id || "-")}</p>
      </div>
      <div class="detail-block">
        <h3>触发该任务的客户消息</h3>
        <div class="message-bubble">${escapeHtml(detail.source_message?.content || detail.task.message_content || "暂无关联消息")}</div>
        <p class="conversation-preview">消息时间：${formatDate(detail.source_message?.created_at)}</p>
      </div>
      <div class="detail-block">
        <button type="button" class="primary-button" id="markTaskResolvedButton">已处理</button>
      </div>
    `;

    const resolveButton = document.getElementById("markTaskResolvedButton");
    if (resolveButton) {
      resolveButton.addEventListener("click", () => markFollowUpTaskResolved(detail.task.id));
    }

    if (!preserveHint) {
      setFollowUpHint("已展开该任务详情。", "success");
    }
  } catch (error) {
    setFollowUpHint(error.message || "任务详情加载失败，请刷新后重试。", "error");
  }
}

async function loadQueue(options = {}) {
  const { preferredTaskId = null, preserveHint = false } = options;
  const tasks = await fetchJson("/api/follow-up/tasks");
  renderQueue(tasks);

  const currentHint = getFollowUpHint();
  const currentMessage = currentHint.textContent;
  const currentTone = currentHint.dataset.tone || "info";
  const targetTaskId =
    preferredTaskId ||
    (tasks.some((task) => task.id === state.selectedFollowUpTaskId) ? state.selectedFollowUpTaskId : null) ||
    tasks[0]?.id ||
    null;

  if (!targetTaskId) {
    resetFollowUpDetail({
      message: preserveHint ? currentMessage : "",
      tone: currentTone,
    });
    return tasks;
  }

  await loadFollowUpDetail(targetTaskId, { scrollIntoView: false, preserveHint });
  return tasks;
}

async function loadKnowledgeBase() {
  const documents = await fetchJson("/api/knowledge-base");
  renderKnowledgeBase(documents);
}

async function loadConfig() {
  const config = await fetchJson("/api/system/config");
  document.getElementById("autoReplyEnabled").checked = config.auto_reply_enabled;
  document.getElementById("llmEnabled").checked = config.llm_enabled;
  document.getElementById("llmModel").value = config.llm_model;
  document.getElementById("confidenceThreshold").value = config.intent_confidence_threshold;
  document.getElementById("qualityBlockSwitch").checked = config.quality_block_on_sensitive_missing_kb;
  document.getElementById("promisePatterns").value = config.promise_risk_patterns.join("\n");
  document.getElementById("autoReplySwitchText").textContent = config.auto_reply_enabled ? "已开启" : "已关闭";
  document.getElementById("chatModelStatus").textContent = config.llm_enabled ? `LLM: ${config.llm_model}` : "模板模式";
}

async function loadConversations() {
  const status = document.getElementById("statusFilter").value;
  const risk = document.getElementById("riskFilter").value;
  const query = new URLSearchParams();
  if (status) query.set("status", status);
  if (risk) query.set("risk_level", risk);
  const conversations = await fetchJson(`/api/conversations?${query.toString()}`);
  renderConversations(conversations);
}

async function loadConversationDetail(id) {
  if (!id) return;
  const detail = await fetchJson(`/api/conversations/${id}`);
  document.getElementById("chatConversationStatus").textContent = detail.conversation.current_intent || "未识别";
  renderConversationDetail(detail);
  renderChatBoard(detail, state.latestSimulation);
}

async function deleteSelectedConversation() {
  if (!state.selectedConversationId) return;
  const confirmed = window.confirm("确定要删除这条历史会话吗？删除后消息、回复和跟进记录都会一起移除。");
  if (!confirmed) return;

  await fetchJson(`/api/conversations/${state.selectedConversationId}`, {
    method: "DELETE",
  });

  state.selectedConversationId = null;
  state.latestSimulation = null;
  resetConversationPanels();
  resetFollowUpDetail();
  await Promise.all([loadMetrics(), loadQueue(), loadConversations()]);
}

async function seedDemoData() {
  await fetchJson("/api/demo/seed", { method: "POST" });
  document.getElementById("demoHint").textContent = "演示数据已重置并灌入。";
  state.selectedConversationId = null;
  state.latestSimulation = null;
  resetFollowUpDetail();
  await Promise.all([loadMetrics(), loadQueue(), loadConversations()]);
}

async function runDemoScenario(scenario) {
  await fetchJson("/api/demo/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario }),
  });
  document.getElementById("demoHint").textContent = `已生成场景：${scenario}`;
  state.selectedConversationId = null;
  state.latestSimulation = null;
  resetFollowUpDetail();
  await Promise.all([loadMetrics(), loadQueue(), loadConversations()]);
}

async function importKnowledgeBase(event) {
  event.preventDefault();
  const input = document.getElementById("kbFileInput");
  const file = input.files?.[0];
  if (!file) {
    document.getElementById("kbImportHint").textContent = "请先选择一个 CSV 文件。";
    return;
  }

  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch("/api/knowledge-base/import", {
    method: "POST",
    body: formData,
  });
  const result = await response.json();
  if (!response.ok) {
    document.getElementById("kbImportHint").textContent = result.detail || "导入失败。";
    return;
  }

  document.getElementById("kbImportHint").textContent =
    `导入成功：新增 ${result.imported_count} 条，跳过 ${result.skipped_count} 条，当前共 ${result.total_count} 条。`;
  input.value = "";
  await loadKnowledgeBase();
}

function fillRiskExample() {
  document.getElementById("simOrderStatus").value = "paid";
  document.getElementById("simMessage").value = "怎么还没发货，明天必须到，不然我要投诉平台，还要给我补偿。";
}

function fillPresaleExample() {
  document.getElementById("simOrderStatus").value = "";
  document.getElementById("simMessage").value = "这条围巾是什么材质，厚不厚，适合冬天戴吗？";
}

async function sendSimulatedMessage(event) {
  event.preventDefault();
  const content = document.getElementById("simMessage").value.trim();
  if (!content) {
    document.getElementById("simulatorResult").innerHTML =
      '<p class="empty-state">请先输入一条客户消息。</p>';
    return;
  }

  const conversationMode = document.getElementById("simConversationMode").value;
  const orderStatus = document.getElementById("simOrderStatus").value;
  const payload = {
    shop_id: document.getElementById("simShopId").value.trim() || "shop-demo",
    user_id: document.getElementById("simUserId").value.trim() || "manual-user-001",
    content,
    product_id: document.getElementById("simProductId").value.trim() || "sku-scarf",
  };

  if (conversationMode === "selected" && state.selectedConversationId) {
    payload.conversation_id = state.selectedConversationId;
  }

  if (orderStatus) {
    payload.order_context = {
      status: orderStatus,
      is_presale: document.getElementById("simIsPresale").checked,
    };
  }

  document.getElementById("simMessage").value = "";

  state.chatMessages.push({
    role: "user",
    content,
    meta: "模拟客户",
  });
  state.chatMessages.push({
    role: "agent",
    content: "",
    meta: "Agent 正在思考中...",
    pending: true,
  });
  renderChatBoard(null, state.latestSimulation);

  const response = await fetch("/api/channel/xiaohongshu/events/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok || !response.body) {
    document.getElementById("simulatorResult").innerHTML =
      '<p class="empty-state">流式请求失败，请稍后重试。</p>';
    return;
  }

  const decoder = new TextDecoder("utf-8");
  const reader = response.body.getReader();
  let buffer = "";
  let finalMeta = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";

    for (const frame of frames) {
      if (!frame.startsWith("data: ")) continue;
      const payloadText = frame.slice(6);
      if (!payloadText.trim()) continue;
      const eventData = JSON.parse(payloadText);

      if (eventData.type === "agent_start") {
        const agentMessage = state.chatMessages[state.chatMessages.length - 1];
        if (agentMessage?.role === "agent") {
          agentMessage.meta = "Agent 正在思考中...";
          agentMessage.pending = true;
        }
      }

      if (eventData.type === "agent_chunk") {
        const agentMessage = state.chatMessages[state.chatMessages.length - 1];
        if (agentMessage?.role === "agent") {
          agentMessage.content += eventData.content || "";
          agentMessage.meta = "Agent 正在回复...";
          agentMessage.pending = true;
        }
      }

      if (eventData.type === "meta") {
        finalMeta = eventData.payload;
      }

      if (eventData.type === "agent_done") {
        const agentMessage = state.chatMessages[state.chatMessages.length - 1];
        if (agentMessage?.role === "agent") {
          agentMessage.meta = "Agent";
          agentMessage.pending = false;
        }
      }

      renderChatBoard(null, state.latestSimulation);
    }
  }

  if (finalMeta) {
    state.selectedConversationId = finalMeta.conversation_id;
    renderSimulatorResult(finalMeta);
    await Promise.all([loadMetrics(), loadQueue(), loadConversations()]);
    await loadConversationDetail(finalMeta.conversation_id);
  }
}

async function saveConfig(event) {
  event.preventDefault();
  const payload = {
    auto_reply_enabled: document.getElementById("autoReplyEnabled").checked,
    llm_enabled: document.getElementById("llmEnabled").checked,
    llm_model: document.getElementById("llmModel").value.trim() || "gpt-4.1-mini",
    intent_confidence_threshold: Number(document.getElementById("confidenceThreshold").value),
    quality_block_on_sensitive_missing_kb: document.getElementById("qualityBlockSwitch").checked,
    promise_risk_patterns: document
      .getElementById("promisePatterns")
      .value.split("\n")
      .map((item) => item.trim())
      .filter(Boolean),
  };

  await fetchJson("/api/system/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  document.getElementById("configHint").textContent = "策略已保存。";
  await loadConfig();
}

function bindEvents() {
  document.getElementById("refreshQueueButton").addEventListener("click", () => loadQueue());
  document.getElementById("refreshConversationsButton").addEventListener("click", loadConversations);
  document.getElementById("deleteConversationButton").addEventListener("click", deleteSelectedConversation);
  document.getElementById("statusFilter").addEventListener("change", loadConversations);
  document.getElementById("riskFilter").addEventListener("change", loadConversations);
  document.getElementById("configForm").addEventListener("submit", saveConfig);
  document.getElementById("kbImportForm").addEventListener("submit", importKnowledgeBase);
  document.getElementById("simulatorForm").addEventListener("submit", sendSimulatedMessage);
  document.getElementById("seedDemoButton").addEventListener("click", seedDemoData);
  document.getElementById("fillRiskExampleButton").addEventListener("click", fillRiskExample);
  document.getElementById("fillPresaleExampleButton").addEventListener("click", fillPresaleExample);
  getQueueBody().addEventListener("click", (event) => {
    const row = event.target.closest(".queue-row");
    const button = event.target.closest(".queue-inline-button");
    if (button?.dataset.taskId) {
      loadFollowUpDetail(button.dataset.taskId);
      return;
    }
    if (row?.dataset.taskId) {
      loadFollowUpDetail(row.dataset.taskId);
    }
  });
  document.querySelectorAll(".demo-button").forEach((button) => {
    button.addEventListener("click", () => runDemoScenario(button.dataset.scenario));
  });
}

async function init() {
  bindEvents();
  resetConversationPanels();
  resetFollowUpDetail();
  await Promise.all([loadMetrics(), loadQueue(), loadConfig(), loadKnowledgeBase(), loadConversations()]);
}

init().catch((error) => {
  console.error(error);
  document.getElementById("conversationDetail").innerHTML =
    '<p class="empty-state">页面初始化失败，请检查服务是否正常启动。</p>';
});
