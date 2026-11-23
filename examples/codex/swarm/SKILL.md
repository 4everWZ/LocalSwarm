---
name: swarm
description: Coordinate authorized local development through the LocalSwarm MCP server.
---

Keep the main chat as the coordinator. Capture scope, split independent work, decide tradeoffs, and review the evidence. Delegate implementation, debugging, builds, and tests to workers and verifiers. When a worker needs a decision, answer on the board and let it continue in its own session.

Check `swarm_policy` before submitting. Use the main chat's actual Codex model and reasoning effort in `coordinator` when the session differs from the service setting. Resolve an unknown active setting before delegating with a live backend.

Give each task an ID, concrete allowed paths, a backend, a `task_level`, and one verifiable acceptance criterion. Use `routine` for bounded edits and mechanical checks, `standard` for ordinary implementation, `demanding` for deeper work on the lower-tier model, and `complex` for work that merits the main model. Let the service select the model and effort by default. Set `model`, `reasoning_effort`, `verifier_model`, or `verifier_reasoning_effort` when the work calls for a specific in-budget choice. Add a dependency when the later task needs the earlier task's integrated work. Submit with `swarm_plan`.

Give agents the names of their peers and ask them to route handoffs directly with `@name` or `@plan:<plan-id>` on the same board. Ask them to mention `@coordinator` only for decisions and blockers. Use `swarm_status` with `compact: true` for state and evidence and `swarm_inbox` with its `next_seq` cursor for messages addressed to you. Use `board_read` for targeted investigations that need the full log. Surface meaningful decisions, blockers, verifier evidence, and revisions ready for review. `swarm_merge` previews integration. The terminal operator approves the revision and integrates it.
