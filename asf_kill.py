import os
import sys
from pathlib import Path

# Third-party packages
import psutil


# =========================
# Ensure Single Instance
# =========================

# Kill the process that matches the stored process ID for the instance of this script running with the same configuration.

def kill_previous(pid_path):
    """
    If a previous instance is running, terminate it.
    Guarded by checking the process cmdline contains SCRIPT_ID or script filename.
    """
    this_pid = os.getpid()

    # Attempt to stop previous
    pid_file = Path(pid_path)
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            old_pid = None

        if old_pid and old_pid != this_pid:
            try:
                p = psutil.Process(old_pid)

                # Safety: ensure we're killing the right thing
                cmd = " ".join(p.cmdline()).lower()
                me = Path(sys.argv[0]).name.lower()

                if me in cmd or SCRIPT_ID.lower() in cmd:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        p.kill()
                # else: don't kill if it doesn't look like our script
            except psutil.NoSuchProcess:
                pass
            except Exception:
                pass

    # Write our PID
    pid_file.write_text(str(this_pid), encoding="utf-8")

    # Optional: cleanup PID file on exit
    import atexit
    def _cleanup():
        try:
            if pid_file.exists() and pid_file.read_text().strip() == str(this_pid):
                pid_file.unlink()
        except Exception:
            pass
    atexit.register(_cleanup)
