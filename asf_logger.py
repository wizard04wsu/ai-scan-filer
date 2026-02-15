import threading
import time
from pathlib import Path


class Logger:
    def __init__(self, log_path: Path | None, max_kb=512, enabled=True):
        self.log_path = log_path
        self.max_bytes = max_kb * 1024
        self.enabled = enabled and log_path is not None
        self._lock = threading.Lock()

    def log(self, msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"

        print(line)

        if not self.enabled:
            return
        with self._lock:
            try:

                if self.log_path.exists() and self.log_path.stat().st_size > self.max_bytes:
                    self.log_path.unlink()
            except Exception:
                pass
            try:
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                # Never let logging crash the script
                pass
