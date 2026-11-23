"""Deterministic DAG driver for bounded, operator-approved local coding work."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import threading


class Orchestrator:
    """Own a plan store beside Engine; serialize with Engine tick at service level."""

    def __init__(self, db_path, engine):
        self.engine = engine
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS plans(
                plan_id TEXT PRIMARY KEY, goal TEXT NOT NULL, definition TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks(
                plan_id TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL,
                attempt INTEGER NOT NULL, revision TEXT, evidence TEXT,
                ready_evidence TEXT, report_verdict TEXT, report_evidence TEXT,
                PRIMARY KEY(plan_id,id)
            );
            CREATE TABLE IF NOT EXISTS reports(
                plan_id TEXT NOT NULL, id TEXT NOT NULL, attempt INTEGER NOT NULL,
                actor TEXT NOT NULL, revision TEXT NOT NULL, verdict TEXT NOT NULL,
                evidence TEXT NOT NULL, PRIMARY KEY(plan_id,id,attempt,actor)
            );
        """)
        plan_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(plans)")}
        for column in ("coordinator", "request"):
            if column not in plan_columns:
                self._db.execute(f"ALTER TABLE plans ADD COLUMN {column} TEXT")

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    @staticmethod
    def _id(s):
        return isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", s)

    @classmethod
    def _validate(cls, tasks):
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 64:
            raise ValueError("plan requires 1–64 tasks")
        required = {"id", "dependencies", "role", "backend", "model", "allowed_paths", "criterion"}
        optional = {"max_attempts", "verifier_backend", "verifier_model", "task_level",
                    "reasoning_effort", "verifier_reasoning_effort", "model_tier", "verifier_model_tier"}
        spec = {}
        for item in tasks:
            if not isinstance(item, dict) or not required <= item.keys() or item.keys() - required - optional:
                raise ValueError("invalid task keys")
            t = dict(item)
            task_id = t["id"]
            if not cls._id(task_id) or task_id in spec:
                raise ValueError("invalid or duplicate task ID")
            for field in ("role", "backend", "model", "criterion"):
                if not isinstance(t[field], str) or not t[field].strip() or len(t[field]) > 8192:
                    raise ValueError(f"invalid {field}")
            if t["role"].lower() in ("coordinator", "operator", "verifier"):
                raise ValueError("reserved implementation role")
            ds = t["dependencies"]
            if not isinstance(ds, list) or any(not cls._id(d) for d in ds) or len(set(ds)) != len(ds):
                raise ValueError("invalid dependencies")
            paths = t["allowed_paths"]
            if not isinstance(paths, list) or not paths or any(
                not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
                or any(p.lower() in ("", ".", "..", ".git") for p in path.rstrip("/").split("/"))
                for path in paths
            ):
                raise ValueError("invalid allowed paths")
            n = t.setdefault("max_attempts", 2)
            if type(n) is not int or not 1 <= n <= 5:
                raise ValueError("max_attempts must be 1–5")
            for target in ("backend", "model"):
                value = t.setdefault("verifier_" + target, t[target])
                if not isinstance(value, str) or not value.strip() or len(value) > 8192:
                    raise ValueError("invalid verifier settings")
            t["dependencies"] = sorted(ds)
            t["allowed_paths"] = sorted(set(paths))
            spec[task_id] = t
        if any(d not in spec for t in spec.values() for d in t["dependencies"]):
            raise ValueError("unknown dependency")
        resolved, order = set(), []
        while len(resolved) < len(spec):
            batch = [k for k in sorted(spec) if k not in resolved and set(spec[k]["dependencies"]) <= resolved]
            if not batch:
                raise ValueError("plan dependencies contain a cycle")
            order.extend(spec[k] for k in batch)
            resolved.update(batch)
        return order

    def replay(self, plan_id, goal, request, coordinator):
        with self._lock:
            row = self._db.execute("SELECT goal, request, coordinator FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None or row["request"] is None:
                return None
            if (row["goal"], json.loads(row["request"]), json.loads(row["coordinator"] or "null")) != (goal, request, coordinator):
                raise ValueError("plan already exists with a different definition")
            return self.summary(plan_id)

    def create(self, plan_id, tasks, goal="", *, coordinator=None, request=None):
        if not self._id(plan_id) or not isinstance(goal, str) or len(goal) > 8192:
            raise ValueError("invalid plan ID or goal")
        spec = self._validate(tasks)
        definition = json.dumps(spec, sort_keys=True)
        with self._lock, self._transaction():
            existing = self._db.execute("SELECT goal,definition FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if existing:
                if (existing["goal"], existing["definition"]) != (goal, definition):
                    raise ValueError("plan already exists with a different definition")
            else:
                self._db.execute("INSERT INTO plans (plan_id, goal, definition, coordinator, request) VALUES(?,?,?,?,?)",
                                 (plan_id, goal, definition, json.dumps(coordinator) if coordinator is not None else None,
                                  json.dumps(request) if request is not None else None))
                self._db.executemany("INSERT INTO tasks VALUES(?,?,'pending',1,NULL,NULL,NULL,NULL,NULL)",
                                     [(plan_id, t["id"]) for t in spec])
            return self.summary(plan_id)

    @staticmethod
    def _names(plan_id, task_id, attempt):
        digest = hashlib.sha256(json.dumps([plan_id, task_id], separators=(",", ":")).encode()).hexdigest()[:16]
        return f"plan-{digest}-a{attempt}-worker", f"plan-{digest}-a{attempt}-verifier"

    def _plan(self, plan_id):
        plan = self._db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise ValueError("unknown plan")
        spec = json.loads(plan["definition"])
        rows = {r["id"]: dict(r) for r in self._db.execute("SELECT * FROM tasks WHERE plan_id=?", (plan_id,))}
        return plan, spec, rows

    def _member_map(self):
        return {m["name"]: m for m in self.engine.members()}

    def plan_members(self, plan_id):
        with self._lock:
            _, spec, rows = self._plan(plan_id)
            members = self._member_map()
            return [name for task in spec if rows[task["id"]]["status"] not in ("pending", "blocked", "passed")
                    for name in self._names(plan_id, task["id"], rows[task["id"]]["attempt"]) if name in members]

    def _git(self, path, *args):
        return self.engine._git(path, *args).strip()

    def _member_head(self, member):
        return self._git(member["repo_path"], "rev-parse", "HEAD")

    def _merged(self, revision):
        if not revision:
            return False
        try:
            self._git(self.engine.repo_path, "merge-base", "--is-ancestor", revision, "HEAD")
            return True
        except RuntimeError:
            return False

    def summary(self, plan_id, *, compact=False):
        with self._lock:
            plan, spec, rows = self._plan(plan_id)
            tasks = []
            merged = {}
            for t in spec:
                r = rows[t["id"]]
                worker, verifier = self._names(plan_id, t["id"], r["attempt"])
                waiting = [d for d in t["dependencies"] if not merged.get(d)]
                merged[t["id"]] = r["status"] == "passed" and self._merged(r["revision"])
                task = {**t, "status": r["status"], "attempt": r["attempt"],
                              "revision": r["revision"], "evidence": r["evidence"],
                              "worker": worker, "verifier": verifier, "waiting_on": waiting,
                              "integrated": merged[t["id"]]}
                if compact:
                    for key in ("allowed_paths", "criterion", "dependencies", "max_attempts"):
                        task.pop(key, None)
                    if task["evidence"]:
                        task["evidence"] = task["evidence"][:1200]
                tasks.append(task)
            states = {r["status"] for r in tasks}
            if states == {"passed"}:
                state = "passed"
            elif states <= {"passed", "blocked"}:
                state = "blocked"
            elif any(t["status"] != "pending" or not t["waiting_on"] for t in tasks
                     if t["status"] not in ("passed", "blocked")):
                state = "running"
            else:
                state = "waiting_for_integration"
            goal = plan["goal"][:600] if compact else plan["goal"]
            return {"plan_id": plan_id, "goal": goal, "status": state, "tasks": tasks,
                    "coordinator": json.loads(plan["coordinator"] or "null")}

    def plans(self):
        with self._lock:
            return [self.summary(r[0]) for r in self._db.execute("SELECT plan_id FROM plans ORDER BY plan_id")]

    def _change(self, r, **updates):
        self._db.execute("UPDATE tasks SET " + ",".join(k + "=?" for k in updates) + " WHERE plan_id=? AND id=?",
                         (*updates.values(), r["plan_id"], r["id"]))
        r.update(updates)

    def _retry(self, t, r, evidence):
        if r["attempt"] >= t["max_attempts"]:
            self._change(r, status="blocked", evidence=evidence)
        else:
            self._change(r, status="pending", attempt=r["attempt"] + 1, revision=None, evidence=evidence,
                         ready_evidence=None, report_verdict=None, report_evidence=None)

    def _ready_member(self, name, members):
        if name not in members:
            raise ValueError("task actor worktree is unavailable")
        return members[name]

    def checkpoint(self, plan_id, task_id, actor, evidence):
        """Snapshot a scheduled implementation's changed, in-scope paths."""
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 8192:
            raise ValueError("report requires non-empty evidence")
        with self._lock:
            _, _, rows = self._plan(plan_id)
            r = rows.get(task_id)
            if not r:
                raise ValueError("unknown task")
            worker, _ = self._names(plan_id, task_id, r["attempt"])
            if actor != worker or r["status"] not in ("implementing", "ready_for_verification"):
                raise ValueError("only the scheduled implementation actor can checkpoint")
            m = self._ready_member(actor, self._member_map())
            path = m["repo_path"]
            if r["status"] == "ready_for_verification":
                return r["revision"]
            status = self.engine._git(path, "status", "--porcelain=v1", "-z", "--no-renames",
                                      "--untracked-files=all")
            changed = [row[3:] for row in status.split("\0") if row]
            if any(not self.engine._allowed(f, m["allowed_paths"]) for f in changed):
                raise ValueError("working changes outside task path scope")
            if changed:
                self._git(path, "--literal-pathspecs", "add", "-A", "--", *changed)
                staged = self.engine._git(path, "diff", "--cached", "--name-only", "-z", "--no-renames")
                if any(not self.engine._allowed(f, m["allowed_paths"]) for f in staged.split("\0") if f):
                    raise ValueError("staged changes outside task path scope")
                if staged:
                    self._git(path, "-c", "user.name=LocalSwarm", "-c", "user.email=swarm@users.noreply.github.com",
                              "commit", "--no-gpg-sign", "-m", f"LocalSwarm task {plan_id}/{task_id}")
            return self._member_head(m)

    def report(self, plan_id, task_id, actor, revision, verdict, evidence):
        """Accept an explicit report only from the service-authenticated scheduled actor."""
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            raise ValueError("report requires full immutable git revision")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 8192:
            raise ValueError("report requires non-empty evidence (up to 8192 chars)")
        with self._lock, self._transaction():
            _, _, rows = self._plan(plan_id)
            if task_id not in rows:
                raise ValueError("unknown task")
            r = rows[task_id]
            worker, verifier = self._names(plan_id, task_id, r["attempt"])
            prior = self._db.execute("SELECT revision,verdict,evidence FROM reports WHERE plan_id=? AND id=? AND attempt=? AND actor=?",
                                     (plan_id, task_id, r["attempt"], actor)).fetchone()
            if prior:
                if tuple(prior) == (revision, verdict, evidence):
                    return {"accepted": True, "duplicate": True}
                raise ValueError("report already recorded and immutable")
            members = self._member_map()
            if actor == worker and verdict == "ready" and r["status"] == "implementing":
                m = self._ready_member(actor, members)
                if self._member_head(m) != revision or revision == m["base_revision"]:
                    raise ValueError("worker report does not match an actual new commit at its HEAD")
                if self._git(m["repo_path"], "status", "--porcelain"):
                    raise ValueError("worker has uncommitted changes")
                self._change(r, status="ready_for_verification", revision=revision, ready_evidence=evidence)
            elif actor == verifier and verdict in ("pass", "rework") and r["status"] == "verifying":
                m = self._ready_member(actor, members)
                if revision != r["revision"] or m["base_revision"] != revision or self._member_head(m) != revision:
                    raise ValueError("verifier report does not match its pinned revision")
                if self._git(m["repo_path"], "status", "--porcelain"):
                    raise ValueError("verification worktree must be clean")
                self._change(r, status="verification_reported", report_verdict=verdict, report_evidence=evidence)
            else:
                raise ValueError("actor, verdict or task state do not match the scheduled action")
            self._db.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?)",
                             (plan_id, task_id, r["attempt"], actor, revision, verdict, evidence))
            return {"accepted": True, "duplicate": False}

    @staticmethod
    def _brief(plan, t, r, verifier=False):
        identity = f"plan_id={plan['plan_id']}; task_id={t['id']}; attempt={r['attempt']}."
        common = f"{identity}\nPlan goal: {plan['goal']}\nExact acceptance criterion: {t['criterion']}"
        if verifier:
            return (f"{common}\nReview the pinned subject without modifying it. Verify the criterion directly. "
                    f"Use swarm_report with this plan_id and task_id, revision={r['revision']}, verdict=pass or rework, "
                    f"and concrete evidence. Implementation reported: {r['ready_evidence']}")
        prior = f"\nPrior attempt finding: {r['evidence']}" if r["evidence"] else ""
        return (f"{common}{prior}\nWork only within the assigned paths. When the scoped changes are ready, use swarm_report "
                f"with this plan_id and task_id, verdict=ready and concrete evidence. Omit revision to ask the service to checkpoint the change. Do not merge.")

    def tick(self):
        """Advance only explicit states; runner execution remains Engine.tick's job."""
        with self._lock:
            members = self._member_map()
            changed = []
            for (plan_id,) in self._db.execute("SELECT plan_id FROM plans ORDER BY plan_id").fetchall():
                plan, spec, rows = self._plan(plan_id)
                coordinator = json.loads(plan["coordinator"] or "null")
                for t in spec:
                    r = rows[t["id"]]
                    before = (r["status"], r["attempt"])
                    worker, verifier = self._names(plan_id, t["id"], r["attempt"])
                    with self._transaction():
                        if r["status"] == "pending":
                            blocked = [d for d in t["dependencies"] if rows[d]["status"] == "blocked"]
                            if blocked:
                                self._change(r, status="blocked", evidence="Prerequisite blocked: " + ", ".join(blocked))
                            elif all(rows[d]["status"] == "passed" and self._merged(rows[d]["revision"])
                                     for d in t["dependencies"]):
                                # The deterministic actor identity lets recovery adopt a
                                # committed Engine spawn if process death preceded this state write.
                                if worker not in members:
                                    members[worker] = self.engine.spawn(worker, t["role"], self._brief(plan, t, r),
                                                                        t["allowed_paths"], t["backend"], t["model"],
                                                                        reasoning_effort=t.get("reasoning_effort"),
                                                                        coordinator=coordinator if t["backend"] == "codex" else None)
                                self._change(r, status="implementing")
                        elif r["status"] in ("implementing", "verifying"):
                            role_actor = worker if r["status"] == "implementing" else verifier
                            m = self._ready_member(role_actor, members)
                            if m.get("turns", 0) and not m["active"] and not self.engine.awaiting_reply(role_actor):
                                reason = self.engine.blocked_reason(role_actor)
                                if reason:
                                    self._change(r, status="blocked", evidence=reason)
                                else:
                                    self._retry(t, r, f"{role_actor} exited without a structured report")
                        elif r["status"] == "ready_for_verification":
                            m = self._ready_member(worker, members)
                            if not m["active"]:
                                try:
                                    if self._member_head(m) != r["revision"] or self._git(m["repo_path"], "status", "--porcelain"):
                                        raise ValueError("worker changed its tree after reporting")
                                    if verifier not in members:
                                        members[verifier] = self.engine.spawn(verifier, "verifier", self._brief(plan, t, r, True),
                                                                             [], t["verifier_backend"], t["verifier_model"],
                                                                             review_worker=worker, review_revision=r["revision"],
                                                                             reasoning_effort=t.get("verifier_reasoning_effort"),
                                                                             coordinator=coordinator if t["verifier_backend"] == "codex" else None)
                                    elif members[verifier]["base_revision"] != r["revision"]:
                                        raise ValueError("recovered verifier is not pinned to the reported revision")
                                    self._change(r, status="verifying")
                                except (RuntimeError, ValueError) as exc:
                                    self._retry(t, r, str(exc))
                        elif r["status"] == "verification_reported":
                            m = self._ready_member(verifier, members)
                            if not m["active"]:
                                if self._member_head(m) != r["revision"] or self._git(m["repo_path"], "status", "--porcelain"):
                                    self._retry(t, r, "Verifier changed its pinned tree after reporting")
                                elif self._member_head(self._ready_member(worker, members)) != r["revision"]:
                                    self._retry(t, r, "Worker changed its reviewed revision")
                                elif r["report_verdict"] == "rework":
                                    self._retry(t, r, r["report_evidence"])
                                else:
                                    try:
                                        if not self._merged(r["revision"]):
                                            actual = self.engine.preview_merge(worker)
                                            if actual["revision"] != r["revision"]:
                                                raise ValueError("worker revision changed after verification")
                                        self._change(r, status="passed", evidence=r["report_evidence"])
                                    except (RuntimeError, ValueError) as exc:
                                        self._retry(t, r, f"Verified work needs a new attempt: {exc}")
                        elif r["status"] == "passed" and not self._merged(r["revision"]):
                            m = self._ready_member(worker, members)
                            if not m["active"]:
                                try:
                                    candidate = self.engine.preview_merge(worker)
                                    if candidate["revision"] != r["revision"]:
                                        raise ValueError("reviewed worker revision changed")
                                except (RuntimeError, ValueError) as exc:
                                    self._retry(t, r, f"Rebuild and re-review on current base: {exc}")
                    if before != (r["status"], r["attempt"]):
                        changed.append({"plan_id": plan_id, "task_id": r["id"], "status": r["status"], "attempt": r["attempt"]})
            return changed

    def require_merge_ready(self, worker_name, revision):
        """Raise for a plan actor unless the current attempt passed for this SHA.

        Returns False for non-plan workers; the operator still needs Engine's
        normal revision, base, clean-worktree and path policy gates.
        """
        with self._lock:
            rows = self._db.execute("SELECT * FROM tasks ORDER BY plan_id,id").fetchall()
            for r in rows:
                for attempt in range(1, r["attempt"] + 1):
                    worker, verifier = self._names(r["plan_id"], r["id"], attempt)
                    if worker_name == verifier:
                        raise ValueError("verification worktrees cannot be merged")
                    if worker_name == worker:
                        if (attempt != r["attempt"] or r["status"] != "passed"
                            or r["revision"] != revision):
                            raise ValueError("plan task has not passed verification for this worker revision")
                        return True
            return False

    def close(self):
        with self._lock:
            self._db.close()
