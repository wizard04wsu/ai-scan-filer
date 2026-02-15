#!/usr/bin/env python3
# Watches a folder for PDFs and makes them searchable via Tesseract.

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
import pymupdf  # PyMuPDF
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from asf_logger import Logger


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
