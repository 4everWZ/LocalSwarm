"""Bounded, read-only views for the browser board."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3


PAGE_SIZE = 50
GRAPH_NODES = 32
MESSAGE = """(
    (source = 'board' AND (status IS NULL OR status <> 'plan'))
    OR source IS NULL
    OR source NOT IN ('board', 'scheduler')
    OR (source = 'scheduler' AND (
        status IN ('blocked', 'failed', 'interrupted', 'limited', 'error', 'cancelled', 'canceled', 'spawned')
        OR (
            content <> ''
            AND (json_array_length(mentions) > 0 OR status IN ('done', 'complete', 'completed', 'passed', 'success'))
            AND board_trim(content) <> ''
        )
    ))
)"""
CONNECTIONS = """
WITH connections AS (
    SELECT actor AS sender, targets.value AS recipient, count(*) AS count
    FROM events JOIN json_each(events.mentions) AS targets
    WHERE seq <= :position AND actor <> '' AND targets.value <> ''
        AND (:member = '' OR instr(board_lower(actor), :member) > 0
            OR instr(board_lower(targets.value), :member) > 0)
    GROUP BY actor, targets.value
)
"""


def parameter(query, key, default=""):
    values = query.get(key)
    if values is None:
        return default
    if len(values) != 1:
        raise ValueError(f"{key} must be supplied once")
    return values[0]


def number(query, key, default=None):
    if key not in query:
        return default
    value = parameter(query, key)
    if len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise ValueError(f"{key} must be a non-negative integer")
    parsed = int(value)
    if parsed > 2 ** 63 - 1:
        raise ValueError(f"{key} is too large")
    return parsed


def latest_sequence(db):
    return db.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def last_statuses(db, position, names=None):
    actor_filter = target_filter = ""
    args = {"position": position}
    if names is not None:
        if not names:
            return {}
        names = {f"name{index}": name for index, name in enumerate(names)}
        placeholders = ", ".join(f":{key}" for key in names)
        actor_filter = f" AND actor IN ({placeholders})"
        target_filter = f" AND targets.value IN ({placeholders})"
        args.update(names)
    return dict(db.execute(f"""
        WITH states AS (
            SELECT actor, max(seq) AS seq FROM events
            WHERE seq <= :position AND status IS NOT NULL AND status NOT IN ('spawned', 'plan', 'merged', '')
                {actor_filter}
            GROUP BY actor
            UNION ALL
            SELECT targets.value, max(seq) FROM events JOIN json_each(events.mentions) AS targets
            WHERE seq <= :position AND actor = 'operator' AND status = 'spawned' {target_filter}
            GROUP BY targets.value
        ), latest AS (
            SELECT actor, max(seq) AS seq FROM states GROUP BY actor
        )
        SELECT latest.actor, e.status FROM latest JOIN events AS e ON e.seq = latest.seq
    """, args))


class BoardAPI:
    def __init__(self, app):
        self.engine = app.engine
        self.orchestrator = app.orchestrator
        self.uri = Path(self.engine.db_path).resolve().as_uri() + "?mode=ro"

    @contextmanager
    def read_db(self):
        db = sqlite3.connect(self.uri, uri=True)
        db.row_factory = sqlite3.Row
        db.create_function("board_lower", 1, str.lower, deterministic=True)
        db.create_function("board_trim", 1, str.strip, deterministic=True)
        try:
            # Use one read snapshot for a response while allowing the scheduler to write to the WAL.
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    def get(self, path, query):
        routes = {
            "/board/messages": self.messages,
            "/board/members": self.members,
            "/board/plans": self.plans,
            "/board/task": self.task,
            "/board/graph": self.graph,
        }
        return routes[path](query)

    def messages(self, query):
        before = number(query, "before")
        after = number(query, "after")
        requested = number(query, "through")
        if before is not None and after is not None:
            raise ValueError("use either before or after")
        direction = parameter(query, "direction", "either")
        if direction not in ("either", "from", "to"):
            raise ValueError("invalid message direction")
        all_events = parameter(query, "all", "0")
        if all_events not in ("0", "1"):
            raise ValueError("all must be 0 or 1")
        term = parameter(query, "q").strip().lower()
        member = parameter(query, "member")
        filters = ["1" if all_events == "1" else MESSAGE]
        args = {"limit": PAGE_SIZE, "term": term, "member": member}
        if term:
            filters.append("instr(board_lower(content), :term) > 0")
        if member:
            match = []
            if direction != "to":
                match.append("actor = :member")
            if direction != "from":
                match.append("EXISTS (SELECT 1 FROM json_each(mentions) WHERE value = :member)")
            filters.append("(" + " OR ".join(match) + ")")
        where = " AND ".join(filters)
        with self.read_db() as db:
            latest = latest_sequence(db)
            through = latest if requested is None else min(requested, latest)
            args["through"] = through
            total, pending = db.execute(
                f"""SELECT count(CASE WHEN seq <= :through THEN 1 END),
                           count(CASE WHEN seq > :through THEN 1 END)
                    FROM events WHERE {where}""", args,
            ).fetchone()
            page = f"SELECT * FROM events WHERE seq <= :through AND {where}"
            if before is not None:
                page += " AND seq < :cursor"
                args["cursor"] = before
            if after is not None:
                page += " AND seq > :cursor"
                args["cursor"] = after
                rows = list(db.execute(page + " ORDER BY seq LIMIT :limit", args))
            else:
                rows = list(reversed(list(db.execute(page + " ORDER BY seq DESC LIMIT :limit", args))))
            next_before = next_after = None
            if rows:
                args.update(first=rows[0]["seq"], last=rows[-1]["seq"])
                older, newer = db.execute(f"""
                    SELECT EXISTS (SELECT 1 FROM events WHERE seq < :first AND {where}),
                           EXISTS (SELECT 1 FROM events WHERE seq > :last AND seq <= :through AND {where})
                """, args).fetchone()
                next_before = args["first"] if older else None
                next_after = args["last"] if newer else None
            events = [self.engine._event(row) for row in rows]
        return dict(events=events, latest_seq=latest, through=through, total=total, pending=pending,
                    next_before=next_before, next_after=next_after)

    def members(self, _query):
        with self.read_db() as db:
            latest = latest_sequence(db)
            rows = db.execute(
                "SELECT name, role, backend, model, reasoning_effort, active FROM workers ORDER BY name"
            )
            members = {row["name"]: dict(row) for row in rows}
            names = [row[0] for row in db.execute("""
                SELECT name FROM workers
                UNION SELECT actor FROM events WHERE actor <> ''
                UNION SELECT value FROM events JOIN json_each(events.mentions) WHERE value <> ''
                ORDER BY 1
            """)]
            if "coordinator" in names:
                members["coordinator"] = {"name": "coordinator", "role": "coordinator"}
            statuses = last_statuses(db, latest)
            messages = dict(db.execute(f"SELECT actor, max(seq) FROM events WHERE {MESSAGE} GROUP BY actor"))
            for name, member in members.items():
                active = bool(member.get("active"))
                status = statuses.get(name)
                if status == "running" and not active:
                    status = None
                member.update(active=active, status="running" if active else status,
                              last_message=messages.get(name))
        return dict(members=list(members.values()), names=names, latest_seq=latest)

    def plans(self, _query):
        plans = []
        if self.orchestrator is not None:
            with self.orchestrator.git_view(refresh=True):
                ids = self.orchestrator._db.execute("SELECT plan_id FROM plans ORDER BY plan_id")
                for (plan_id,) in ids:
                    summary = self.orchestrator.summary(plan_id)
                    plans.append({key: summary[key] for key in ("plan_id", "goal", "status")} | {
                        "tasks": [{key: task[key] for key in ("id", "status", "integrated")}
                                  for task in summary["tasks"]],
                    })
        with self.read_db() as db:
            return dict(plans=plans, latest_seq=latest_sequence(db))

    def task(self, query):
        plan_id = parameter(query, "plan_id")
        task_id = parameter(query, "task_id")
        if self.orchestrator is None or not plan_id or not task_id:
            raise KeyError("unknown task")
        with self.orchestrator.git_view(refresh=True):
            try:
                plan = self.orchestrator.summary(plan_id)
            except ValueError:
                raise KeyError("unknown plan") from None
        for task in plan["tasks"]:
            if task["id"] == task_id:
                return {"task": task}
        raise KeyError("unknown task")

    @staticmethod
    def _edge(row):
        return {"from": row["sender"], "to": row["recipient"], "count": row["count"]}

    def graph(self, query):
        requested_index = number(query, "index")
        requested_through = number(query, "through")
        offset = number(query, "offset", 0)
        member = parameter(query, "member").strip().lower()
        with self.read_db() as db:
            latest = latest_sequence(db)
            through = latest if requested_through is None else min(requested_through, latest)
            total = db.execute("SELECT count(*) FROM events WHERE seq <= ?", (through,)).fetchone()[0]
            index = total if requested_index is None else min(total, requested_index)
            if index:
                seq, stamp = db.execute(
                    "SELECT seq, created_at FROM events WHERE seq <= ? ORDER BY seq LIMIT 1 OFFSET ?",
                    (through, index - 1),
                ).fetchone()
            else:
                seq, stamp = 0, None
            args = dict(position=seq, member=member, offset=offset, limit=PAGE_SIZE)
            rows = list(db.execute(CONNECTIONS + """
                , page AS (
                    SELECT * FROM connections ORDER BY count DESC, sender, recipient
                    LIMIT :limit OFFSET :offset
                )
                SELECT page.*, totals.total FROM (SELECT count(*) AS total FROM connections) AS totals
                LEFT JOIN page ON 1 ORDER BY page.count DESC, page.sender, page.recipient
            """, args))
            connection_total = rows[0]["total"]
            connections = [self._edge(row) for row in rows if row["sender"] is not None]
            strongest = connections if not offset else [
                self._edge(row) for row in db.execute(CONNECTIONS + """
                    SELECT * FROM connections ORDER BY count DESC, sender, recipient LIMIT :limit
                """, args)
            ]
            live = requested_index is None and requested_through is None
            nodes, edges = self._graph_nodes(db, seq, member, strongest, live)
        return dict(index=index, total=total, through=through, seq=seq, created_at=stamp, nodes=nodes,
                    edges=edges, connections=connections, connections_total=connection_total,
                    next_offset=offset + PAGE_SIZE if offset + PAGE_SIZE < connection_total else None)

    @staticmethod
    def _graph_nodes(db, seq, member, edges, live):
        names = {}
        endpoints = [name for edge in edges for name in (edge["from"], edge["to"])]
        if member:
            endpoints.sort(key=lambda name: member not in name.lower())
        for name in endpoints:
            names.setdefault(name, None)
            if len(names) == GRAPH_NODES:
                break
        if len(names) < GRAPH_NODES:
            for (name,) in db.execute("""
                SELECT actor FROM events WHERE seq <= ? AND actor <> ''
                AND (? = '' OR instr(board_lower(actor), ?) > 0)
                GROUP BY actor ORDER BY max(seq) DESC LIMIT ?
            """, (seq, member, member, GRAPH_NODES)):
                names.setdefault(name, None)
                if len(names) == GRAPH_NODES:
                    break
        active, known = set(), set()
        if live:
            for name, run in db.execute("SELECT name, active FROM workers ORDER BY name"):
                if (not member or member in name.lower()) and len(names) < GRAPH_NODES:
                    names.setdefault(name, None)
                if name in names:
                    known.add(name)
                    if run:
                        active.add(name)
        statuses = last_statuses(db, seq, list(names))
        nodes = []
        for name in names:
            status = statuses.get(name)
            if name in active:
                status = "running"
            elif name in known and status == "running":
                status = None
            nodes.append({"name": name, "status": status})
        return nodes, [edge for edge in edges if edge["from"] in names and edge["to"] in names]
