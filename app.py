import copy
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import logging
from pathlib import Path

try:
    from flask import Flask, jsonify, request, send_from_directory
except ImportError as exc:
    print("缺少 Flask。请使用 ComfyUI 自带 Python 安装一次: python -m pip install flask")
    raise
from werkzeug.serving import WSGIRequestHandler, make_server


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_FILE = BASE_DIR / "config.json"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
OUTPUT_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | {".gif", ".wav", ".mp3", ".flac"}

DEFAULT_CONFIG = {
    "comfy_root": r"C:\FR_comfyui",
    "python_path": r"C:\FR_comfyui\python\python.exe",
    "server_url": "http://127.0.0.1:8188",
    "last_workflow_path": "",
    "last_output_dir": "",
    "last_input_dir": "",
    "poll_interval": 20,
}

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.logger.disabled = True
logging.getLogger("werkzeug").disabled = True


class QuietRequestHandler(WSGIRequestHandler):
    def log(self, type, message, *args):
        return

STATE_LOCK = threading.Lock()
WORKFLOW_CACHE = {}
RUN_STATE = {
    "running": False,
    "stop_requested": False,
    "started_at": None,
    "finished_at": None,
    "total": 0,
    "current": 0,
    "success": 0,
    "failed": 0,
    "message": "等待任务",
    "logs": [],
}


def natural_sort_key(value):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def safe_json_load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_config():
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = safe_json_load(CONFIG_FILE)
    except Exception:
        return dict(DEFAULT_CONFIG)
    config = dict(DEFAULT_CONFIG)
    config.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    return config


def save_config(config):
    clean = dict(DEFAULT_CONFIG)
    clean.update({k: config.get(k, v) for k, v in DEFAULT_CONFIG.items()})
    CONFIG_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def add_log(message, level="info"):
    timestamp = time.strftime("%H:%M:%S")
    with STATE_LOCK:
        RUN_STATE["logs"].append(
            {
                "time": timestamp,
                "level": level,
                "message": message,
            }
        )
        RUN_STATE["logs"] = RUN_STATE["logs"][-300:]
        RUN_STATE["message"] = message
    print(f"[{timestamp}] {level:<7} {message}", flush=True)


def reset_run_state(total=0):
    with STATE_LOCK:
        RUN_STATE.update(
            {
                "running": True,
                "stop_requested": False,
                "started_at": time.time(),
                "finished_at": None,
                "total": total,
                "current": 0,
                "success": 0,
                "failed": 0,
                "message": "任务启动中",
                "logs": [],
            }
        )


def finish_run_state(message):
    timestamp = time.strftime("%H:%M:%S")
    with STATE_LOCK:
        RUN_STATE["running"] = False
        RUN_STATE["finished_at"] = time.time()
        RUN_STATE["message"] = message
        RUN_STATE["logs"].append(
            {
                "time": timestamp,
                "level": "done",
                "message": message,
            }
        )
        RUN_STATE["logs"] = RUN_STATE["logs"][-300:]
    print(f"[{timestamp}] done    {message}", flush=True)


def node_title(node):
    return str(node.get("_meta", {}).get("title", "") or "")


def confidence_label(score):
    if score >= 90:
        return "高"
    if score >= 65:
        return "中"
    return "低"


def classify_input_node(node_id, node):
    class_type = str(node.get("class_type", ""))
    title = node_title(node)
    inputs = node.get("inputs", {})
    lower = f"{class_type} {title}".lower()
    candidates = []

    for key, value in inputs.items():
        key_lower = key.lower()
        if isinstance(value, list):
            continue
        role = None
        score = 0

        if class_type == "LoadImage" and key_lower == "image":
            role, score = "image", 98
        elif "loadimage" in lower and key_lower in {"image", "file", "filename"}:
            role, score = "image", 92
        elif "loadvideo" in lower and key_lower in {"video", "file", "filename"}:
            role, score = "video", 96
        elif "vhs_loadvideo" in lower and key_lower == "video":
            role, score = "video", 98
        elif "video" in key_lower and isinstance(value, str):
            role, score = "video", 72
        elif "image" in key_lower and isinstance(value, str):
            role, score = "image", 72
        elif key_lower in {"file", "filename", "path"} and "load" in lower:
            role, score = "file", 62

        if role:
            candidates.append(
                {
                    "id": str(node_id),
                    "class_type": class_type,
                    "title": title,
                    "field": key,
                    "role": role,
                    "enabled": True,
                    "current_value": value,
                    "confidence": score,
                    "confidence_label": confidence_label(score),
                    "source_mode": "folder",
                    "source_path": "",
                }
            )
    return candidates


def classify_output_node(node_id, node):
    class_type = str(node.get("class_type", ""))
    title = node_title(node)
    inputs = node.get("inputs", {})
    lower = f"{class_type} {title}".lower()
    score = 0
    self_saved = class_type in {"RuiUniversalImageSaver"} or class_type.lower().endswith("universalimagesaver")

    if class_type in {"SaveImage", "SaveAnimatedWEBP", "SaveAudio"}:
        score = 96
    elif self_saved:
        score = 82
    elif "videocombine" in lower or "video combine" in lower:
        score = 92
    elif class_type.startswith("Save") or "save" in lower:
        score = 82
    elif "combine" in lower and "filename_prefix" in inputs:
        score = 70

    if not score:
        return None

    save_output = inputs.get("save_output")
    enabled = save_output is not False
    if save_output is False:
        score = max(55, score - 25)

    return {
        "id": str(node_id),
        "class_type": class_type,
        "title": title,
        "field": "filename_prefix" if "filename_prefix" in inputs else "filename",
        "role": "output",
        "enabled": enabled,
        "current_value": inputs.get("filename_prefix", inputs.get("filename", "")),
        "save_output": save_output,
        "self_saved": self_saved,
        "confidence": score,
        "confidence_label": confidence_label(score),
    }


def analyze_workflow(workflow):
    input_nodes = []
    output_nodes = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        input_nodes.extend(classify_input_node(node_id, node))
        output_node = classify_output_node(node_id, node)
        if output_node:
            output_nodes.append(output_node)

    input_nodes.sort(key=lambda item: (-item["confidence"], natural_sort_key(item["id"])))
    output_nodes.sort(key=lambda item: (-int(item["enabled"]), -item["confidence"], natural_sort_key(item["id"])))
    return {"inputs": input_nodes, "outputs": output_nodes}


def list_media_files(path, role):
    directory = Path(path)
    if not directory.exists() or not directory.is_dir():
        raise ValueError(f"文件夹不存在: {path}")

    if role == "image":
        allowed = IMAGE_EXTENSIONS
    elif role == "video":
        allowed = VIDEO_EXTENSIONS
    else:
        allowed = None

    files = []
    for child in directory.iterdir():
        if child.is_file() and (allowed is None or child.suffix.lower() in allowed):
            files.append(child)
    return sorted(files, key=lambda p: natural_sort_key(p.name))


def build_batch_plan(inputs):
    enabled_inputs = [item for item in inputs if item.get("enabled")]
    if not enabled_inputs:
        raise ValueError("没有启用任何输入节点")

    prepared = []
    batch_lengths = []
    for item in enabled_inputs:
        source_path = str(item.get("source_path", "")).strip().strip('"')
        if not source_path:
            raise ValueError(f"节点 {item.get('id')} 没有填写素材路径")
        mode = item.get("source_mode", "folder")
        role = item.get("role", "file")
        if mode == "single":
            source = Path(source_path)
            if not source.exists() or not source.is_file():
                raise ValueError(f"节点 {item.get('id')} 的单文件不存在: {source_path}")
            files = [source]
        else:
            files = list_media_files(source_path, role)
            if not files:
                raise ValueError(f"节点 {item.get('id')} 的文件夹没有找到可用素材: {source_path}")
            batch_lengths.append(len(files))
        prepared.append({**item, "files": files})

    total = min(batch_lengths) if batch_lengths else 1
    if total < 1:
        raise ValueError("没有可运行的素材")

    batches = []
    for index in range(total):
        entries = []
        names = []
        for item in prepared:
            files = item["files"]
            source = files[0] if item.get("source_mode") == "single" else files[index]
            names.append(source.stem)
            entries.append({**item, "source": source})
        batches.append({"index": index, "entries": entries, "base_name": choose_base_name(names, index)})
    return batches, prepared


def choose_base_name(names, index):
    clean = [re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name).strip("_") for name in names if name]
    if not clean:
        return f"batch_{index + 1:04d}"
    base = clean[0]
    if len(clean) > 1 and clean[1] != base:
        base = f"{base}_{clean[1]}"
    return base[:90] or f"batch_{index + 1:04d}"


def comfy_request(server_url, path, payload=None, timeout=20):
    url = server_url.rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def make_input_filename(batch_index, node_id, source):
    safe_node = re.sub(r"[^0-9A-Za-z_-]+", "_", str(node_id)).strip("_") or "node"
    return f"Batch_{batch_index + 1:04d}_{safe_node}_{source.name}"


def copy_to_input(source, comfy_input, target_name):
    comfy_input.mkdir(parents=True, exist_ok=True)
    target = comfy_input / target_name
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return target.name


def set_nested_input(workflow, node_id, field, value):
    node = workflow.get(str(node_id))
    if not node:
        return False
    inputs = node.setdefault("inputs", {})
    inputs[field] = value
    return True


def collect_output_files(history_item, comfy_output, prefix, since_time):
    found = []
    outputs = history_item.get("outputs", {}) if isinstance(history_item, dict) else {}
    for output in outputs.values():
        if not isinstance(output, dict):
            continue
        for group in ("images", "videos", "audio", "gifs"):
            for item in output.get(group, []) or []:
                filename = item.get("filename") if isinstance(item, dict) else None
                subfolder = item.get("subfolder", "") if isinstance(item, dict) else ""
                if filename:
                    found.append(comfy_output / subfolder / filename)

    for child in comfy_output.rglob("*"):
        try:
            if child.is_file() and child.suffix.lower() in OUTPUT_EXTENSIONS:
                if child.name.startswith(prefix) or child.stat().st_mtime >= since_time:
                    found.append(child)
        except OSError:
            continue

    unique = []
    seen = set()
    for path in found:
        key = str(path.resolve())
        if key not in seen and path.exists():
            seen.add(key)
            unique.append(path)
    return sorted(unique, key=lambda p: p.stat().st_mtime)


def move_outputs(files, output_dir, base_name):
    output_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for index, source in enumerate(files, start=1):
        suffix = source.suffix or ".out"
        name = f"{base_name}{suffix}" if len(files) == 1 else f"{base_name}_{index:02d}{suffix}"
        target = output_dir / name
        counter = 2
        while target.exists():
            target = output_dir / f"{Path(name).stem}_{counter}{suffix}"
            counter += 1
        shutil.move(str(source), str(target))
        moved.append(target)
    return moved


def run_worker(payload):
    config = payload.get("config", {})
    workflow_id = payload.get("workflow_id")
    workflow = WORKFLOW_CACHE.get(workflow_id)
    if not workflow:
        finish_run_state("找不到已导入的工作流，请重新导入")
        return

    comfy_root = Path(config.get("comfy_root") or DEFAULT_CONFIG["comfy_root"])
    comfy_input = comfy_root / "input"
    comfy_output = comfy_root / "output"
    server_url = config.get("server_url") or DEFAULT_CONFIG["server_url"]
    output_dir = Path(payload.get("output_dir") or config.get("last_output_dir") or "")
    poll_interval = max(2, int(config.get("poll_interval") or DEFAULT_CONFIG["poll_interval"]))
    outputs = [item for item in payload.get("outputs", []) if item.get("enabled")]

    try:
        if not comfy_input.exists():
            raise ValueError(f"ComfyUI input 文件夹不存在: {comfy_input}")
        if not comfy_output.exists():
            raise ValueError(f"ComfyUI output 文件夹不存在: {comfy_output}")
        if not output_dir:
            raise ValueError("没有填写输出保存目录")
        batches, prepared = build_batch_plan(payload.get("inputs", []))
    except Exception as exc:
        finish_run_state(str(exc))
        add_log(str(exc), "error")
        return

    with STATE_LOCK:
        RUN_STATE["total"] = len(batches)
    add_log(f"准备运行 {len(batches)} 个批次")

    for batch in batches:
        with STATE_LOCK:
            if RUN_STATE["stop_requested"]:
                add_log("已停止后续任务", "warn")
                break
            RUN_STATE["current"] = batch["index"] + 1

        prefix = f"Batch_{batch['index'] + 1:04d}_{batch['base_name']}"
        add_log(f"开始第 {batch['index'] + 1}/{len(batches)} 批: {batch['base_name']}")
        work = copy.deepcopy(workflow)

        try:
            for entry in batch["entries"]:
                target_name = make_input_filename(batch["index"], entry["id"], entry["source"])
                filename = copy_to_input(entry["source"], comfy_input, target_name)
                set_nested_input(work, entry["id"], entry["field"], filename)
                add_log(f"节点 {entry['id']} 使用素材: {entry['source'].name} -> {filename}")

            for output in outputs:
                node = work.get(str(output.get("id")))
                if node:
                    node_inputs = node.setdefault("inputs", {})
                    if "filename_prefix" in node_inputs:
                        node_inputs["filename_prefix"] = prefix
                    elif output.get("field"):
                        node_inputs[output["field"]] = prefix
                    if "save_output" in node_inputs:
                        node_inputs["save_output"] = True

            since_time = time.time()
            result = comfy_request(server_url, "/prompt", {"prompt": work}, timeout=30)
            prompt_id = result.get("prompt_id")
            if not prompt_id:
                raise RuntimeError(f"ComfyUI 没有返回 prompt_id: {result}")

            add_log(f"已提交任务 {prompt_id}")
            history_item = None
            while True:
                time.sleep(poll_interval)
                with STATE_LOCK:
                    if RUN_STATE["stop_requested"]:
                        add_log("收到停止请求，当前任务完成后将退出", "warn")
                history = comfy_request(server_url, f"/history/{prompt_id}", timeout=30)
                if prompt_id in history:
                    history_item = history[prompt_id]
                    break
                add_log(f"任务 {prompt_id} 仍在运行")

            files = collect_output_files(history_item or {}, comfy_output, prefix, since_time)
            if not files:
                self_saved_outputs = [item for item in outputs if item.get("self_saved")]
                if self_saved_outputs:
                    names = ", ".join(f"#{item.get('id')} {item.get('class_type')}" for item in self_saved_outputs)
                    with STATE_LOCK:
                        RUN_STATE["success"] += 1
                    add_log(f"任务完成，结果由工作流保存器保存: {names}", "success")
                else:
                    raise RuntimeError("任务完成，但没有找到可搬运的输出文件")
            else:
                moved = move_outputs(files, output_dir, batch["base_name"])
                with STATE_LOCK:
                    RUN_STATE["success"] += 1
                add_log("已保存: " + " | ".join(str(path) for path in moved), "success")
        except Exception as exc:
            with STATE_LOCK:
                RUN_STATE["failed"] += 1
            add_log(f"第 {batch['index'] + 1} 批失败: {exc}", "error")

    with STATE_LOCK:
        success = RUN_STATE["success"]
        failed = RUN_STATE["failed"]
    finish_run_state(f"完成: 成功 {success}，失败 {failed}")


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/config")
def api_get_config():
    return jsonify(load_config())


@app.post("/api/config")
def api_save_config():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(save_config(data))


@app.post("/api/analyze-workflow")
def api_analyze_workflow():
    workflow_name = ""
    workflow_path = ""

    try:
        if "workflow" in request.files:
            file = request.files["workflow"]
            workflow_name = file.filename
            workflow = json.loads(file.read().decode("utf-8"))
        else:
            data = request.get_json(force=True, silent=True) or {}
            workflow_path = str(data.get("workflow_path", "")).strip().strip('"')
            if not workflow_path:
                return jsonify({"error": "请选择或填写工作流 JSON"}), 400
            workflow_name = Path(workflow_path).name
            workflow = safe_json_load(workflow_path)
    except Exception as exc:
        return jsonify({"error": f"工作流读取失败: {exc}"}), 400

    workflow_id = uuid.uuid4().hex
    WORKFLOW_CACHE[workflow_id] = workflow
    analysis = analyze_workflow(workflow)
    config = load_config()
    if workflow_path:
        config["last_workflow_path"] = workflow_path
        save_config(config)
    return jsonify({"workflow_id": workflow_id, "workflow_name": workflow_name, **analysis})


@app.post("/api/run-batch")
def api_run_batch():
    data = request.get_json(force=True, silent=True) or {}
    with STATE_LOCK:
        if RUN_STATE["running"]:
            return jsonify({"error": "已有任务正在运行"}), 409
    total_hint = 0
    reset_run_state(total_hint)
    thread = threading.Thread(target=run_worker, args=(data,), daemon=True)
    thread.start()
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    with STATE_LOCK:
        snapshot = {
            "running": RUN_STATE["running"],
            "stop_requested": RUN_STATE["stop_requested"],
            "started_at": RUN_STATE["started_at"],
            "finished_at": RUN_STATE["finished_at"],
            "total": RUN_STATE["total"],
            "current": RUN_STATE["current"],
            "success": RUN_STATE["success"],
            "failed": RUN_STATE["failed"],
            "message": RUN_STATE["message"],
            "logs": list(RUN_STATE["logs"]),
        }
    return jsonify(snapshot)


@app.post("/api/stop")
def api_stop():
    with STATE_LOCK:
        RUN_STATE["stop_requested"] = True
    add_log("已请求停止后续任务", "warn")
    return jsonify({"ok": True})


@app.post("/api/check-comfy")
def api_check_comfy():
    data = request.get_json(force=True, silent=True) or {}
    config = data.get("config", {})
    root = Path(config.get("comfy_root") or DEFAULT_CONFIG["comfy_root"])
    server_url = config.get("server_url") or DEFAULT_CONFIG["server_url"]
    result = {
        "input_exists": (root / "input").exists(),
        "output_exists": (root / "output").exists(),
        "server_ok": False,
        "message": "",
    }
    try:
        comfy_request(server_url, "/system_stats", timeout=5)
        result["server_ok"] = True
        result["message"] = "ComfyUI 连接正常"
    except Exception as exc:
        result["message"] = f"ComfyUI 未连接: {exc}"
    return jsonify(result)


@app.post("/api/pick")
def api_pick():
    data = request.get_json(force=True, silent=True) or {}
    kind = data.get("kind", "folder")
    initial = data.get("initial") or str(BASE_DIR)
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if kind == "file":
            path = filedialog.askopenfilename(initialdir=initial)
        elif kind == "workflow":
            path = filedialog.askopenfilename(
                initialdir=initial,
                filetypes=[("ComfyUI API Workflow", "*.json"), ("All files", "*.*")],
            )
        else:
            path = filedialog.askdirectory(initialdir=initial)
        root.destroy()
        return jsonify({"path": path or ""})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def main():
    config = save_config(load_config())
    port = int(os.environ.get("COMFY_BATCH_PORT", "7869"))
    url = f"http://127.0.0.1:{port}"
    print("=" * 64)
    print("FR_工作流通用批量工具")
    print(f"打开地址: {url}")
    print(f"ComfyUI 路径: {config['comfy_root']}")
    print("按 Ctrl+C 可停止工具")
    print("=" * 64)
    if os.environ.get("COMFY_BATCH_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    server = make_server("127.0.0.1", port, app, threaded=True, request_handler=QuietRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nTool stopped.")


if __name__ == "__main__":
    main()
