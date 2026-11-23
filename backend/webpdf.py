"""Text extraction for search PDFs, run in a disposable Python subprocess."""

import io
import json
import sys
from itertools import islice

# --- TUNABLES ---------------------------------------------------------------
MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 3
MAX_CHARS = 16_000

SIGNATURE = b"%PDF-"


def extract(data):
    if len(data) > MAX_BYTES:
        return "pdf_oversize", ""
    if not data.startswith(SIGNATURE):
        return "pdf_corrupt", ""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "pdf_unavailable", ""
    parts = []
    remaining = MAX_CHARS
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            return "pdf_encrypted", ""
        for page in islice(reader.pages, MAX_PAGES):
            text = (page.extract_text() or "").strip()
            if text:
                part = ("\n\n" + text if parts else text)[:remaining]
                parts.append(part)
                remaining -= len(part)
                if not remaining:
                    break
    except Exception:
        return "pdf_corrupt", ""
    return ("ok", "".join(parts).rstrip()) if parts else ("pdf_no_text", "")


if __name__ == "__main__":
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    print(json.dumps(extract(data)))
