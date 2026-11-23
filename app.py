import json
import logging
import os
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.serving import WSGIRequestHandler, make_server

from batch_queue import BatchWorker, QueueConflict, QueueStore
from comfy_execution import comfy_request, paths_for
from workflow_batch import WorkflowGraph, checked_workflow, local_path, normalize_bindings, path_value, plan_batches

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_CONFIG = {
    "comfy_root": r"C:\FR_comfyui",
    "python_path": r"C:\FR_comfyui\python\python.exe",
    "server_url": "http://127.0.0.1:8188",
    "comfy_input_dir": "",
    "comfy_output_dir": "",
    "last_workflow_path": "",
    "last_output_dir": "",
    "last_input_dir": "",
    "poll_interval": 1,
}


class QuietRequestHandler(WSGIRequestHandler):
    def log(self, kind, message, *args):
        if kind == "error":
            super().log(kind, message, *args)


class Recent:
    def __init__(self, capacity=24):
        self.capacity = capacity
        self.values = OrderedDict()
        self.lock = threading.Lock()

    def put(self, key, value):
        with self.lock:
            self.values[key] = value
            self.values.move_to_end(key)
            if len(self.values) > self.capacity:
                self.values.popitem(last=False)

    def get(self, key):
        with self.lock:
            return self.values.get(key)


def load_config(path):
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        return {**DEFAULT_CONFIG, **{key: config[key] for key in DEFAULT_CONFIG if key in config}}
    except (OSError, ValueError, TypeError):
        return dict(DEFAULT_CONFIG)


def merge_config(old, updates):
    if not isinstance(updates, dict):
        raise ValueError("环境设置格式无效")
    return {key: updates.get(key, old[key]) for key in DEFAULT_CONFIG}


def write_config(path, config):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def server_address(config):
    address = str(config["server_url"] or "").strip().rstrip("/")
    split = urlsplit(address)
    if split.scheme not in {"http", "https"} or not split.hostname or split.query or split.fragment:
        raise ValueError("ComfyUI 服务地址需要是 http 或 https 地址")
    return address


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("请求格式无效")
    return value


def create_app(config_path=None, db_path=None, transport=comfy_request):
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    config_path = Path(config_path or os.environ.get("COMFY_BATCH_CONFIG", BASE_DIR / "config.json"))
    db_path = Path(db_path or os.environ.get("COMFY_BATCH_DB", BASE_DIR / "batch_queue.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = QueueStore(db_path)
    worker = BatchWorker(store, request=transport)
    workflows = Recent()
    previews = Recent()
    metadata_cache = Recent(5)
    config_lock = threading.Lock()
    app.extensions["batch_worker"] = worker
    app.extensions["batch_store"] = store

    def metadata(address, reload=False):
        cached = metadata_cache.get(address)
        if not reload and cached and time.monotonic() - cached[0] < 120:
            return cached[1], None
        try:
            values = transport(address, "/object_info", None, 3.5)
            if not isinstance(values, dict) or not values:
                raise OSError
        except (OSError, ValueError):
            return {}, "暂未取得 ComfyUI 节点信息，已按工作流结构分析。预览时会重新检查服务"
        metadata_cache.put(address, (time.monotonic(), values))
        return values, None

    def update_config(updates):
        with config_lock:
            config = merge_config(load_config(config_path), updates)
            write_config(config_path, config)
            return config

    def build_preview(data):
        draft = workflows.get(str(data.get("workflow_id", "")))
        if not draft:
            raise ValueError("请先上传本次任务的 API 工作流")
        config = merge_config(load_config(config_path), data.get("config", {}))
        config["server_url"] = server_address(config)
        config["comfy_root"] = str(local_path(config["comfy_root"], "ComfyUI 根目录"))
        for key, label in (("comfy_input_dir", "ComfyUI input 目录"), ("comfy_output_dir", "ComfyUI output 目录")):
            if path_value(config[key]):
                config[key] = str(local_path(config[key], label))
        try:
            config["poll_interval"] = min(2.0, max(0.5, float(config["poll_interval"] or 1)))
        except (ValueError, TypeError):
            config["poll_interval"] = 1
        output_dir = local_path(data.get("output_dir") or config["last_output_dir"], "成品保存目录")
        if output_dir.exists() and not output_dir.is_dir():
            raise ValueError(f"成品保存的位置是文件: {output_dir}")
        warning = None
        analysis = draft["analysis"]
        if config["server_url"] != draft["server"] or draft["metadata_warning"]:
            values, warning = metadata(config["server_url"])
            if values:
                analysis = WorkflowGraph(draft["workflow"], values).analyze()
        if analysis["missing_types"] and not warning:
            raise ValueError("目标 ComfyUI 缺少以下节点: " + "、".join(analysis["missing_types"][:14]))
        if analysis["connection_warnings"] and not warning:
            raise ValueError("；".join(analysis["connection_warnings"][:8]))
        for field in ("inputs", "outputs"):
            bindings = data.get(field, [])
            if not isinstance(bindings, list) or not all(isinstance(item, dict) for item in bindings):
                raise ValueError("节点绑定格式无效")
        inputs, outputs = normalize_bindings(draft["workflow"], analysis, data)
        comfy_input, comfy_output = paths_for(config)
        if any(item["access"] == "upload" for item in inputs) and not comfy_input.is_dir():
            raise ValueError(f"找不到 ComfyUI input 目录: {comfy_input}")
        if any(item["mode"] == "history" for item in outputs) and not comfy_output.is_dir():
            raise ValueError(f"找不到 ComfyUI output 目录: {comfy_output}")
        items, unmatched = plan_batches(inputs, draft["workflow"], outputs)
        snapshot = {"workflow": draft["workflow"], "config": config, "outputs": outputs, "output_dir": str(output_dir)}
        return {"name": draft["name"], "snapshot": snapshot, "items": items,
                "unmatched": unmatched, "warning": warning}

    @app.errorhandler(ValueError)
    def invalid_request(error):
        return jsonify(error=str(error)), 409 if isinstance(error, QueueConflict) else 400

    @app.errorhandler(OSError)
    def filesystem_error(error):
        return jsonify(error=f"访问本地文件或服务失败: {error}"), 400

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/config")
    def get_config():
        return jsonify(load_config(config_path))

    @app.post("/api/config")
    def save_config():
        return jsonify(update_config(body()))

    @app.post("/api/analyze-workflow")
    def analyze():
        config = load_config(config_path)
        if "workflow" in request.files:
            file = request.files["workflow"]
            name = (file.filename or "工作流.json").replace("\\", "/").split("/")[-1]
            config["server_url"] = request.form.get("server_url") or config["server_url"]
            workflow = checked_workflow(json.loads(file.read().decode("utf-8-sig")))
        else:
            data = body()
            path = local_path(data.get("workflow_path"), "API 工作流 JSON 路径")
            name = path.name
            config = merge_config(config, data.get("config", {}))
            workflow = checked_workflow(json.loads(path.read_text(encoding="utf-8-sig")))
            update_config({"last_workflow_path": str(path)})
        address = server_address(config)
        values, warning = metadata(address)
        analysis = WorkflowGraph(workflow, values).analyze()
        identity = uuid.uuid4().hex
        workflows.put(identity, {"workflow": workflow, "analysis": analysis, "name": name,
                                 "metadata_warning": warning, "server": address})
        return jsonify(workflow_id=identity, workflow_name=name, warning=warning, **analysis)

    @app.post("/api/preview")
    def preview_batch():
        preview = build_preview(body())
        token = uuid.uuid4().hex
        previews.put(token, preview)
        rows = [{
            "position": index, "label": item["label"], "match": item["match"],
            "paths": [entry["source"] for entry in item["entries"]],
            "expected_direct": item.get("expected_direct"),
        } for index, item in enumerate(preview["items"], start=1)]
        return jsonify(preview_id=token, name=preview["name"], items=rows,
                       unmatched=preview["unmatched"], warning=preview["warning"])

    @app.post("/api/queue")
    def enqueue():
        data = body()
        token = str(data.get("preview_id") or "")
        existing = store.existing_job(token)
        if existing:
            return jsonify(job_id=existing)
        planned = previews.get(token)
        if not planned:
            raise ValueError("配对预览已失效，请重新预览")
        if planned["unmatched"] and data.get("accept_unmatched") is not True:
            raise ValueError("请补齐素材，或确认排除未匹配的路径后再入队")
        identity = store.enqueue(token, planned["name"], planned["snapshot"], planned["items"])
        worker.notify()
        return jsonify(job_id=identity)

    @app.post("/api/run-batch")
    def run_batch():
        data = body()
        planned = build_preview(data)
        if planned["unmatched"] and data.get("accept_unmatched") is not True:
            raise ValueError("存在未匹配的素材，请通过预览确认")
        identity = store.enqueue(uuid.uuid4().hex, planned["name"], planned["snapshot"], planned["items"])
        store.set_enabled(True)
        worker.notify()
        return jsonify(ok=True, job_id=identity)

    @app.post("/api/queue/start")
    def start_queue():
        store.set_enabled(True)
        worker.notify()
        return jsonify(ok=True)

    @app.post("/api/queue/pause")
    @app.post("/api/stop")
    def pause_queue():
        store.set_enabled(False)
        worker.notify()
        return jsonify(ok=True)

    @app.get("/api/status")
    def status():
        return jsonify(store.snapshot())

    @app.get("/api/queue/<int:job_id>")
    def job_detail(job_id):
        items = store.job_items(job_id)
        if not items:
            return jsonify(error="找不到该工作流任务"), 404
        return jsonify(items=items)

    @app.post("/api/queue/items/<int:item_id>")
    def item_decision(item_id):
        store.resolve(item_id, body().get("decision"))
        worker.notify()
        return jsonify(ok=True)

    @app.post("/api/check-comfy")
    def check_comfy():
        config = merge_config(load_config(config_path), body().get("config", {}))
        comfy_input, comfy_output = paths_for(config)
        ok = True
        message = "ComfyUI 连接正常"
        try:
            transport(server_address(config), "/system_stats", None, 4)
        except (OSError, ValueError) as error:
            ok, message = False, f"ComfyUI 未连接: {error}"
        return jsonify(server_ok=ok, message=message, input_exists=comfy_input.is_dir(),
                       output_exists=comfy_output.is_dir())

    @app.post("/api/pick")
    def pick():
        import tkinter as tk
        from tkinter import filedialog

        data = body()
        kind = data.get("kind", "folder")
        initial = str(data.get("initial") or BASE_DIR)
        if Path(initial).is_file():
            initial = str(Path(initial).parent)
        root = tk.Tk()
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            if kind in {"workflow", "file"}:
                filters = [("ComfyUI API Workflow", "*.json"), ("所有文件", "*.*")] if kind == "workflow" else [("所有文件", "*.*")]
                chosen = filedialog.askopenfilename(parent=root, initialdir=initial, filetypes=filters)
            else:
                chosen = filedialog.askdirectory(parent=root, initialdir=initial)
            return jsonify(path=chosen or "")
        finally:
            root.destroy()

    return app


def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    port = int(os.environ.get("COMFY_BATCH_PORT", "7869"))
    url = f"http://127.0.0.1:{port}"
    app = create_app()
    server = make_server("127.0.0.1", port, app, threaded=True, request_handler=QuietRequestHandler)
    worker = app.extensions["batch_worker"]
    try:
        worker.start()
    except QueueConflict as error:
        server.server_close()
        raise SystemExit(str(error)) from None
    print(f"FR 工作流通用批量工具 · {url} · Ctrl+C 关闭工具", flush=True)
    if os.environ.get("COMFY_BATCH_NO_BROWSER") != "1":
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        server.server_close()


if __name__ == "__main__":
    main()
