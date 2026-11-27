"""List this Windows user's LocalSwarm boards and open the selected one."""

from contextlib import closing
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import sqlite3
from subprocess import SubprocessError
from threading import Thread
import tkinter as tk
from tkinter import ttk


STATE = Path(__file__).resolve().parent
ENTRY = re.compile(r"Private board: (http://127\.0\.0\.1:(\d+)(/open/[A-Za-z0-9_-]+))")


@dataclass(frozen=True)
class Board:
    checkout: Path
    state: Path
    status: str
    plans: int | None
    active: int | None


def count(path, query):
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.3)) as db:
            return db.execute(query).fetchone()[0]
    except sqlite3.Error:
        return None


def board_url(state, port):
    with (state / "service.log").open(encoding="utf-8", errors="replace") as log:
        entry = ENTRY.fullmatch(log.readline().strip())
    if entry is None or int(entry[2]) != port:
        return None
    try:
        with closing(HTTPConnection("127.0.0.1", port, timeout=.6)) as http:
            http.request("GET", entry[3])
            with http.getresponse() as response:
                if response.status == 303 and response.getheader("Location") == "/":
                    return entry[1]
    except (OSError, HTTPException):
        return None
    return None


def scan():
    boards = []
    for state in (STATE / "LocalSwarm", *(STATE / "projects").glob("*")):
        try:
            endpoint = json.loads((state / "endpoint.json").read_text(encoding="utf-8"))
            checkout, port = Path(endpoint["repo"]), endpoint["port"]
            if not checkout.is_absolute() or type(port) is not int or not 1 <= port <= 65535:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        status = "仓库不可用"
        if checkout.is_dir():
            status = "休眠" if endpoint.get("status") == "asleep" else "离线"
            if status != "休眠":
                try:
                    if board_url(state, port):
                        status = "在线"
                except OSError:
                    pass
        boards.append(Board(checkout, state, status,
                            count(state / "board.db.plans", "SELECT count(*) FROM plans"),
                            count(state / "board.db", "SELECT count(*) FROM workers WHERE active IS NOT NULL")))
    return sorted(boards, key=lambda board: (board.status != "在线", -(board.active or 0),
                                             str(board.checkout).casefold()))


def wake_board(board):
    spec = spec_from_file_location("localswarm_host", STATE / "global-swarm-windows.py")
    host = module_from_spec(spec)
    spec.loader.exec_module(host)
    checkout = host.git_checkout(board.checkout)
    if host.project_state(checkout).resolve() != board.state.resolve():
        raise ValueError("LocalSwarm checkout does not match the selected board.")
    port, _ = host.ensure_service(checkout)
    entry = board_url(board.state, port)
    if not entry:
        raise RuntimeError("LocalSwarm did not return a board link.")
    return entry


class Picker:
    def __init__(self, window, open_url=os.startfile, request_board=wake_board):
        self.window, self.open_url = window, open_url
        self.request_board = request_board
        self.boards = []
        self.results = Queue()
        self.busy = False
        window.title("LocalSwarm 看板")
        window.geometry("760x370")
        window.minsize(540, 280)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="选一个仓库打开看板。仓库内的 Swarm 计划在 Plans 页。", wraplength=490).pack(anchor="w", pady=(0, 10))
        table = ttk.Frame(frame)
        self.tree = ttk.Treeview(table, columns=("repo", "status", "plans", "workers"), show="headings", selectmode="browse")
        for column, label, width in (("repo", "Git 仓库", 280), ("status", "看板状态", 95),
                                     ("plans", "计划", 75), ("workers", "运行中 Worker", 135)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, minwidth=width if column != "repo" else 120,
                             stretch=column == "repo", anchor="w" if column == "repo" else "center")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.show_selection)
        self.tree.bind("<Double-1>", self.open_selection)
        self.tree.bind("<Return>", self.open_selection)

        self.detail = tk.StringVar()
        actions = ttk.Frame(frame)
        actions.pack(side="bottom", fill="x")
        path = ttk.Label(frame, textvariable=self.detail, wraplength=700, justify="left")
        path.pack(side="bottom", fill="x", pady=(8, 8))
        path.bind("<Configure>", lambda event: path.configure(wraplength=max(100, event.width - 8)))
        table.pack(fill="both", expand=True)
        self.refresh_button = ttk.Button(actions, text="刷新", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.open_button = ttk.Button(actions, text="打开看板", command=self.open_selection, state="disabled")
        self.open_button.pack(side="right")
        self.refresh()

    def selected(self):
        selected = self.tree.selection()
        return self.boards[int(selected[0])] if selected else None

    def refresh(self):
        active = self.selected()
        checkout = active.checkout if active else None
        self.run_job(scan, lambda rows: self.show_results(rows, checkout), "正在读取本机看板…",
                     "无法读取本机看板。请重试。")

    def run_job(self, job, completed, progress, failure):
        if self.busy:
            return
        self.busy = True
        self.refresh_button.state(["disabled"])
        self.open_button.state(["disabled"])
        self.detail.set(progress)

        def work():
            try:
                self.results.put((True, job()))
            except (OSError, ValueError, RuntimeError, ImportError, HTTPException, KeyError, SubprocessError):
                self.results.put((False, failure))

        Thread(target=work, daemon=True).start()
        self.window.after(40, self.finish_job, completed)

    def finish_job(self, completed):
        try:
            success, result = self.results.get_nowait()
        except Empty:
            self.window.after(40, self.finish_job, completed)
            return
        self.busy = False
        self.refresh_button.state(["!disabled"])
        if success:
            completed(result)
        else:
            self.show_selection()
            self.detail.set(result)

    def show_results(self, boards, checkout):
        self.boards = boards
        previous = None
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for i, board in enumerate(self.boards):
            self.tree.insert("", "end", iid=str(i), values=(board.checkout.name, board.status,
                             board.plans if board.plans is not None else "—", board.active if board.active is not None else "—"))
            if board.checkout == checkout:
                previous = str(i)
        if self.boards:
            self.tree.selection_set(previous or "0")
            self.tree.focus(previous or "0")
            self.tree.focus_set()
        self.show_selection()

    def show_selection(self, _event=None):
        if self.busy:
            return
        board = self.selected()
        self.open_button.state(["!disabled"] if board and board.status != "仓库不可用" else ["disabled"])
        if board:
            hints = {"休眠": "打开看板时会启动服务。", "离线": "打开看板时会尝试启动服务。",
                     "仓库不可用": "找不到保存的仓库路径。"}
            hint = hints.get(board.status)
            self.detail.set(str(board.checkout) + ("\n" + hint if hint else ""))
        else:
            self.detail.set("还没有发现看板。在 Git 仓库里用 $swarm 后刷新。")

    def open_selection(self, _event=None):
        board = self.selected()
        if board and board.status != "仓库不可用":
            self.run_job(lambda: self.request_board(board), self.show_board, "正在打开 " + board.checkout.name + "…",
                         "无法启动本机看板。请确认仓库仍可用，然后重试。")

    def show_board(self, entry):
        try:
            self.open_url(entry)
        except OSError:
            self.show_selection()
            self.detail.set("无法打开默认浏览器。请检查 Windows 的默认浏览器设置。")
        else:
            self.refresh()


if __name__ == "__main__":
    window = tk.Tk()
    Picker(window)
    window.mainloop()
