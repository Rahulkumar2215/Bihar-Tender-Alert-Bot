"""Keeps the Telegram bot running in the background with no window and no clicks.

* Only one bot may run (Telegram rejects two readers of the same bot): the running bot holds a
  local port as a lock.
* `ensure-bot` (run every 5 minutes by Task Scheduler) starts the bot hidden if it is not running.
* The bot exits by itself when its code files change, and `ensure-bot` starts the new version,
  so code updates never need a manual restart.
"""
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

LOCK_PORT = 47321
ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = Path(__file__).resolve().parent
log = logging.getLogger(__name__)


def acquire_lock():
    """Return a bound socket if no other bot holds the lock, else None. Keep it open while running."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: never share the port
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def bot_running():
    s = acquire_lock()
    if s is None:
        return True
    s.close()
    return False


def code_stamp():
    """Newest change to the code or to .env: either one restarts the bot with the new version."""
    files = list(CODE_DIR.rglob("*.py")) + [p for p in [ROOT / ".env"] if p.exists()]
    return max(p.stat().st_mtime for p in files)


def kill_old_bots():
    """Stop bots started the old way (start_bot.bat window). Windows only, best effort."""
    if os.name != "nt":
        return
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne %d -and "
          "($_.CommandLine -like '*start_bot.bat*' -or $_.CommandLine -like '*-m tenderbot bot*') } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }") % os.getpid()
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(3)


def _pythonw():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def start_bot_hidden():
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
    args = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True)
    cmd = [_pythonw(), "-m", "tenderbot", "bot"]
    try:  # leave Task Scheduler's job so the bot outlives the task that started it
        subprocess.Popen(cmd, creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0), **args)
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **args)


def ensure_bot(kill_old=False):
    if kill_old:
        kill_old_bots()
    if bot_running():
        return "already running"
    start_bot_hidden()
    for _ in range(20):
        time.sleep(1)
        if bot_running():
            return "started"
    return "start attempted (not confirmed yet)"


def code_changed_checker():
    start = code_stamp()

    def changed():
        try:
            return code_stamp() != start
        except OSError:
            return False
    return changed
