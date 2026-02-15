import argparse
import hashlib
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# Third-party packages
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from asf_logger import Logger
from asf_kill import kill_previous
from asf_layout import export_layout_json


SCRIPT_ID = "ai-scan-filer-ocr"

# Globals
#logger
#config
#full_paths


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
# Main
# =========================

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
        

def main():
    global logger, config
    
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="", help="Path to config.json")
    args = ap.parse_args()
    
    config_path = (
        Path(args.config)
        if args.config
        else Path(__file__).with_name("config.json")
    )
    print(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    ocr_cfg = config.get("ocr", {})
    
    root = Path(config["root"]).expanduser()
    paths = config["paths"]
    
    runtime = root / paths.get("runtime", ".asf")
    runtime.mkdir(parents=True, exist_ok=True)
    
    log_file = runtime / ocr_cfg.get("log_file", "ai-scan-filer-ocr.log")
    log_max_kb = ocr_cfg.get("log_max_kb", 512)
    logger = Logger(log_file, log_max_kb, enabled=True)
    
    
    pid_path = runtime / f"{SCRIPT_ID}.pid"
    kill_previous(pid_path)
    

    inbox = root / paths.get("inbox", "Inbox")
    processed = root / paths.get("processed", "Processed")
    archive = root / paths.get("archive", "Originals")
    
    inbox.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    
    lang = ocr_cfg.get("lang", "eng")
    psm = ocr_cfg.get("psm", "11")
    jobs = ocr_cfg.get("jobs", "4")

    actionOnProcessed = ocr_cfg.get("actionOnProcessed", "none")
    process_existing = ocr_cfg.get("process_existing", False)
    skip_duplicates = ocr_cfg.get("skip_duplicates", True)
    capture_errors = ocr_cfg.get("capture_errors", False)
    
    db_path = runtime / ocr_cfg.get("hash_db", "processed_hashes.json")
    
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
