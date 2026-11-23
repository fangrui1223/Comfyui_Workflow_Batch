import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from comfy_execution import (
    ComfyClient, ItemFailed, JobSetupError, NeedsReview, WorkerStopped,
    cleanup_staging, comfy_request, deliver_manifest, output_manifest, prepare_prompt,
)

IN_FLIGHT = ("preparing", "submitting", "running", "saving")
SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL
);
INSERT OR IGNORE INTO queue_settings VALUES (1, 0);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    position INTEGER NOT NULL,
    label TEXT NOT NULL,
    plan TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempt TEXT,
    prompt_id TEXT,
    manifest TEXT,
    message TEXT,
    started_at REAL,
    finished_at REAL,
    UNIQUE (job_id, position)
);
CREATE INDEX IF NOT EXISTS items_order ON items(state, job_id, position);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


class QueueConflict(ValueError):
    pass


def lock_database(path):
    lock_path = Path(path).with_name(Path(path).name + ".lock")
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise QueueConflict("这个队列数据库已由另一个批量工具打开") from error
    return handle


class QueueStore:
    def __init__(self, path):
        self.path = path
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                conn.execute("PRAGMA foreign_keys=ON")
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _event(conn, message, level="info"):
        conn.execute("INSERT INTO events(created_at, level, message) VALUES(?, ?, ?)",
                     (time.time(), level, str(message)[:1800]))

    def log(self, message, level="info"):
        with self.connection(write=True) as conn:
            self._event(conn, message, level)

    def startup(self):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE queue_settings SET enabled=0 WHERE id=1")
            if conn.execute("SELECT 1 FROM items WHERE state IN ('submitting','running','saving')").fetchone():
                self._event(conn, "已载入上次队列，正在核对此前提交的项目", "warn")

    def enabled(self, conn):
        return bool(conn.execute("SELECT enabled FROM queue_settings WHERE id=1").fetchone()[0])

    def set_enabled(self, enabled):
        with self.connection(write=True) as conn:
            if enabled and conn.execute("SELECT 1 FROM items WHERE state='review'").fetchone():
                raise QueueConflict("请先处理待确认项，再继续队列")
            if self.enabled(conn) == enabled:
                return
            conn.execute("UPDATE queue_settings SET enabled=? WHERE id=1", (int(enabled),))
            self._event(conn, "队列已开始或继续" if enabled else "已请求暂停，正在处理的提交保存后暂停")

    def existing_job(self, token):
        with self.connection() as conn:
            row = conn.execute("SELECT id FROM jobs WHERE token=?", (token,)).fetchone()
            return row[0] if row else None

    def enqueue(self, token, name, snapshot, items):
        with self.connection(write=True) as conn:
            existing = conn.execute("SELECT id FROM jobs WHERE token=?", (token,)).fetchone()
            if existing:
                return existing[0]
            job_id = conn.execute(
                "INSERT INTO jobs(token, name, snapshot, created_at) VALUES(?, ?, ?, ?)",
                (token, name, json.dumps(snapshot, ensure_ascii=False), time.time()),
            ).lastrowid
            conn.executemany(
                "INSERT INTO items(job_id, position, label, plan) VALUES(?, ?, ?, ?)",
                ((job_id, index, item["label"], json.dumps(item, ensure_ascii=False))
                 for index, item in enumerate(items, start=1)),
            )
            self._event(conn, f"已入队 · #{job_id} {name} · {len(items)} 项")
            return job_id

    def _active(self, conn):
        return conn.execute(
            "SELECT * FROM items WHERE state IN ('preparing','submitting','running','saving') ORDER BY id LIMIT 1"
        ).fetchone()

    def get_work(self):
        with self.connection(write=True) as conn:
            current = self._active(conn)
            if not current:
                if not self.enabled(conn):
                    return None
                current = conn.execute(
                    "SELECT * FROM items WHERE state='pending' ORDER BY job_id, position LIMIT 1"
                ).fetchone()
                if not current:
                    conn.execute("UPDATE queue_settings SET enabled=0 WHERE id=1")
                    self._event(conn, "本轮队列处理完成", "success")
                    return None
                attempt = str(uuid.uuid4())
                conn.execute(
                    "UPDATE items SET state='preparing', attempt=?, prompt_id=?, started_at=? WHERE id=?",
                    (attempt, attempt, time.time(), current["id"]),
                )
                current = conn.execute("SELECT * FROM items WHERE id=?", (current["id"],)).fetchone()
            job = conn.execute("SELECT name, snapshot FROM jobs WHERE id=?", (current["job_id"],)).fetchone()
            work = dict(current)
            work["plan"] = json.loads(work["plan"])
            work["manifest"] = json.loads(work["manifest"]) if work["manifest"] else None
            work["snapshot"] = json.loads(job["snapshot"])
            work["job_name"] = job["name"]
            return work

    def can_prepare(self):
        with self.connection() as conn:
            return self.enabled(conn)

    def requeue_preparing(self, item_id):
        with self.connection(write=True) as conn:
            conn.execute(
                """UPDATE items SET state='pending', attempt=NULL, prompt_id=NULL, started_at=NULL
                   WHERE id=? AND state='preparing'""", (item_id,)
            )

    def begin_submission(self, work):
        with self.connection(write=True) as conn:
            if not self.enabled(conn):
                return False
            result = conn.execute(
                "UPDATE items SET state='submitting' WHERE id=? AND state='preparing' AND attempt=?",
                (work["id"], work["attempt"]),
            )
            if not result.rowcount:
                return False
            self._event(conn, f"提交中 · {work_label(work)} · {work['attempt']}")
            return True

    def record_prompt(self, work, prompt_id):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE items SET state='running', prompt_id=? WHERE id=? AND attempt=?",
                         (prompt_id, work["id"], work["attempt"]))
            self._event(conn, f"跟踪 ComfyUI · {work_label(work)} · {prompt_id}")

    def record_manifest(self, work, manifest):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE items SET state='saving', manifest=? WHERE id=? AND attempt=?",
                         (json.dumps(manifest, ensure_ascii=False), work["id"], work["attempt"]))
            self._event(conn, f"成品已核对 · {work_label(work)} · {len(manifest)} 个文件")

    def finish(self, work, state, message, block_job=False):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE items SET state=?, message=?, finished_at=? WHERE id=? AND attempt=?",
                         (state, message, time.time(), work["id"], work["attempt"]))
            if block_job:
                conn.execute(
                    "UPDATE items SET state='blocked', message=?, finished_at=? WHERE job_id=? AND state='pending'",
                    (message, time.time(), work["job_id"]),
                )
            if state == "review":
                conn.execute("UPDATE queue_settings SET enabled=0 WHERE id=1")
            self._event(conn, f"{work_label(work)} · {message}", {
                "done": "success", "failed": "error", "review": "warn",
            }[state])

    def resolve(self, item_id, decision):
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT id, job_id, position, state FROM items WHERE id=?", (item_id,)).fetchone()
            if not row or row["state"] not in {"review", "failed", "blocked"}:
                raise QueueConflict("该项当前不能重跑或跳过")
            if decision == "retry":
                conn.execute(
                    """UPDATE items SET state='pending', attempt=NULL, prompt_id=NULL, manifest=NULL,
                                        message=NULL, started_at=NULL, finished_at=NULL WHERE id=?""", (item_id,)
                )
            elif decision == "skip":
                conn.execute("UPDATE items SET state='skipped', finished_at=? WHERE id=?",
                             (time.time(), item_id))
            else:
                raise ValueError("请选择重跑或跳过")
            action = "已标记重跑" if decision == "retry" else "已跳过"
            self._event(conn, f"{action} · #{row['job_id']} 第 {row['position']} 项", "warn")

    def snapshot(self):
        with self.connection() as conn:
            conn.execute("BEGIN")
            jobs = [dict(row) for row in conn.execute("""
                SELECT jobs.id, jobs.name, jobs.created_at, COUNT(items.id) AS total,
                    SUM(items.state='pending') AS pending, SUM(items.state='done') AS done,
                    SUM(items.state IN ('failed','blocked')) AS failed,
                    SUM(items.state='review') AS review, SUM(items.state='skipped') AS skipped,
                    SUM(items.state IN ('preparing','submitting','running','saving')) AS active
                FROM jobs JOIN items ON jobs.id=items.job_id
                GROUP BY jobs.id ORDER BY jobs.id
                """)]
            current = conn.execute("""
                SELECT i.id, i.job_id, i.position, i.state, i.label, i.prompt_id, j.name
                FROM items i JOIN jobs j ON i.job_id=j.id
                WHERE i.state IN ('preparing','submitting','running','saving','review')
                ORDER BY i.id LIMIT 1
                """).fetchone()
            events = [dict(row) for row in reversed(conn.execute(
                "SELECT id, created_at, level, message FROM events ORDER BY id DESC LIMIT 120"
            ).fetchall())]
            enabled = self.enabled(conn)
        has_pending = any(job["pending"] for job in jobs)
        if current and current["state"] == "review":
            state = "review"
        elif current:
            state = "running" if enabled else "pausing"
        elif enabled:
            state = "running"
        elif has_pending:
            state = "paused"
        else:
            state = "idle"
        return {"jobs": jobs, "current": dict(current) if current else None, "state": state,
                "enabled": enabled, "events": events, "has_pending": has_pending}

    def job_items(self, job_id):
        with self.connection() as conn:
            job = conn.execute("SELECT snapshot FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return []
            output_dir = Path(json.loads(job["snapshot"])["output_dir"])
            rows = conn.execute(
                """SELECT id, position, label, state, attempt, prompt_id, manifest, message, plan, started_at, finished_at
                   FROM items WHERE job_id=? ORDER BY position""", (job_id,)
            )
            items = []
            for row in rows:
                item = dict(row)
                item["paths"] = [entry["source"] for entry in json.loads(item.pop("plan"))["entries"]]
                item["files"] = ([entry["destination"] for entry in json.loads(item["manifest"])]
                                 if item["manifest"] else [])
                del item["manifest"]
                if item["attempt"] and item["state"] in {"review", "failed"}:
                    staged = output_dir / ".fr-batch-staging" / item["attempt"]
                    if staged.is_dir() and any(path.is_file() for path in staged.rglob("*")):
                        item["staging"] = str(staged)
                items.append(item)
        return items


def work_label(work):
    return f"#{work['job_id']} {work['job_name']} · 第 {work['position']} 项 · {work['label']}"


class BatchWorker:
    def __init__(self, store, request=comfy_request):
        self.store = store
        self.request = request
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is not None:
            raise RuntimeError("队列工作线程已经启动")
        lock = lock_database(self.store.path)
        try:
            self.store.startup()
            self.thread = threading.Thread(target=self._run, args=(lock,), daemon=True, name="comfy-batch-worker")
            self.thread.start()
        except Exception:
            lock.close()
            raise

    def stop(self):
        self.stopped.set()
        self.wake.set()

    def notify(self):
        self.wake.set()

    def _run(self, lock):
        with lock:
            while not self.stopped.is_set():
                try:
                    work = self.store.get_work()
                    if work:
                        self._process(work)
                        continue
                except WorkerStopped:
                    return
                except sqlite3.Error as error:
                    print(f"队列数据库暂不可用: {error}", flush=True)
                    self.stopped.wait(1)
                self.wake.wait(0.75)
                self.wake.clear()

    def _process(self, work):
        job, item, attempt = work["snapshot"], work["plan"], work["attempt"]
        state = work["state"]
        client = ComfyClient(job["config"], self.stopped,
                             lambda message, level: self.store.log(f"{work_label(work)} · {message}", level),
                             request=self.request)
        try:
            if self.stopped.is_set():
                raise WorkerStopped
            if state == "preparing":
                cleanup_staging(job, item, attempt)
                if not self.store.can_prepare():
                    self.store.requeue_preparing(work["id"])
                    return
                workflow = prepare_prompt(job, item, attempt)
                if not self.store.begin_submission(work):
                    self.store.requeue_preparing(work["id"])
                    cleanup_staging(job, item, attempt)
                    return
                state = "submitting"
            if state != "saving":
                with client.notifications(attempt):
                    prompt_id = work["prompt_id"]
                    if work["state"] == "preparing":
                        try:
                            prompt_id = client.submit(workflow, attempt)
                        except (OSError, ValueError) as error:
                            self.store.log(f"{work_label(work)} · 提交响应不明确，按原编号核对（{type(error).__name__}）", "warn")
                        else:
                            self.store.record_prompt(work, prompt_id)
                    history = client.await_history(prompt_id, attempt, lambda found: self.store.record_prompt(work, found))
                manifest = output_manifest(job, item, attempt, history)
                self.store.record_manifest(work, manifest)
                state = "saving"
            else:
                manifest = work["manifest"]
            if not manifest:
                raise NeedsReview("该次提交缺少已核对的成品清单")
            destinations = deliver_manifest(manifest, attempt, self.stopped)
            self.store.finish(work, "done", f"已保存 {len(destinations)} 个成品")
        except WorkerStopped:
            raise
        except JobSetupError as error:
            self.store.finish(work, "failed", str(error), block_job=True)
        except ItemFailed as error:
            self.store.finish(work, "failed", str(error))
        except NeedsReview as error:
            self.store.finish(work, "review", str(error))
            return
        except sqlite3.Error:
            raise
        except Exception as error:
            message = f"{type(error).__name__}: {str(error)[:250]}"
            if state == "preparing":
                self.store.finish(work, "failed", message)
            else:
                self.store.finish(work, "review", message)
                return
        cleanup_staging(job, item, attempt)
