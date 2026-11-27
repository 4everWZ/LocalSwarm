"""Durable local coordination engine. Runner is an injected callable, never a CLI adapter."""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
from contextlib import contextmanager
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import threading
import uuid

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class Engine:
    def __init__(self, db_path, repo_path, runner, max_workers=4, max_turns=8):
        if max_workers < 1 or max_turns < 1:
            raise ValueError("worker and turn limits must be positive")
        self.db_path = Path(db_path).resolve()
        self.repo_path = Path(repo_path).resolve()
        self.runner = runner
        self.max_workers, self.max_turns = max_workers, max_turns
        self._lock = threading.RLock()
        self._closed = False
        self._active = {}
        self.changed = threading.Event()
        self._file = self.db_path.with_suffix(self.db_path.suffix + ".lock").open("a+b")
        try:
            if os.name == "nt":
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            raise RuntimeError("another engine owns this database") from exc
        try:
            self._db = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
                    mentions TEXT NOT NULL, content TEXT NOT NULL, status TEXT, created_at TEXT, source TEXT
                );
                CREATE TABLE IF NOT EXISTS workers(
                    name TEXT PRIMARY KEY, role TEXT NOT NULL, brief TEXT NOT NULL,
                    allowed_paths TEXT NOT NULL, backend TEXT NOT NULL, model TEXT NOT NULL,
                    repo_path TEXT NOT NULL, base_revision TEXT NOT NULL,
                    session_id TEXT, cursor INTEGER NOT NULL, active TEXT, turns INTEGER NOT NULL DEFAULT 0
                );
            """)
            if "created_at" not in {r["name"] for r in self._db.execute("PRAGMA table_info(events)")}:
                self._db.execute("ALTER TABLE events ADD COLUMN created_at TEXT")
            if "source" not in {r["name"] for r in self._db.execute("PRAGMA table_info(events)")}:
                self._db.execute("ALTER TABLE events ADD COLUMN source TEXT")
            if "turns" not in {r["name"] for r in self._db.execute("PRAGMA table_info(workers)")}:
                self._db.execute("ALTER TABLE workers ADD COLUMN turns INTEGER NOT NULL DEFAULT 0")
            worker_columns = {r["name"] for r in self._db.execute("PRAGMA table_info(workers)")}
            for column in ("reasoning_effort", "coordinator"):
                if column not in worker_columns:
                    self._db.execute(f"ALTER TABLE workers ADD COLUMN {column} TEXT")
            for column, schema in (("archive_since", "REAL"), ("archive_revision", "TEXT"),
                                   ("archive_state", "TEXT NOT NULL DEFAULT 'live'")):
                if column not in worker_columns:
                    self._db.execute(f"ALTER TABLE workers ADD COLUMN {column} {schema}")
            with self._txn():
                for row in self._db.execute("SELECT name,active FROM workers WHERE active IS NOT NULL").fetchall():
                    self._insert(row["name"], [], f"Interrupted run {row['active']}; claimed events will not be replayed.", "interrupted")
                self._db.execute("UPDATE workers SET active=NULL WHERE active IS NOT NULL")
            self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="swarm")
        except BaseException:
            if hasattr(self, "_db"):
                self._db.close()
            self._file.close()
            raise

    @contextmanager
    def _txn(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    @staticmethod
    def _git(path, *args):
        result = subprocess.run(["git", "-C", str(path), "-c", f"core.hooksPath={os.devnull}", *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or f"git {args[0]} failed")
        return result.stdout.decode("utf-8")

    @staticmethod
    def _event(row):
        data = dict(row)
        data["mentions"] = json.loads(data["mentions"])
        return data

    @staticmethod
    def _worker(row):
        data = dict(row)
        data["allowed_paths"] = json.loads(data["allowed_paths"])
        if data["coordinator"] is not None:
            data["coordinator"] = json.loads(data["coordinator"])
        return data

    def _insert(self, actor, mentions, content, status, source="scheduler"):
        seq = self._db.execute(
            "INSERT INTO events(actor,mentions,content,status,created_at,source) VALUES(?,?,?,?,?,?)",
            (actor, json.dumps(mentions), content, status, datetime.now(timezone.utc).isoformat(), source)
        ).lastrowid
        self.changed.set()
        return seq

    def spawn(self, name, role, brief, allowed_paths, backend, model,
              review_worker=None, review_revision=None, reasoning_effort=None, coordinator=None):
        if (not isinstance(name, str) or name.lower() in {"operator", "coordinator"}
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", name)):
            raise ValueError("invalid worker name")
        if isinstance(allowed_paths, str):
            raise ValueError("allowed_paths must be a sequence of paths")
        allowed = list(allowed_paths)
        for path in allowed:
            if (not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
                or any(p in ("", ".", "..", ".git") for p in path.rstrip("/").split("/"))):
                raise ValueError("allowed paths must be safe repo-relative paths or globs")
        with self._lock:
            if self._db.execute("SELECT 1 FROM workers WHERE name=?", (name,)).fetchone():
                raise ValueError("worker already exists")
            base = self._git(self.repo_path, "rev-parse", "HEAD").strip()
            if review_worker or review_revision:
                if role != "verifier" or not review_worker or not review_revision:
                    raise ValueError("review_worker and review_revision are verifier-only and must be paired")
                review = self.preview_merge(review_worker)
                if review["revision"] != review_revision:
                    raise ValueError("reviewed worker revision changed")
                base = review_revision
                brief = f"Review the pinned worker revision {base} relative to {review['base_revision']}.\n" + brief
            location = self.db_path.parent / (self.db_path.name + ".worktrees") / (name + "-" + uuid.uuid4().hex)
            location.parent.mkdir(exist_ok=True)
            self._git(self.repo_path, "worktree", "add", "--detach", str(location), base)
            with self._txn():
                cursor = self._db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
                self._db.execute("""INSERT INTO workers
                    (name, role, brief, allowed_paths, backend, model, repo_path, base_revision, cursor,
                     reasoning_effort, coordinator) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (name, role, brief, json.dumps(allowed), backend, model, str(location), base, cursor,
                     reasoning_effort, json.dumps(coordinator) if coordinator is not None else None))
                self._insert("operator", [name], brief, "spawned")
            return self._get(name)

    def _get(self, name):
        row = self._db.execute("SELECT * FROM workers WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValueError("unknown worker")
        return self._worker(row)

    def post(self, actor, mentions, content, status=None):
        if isinstance(mentions, str):
            mentions = [mentions]
        if not isinstance(actor, str) or not isinstance(content, str) or (status is not None and not isinstance(status, str)):
            raise ValueError("actor, content and status must be strings")
        targets = list(dict.fromkeys(mentions or []))
        if any(not isinstance(t, str) for t in targets):
            raise ValueError("mentions must be strings")
        with self._lock, self._txn():
            seq = self._insert(actor, targets, content, status, "board")
        return seq

    def read(self, since_seq=0, *, actor=None, limit=None):
        query = "SELECT * FROM events WHERE seq>?"
        args = [since_seq]
        if actor is not None:
            query += " AND actor=?"
            args.append(actor)
        query += " ORDER BY seq"
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        with self._lock:
            return [self._event(row) for row in self._db.execute(query, args)]

    def inbox(self, name, since_seq=0, limit=20):
        with self._lock:
            rows = self._db.execute("""SELECT e.* FROM events e WHERE e.seq>? AND e.source='board' AND e.actor<>?
                AND EXISTS (SELECT 1 FROM json_each(e.mentions) WHERE value=?)
                ORDER BY e.seq LIMIT ?""", (since_seq, name, name, limit + 1)).fetchall()
            events = [self._event(row) for row in rows[:limit]]
            for event in events:
                event["content"] = event["content"][:1500]
            has_more = len(rows) > limit
            latest = self._db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
            return {"events": events, "next_seq": events[-1]["seq"] if has_more else max(since_seq, latest),
                    "has_more": has_more}

    def members(self):
        with self._lock:
            return [self._worker(row) for row in self._db.execute("SELECT * FROM workers ORDER BY name")]

    def latest_seq(self):
        with self._lock:
            return self._db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]

    def sequences(self, since, limit):
        with self._lock:
            return [row[0] for row in self._db.execute(
                "SELECT seq FROM events WHERE seq>? ORDER BY seq LIMIT ?", (since, limit))]

    def _addressed(self, name, cursor, through, limit=None):
        query = """SELECT e.* FROM events e
            WHERE e.seq>? AND e.seq<=? AND e.actor<>?
              AND EXISTS (SELECT 1 FROM json_each(e.mentions) WHERE value=?)
            ORDER BY e.seq"""
        args = [cursor, through, name, name]
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        return self._db.execute(query, args)

    def has_work(self):
        """Use after tick. Unexamined rows can conservatively keep the service awake."""
        with self._lock:
            return bool(self._active or self._db.execute(
                "SELECT 1 FROM workers WHERE cursor < (SELECT max(seq) FROM events) LIMIT 1"
            ).fetchone())

    def _archive_path(self, worker):
        root = self.db_path.parent / (self.db_path.name + ".worktrees")
        path = Path(worker["repo_path"])
        if (not re.fullmatch(re.escape(worker["name"]) + r"-[a-f0-9]{32}", path.name) or path.parent != root
                or root.resolve() != root or path.resolve() != path):
            raise ValueError("worktree path is outside managed storage")
        return path

    @staticmethod
    def _archive_ref(worker):
        return "refs/localswarm/archived/" + worker["name"]

    def _check_archive(self, worker, revision, through):
        if worker["active"]:
            return "Worker is active"
        if self._addressed(worker["name"], worker["cursor"], through, 1).fetchone():
            return "Messages are pending"
        try:
            path = self._archive_path(worker)
            if not path.is_dir():
                return "Worktree is missing"
            root = self._git(path, "rev-parse", "--show-toplevel").strip()
            common = self._git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
            expected = self._git(self.repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
            if Path(root).resolve() != path or Path(common).resolve() != Path(expected).resolve():
                return "Worktree ownership could not be verified"
            if self._git(path, "rev-parse", "HEAD").strip() != revision:
                return "Worktree HEAD differs from the integrated revision"
            if self._git(path, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"):
                return "Worktree contains changes, untracked files or ignored files"
            self._git(self.repo_path, "merge-base", "--is-ancestor", revision, "HEAD")
        except (RuntimeError, OSError, ValueError):
            return "Worktree or integration could not be verified"
        return None

    def maintain_worktrees(self, eligible, now, retention, *, dry_run=False):
        """Inspect only plan-owned worktrees with a verified integrated revision."""
        results = []
        with self._lock:
            through = self.latest_seq()
            for worker in self.members():
                name = worker["name"]
                revision = eligible.get(name)
                state = worker["archive_state"]
                if state == "archived":
                    results.append({"worker": name, "status": "archived"})
                    continue
                if state == "archiving" and not dry_run:
                    try:
                        path = self._archive_path(worker)
                        state = "live" if path.exists() else "archived"
                        self._db.execute("UPDATE workers SET archive_state=? WHERE name=?", (state, name))
                    except (OSError, ValueError):
                        results.append({"worker": name, "status": "skipped", "reason": "Archive recovery requires inspection"})
                        continue
                    if state == "archived":
                        results.append({"worker": name, "status": state})
                        continue
                reason = self._check_archive(worker, revision, through) if revision else "No verified integrated plan revision"
                if reason:
                    if not dry_run and worker["archive_since"] is not None:
                        self._db.execute("UPDATE workers SET archive_since=NULL WHERE name=?", (name,))
                    results.append({"worker": name, "status": "skipped", "reason": reason})
                    continue
                since = worker["archive_since"]
                if since is None and not dry_run:
                    self._db.execute("UPDATE workers SET archive_since=? WHERE name=?", (now, name))
                ready = since is not None and now - since >= retention
                report = {"worker": name, "status": "eligible" if ready else "retaining",
                          "eligible_after": (now if since is None else since) + retention}
                if ready and not dry_run:
                    path = self._archive_path(worker)
                    try:
                        self._git(self.repo_path, "update-ref", self._archive_ref(worker), revision)
                        self._db.execute(
                            "UPDATE workers SET archive_state='archiving', archive_revision=? WHERE name=?",
                            (revision, name),
                        )
                        self._git(self.repo_path, "worktree", "remove", str(path))
                        self._db.execute("UPDATE workers SET archive_state='archived' WHERE name=?", (name,))
                        report["status"] = "archived"
                    except (OSError, RuntimeError):
                        report.update(status="skipped", reason="Git could not safely remove the worktree")
                results.append(report)
        return results

    def ensure_worktree(self, worker):
        with self._lock:
            if worker["archive_state"] == "live":
                return
            path = self._archive_path(worker)
            revision = worker["archive_revision"]
            pinned = self._git(self.repo_path, "rev-parse", "--verify", self._archive_ref(worker)).strip()
            if not revision or pinned != revision:
                raise ValueError("worktree archive revision is unavailable")
            if not path.exists():
                self._git(self.repo_path, "worktree", "add", "--detach", str(path), revision)
            else:
                root = self._git(path, "rev-parse", "--show-toplevel").strip()
                common = self._git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
                expected = self._git(self.repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
                if (Path(root).resolve() != path or Path(common).resolve() != Path(expected).resolve()
                        or self._git(path, "rev-parse", "HEAD").strip() != revision):
                    raise ValueError("worktree archive location is already occupied")
            self._db.execute("UPDATE workers SET archive_state='live',archive_since=NULL WHERE name=?", (worker["name"],))

    def awaiting_reply(self, name):
        with self._lock:
            state = self._db.execute("""SELECT status FROM events
                WHERE actor=? AND source='board' AND status IN ('waiting','done')
                AND seq > (SELECT coalesce(max(seq),0) FROM events WHERE actor=? AND status='running')
                ORDER BY seq DESC LIMIT 1""", (name, name)).fetchone()
            return bool(state and state[0] == "waiting")

    def blocked_reason(self, name):
        with self._lock:
            event = self._db.execute("""SELECT content FROM events WHERE actor=? AND status='blocked'
                AND seq > (SELECT coalesce(max(seq),0) FROM events WHERE actor=? AND status='running')
                ORDER BY seq DESC LIMIT 1""", (name, name)).fetchone()
            return event["content"] if event else None

    def _finish(self, worker, run_id, future):
        try:
            result = future.result() or {}
            if not isinstance(result, dict):
                raise TypeError("runner result must be a dict")
            session = result.get("session_id")
            content = result.get("content")
            mentions = result.get("mentions") or []
            status = result.get("status") or "completed"
            if isinstance(mentions, str):
                mentions = [mentions]
            if ((session is not None and not isinstance(session, str)) or
                (content is not None and not isinstance(content, str)) or
                not isinstance(status, str) or not isinstance(mentions, (list, tuple)) or
                any(not isinstance(target, str) for target in mentions)):
                raise TypeError("invalid runner result fields")
        except Exception as exc:
            session, content, mentions, status = None, f"Run failed: {type(exc).__name__}: {exc}", [], "failed"
        with self._txn():
            if status in ("completed", "waiting"):
                explicit = self._db.execute("""SELECT status FROM events WHERE actor=?
                    AND status IN ('waiting','done') AND seq >
                    (SELECT coalesce(max(seq),0) FROM events WHERE actor=? AND status='running')
                    ORDER BY seq DESC LIMIT 1""", (worker, worker)).fetchone()
                if explicit:
                    status = explicit["status"]
            update = self._db.execute("UPDATE workers SET active=NULL,session_id=coalesce(?,session_id) WHERE name=? AND active=?",
                                      (session, worker, run_id))
            if update.rowcount:
                if status == "blocked":
                    self._insert(worker, ["coordinator"], content or "Worker dispatch blocked", status, "board")
                else:
                    self._insert(worker, list(mentions), content or "", status)

    def tick(self):
        """Reap finished futures and launch pending actors without waiting for their results."""
        with self._lock:
            if self._closed:
                return {"launched": [], "reaped": []}
            reaped, launched = [], []
            for name, (run_id, future) in list(self._active.items()):
                if future.done():
                    self._finish(name, run_id, future)
                    del self._active[name]
                    reaped.append(name)
            through = self.latest_seq()
            candidates = self._db.execute(
                "SELECT * FROM workers WHERE active IS NULL AND cursor<? ORDER BY name", (through,)
            ).fetchall()
            for row in candidates:
                if len(self._active) >= self.max_workers:
                    break
                worker = self._worker(row)
                name = worker["name"]
                events = [self._event(e) for e in self._addressed(name, worker["cursor"], through)]
                if not events:
                    self._db.execute("UPDATE workers SET cursor=? WHERE name=?", (through, name))
                    continue
                if worker["turns"] >= self.max_turns:
                    with self._txn():
                        self._db.execute("UPDATE workers SET cursor=? WHERE name=?", (through, name))
                        self._insert(name, [], "Turn limit reached; no provider call was made.", "limited")
                    continue
                try:
                    self.ensure_worktree(worker)
                except (OSError, RuntimeError, ValueError) as exc:
                    with self._txn():
                        self._db.execute("UPDATE workers SET cursor=? WHERE name=?", (through, name))
                        self._insert(name, ["coordinator"], f"Worktree recovery blocked: {exc}", "blocked", "board")
                    continue
                run_id = uuid.uuid4().hex
                with self._txn():
                    self._db.execute("UPDATE workers SET cursor=?,active=?,turns=turns+1,archive_since=NULL WHERE name=?",
                                     (through, run_id, name))
                    self._insert(name, [], "", "running")
                request = {**worker, "actor": name, "active": run_id, "run_id": run_id, "events": events}
                try:
                    future = self._pool.submit(self.runner, request)
                except Exception as exc:
                    future = concurrent.futures.Future()
                    future.set_exception(exc)
                self._active[name] = (run_id, future)
                future.add_done_callback(lambda _: self.changed.set())
                launched.append(name)
            return {"launched": launched, "reaped": reaped}

    @staticmethod
    def _allowed(path, patterns):
        parts = PurePosixPath(path).parts
        if not parts or any(part.lower() == ".git" for part in parts):
            return False
        return any(fnmatchcase(path, pat) if any(c in pat for c in "*?[")
                   else path == pat.rstrip("/") or path.startswith(pat.rstrip("/") + "/")
                   for pat in patterns)

    def preview_merge(self, worker_name):
        with self._lock:
            worker = self._get(worker_name)
            if worker["role"] == "verifier":
                raise ValueError("verifiers cannot be merged")
            if worker["active"]:
                raise RuntimeError("worker run must finish before merge review")
            self.ensure_worktree(worker)
            location = worker["repo_path"]
            revision = self._git(location, "rev-parse", "HEAD").strip()
            base = self._git(self.repo_path, "rev-parse", "HEAD").strip()
            if revision == base:
                raise ValueError("nothing to merge")
            self._git(self.repo_path, "merge-base", "--is-ancestor", base, revision)
            # All commits, including net-zero edits: model changes outside the
            # allowlist may not enter main history even if a later commit undoes them.
            commits = self._git(self.repo_path, "rev-list", f"{base}..{revision}").splitlines()
            paths = set()
            for commit in commits:
                words = self._git(self.repo_path, "rev-list", "--parents", "-n", "1", commit).split()
                if len(words) != 2:
                    raise ValueError("worker history must be linear")
                paths.update(p for p in self._git(
                    self.repo_path, "diff", "--name-only", "-z", "--no-renames", words[1], commit, "--"
                ).split("\0") if p)
            denied = sorted(p for p in paths if not self._allowed(p, worker["allowed_paths"]))
            if denied:
                raise ValueError(f"paths outside worker scope: {denied!r}")
            return {"worker": worker_name, "revision": revision, "base_revision": base, "paths": sorted(paths)}

    def merge(self, worker_name, expected_revision=None, expected_base_revision=None):
        """Explicit operator action only; approve the exact previewed revision."""
        if not expected_revision:
            raise ValueError("explicit expected_revision required")
        with self._lock:
            review = self.preview_merge(worker_name)
            if expected_revision != review["revision"]:
                raise ValueError("worker revision changed")
            if expected_base_revision is not None and expected_base_revision != review["base_revision"]:
                raise ValueError("target base changed")
            if self._git(self.repo_path, "status", "--porcelain"):
                raise ValueError("target worktree is not clean")
            if self._git(self.repo_path, "rev-parse", "HEAD").strip() != review["base_revision"]:
                raise ValueError("target base changed")
            self._git(self.repo_path, "merge", "--ff-only", review["revision"])
            self.post("operator", [], f"Merged {worker_name} at {review['revision']}", "merged")
            return review

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=True)
        with self._lock:
            try:
                for name, (run_id, future) in list(self._active.items()):
                    self._finish(name, run_id, future)
                self._active.clear()
            finally:
                self._db.close()
                self._file.close()
