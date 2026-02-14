#!/usr/bin/env python3
# Watches a folder for PDFs and makes them searchable via Tesseract.

import argparse
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# Third-party packages
import pymupdf  # PyMuPDF
import psutil
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


SCRIPT_ID = "ai-scan-filer-ocr"


# =========================
# Ensure Single Instance
# =========================

def get_pid_file(config):
    root = Path(config["root"])
    runtime = config["paths"].get("runtime", ".runtime")

    pid_dir = root / runtime
    pid_dir.mkdir(parents=True, exist_ok=True)
    return pid_dir / f"{SCRIPT_ID}.pid"

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
# Utilities
# =========================

def sha256_file(path: Path, chunk_size=65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def wait_for_stable_file(path: Path, timeout_s=10):
    last_size = -1
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last_size and size > 0:
            return True
        last_size = size
        time.sleep(0.25)
    return True


# =========================
# Layout JSON export
# =========================

def export_layout_json(pdf_path: Path, json_path: Path, logger: Logger) -> None:
    """
    Export word-level coordinates from the PDF text layer.
    Output schema is stable and easy for later AI grouping.
    """
    #try:
    doc = pymupdf.open(str(pdf_path))
    
    
    
    page = doc.load_page(0)
    words = page.get_text("words") or []
    mx = 1000/float(page.rect.width)
    payload = [
        # words: [x0, y0, x1, y1, "word", block_no, line_no, word_no]
        {
            "text": w[4],
            "x": int(mx * (w[2] - w[0]) / 2),
            "y": int(mx * (w[3] - w[1]) / 2),
        }
        for w in words[:200] if str(w[4]).strip()
    ]
    
    
    
    if False:
        payload = {
            "source_pdf": pdf_path.name,
            "page_count": doc.page_count,
            "pages": []
        }

        for pno in range(doc.page_count):
            page = doc.load_page(pno)
            # words: [x0, y0, x1, y1, "word", block_no, line_no, word_no]
            words = page.get_text("words") or []
            page_w = float(page.rect.width)
            page_h = float(page.rect.height)

            payload["pages"].append({
                "page_index": pno,
                "width": page_w,
                "height": page_h,
                "words": [
                    {
                        "text": w[4],
                        "x0": float(w[0]), "y0": float(w[1]),
                        "x1": float(w[2]), "y1": float(w[3]),
                        "block": int(w[5]),
                        "line": int(w[6]),
                        "word": int(w[7]),
                    }
                    for w in words if str(w[4]).strip()
                ]
            })

    doc.close()
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.log(f"Layout JSON written: {json_path.name}")
    #except Exception as e:
    #    logger.log(f"Layout export failed for {pdf_path.name}: {e}")


class Processor:
    def __init__(self, inbox, processed, archive, db_path,
                 actionOnProcessed, lang, psm, jobs, capture_errors, logger, skip_duplicates):
        self.inbox = inbox
        self.processed = processed
        self.archive = archive
        self.db_path = db_path
        self.actionOnProcessed = actionOnProcessed
        self.lang = lang
        self.psm = psm
        self.jobs = jobs
        self.capture_errors = capture_errors
        self.logger = logger
        self.skip_duplicates = skip_duplicates
        self.hash_db = self._load_db()

    def _load_db(self):
        try:
            return json.loads(self.db_path.read_text())
        except Exception:
            return {}

    def _save_db(self):
        try:
            self.db_path.write_text(json.dumps(self.hash_db, indent=2))
        except Exception:
            pass

    def process(self, pdf_path: Path):
        if not wait_for_stable_file(pdf_path):
            return
        
        h = sha256_file(pdf_path)
        if self.skip_duplicates:
            if h in self.hash_db:
                self.logger.log(f"Skipping duplicate: {pdf_path.name}")
                return

        out_path = self.processed / pdf_path.name

        cmd = [
            sys.executable, "-m", "ocrmypdf",
            "--force-ocr",
            "--language", self.lang,
            "--tesseract-pagesegmode", str(self.psm),
            "--jobs", self.jobs,
            str(pdf_path),
            str(out_path),
        ]

        try:
            subprocess.run(cmd, check=True,
                           capture_output=self.capture_errors)
            self.logger.log(f"OCR done: {out_path.name}")
        except subprocess.CalledProcessError as e:
            self.logger.log(f"OCR failed: {e}")
            return

        self.hash_db[h] = pdf_path.name
        self._save_db()

        # Layout export
        layout_path = out_path.with_suffix(".pdf.layout.json")
        export_layout_json(out_path, layout_path, self.logger)

        # Handle original
        if self.actionOnProcessed == "delete":
            pdf_path.unlink(missing_ok=True)
        elif self.actionOnProcessed == "archive":
            dest = self.archive / pdf_path.name
            shutil.move(str(pdf_path), str(dest))
        

# =========================
# Worker Queue
# =========================

class WorkQueue:
    def __init__(self, processor, logger):
        self.q = queue.Queue()
        self.processor = processor
        self.logger = logger
        self.running = True
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.running = False
        self.thread.join()

    def enqueue(self, path):
        self.q.put(path)

    def run(self):
        while self.running:
            try:
                path = self.q.get(timeout=1)
            except queue.Empty:
                continue
            self.processor.process(path)


# =========================
# Watch handler
# =========================

class WatchHandler(FileSystemEventHandler):
    def __init__(self, work):
        self.work = work

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() == ".pdf":
            self.work.enqueue(path)

    def on_moved(self, event):
        if event.is_directory:
            return
        path = Path(event.dest_path)
        if path.suffix.lower() == ".pdf":
            self.work.enqueue(path)


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
    
    inbox = root / paths.get("inbox", "Inbox")
    processed = root / paths.get("processed", "Processed")
    runtime = root / paths.get("runtime", ".runtime")
    archive = root / paths.get("archive", "Originals")
    
    inbox.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    
    ocr_cfg = config.get("ocr", {})
    lang = ocr_cfg.get("lang", "eng")
    psm = ocr_cfg.get("psm", "11")
    jobs = ocr_cfg.get("jobs", "4")

    actionOnProcessed = ocr_cfg.get("actionOnProcessed", "none")
    process_existing = ocr_cfg.get("process_existing", False)
    skip_duplicates = ocr_cfg.get("skip_duplicates", True)
    capture_errors = ocr_cfg.get("capture_errors", False)
    
    db_path = runtime / ocr_cfg.get("hash_db", "processed_hashes.json")
    
    log_file = runtime / ocr_cfg.get("log_file", "ai-scan-filer-ocr.log")
    log_max_kb = ocr_cfg.get("log_max_kb", 512)
    logger = Logger(log_file, log_max_kb, enabled=True)

    logger.log(f"Started OCR Processor.")
    logger.log(f"Locations:")
    logger.log(f"  Config: {config_path}")
    logger.log(f"  Runtime: {runtime}")
    logger.log(f"  Watching: {inbox}")
    logger.log(f"  Processed: {processed}")
    logger.log(f"OCR:")
    logger.log(f"  Lang: {lang}")
    logger.log(f"  PSM: {psm}")
    logger.log(f"  Jobs: {jobs}")
    logger.log(f"Post-processing: {actionOnProcessed}")

    processor = Processor(
        inbox=inbox,
        processed=processed,
        archive=archive,
        db_path=db_path,
        actionOnProcessed=actionOnProcessed,
        lang=lang,
        psm=psm,
        jobs=jobs,
        capture_errors=capture_errors,
        logger=logger,
        skip_duplicates=skip_duplicates,
    )

    work = WorkQueue(processor, logger)
    work.start()

    handler = WatchHandler(work)
    observer = Observer()
    observer.schedule(handler, str(inbox), recursive=False)
    observer.start()

    if process_existing:
        pdfs = sorted(inbox.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
        logger.log(f"Startup enqueue: {len(pdfs)} existing PDF(s)")
        for p in pdfs:
            work.enqueue(p)

    logger.log("Watching for PDFs. Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()
    work.stop()
    logger.log("Stopped.")


if __name__ == "__main__":
    main()
