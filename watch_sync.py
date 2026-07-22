"""
watch_sync.py
--------------
Watches this folder for file changes and automatically:
    add -> commit -> pull --rebase -> push
It also pulls from GitHub every 5 minutes in the background so that
edits made directly on github.com come down into this folder too.

Run it from inside your repo folder:
    python watch_sync.py

Stop it any time with Ctrl+C.
"""

import subprocess
import threading
import time
import logging
import os
import sys
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ---------------------------------------------------------------------------
# Settings you can tweak
# ---------------------------------------------------------------------------
DEBOUNCE_SECONDS = 5          # wait this long after the last save before syncing
BACKGROUND_PULL_SECONDS = 300  # 5 minutes - how often to check GitHub for remote edits
LARGE_FILE_MB = 5             # files bigger than this trigger a confirmation prompt
BRANCH = "main"

# Extensions that are treated as "binary/large" and will prompt before syncing,
# regardless of size.
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico",
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".mp4", ".mov", ".avi", ".mkv", ".mp3", ".wav",
    ".exe", ".msi", ".dll", ".so", ".a", ".lib", ".bin",
}

# Folders/files we never want to trigger a sync (git internals + our own log)
IGNORED_DIR_NAMES = {".git", "__pycache__", "node_modules", ".venv", "venv"}
IGNORED_FILE_NAMES = {"sync.log"}

REPO_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Logging: prints to terminal AND writes to sync.log in the repo folder
# ---------------------------------------------------------------------------
logger = logging.getLogger("watch_sync")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler(sys.stdout)
file_handler = logging.FileHandler(REPO_DIR / "sync.log", encoding="utf-8")

formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Lock so the debounce-triggered sync and the 5-minute background pull
# never run git commands at the same time.
git_lock = threading.Lock()


def run_git(args, check=False):
    """Run a git command in the repo folder and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git"] + args,
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        logger.error(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_changed_files():
    """Return a list of (status, filepath) for changed/untracked files."""
    code, out, err = run_git(["status", "--porcelain"])
    if code != 0:
        logger.error(f"Could not get git status: {err}")
        return []
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status = line[:2].strip()
        filepath = line[3:].strip().strip('"')
        files.append((status, filepath))
    return files


def is_flagged(filepath: str) -> bool:
    """Return True if this file is binary-type or larger than the size limit."""
    full_path = REPO_DIR / filepath
    ext = Path(filepath).suffix.lower()
    if ext in BINARY_EXTENSIONS:
        return True
    if full_path.exists() and full_path.is_file():
        size_mb = full_path.stat().st_size / (1024 * 1024)
        if size_mb > LARGE_FILE_MB:
            return True
    return False


def sync_and_push():
    """The core add -> commit -> pull --rebase -> push flow."""
    with git_lock:
        changed = get_changed_files()
        if not changed:
            return  # nothing to do

        flagged = [f for _, f in changed if is_flagged(f)]
        normal = [f for _, f in changed if f not in flagged]

        # Stage the normal (non-flagged) files first
        if normal:
            run_git(["add"] + normal)

        # Ask before staging large/binary files
        if flagged:
            logger.info("The following large/binary files changed:")
            for f in flagged:
                print(f"   - {f}")
            answer = input("Include these in the sync? [y/N]: ").strip().lower()
            if answer == "y":
                run_git(["add"] + flagged)
            else:
                logger.info("Skipping large/binary files for now. They stay unstaged.")

        # Check if anything is actually staged before committing
        code, out, _ = run_git(["diff", "--cached", "--quiet"])
        if code == 0:
            logger.info("No staged changes to commit.")
            return

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        commit_msg = f"Auto-sync: {timestamp}"
        code, out, err = run_git(["commit", "-m", commit_msg])
        if code != 0:
            logger.error(f"Commit failed: {err}")
            return
        logger.info(f"Committed: {commit_msg}")

        # Pull with rebase before pushing, so remote edits aren't clobbered
        code, out, err = run_git(["pull", "--rebase", "origin", BRANCH])
        if code != 0:
            logger.error("Pull --rebase failed (likely a conflict). Aborting rebase.")
            logger.error(err)
            run_git(["rebase", "--abort"])
            logger.error(
                "Sync paused for this change. Open the folder in VS Code, "
                "resolve the conflict manually, then re-save the file to resume."
            )
            return

        # Push
        code, out, err = run_git(["push", "origin", BRANCH])
        if code != 0:
            logger.error(f"Push failed: {err}")
            return
        logger.info("Pushed to GitHub successfully.")


# ---------------------------------------------------------------------------
# File watcher with debounce
# ---------------------------------------------------------------------------
class DebouncedHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_change_time = None
        self.timer_lock = threading.Lock()

    def _should_ignore(self, path: str) -> bool:
        parts = Path(path).parts
        if any(p in IGNORED_DIR_NAMES for p in parts):
            return True
        if Path(path).name in IGNORED_FILE_NAMES:
            return True
        return False

    def on_any_event(self, event):
        if event.is_directory:
            return
        if self._should_ignore(event.src_path):
            return
        with self.timer_lock:
            self.last_change_time = time.time()


def debounce_loop(handler: DebouncedHandler):
    """Continuously checks whether enough quiet time has passed to trigger a sync."""
    while True:
        time.sleep(1)
        with handler.timer_lock:
            last_change = handler.last_change_time
        if last_change is not None and (time.time() - last_change) >= DEBOUNCE_SECONDS:
            with handler.timer_lock:
                handler.last_change_time = None  # reset so we don't re-trigger
            sync_and_push()


def background_pull_loop():
    """Every N seconds, pull any changes made directly on GitHub."""
    while True:
        time.sleep(BACKGROUND_PULL_SECONDS)
        with git_lock:
            code, out, err = run_git(["pull", "--rebase", "origin", BRANCH])
            if code != 0:
                logger.error("Background pull --rebase failed (likely a conflict). Aborting rebase.")
                logger.error(err)
                run_git(["rebase", "--abort"])
                logger.error(
                    "Background pull paused due to a conflict. Resolve it manually in VS Code."
                )
            elif "up to date" not in out.lower() and out:
                logger.info("Pulled new changes from GitHub.")


def main():
    if not (REPO_DIR / ".git").exists():
        logger.error(f"{REPO_DIR} is not a git repository. Run this script from inside your repo folder.")
        sys.exit(1)

    logger.info(f"Watching {REPO_DIR} for changes...")
    logger.info(f"Debounce: {DEBOUNCE_SECONDS}s | Background pull every {BACKGROUND_PULL_SECONDS}s")

    handler = DebouncedHandler()
    observer = Observer()
    observer.schedule(handler, str(REPO_DIR), recursive=True)
    observer.start()

    threading.Thread(target=debounce_loop, args=(handler,), daemon=True).start()
    threading.Thread(target=background_pull_loop, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping watcher...")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
