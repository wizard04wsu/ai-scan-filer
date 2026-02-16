import json
from pathlib import Path
import re

# Third-party packages
import pymupdf  # PyMuPDF

from asf_logger import Logger


# =========================
# Layout JSON export
# =========================

def export_layout_json(pdf_path: Path, json_path: Path, page: int=0, logger: Logger=None) -> None:
    """
    Export word-level coordinates from the PDF text layer.
    Output schema is stable and easy for later AI grouping.
    """
    #try:
    doc = pymupdf.open(str(pdf_path))
    
    
    
    page = doc.load_page(page)
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

    doc.close()
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if logger:
        logger.log(f"Layout JSON written: {json_path.name}")

def get_ocr_content(pdf_path: Path, page: int=0, logger: Logger=None, filter: str=r"(?i)^[a-z\d$,.-]+$") -> str:
    doc = pymupdf.open(str(pdf_path))
    page = doc.load_page(page)
    page_text = page.get_text("words") or []
    words = []
    for word in page_text:
        words.append(word[4])
    #if logger: logger.log(words)
    if filter:
        # Apply the filter to remove words that don't match
        filtered_words = []
        for word in words:
            word = str(word)
            if re.search(filter, word):
                filtered_words.append(word)
        #if logger: logger.log(filtered_words)
        return " ".join(filtered_words)
    return " ".join(words)
