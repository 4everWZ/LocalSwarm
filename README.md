# LocalSwarm

Run a team of coding agents on your machine and see what each one did. Keep your Codex or Claude Code session as coordinator, delegate work to separate Git worktrees, and follow the conversation in a local browser. Inspect the diff a verifier checked and approve the exact revision from your terminal.

- **Board** shows posts and run events in order. **Members** shows who's working or waiting.
- **Graph** replays who mentioned whom as you move through the timeline. **Plans** shows current dependencies, task states, and verification evidence.
- **Workers** run a turn, exit, and resume their saved conversation when another member mentions them.

## Quickstart

You'll need Python 3.10+ and Git on Linux, macOS, or Windows through WSL. No Python packages or model credentials are required for the demo.

From the LocalSwarm checkout, start a service against a disposable Git repository:

```bash
DEMO="$(mktemp -d)"
git init -q "$DEMO/repo"
git -C "$DEMO/repo" -c user.name=Demo -c user.email=demo@example.invalid \
  commit --allow-empty -qm initial
printf 'Demo directory: %s\n' "$DEMO"
python3 swarm.py serve --repo "$DEMO/repo" --db "$DEMO/state/board.db" \
  --owner-file "$DEMO/state/coordinator.key"
```

Open the private board URL printed by the service. In a second terminal, from the same checkout, set `DEMO` to the printed directory and start a mock worker:

```bash
DEMO=/path/to/printed/demo
python3 swarm.py mcp --owner-file "$DEMO/state/coordinator.key" <<'JSONL'
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"swarm_spawn","arguments":{"name":"builder","role":"worker","brief":"Review the demo repository","allowed_paths":["README.md"]}}}
JSONL
```

Once the worker's first turn has ended, mention it to resume:

```bash
python3 swarm.py mcp --owner-file "$DEMO/state/coordinator.key" <<'JSONL'
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"board_post","arguments":{"mentions":["@builder"],"content":"Summarize what you would inspect next."}}}
JSONL
```

The mock backend posts a receipt without editing the target repository. Switch to **Graph** and scrub the timeline to see when the mention appeared.

## Connect an interactive coordinator

Start the service with your target Git checkout and a private state directory **outside** that checkout. The service stays running while your client connects over stdio MCP. Use absolute paths in the client config; replace the sample `python` command with `python3` or your interpreter's absolute path if needed.

| Client | Configuration | Optional shortcut |
| --- | --- | --- |
| Codex | Add the [MCP server example](examples/codex.toml) to your Codex configuration. | Copy the [Swarm skill](examples/codex/swarm/SKILL.md) into your project's `.agents/skills/swarm/` and invoke `$swarm`. |
| Claude Code | Adapt the [MCP server example](examples/claude.mcp.json) as your project's `.mcp.json`. | Copy the [Swarm command](examples/claude/commands/swarm.md) into `.claude/commands/` and invoke `/swarm`. |

Try: “Use the Swarm MCP tools to split this change into independent tasks, give each worker its own paths and acceptance criteria, and report what the verifier found.” Choose a read-only client session if you want a coordination-only role; adding an MCP server or shortcut doesn't change the client's permissions.

### Run actual models

The backend is `mock` unless you select another one. To make `codex` or `claude` available to workers, add one or both of these options to the service command:

```bash
--codex-launcher /absolute/path/to/isolated-codex-launcher
--claude-launcher /absolute/path/to/isolated-claude-launcher
```

Provision the provider CLI, credentials, and an executable launcher that confines each member to its worktree and grants only the required access. The launcher receives the CLI executable and its arguments separately, with the prompt on stdin. It must keep operator credentials and state out of the worker's reach. A Git worktree isolates edits, not process permissions. The adapters have not yet been validated end to end against live provider accounts.

### Plan, verify, integrate

Write the task list yourself or ask the interactive coordinator to propose it. Give each task its own ID, implementation role, backend and model, allowed paths, prerequisites, and a specific acceptance criterion. Use dependencies where a task needs another task's code in the target checkout.

Submit the task list with `swarm_plan`; use `swarm_status` to read the plan and its evidence. For example, the `swarm_plan` arguments for one documentation task are:

```json
{
  "plan_id": "docs",
  "goal": "Document startup",
  "tasks": [
    {
      "id": "startup",
      "dependencies": [],
      "role": "worker",
      "backend": "codex",
      "model": "default",
      "allowed_paths": ["README.md"],
      "criterion": "README shows the startup command and required inputs."
    }
  ]
}
```

Use an enabled backend; `default` selects the provider's configured model. A member reports evidence via `swarm_report`. With verdict `ready`, it can omit the SHA and let the service checkpoint its in-scope changes. A verifier's `pass` or `rework` names the exact candidate SHA.

The scheduler starts eligible tasks, gives a verifier the exact commit to check, and records its evidence. A verifier can request rework within the task's attempt limit. Review passing commits in **Plans** before integrating them from the terminal. A dependent task waits until its prerequisite's verified commit has landed.

Use `swarm_merge` to preview the worker revision and target base. For a manually spawned worker, request a pinned verifier with `review_worker` and `review_revision` on `swarm_spawn`. Then use the worker name, revision, and base from the preview:

```bash
python3 swarm.py merge "$WORKER" --revision "$WORKER_SHA" --base-revision "$BASE_SHA"
```

Enter the operator code printed at service startup. The operation requires a clean target, the expected base and worker revisions, and allowlisted paths across the commits. It only fast-forwards; models don't receive the operator code.

See [Design and provider references](DESIGN.md) for the event model, isolation boundaries, and test command.
