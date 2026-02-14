#!/usr/bin/env python3
# Watches a folder for PDFs, renaming and moving them using AI.

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time

# Third-party packages
import psutil
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from prompt_classifier import get_ai_prompt
from pprint import pprint


SCRIPT_ID = "ai-scan-filer"

# =========================
# Ensure Single Instance
# =========================

def get_pid_file():
    root = Path(config["root"])
    runtime = config["paths"].get("runtime", ".runtime")

    pid_dir = root / runtime
    pid_dir.mkdir(parents=True, exist_ok=True)
    return pid_dir / f"{SCRIPT_ID}.pid"

def ensure_single_instance_kill_previous():
    """
    If a previous instance is running, terminate it.
    Guarded by checking the process cmdline contains SCRIPT_ID or script filename.
    """
    this_pid = os.getpid()

    # Attempt to stop previous
    pid_file = get_pid_file()
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

DATE_MONTH_RE = r"(Jan(?:uary)?)|(Feb(?:ruary)?)|(Mar(?:ch)?)|(Apr(?:il)?)|(May)|(Jun(?:e)?)|(Jul(?:y)?)|(Aug(?:ust)?)|(Sep(?:tember)?)|(Oct(?:ober)?)|(Nov(?:ember)?)|(Dec(?:ember)?)"
DATE_MMMDY_RE = r"(" + DATE_MONTH_RE + r")\s+(\d{1,2}),?\s+((?:19|20)\d{2})"
DATE_MDY_RE = r"(\d{1,2})\s?[-/.]\s?(\d{1,2})\s?[-/.]\s?((?:19|20)\d{2})"
DATE_YMD_RE = r"((?:19|20)\d{2})\s?[-/.]\s?(\d{1,2})\s?[-/.]\s?(\d{1,2})"
DATE_RE = re.compile(r"(\b(?:" + DATE_MDY_RE + r"|" + DATE_YMD_RE + r"|" + DATE_MMMDY_RE + r")\b)")


def select_date(meta, file_path):
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


from datetime import datetime, date

def python_strftime_format(cfg_fmt: str) -> str:
    """
    Convert config format like 'yyyy-mm-dd' into Python strftime like '%Y-%m-%d'.
    Supports: yyyy, yy, mm, m, dd, d (keeps separators as-is).
    """
    fmt = (cfg_fmt or "yyyy-mm-dd").lower()
    # Replace longer tokens first
    fmt = fmt.replace("yyyy", "%Y").replace("yy", "%y")
    fmt = fmt.replace("mm", "%m").replace("dd", "%d")
    # If user typed single-letter tokens, normalize them too
    fmt = fmt.replace("%m", "%m").replace("%d", "%d")
    return fmt

def format_date(dt_value) -> str:
    if not dt_value:
        return "undated"

    # Accept datetime/date objects OR ISO strings
    try:
        if isinstance(dt_value, str):
            # handle ISO 'YYYY-MM-DD' (and tolerate a trailing time)
            dt_value = datetime.fromisoformat(dt_value[:10])
        elif isinstance(dt_value, date) and not isinstance(dt_value, datetime):
            dt_value = datetime(dt_value.year, dt_value.month, dt_value.day)

        fmt_cfg = config.get("date", {}).get("format", "yyyy-mm-dd")
        fmt = python_strftime_format(fmt_cfg)
        return dt_value.strftime(fmt)
    except Exception:
        return "date_error"

def month_name_to_number(month_name):
    """
    Converts a month name (e.g., 'January' or 'Jan') to its number (1-12).
    Case-insensitive.
    """
    # Determine format based on name length or simply try with '%b' after slicing
    try:
        # Try parsing as full month name
        date_object = datetime.datetime.strptime(month_name, '%B')
    except ValueError:
        # If that fails, try parsing as abbreviated month name
        date_object = datetime.datetime.strptime(month_name, '%b')
    
    return date_object.month

def date_to_iso(dt):
    matches = re.findall(DATE_MONTH_RE, dt, re.I)
    if len(matches):
        for index, match in enumerate(list(matches[0])):
            if len(match):
                matches = list(re.findall(DATE_MMMDY_RE, dt, re.I)[0])
                iso_date = matches[14] + "-" + str(index + 1) + "-" + matches[13]
                return iso_date
    matches = list(re.findall(DATE_MDY_RE + r"|" + DATE_YMD_RE, dt, re.I)[0])
    iso_date = matches[2]+matches[3] + "-" + matches[0]+matches[4] + "-" + matches[1]+matches[5]
    return iso_date


# =========================
# FEATURE EXTRACTION
# =========================

MONEY_RE = re.compile(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})")
#ACCOUNT_NUMBER_RE = re.compile(r"(?i)\b(\d[\d-]{9,}\d|(?=[a-z-]*\d)[a-z\d][a-z\d-]{4,}[a-z\d])\b")
ACCOUNT_NUMBER_RE = re.compile(r"(?i)(?:^|\s)\b([a-z]{,3}\d{6,})\b(?=\s|$)")


def extract_features(layout):
    return layout
    page = layout["pages"][0]
    words = page["words"]

    texts = [w["text"] for w in words]
    all_text = " ".join(texts).lower()
    
    dates = [date_to_iso(match[0]) for match in DATE_RE.findall(all_text)]
    # Remove duplicates.
    dates = list(set(dates))
    
    amounts = []
    for amt in MONEY_RE.findall(all_text):
        amounts.append(amt.replace(",", ""))
    # Remove duplicates.
    amounts = list(set(amounts))
    
#    account_numbers = ACCOUNT_NUMBER_RE.findall(all_text)
    account_numbers = [match[0] for match in ACCOUNT_NUMBER_RE.findall(all_text)]
    
    # Remove duplicates.
    account_numbers = list(set(account_numbers))

    candidates = {
        "amounts": amounts,
        "dates": dates,
        "account_numbers": account_numbers,
    }

    keywords = []
    for kw in config["keywords"]:
        if kw in all_text.lower():
            keywords.append(kw)

    features = {
        "candidates": candidates,
        "keywords": keywords,
        #"sample_text": " ".join(texts[:200])
        "sample_text": " ".join(texts)
    }
    pprint(features)
    return features


# =========================
# OLLAMA AI CALL
# =========================

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.1:8b"


def call_ollama(features: dict):
    prompt = (
        get_ai_prompt(config, features)
        + "\n\nFEATURE_PACK_JSON:\n"
        + json.dumps(features, ensure_ascii=False)
        + "\n\nReturn STRICT JSON ONLY."
    )
    prompt = get_ai_prompt(config, features)

    r = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",   # key improvement if Ollama supports it
        },
        timeout=120,
    )
    if r.status_code >= 400:
        logger.log(f"Ollama HTTP {r.status_code}: {r.text[:800]}")
        r.raise_for_status()

    raw = r.json().get("response", "").strip()
    try:
        return json.loads(raw)
    except Exception:
        logger.log(f"Ollama non-JSON response (first 800 chars): {raw[:800]}")
        return {"doc_type": "unknown"}


# =========================
# PATH + NAMING
# =========================

def build_dest_path(doc_type):
    root = Path(config["root"])
    folders = config["folders"]
    mapping = config["mapping"]

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

def process_pdf(pdf_path: Path, logger):
    if not wait_until_stable(pdf_path):
        return

    layout_path = pdf_path.with_suffix(".pdf.layout.json")
    if not layout_path.exists():
        return

    layout = json.loads(layout_path.read_text(encoding="utf-8"))

    features = extract_features(layout)
    
    logger.log("==========")
    logger.log("Features:")
    pprint(features)
    logger.log("----------")
    
    logger.log(f"Analyzing {pdf_path.name}")
    
    #meta = call_ollama(features)
    meta = call_ollama(layout)
    
    logger.log("==========")
    logger.log("Meta:")
    pprint(meta)
    logger.log("----------")

    doc_type = meta.get("doc_type", "unknown")

    # Ensure title always exists
    title = meta.get("title", "").strip()
    if not title:
        title = pdf_path.stem or "Untitled"
    meta["title"] = title

    dt = select_date(meta, pdf_path)
    meta["date"] = format_date(dt)

    template = config["naming"].get(doc_type, config["naming"]["unknown"])
    filename = apply_template(template, meta)

    dest_dir = build_dest_path(doc_type)
    dest_path = dest_dir / filename

    moved = safe_move(pdf_path, dest_path)
    print(f"[✓] Filed: {moved}")

    # Remove layout JSON after successful filing
    layout_path = pdf_path.with_suffix(".pdf.layout.json")
    try:
        if layout_path.exists():
            layout_path.unlink()
            print(f"[✓] Removed layout file: {layout_path.name}")
    except Exception as e:
        print(f"[!] Failed to remove layout file: {layout_path.name} ({e})")

# =========================
# WATCHER
# =========================

class Handler(FileSystemEventHandler):
    def __init__(self, logger):
        self.config = config
        self.logger = logger

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix.lower() == ".pdf":
            #try:
                process_pdf(p, self.logger)
            #except Exception as e:
            #    self.logger.log(f"AI filer error processing {p.name}: {e}")

    def on_moved(self, event):
        if event.is_directory:
            return
        p = Path(event.dest_path)
        if p.suffix.lower() == ".pdf":
            #try:
                process_pdf(p, self.logger)
            #except Exception as e:
            #    self.logger.log(f"AI filer error processing {p.name}: {e}")

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
    global config
    config = load_config(config_path)
    
    ensure_single_instance_kill_previous()

    root = Path(config["root"]).expanduser()
    paths = config["paths"]
    processed = root / paths.get("processed", "Processed")
    runtime = root / paths.get("runtime", ".runtime")

    runtime.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    
    global logger
    log_file = runtime / paths.get("log_file", "ai-scan-filer.log")
    log_max_kb = paths.get("log_max_kb", 512)
    logger = Logger(log_file, log_max_kb, enabled=True)

    logger.log(f"Started AI Filer.")
    logger.log(f"Runtime: {runtime}")
    logger.log(f"Config: {config_path}")
    logger.log(f"Watching: {processed}")
    
    observer = Observer()
    observer.schedule(Handler(logger), str(processed), recursive=False)
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
