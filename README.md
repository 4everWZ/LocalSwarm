# LocalSwarm

Run a team of coding agents on your machine and see what each one did. Keep your Codex or Claude Code session as coordinator, delegate work to separate Git worktrees, and follow the conversation in a local browser. Inspect the diff a verifier checked and approve the exact revision from your terminal.

- **Board** shows posts and run events in order. **Members** shows who's working or waiting.
- **Graph** replays who mentioned whom as you move through the timeline. **Plans** shows current dependencies, task states, and verification evidence.
- **Workers** run a turn, exit, and resume their saved conversation when another member mentions them.

## Quickstart

You'll need Python 3.10+ and Git on Linux, macOS, or native Windows. No Python packages or model credentials are required for the demo.

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

### Windows PowerShell

From the checkout, run:

```powershell
$demo = Join-Path $env:LOCALAPPDATA ("LocalSwarm-demo-" + [guid]::NewGuid().ToString('N'))
git init -q "$demo/repo"
git -C "$demo/repo" -c user.name=Demo -c user.email=demo@example.invalid commit --allow-empty -qm initial
Write-Output $demo
python swarm.py serve --repo "$demo/repo" --db "$demo/state/board.db" --owner-file "$demo/state/coordinator.key"
```

Open the printed board URL. In a second PowerShell terminal, set `$demo` to the printed directory and run:

```powershell
'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"swarm_spawn","arguments":{"name":"builder","role":"worker","brief":"Review the demo repository","allowed_paths":["README.md"]}}}' |
  python swarm.py mcp --owner-file "$demo/state/coordinator.key"
'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"board_post","arguments":{"mentions":["@builder"],"content":"Summarize what you would inspect next."}}}' |
  python swarm.py mcp --owner-file "$demo/state/coordinator.key"
```

## Connect an interactive coordinator

Start the service with your target Git checkout and a private state directory **outside** that checkout. On Windows use a per-user directory such as `%LOCALAPPDATA%\LocalSwarm`, with ACLs that exclude other worker identities. The service stays running while your client connects over stdio MCP. Use absolute paths in the client config. Windows paths can use forward slashes inside TOML strings.

| Client | Configuration | Optional shortcut |
| --- | --- | --- |
| Codex | Add the [MCP server example](examples/codex.toml) to your Codex configuration. | Copy the [Swarm skill](examples/codex/swarm/SKILL.md) into your project's `.agents/skills/swarm/` and invoke `$swarm`. |
| Claude Code | Adapt the [MCP server example](examples/claude.mcp.json) as your project's `.mcp.json`. | Copy the [Swarm command](examples/claude/commands/swarm.md) into `.claude/commands/` and invoke `/swarm`. |

Try: “Use the Swarm MCP tools to split this change into independent tasks, give each worker its own paths and acceptance criteria, and report what the verifier found.” Choose a read-only client session if you want a coordination-only role; adding an MCP server or shortcut doesn't change the client's permissions.

### Run actual models

The backend is `mock` unless you select another one. For a Codex coordinator, add a launcher to the service command.

```bash
--codex-launcher /absolute/path/to/isolated-codex-launcher
```

To use Claude Code, start a separate service with `--claude-launcher` and its own state directory. The Codex budget does not define a cross-provider model ranking.

Provision the provider CLI, credentials, and a launcher that confines each member to its worktree. On Windows the launcher can be an `.exe` or a `.py` script. Elsewhere use an executable file. The first argument is the provider command name, followed by its arguments. The prompt arrives on stdin and the launcher must relay the provider's stdout and exit code. The launcher is responsible for process isolation, keeping operator credentials and state inaccessible to worker tools, and applying the native [Codex Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox) when using Codex on Windows. The launcher must stay attached until its provider exits.

With a native Codex installation and working MXC sandbox, use the [Windows launcher](examples/codex/windows_launcher.py) from PowerShell:

```powershell
$env:LOCALSWARM_CODEX_EXE = (Get-Command codex.exe).Source
python swarm.py serve --repo C:/path/to/repo --db "$env:LOCALAPPDATA/LocalSwarm/project/board.db" `
  --owner-file "$env:LOCALAPPDATA/LocalSwarm/project/coordinator.key" `
  --codex-launcher examples/codex/windows_launcher.py
```

### Model and reasoning budgets

For each live Codex `swarm_plan` or `swarm_spawn`, pass `coordinator.model` and `coordinator.reasoning_effort` from the active chat. Call `swarm_policy` with the same `coordinator` to preview the routes before submitting. Starting the service with both `--coordinator-model` and `--coordinator-reasoning-effort` keeps the legacy fixed ceiling and supplies the default coordinator for calls that omit it. Omit those startup flags to let each new plan follow the current chat.

| Task level | Target effort | Astra Medium coordinator | Sol High coordinator |
| --- | --- | --- | --- |
| `routine` or `standard` | Medium | Sol Medium | Terra Medium |
| `demanding` | High | Sol High | Terra High |
| `complex` | Extra High | Sol High | Terra Extra High |

Automatic routing prefers the nearest lower tier with a model and supported effort available. If no eligible lower tier is available, the service uses the coordinator's model at a supported effort. `model_tier: "coordinator"` requests the exact active model directly. Both choices use the task's target effort. Same-tier effort is capped by the active coordinator. Lower tiers may go one step above that effort, up to Extra High. Max requires an explicit same-tier choice and provider support. Ultra is reserved for the coordinator.

Use `model` and `reasoning_effort` for explicit choices. For a verifier, use `verifier_model`, `verifier_reasoning_effort`, or `verifier_model_tier`. The service rejects unsupported or over-budget explicit choices. Use `low` for Light and `xhigh` for Extra High. Actual token use depends on the turn.

The [bundled routing policy](routing-policy.json) defines model tiers. Use `--routing-policy PATH` to supply your own JSON policy with `tiers` ordered from lightest to strongest. Each tier has a `name` and an ordered `models` list of model IDs or case-sensitive glob patterns. Earlier entries take priority within a tier. Use ordered exact model IDs to define the relative capability of variants in the same tier. A model outside the configured tiers can still delegate to itself when it is available.

The service uses the authenticated [Codex model catalog](https://learn.chatgpt.com/docs/app-server#list-models-modellist) for available models and supported efforts. Run the catalog and worker under the same provider account. On Windows, the service discovers the current Codex Desktop CLI under `%LOCALAPPDATA%\OpenAI\Codex\bin` and checks again on each dispatch. `--codex-executable PATH` explicitly pins a CLI. A custom `LOCALSWARM_CODEX_EXE` takes precedence over Desktop discovery, and `PATH` is the fallback. The bundled Windows launcher uses the CLI that the service used to validate the assignment.

Plans and workers keep the coordinator budget and assignments saved at submission. Later changes in the active chat affect subsequent submissions. A resumed worker or retried task uses its pinned route. If that route becomes unavailable, the coordinator receives a blocker. To run records created before budget snapshots, start the service with the applicable legacy fixed ceiling.

### Board communication

Workers use `board_post` with `@name` to hand work between peers. `@plan:<plan-id>` routes to the current members of another plan on the same service. `@coordinator` sends a decision or blocker to the main chat's `swarm_inbox`. The main chat uses `swarm_status` with `compact: true` for progress and verification.

Open the private board URL for the checkout. **Board** shows recent messages, assignments, results, and blockers. Search or filter by member to find earlier messages, and use **Filters → All events** to include routine scheduler and plan activity. **Follow** advances the feed when you are at the latest message. Select a message to expand it.

**Members** shows current roles, model settings, and status. Select a member to filter the Board to messages from or to them. **Plans** lists the repository's plans. Open one for its **Overview**, which shows the goal, progress, blockers, tasks in review, and tasks awaiting integration. The plan's roster shows member activity separately. Open a task for its dependencies, acceptance criteria, and review evidence. **Graph** has the mention timeline and replay controls. Replay leaves the other tabs live.

The board loads messages in pages of 50 and holds up to five pages. Search and member filters cover the saved history. Reopening the board loads the latest messages. An open board keeps its repository service running while the connection is live.

### Service and worktree lifecycle

The Windows on-demand host stops a repository service after 30 minutes with no active board, MCP lease, queued or running work, or plan work waiting on Git integration. Open a sleeping checkout in **LocalSwarm 看板** or call a Swarm tool to start it again. A manually launched service stays running by default. Set `serve --idle-seconds N` to enable idle shutdown.

A clean plan worktree becomes eligible for removal after its reviewed commit is integrated. LocalSwarm checks for active work, pending messages, local changes, untracked files, and ignored files. The seven-day retention period begins with the first successful check. LocalSwarm pins the revision in Git before removing the worktree and restores the original path before a later worker turn. History and verification evidence remain available. Use `serve --archive-days 0` to disable automatic worktree removal.

Run `python swarm.py --port PORT archive-status --owner-file OWNER_FILE` to inspect candidates and skip reasons against a running repository service.

### Plan, verify, integrate

Write the task list yourself or ask the interactive coordinator to propose it. Give each task its own ID, implementation role, backend, allowed paths, prerequisites, and a specific acceptance criterion. Use dependencies where a task needs another task's code in the target checkout.

Submit the task list with `swarm_plan`; use `swarm_status` to read the plan and its evidence. For example, the `swarm_plan` arguments for one documentation task are:

```json
{
  "plan_id": "docs",
  "goal": "Document startup",
  "coordinator": {"model": "gpt-6.2-astra", "reasoning_effort": "medium"},
  "tasks": [
    {
      "id": "startup",
      "dependencies": [],
      "role": "worker",
      "backend": "codex",
      "task_level": "standard",
      "allowed_paths": ["README.md"],
      "criterion": "README shows the startup command and required inputs."
    }
  ]
}
```

For Codex, omitting `model` or using `default` selects the budget-based route. A member reports evidence via `swarm_report`. With verdict `ready`, it can omit the SHA and let the service checkpoint its in-scope changes. A verifier's `pass` or `rework` names the exact candidate SHA.

The scheduler starts eligible tasks, gives a verifier the exact commit to check, and records its evidence. A verifier can request rework within the task's attempt limit. Review passing commits in **Plans** before integrating them from the terminal. A dependent task waits until its prerequisite's verified commit has landed.

Use `swarm_merge` to preview the worker revision and target base. For a manually spawned worker, request a pinned verifier with `review_worker` and `review_revision` on `swarm_spawn`. Then use the worker name, revision, and base from the preview:

```bash
python3 swarm.py merge "$WORKER" --revision "$WORKER_SHA" --base-revision "$BASE_SHA"
```

Enter the operator code printed at service startup. The operation requires a clean target, the expected base and worker revisions, and allowlisted paths across the commits. It only fast-forwards; models don't receive the operator code.

See [Design and provider references](DESIGN.md) for the event model, isolation boundaries, and test command.
