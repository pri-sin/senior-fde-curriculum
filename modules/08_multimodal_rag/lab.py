"""Module 08 lab: layout-aware document parsing, header-aware table extraction with numeric validation,
and ColPali-style late-interaction (MaxSim) retrieval over page patch embeddings.

No ML weights are needed: OCR output is simulated as positioned words (the structure a layout model emits),
and "patch embeddings" are deterministic hashed vectors with the same shape and scoring math as ColPali.
The "Syllabus extension" sections add document-structure routing, an OCR pipeline (deskew, Otsu, glyph matching, confidence
gate), pixel-level ColPali-style MaxSim, a vision-language bridge, image prompt payloads, CLIP InfoNCE math, image similarity
search, merged-cell tables, chart reading, legacy PDF ingestion, bbox alignment and multi-page context management.
Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import logging
import math
import random
import re
import struct
import sys
import zlib
from collections import Counter
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal, TypeVar

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


# ---- Syllabus extension: identifying document structures ---------------------------------------------
Grid = list[list[int]]  # grayscale pixels 0 (black) to 255 (white); plain lists, no numpy
RgbGrid = list[list[tuple[int, int, int]]]
T = TypeVar("T")


class ImageValidationError(DocumentError):
    pass


class MissingDependencyError(DocumentError):
    """An optional third-party adapter was used without its package installed."""


class SourceKind(str, Enum):
    BORN_DIGITAL = "born_digital"
    SCANNED = "scanned"
    MIXED = "mixed"


class PageSignals(BaseModel):
    page: int
    text_chars: int = Field(ge=0)  # characters in the embedded text layer
    image_coverage: float = Field(ge=0, le=1)  # fraction of the page area covered by raster images
    garbled_ratio: float = Field(default=0.0, ge=0, le=1)  # share of text-layer characters that are replacement or control glyphs


def classify_page_source(s: PageSignals) -> SourceKind:
    """Route decision: a usable text layer and little imagery is born-digital; no usable text under a page-sized image is a scan;
    everything else (figures on digital pages, scans carrying a thin OCR layer) needs both paths."""
    usable = s.text_chars * (1 - s.garbled_ratio)
    if usable >= 200 and s.image_coverage < 0.5:
        return SourceKind.BORN_DIGITAL
    if usable < 20 and s.image_coverage >= 0.5:
        return SourceKind.SCANNED
    return SourceKind.MIXED


ROUTES = {SourceKind.BORN_DIGITAL: "text_layer", SourceKind.SCANNED: "ocr", SourceKind.MIXED: "text_layer+ocr_for_images"}


class DocumentProfile(BaseModel):
    kind: SourceKind
    routes: dict[int, str]


def classify_document(signals: list[PageSignals]) -> DocumentProfile:
    kinds = {s.page: classify_page_source(s) for s in signals}
    unique = set(kinds.values())
    return DocumentProfile(kind=unique.pop() if len(unique) == 1 else SourceKind.MIXED, routes={p: ROUTES[k] for p, k in kinds.items()})


BULLET = re.compile(r"^(?:[-*•]|\d{1,2}[.)])\s+\S")
CAPTION = re.compile(r"^(?:Figure|Fig\.)\s*\d+", re.I)
NUMBERED_HEADING = re.compile(r"^\d+(?:\.\d+)*\s+[A-Z][^.]{0,60}$")


class StructureReport(BaseModel):
    page: int
    headings: list[str]
    list_items: int
    tables: int
    figures: list[str]
    text_lines: int


def detect_structure(page: Page) -> StructureReport:
    """Builds on parse_page (title, table, text, footer) and adds headings, list items and figure captions from line patterns."""
    rep = StructureReport(page=page.number, headings=[], list_items=0, tables=0, figures=[], text_lines=0)
    for el in parse_page(page):
        if el.type is ElementType.TITLE:
            rep.headings.append(el.text)
        elif el.type is ElementType.TABLE:
            rep.tables += 1
        elif CAPTION.match(el.text):
            rep.figures.append(el.text)
        elif NUMBERED_HEADING.match(el.text):
            rep.headings.append(el.text)
        elif BULLET.match(el.text):
            rep.list_items += 1
        else:
            rep.text_lines += 1
    return rep


def words_from_text(text: str, x: float, y: float, conf: float = 0.97) -> list[Word]:
    """Positions words left to right on one line (7 px per character, one-character gaps)."""
    out: list[Word] = []
    for tok in text.split():
        out.append(Word(text=tok, x=x, y=y, w=len(tok) * 7, conf=conf))
        x += (len(tok) + 1) * 7
    return out


# ---- Syllabus extension: OCR pipeline (preprocess, recognize, confidence gate) -----------------------
GLYPHS = {  # a 3x5 digit font: the recognizer's templates and the renderer's source
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111", "4": "101101111001001",
    "5": "111100111001111", "6": "111100111101111", "7": "111001001001001", "8": "111101111101111", "9": "111101111001111",
}
INK, PAPER = 30, 225


def blank(h: int, w: int, v: int = 255) -> Grid:
    return [[v] * w for _ in range(h)]


def render_lines(lines: list[str], scale: int = 4, margin: int = 1) -> Grid:
    """Each character occupies a cell of (7*scale rows, 4*scale cols); glyph pixels are scale x scale squares."""
    cell_h, cell_w = 7 * scale, 4 * scale
    cols = max(len(l) for l in lines) + 2 * margin
    g = blank((len(lines) + 2 * margin) * cell_h, cols * cell_w, PAPER)
    for li, line in enumerate(lines):
        for ci, ch in enumerate(line):
            if ch == " ":
                continue
            if ch not in GLYPHS:
                raise DocumentError(f"no glyph for {ch!r}")
            for k, bit in enumerate(GLYPHS[ch]):
                if bit == "1":
                    y0, x0 = (li + margin) * cell_h + (k // 3) * scale, (ci + margin) * cell_w + (k % 3) * scale
                    for y in range(y0, y0 + scale):
                        for x in range(x0, x0 + scale):
                            g[y][x] = INK
    return g


def add_noise(g: Grid, amplitude: int, salt: float, seed: int) -> Grid:
    """Uniform gray noise plus salt-and-pepper specks: a stand-in for sensor noise on a photographed page."""
    rnd = random.Random(seed)
    out = []
    for row in g:
        new = []
        for v in row:
            v = min(255, max(0, v + rnd.randint(-amplitude, amplitude)))
            new.append(rnd.choice((0, 255)) if rnd.random() < salt else v)
        out.append(new)
    return out


def rotate(g: Grid, deg: float, fill: int = 255) -> Grid:
    """Nearest-neighbour rotation about the centre (inverse mapping, so there are no holes)."""
    h, w = len(g), len(g[0])
    cy, cx, t = (h - 1) / 2, (w - 1) / 2, math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    out = blank(h, w, fill)
    for y in range(h):
        for x in range(w):
            dy, dx = y - cy, x - cx
            sx, sy = round(c * dx + s * dy + cx), round(-s * dx + c * dy + cy)
            if 0 <= sx < w and 0 <= sy < h:
                out[y][x] = g[sy][sx]
    return out


def median3(g: Grid) -> Grid:
    h, w = len(g), len(g[0])
    return [[sorted(g[min(h - 1, max(0, y + dy))][min(w - 1, max(0, x + dx))] for dy in (-1, 0, 1) for dx in (-1, 0, 1))[4]
             for x in range(w)] for y in range(h)]


def otsu_threshold(g: Grid) -> int:
    """Picks the gray level that maximizes between-class variance (ink vs paper)."""
    hist = [0] * 256
    for row in g:
        for v in row:
            hist[v] += 1
    total, sum_all = sum(hist), sum(i * n for i, n in enumerate(hist))
    w_b = sum_b = 0
    best, best_t = -1.0, 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        var = w_b * w_f * (sum_b / w_b - (sum_all - sum_b) / w_f) ** 2
        if var > best:
            best, best_t = var, t
    return best_t


def binarize(g: Grid, threshold: int) -> Grid:
    """1 = ink, 0 = paper."""
    return [[1 if v <= threshold else 0 for v in row] for row in g]


def estimate_skew(ink: Grid, max_deg: float = 6.0, step: float = 0.5) -> float:
    """Projection-profile method: text lines make the row-sum histogram spiky only when the page is level, so the
    best correction maximizes the sum of squared row sums. Returns the angle to rotate by to deskew."""
    best_a, best_score = 0.0, -1.0
    a = -max_deg
    while a <= max_deg + 1e-9:
        score = sum(sum(r) ** 2 for r in rotate(ink, a, fill=0))
        if score > best_score + 1e-9:
            best_a, best_score = round(a, 3), score
        a += step
    return best_a


def _runs(profile: list[int], min_value: float) -> list[tuple[int, int]]:
    out, start = [], None
    for i, v in enumerate(profile + [0]):
        if v > min_value and start is None:
            start = i
        elif v <= min_value and start is not None:
            out.append((start, i))
            start = None
    return out


def block_bits(patch: Grid, rows: int = 5, cols: int = 3, ink_below: int = 128) -> list[int]:
    """Downsamples any pixel block to a rows x cols ink map by block means."""
    h, w = len(patch), len(patch[0])
    bits = []
    for r in range(rows):
        for c in range(cols):
            ys, ye = r * h // rows, max(r * h // rows + 1, (r + 1) * h // rows)
            xs, xe = c * w // cols, max(c * w // cols + 1, (c + 1) * w // cols)
            vals = [patch[y][x] for y in range(ys, ye) for x in range(xs, xe)]
            bits.append(1 if sum(vals) / len(vals) < ink_below else 0)
    return bits


def recognize_glyph(crop_ink: Grid) -> tuple[str, float]:
    bits = "".join(map(str, block_bits([[255 - 255 * v for v in row] for row in crop_ink])))
    best = max(GLYPHS, key=lambda ch: sum(a == b for a, b in zip(GLYPHS[ch], bits)))
    return best, sum(a == b for a, b in zip(GLYPHS[best], bits)) / 15


class OcrResult(BaseModel):
    text: str
    confidence: float  # minimum character confidence
    char_confidences: list[float]
    skew_deg: float
    threshold: int
    status: Literal["accepted", "review"]


def run_ocr_pipeline(gray: Grid, accept: float = 0.9, reject: float = 0.7) -> OcrResult:
    """denoise -> binarize (Otsu) -> deskew -> segment lines and glyphs -> template match -> confidence gate.
    Confidence at least `accept` is auto-accepted, between the two goes to human review, below `reject` raises."""
    clean = median3(gray)
    t = otsu_threshold(clean)
    ink = binarize(clean, t)
    skew = estimate_skew(ink)
    ink = rotate(ink, skew, fill=0)
    row_profile = [sum(r) for r in ink]
    lines: list[str] = []
    confs: list[float] = []
    for y0, y1 in _runs(row_profile, 0.15 * max(row_profile)):
        band = ink[y0:y1]
        col_profile = [sum(band[y][x] for y in range(len(band))) for x in range(len(band[0]))]
        min_w = max(2, int(0.2 * (y1 - y0)))
        runs = [(x0, x1) for x0, x1 in _runs(col_profile, 0) if x1 - x0 >= min_w]  # narrower runs are specks, not glyphs
        typical_w = sorted(x1 - x0 for x0, x1 in runs)[len(runs) // 2] if runs else 0
        text, prev_end = "", None
        for x0, x1 in runs:
            if prev_end is not None and x0 - prev_end > 0.8 * typical_w:
                text += " "  # a gap wider than most of a glyph is a space
            ch, c = recognize_glyph([row[x0:x1] for row in band])
            text += ch
            confs.append(c)
            prev_end = x1
        lines.append(text)
    if not confs:
        raise LowConfidenceError("no glyphs found")
    low = min(confs)
    if low < reject:
        raise LowConfidenceError(f"min character confidence {low:.2f} below {reject}")
    return OcrResult(text="\n".join(lines), confidence=round(low, 3), char_confidences=[round(c, 2) for c in confs], skew_deg=skew,
                     threshold=t, status="accepted" if low >= accept else "review")


def grid_from_pillow(path: str) -> Grid:
    """Optional adapter: load a real image as a grayscale grid. Pillow is imported lazily; the offline lab never needs it."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise MissingDependencyError("Pillow is not installed: pip install pillow") from exc
    with Image.open(path) as im:
        gray = im.convert("L")
        w, h = gray.size
        px = list(gray.getdata())
    return [px[y * w:(y + 1) * w] for y in range(h)]


# ---- Syllabus extension: OCR-free page embeddings (patch grid and MaxSim over pixels) ----------------
def patchify(g: Grid, ph: int, pw: int) -> list[Grid]:
    """Cuts the page into a row-major grid of ph x pw patches, padding the border with white (a ViT-style patch grid)."""
    h, w = len(g), len(g[0])
    padded = [row + [255] * (-w % pw) for row in g] + [[255] * (w + -w % pw) for _ in range(-h % ph)]
    return [[row[x:x + pw] for row in padded[y:y + ph]] for y in range(0, len(padded), ph) for x in range(0, len(padded[0]), pw)]


def encode_patch(patch: Grid) -> list[float]:
    """Toy patch encoder: 5x3 ink map mapped to +-1 and L2-normalized. Near-identical glyphs stay close under noise."""
    v = [1.0 if b else -1.0 for b in block_bits(patch)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


class PixelPageIndex:
    """ColPali-shaped index over raw pixels: one vector per patch, query score = sum over query tokens of the best patch match.
    Query tokens are rendered with the same font, so no OCR is involved on the page side."""

    def __init__(self, scale: int = 4) -> None:
        self.scale = scale
        self.cell = (7 * scale, 4 * scale)  # patch size = one character cell, so patches align with glyphs in this synthetic font
        self.patches: dict[str, list[list[float]]] = {}

    def _encode(self, patch: Grid) -> list[float]:
        """The toy encoder reads the glyph area of the cell (5 x 3 font pixels); a real ViT sees every pixel of its patch."""
        return encode_patch([row[:3 * self.scale] for row in patch[:5 * self.scale]])

    def add_page(self, page_id: str, g: Grid) -> int:
        self.patches[page_id] = [self._encode(p) for p in patchify(g, *self.cell)]
        return len(self.patches[page_id])

    def query_vectors(self, query: str) -> list[list[float]]:
        return [self._encode(render_lines([ch], self.scale, margin=0)) for ch in query if ch != " "]

    def maxsim(self, query: str, page_id: str) -> float:
        return sum(max(dot(q, p) for p in self.patches[page_id]) for q in self.query_vectors(query))

    def search(self, query: str, k: int = 2) -> list[tuple[str, float]]:
        return sorted(((pid, self.maxsim(query, pid)) for pid in self.patches), key=lambda x: -x[1])[:k]

    def index_bytes(self, dim: int = 128, bytes_per_dim: int = 1) -> int:
        """Storage if each patch were a ColPali-style int8 vector of `dim` dimensions."""
        return sum(len(v) for v in self.patches.values()) * dim * bytes_per_dim


# ---- Syllabus extension: vision-language bridge (encoder, projector, token budget) -------------------
class BridgeSpec(BaseModel):
    image_h: int = 448
    image_w: int = 448
    patch: int = 14
    pool: int = 2  # pool x pool neighbouring patches are merged into one token
    d_vision: int = 1024
    d_lm: int = 4096

    @property
    def patches(self) -> int:
        return math.ceil(self.image_h / self.patch) * math.ceil(self.image_w / self.patch)

    @property
    def tokens(self) -> int:
        return math.ceil(self.patches / (self.pool * self.pool))

    @property
    def linear_params(self) -> int:
        return self.d_vision * self.pool ** 2 * self.d_lm + self.d_lm  # merged patch features in, LM embedding out, plus bias


class LinearProjector:
    """The bridge: one learned linear map from vision feature space to the language model's embedding space.
    Weights here are deterministic (hash-derived) so the demo is reproducible; real ones are trained."""

    def __init__(self, d_in: int, d_out: int, seed: str = "bridge") -> None:
        scale = 1 / math.sqrt(d_in)
        self.w = [[((hashlib.sha256(f"{seed}:{i}:{j}".encode()).digest()[0] - 127.5) / 127.5) * scale for j in range(d_in)] for i in range(d_out)]
        self.b = [0.0] * d_out

    def __call__(self, x: list[float]) -> list[float]:
        return [sum(wi * xi for wi, xi in zip(row, x)) + b for row, b in zip(self.w, self.b)]


def vision_features(g: Grid, patch: int) -> tuple[list[list[float]], int, int]:
    """Per-patch features: a 4x4 block-mean map scaled to [-1, 1]. Returns (features, patch rows, patch cols)."""
    rows, cols = math.ceil(len(g) / patch), math.ceil(len(g[0]) / patch)
    feats = []
    for p in patchify(g, patch, patch):
        h, w = len(p), len(p[0])
        feats.append([sum(p[y][x] for y in range(r * h // 4, (r + 1) * h // 4) for x in range(c * w // 4, (c + 1) * w // 4))
                      / (max(1, (h // 4) * (w // 4)) * 127.5) - 1.0 for r in range(4) for c in range(4)])
    return feats, rows, cols


def merge_patches(feats: list[list[float]], rows: int, cols: int, pool: int) -> list[list[float]]:
    """Concatenates pool x pool neighbouring patch vectors into one wider vector (pixel-shuffle style token merging)."""
    out = []
    for r in range(0, rows, pool):
        for c in range(0, cols, pool):
            merged: list[float] = []
            for dr in range(pool):
                for dc in range(pool):
                    rr, cc = min(rows - 1, r + dr), min(cols - 1, c + dc)
                    merged += feats[rr * cols + cc]
            out.append(merged)
    return out


class SeqToken(BaseModel):
    pos: int
    kind: Literal["text", "image_start", "image", "image_end"]
    ref: str


def build_sequence(text_before: list[str], visual_embeddings: list[list[float]], text_after: list[str], context_limit: int) -> list[SeqToken]:
    """The LM sees one flat sequence: text tokens, an image-start marker, one embedding per visual token, an end marker, text."""
    seq = [SeqToken(pos=0, kind="text", ref=t) for t in text_before]
    seq.append(SeqToken(pos=0, kind="image_start", ref="<img>"))
    seq += [SeqToken(pos=0, kind="image", ref=f"v{i}") for i in range(len(visual_embeddings))]
    seq.append(SeqToken(pos=0, kind="image_end", ref="</img>"))
    seq += [SeqToken(pos=0, kind="text", ref=t) for t in text_after]
    if len(seq) > context_limit:
        raise DocumentError(f"sequence of {len(seq)} tokens exceeds the context limit {context_limit}")
    return [t.model_copy(update={"pos": i}) for i, t in enumerate(seq)]


# ---- Syllabus extension: prompting with images (payloads, validation, token cost) --------------------
ALLOWED_MEDIA = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff", "image/gif": b"GIF8", "image/webp": b"RIFF"}


def make_png(g: Grid) -> bytes:
    """Writes a valid 8-bit grayscale PNG with the stdlib only (zlib + struct), so payloads can carry real image bytes."""
    h, w = len(g), len(g[0])
    raw = b"".join(b"\x00" + bytes(max(0, min(255, v)) for v in row) for row in g)  # filter type 0 per scanline

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


class ImageInfo(BaseModel):
    media_type: str
    width: int
    height: int
    size_bytes: int


def validate_image(data: bytes, media_type: str, max_bytes: int = 5_000_000, max_edge: int = 8000,
                   dims: tuple[int, int] | None = None) -> ImageInfo:
    """Rejects wrong media types, bytes whose signature disagrees with the declared type, oversize files and absurd dimensions."""
    magic = ALLOWED_MEDIA.get(media_type)
    if magic is None:
        raise ImageValidationError(f"unsupported media type {media_type!r}; allowed: {sorted(ALLOWED_MEDIA)}")
    if not data.startswith(magic) or (media_type == "image/webp" and data[8:12] != b"WEBP"):
        raise ImageValidationError(f"bytes do not look like {media_type}")
    if len(data) > max_bytes:
        raise ImageValidationError(f"image is {len(data)} bytes; limit is {max_bytes}")
    if media_type == "image/png":
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ImageValidationError("PNG header is missing IHDR")
        w, h = struct.unpack(">II", data[16:24])
    elif dims is not None:
        w, h = dims
    else:
        raise ImageValidationError("dimensions are only parsed for PNG here; pass dims for other formats")
    if min(w, h) < 1 or max(w, h) > max_edge:
        raise ImageValidationError(f"dimensions {w}x{h} outside 1..{max_edge}")
    return ImageInfo(media_type=media_type, width=w, height=h, size_bytes=len(data))


def anthropic_user_message(images: list[tuple[bytes, str]], text: str) -> dict[str, Any]:
    """Anthropic Messages API shape: content blocks of type image (base64 source) and text. Images go before the question."""
    blocks: list[dict[str, Any]] = []
    for data, mt in images:
        validate_image(data, mt)
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(data).decode("ascii")}})
    blocks.append({"type": "text", "text": text})
    return {"role": "user", "content": blocks}


def openai_user_message(images: list[tuple[bytes, str]], text: str, detail: Literal["low", "high", "auto"] = "auto") -> dict[str, Any]:
    """OpenAI Chat Completions shape: text and image_url parts; base64 travels as a data URL."""
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for data, mt in images:
        validate_image(data, mt)
        parts.append({"type": "image_url", "image_url": {"url": "data:" + mt + ";base64," + base64.b64encode(data).decode("ascii"), "detail": detail}})
    return {"role": "user", "content": parts}


def anthropic_image_tokens(w: int, h: int, max_edge: int = 1568) -> int:
    """Published rule of thumb: tokens = width x height / 750 after downscaling so the long edge is at most 1568 px.
    Provider limits change; confirm against the current documentation before budgeting."""
    scale = min(1.0, max_edge / max(w, h))
    return int(round(w * scale) * round(h * scale) / 750)


def tile_image_tokens(w: int, h: int, detail: str = "high") -> int:
    """Tile-based heuristic used by some OpenAI vision models: low detail = flat 85 tokens; high detail = fit within 2048x2048,
    scale the short side to 768, then 85 + 170 per 512 px tile. Confirm against current provider documentation."""
    if detail == "low":
        return 85
    s = min(1.0, 2048 / max(w, h))
    w, h = w * s, h * s
    s = min(1.0, 768 / min(w, h))
    w, h = w * s, h * s
    return 85 + 170 * math.ceil(round(w) / 512) * math.ceil(round(h) / 512)


class AnthropicVisionAdapter:
    """Optional live adapter. The `anthropic` package is imported lazily; the API key is read from ANTHROPIC_API_KEY by the SDK.
    Wrapped in exponential backoff for rate limits and connection errors."""

    def __init__(self, model: str, max_tokens: int = 512) -> None:
        self.model, self.max_tokens = model, max_tokens

    async def ask(self, message: dict[str, Any]) -> str:
        try:
            import anthropic
        except ImportError as exc:
            raise MissingDependencyError("the anthropic package is not installed: pip install anthropic") from exc
        client = anthropic.AsyncAnthropic()
        retryable = (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)

        async def once() -> str:
            resp = await client.messages.create(model=self.model, max_tokens=self.max_tokens, messages=[message])
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

        return await retry_async(once, retryable)


async def retry_async(fn: Callable[[], Awaitable[T]], retryable: tuple[type[BaseException], ...], attempts: int = 4, base: float = 0.005) -> T:
    """Exponential backoff with full jitter around any I/O call."""
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except retryable:
            if i == attempts:
                raise
            await asyncio.sleep(random.uniform(0, base * 2 ** i))
    raise AssertionError("unreachable")


# ---- Syllabus extension: CLIP-style contrastive learning (InfoNCE, zero-shot) ------------------------
def unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def text_embed(prompt: str) -> list[float]:
    """Toy text tower: bag of hashed word vectors, L2-normalized."""
    acc = [0.0] * DIM
    for w in re.findall(r"\w+", prompt):
        acc = [a + b for a, b in zip(acc, vec(w))]
    return unit(acc)


def image_embed(label: str, seed: int, noise: float = 0.3) -> list[float]:
    """Toy image tower: the concept direction plus per-image noise (stands in for what a trained vision encoder produces)."""
    rnd = random.Random(seed)
    return unit([a + noise * rnd.gauss(0, 1) / math.sqrt(DIM) * 3 for a in vec(label)])


def logsumexp(xs: list[float]) -> float:
    m = max(xs)
    return m + math.log(sum(math.exp(x - m) for x in xs))


def info_nce(images: list[list[float]], texts: list[list[float]], scale: float) -> dict[str, float]:
    """Symmetric InfoNCE: logits[i][j] = scale * cos(image_i, text_j); the correct pair is the diagonal.
    Loss is cross-entropy over rows (image to text) and over columns (text to image), averaged. Chance level is ln(N)."""
    n = len(images)
    logits = [[scale * dot(i, t) for t in texts] for i in images]
    i2t = sum(logsumexp(logits[i]) - logits[i][i] for i in range(n)) / n
    t2i = sum(logsumexp([logits[r][c] for r in range(n)]) - logits[c][c] for c in range(n)) / n
    acc = sum(max(range(n), key=lambda j: logits[i][j]) == i for i in range(n)) / n
    return {"i2t": round(i2t, 4), "t2i": round(t2i, 4), "loss": round((i2t + t2i) / 2, 4), "top1": acc, "chance": round(math.log(n), 4)}


def zero_shot(image: list[float], labels: list[str], template: str = "a photo of a {}", scale: float = 100.0) -> dict[str, float]:
    """Zero-shot classification: embed one text prompt per class, softmax over image-text similarities."""
    logits = [scale * dot(image, text_embed(template.format(lab))) for lab in labels]
    z = logsumexp(logits)
    return {lab: round(math.exp(l - z), 4) for lab, l in zip(labels, logits)}


def contrastive_step(images: list[list[float]], texts: list[list[float]], scale: float, lr: float) -> list[list[float]]:
    """One gradient step on the image embeddings for the image-to-text loss: dL/de_i = -(scale/N)(t_i - sum_j p_ij t_j)."""
    n, out = len(images), []
    for i, e in enumerate(images):
        logits = [scale * dot(e, t) for t in texts]
        z = logsumexp(logits)
        p = [math.exp(l - z) for l in logits]
        grad = [-(scale / n) * (texts[i][d] - sum(p[j] * texts[j][d] for j in range(n))) for d in range(DIM)]
        out.append(unit([x - lr * g for x, g in zip(e, grad)]))
    return out


# ---- Syllabus extension: image-to-image similarity search (perceptual hash + color histogram) ---------
def make_rgb_image(kind: str, size: int = 24) -> RgbGrid:
    img: RgbGrid = [[(0, 0, 0)] * size for _ in range(size)]
    for y in range(size):
        for x in range(size):
            if kind == "red_on_blue":
                inside = size // 4 <= x < 3 * size // 4 and size // 4 <= y < 3 * size // 4
                img[y][x] = (200, 30, 30) if inside else (20, 30, 170)
            elif kind == "stripes":
                img[y][x] = (30, 160, 60) if (y // 3) % 2 == 0 else (240, 240, 240)
            elif kind == "gradient":
                img[y][x] = (int(255 * x / (size - 1)), 40, int(255 * (1 - x / (size - 1))))
            elif kind == "checker":
                img[y][x] = (0, 0, 0) if ((x // 4) + (y // 4)) % 2 == 0 else (255, 255, 255)
            else:
                raise DocumentError(f"unknown synthetic image kind {kind!r}")
    return img


def perturb(img: RgbGrid, brightness: int, noise: int, seed: int) -> RgbGrid:
    """A re-encoded, slightly brighter, noisy copy of the same picture."""
    rnd = random.Random(seed)
    return [[tuple(min(255, max(0, c + brightness + rnd.randint(-noise, noise))) for c in px) for px in row] for row in img]  # type: ignore[misc]


def to_gray(img: RgbGrid) -> Grid:
    return [[(299 * r + 587 * g + 114 * b) // 1000 for r, g, b in row] for row in img]


def area_resize(g: Grid, h: int, w: int) -> Grid:
    H, W = len(g), len(g[0])
    out = []
    for y in range(h):
        row = []
        for x in range(w):
            ys, ye = y * H // h, max(y * H // h + 1, (y + 1) * H // h)
            xs, xe = x * W // w, max(x * W // w + 1, (x + 1) * W // w)
            vals = [g[j][i] for j in range(ys, ye) for i in range(xs, xe)]
            row.append(sum(vals) // len(vals))
        out.append(row)
    return out


def average_hash(g: Grid) -> int:
    """64-bit aHash: shrink to 8x8, bit = pixel brighter than the mean. Robust to brightness shifts and re-encoding."""
    small = [v for row in area_resize(g, 8, 8) for v in row]
    mean = sum(small) / 64
    return sum(1 << i for i, v in enumerate(small) if v > mean)


def difference_hash(g: Grid) -> int:
    """64-bit dHash: shrink to 8x9, bit = left pixel darker than its right neighbour by a 3-level margin (captures gradients and edges; flat regions stay stable)."""
    small = area_resize(g, 8, 9)
    return sum(1 << (r * 8 + c) for r in range(8) for c in range(8) if small[r][c] + 3 < small[r][c + 1])


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def color_histogram(img: RgbGrid, bins: int = 4) -> list[float]:
    """Joint RGB histogram with bins^3 cells, L2-normalized so that dot product is cosine similarity."""
    h = [0.0] * bins ** 3
    for row in img:
        for r, g, b in row:
            h[(r * bins // 256) * bins * bins + (g * bins // 256) * bins + (b * bins // 256)] += 1
    return unit(h)


class ImageIndex:
    """Flat (exact, brute-force) index: fine up to roughly 1e5 images; use an ANN index such as HNSW beyond that."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[int, int, list[float]]] = {}

    def add(self, image_id: str, img: RgbGrid) -> None:
        gray = to_gray(img)
        self.rows[image_id] = (average_hash(gray), difference_hash(gray), color_histogram(img))

    def search(self, img: RgbGrid, k: int = 3, hash_weight: float = 0.5) -> list[dict[str, Any]]:
        gray = to_gray(img)
        a, d, hist = average_hash(gray), difference_hash(gray), color_histogram(img)
        out = []
        for image_id, (ra, rd, rh) in self.rows.items():
            dist = hamming(a, ra) + hamming(d, rd)
            cos = dot(hist, rh)
            out.append({"id": image_id, "score": round(hash_weight * (1 - dist / 128) + (1 - hash_weight) * cos, 3), "hash_distance": dist,
                        "color_cosine": round(cos, 3)})
        return sorted(out, key=lambda r: -r["score"])[:k]


# ---- Syllabus extension: nested and merged-cell tables ---------------------------------------------
class SpanCell(BaseModel):
    text: str
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    rowspan: int = Field(default=1, ge=1)
    colspan: int = Field(default=1, ge=1)


class CellRecord(BaseModel):
    row_path: tuple[str, ...]
    col_path: tuple[str, ...]
    value: Decimal


def expand_spans(cells: list[SpanCell], n_rows: int, n_cols: int) -> list[list[SpanCell]]:
    """Fills a dense grid where a spanning cell occupies every slot it covers. Overlaps, overflow and holes are extraction errors."""
    grid: list[list[SpanCell | None]] = [[None] * n_cols for _ in range(n_rows)]
    for c in cells:
        if c.row + c.rowspan > n_rows or c.col + c.colspan > n_cols:
            raise TableValidationError(f"cell {c.text!r} spills outside the {n_rows}x{n_cols} grid")
        for r in range(c.row, c.row + c.rowspan):
            for k in range(c.col, c.col + c.colspan):
                if grid[r][k] is not None:
                    raise TableValidationError(f"cells overlap at ({r}, {k})")
                grid[r][k] = c
    holes = [(r, k) for r in range(n_rows) for k in range(n_cols) if grid[r][k] is None]
    if holes:
        raise TableValidationError(f"grid has uncovered slots: {holes[:3]}")
    return grid  # type: ignore[return-value]


def _path(cells: list[SpanCell]) -> tuple[str, ...]:
    out: list[str] = []
    prev: SpanCell | None = None
    for c in cells:
        if c is not prev:  # a spanning cell appears once in the path, not once per slot it covers
            out.append(c.text)
        prev = c
    return tuple(out)


def span_table_to_records(cells: list[SpanCell], n_rows: int, n_cols: int, header_rows: int, label_cols: int) -> list[CellRecord]:
    """Multi-level headers become column paths (('FY2024', 'H1')), nested row labels become row paths (('EMEA', 'Cloud')).
    Total rows are checked against the column sums and then dropped from the records."""
    grid = expand_spans(cells, n_rows, n_cols)
    col_paths = {c: _path([grid[r][c] for r in range(header_rows)]) for c in range(label_cols, n_cols)}
    records: list[CellRecord] = []
    totals: dict[int, Decimal] = {}
    for r in range(header_rows, n_rows):
        row_path = _path([grid[r][c] for c in range(label_cols)])
        for c in range(label_cols, n_cols):
            value = parse_number(grid[r][c].text)
            if row_path[0].lower().startswith("total"):
                totals[c] = value
            else:
                records.append(CellRecord(row_path=row_path, col_path=col_paths[c], value=value))
    for c, total in totals.items():
        expected = sum((rec.value for rec in records if rec.col_path == col_paths[c]), Decimal(0))
        if total != expected:
            raise TableValidationError(f"column {'/'.join(col_paths[c])}: total {total} != sum {expected}")
    return records


def records_to_tree(records: list[CellRecord]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for rec in records:
        node = tree
        for key in rec.row_path:
            node = node.setdefault(key, {})
        node["/".join(rec.col_path)] = str(rec.value)
    return tree


def make_nested_table(bad_total: bool = False) -> tuple[list[SpanCell], int, int]:
    """Two header rows (FY spans two half-year columns), a region cell spanning two product rows, and a Total label spanning two columns."""
    c = SpanCell
    cells = [c(text="Region", row=0, col=0, rowspan=2), c(text="Product", row=0, col=1, rowspan=2), c(text="FY2023", row=0, col=2, colspan=2),
             c(text="FY2024", row=0, col=4, colspan=2)]
    cells += [c(text=t, row=1, col=2 + i) for i, t in enumerate(["H1", "H2", "H1", "H2"])]
    data = {("EMEA", "Cloud"): [10, 12, 14, 18], ("EMEA", "Services"): [5, 6, 7, 8], ("APAC", "Cloud"): [3, 4, 6, 9], ("APAC", "Services"): [2, 2, 3, 3]}
    for i, ((region, product), vals) in enumerate(data.items()):
        r = 2 + i
        if i % 2 == 0:
            cells.append(c(text=region, row=r, col=0, rowspan=2))
        cells.append(c(text=product, row=r, col=1))
        cells += [c(text=str(v), row=r, col=2 + k) for k, v in enumerate(vals)]
    totals = [sum(v[k] for v in data.values()) for k in range(4)]
    if bad_total:
        totals[3] += 1
    cells.append(c(text="Total", row=6, col=0, colspan=2))
    cells += [c(text=str(v), row=6, col=2 + k) for k, v in enumerate(totals)]
    return cells, 7, 6


# ---- Syllabus extension: chart reasoning from a pixel grid -------------------------------------------
BAR_GRAY = 90


class AxisTick(BaseModel):
    row: int  # pixel row of the tick mark
    value: float  # the number printed beside it (what OCR of the tick label returns)


class ChartTable(BaseModel):
    columns: list[str]
    rows: list[tuple[str, float]]


def render_bar_chart(values: list[float], height: int = 64, max_value: int = 50, bar_w: int = 6, gap: int = 4) -> tuple[Grid, list[AxisTick]]:
    """Synthetic chart: black y-axis and baseline, gray bars, tick marks at 0, max/2 and max. One pixel row per axis unit here."""
    left, base = 6, height - 6
    ppu = (base - 4) / max_value
    width = left + gap + len(values) * (bar_w + gap) + 4
    g = blank(height, width, 255)
    for y in range(4, base + 1):
        g[y][left] = 0  # y-axis
    for x in range(left, width - 2):
        g[base][x] = 0  # baseline
    ticks = []
    for v in (0, max_value // 2, max_value):
        row = base - round(v * ppu)
        for x in range(left - 3, left):
            g[row][x] = 0
        ticks.append(AxisTick(row=row, value=float(v)))
    for i, v in enumerate(values):
        x0 = left + gap + i * (bar_w + gap)
        for y in range(base - round(v * ppu), base):
            for x in range(x0, x0 + bar_w):
                g[y][x] = BAR_GRAY
    return g, ticks


def extract_bars(g: Grid, ticks: list[AxisTick], labels: list[str]) -> ChartTable:
    """Finds the baseline (row with most black pixels), calibrates the value axis from two ticks, groups gray columns into bars
    and converts each bar height to a value. Labels come from OCR of the category axis, left to right."""
    if len(ticks) < 2 or len({t.row for t in ticks}) < 2:
        raise TableValidationError("need two distinct axis ticks to calibrate the value axis")
    t0, t1 = min(ticks, key=lambda t: t.row), max(ticks, key=lambda t: t.row)  # t1 is lowest on the page (smallest value)
    units_per_px = (t0.value - t1.value) / (t1.row - t0.row)
    base = max(range(len(g)), key=lambda y: sum(1 for v in g[y] if v < 50))
    col_heights = [sum(1 for y in range(base) if BAR_GRAY - 10 <= g[y][x] <= BAR_GRAY + 10) for x in range(len(g[0]))]
    bars = _runs(col_heights, 0)
    if len(bars) != len(labels):
        raise TableValidationError(f"found {len(bars)} bars but {len(labels)} category labels")
    rows = []
    for (x0, x1), label in zip(bars, labels):
        h = sorted(col_heights[x0:x1])[(x1 - x0) // 2]  # median column height resists ragged edges
        rows.append((label, round(t1.value + h * units_per_px, 2)))
    return ChartTable(columns=["category", "value"], rows=rows)


CHART_PROMPT = ("Read the chart image. Return JSON {\"title\": str, \"unit\": str, \"series\": [{\"label\": str, \"value\": number}]}. "
                "Only report values you can read from bar heights or data labels; use null when a value is not legible.")


def reconcile(pixel: ChartTable, model_rows: list[tuple[str, float]], rel_tol: float = 0.1) -> list[str]:
    """Cross-checks a VLM's reading against pixel measurements and reports every disagreement above rel_tol."""
    got = dict(model_rows)
    issues = []
    for label, measured in pixel.rows:
        if label not in got:
            issues.append(f"{label}: missing from model answer")
        elif abs(got[label] - measured) > rel_tol * max(abs(measured), 1e-9):
            issues.append(f"{label}: model said {got[label]}, pixels say {measured}")
    return issues


def describe_trend(t: ChartTable) -> list[str]:
    """Deterministic facts a VLM answer can be checked against: extremes and the largest step change."""
    vals = t.rows
    hi, lo = max(vals, key=lambda r: r[1]), min(vals, key=lambda r: r[1])
    steps = [(b[0], b[1] - a[1], (b[1] - a[1]) / a[1] * 100 if a[1] else 0.0) for a, b in zip(vals, vals[1:])]
    big = max(steps, key=lambda s: abs(s[1]))
    return [f"highest: {hi[0]} ({hi[1]})", f"lowest: {lo[0]} ({lo[1]})", f"largest change: {big[0]} ({big[1]:+g}, {big[2]:+.0f} percent)"]


# ---- Syllabus extension: ingesting unstructured legacy PDFs ----------------------------------------
class RawLine(BaseModel):
    text: str
    x: float
    y: float


class RawPage(BaseModel):
    number: int
    width: float = 600
    height: float = 800
    lines: list[RawLine]


def _shape(text: str) -> str:
    return re.sub(r"\d+", "#", text.strip().lower())  # 'Page 3' and 'Page 4' share the shape 'page #'


def strip_headers_footers(pages: list[RawPage], band: float = 0.08, min_share: float = 0.6) -> tuple[list[RawPage], list[str]]:
    """Lines in the top or bottom band whose digit-normalized shape repeats on most pages are running headers and footers."""
    seen: Counter[str] = Counter()
    for p in pages:
        seen.update({_shape(l.text) for l in p.lines if l.y < p.height * band or l.y > p.height * (1 - band)})
    noise = {s for s, n in seen.items() if n >= max(2, min_share * len(pages))}
    cleaned = [p.model_copy(update={"lines": [l for l in p.lines if _shape(l.text) not in noise]}) for p in pages]
    return cleaned, sorted(noise)


def repair_hyphenation(lines: list[str], keep_compounds: frozenset[str] = frozenset()) -> str:
    """Joins 'inter-' + 'national' into 'international' when the next line starts lowercase. Real compounds listed in
    keep_compounds ('well-known') keep their hyphen."""
    out: list[str] = []
    for line in lines:
        if out and re.search(r"[A-Za-z]-$", out[-1]) and re.match(r"[a-z]", line):
            compound = (out[-1].split()[-1] + line.split()[0]).lower()
            out[-1] = out[-1] + line if compound in keep_compounds else out[-1][:-1] + line
        else:
            out.append(line)
    return "\n".join(out)


def reading_order(lines: list[RawLine], page_width: float) -> list[RawLine]:
    """Two-column detection from the x positions of line starts: a gap wider than 15 percent of the page with at least
    two lines on each side splits the page; the left column is read to the bottom before the right one."""
    xs = sorted({round(l.x) for l in lines})
    split = None
    for a, b in zip(xs, xs[1:]):
        left_n, right_n = sum(1 for l in lines if l.x <= a), sum(1 for l in lines if l.x >= b)
        if b - a > 0.15 * page_width and left_n >= 2 and right_n >= 2:
            split = (a + b) / 2
            break
    if split is None:
        return sorted(lines, key=lambda l: (l.y, l.x))
    left = sorted((l for l in lines if l.x < split), key=lambda l: l.y)
    right = sorted((l for l in lines if l.x >= split), key=lambda l: l.y)
    return left + right


def text_quality(text: str) -> float:
    """0..1 heuristic: share of ordinary characters minus a penalty for replacement glyphs (a broken text layer)."""
    if not text.strip():
        return 0.0
    ordinary = sum(ch.isalnum() or ch.isspace() or ch in ".,;:-()%$'\"/" for ch in text) / len(text)
    return max(0.0, ordinary - 3 * text.count("�") / len(text))


Extractor = Callable[[str, int], Awaitable[str]]


class ExtractorChain:
    """Tries extractors in order and keeps the first result whose quality clears the bar; otherwise the best one seen.
    Transient failures of any step are retried with backoff before falling through to the next extractor."""

    def __init__(self, steps: list[tuple[str, Extractor]], min_quality: float = 0.85) -> None:
        self.steps, self.min_quality = steps, min_quality

    async def extract(self, doc: str, page_no: int) -> tuple[str, str, float]:
        best: tuple[str, str, float] = ("", "none", 0.0)
        for name, fn in self.steps:
            try:
                text = await retry_async(lambda fn=fn: fn(doc, page_no), (OcrTransientError,), attempts=5, base=0.004)
            except OcrTransientError:
                log.warning("extractor failed", extra={"ctx": {"step": name, "page": page_no}})
                continue
            q = text_quality(text)
            if q >= self.min_quality:
                return text, name, round(q, 3)
            if q > best[2]:
                best = (text, name, q)
            log.info("extractor below quality bar, falling back", extra={"ctx": {"step": name, "page": page_no, "quality": round(q, 3)}})
        return best[0], best[1], round(best[2], 3)


class LegacyPdfAdapters:
    """Optional real-PDF adapters, imported lazily. The offline lab uses RawPage objects instead."""

    @staticmethod
    def pypdf_text(path: str, page_index: int) -> str:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise MissingDependencyError("pypdf is not installed: pip install pypdf") from exc
        return PdfReader(path).pages[page_index].extract_text() or ""

    @staticmethod
    def docling_markdown(path: str) -> str:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            raise MissingDependencyError("docling is not installed: pip install docling") from exc
        return DocumentConverter().convert(path).document.export_to_markdown()


def make_legacy_pages() -> list[RawPage]:
    def page(n: int, left: list[str], right: list[str]) -> RawPage:
        lines = [RawLine(text="ACME Annual Report - Confidential", x=40, y=20), RawLine(text=f"Page {n}", x=280, y=780)]
        lines += [RawLine(text=t, x=40, y=100 + 20 * i) for i, t in enumerate(left)]
        lines += [RawLine(text=t, x=330, y=100 + 20 * i) for i, t in enumerate(right)]
        return RawPage(number=n, lines=lines)

    return [page(1, ["Revenue grew in the inter-", "national segment during the", "year and the well-", "known cloud product led."], ["Costs fell as the", "company consolidated its", "data centers across the", "region."]),
            page(2, ["The board approved a new", "dividend policy after the", "audit committee review."], ["Risks include supply", "constraints and currency", "movements."]),
            page(3, ["Outlook remains positive."], ["Guidance will be updated."])]


# ---- Syllabus extension: aligning bounding boxes with text chunks ---------------------------------
class Box(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)


def intersection(a: Box, b: Box) -> float:
    return Box(x0=max(a.x0, b.x0), y0=max(a.y0, b.y0), x1=min(a.x1, b.x1), y1=min(a.y1, b.y1)).area


def iou(a: Box, b: Box) -> float:
    """Intersection over union: symmetric, penalizes both too-big and too-small predicted boxes."""
    inter = intersection(a, b)
    union = a.area + b.area - inter
    return inter / union if union else 0.0


def containment(inner: Box, outer: Box) -> float:
    """Share of `inner` that lies inside `outer`: the right test for assigning a word to a layout region."""
    return intersection(inner, outer) / inner.area if inner.area else 0.0


def word_box(w: Word, line_h: float = 12.0) -> Box:
    return Box(x0=w.x, y0=w.y, x1=w.x + w.w, y1=w.y + line_h)


def union_box(boxes: list[Box]) -> Box:
    return Box(x0=min(b.x0 for b in boxes), y0=min(b.y0 for b in boxes), x1=max(b.x1 for b in boxes), y1=max(b.y1 for b in boxes))


class Citation(BaseModel):
    chunk_id: str
    page: int
    bbox: Box
    text: str


def assign_words_to_regions(words: list[Word], regions: dict[str, Box], min_containment: float = 0.5) -> dict[str, list[Word]]:
    """Each word goes to the region that contains most of it (and at least min_containment); unassigned words are returned under ''."""
    out: dict[str, list[Word]] = {name: [] for name in regions} | {"": []}
    for w in words:
        wb = word_box(w)
        name, score = max(((n, containment(wb, r)) for n, r in regions.items()), key=lambda x: x[1], default=("", 0.0))
        out[name if score >= min_containment else ""].append(w)
    return out


def chunk_citation(chunk_id: str, page: int, words: list[Word]) -> Citation:
    ordered = sorted(words, key=lambda w: (w.y, w.x))
    return Citation(chunk_id=chunk_id, page=page, bbox=union_box([word_box(w) for w in ordered]), text=" ".join(w.text for w in ordered))


def locate_quote(quote: str, words: list[Word], page: int, chunk_id: str = "quote") -> Citation | None:
    """Maps a quoted span from an LLM answer back to pixels: find the token run in reading order, cite the union box."""
    ordered = sorted(words, key=lambda w: (round(w.y / 6), w.x))
    toks = quote.split()
    for i in range(len(ordered) - len(toks) + 1):
        if [w.text for w in ordered[i:i + len(toks)]] == toks:
            return chunk_citation(chunk_id, page, ordered[i:i + len(toks)])
    return None


# ---- Syllabus extension: managing multi-page visual context -------------------------------------------
class PageRef(BaseModel):
    number: int
    tokens: int
    relevance: float


def select_pages(pages: list[PageRef], budget: int, window: int = 1, neighbor_decay: float = 0.5) -> list[int]:
    """Greedy page selection under an image-token budget: take pages by relevance, then neighbours within `window` at a decayed
    priority (a table or paragraph often continues on the adjacent page). Result is in reading order."""
    by_no = {p.number: p for p in pages}
    priority: dict[int, float] = {p.number: p.relevance for p in pages}
    for p in pages:
        for d in range(1, window + 1):
            for n in (p.number - d, p.number + d):
                if n in by_no:
                    priority[n] = max(priority[n], p.relevance * neighbor_decay ** d)
    chosen: list[int] = []
    used = 0
    for n in sorted(priority, key=lambda k: -priority[k]):
        if priority[n] > 0 and used + by_no[n].tokens <= budget:
            chosen.append(n)
            used += by_no[n].tokens
    return sorted(chosen)


def _continuation_rows(pending: Element, lead: Element | None, page_height: float) -> list[list[str]] | None:
    if lead is None or lead.type is not ElementType.TABLE or not lead.rows or not pending.rows or lead.bbox[1] >= 0.25 * page_height:
        return None
    if lead.rows[0] == pending.rows[0]:
        return lead.rows[1:]  # repeated header row: drop the duplicate
    return lead.rows if len(lead.rows[0]) == len(pending.rows[0]) else None  # headerless continuation: same column count


def stitch_tables(pages: list[list[Element]], page_height: float = 800.0) -> list[Element]:
    """Merges a table that ends near the bottom of page n with one that starts near the top of page n+1 when the header repeats
    or the column count matches. The merged table can then be validated (totals, row widths) as a single table."""
    out: list[Element] = []
    pending: Element | None = None
    for els in pages:
        tables = [e for e in els if e.type is ElementType.TABLE]
        lead = next((e for e in els if e.type is not ElementType.TITLE), None)
        if pending is not None:
            rows = _continuation_rows(pending, lead, page_height)
            if rows is None:
                out.append(pending)
            else:
                merged = (pending.rows or []) + rows
                pending = pending.model_copy(update={"rows": merged, "text": "\n".join(" | ".join(r) for r in merged),
                                                     "bbox": (pending.bbox[0], pending.bbox[1], max(pending.bbox[2], tables[0].bbox[2]), tables[0].bbox[3]),
                                                     "confidence": min(pending.confidence, tables[0].confidence)})
                tables = [pending] + tables[1:]  # the merged table replaces the page's first table and may continue again
            pending = None
        for k, t in enumerate(tables):
            if k == len(tables) - 1 and t.bbox[3] > 0.75 * page_height:
                pending = t  # may continue on the next page
            else:
                out.append(t)
    if pending is not None:
        out.append(pending)
    return out


def make_table_page(n: int, rows: list[list[str]], y0: float, repeat_header: list[str] | None = None) -> Page:
    """A page holding only a table: rows are laid out from y0 downward, preceded by a repeated header row when given."""
    words: list[Word] = []
    for i, cells in enumerate(([repeat_header] if repeat_header else []) + rows):
        words += [Word(text=c, x=x, y=y0 + 20 * i, w=len(c) * 7, conf=0.97) for x, c in zip((40, 300, 420), cells)]
    return Page(number=n, words=words)


# ---- Syllabus extension: demo ------------------------------------------------------------------------
class ScriptedVlm:
    """Stand-in for a vision-language model reading a chart: one value is misread so reconcile() has something to catch."""

    def read_chart(self, labels: list[str], true_values: list[float]) -> list[tuple[str, float]]:
        return [(lab, round(v * (1.4 if i == 2 else 1.0), 1)) for i, (lab, v) in enumerate(zip(labels, true_values))]


def adapter_status() -> dict[str, str]:
    """Optional adapters: report 'installed', or the typed error (with install hint) raised when the package is missing."""
    status: dict[str, str] = {}
    probes: dict[str, Callable[[], object]] = {"pillow": lambda: grid_from_pillow("missing.png"),
                                               "pypdf": lambda: LegacyPdfAdapters.pypdf_text("missing.pdf", 0),
                                               "docling": lambda: LegacyPdfAdapters.docling_markdown("missing.pdf")}
    modules = {"pillow": "PIL", "pypdf": "pypdf", "docling": "docling"}
    for name, probe in probes.items():
        if importlib.util.find_spec(modules[name]) is not None:
            status[name] = "installed (not exercised offline)"
            continue
        try:
            probe()
        except MissingDependencyError as exc:
            status[name] = str(exc)
    return status


async def extension_demo() -> None:
    # 1. document structure
    profile = classify_document([PageSignals(page=1, text_chars=1800, image_coverage=0.05), PageSignals(page=2, text_chars=0, image_coverage=0.98),
                                 PageSignals(page=3, text_chars=300, image_coverage=0.95, garbled_ratio=0.95),
                                 PageSignals(page=4, text_chars=900, image_coverage=0.6)])
    demo_page = make_page(1)
    demo_page.words += (words_from_text("1.2 Revenue Drivers", 40, 80) + words_from_text("- Cloud grew strongly", 40, 140)
                        + words_from_text("- Services declined", 40, 160) + words_from_text("Figure 3 Revenue by segment", 40, 300))
    log.info("document structure", extra={"ctx": {"kind": profile.kind, "routes": profile.routes, "structure": detect_structure(demo_page).model_dump()}})

    # 2. OCR pipeline with preprocessing and a confidence gate
    clean = render_lines(["1204 3987", "5516"])
    for name, img in (("noisy", add_noise(clean, 35, 0.01, seed=1)), ("skewed_3deg_noisy", add_noise(rotate(clean, 3.0, PAPER), 35, 0.01, seed=2)),
                      ("destroyed", add_noise(clean, 120, 0.4, seed=3))):
        try:
            r = run_ocr_pipeline(img)
            log.info("ocr", extra={"ctx": {"case": name, "text": r.text, "min_conf": r.confidence, "skew_deg": r.skew_deg, "threshold": r.threshold, "status": r.status}})
        except LowConfidenceError as exc:
            log.info("ocr gated", extra={"ctx": {"case": name, "reason": str(exc)}})

    # 3. OCR-free page embeddings: patch grid + MaxSim straight from pixels
    pix = PixelPageIndex()
    counts = {pid: pix.add_page(pid, add_noise(render_lines([txt]), 35, 0.01, seed=i)) for i, (pid, txt) in enumerate(
        (("page_a", "1204 3987"), ("page_b", "8657 5516"), ("page_c", "2024 0888")))}
    log.info("pixel maxsim", extra={"ctx": {"patches_per_page": counts, "q_1204": [(p, round(s, 2)) for p, s in pix.search("1204", 3)],
                                            "q_5516": [(p, round(s, 2)) for p, s in pix.search("5516", 3)], "int8_128d_bytes": pix.index_bytes()}})

    # 4. vision-language bridge
    spec = BridgeSpec()
    feats, rows, cols = vision_features(clean, 14)
    merged = merge_patches(feats, rows, cols, 2)
    projector = LinearProjector(d_in=len(merged[0]), d_out=8)
    visual = [projector(v) for v in merged]
    seq = build_sequence(["Describe", "the", "image"], visual, ["."], context_limit=512)
    log.info("vision language bridge", extra={"ctx": {"toy": {"patches": len(feats), "tokens": len(visual), "projector_in": len(merged[0]), "projector_out": len(visual[0]),
                                                             "sequence_len": len(seq), "first_visual_token": [round(x, 3) for x in visual[0]]},
                                                     "real_scale": {"patches": spec.patches, "tokens": spec.tokens, "linear_params": spec.linear_params,
                                                                    "three_images_plus_40_text_tokens": 3 * spec.tokens + 40,
                                                                    "share_of_8192_context": round((3 * spec.tokens + 40) / 8192, 3)}}})

    # 5. prompting with images
    png = make_png(clean)
    info = validate_image(png, "image/png")
    am = anthropic_user_message([(png, "image/png")], "What numbers appear on this page?")
    om = openai_user_message([(png, "image/png")], "What numbers appear on this page?", detail="low")
    for label, data, mt in (("wrong media type", png, "image/bmp"), ("bytes are not a jpeg", png, "image/jpeg"), ("not an image", b"hello", "image/png")):
        try:
            validate_image(data, mt)
        except ImageValidationError as exc:
            log.info("image rejected", extra={"ctx": {"case": label, "reason": str(exc)}})
    try:
        validate_image(png, "image/png", max_bytes=100)
    except ImageValidationError as exc:
        log.info("image rejected", extra={"ctx": {"case": "too large", "reason": str(exc)}})
    log.info("image payloads", extra={"ctx": {"info": info.model_dump(), "anthropic_blocks": [b["type"] for b in am["content"]],
                                              "anthropic_media_type": am["content"][0]["source"]["media_type"],
                                              "openai_parts": [p["type"] for p in om["content"]], "openai_url_prefix": om["content"][1]["image_url"]["url"][:30],
                                              "tokens_1024x1024": {"anthropic_rule": anthropic_image_tokens(1024, 1024), "tiles_high": tile_image_tokens(1024, 1024),
                                                                   "tiles_low": tile_image_tokens(1024, 1024, "low")},
                                              "tokens_3000x2000": {"anthropic_rule": anthropic_image_tokens(3000, 2000), "tiles_high": tile_image_tokens(3000, 2000)}}})

    # 6. CLIP-style contrastive learning
    labels = ["cat", "dog", "truck", "boat"]
    texts = [text_embed(f"a photo of a {lab}") for lab in labels]
    images = [image_embed(lab, seed=i) for i, lab in enumerate(labels)]
    shuffled = texts[1:] + texts[:1]
    zs = zero_shot(images[1], labels)
    noisy_images = [image_embed(lab, seed=10 + i, noise=4.0) for i, lab in enumerate(labels)]
    before = info_nce(noisy_images, texts, 1 / 0.07)["loss"]
    trained = noisy_images
    for _ in range(30):
        trained = contrastive_step(trained, texts, 1 / 0.07, lr=2.0)
    log.info("clip contrastive", extra={"ctx": {"aligned_temp_0.07": info_nce(images, texts, 1 / 0.07), "aligned_temp_1": info_nce(images, texts, 1.0),
                                                "shuffled_pairs": info_nce(images, shuffled, 1 / 0.07), "zero_shot_dog": zs,
                                                "noisy_loss_before": before, "loss_after_30_steps": info_nce(trained, texts, 1 / 0.07)["loss"]}})

    # 7. image-to-image similarity search
    idx = ImageIndex()
    kinds = ("red_on_blue", "stripes", "gradient", "checker")
    for kind in kinds:
        idx.add(kind, make_rgb_image(kind))
    queries = {k: perturb(make_rgb_image(k), brightness=8, noise=6, seed=i) for i, k in enumerate(("red_on_blue", "gradient"))}
    log.info("image search", extra={"ctx": {k: idx.search(q, 2) for k, q in queries.items()}})

    # 8. nested, merged-cell table
    cells, n_rows, n_cols = make_nested_table()
    recs = span_table_to_records(cells, n_rows, n_cols, header_rows=2, label_cols=2)
    log.info("nested table", extra={"ctx": {"records": len(recs), "first": recs[0].model_dump(mode="json"), "tree_EMEA": records_to_tree(recs)["EMEA"]}})
    bad_cells, _, _ = make_nested_table(bad_total=True)
    overlap = [*cells, SpanCell(text="X", row=2, col=1)]
    for label, args in (("bad total", (bad_cells, n_rows, n_cols)), ("overlapping cells", (overlap, n_rows, n_cols))):
        try:
            span_table_to_records(*args, header_rows=2, label_cols=2)  # type: ignore[arg-type]
        except TableValidationError as exc:
            log.info("table rejected", extra={"ctx": {"case": label, "reason": str(exc)}})

    # 9. chart reasoning from pixels
    values = [12.0, 30.0, 22.0, 41.0, 35.0]
    chart_labels = ["Q1", "Q2", "Q3", "Q4", "Q5"]
    grid, ticks = render_bar_chart(values)
    table = extract_bars(grid, ticks, chart_labels)
    vlm_rows = ScriptedVlm().read_chart(chart_labels, values)
    log.info("chart", extra={"ctx": {"recovered": table.rows, "max_abs_error": round(max(abs(v - r[1]) for v, r in zip(values, table.rows)), 2),
                                     "vlm_disagreements": reconcile(table, vlm_rows), "facts": describe_trend(table)}})

    # 10. legacy PDF ingestion: header/footer stripping, reading order, hyphen repair, extractor fallback chain
    raw = make_legacy_pages()
    cleaned, noise = strip_headers_footers(raw)
    keep = frozenset({"well-known"})

    async def text_layer(doc: str, n: int) -> str:
        if n == 3:
            return "��\x02\x02��\x02�"  # a broken font encoding: the classic legacy-scan symptom
        return repair_hyphenation([l.text for l in reading_order(cleaned[n - 1].lines, cleaned[n - 1].width)], keep)

    async def ocr_text(doc: str, n: int) -> str:
        return " ".join(e.text for e in parse_page(await ocr_with_retry(chain_ocr, doc, n)))

    chain_ocr = FlakyOcr()
    chain = ExtractorChain([("text_layer", text_layer), ("ocr", ocr_text)])
    results = [await chain.extract("legacy.pdf", n) for n in (1, 3)]
    log.info("legacy pdf", extra={"ctx": {"stripped_shapes": noise, "page1_text": results[0][0], "page1_source": results[0][1],
                                          "page3_source": results[1][1], "page3_text": results[1][0][:40], "adapters": adapter_status()}})

    # 11. bounding boxes aligned with text chunks
    page1 = make_page(1)
    predicted = Box(x0=35, y0=195, x1=480, y1=280)
    regions = {"title": Box(x0=30, y0=20, x1=200, y1=50), "table": predicted}
    assigned = assign_words_to_regions(page1.words, regions)
    truth = union_box([word_box(w) for w in assigned["table"]])
    cite = chunk_citation("q4#table", 1, assigned["table"])
    quote = locate_quote("Cloud 1,200", page1.words, 1)
    log.info("bbox alignment", extra={"ctx": {"words_per_region": {k: len(v) for k, v in assigned.items()}, "region_iou": round(iou(predicted, truth), 3),
                                              "word_containment": round(containment(word_box(assigned["table"][0]), predicted), 2),
                                              "table_citation_bbox": cite.bbox.model_dump(), "quote_citation": quote.model_dump() if quote else None,
                                              "hallucinated_quote_found": locate_quote("Cloud 9,999", page1.words, 1) is not None}})

    # 12. multi-page visual context
    header = ["Segment", "2023", "2024"]
    p1 = make_table_page(1, [header, ["Cloud", "1,200", "(1,204)"], ["Services", "800", "300"]], y0=620)
    p2 = make_table_page(2, [["Support", "500", "100"], ["Total", "2,500", "(804)"]], y0=80, repeat_header=header)
    stitched = stitch_tables([parse_page(p1), parse_page(p2)])
    try:
        validate_totals(next(e for e in parse_page(p2) if e.type is ElementType.TABLE))
    except TableValidationError as exc:
        alone = str(exc)
    validate_totals(stitched[0])
    per_page = anthropic_image_tokens(1275, 1650)  # a Letter page rendered at 150 dpi
    idx_text = PageIndex()
    topics = {1: "policy handbook leave", 2: "board dividend audit", 3: "cloud revenue 2024 growth", 4: "cloud costs datacenter", 5: "risk currency supply",
              6: "policy handbook travel", 7: "leave vacation parental", 8: "appendix glossary terms"}
    for n, text in topics.items():
        idx_text.add_page(str(n), Page(number=n, words=[Word(text=t, x=10 * i, y=50, w=30, conf=0.9) for i, t in enumerate(text.split())]))
    refs = [PageRef(number=n, tokens=per_page, relevance=max(0.0, idx_text.maxsim("cloud revenue 2024", str(n)))) for n in topics]
    chosen = select_pages(refs, budget=3 * per_page + 100)
    log.info("multi-page context", extra={"ctx": {"stitched_tables": len(stitched), "stitched_rows": len(stitched[0].rows or []), "page2_alone_fails": alone,
                                                  "tokens_per_page": per_page, "budget": 3 * per_page + 100, "selected_pages": chosen,
                                                  "tokens_used": per_page * len(chosen)}})


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

    await extension_demo()


if __name__ == "__main__":
    asyncio.run(main())
