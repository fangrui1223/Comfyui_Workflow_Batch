import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    import websocket
except ImportError:
    websocket = None


class JobSetupError(Exception):
    """A problem in the job configuration that would also affect later items."""


class ItemFailed(Exception):
    """The current item definitively failed."""


class NeedsReview(Exception):
    """ComfyUI may have accepted the request or saved partial results."""


class WorkerStopped(Exception):
    pass


def comfy_request(server_url, route, payload=None, timeout=8):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = Request(server_url.rstrip("/") + route, data=data, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _node_errors(data):
    errors = data.get("node_errors") or {}
    return "，涉及节点 " + "、".join(f"#{node_id}" for node_id in list(errors)[:12]) if errors else ""


def readable_reason(value):
    text = " ".join(str(value or "").splitlines()).strip()
    if re.search(r"api.?key|authorization|bearer |access.?token|password|secret|https?://|sk-[\w-]+",
                 text, flags=re.IGNORECASE):
        return ""
    return text[:200]


class ComfyClient:
    def __init__(self, config, stopped, log, request=comfy_request):
        self.url = config["server_url"]
        self.stopped = stopped
        self.log = log
        self.request = request
        self.poll = min(2.0, max(0.5, float(config.get("poll_interval") or 1)))
        self.missing_limit = 15
        self.wake = threading.Event()

    def call(self, route, payload=None, timeout=8):
        return self.request(self.url, route, payload, timeout)

    def submit(self, workflow, attempt):
        try:
            result = self.call("/prompt", {
                "prompt": workflow,
                "prompt_id": attempt,
                "client_id": attempt,
                "extra_data": {"fr_batch_attempt": attempt},
            }, timeout=30)
        except HTTPError as error:
            if error.code in {400, 401, 403, 404, 422}:
                try:
                    data = json.load(error)
                except (ValueError, OSError):
                    data = {}
                finally:
                    error.close()
                if not isinstance(data, dict):
                    data = {}
                reason = data.get("error")
                if isinstance(reason, dict):
                    reason = reason.get("message") or reason.get("type")
                raise JobSetupError(f"ComfyUI 拒绝该工作流（HTTP {error.code}）"
                                    f" {readable_reason(reason)}{_node_errors(data)}") from None
            error.close()
            raise
        if not isinstance(result, dict) or not isinstance(result.get("prompt_id"), str):
            raise OSError("未收到明确的提交编号")
        return result["prompt_id"]

    @contextmanager
    def notifications(self, client_id):
        closed = threading.Event()
        if websocket is not None and not self.stopped.is_set():
            threading.Thread(target=self._listen, args=(client_id, closed), daemon=True).start()
        try:
            yield
        finally:
            closed.set()
            self.wake.set()

    def _listen(self, client_id, closed):
        address = urlsplit(self.url)
        url = urlunsplit(("wss" if address.scheme == "https" else "ws", address.netloc,
                          address.path.rstrip("/") + "/ws", urlencode({"clientId": client_id}), ""))
        connection = None
        try:
            connection = websocket.create_connection(url, timeout=1.2)
            while not closed.is_set() and not self.stopped.is_set():
                try:
                    message = connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if message == "":
                    return
                if isinstance(message, str):
                    event = json.loads(message)
                    if event.get("type") in {"execution_success", "execution_error", "execution_interrupted"}:
                        self.wake.set()
        except (OSError, ValueError, websocket.WebSocketException):
            # History polling is always available, including for older servers.
            return
        finally:
            if connection is not None:
                connection.close(timeout=0.2)

    @staticmethod
    def _belongs(record, prompt_id, attempt):
        if not isinstance(record, (tuple, list)) or len(record) < 4:
            return False
        data = record[3]
        return record[1] == prompt_id or (isinstance(data, dict) and data.get("fr_batch_attempt") == attempt)

    def await_history(self, prompt_id, attempt, record_id):
        missing_since = None
        next_queue_check = 0.0
        backoff = self.poll
        interrupted = False
        while not self.stopped.is_set():
            try:
                history = self.call("/history/" + prompt_id)
                if not isinstance(history, dict):
                    raise OSError("历史接口返回了无效结果")
                if prompt_id in history:
                    return history[prompt_id]
                now = time.monotonic()
                if now >= next_queue_check:
                    next_queue_check = now + 4
                    queue = self.call("/queue")
                    if not isinstance(queue, dict) or not {"queue_running", "queue_pending"} <= queue.keys():
                        raise OSError("队列接口返回了无效结果")
                    records = queue["queue_running"] + queue["queue_pending"]
                    match = next((row for row in records if self._belongs(row, prompt_id, attempt)), None)
                    if match:
                        if match[1] != prompt_id:
                            prompt_id = match[1]
                            record_id(prompt_id)
                        missing_since = None
                    else:
                        # Legacy servers may allocate an ID before a lost POST reply.
                        if prompt_id == attempt:
                            recent = self.call("/history?max_items=30")
                            if isinstance(recent, dict):
                                for candidate, result in recent.items():
                                    if isinstance(result, dict) and self._belongs(result.get("prompt"), prompt_id, attempt):
                                        record_id(candidate)
                                        return result
                        if missing_since is None:
                            missing_since = now
                        elif now - missing_since >= self.missing_limit:
                            raise NeedsReview("ComfyUI 可连接，但该次提交已不在队列或历史中，请确认成品后重跑或跳过")
                if interrupted:
                    self.log("与 ComfyUI 的连接已恢复", "info")
                interrupted = False
                backoff = self.poll
            except (OSError, ValueError) as error:
                if not interrupted:
                    self.log(f"等待 ComfyUI 重新连接（{type(error).__name__}）", "warn")
                interrupted = True
                missing_since = None
                backoff = min(15, backoff * 2)
            self.wake.wait(backoff)
            self.wake.clear()
        raise WorkerStopped


def _safe_child(root, path):
    root, path = root.resolve(), path.resolve()
    if not path.is_relative_to(root) or path == root:
        raise NeedsReview(f"成品路径超出配置的输出目录: {path}")
    return path


def paths_for(config):
    root = Path(config["comfy_root"])
    return (Path(config.get("comfy_input_dir") or root / "input"),
            Path(config.get("comfy_output_dir") or root / "output"))


def _direct_layout(job, attempt):
    stage_root = Path(job["output_dir"]) / ".fr-batch-staging" / attempt
    result = {}
    for output in job["outputs"]:
        if output["mode"] != "direct":
            continue
        key = (output["binding"]["id"], output["binding"]["field"])
        if key not in result:
            result[key] = stage_root / f"direct_{len(result) + 1:02d}"
    return result


def input_stage_path(folder, attempt, index, source):
    name = f"FRBatch_{attempt.replace('-', '')}_{index:02d}_{source.name[-135:]}"
    return folder / name


def prepare_prompt(job, item, attempt):
    graph = copy.deepcopy(job["workflow"])
    comfy_input, comfy_output = paths_for(job["config"])
    if any(entry["access"] == "upload" for entry in item["entries"]) and not comfy_input.is_dir():
        raise JobSetupError(f"ComfyUI input 目录不存在: {comfy_input}")
    if any(output["mode"] == "history" for output in job["outputs"]) and not comfy_output.is_dir():
        raise JobSetupError(f"ComfyUI output 目录不存在: {comfy_output}")
    Path(job["output_dir"]).mkdir(parents=True, exist_ok=True)
    for index, entry in enumerate(item["entries"], start=1):
        source = Path(entry["source"])
        access = entry["access"]
        valid = source.is_dir() if access == "directory" else source.is_file()
        if not valid:
            raise ItemFailed(f"素材路径已移除或类型改变: {source}")
        value = str(source)
        if access == "upload":
            target = input_stage_path(comfy_input, attempt, index, source)
            shutil.copy2(source, target)
            value = target.name
        graph[entry["id"]]["inputs"][entry["field"]] = value
    for (node_id, field), directory in _direct_layout(job, attempt).items():
        directory.mkdir(parents=True, exist_ok=True)
        graph[node_id]["inputs"][field] = str(directory)
    for output in job["outputs"]:
        if output["mode"] == "history" and output["field"]:
            node = graph[output["id"]]
            node["inputs"][output["field"]] = f"FRBatch_{attempt}_{item['base_name']}"
    return graph


def cleanup_staging(job, item, attempt):
    """Delete only the upload files and empty directories assigned to this attempt."""
    comfy_input, _ = paths_for(job["config"])
    for index, entry in enumerate(item["entries"], start=1):
        if entry["access"] == "upload":
            try:
                input_stage_path(comfy_input, attempt, index, Path(entry["source"])).unlink(missing_ok=True)
            except OSError:
                pass
    for folder in _direct_layout(job, attempt).values():
        if not folder.is_dir():
            continue
        directories = sorted((path for path in folder.rglob("*") if path.is_dir() and not path.is_symlink()),
                             key=lambda path: -len(path.parts))
        for directory in [*directories, folder, folder.parent, folder.parent.parent]:
            try:
                directory.rmdir()
            except OSError:
                pass


def _reported_files(history, node_id, root):
    outputs = history.get("outputs") or {}
    metadata = history.get("meta") or {}
    found = []
    for runtime_id, output in outputs.items():
        origin = metadata.get(runtime_id) or {}
        if node_id not in {runtime_id, str(origin.get("display_node")), str(origin.get("real_node_id"))}:
            continue
        if not isinstance(output, dict):
            continue
        for records in output.values():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict) or not record.get("filename") or record.get("type", "output") != "output":
                    continue
                relative = Path(record.get("subfolder") or "") / record["filename"]
                full_path = record.get("fullpath")
                if full_path and Path(full_path).is_absolute() and Path(full_path).resolve().is_relative_to(root.resolve()):
                    found.append(_safe_child(root, Path(full_path)))
                else:
                    found.append(_safe_child(root, root / relative))
    return list(dict.fromkeys(found))


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique_destination(folder, name, reserved, source):
    dest = folder / name
    counter = 2
    while dest in reserved or (dest.exists() and dest != source):
        dest = folder / f"{Path(name).stem}_{counter}{Path(name).suffix}"
        counter += 1
    reserved.add(dest)
    return dest


def output_manifest(job, item, attempt, history):
    if not isinstance(history, dict):
        raise NeedsReview("ComfyUI 历史记录格式无效")
    status = history.get("status") or {}
    if status.get("status_str") == "error" or status.get("completed") is False:
        events = status.get("messages") or []
        details = next((entry[1] for entry in reversed(events)
                        if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], dict)
                        and entry[0] in {"execution_error", "execution_interrupted"}), {})
        node_id = details.get("node_id")
        node_type = details.get("node_type")
        context = f" · #{node_id} {node_type or ''}" if node_id is not None else ""
        raise ItemFailed(f"ComfyUI 执行失败{context}"
                         + "".join(f" · {text}" for text in (
                             readable_reason(details.get("exception_type")),
                             readable_reason(details.get("exception_message")),
                         ) if text))
    if status.get("status_str") != "success":
        raise NeedsReview("ComfyUI 未报告本次工作流成功结束")
    output_dir = Path(job["output_dir"]).resolve()
    _, comfy_output = paths_for(job["config"])
    reported = []
    for output in job["outputs"]:
        if output["mode"] != "history":
            continue
        files = _reported_files(history, output["id"], comfy_output)
        if not files:
            raise NeedsReview(f"在本次历史中找不到保存节点 #{output['id']} 的成品")
        reported.extend(files)
    reported = list(dict.fromkeys(reported))
    dests = set()
    pairs = []
    for index, source in enumerate(reported, start=1):
        suffix = f"_{index:02d}" if len(reported) > 1 else ""
        filename = f"{item['base_name']}{suffix}{source.suffix}"
        pairs.append((source, _unique_destination(output_dir, filename, dests, source)))
    for stage in _direct_layout(job, attempt).values():
        files = sorted((_safe_child(stage, path) for path in stage.rglob("*")
                        if path.is_file() and path.suffix.lower() not in {".part", ".tmp"}), key=str)
        if len(files) < (item.get("expected_direct") or 1):
            raise NeedsReview(f"直接保存的成品数量不足（找到 {len(files)} 个）: {stage}")
        for source in files:
            destination_folder = (output_dir / source.relative_to(stage).parent).resolve()
            if not destination_folder.is_relative_to(output_dir):
                raise NeedsReview(f"目标目录超出配置路径: {destination_folder}")
            pairs.append((source, _unique_destination(destination_folder, source.name, dests, source)))
    manifest = []
    for source, destination in pairs:
        if not source.is_file() or not (size := source.stat().st_size):
            raise NeedsReview(f"成品尚未落盘或为空: {source}")
        manifest.append({"source": str(source), "destination": str(destination),
                         "size": size, "sha256": file_digest(source)})
    if not manifest:
        raise NeedsReview("找不到该次工作的成品")
    return manifest


def _matches(path, record):
    return path.is_file() and path.stat().st_size == record["size"] and file_digest(path) == record["sha256"]


def deliver_manifest(manifest, attempt, stopped):
    for index, record in enumerate(manifest, start=1):
        if stopped.is_set():
            raise WorkerStopped
        source, dest = Path(record["source"]), Path(record["destination"])
        if not dest.exists():
            if not source.is_file() or source.stat().st_size != record["size"]:
                raise NeedsReview(f"待交付成品不存在或大小变化: {source}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            stage = dest.parent / f".frbatch-{attempt}-{index}.part"
            with source.open("rb") as incoming, stage.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if not _matches(stage, record):
                raise NeedsReview(f"成品交付时校验失败: {source}")
            try:
                if os.name == "nt":
                    stage.rename(dest)
                else:
                    os.link(stage, dest)
                    stage.unlink()
            except FileExistsError:
                if not _matches(dest, record):
                    raise NeedsReview(f"交付目标已被其他文件占用: {dest}") from None
                stage.unlink(missing_ok=True)
        elif not _matches(dest, record):
            raise NeedsReview(f"已有成品与该次提交的校验记录不符: {dest}")
        if source != dest and source.exists() and _matches(source, record):
            source.unlink()
    return [record["destination"] for record in manifest]
