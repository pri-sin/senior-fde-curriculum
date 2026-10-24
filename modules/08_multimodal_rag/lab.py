"""Module 08 lab: layout-aware document parsing, header-aware table extraction with numeric validation,
and ColPali-style late-interaction (MaxSim) retrieval over page patch embeddings.

No ML weights are needed: OCR output is simulated as positioned words (the structure a layout model emits),
and "patch embeddings" are deterministic hashed vectors with the same shape and scoring math as ColPali.
Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import re
import sys
from decimal import Decimal, InvalidOperation
from enum import Enum

from pydantic import BaseModel, Field


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "msg": record.getMessage()}
        payload.update(getattr(record, "ctx", {}))
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


log = get_logger("lab08")


class DocumentError(Exception):
    pass


class OcrTransientError(DocumentError):
    """Retryable OCR service failure."""


class TableValidationError(DocumentError):
    pass


class LowConfidenceError(DocumentError):
    pass


class ElementType(str, Enum):
    TITLE = "title"
    TEXT = "text"
    TABLE = "table"
    FOOTER = "footer"


class Word(BaseModel):
    text: str
    x: float
    y: float
    w: float
    conf: float = Field(ge=0, le=1)


class Page(BaseModel):
    number: int
    width: float = 600
    height: float = 800
    words: list[Word]


class Element(BaseModel):
    type: ElementType
    page: int
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float
    rows: list[list[str]] | None = None


def group_lines(words: list[Word], y_tol: float = 6.0) -> list[list[Word]]:
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.y, w.x)):
        if lines and abs(lines[-1][0].y - w.y) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(l, key=lambda w: w.x) for l in lines]


def split_columns(line: list[Word], gap: float = 40.0) -> list[str]:
    """Words separated by a large horizontal gap belong to different cells."""
    cells: list[list[str]] = [[line[0].text]]
    for prev, cur in zip(line, line[1:]):
        if cur.x - (prev.x + prev.w) > gap:
            cells.append([cur.text])
        else:
            cells[-1].append(cur.text)
    return [" ".join(c) for c in cells]


def parse_page(page: Page, min_conf: float = 0.8) -> list[Element]:
    """Heuristic layout analysis: footer by position, title by size/position, table by multi-cell aligned lines."""
    elements: list[Element] = []
    lines = group_lines(page.words)
    table_rows: list[tuple[list[str], list[Word]]] = []

    def flush_table() -> None:
        if len(table_rows) >= 2:
            ws = [w for _, ln in table_rows for w in ln]
            elements.append(Element(type=ElementType.TABLE, page=page.number, text="\n".join(" | ".join(r) for r, _ in table_rows),
                                    bbox=(min(w.x for w in ws), min(w.y for w in ws), max(w.x + w.w for w in ws), max(w.y for w in ws)),
                                    confidence=min(w.conf for w in ws), rows=[r for r, _ in table_rows]))
        else:
            for r, ln in table_rows:
                elements.append(_text_element(page.number, ln, ElementType.TEXT))
        table_rows.clear()

    for ln in lines:
        y = ln[0].y
        if y > page.height * 0.93:
            flush_table()
            elements.append(_text_element(page.number, ln, ElementType.FOOTER))
            continue
        cells = split_columns(ln)
        if len(cells) >= 3:
            table_rows.append((cells, ln))
            continue
        flush_table()
        etype = ElementType.TITLE if y < page.height * 0.08 else ElementType.TEXT
        elements.append(_text_element(page.number, ln, etype))
    flush_table()
    low = [e for e in elements if e.confidence < min_conf]
    if low:
        log.warning("low confidence elements", extra={"ctx": {"page": page.number, "count": len(low), "types": [e.type for e in low]}})
    return [e for e in elements if e.type is not ElementType.FOOTER]  # footers/headers are noise for retrieval


def _text_element(page: int, ln: list[Word], etype: ElementType) -> Element:
    return Element(type=etype, page=page, text=" ".join(w.text for w in ln),
                   bbox=(min(w.x for w in ln), ln[0].y, max(w.x + w.w for w in ln), ln[0].y),
                   confidence=min(w.conf for w in ln))


NUM = re.compile(r"^\(?-?\$?[\d,]+(\.\d+)?\)?$")


def parse_number(cell: str) -> Decimal:
    """'(1,204)' -> -1204 (accounting negative); strips currency and thousands separators."""
    s = cell.strip()
    if not NUM.match(s):
        raise TableValidationError(f"not numeric: {cell!r}")
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = Decimal(re.sub(r"[()$,]", "", s))
    except InvalidOperation as exc:
        raise TableValidationError(f"bad number {cell!r}") from exc
    return -v if neg else v


class TableRecord(BaseModel):
    page: int
    row_label: str
    values: dict[str, Decimal]


def table_to_records(el: Element) -> list[TableRecord]:
    """Header-aware: every data cell is tied to its row label and column header ('row-as-record' chunks)."""
    assert el.rows
    header, *body = el.rows
    out: list[TableRecord] = []
    for row in body:
        if len(row) != len(header):
            raise TableValidationError(f"row has {len(row)} cells, header has {len(header)}: {row}")
        label = row[0]
        if label.lower().startswith("total"):
            continue
        out.append(TableRecord(page=el.page, row_label=label, values={h: parse_number(c) for h, c in zip(header[1:], row[1:])}))
    return out


def validate_totals(el: Element) -> None:
    header, *body = el.rows or [[]]
    totals = [r for r in body if r and r[0].lower().startswith("total")]
    data = [r for r in body if r and not r[0].lower().startswith("total")]
    for t in totals:
        for col in range(1, len(header)):
            expected = sum((parse_number(r[col]) for r in data), Decimal(0))
            if parse_number(t[col]) != expected:
                raise TableValidationError(f"column {header[col]!r}: total {t[col]} != sum {expected}")


# ---- ColPali-style late interaction -----------------------------------------------------------------
DIM = 64


def vec(token: str) -> list[float]:
    h = hashlib.sha256(token.lower().encode()).digest()
    v = [(b - 127.5) / 127.5 for b in h[:DIM // 2]] + [(b - 127.5) / 127.5 for b in hashlib.sha256(h).digest()[:DIM // 2]]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class PageIndex:
    """One multi-vector per page: a vector per word-patch. Score = sum over query tokens of the max patch similarity."""

    def __init__(self) -> None:
        self.patches: dict[str, list[list[float]]] = {}

    def add_page(self, page_id: str, page: Page) -> None:
        self.patches[page_id] = [vec(w.text) for w in page.words] or [vec("")]

    def maxsim(self, query: str, page_id: str) -> float:
        q = [vec(t) for t in re.findall(r"\w+", query)]
        return sum(max(dot(qv, pv) for pv in self.patches[page_id]) for qv in q)

    def search(self, query: str, k: int = 2) -> list[tuple[str, float]]:
        return sorted(((pid, self.maxsim(query, pid)) for pid in self.patches), key=lambda x: -x[1])[:k]


class FlakyOcr:
    """Simulated OCR service: fails twice, then returns positioned words for a financial page."""

    def __init__(self) -> None:
        self.calls = 0

    async def ocr(self, doc_id: str, page_no: int) -> Page:
        self.calls += 1
        await asyncio.sleep(0.002)
        if self.calls <= 2:
            raise OcrTransientError("503 from OCR endpoint")
        return make_page(page_no)


def make_page(n: int, bad_total: bool = False) -> Page:
    def row(y: float, cells: list[str], conf: float = 0.98) -> list[Word]:
        xs = [40, 300, 420]
        return [Word(text=c, x=x, y=y, w=len(c) * 7, conf=conf) for x, c in zip(xs, cells)]

    words = [Word(text="Quarterly", x=40, y=30, w=70, conf=0.99), Word(text="Report", x=115, y=30, w=50, conf=0.99),
             Word(text="Revenue", x=40, y=120, w=50, conf=0.97), Word(text="rose", x=95, y=120, w=30, conf=0.97)]
    words += row(200, ["Segment", "2023", "2024"]) + row(220, ["Cloud", "1,200", "(1,204)"]) + row(240, ["Services", "800", "300"])
    words += row(260, ["Total", "2,000", "1,100" if bad_total else "(904)"], conf=0.85)
    words += [Word(text="Page", x=280, y=770, w=30, conf=0.99), Word(text=str(n), x=315, y=770, w=10, conf=0.99)]
    return Page(number=n, words=words)


async def ocr_with_retry(ocr: FlakyOcr, doc: str, page_no: int, attempts: int = 5) -> Page:
    for i in range(1, attempts + 1):
        try:
            return await ocr.ocr(doc, page_no)
        except OcrTransientError:
            if i == attempts:
                raise
            await asyncio.sleep(random.uniform(0, 0.004 * 2 ** i))
    raise AssertionError("unreachable")


async def main() -> None:
    ocr = FlakyOcr()
    page = await ocr_with_retry(ocr, "q4.pdf", 1)
    log.info("ocr ok", extra={"ctx": {"attempts": ocr.calls, "words": len(page.words)}})
    elements = parse_page(page)
    for e in elements:
        log.info("element", extra={"ctx": {"type": e.type, "text": e.text[:60], "conf": e.confidence}})
    table = next(e for e in elements if e.type is ElementType.TABLE)
    validate_totals(table)
    log.info("table valid", extra={"ctx": {"records": [r.model_dump(mode="json") for r in table_to_records(table)]}})

    bad_table = next(e for e in parse_page(make_page(2, bad_total=True)) if e.type is ElementType.TABLE)
    try:
        validate_totals(bad_table)
    except TableValidationError as exc:
        log.info("validation caught bad extraction", extra={"ctx": {"error": str(exc)}})

    index = PageIndex()
    index.add_page("q4.pdf#1", page)
    other = Page(number=2, words=[Word(text=t, x=10 * i, y=50, w=30, conf=0.9) for i, t in enumerate("employee leave policy vacation days parental".split())])
    index.add_page("handbook.pdf#2", other)
    for q in ("cloud revenue 2024", "parental leave vacation"):
        log.info("maxsim search", extra={"ctx": {"q": q, "ranked": [(p, round(s, 3)) for p, s in index.search(q)]}})


if __name__ == "__main__":
    asyncio.run(main())
