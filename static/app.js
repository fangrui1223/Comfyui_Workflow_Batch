const $ = (selector, root = document) => root.querySelector(selector);
const get = (id) => document.getElementById(id).value.trim();
const set = (id, value) => { document.getElementById(id).value = value || ""; };
const state = {
  config: {}, draft: null, preview: null, revision: 0, upload: 0,
  detailJob: null, detailEvent: null, jobsSignature: null, pending: new Set(),
};

const queueStates = {
  idle: "待命", paused: "等待开始或继续", running: "正在运行",
  pausing: "当前项保存后暂停", review: "有待确认项，队列已暂停",
};

const itemStates = {
  pending: "待处理", preparing: "准备素材", submitting: "提交中", running: "ComfyUI 执行中",
  saving: "保存成品", done: "已完成", failed: "失败", blocked: "配置失败",
  review: "待确认", skipped: "已跳过",
};

function escape(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function table(headers, rows) {
  return "<table><thead><tr>" + headers.map((header) => "<th scope=\"col\">" + escape(header) + "</th>").join("")
    + "</tr></thead><tbody>" + rows.map((row) => "<tr>" + row.map((cell) => "<td>" + cell + "</td>").join("")
    + "</tr>").join("") + "</tbody></table>";
}

function readout(text) {
  return "<div class=\"path-list\">" + escape(text) + "</div>";
}

function option(value, text, chosen) {
  return "<option value=\"" + escape(value) + "\"" + (value === chosen ? " selected" : "") + ">" + escape(text) + "</option>";
}

function check(text, checked, disabled = false) {
  return "<label class=\"check-line\"><input type=\"checkbox\"" + (checked ? " checked" : "")
    + (disabled ? " disabled" : "") + ">" + escape(text) + "</label>";
}

async function api(path, method = "GET", value) {
  const options = { method, cache: "no-store" };
  if (value instanceof FormData) {
    options.body = value;
  } else if (value !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(value);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "请求失败（" + response.status + "）");
  return data;
}

function draftMessage(message, isError = false) {
  $("#draftStatus").textContent = message;
  $("#draftStatus").classList.toggle("warning", isError);
  if (!state.draft && isError) showNotice(message, "error");
}

function showNotice(text, kind = "info") {
  const notice = $("#analysisWarning");
  notice.textContent = text;
  notice.dataset.state = kind;
}

function showWorkflow(kind, label) {
  const stage = $("#dropZone");
  const badge = $("#workflowBadge");
  stage.dataset.state = kind;
  badge.dataset.state = kind;
  badge.textContent = label;
  badge.title = label;
  $("#uploadTitle").textContent = kind === "loading" ? "正在识别工作流"
    : kind === "ready" ? "更换 API 工作流" : "上传 API 工作流";
}

function showConnection(kind, label, description = label) {
  const badge = $("#connectionBadge");
  badge.dataset.state = kind;
  badge.title = description;
  $("#comfyStatus").textContent = label;
}

function guard(key, action, onError = (error) => draftMessage(error.message, true)) {
  return async (...args) => {
    if (state.pending.has(key)) return;
    state.pending.add(key);
    try {
      await action(...args);
    } catch (error) {
      onError(error);
    } finally {
      state.pending.delete(key);
    }
  };
}

function configFromForm() {
  return {
    ...state.config,
    comfy_root: get("comfyRoot"),
    server_url: get("serverUrl"),
    comfy_input_dir: get("comfyInputDir"),
    comfy_output_dir: get("comfyOutputDir"),
    last_workflow_path: get("workflowPath"),
    last_output_dir: get("outputDir"),
  };
}

function invalidate() {
  state.revision += 1;
  state.preview = null;
  $("#preview").hidden = true;
  $("#enqueueBtn").disabled = true;
}

function nodeLabel(node) {
  return (node.title || node.class_type) + " · #" + node.id;
}

function docField(node) {
  return node.field ? "#" + node.id + "." + node.field : "";
}

function bindCheck(container, item, change) {
  const input = $("input[type=checkbox]", container);
  input.addEventListener("change", () => {
    item.enabled = input.checked;
    invalidate();
    change();
  });
}

async function pick(kind, initial) {
  const data = await api("/api/pick", "POST", { kind, initial });
  return data.path || "";
}

function inputCard(item) {
  const card = document.createElement("article");
  card.className = "node-card";
  const modes = item.access === "directory"
    ? [["directory", "当前目录固定"], ["groups", "总目录配对子文件夹"]]
    : [["folder", "文件夹内逐文件批量"], ["single", "固定单文件"]];
  card.innerHTML = '<div class="binding-heading"><strong>' + escape(nodeLabel(item)) + "</strong><small>"
    + escape([docField(item), item.reason, item.confidence_label].filter(Boolean).join(" · ")) + "</small></div>"
    + '<div class="enabled">' + check("使用此入口", item.enabled) + "</div>"
    + '<div class="node-controls"><label>节点接收<select class="access">'
    + [["upload", "ComfyUI 上传文件"], ["absolute", "文件绝对路径"], ["directory", "目录路径"]]
      .map(([value, label]) => option(value, label, item.access)).join("")
    + '</select></label><label>批量方式<select class="kind">' + modes.map(([value, label]) => option(value, label, item.source_mode)).join("")
    + '</select></label><label>素材类型<select class="role">'
    + [["image", "图片"], ["video", "视频"], ["audio", "音频"], ["file", "普通文件"]]
      .map(([value, label]) => option(value, label, item.role)).join("")
    + "</select></label></div>"
    + "<label>" + (item.source_mode === "groups" ? "总目录" : "素材路径")
    + '<span class="field-row"><input class="path" spellcheck="false"><button class="pick">选择</button></span></label>'
    + (item.source_mode === "groups" && item.scope !== true
      ? '<div class="scope">' + check("确认该节点只读取传入的当前子目录", item.confirm_scope, item.scope === false) + "</div>"
      : "");
  bindCheck($(".enabled", card), item, () => {});
  const pathInput = $(".path", card);
  pathInput.value = item.source_path || "";
  pathInput.addEventListener("input", () => {
    item.source_path = pathInput.value;
    invalidate();
  });
  $(".access", card).addEventListener("change", (event) => {
    const previous = item.access;
    item.access = event.target.value;
    if (previous === "directory" || item.access === "directory") {
      item.source_mode = item.access === "directory" ? "directory" : "folder";
    }
    invalidate();
    renderBindings();
  });
  $(".kind", card).addEventListener("change", (event) => {
    item.source_mode = event.target.value;
    invalidate();
    renderBindings();
  });
  $(".role", card).addEventListener("change", (event) => {
    item.role = event.target.value;
    invalidate();
  });
  const scope = $(".scope input", card);
  if (scope) scope.addEventListener("change", () => { item.confirm_scope = scope.checked; invalidate(); });
  $(".pick", card).addEventListener("click", guard("pick", async () => {
    const path = await pick(item.source_mode === "single" ? "file" : "folder", item.source_path || state.config.last_input_dir);
    if (!path) return;
    item.source_path = path;
    pathInput.value = path;
    invalidate();
  }));
  return card;
}

function fieldOptions(selected) {
  const allFields = state.draft.nodes.flatMap((node) =>
    node.fields.map((field) => [JSON.stringify([node.id, field]), nodeLabel(node) + "." + field]));
  return option("", "请选择目标路径字段", selected)
    + allFields.map(([value, label]) => option(value, label, selected)).join("");
}

function outputCard(item) {
  const card = document.createElement("article");
  card.className = "node-card";
  const target = item.binding ? JSON.stringify([item.binding.id, item.binding.field]) : "";
  card.innerHTML = '<div class="binding-heading"><strong>' + escape(nodeLabel(item)) + "</strong><small>"
    + escape(item.reason) + "</small></div>"
    + '<div class="enabled">' + check("收取此节点的成品", item.enabled, item.preview) + "</div>"
    + (item.preview ? "" : '<label>保存方式<select class="mode">'
      + option("history", "ComfyUI 标准输出", item.mode) + option("direct", "由工作流直接保存", item.mode)
      + "</select></label>")
    + (item.mode === "direct" && !item.preview ? '<label>工作流中的保存路径字段<select class="dest">'
      + fieldOptions(target) + "</select></label>" : "");
  bindCheck($(".enabled", card), item, () => {});
  const mode = $(".mode", card);
  if (mode) mode.addEventListener("change", () => {
    item.mode = mode.value;
    invalidate();
    renderBindings();
  });
  const dest = $(".dest", card);
  if (dest) dest.addEventListener("change", () => {
    const [id, field] = dest.value ? JSON.parse(dest.value) : [];
    item.binding = id ? { id, field } : null;
    invalidate();
  });
  return card;
}

function renderBindings() {
  $("#bindings").hidden = !state.draft;
  const inputs = $("#inputNodes");
  const outputs = $("#outputNodes");
  inputs.replaceChildren();
  outputs.replaceChildren();
  if (!state.draft) return;
  inputs.append(...state.draft.inputs.map(inputCard));
  outputs.append(...state.draft.outputs.map(outputCard));
  if (!inputs.children.length) inputs.textContent = "未识别到素材入口。固定在工作流中的素材会按原配置读取。";
  if (!outputs.children.length) outputs.textContent = "未识别到成品节点。请添加输出或检查工作流是否包含保存节点。";
}

function applyAnalysis(result) {
  state.draft = result;
  showWorkflow("ready", result.workflow_name);
  showNotice([
    result.warning,
    result.missing_types?.length ? "服务缺少节点 " + result.missing_types.join("、") : "",
    ...(result.connection_warnings || []),
  ].filter(Boolean).join("。"), result.missing_types?.length || result.connection_warnings?.length ? "error" : "info");
  invalidate();
  renderBindings();
  draftMessage("请检查绑定并预览配对。");
}

async function analyze(value, fromFile) {
  const serial = ++state.upload;
  state.draft = null;
  invalidate();
  renderBindings();
  showWorkflow("loading", "正在识别");
  showNotice("");
  const payload = fromFile ? new FormData() : { workflow_path: value, config: configFromForm() };
  if (fromFile) {
    payload.append("workflow", value);
    payload.append("server_url", get("serverUrl"));
  }
  try {
    const result = await api("/api/analyze-workflow", "POST", payload);
    if (serial === state.upload) applyAnalysis(result);
  } catch (error) {
    if (serial === state.upload) {
      showWorkflow("error", "等待有效文件");
      draftMessage(error.message, true);
    }
  }
}

function addBinding(kind) {
  if (!state.draft) return;
  const dialog = $("#bindingDialog");
  const selection = $("#bindingNode");
  dialog.dataset.kind = kind;
  $("#bindingTitle").textContent = kind === "input" ? "手动添加素材入口" : "手动添加成品输出";
  $("#bindingFieldRow").hidden = kind === "output";
  const candidates = state.draft.nodes.filter((node) => kind === "output" || node.fields.length);
  selection.innerHTML = candidates.map((node) => option(node.id, nodeLabel(node))).join("");
  updateBindingFields();
  dialog.showModal();
}

function updateBindingFields() {
  const node = state.draft?.nodes.find((candidate) => candidate.id === $("#bindingNode").value);
  $("#bindingField").innerHTML = (node?.fields || []).map((field) => option(field, field)).join("");
}

function saveBinding() {
  const dialog = $("#bindingDialog");
  const draft = state.draft;
  if (dialog.returnValue !== "confirm" || !draft) return;
  const node = draft.nodes.find((candidate) => candidate.id === $("#bindingNode").value);
  if (!node) return;
  if (dialog.dataset.kind === "input") {
    const field = $("#bindingField").value;
    if (draft.inputs.some((item) => item.id === node.id && item.field === field)) {
      draftMessage("该入口已在列表中，可以直接调整设置。", true);
      return;
    }
    draft.inputs.push({ ...node, field, enabled: true, role: "file", access: "absolute",
      source_mode: "single", reason: "手动绑定" });
  } else {
    if (draft.outputs.some((item) => item.id === node.id)) {
      draftMessage("该输出已在列表中，可以直接调整设置。", true);
      return;
    }
    draft.outputs.push({ ...node, enabled: true, mode: "history", reason: "手动绑定" });
  }
  invalidate();
  renderBindings();
}

const previewBatch = guard("preview", async () => {
  if (!state.draft) return;
  const revision = state.revision;
  const result = await api("/api/preview", "POST", {
    workflow_id: state.draft.workflow_id, config: configFromForm(),
    output_dir: get("outputDir"), inputs: state.draft.inputs, outputs: state.draft.outputs,
  });
  if (revision !== state.revision) return;
  state.preview = result;
  $("#preview").hidden = false;
  $("#previewTitle").textContent = "配对预览 · " + result.items.length + " 项";
  $("#previewRows").innerHTML = table(["顺序", "匹配", "素材"], result.items.map((item) => [
    item.position, escape(item.match), item.paths.map(readout).join("") || "由工作流配置",
  ]));
  $("#unmatched").innerHTML = result.unmatched.length
    ? "<p>未匹配 " + result.unmatched.length + " 个路径</p>" + result.unmatched.map(readout).join("")
      + check("排除以上路径，继续使用当前配对", false) : "";
  const acknowledgment = $("#unmatched input");
  $("#enqueueBtn").disabled = Boolean(acknowledgment);
  if (acknowledgment) acknowledgment.addEventListener("change", () => { $("#enqueueBtn").disabled = !acknowledgment.checked; });
  draftMessage(result.warning || "配对已确认，可以加入队列。");
});

const enqueue = guard("enqueue", async () => {
  const selected = state.preview;
  if (!selected) return;
  const response = await api("/api/queue", "POST", {
    preview_id: selected.preview_id,
    accept_unmatched: Boolean($("#unmatched input")?.checked),
  });
  if (state.preview === selected) {
    state.draft = null;
    state.upload += 1;
    set("workflowPath", "");
    $("#workflowFile").value = "";
    showWorkflow("idle", "等待下一份");
    showNotice("已加入队列 #" + response.job_id + " · " + selected.name, "success");
    invalidate();
    renderBindings();
  }
  await refreshStatus();
});

function queueCounts(jobs) {
  return jobs.reduce((total, job) => {
    for (const name of Object.keys(total)) total[name] += job[name] || 0;
    return total;
  }, { total: 0, done: 0, failed: 0, skipped: 0, pending: 0 });
}

async function renderItems(jobId) {
  const data = await api("/api/queue/" + jobId);
  if (state.detailJob !== jobId) return;
  $("#jobDetails").hidden = false;
  $("#jobDetailsTitle").textContent = "执行明细 · 工作流 #" + jobId;
  $("#items").innerHTML = table(["项", "状态", "素材与成品", "操作"], data.items.map((item) => {
    const actionable = ["review", "failed", "blocked"].includes(item.state);
    const controls = actionable ? '<button data-item="' + item.id + '" data-decision="retry">重跑</button> '
      + '<button data-item="' + item.id + '" data-decision="skip">跳过</button>' : "";
    const details = [
      item.message, ...item.paths, ...item.files, item.prompt_id && "ComfyUI 编号 " + item.prompt_id,
      item.staging && "部分成品目录 " + item.staging,
    ].filter(Boolean).map(readout).join("");
    return [item.position, escape(itemStates[item.state]), escape(item.label) + details, controls];
  }));
}

async function refreshStatus() {
  const status = await api("/api/status");
  const totals = queueCounts(status.jobs);
  const settled = totals.done + totals.failed + totals.skipped;
  $("#progressBar").style.width = (totals.total ? Math.min(100, settled * 100 / totals.total) : 0) + "%";
  const current = status.current;
  const summary = queueStates[status.state] + (current
    ? " · " + current.name + " · 第 " + current.position + " 项 · " + current.label + " · " + itemStates[current.state] : "");
  const statusText = $("#queueStatus");
  if (statusText.textContent !== summary) statusText.textContent = summary;
  $("#queueMonitor").dataset.state = status.state;
  $("#startBtn").disabled = status.enabled || !status.has_pending || status.state === "review";
  $("#startBtn").textContent = settled || current ? "继续队列" : "开始队列";
  $("#pauseBtn").disabled = !status.enabled;
  $("#statTotal").textContent = settled + " / " + totals.total;
  $("#statSuccess").textContent = totals.done;
  $("#statFailed").textContent = totals.failed;
  $("#statSkipped").textContent = totals.skipped;
  const signature = JSON.stringify(status.jobs);
  if (signature !== state.jobsSignature) {
    state.jobsSignature = signature;
    $("#jobs").innerHTML = status.jobs.length ? table(["工作流", "进度", "状态", "操作"], status.jobs.map(jobRow))
      : '<div class="queue-empty"><span class="empty-icon" aria-hidden="true"><i></i><i></i><i></i></span>'
        + '<strong>队列为空</strong><p class="hint">导入工作流并确认配对后，即可加入队列。</p></div>';
  }
  const log = $("#logs");
  const position = log.scrollTop;
  const nearBottom = log.scrollHeight - position - log.clientHeight < 45;
  log.innerHTML = status.events.map((event) => '<div class="log-line ' + escape(event.level) + '"><span>'
    + new Date(event.created_at * 1000).toLocaleTimeString("zh-CN", { hour12: false }) + "</span><span>"
    + escape(event.message) + "</span></div>").join("");
  log.scrollTop = nearBottom ? log.scrollHeight : position;
  const newestEvent = status.events.at(-1)?.id;
  if (state.detailJob !== null && state.detailEvent !== newestEvent) {
    state.detailEvent = newestEvent;
    await renderItems(state.detailJob);
  }
}

function jobRow(job) {
  const [kind, label] = job.review ? ["review", "待确认"] : job.active ? ["active", "执行中"]
    : job.pending ? ["pending", "排队中"] : job.failed ? ["failed", "有失败"] : ["done", "已处理"];
  return [
    '<div class="job-name">#' + job.id + " · " + escape(job.name) + "</div>",
    '<span class="queue-progress">' + (job.done + job.failed + job.skipped) + " / " + job.total + "</span>",
    '<span class="job-state ' + kind + '">' + label + "</span>",
    '<button data-detail="' + job.id + '">查看明细</button>',
  ];
}

function queueError(error) {
  $("#queueStatus").textContent = error.message;
  $("#queueMonitor").dataset.state = "error";
}

function queueAction(action) {
  return guard("queue-action", async () => {
    await api("/api/queue/" + action, "POST");
    await refreshStatus();
  }, queueError);
}

function wireEvents() {
  $("#settingsLink").addEventListener("click", () => { $("#environment").open = true; });
  $("#workflowFile").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file) {
      set("workflowPath", "");
      analyze(file, true);
    }
    event.target.value = "";
  });
  const upload = $("#dropZone");
  upload.addEventListener("click", () => $("#workflowFile").click());
  for (const name of ["dragover", "dragleave", "drop"]) {
    upload.addEventListener(name, (event) => {
      event.preventDefault();
      upload.classList.toggle("active", name === "dragover");
      const file = name === "drop" ? event.dataTransfer.files?.[0] : null;
      if (file) {
        set("workflowPath", "");
        analyze(file, true);
      }
    });
  }
  $("#analyzePathBtn").addEventListener("click", () => analyze(get("workflowPath"), false));
  $("#workflowPath").addEventListener("input", () => {
    state.upload += 1;
    state.draft = null;
    showWorkflow("idle", "等待读取");
    showNotice("");
    invalidate();
    renderBindings();
  });
  for (const id of ["outputDir", "comfyRoot", "comfyInputDir", "comfyOutputDir", "serverUrl"]) {
    document.getElementById(id).addEventListener("input", invalidate);
  }
  document.querySelectorAll("[data-pick]").forEach((button) => {
    button.addEventListener("click", guard("pick", async () => {
      const { pick: kind, target } = button.dataset;
      const path = await pick(kind, get(target));
      if (!path) return;
      set(target, path);
      if (kind === "workflow") await analyze(path, false);
      else invalidate();
    }));
  });
  $("#addInput").addEventListener("click", () => addBinding("input"));
  $("#addOutput").addEventListener("click", () => addBinding("output"));
  $("#bindingNode").addEventListener("change", updateBindingFields);
  $("#bindingDialog").addEventListener("close", saveBinding);
  $("#previewBtn").addEventListener("click", previewBatch);
  $("#enqueueBtn").addEventListener("click", enqueue);
  $("#pauseBtn").addEventListener("click", queueAction("pause"));
  $("#startBtn").addEventListener("click", queueAction("start"));
  $("#jobs").addEventListener("click", guard("details", async (event) => {
    const button = event.target.closest("[data-detail]");
    if (!button) return;
    state.detailJob = Number(button.dataset.detail);
    await renderItems(state.detailJob);
  }, queueError));
  $("#items").addEventListener("click", guard("decision", async (event) => {
    const button = event.target.closest("[data-item]");
    if (!button) return;
    if (button.dataset.decision === "retry" && !confirm("确认重新提交这一项？此前生成的部分文件会保留。")) return;
    await api("/api/queue/items/" + button.dataset.item, "POST", { decision: button.dataset.decision });
    await refreshStatus();
  }, queueError));
  $("#closeDetails").addEventListener("click", () => {
    state.detailJob = null;
    state.detailEvent = null;
    $("#jobDetails").hidden = true;
  });
  $("#saveConfigBtn").addEventListener("click", guard("settings", async () => {
    state.config = await api("/api/config", "POST", configFromForm());
    showConnection("unchecked", "设置已保存");
  }, (error) => showConnection("error", "保存失败", error.message)));
  $("#checkComfyBtn").addEventListener("click", guard("settings", async () => {
    const result = await api("/api/check-comfy", "POST", { config: configFromForm() });
    const pathsOk = result.input_exists && result.output_exists;
    if (!result.server_ok) {
      showConnection("error", "ComfyUI 未连接", result.message);
    } else if (!pathsOk) {
      showConnection("error", "请检查输入和输出目录");
    } else {
      showConnection("online", "ComfyUI 已连接");
    }
  }, (error) => showConnection("error", "连接检测失败", error.message)));
}

async function init() {
  wireEvents();
  const config = await api("/api/config");
  state.config = config;
  set("comfyRoot", config.comfy_root);
  set("serverUrl", config.server_url);
  set("comfyInputDir", config.comfy_input_dir);
  set("comfyOutputDir", config.comfy_output_dir);
  set("outputDir", config.last_output_dir);
  const tick = guard("Status", refreshStatus, (error) => queueError(new Error("无法读取本地队列 · " + error.message)));
  tick();
  setInterval(tick, 1500);
}

init().catch(queueError);
