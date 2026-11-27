# LocalSwarm design

LocalSwarm separates what the agents **say**, what the scheduler **does**, and what actually **lands in Git**.

## Coordination and replay

The long-lived, loopback-only service owns the SQLite board and worker scheduler. MCP clients connect and disconnect independently. The coordinator assigns scoped work and previews integration; worker capabilities allow reading and posting on the board; the browser is read-only. Only the terminal operator can authorize integration. A scheduled role can also submit the structured report for its own task.

Workers receive a brief and a Git worktree. An addressed post queues input for that member. The scheduler claims posts in order, combines pending mentions into one turn, and runs no more than one turn per member at a time. A waiting post is not a process receipt: the service marks a run finished after the process returns. A restart marks an outstanding claim interrupted rather than automatically sending its prompt again.

Replay reads persisted events in sequence. The mention graph projects message traffic, not task dependencies or tool actions. The Plans tab shows the latest saved task state separately. Scrubbing the timeline never invokes a model or replays a side effect.

The scheduler scans each worker's unclaimed sequence range once and only loads addressed rows. A durable claim advances through the range the scheduler inspected. New posts and completed turns wake the scheduler. It also checks the target Git revision for changes made outside the service. Browser requests use paginated message and graph views and separate member and plan summaries. The legacy snapshot remains available for existing consumers.

Named recipients and `@plan:<id>` expand to concrete board recipients before a post is stored. Plan group routing includes spawned members of the current unfinished attempts. The main coordinator reads compact plan state and its paginated inbox. The inbox includes explicit board messages addressed to that actor. Scheduler output and peer-only handoffs stay in the full history.

## Task plans and evidence

A plan is a persisted directed acyclic graph, specified by the operator or proposed by the coordinator. Each task defines its dependencies, change scope, acceptance criterion, implementation role and provider selection. The scheduler starts independent eligible tasks and waits for prerequisites to be verified and present in the target branch.

Implementation and verification are separate phases of each attempt. An implementation worker reports readiness for its exact commit. A verifier receives a worktree pinned to that commit and reports evidence with a pass or rework verdict. Rework can schedule a fresh attempt within the task's limit. A passing report and an integrated commit are separate states. Free-text board posts cannot satisfy a task gate.

Codex routing uses the submitting chat's coordinator budget, the configured model tiers, and the authenticated Codex `model/list` capabilities. The operator can replace the tier policy. A service started with the legacy fixed coordinator flags enforces that additional ceiling. `swarm_policy` previews routes for a supplied coordinator without creating worktrees.

The service saves the coordinator budget and resolved model and effort for each role at submission. Retries, restarts, and addressed turns use those saved assignments. Dispatch checks whether the saved pair is still available and reports a blocker when it cannot be used. A new coordinator setting applies to subsequent submissions. Legacy records without a coordinator budget need the legacy service flags to dispatch.

## Provider sessions and Git

The Codex adapter uses `codex exec --json` and `codex exec resume`; the Claude Code adapter uses print mode, structured output, and `--resume`. A saved provider session ID restores conversation context in a new process, not a suspended process or in-flight command. The mock adapter exercises the scheduler without contacting a provider.

A worker's proposed Git revision and its target base are explicit. A verifier can be pinned to the candidate revision. Merge authorization is separate from an agent report: the terminal operator approves the exact pair, and the service checks the clean target, expected revisions, and paths across the commit range before a fast-forward. When parallel proposals diverge, the target rejects the stale fast-forward; build on the updated base and verify the replacement commit before trying to integrate it. Worktree and merge checks do not replace the operator-provisioned launcher for process and credential isolation.

Run the local tests with `python -m unittest discover -s tests -v` (or `python3` where that is the interpreter name). Windows uses an exclusive byte-range lock for the board database and launches native `.exe` or `.py` worker wrappers without a shell.

On Windows, a CLI that emits `turn.completed` and stalls while exiting is reaped after a short shutdown grace period. The scheduler records the completed turn after the process tree is stopped. A worker without a complete provider receipt still fails on timeout.

The Windows host starts checkout services on demand. Live HTTP requests, active MCP leases, outstanding worker work, and tasks that can advance via Git keep an idle-enabled service awake. Worktree archiving tracks eligibility in the worker record and pins the integrated revision under `refs/localswarm/archived/` before asking Git to remove the worktree. The archive state can be reconciled after interruption. A later turn restores the worktree and uses the saved provider session and budget.

## Related systems

- [Google Antigravity Swarm](https://antigravity.google/docs/swarm/) inspired the coordinator, worker, verifier, shared board, and replay of public mentions. LocalSwarm makes its own persistence and worker lifecycle choices.
- [Google Antigravity Orchestrator](https://antigravity.google/docs/models/#orchestrator) describes planner and executor handoffs. [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents) and [Codex models](https://learn.chatgpt.com/docs/models) document keeping execution details out of the main chat and setting model and reasoning effort.
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) documents JSONL events and resuming by session ID. The [Codex App Server model list](https://learn.chatgpt.com/docs/app-server#list-models-modellist) supplies model and effort capabilities. LocalSwarm runs worker turns through the CLI.
- [Claude Code headless mode](https://code.claude.com/docs/en/headless) documents structured output and session resumption. [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams) is a separate orchestration feature.
