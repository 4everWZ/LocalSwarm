"""Native Codex launcher using MXC and a worktree-scoped permission profile."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    if os.name != "nt" or sys.argv[1:3] != ["codex", "-C"]:
        raise SystemExit("Use this launcher for native Windows Codex workers")
    binary = Path(os.environ.get("LOCALSWARM_CODEX_EXE", ""))
    if not binary.is_absolute() or binary.name.lower() != "codex.exe" or not binary.is_file():
        raise SystemExit("Set LOCALSWARM_CODEX_EXE to the absolute path of your trusted Codex executable")

    worktree = Path(sys.argv[3]).resolve()
    args = sys.argv[4:]
    if worktree != Path.cwd().resolve() or args[:2] not in (["-s", "read-only"], ["-s", "workspace-write"]):
        raise SystemExit("Expected a LocalSwarm Git worktree and sandbox role")
    mode = "read" if args[1] == "read-only" else "write"
    prefix, separator, gitdir = (worktree / ".git").read_text(encoding="utf-8").strip().partition(": ")
    if prefix != "gitdir" or not separator or not gitdir:
        raise SystemExit("Expected a linked Git worktree")
    worktree_git = (worktree / gitdir).resolve(strict=True)
    common_git = (worktree_git / (worktree_git / "commondir").read_text(encoding="utf-8").strip()).resolve(strict=True)

    # The CLI uses its own login and can reach the provider. Its worker commands
    # run in MXC with a separate filesystem and network policy.
    workspace = f'{{ "." = "{mode}", ".git" = "read", ".codex" = "read", ".agents" = "read" }}'
    git_metadata = json.dumps(common_git.as_posix(), ensure_ascii=False)
    filesystem = ('{ ":root" = "deny", ":minimal" = "read", '
                  f'{git_metadata} = "read", ":workspace_roots" = {workspace} }}')
    config = ["-c", 'windows.sandbox="mxc"', "-c", 'default_permissions="localswarm"',
              "-c", f'permissions.localswarm={{ filesystem = {filesystem}, network = {{ enabled = false }} }}',
              "-c", 'features.apps=false', "-c", 'web_search="disabled"']
    return subprocess.call([str(binary), "-C", str(worktree), *config, *args[2:]])


if __name__ == "__main__":
    sys.exit(main())
