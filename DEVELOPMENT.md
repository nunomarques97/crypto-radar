# Development

The application's inference is local only. Whatever tools you use to develop it, the radar itself must never acquire a dependency on a cloud inference API.

## 1. Environment

Windows 11, Python 3.12, Git. Node is needed only for the frontend tests, and Ollama only for real local inference. The UI uses the Windows WebView runtime.

Create a dedicated environment inside the repository rather than depending on global packages:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-ui.txt -c requirements-lock-windows-py312.txt
```

Use the explicit interpreter path so the PowerShell activation policy does not matter. The base `requirements.txt` is enough for headless analysis and tests; `requirements-ui.txt` adds PyWebView, `requirements-dev.txt` adds ruff and mypy, and `requirements-build.txt` adds PyInstaller. `requirements-lock-windows-py312.txt` is a constraints file resolved on Windows with CPython 3.12.

To check a dependency group without launching the radar:

```powershell
.\.venv\Scripts\python.exe scripts/verify_environment.py --group core
```

The frozen UI still needs the source project and a real Python executable. Set `RADAR_PYTHON_EXE` to the venv interpreter when launching the packaged UI; do not point it at the Control Room executable.

## 2. Never put secrets in prompts or commits

No API key, token or `.env` content belongs in the repository, in a task description or in a log. The radar reads public market data only and needs no credentials.

## 3. Work on isolated changes

Start each change from a clean baseline on its own branch or worktree. A worktree needs its own environment and state paths. Never share a live SQLite file or a mutable working directory between two changes in progress.

## 4. Verify and review

See [TESTING.md](TESTING.md). Before committing, review `git status --short`, `git diff --stat`, `git diff` and, after staging, `git diff --cached`. Tests alone do not prove sound architecture: review contracts, evidence, negative paths, data freshness, side effects and rollback as well.

## 5. Recover

Before discarding work, save the patch and any untracked files you want to keep. For an unwanted uncommitted change, restore only the named file; never use a broad destructive reset or clean as routine recovery. For a committed change, use `git revert COMMIT` to preserve history. Never roll a live database back by reverting source code.

## Build

In a controlled environment, after the tests pass:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm CryptoRadarControlRoom.spec
```

The command replaces generated build/dist artifacts. Do not rebuild an executable that is currently running. Verify startup, the state root, interpreter selection, process start/stop and the mock-popup safeguard with disposable state. A unit test that monkeypatches `sys.frozen` does not replace that smoke test.

Architecture work never authorises private or live activation: see [RISK.md](RISK.md) and [docs/FUTURE_TRADING_ROADMAP.md](docs/FUTURE_TRADING_ROADMAP.md).
