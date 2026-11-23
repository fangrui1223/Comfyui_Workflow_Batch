import copy
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from batch_queue import BatchWorker, QueueConflict, QueueStore
from comfy_execution import deliver_manifest, output_manifest
from workflow_batch import WorkflowGraph, checked_workflow, plan_batches


def node(kind, title="", **fields):
    return {"class_type": kind, "_meta": {"title": title}, "inputs": fields}


def file_workflow():
    return {
        "A": node("LoadImage", "参考图", image="previous.png"),
        "B": node("VHS_LoadVideo", "动作", video="previous.mp4"),
        "C": node("RenderFrames", image=["A", 0], video=["B", 0]),
        "D": node("VHS_VideoCombine", filename_prefix="test", images=["C", 0], save_output=True),
        "E": node("VHS_VideoCombine", filename_prefix="test", images=["C", 0], save_output=False),
    }


def directory_workflow():
    return {
        "1": node("PrimitiveString", value="A"),
        "2": node("StringPassThrough", value=["1", 0]),
        "3": node("PrimitiveString", value="B"),
        "4": node("ImageDirectoryReader", directory=["2", 0], current_folder_only=True),
        "5": node("ImageDirectoryReader", directory=["3", 0], current_folder_only=True),
        "6": node("MergePaths", a=["4", 0], b=["5", 0]),
        "7": node("PrimitiveString", "保存路径", value="C"),
        "8": node("SaveImageDirectory", output_dir=["7", 0], images=["6", 0]),
    }


def renumber(workflow):
    ids = {key: str(901 + i * 13) for i, key in enumerate(reversed(workflow))}
    changed = {}
    for key, value in workflow.items():
        result = copy.deepcopy(value)
        for field, value in result["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and value[0] in ids:
                result["inputs"][field] = [ids[value[0]], value[1]]
        changed[ids[key]] = result
    return changed, ids


class FakeComfy:
    def __init__(self, root):
        self.root = root
        self.prompts = {}
        self.records = {}
        self.submissions = []
        self.reject_outputs = False
        self.offline = False
        self.lost_response = False
        self.fail_next = False
        self.wait_for_release = False
        self.lock = threading.RLock()

    def release(self, attempt):
        with self.lock:
            workflow = self.prompts[attempt]
            outputs, graph_meta = {}, {}
            if "D" in workflow:
                images = workflow["D"]["inputs"]
                if not self.reject_outputs:
                    name = images["filename_prefix"] + ".mp4"
                    (self.root / "output" / name).write_bytes(b"new video")
                    outputs["D:expanded"] = {"gifs": [{"filename": name, "type": "output"}]}
                    graph_meta["D:expanded"] = {"display_node": "D"}
                    (self.root / "output" / "other.mp4").write_bytes(b"unrelated job")
                    outputs["E"] = {"gifs": [{"filename": "other.mp4", "type": "temp"}]}
            else:
                root = Path(workflow["7"]["inputs"]["value"])
                source = Path(workflow["1"]["inputs"]["value"])
                for file in source.glob("*.jpg"):
                    target = root / source.name / file.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"face result")
            self.records[attempt] = {"status": {"status_str": "success", "completed": True},
                                     "outputs": outputs, "meta": graph_meta}

    def call(self, _server, route, payload=None, timeout=8):
        with self.lock:
            if self.offline:
                raise OSError("temporary test outage")
            if route == "/object_info":
                return {}
            if route == "/system_stats":
                return {"system": {"comfyui_version": "test"}}
            if route == "/prompt":
                attempt = payload["prompt_id"]
                self.prompts[attempt] = payload["prompt"]
                self.submissions.append(attempt)
                if self.fail_next:
                    self.fail_next = False
                    self.records[attempt] = {"status": {"status_str": "error", "completed": False}}
                elif not self.wait_for_release:
                    self.release(attempt)
                if self.lost_response:
                    raise OSError("POST response lost after acceptance")
                return {"prompt_id": attempt}
            if route == "/queue":
                return {"queue_pending": [], "queue_running": [
                    [0, attempt, self.prompts[attempt], {"fr_batch_attempt": attempt}]
                    for attempt in self.prompts if attempt not in self.records
                ]}
            if route.startswith("/history/"):
                attempt = route.rsplit("/", 1)[1]
                return {attempt: self.records[attempt]} if attempt in self.records else {}
            if route.startswith("/history?"):
                return self.records
            raise AssertionError(route)


class BatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="fr-batch-check-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("input", "output", "results", "left", "right"):
            (self.root / name).mkdir()
        self.db_path = self.root / "queue.sqlite3"
        self.config_path = self.root / "config.json"
        self.config = {
            "comfy_root": str(self.root), "server_url": "http://127.0.0.1:18542",
            "poll_interval": 0.5, "last_output_dir": str(self.root / "results"),
        }
        self.fake = FakeComfy(self.root)
        notification = patch("comfy_execution.websocket", None)
        notification.start()
        self.addCleanup(notification.stop)
        self.setup_app()

    def setup_app(self):
        self.app = create_app(self.config_path, self.db_path, self.fake.call)
        self.client = self.app.test_client()
        self.worker = self.app.extensions["batch_worker"]
        self.store = self.app.extensions["batch_store"]
        self.addCleanup(self.stop_worker, self.worker)
        self.post("/api/config", self.config)

    @staticmethod
    def stop_worker(worker):
        worker.stop()
        if worker.thread:
            worker.thread.join(3)
            if worker.thread.is_alive():
                raise AssertionError("测试工作线程未及时停止")

    def wait(self, predicate, timeout=8):
        stop = time.monotonic() + timeout
        while time.monotonic() < stop:
            if result := predicate():
                return result
            time.sleep(0.03)
        self.fail("等待超时: " + repr(self.store.snapshot()))

    def post(self, endpoint, payload=None, code=200):
        response = self.client.post(endpoint, json=payload)
        self.assertEqual(response.status_code, code, response.get_json())
        return response.get_json()

    def upload(self, workflow, filename="test.json"):
        response = self.client.post("/api/analyze-workflow", data={
            "workflow": (io.BytesIO(json.dumps(workflow).encode()), filename),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def preview(self, workflow, left=None, right=None, filename="test.json", output=None):
        draft = self.upload(workflow, filename)
        inputs = [item for item in draft["inputs"] if item["enabled"]]
        self.assertEqual(len(inputs), 2, draft)
        for item, path in zip(inputs, (left or self.root / "left", right or self.root / "right")):
            item["source_path"] = str(path)
        return self.post("/api/preview", {
            "workflow_id": draft["workflow_id"], "config": self.config,
            "inputs": inputs, "outputs": draft["outputs"],
            "output_dir": str(output or self.root / "results"),
        })

    def enqueue(self, preview, accept=False):
        return self.post("/api/queue", {"preview_id": preview["preview_id"],
                                        "accept_unmatched": accept})["job_id"]

    def files(self, count):
        for number in range(1, count + 1):
            (self.root / "left" / f"{number}.jpg").write_bytes(b"image")
            (self.root / "right" / f"{number}.mp4").write_bytes(b"video")

    def groups(self, names_a, names_b, count=1):
        for side, names in (("left", names_a), ("right", names_b)):
            for name in names:
                folder = self.root / side / str(name)
                folder.mkdir()
                for number in range(count if side == "left" else 1):
                    (folder / f"{number}.jpg").write_bytes(b"image")

    def test_different_numbering_and_schema(self):
        known = directory_workflow()
        new, identifiers = renumber(known)
        for graph, expected in ((known, ["1", "3", "8"]), (new, [identifiers[k] for k in ["1", "3", "8"]])):
            result = WorkflowGraph(graph).analyze()
            self.assertEqual([src["id"] for src in result["inputs"] if src["enabled"]], expected[:2])
            self.assertEqual([src["id"] for src in result["outputs"] if src["enabled"]], [expected[2]])
            self.assertEqual([item["source_mode"] for item in result["inputs"]], ["groups", "groups"])
        metadata = {"MyImporter": {"input": {"required": {"source": ["STRING", {"audio_upload": True}]}},
                                   "output": ["AUDIO"]},
                    "MyPublisher": {"input": {"required": {"prefix": ["STRING", {}]}},
                                    "output": [], "output_node": True}}
        result = WorkflowGraph({"x": node("MyImporter", source="old.wav"),
                                "y": node("MyPublisher", prefix="batch", media=["x", 0])}, metadata).analyze()
        self.assertEqual(result["inputs"][0]["role"], "audio")
        self.assertEqual(result["outputs"][0]["id"], "y")
        with self.assertRaisesRegex(ValueError, "画布"):
            checked_workflow({"nodes": []})
        with self.assertRaisesRegex(ValueError, "无效连接"):
            checked_workflow({"x": node("Preview", values=["missing", 0])})

    def test_two_directories_sort_and_missing_preview(self):
        self.groups(["1", "2", "10", "11"], ["ref2", "ref10", "2"])
        preview = self.preview(directory_workflow())
        self.assertEqual([item["label"] for item in preview["items"]], ["1 / ref2", "2 / 2", "10 / ref10"])
        self.assertEqual([item["match"] for item in preview["items"]], ["自然顺序", "同名", "自然顺序"])
        self.assertTrue(preview["unmatched"][0].endswith("11"))
        self.post("/api/queue", {"preview_id": preview["preview_id"]}, 400)
        self.assertEqual(self.enqueue(preview, True), self.enqueue(preview, True))

    def test_file_batches_show_extra_asset(self):
        self.files(2)
        (self.root / "left" / "10.jpg").write_bytes(b"extra")
        preview = self.preview(file_workflow())
        self.assertEqual(len(preview["items"]), 2)
        self.assertEqual([Path(path).name for path in preview["unmatched"]], ["10.jpg"])

    def test_fixed_file_is_reused_and_known_failure_does_not_block_queue(self):
        self.files(2)
        draft = self.upload(file_workflow())
        enabled = [item for item in draft["inputs"] if item["enabled"]]
        enabled[0].update(source_path=str(self.root / "left" / "1.jpg"), source_mode="single")
        enabled[1].update(source_path=str(self.root / "right"), source_mode="folder")
        preview = self.post("/api/preview", {"workflow_id": draft["workflow_id"], "inputs": enabled,
                                             "outputs": draft["outputs"]})
        self.assertEqual(len(preview["items"]), 2)
        self.assertEqual([Path(item["paths"][0]).name for item in preview["items"]], ["1.jpg", "1.jpg"])
        identity = self.enqueue(preview)
        self.fake.fail_next = True
        self.worker.start()
        self.post("/api/queue/start")
        self.wait(lambda: self.store.job_items(identity)[1]["state"] == "done")
        self.assertEqual([row["state"] for row in self.store.job_items(identity)], ["failed", "done"])

    def test_pause_finish_current_then_restart_and_continue(self):
        self.groups([1, 2], [1, 2], 5)
        self.fake.wait_for_release = True
        job = self.enqueue(self.preview(directory_workflow()))
        self.worker.start()
        self.post("/api/queue/start")
        first = self.wait(lambda: self.fake.submissions and self.fake.submissions[0])
        self.post("/api/queue/pause")
        self.assertEqual(self.store.snapshot()["state"], "pausing")
        self.fake.release(first)
        self.wait(lambda: self.store.job_items(job)[0]["state"] == "done")
        self.assertEqual(self.store.snapshot()["state"], "paused")
        self.assertEqual(len(self.fake.submissions), 1)
        self.assertEqual(len(list((self.root / "results" / "1").glob("*.jpg"))), 5)
        self.stop_worker(self.worker)
        self.setup_app()
        self.worker.start()
        self.assertEqual(self.store.snapshot()["state"], "paused")
        self.post("/api/queue/start")
        self.wait(lambda: len(self.fake.submissions) == 2)
        self.fake.release(self.fake.submissions[-1])
        self.wait(lambda: all(row["state"] == "done" for row in self.store.job_items(job)))
        self.assertEqual(len(list((self.root / "results").rglob("*.jpg"))), 10)

    def test_recover_active_and_post_response_loss(self):
        self.files(2)
        self.fake.wait_for_release = True
        self.fake.lost_response = True
        job = self.enqueue(self.preview(file_workflow()))
        self.worker.start()
        self.post("/api/queue/start")
        first = self.wait(lambda: self.fake.submissions and self.fake.submissions[0])
        self.stop_worker(self.worker)
        self.setup_app()
        self.worker.start()
        self.fake.release(first)
        self.wait(lambda: self.store.job_items(job)[0]["state"] == "done")
        self.assertEqual(len(self.fake.submissions), 1)
        self.assertEqual(self.store.job_items(job)[0]["prompt_id"], first)
        self.assertEqual(self.store.job_items(job)[1]["state"], "pending")
        self.assertTrue((self.root / "output" / "other.mp4").exists())
        self.assertFalse(any(self.root.joinpath("input").iterdir()))

    def test_results_unknown_pause_queue_and_require_decision(self):
        self.files(2)
        self.fake.reject_outputs = True
        job = self.enqueue(self.preview(file_workflow()))
        self.worker.start()
        self.post("/api/queue/start")
        self.wait(lambda: self.store.snapshot()["state"] == "review")
        first, pending = self.store.job_items(job)
        self.assertEqual((first["state"], pending["state"]), ("review", "pending"))
        self.assertEqual(len(self.fake.submissions), 1)
        self.post("/api/queue/start", None, 409)
        self.post(f"/api/queue/items/{first['id']}", {"decision": "skip"})
        self.fake.reject_outputs = False
        self.post("/api/queue/start")
        self.wait(lambda: self.store.job_items(job)[1]["state"] == "done")
        self.assertEqual(len(self.fake.submissions), 2)

    def test_queue_one_hundred_groups_and_append(self):
        self.groups(range(1, 101), range(1, 101))
        one = self.enqueue(self.preview(directory_workflow(), filename="same.json"))
        self.worker.start()
        self.post("/api/queue/start")
        self.wait(lambda: bool(self.fake.submissions))
        self.files(2)
        second_output = self.root / "another"
        two = self.enqueue(self.preview(file_workflow(), filename="same.json", output=second_output))
        self.wait(lambda: all(row["state"] == "done" for row in self.store.job_items(two)), timeout=30)
        rows = self.store.job_items(one)
        self.assertEqual(len(rows), 100)
        self.assertTrue(all(row["state"] == "done" for row in rows))
        labels = [Path(self.fake.prompts[attempt]["1"]["inputs"]["value"]).name
                  for attempt in self.fake.submissions[:100]]
        self.assertEqual(labels, [str(n) for n in range(1, 101)])
        self.assertEqual(set(p.name for p in second_output.iterdir()), {"1.mp4", "2.mp4"})

    def test_one_worker_can_own_a_database(self):
        self.worker.start()
        duplicate = BatchWorker(QueueStore(self.db_path), self.fake.call)
        with self.assertRaisesRegex(QueueConflict, "另一个"):
            duplicate.start()
        self.stop_worker(self.worker)
        duplicate.start()
        self.stop_worker(duplicate)

    def test_manifest_can_be_replayed_without_source(self):
        item = {"base_name": "video"}
        job = {"config": self.config, "output_dir": str(self.root / "results"),
               "outputs": [{"id": "D", "mode": "history", "field": "filename_prefix"}]}
        old = self.root / "output" / "source.mp4"
        old.write_bytes(b"complete")
        history = {"status": {"status_str": "success", "completed": True},
                   "outputs": {"D": {"gifs": [{"filename": old.name, "type": "output"}]}}}
        manifest = output_manifest(job, item, "test", history)
        deliver_manifest(manifest, "test", threading.Event())
        self.assertFalse(old.exists())
        deliver_manifest(manifest, "test", threading.Event())
        self.assertEqual([p.name for p in (self.root / "results").iterdir()], ["video.mp4"])

    def test_restart_with_manifest_finishes_without_poll_or_submission(self):
        self.files(1)
        job_id = self.enqueue(self.preview(file_workflow()))
        self.store.set_enabled(True)
        work = self.store.get_work()
        self.assertTrue(self.store.begin_submission(work))
        path = self.root / "output" / "history.mp4"
        path.write_bytes(b"complete")
        manifest = output_manifest(
            work["snapshot"], work["plan"], work["attempt"],
            {"status": {"status_str": "success", "completed": True},
             "outputs": {"D": {"gifs": [{"filename": path.name, "type": "output"}]}}},
        )
        self.store.record_manifest(work, manifest)
        deliver_manifest(manifest, work["attempt"], threading.Event())
        self.setup_app()
        self.fake.offline = True
        self.worker.start()
        self.wait(lambda: self.store.job_items(job_id)[0]["state"] == "done")
        self.assertEqual(self.fake.submissions, [])
        self.assertFalse(path.exists())


class UserSampleTest(unittest.TestCase):
    def test_samples_if_available(self):
        folder = Path(os.environ.get("COMFY_BATCH_SAMPLE_DIR", Path.home() / "Downloads"))
        face = list(folder.glob("*直出版-批量 (1).json"))
        scail = list(folder.glob("*Scail-2*4.2 (1).json"))
        if len(face) != 1 or len(scail) != 1:
            self.skipTest("未找到用户的两份样例，通用夹具仍覆盖该流程")
        cases = [(face[0], ["660", "667"], "668", "669"), (scail[0], ["551", "573"], "119", None)]
        for file, input_ids, output_id, path_id in cases:
            graph = json.loads(file.read_text(encoding="utf-8-sig"))
            if path_id:
                with tempfile.TemporaryDirectory(prefix="fr-batch-sample-pair-") as directory:
                    root = Path(directory)
                    for side, count in (("left", 5), ("right", 1)):
                        d = root / side / "002"
                        d.mkdir(parents=True)
                        for i in range(count):
                            (d / f"{i}.jpg").touch()
                    analysis = WorkflowGraph(graph).analyze()
                    rows = [{**item, "source_path": str(root / side)}
                            for item, side in zip([x for x in analysis["inputs"] if x["enabled"]], ("left", "right"))]
                    plan, extra = plan_batches(rows, graph, [x for x in analysis["outputs"] if x["enabled"]])
                    self.assertEqual(plan[0]["expected_direct"], 5)
                    self.assertEqual(extra, [])
            remapped, ids = renumber(graph)
            for data, mapping in ((graph, {key: key for key in graph}), (remapped, ids)):
                result = WorkflowGraph(data).analyze()
                self.assertCountEqual([it["id"] for it in result["inputs"] if it["enabled"]],
                                      [mapping[key] for key in input_ids])
                outputs = [it for it in result["outputs"] if it["enabled"]]
                self.assertEqual([it["id"] for it in outputs], [mapping[output_id]])
                if path_id:
                    self.assertEqual(outputs[0]["binding"]["id"], mapping[path_id])


if __name__ == "__main__":
    unittest.main()
