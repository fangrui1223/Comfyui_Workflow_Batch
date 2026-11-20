const state = {
  config: {},
  workflowId: "",
  inputNodes: [],
  outputNodes: [],
  statusTimer: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function setValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value || "";
}

function getValue(id) {
  return (document.getElementById(id)?.value || "").trim();
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败: ${response.status}`);
  return data;
}

function configFromForm() {
  return {
    comfy_root: getValue("comfyRoot"),
    python_path: getValue("pythonPath"),
    server_url: getValue("serverUrl"),
    last_workflow_path: getValue("workflowPath"),
    last_output_dir: getValue("outputDir"),
    poll_interval: 20,
  };
}

async function loadConfig() {
  state.config = await api("/api/config");
  setValue("comfyRoot", state.config.comfy_root);
  setValue("pythonPath", state.config.python_path);
  setValue("serverUrl", state.config.server_url);
  setValue("workflowPath", state.config.last_workflow_path);
  setValue("outputDir", state.config.last_output_dir);
}

async function saveConfig() {
  state.config = await api("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(configFromForm()),
  });
  toast("设置已保存");
}

function toast(message) {
  $("#comfyStatus").textContent = message;
}

async function pick(kind, target) {
  const initial = getValue(target) || getValue("comfyRoot") || "";
  const data = await api("/api/pick", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, initial }),
  });
  if (data.path) {
    setValue(target, data.path);
    if (target === "workflowPath") analyzeByPath();
  }
}

function nodeHeadline(node) {
  return `#${node.id} · ${node.class_type}`;
}

function nodeMeta(node) {
  const title = node.title ? ` · ${node.title}` : "";
  const value = node.current_value ? ` · 当前: ${node.current_value}` : "";
  const field = node.field ? `字段: ${node.field}` : "";
  return `${field}${title}${value}`;
}

function renderInputNodes() {
  const container = $("#inputNodes");
  container.className = "node-list";
  container.innerHTML = "";
  if (!state.inputNodes.length) {
    container.className = "node-list empty";
    container.textContent = "没有识别到输入节点";
    return;
  }

  const template = $("#inputNodeTemplate");
  state.inputNodes.forEach((node, index) => {
    const card = template.content.firstElementChild.cloneNode(true);
    $("[data-field-text='headline']", card).textContent = nodeHeadline(node);
    $("[data-field-text='meta']", card).textContent = nodeMeta(node);
    $("[data-field-text='confidence']", card).textContent = `${node.confidence_label} ${node.confidence}`;
    $("[data-field='enabled']", card).checked = node.enabled;
    $("[data-field='role']", card).value = node.role;
    $("[data-field='source_mode']", card).value = node.source_mode || "folder";
    $("[data-field='source_path']", card).value = node.source_path || "";

    $$("[data-field]", card).forEach((input) => {
      input.addEventListener("change", () => {
        const field = input.dataset.field;
        node[field] = input.type === "checkbox" ? input.checked : input.value;
      });
      input.addEventListener("input", () => {
        node[input.dataset.field] = input.value;
      });
    });

    $("[data-node-pick]", card).addEventListener("click", async () => {
      const mode = node.source_mode || "folder";
      const kind = mode === "single" ? "file" : "folder";
      const data = await api("/api/pick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, initial: node.source_path || state.config.last_input_dir || "" }),
      });
      if (data.path) {
        node.source_path = data.path;
        $("[data-field='source_path']", card).value = data.path;
      }
    });

    container.appendChild(card);
  });
}

function renderOutputNodes() {
  const container = $("#outputNodes");
  container.className = "node-list";
  container.innerHTML = "";
  if (!state.outputNodes.length) {
    container.className = "node-list empty";
    container.textContent = "没有识别到输出节点";
    return;
  }

  const template = $("#outputNodeTemplate");
  state.outputNodes.forEach((node) => {
    const card = template.content.firstElementChild.cloneNode(true);
    $("[data-field-text='headline']", card).textContent = nodeHeadline(node);
    $("[data-field-text='meta']", card).textContent = nodeMeta(node);
    $("[data-field-text='confidence']", card).textContent = `${node.confidence_label} ${node.confidence}`;
    $("[data-field='enabled']", card).checked = node.enabled;
    $("[data-field='enabled']", card).addEventListener("change", (event) => {
      node.enabled = event.target.checked;
    });
    container.appendChild(card);
  });
}

function applyAnalysis(data) {
  state.workflowId = data.workflow_id;
  state.inputNodes = data.inputs || [];
  state.outputNodes = data.outputs || [];
  $("#workflowBadge").textContent = data.workflow_name || "已导入";
  renderInputNodes();
  renderOutputNodes();
}

async function analyzeByPath() {
  const workflowPath = getValue("workflowPath");
  if (!workflowPath) {
    toast("请先选择工作流 JSON");
    return;
  }
  const data = await api("/api/analyze-workflow", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workflow_path: workflowPath }),
  });
  applyAnalysis(data);
  toast(`已识别 ${data.inputs.length} 个输入节点，${data.outputs.length} 个输出节点`);
}

async function analyzeFile(file) {
  const form = new FormData();
  form.append("workflow", file);
  const response = await fetch("/api/analyze-workflow", { method: "POST", body: form });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "工作流解析失败");
  applyAnalysis(data);
  toast(`已识别 ${data.inputs.length} 个输入节点，${data.outputs.length} 个输出节点`);
}

async function checkComfy() {
  const data = await api("/api/check-comfy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config: configFromForm() }),
  });
  const pathStatus = data.input_exists && data.output_exists ? "路径正常" : "input/output 未找到";
  toast(`${pathStatus} · ${data.message}`);
}

async function runBatch() {
  if (!state.workflowId) {
    toast("请先导入并识别工作流");
    return;
  }
  const config = configFromForm();
  await saveConfig();
  await api("/api/run-batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workflow_id: state.workflowId,
      config,
      output_dir: getValue("outputDir"),
      inputs: state.inputNodes,
      outputs: state.outputNodes,
    }),
  });
  $("#runBtn").disabled = true;
  startStatusPolling();
}

async function stopBatch() {
  await api("/api/stop", { method: "POST" });
  toast("已请求停止后续任务");
}

function renderStatus(status) {
  const total = status.total || 0;
  const current = status.current || 0;
  const percent = total ? Math.min(100, Math.round((current / total) * 100)) : 0;
  $("#progressBar").style.width = `${percent}%`;
  $("#statCurrent").textContent = `${current} / ${total}`;
  $("#statSuccess").textContent = `成功 ${status.success || 0}`;
  $("#statFailed").textContent = `失败 ${status.failed || 0}`;
  $("#statMessage").textContent = status.message || "等待任务";
  $("#runBtn").disabled = Boolean(status.running);

  const logs = status.logs || [];
  $("#logs").innerHTML = logs
    .map((line) => `<div class="log-line ${line.level}"><span>${line.time}</span><span>${line.level}</span><span>${escapeHtml(line.message)}</span></div>`)
    .join("");
  $("#logs").scrollTop = $("#logs").scrollHeight;

  if (!status.running && state.statusTimer) {
    clearInterval(state.statusTimer);
    state.statusTimer = null;
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function startStatusPolling() {
  if (state.statusTimer) clearInterval(state.statusTimer);
  const poll = async () => {
    try {
      renderStatus(await api("/api/status"));
    } catch (error) {
      toast(error.message);
    }
  };
  poll();
  state.statusTimer = setInterval(poll, 5000);
}

function wireEvents() {
  const workflowFile = $("#workflowFile");
  workflowFile.hidden = true;
  workflowFile.style.display = "none";
  $("#saveConfigBtn").addEventListener("click", saveConfig);
  $("#checkComfyBtn").addEventListener("click", checkComfy);
  $("#analyzePathBtn").addEventListener("click", analyzeByPath);
  $("#runBtn").addEventListener("click", runBatch);
  $("#stopBtn").addEventListener("click", stopBatch);

  $$("[data-pick]").forEach((button) => {
    button.addEventListener("click", () => pick(button.dataset.pick, button.dataset.target));
  });

  workflowFile.addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file) analyzeFile(file).catch((error) => toast(error.message));
  });

  const dropZone = $("#dropZone");
  dropZone.addEventListener("click", (event) => {
    event.preventDefault();
    workflowFile.click();
  });
  ["dragenter", "dragover"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.add("active");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.remove("active");
    });
  });
  dropZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files?.[0];
    if (file) analyzeFile(file).catch((error) => toast(error.message));
  });
}

loadConfig()
  .then(() => {
    wireEvents();
    startStatusPolling();
  })
  .catch((error) => toast(error.message));
