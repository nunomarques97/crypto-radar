# Development and Codex setup

This guide is for `<repo>`. It separates the AI tool used to develop the application from the application's local-only inference requirement. Codex development may use your ChatGPT subscription; the deployed radar must not acquire an OpenAI/Anthropic inference dependency.

## 1. Tools and environment

Verified on 2026-09-14: Python 3.12.10, Node 24.14.0, Git, Codex desktop/CLI and Ollama are installed. Current global packages: requests 2.34.2, pywebview 6.2.1, PyInstaller 6.22.3; optional legacy anthropic 1.5.0. The new requirements files record these observed direct dependencies, not a complete transitive lock. Do not install Anthropic for the target runtime.

Create a dedicated environment rather than depending on global packages:

```powershell
Set-Location <repo>
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-ui.txt
```

Use the explicit interpreter path so PowerShell activation policy is irrelevant. The base requirements are enough for headless analysis/tests; UI adds PyWebView. Build dependencies are separate. A clean installation/build has not been performed by this takeover audit and remains an R0 verification item. Do not claim that direct pins are a tested lockfile.

Ollama is needed only for real local inference; Node runs the frontend tests. The UI uses the Windows WebView runtime; validate the installed runtime with a controlled source UI smoke test before packaging. The frozen UI still needs the source project and a real Python executable. Set `RADAR_PYTHON_EXE` to the venv interpreter when launching the packaged UI; do not point it to the Control Room executable.

## 2. Sign in without putting secrets in prompts

Use the desktop sign-in flow with your ChatGPT account. For CLI, run `codex login` and complete the browser flow. ChatGPT subscription sign-in and usage-based API-key sign-in are different options; this workflow does not require you to create an API key. Available models/limits depend on your account. Never paste authentication tokens or `.env` contents into a task. [Official authentication guidance](https://learn.chatgpt.com/docs/auth).

## 3. Open the right folder and select the role

In desktop, add/open `<repo>` as the local project and verify that exact working folder in the task. Do not use the whole home directory as the normal implementation workspace, and do not open Sextant as the target. This takeover started with a broader workspace solely to inspect both projects.

Use desktop Codex for implementation and diff review on Windows. Use a PO conversation/Work session for research, specifications and reviewing artifacts, but bring decisions into these files before implementation. Cloud work cannot directly inspect your `C:\` folder without an explicitly provided remote repository/environment; it also does not validate native Windows packaging. Prefer local execution for this project. [Official desktop guidance](https://learn.chatgpt.com/docs/app).

For PO work, choose Terra and the highest available reasoning setting in your picker. This session exposed Terra `ultra` for the delegated architectural review; public API documentation lists a different effort set, so do not transplant settings between interfaces blindly. The current coordinating conversation was not silently switched to Terra. For developers choose the available Codex coding model at **high** reasoning. Record the actual model/effort in each handoff; product runtime model selection is a separate decision.

Optional CLI equivalent:

```powershell
codex -C <repo>
```

Use normal workspace permissions and per-action approval when needed; do not disable safeguards as setup advice. This session's initial shell launch hit a Windows filesystem sandbox configuration error, so narrowly scoped commands used the app's approval review. That is not a repository requirement or a reason to give all future tasks unrestricted access.

## 4. Establish version control before routine parallel work

Audit finding: the primary folder had **no `.git` directory/history**. This takeover did not invent a remote, commit identity, or publish your code. A source-only recovery archive was created outside the project at `<recovery-dir>\2026-09-14-source-before-audit.zip`. It excludes data, secrets, build output and binaries. It is not a database backup.

The first tightly scoped task may use the archived baseline and file allowlist while you establish Git. This is a documented bootstrap exception, not the ongoing workflow. From the repository folder:

```powershell
git init -b main
git status --short
git add -- README.md ARCHITECTURE.md ROADMAP.md DECISIONS.md RISK.md AGENTS.md DEVELOPMENT.md TESTING.md DESIGN.md CLAUDE.md .gitignore requirements.txt requirements-ui.txt requirements-build.txt radar.py _radar_copy_prompt_popup.py _radar_toast_notify.ps1 radar_v08 ui tests docs CryptoRadarControlRoom.spec
git diff --cached --stat
git diff --cached
```

Inspect staged content; `.gitignore` excludes production DB/logs/history, environments, local agent settings and packaged output. Add the reviewed `scripts/` folder, which T001 has now created. Then make your baseline commit with your own configured identity: `git commit -m "Establish audited project baseline"`. If Git asks for identity, configure your real preferred local Git name/email; do not ask an agent to invent them. No remote or GitHub plugin is necessary for local work.

After that, start each change from a clean baseline on a task branch (`git switch -c task/T002-description`) or a Codex worktree. A worktree is a separate checkout of this same Git repository; it needs an initial commit and its own environment/state paths. Share neither a live SQLite file nor a mutable working directory between simultaneous implementation tasks.

## 5. Give Codex bounded context

`AGENTS.md` is the automatic repository instruction entry point. The full architecture is linked, not pasted into that file. Start in the repository root; ask the developer to read the files named there and the specific task brief, and name the instruction sources it loaded. Restart/create a fresh session after changing repository instructions. More-specific instruction files may override broader ones, so review any nested `AGENTS.md`/`AGENTS.override.md`. [Official instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

Every brief supplies CONTEXT, OBJECTIVE, FILES/AREA, INVARIANTS, OUT OF SCOPE, IMPLEMENTATION REQUIREMENTS, TESTS, ACCEPTANCE CRITERIA and DELIVERABLE FORMAT. Task file is the durable specification; a conversation is not the source of truth. File allowlists constrain intent; permissions and review enforce the boundary. Ask Codex to report necessary scope changes before implementing them.

Starter prompt: “Read AGENTS.md and its required documentation, then implement only docs/tasks/T002-environment.md. State your intended files and invariants first. Run the required checks, report all failures/skips, and do not commit or modify production state. Stop at a reviewable result.”

## 6. Verify and review

T001 is accepted; use `.\.venv\Scripts\python.exe scripts/run_tests.py` (or the verified Python interpreter if the venv is not installed yet). Review exact output, not only the final sentence. See `TESTING.md` for the historical failure and Node requirements.

Inspect desktop's changed-files/review panel. In a terminal use `git status --short`, `git diff --stat`, `git diff`, and, after staging, `git diff --cached`. Match every changed file and behavior to the brief. PO review checks contracts, evidence, negative paths, data freshness, side effects and rollback; tests alone do not prove sound architecture. A fresh reviewer should read the diff and tests without trusting the developer's explanation. Commit only the accepted, inspected files. No automatic push/merge.

## 7. Recover and continue

Before discarding work, save the patch and any untracked files you want to retain. For an unwanted uncommitted change, inspect and restore only the named file from the known baseline; never use a broad destructive reset/clean as routine recovery. For a committed change, use `git revert COMMIT` to preserve history. Never roll a live database back by reverting source code.

Without a Git baseline, extract the recovery archive to a **separate comparison directory**, compare the affected file, and copy back only the file you intend to restore. Do not extract over the whole live project. The archive cannot restore new files created after the audit or production data.

At task end update its status/result and relevant docs, then record the next brief ID and unresolved findings. New sessions start with these documents and the task result. Keep source changes, acceptance evidence and decisions together; do not rely on giant chat handovers.

## Build workflow

In a controlled environment after tests:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm CryptoRadarControlRoom.spec
```

The command replaces generated build/dist artifacts. Do not rebuild an executable currently in use. Verify startup, correct state root, interpreter selection, process start/stop and mock-popup safeguard with disposable state. A unit test that monkeypatches `sys.frozen` does not replace that smoke test. This takeover did not rebuild or launch the UI.

## Continuing the completed architecture program

Read docs/PO_HANDOVER.md, then follow the catalog. Both Codex and Claude Code may implement briefs; the PO owns the versioned contracts and independently reviews results. Architecture work does not authorize private/live activation. Future user permission is an execution gate, not a question for a developer to answer. Pin the current accepted profile until empirical promotion rules pass. Do not ask a future developer to invent agent ordering, money policy, stale limits, context selection or order recovery.
