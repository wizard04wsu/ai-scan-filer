import argparse
from enum import Enum
import json
import queue
from pathlib import Path
import threading
import time

# Third-party packages
import httpx
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import ollama
from prompt_classifier_text import get_ai_prompt as get_text_prompt
from prompt_classifier_layout import get_ai_prompt as get_layout_prompt
from pydantic import BaseModel, Field, ValidationError

# Project packages
from asf_logger import Logger
from asf_kill import kill_previous
from asf_ocr_data import get_ocr_content


SCRIPT_ID = "ai-scan-filer"


# Globals
#logger
#config
#categories


# =========================
# UTILITIES
# =========================

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
# OLLAMA AI CALL
# =========================

MODEL = "llama3.1:8b"

def build_schema_from_categories(categories: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "doc_category": {
                "type": "string",
                "enum": sorted(categories.keys()),   # restrict values to config keys
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "reasoning": {
                "type": "string",
            },
            "error": {
                "type": "string",
            },
        },
        "required": ["doc_category", "confidence", "reasoning"],
        "additionalProperties": False,
    }

class ClassificationError(Exception):
    """Raised when the document classification step fails."""
    pass

def classify_document(content, prompt):
    
    schema = build_schema_from_categories(categories)
    
    try:
        response = ollama.chat(
            model=MODEL,
            messages=[
                {
                    'role': 'system',
                    'content': prompt
                },
                {
                    'role': 'user',
                    'content': f"Classify this document content:\n\n{content}"
                }
            ],
            format=schema,
            options={'temperature': 0} # Set to 0 for maximum consistency
        )
        
        content = response["message"]["content"]
        data = json.loads(content)
        
        # schema should already enforce this, but keeping a guard is nice:
        if data.get("doc_category") not in categories:
            raise ClassificationError(f"Unknown doc_category: {data.get('doc_category')!r}")
        
        return data
        
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        return {
            "doc_category": "Unknown",
            "confidence": 0.0,
            "reasoning": "Error",
            "error": f"Ollama/network error: {e}",
        }
    except (KeyError, TypeError, json.JSONDecodeError, ClassificationError) as e:
        return {
            "doc_category": "Unknown",
            "confidence": 0.0,
            "reasoning": "Error",
            "error": str(e),
        }


# =========================
# Worker Queue
# =========================

class WorkQueue:
    def __init__(self, processor):
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
    def __init__(self, processed):
        self.processed = processed
        self.logger = logger

    def process(self, pdf_path: Path):
        if not wait_for_stable_file(pdf_path):
            return

        layout_path = pdf_path.with_suffix(".pdf.layout.json")

        logger.log("==============================")
        logger.log(f"Analyzing {pdf_path.name}")
        
        text = get_ocr_content(pdf_path, 0, logger)
        meta = classify_document(text, get_text_prompt(categories))
        logger.log(f"\033[92mCategory: {meta["doc_category"]} ({meta["confidence"]*100}% confidence)\033[0m")
        logger.log(f"Reasoning: {meta["reasoning"]}")
        
        #if meta["confidence"] <= 0.8:
        #    text = get_ocr_content(pdf_path, 1, logger)
        #    if text:
        #        logger.log("Low confidence; retrying using 2nd page.")
        #        meta = classify_document(text, get_text_prompt(categories))
        #        logger.log(f"Category: {meta["doc_category"]} ({meta["confidence"]*100}% confidence)")
        #        logger.log(f"Reasoning: {meta["reasoning"]}")
        
        #TODO: choose the highest confidence of above and below, and go with that; ties first go to not-Unknown, then to upper
        if meta["confidence"] <= 0.8 and layout_path.exists():
            logger.log("\033[31mLow confidence\033[0m; retrying using layout information.")
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            meta = classify_document(layout, get_layout_prompt(categories))
            logger.log(f"\033[92mCategory: {meta["doc_category"]} ({meta["confidence"]*100}% confidence)\033[0m")
            logger.log(f"Reasoning: {meta["reasoning"]}")
        
        #TODO: if confidence is still <= 0.8, make sure it is not sorted
        
        doc_category = meta.get("doc_category", "Unknown")
        
        logger.log("Watching for PDFs. Ctrl+C to stop.")

        if False:
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


def main():
    global logger, config, categories
    
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="", help="Path to config.json")
    args = ap.parse_args()
    
    config_path = (
        Path(args.config)
        if args.config
        else Path(__file__).with_name("config.json")
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    filer_cfg = config.get("ocr", {})
    
    categories_path = Path(".\\categories.json")
    categories = json.loads(categories_path.read_text(encoding="utf-8"))
    

    root = Path(config["root"]).expanduser()
    paths = config["paths"]
    
    runtime = root / paths.get("runtime", ".asf")
    runtime.mkdir(parents=True, exist_ok=True)
    
    log_file = runtime / filer_cfg.get("log_file", "asf-filer.log")
    log_max_kb = filer_cfg.get("log_max_kb", 512)
    logger = Logger(log_file, log_max_kb, enabled=True)
    
    
    pid_path = runtime / f"{SCRIPT_ID}.pid"
    kill_previous(pid_path)
    

    processed = root / paths.get("processed", "Processed")

    processed.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    
    process_existing = filer_cfg.get("process_existing", False)

    logger.log(f"Started AI Filer.")
    logger.log(f"Locations:")
    logger.log(f"  Config: {config_path}")
    logger.log(f"  Runtime: {runtime}")
    logger.log(f"  Watching: {processed}")

    processor = Processor(
        processed=processed,
    )
    work = WorkQueue(processor)
    work.start()

    handler = WatchHandler(work)
    observer = Observer()
    observer.schedule(handler, str(processed), recursive=False)
    observer.start()

    if process_existing:
        pdfs = sorted(processed.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
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
