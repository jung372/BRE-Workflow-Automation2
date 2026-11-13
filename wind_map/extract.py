"""Conservative row extraction. Extraction never approves a legal fact."""
from __future__ import annotations
import hashlib
import io
import json
import re
import zipfile
from xml.etree import ElementTree
from .core import WindError, capacity_w, date_value

VERSION = "register-tables-1"
HEADER = {"NO", "N0", "상호(사업자)"}


def compact(value):
    return re.sub(r"\s+", "", value or "")


def register_date(value):
    text = compact(value).replace("‘", "").replace("'", "").replace(chr(96), "").rstrip(".")
    if not text:
        return date_value()
    match = re.fullmatch(r"(\d{2}|\d{4})[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?", text)
    if not match:
        raise WindError("Ambiguous register date")
    year = int(match[1])
    if year < 100:
        year += 2000 if year < 70 else 1900
    value = f"{year:04}-{int(match[2]):02}"
    if match[3]:
        value += f"-{int(match[3]):02}"
    return date_value(value)


def parse_tables(tables, text="", inherited_year=None):
    """Retain every table row, including detached continuations and non-wind rows."""
    heading = re.search(r"[ㅇ○]\s*(20\d{2}|19\d{2})\s*[-–]", text)
    year = int(heading[1]) if heading else inherited_year
    rows = []
    for ti, table in enumerate(tables):
        header = None
        for ri, cells in enumerate(table):
            raw = [c or "" for c in cells]
            flat = [compact(c) for c in raw]
            row = {"table": ti, "row": ri, "cells": raw, "extractor_version": VERSION}
            if not any(flat):
                row["classification"] = "blank"
            elif flat[0].upper() in HEADER and any("용량" in c for c in flat):
                header = flat
                row["classification"] = "header"
            elif header is None or len(flat) != len(header):
                row.update(classification="review", issue="unrecognized_table")
            elif not flat[0] or flat[0] in {"〃", "”", '"'}:
                row.update(classification="review", issue="detached_continuation_or_ditto")
            else:
                # Locate by labels, not fixed offsets: old tables omit year and plan period.
                def index(fragment):
                    return next((i for i, cell in enumerate(header) if fragment in cell), None)
                def value(fragment):
                    pos = index(fragment)
                    return raw[pos] if pos is not None else ""
                split_number = len(header) > 1 and not header[1]
                row_year = flat[0] if split_number else str(year or "")
                number = flat[1] if split_number else flat[0]
                fuel = compact(value("원동력"))
                candidate = {
                    "permit_year": row_year or None, "number": number,
                    "name": compact(value("상호")), "location": value("위치").replace("\n", ""),
                    "fuel": fuel, "capacity_raw": value("용량"),
                    "permit_or_change_raw": value("변경일") or value("허가일"),
                    "construction_plan_deadline_raw": value("인가기간"),
                    "preparation_deadline_raw": value("준비기간"),
                    "remarks": value("기타"),
                }
                row["candidate"] = candidate
                try:
                    quantity = compact(candidate["capacity_raw"])
                    candidate["capacity_w"] = capacity_w(quantity, None if re.search(r"[kKmM]?[wW]", quantity) else "MW")
                    candidate["permit_or_change_date"] = register_date(candidate["permit_or_change_raw"])
                    for name in ("construction_plan_deadline", "preparation_deadline"):
                        candidate[name] = register_date(candidate[name + "_raw"])
                except WindError as exc:
                    row["issue"] = str(exc)
                # Unknown/ambiguous wind subtype must be reviewed, never silently excluded.
                if fuel == "육상풍력":
                    row["classification"] = "review" if row.get("issue") or not row_year else "candidate"
                elif "풍력" in fuel and "해상" not in fuel:
                    row.update(classification="review", issue="wind_subtype_unconfirmed")
                elif fuel in {"〃", "“", "”", chr(34)}:
                    row.update(classification="review", issue="fuel_ditto_requires_context")
                elif fuel:
                    row["classification"] = "out_of_scope"
                else:
                    row.update(classification="review", issue="fuel_unconfirmed")
            rows.append(row)
    return rows, year


def extract_pdf(store, document_id, *, page_numbers=None):
    import pdfplumber
    doc = store.document(document_id)
    path = store.root / doc["asset"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
        raise WindError("Original hash mismatch")
    report = {"document_id": document_id, "pages": 0, "needs_review": 0}
    inherited_year = None
    with pdfplumber.open(path) as pdf:
        if doc.get("page_count") != len(pdf.pages):
            raise WindError("Declared page count differs from PDF")
        selected = set(page_numbers) if page_numbers is not None else None
        if selected is not None and (not selected or min(selected) < 1 or max(selected) > len(pdf.pages)):
            raise WindError("Page outside document")
        for number, page in enumerate(pdf.pages, 1):
            if selected is not None and number not in selected:
                # A selected-page run cannot infer a year from unprocessed pages.
                inherited_year = None
                continue
            text = page.extract_text() or ""
            tables = page.extract_tables()
            rows, inherited_year = parse_tables(tables, text, inherited_year)
            status = "extracted"
            if not text.strip():
                status = "needs_ocr"
            elif not rows or all(r["classification"] in {"blank", "header"} for r in rows):
                status = "needs_review"  # Image review distinguishes blank from extraction failure.
            store.record_page(document_id, number, rows, status=status,
                              metadata={"extractor_version": VERSION, "text": text, "source_sha256": doc["sha256"]})
            report["pages"] += 1
            report["needs_review"] += status != "extracted" or any(r["classification"] == "review" for r in rows)
    return report


def import_page_review(store, document_id, number, payload, *, actor, reason):
    """Accept OCR/manual repair only with explicit rendered-page review and original hash."""
    doc = store.document(document_id)
    if payload.get("source_sha256") != doc["sha256"] or payload.get("image_reviewed") is not True:
        raise WindError("Page repair requires original hash and image review")
    if not actor.strip() or not reason.strip() or not payload.get("method"):
        raise WindError("Page repair needs operator, reason and method")
    if number < 1 or number > doc["page_count"]:
        raise WindError("Invalid page number")
    prior = store.db.execute("SELECT reviewed FROM pages WHERE document_id=? AND page=?", (document_id, number)).fetchone()
    if prior and prior["reviewed"]:
        raise WindError("Reopen a reviewed page before replacing its extraction")
    rows = payload.get("rows", [])
    if payload.get("blank") is True:
        if any(r.get("classification") not in {"blank", "header"} for r in rows):
            raise WindError("Blank page contains candidate rows")
        status = "blank"
    else:
        if not rows or any(r.get("classification") == "review" for r in rows):
            raise WindError("Unresolved repaired rows")
        status = "extracted"
    # Repaired pages replace only unreviewed extraction. Reopening is separately audited.
    store.record_page(document_id, number, rows, status=status,
                      metadata={**payload, "review_actor": actor, "review_reason": reason})
    store.approve_page(document_id, number, actor=actor, reason=reason)


def hwpx_text(content, max_size=20_000_000):
    """No filesystem extraction or XML entity expansion of attachment archives."""
    result = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = [i for i in archive.infolist() if re.fullmatch(r"Contents/section\d+\.xml", i.filename)]
        if not infos or sum(i.file_size for i in infos) > max_size:
            raise WindError("Missing or oversized HWPX sections")
        for info in sorted(infos, key=lambda i: int(re.search(r"(\d+)\.xml$", i.filename)[1])):
            data = archive.read(info)
            if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                raise WindError("DTD/entities are not accepted")
            root = ElementTree.fromstring(data)
            result.append("\n".join(n.text or "" for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "t"))
    return "\n\n".join(result)


def compare_registers(previous, current):
    """Never interpret disappearance as cancellation; ambiguous identities stay unresolved."""
    def keyed(rows):
        grouped = {}
        for r in rows:
            c = r.get("candidate")
            if c:
                key = (c.get("permit_year"), c.get("number"), c.get("name"), c.get("location"))
                grouped.setdefault(key, []).append(c)
        return grouped
    old, new = keyed(previous), keyed(current)
    changes = []
    for key in sorted(old.keys() | new.keys(), key=str):
        before, after = old.get(key, []), new.get(key, [])
        kind = "missing_requires_followup" if not after else "new_candidate" if not before else "changed_candidate" if before != after else "unchanged"
        if len(before) > 1 or len(after) > 1:
            kind = "ambiguous_identity"
        changes.append({"key": list(key), "kind": kind, "before": before, "after": after})
    return changes

def ocr_page(store, document_id, number, *, executable="tesseract", language="kor+eng"):
    """Optional installed Tesseract; all OCR words remain pending visual review."""
    import csv
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    import pdfplumber
    from .core import atomic_write
    tool = shutil.which(executable)
    if not tool or not re.fullmatch(r"[a-zA-Z_+]+", language):
        raise WindError("Installed Tesseract and explicit OCR language data are required")
    doc = store.document(document_id)
    source = store.root / doc["asset"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != doc["sha256"]:
        raise WindError("Original hash mismatch")
    with pdfplumber.open(source) as pdf:
        if not 1 <= number <= len(pdf.pages):
            raise WindError("OCR page outside document")
        page = pdf.pages[number - 1]
        if max(page.width, page.height) > 3000:
            raise WindError("Oversized OCR page")
        image = page.to_image(resolution=200).original
    with tempfile.TemporaryDirectory(prefix="bre-wind-ocr-") as temporary:
        image_path = Path(temporary) / "page.png"
        image.save(image_path)
        rendered = image_path.read_bytes()
        result = subprocess.run([tool, str(image_path), "stdout", "-l", language, "tsv"], capture_output=True, timeout=90)
        if result.returncode:
            raise WindError("OCR failed; verify Tesseract language files")
        tsv = result.stdout.decode("utf-8")
    words = list(csv.DictReader(io.StringIO(tsv), delimiter="\t"))
    rows = [{"classification":"review", "text":w["text"], "bbox":[w["left"],w["top"],w["width"],w["height"]],
             "confidence":w["conf"], "extractor_version":"tesseract-tsv-1"} for w in words if w.get("text","").strip()]
    image_hash = hashlib.sha256(rendered).hexdigest()
    image_asset = "derivatives/" + image_hash + ".png"
    atomic_write(store.root / image_asset, rendered)
    store.record_page(document_id,number,rows,status="needs_review",
        metadata={"extractor_version":"tesseract-tsv-1","language":language,"dpi":200,
                  "image_asset":image_asset,"image_sha256":image_hash,"source_sha256":doc["sha256"]})
    return {"document_id":document_id,"page":number,"status":"needs_review","words":len(rows),"image_asset":image_asset}
