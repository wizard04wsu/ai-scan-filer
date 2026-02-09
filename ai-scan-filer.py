#!/usr/bin/env python3
# Watches a folder for PDFs, renaming and moving them using AI.

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import threading
import time

# Third-party packages
import psutil
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler



SCRIPT_ID = "ai-scan-filer"


# =========================
# Ensure Single Instance
# =========================

def get_pid_file(config):
    root = Path(config["root"])
    runtime = config["paths"].get("runtime", ".runtime")

    pid_dir = root / runtime
    pid_dir.mkdir(parents=True, exist_ok=True)
    return pid_dir / f"{SCRIPT_ID}.pid"

def _proc_cmdline(pid: int):
    try:
        p = psutil.Process(pid)
        return " ".join(p.cmdline()).lower()
    except Exception:
        return ""

def ensure_single_instance_kill_previous(config):
    """
    If a previous instance is running, terminate it.
    Guarded by checking the process cmdline contains SCRIPT_ID or script filename.
    """
    this_pid = os.getpid()

    # Attempt to stop previous
    pid_file = get_pid_file(config)
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


# =========================
# Logging
# =========================

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
                # Never let logging crash the watcher
                pass


# =========================
# UTILITIES
# =========================

def sanitize_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", s)
    s = s.rstrip(". ")
    return s[:160]


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def safe_move(src: Path, dst: Path) -> Path:
    ensure_dir(dst.parent)
    candidate = dst
    i = 1
    while candidate.exists():
        candidate = dst.with_name(f"{dst.stem} ({i}){dst.suffix}")
        i += 1
    shutil.move(str(src), str(candidate))
    return candidate


def wait_until_stable(path: Path, timeout_s=10.0):
    last = -1
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last and size > 0:
            return True
        last = size
        time.sleep(0.25)
    return True


# =========================
# DATE HANDLING
# =========================

FORMAT_MAP = {
    "yyyy-mm-dd": "%Y-%m-%d",
    "mm-dd-yyyy": "%m-%d-%Y",
    "dd-mm-yyyy": "%d-%m-%Y",
    "yyyy.mm.dd": "%Y.%m.%d",
    "yyyyMMdd": "%Y%m%d"
}


def select_date(meta, config, file_path):
    priority = config["date"]["priority"]

    for key in priority:
        if key == "file_date":
            dt = datetime.fromtimestamp(file_path.stat().st_mtime)
            return dt
        val = meta.get(key)
        if val:
            try:
                return datetime.fromisoformat(val)
            except Exception:
                pass
    return None


def format_date(dt, config):
    if not dt:
        return "undated"
    fmt = config["date"].get("format", "yyyy-mm-dd")
    return dt.strftime(FORMAT_MAP.get(fmt, "%Y-%m-%d"))


# =========================
# FEATURE EXTRACTION
# =========================

MONEY_RE = re.compile(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})")
DATE_RE = re.compile(r"\b(20\d{2})[-/\.](\d{2})[-/\.](\d{2})\b")


def extract_features(layout):
    page = layout["pages"][0]
    words = page["words"]

    texts = [w["text"] for w in words]
    all_text = " ".join(texts).lower()

    candidates = {
        "amounts": MONEY_RE.findall(" ".join(texts)),
        "dates": DATE_RE.findall(" ".join(texts))
    }

    keywords = []
    for kw in ["statement", "receipt", "invoice", "eob", "payment", "bill"]:
        if kw in all_text:
            keywords.append(kw)

    return {
        "candidates": candidates,
        "keywords": keywords,
        "sample_text": " ".join(texts[:200])
    }


# =========================
# OLLAMA CALL
# =========================

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.1:8b"


def call_ollama(features, config):
    prompt = f"""
You are a document filing assistant.

Based on the document features below, determine:
- doc_type
- provider or merchant
- key fields (dates, amounts, account numbers)

Return STRICT JSON only.

Config naming templates:
{json.dumps(config["naming"], indent=2)}

Document features:
{json.dumps(features, indent=2)}

Return JSON:
{{
  "doc_type": "...",
  "provider": "...",
  "merchant": "...",
  "statement_date": "...",
  "payment_date": "...",
  "service_date": "...",
  "amount_due": "...",
  "amount_paid": "...",
  "account_number": "...",
  "title": "fallback short title"
}}
"""

    r = requests.post(
        OLLAMA_URL,
        json={"model": MODEL, "prompt": prompt, "stream": False},
        timeout=120
    )
    r.raise_for_status()
    raw = r.json().get("response", "").strip()

    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        return {"doc_type": "unknown"}

    try:
        return json.loads(m.group(0))
    except Exception:
        return {"doc_type": "unknown"}


# =========================
# PATH + NAMING
# =========================

def build_dest_path(config, doc_type):
    root = Path(config["root"])
    folders = config["folders"]

    # simple mapping for now
    mapping = {
        "eob": ["Medical", "EOBs"],
        "medical_bill": ["Medical", "Bills"],
        "receipt": ["Receipts"],
        "bill": ["Bills"],
        "tax": ["Taxes"]
    }

    parts = mapping.get(doc_type, [config["paths"]["unsorted"]])
    path = root
    for p in parts:
        path = path / p
    return path


def apply_template(template, fields):
    def repl(match):
        key = match.group(1)
        return sanitize_filename(str(fields.get(key, "")))

    return re.sub(r"\{(\w+)\}", repl, template)


# =========================
# MAIN PROCESS
# =========================

def process_pdf(pdf_path: Path, config):
    if not wait_until_stable(pdf_path):
        return

    layout_path = pdf_path.with_suffix(".pdf.layout.json")
    if not layout_path.exists():
        return

    layout = json.loads(layout_path.read_text(encoding="utf-8"))

    features = extract_features(layout)
    meta = call_ollama(features, config)

    doc_type = meta.get("doc_type", "unknown")

    dt = select_date(meta, config, pdf_path)
    meta["date"] = format_date(dt, config)

    template = config["naming"].get(doc_type, config["naming"]["unknown"])
    filename = apply_template(template, meta)

    dest_dir = build_dest_path(config, doc_type)
    dest_path = dest_dir / filename

    moved = safe_move(pdf_path, dest_path)
    print(f"[✓] Filed: {moved}")


# =========================
# WATCHER
# =========================

class Handler(FileSystemEventHandler):
    def __init__(self, config):
        self.config = config

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix.lower() == ".pdf":
            process_pdf(p, self.config)


# =========================
# Config helpers
# =========================

def load_config(config_path: Path) -> dict:
    return json.loads(config_path.read_text(encoding="utf-8"))


# =========================
# Main
# =========================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="", help="Path to config.json")
    args = ap.parse_args()
    
    config_path = (
        Path(args.config)
        if args.config
        else Path(__file__).with_name("config.json")
    )
    config = load_config(config_path)
    
    ensure_single_instance_kill_previous(config)

    root = Path(config["root"]).expanduser()
    paths = config["paths"]
    processed = root / paths.get("processed", "Processed")
    runtime = root / paths.get("runtime", ".runtime")

    runtime.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    
    log_file = runtime / paths.get("log_file", "ai-scan-filer.log")
    log_max_kb = paths.get("log_max_kb", 512)
    logger = Logger(log_file, log_max_kb, enabled=True)

    logger.log(f"Started AI Filer.")
    logger.log(f"Runtime: {runtime}")
    logger.log(f"Config: {config_path}")
    logger.log(f"Watching: {processed}")


    observer = Observer()
    observer.schedule(Handler(config), str(processed), recursive=False)
    observer.start()

    logger.log("Watching for PDFs. Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    logger.log("Stopped.")


if __name__ == "__main__":
    main()
