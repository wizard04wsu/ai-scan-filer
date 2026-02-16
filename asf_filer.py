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


DOC_CATEGORIES = {
    "Medical":
        "Any document regarding healthcare (labs, scans, insurance EOBs). This does not include documents about healthcare insurance policies themselves.",
    "Financial":
        "Bank statements, investment reports, or credit card statements.",
    "Property":
        "Documents tied to the ownership of a physical address or vehicle (mortgage refinancing, property taxes, lease agreements).",
    "Maintenance":
        "Invoices for labor or parts involving maintenance, repair, or annual inspection of a home, vehicle, or appliance.",
    "Insurance":
        "Policy renewals, coverage summaries, or declarations pages (health, auto, home, life).",
    "Administrative":
        "Warranties, birth certificates, passports, or legal contracts.",
    "Utility":
        "Recurring household bills (electricity, water, internet, trash).",
    "Unknown":
        "Use this category if you are unsure or if the document does not match any of the above.",
}
DocCategory = Enum("DocCategory", {k: k for k in DOC_CATEGORIES.keys()})

# Globals
#logger
#config
get_ai_prompt = get_text_prompt


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

class ClassificationError(Exception):
    """Raised when the document classification step fails."""
    pass

# Use this to enforce strict JSON responses
class DocumentClassification(BaseModel):
    # Use Literal to restrict doc_category to your specific categories
    doc_category: DocCategory = Field(description="Document category")
    confidence: float = Field(ge=0, le=1.0)
    reasoning: str

def classify_document(content):
    schema = DocumentClassification.model_json_schema()
    prompt = get_ai_prompt(DOC_CATEGORIES)
    
    try:
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
        
        # ---- Network / timeout / connection issues ----
        except httpx.ResponseError as e:
            raise ClassificationError(f"Ollama connection error: {e}") from e
        
        # ---- 4xx / 5xx HTTP errors ----
        except httpx.HTTPStatusError as e:
            raise ClassificationError(f"Ollama HTTP error: {e}") from e
        
        # ---- Anything else from Ollama ----
        except Exception as e:
            raise ClassificationError(f"Ollama call failed: {e}") from e
        
        
        # ---- Validate response structure ----
        try:
            content = response["message"]["content"]
        except (KeyError, TypeError) as e:
            raise ClassificationError(
                f"Unexpected Ollama response structure:\n{response!r}"
            ) from e
        
        # ---- Parse JSON ----
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ClassificationError(
                f"Model returned invalid JSON:\n{content}"
            ) from e
        
        # ---- Validate schema ----
        try:
            obj = DocumentClassification.model_validate(data)
            return obj.model_dump(mode="json")
        except ValidationError as e:
            raise ClassificationError(
                f"Schema validation failed:\n{e}"
            ) from e
    
    except Exception as e:
        logger.log(f"Classification failed:\n{e}")
        return {
            "doc_category": "Unknown",
            "confidence": 0.0,
            "error": str(e)
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
        meta = classify_document(text)
        logger.log(f"Category: {meta["doc_category"]} ({meta["confidence"]*100}% confidence)")
        logger.log(f"Reasoning: {meta["reasoning"]}")
        
        if meta["confidence"] < 0.8 and layout_path.exists():
            logger.log("Low confidence; retrying using layout information.")
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            meta = classify_document(layout)
            logger.log(f"Category: {meta["doc_category"]} ({meta["confidence"]*100}% confidence)")
            logger.log(f"Reasoning: {meta["reasoning"]}")
        
        #TODO: if confidence is still < 0.8, make sure it is not sorted
        
        doc_category = meta.get("doc_category", "Unknown")

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
    global logger, config
    
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
