---
name: swarm
description: Coordinate authorized local development through the LocalSwarm MCP server.
---

Keep the main chat as the coordinator. Capture scope, split independent work, decide tradeoffs, and review the evidence. Delegate implementation, debugging, builds, and tests to workers and verifiers. When a worker needs a decision, answer on the board and let it continue in its own session.

For each live Codex submission, use the main chat's current model and reasoning effort in `coordinator`. When the active settings are unavailable, ask the coordinator to confirm them before delegation. Call `swarm_policy` with that coordinator to preview the routes. Existing workers resume using their saved assignments.

Give each task an ID, concrete allowed paths, a backend, a `task_level`, and one verifiable acceptance criterion. Set `routine` or `standard` for Medium, `demanding` for High, and `complex` for Extra High. Let the service prefer an available lower tier and cap effort within the coordinator budget. Use `model_tier: "coordinator"` only when the task needs the main model, or `verifier_model_tier: "coordinator"` when verification needs it. Set `model`, `reasoning_effort`, `verifier_model`, or `verifier_reasoning_effort` for specific in-budget values. Add a dependency when the later task needs the earlier task's integrated work. Submit with `swarm_plan`.

Give agents the names of their peers and ask them to route handoffs directly with `@name` or `@plan:<plan-id>` on the same board. Ask them to mention `@coordinator` only for decisions and blockers. Use `swarm_status` with `compact: true` for state and evidence and `swarm_inbox` with its `next_seq` cursor for messages addressed to you. Use `board_read` for targeted investigations that need the full log. Surface meaningful decisions, blockers, verifier evidence, and revisions ready for review. `swarm_merge` previews integration. The terminal operator approves the revision and integrates it.
