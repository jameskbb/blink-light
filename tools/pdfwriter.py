"""A tiny PDF writer: vector rectangles and base-14 text, stdlib only.

The reference card could have been rendered by a browser or by reportlab, but
either one turns "regenerate the printout" into "install a toolchain first",
and this repo's whole install story is one batch file. The PDF format's own
subset - rects, text runs, the standard fonts every reader already has - is
small enough to write directly, so the card stays regenerable anywhere Python
runs.

Coordinates here are top-left origin, in points, because every layout in
build_reference_card.py reads as a column of rows flowing down the page. The
flip to PDF's bottom-left origin happens at the last moment, in `_y`.
"""

from __future__ import annotations

import zlib


PAGE_WIDTH = 612.0
PAGE_HEIGHT = 792.0

HELVETICA = "F1"
HELVETICA_BOLD = "F2"
COURIER = "F3"
COURIER_BOLD = "F4"

_BASE_FONTS = {
    HELVETICA: "Helvetica",
    HELVETICA_BOLD: "Helvetica-Bold",
    COURIER: "Courier",
    COURIER_BOLD: "Courier-Bold",
}

# Adobe's published widths for the standard fonts, in 1/1000 em. Only the
# printable ASCII range is listed; anything outside it falls back to the space
# width, which is narrow enough that wrapping errs toward a short line.
_HELVETICA_WIDTHS = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191,
    "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278,
    ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556, "@": 1015,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722,
    "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722, "O": 778, "P": 667,
    "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667,
    "Y": 667, "Z": 611, "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556,
    "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556, "p": 556,
    "q": 556, "r": 333, "s": 500, "t": 278, "u": 556, "v": 500, "w": 722, "x": 500,
    "y": 500, "z": 500, "{": 334, "|": 260, "}": 334, "~": 584,
    "—": 1000, "–": 556, "…": 1000, "·": 278,
}
_HELVETICA_BOLD_WIDTHS = {
    " ": 278, "!": 333, '"': 474, "#": 556, "$": 556, "%": 889, "&": 722, "'": 238,
    "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278,
    ":": 333, ";": 333, "<": 584, "=": 584, ">": 584, "?": 611, "@": 975,
    "A": 722, "B": 722, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722,
    "I": 278, "J": 556, "K": 722, "L": 611, "M": 833, "N": 722, "O": 778, "P": 667,
    "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667,
    "Y": 667, "Z": 611, "[": 333, "\\": 278, "]": 333, "^": 584, "_": 556, "`": 333,
    "a": 556, "b": 611, "c": 556, "d": 611, "e": 556, "f": 333, "g": 611, "h": 611,
    "i": 278, "j": 278, "k": 556, "l": 278, "m": 889, "n": 611, "o": 611, "p": 611,
    "q": 611, "r": 389, "s": 556, "t": 333, "u": 611, "v": 556, "w": 778, "x": 556,
    "y": 556, "z": 500, "{": 389, "|": 280, "}": 389, "~": 584,
    "—": 1000, "–": 556, "…": 1000, "·": 278,
}
for _digit in "0123456789":
    _HELVETICA_WIDTHS[_digit] = 556
    _HELVETICA_BOLD_WIDTHS[_digit] = 556

_WIDTHS = {
    HELVETICA: _HELVETICA_WIDTHS,
    HELVETICA_BOLD: _HELVETICA_BOLD_WIDTHS,
}


def text_width(value: str, font: str, size: float, tracking: float = 0.0) -> float:
    """Width of a text run as the reader will lay it out."""
    if font in (COURIER, COURIER_BOLD):
        return len(value) * size * 0.6 + tracking * max(0, len(value) - 1)
    table = _WIDTHS[font]
    total = sum(table.get(character, table[" "]) for character in value)
    return total / 1000.0 * size + tracking * max(0, len(value) - 1)


def wrap(value: str, font: str, size: float, width: float, tracking: float = 0.0) -> list[str]:
    """Greedy word wrap. A word wider than the column gets its own line."""
    lines: list[str] = []
    current = ""
    for word in value.split(" "):
        candidate = f"{current} {word}".strip()
        if current and text_width(candidate, font, size, tracking) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def rgb(color: str) -> tuple[float, float, float]:
    text = color.lstrip("#")
    return tuple(int(text[index : index + 2], 16) / 255.0 for index in (0, 2, 4))


def _escape(value: str) -> bytes:
    encoded = value.encode("cp1252", "replace")
    for raw, escaped in ((b"\\", b"\\\\"), (b"(", b"\\("), (b")", b"\\)")):
        encoded = encoded.replace(raw, escaped)
    return encoded


class Page:
    """One page's content stream, written top-left down."""

    def __init__(self) -> None:
        self._ops: list[bytes] = []

    def _y(self, y: float) -> float:
        return PAGE_HEIGHT - y

    def rect(self, x: float, y: float, width: float, height: float, color: str) -> None:
        red, green, blue = rgb(color)
        self._ops.append(
            f"{red:.4f} {green:.4f} {blue:.4f} rg "
            f"{x:.2f} {self._y(y + height):.2f} {width:.2f} {height:.2f} re f".encode("ascii")
        )

    def line(self, x: float, y: float, width: float, color: str, thickness: float = 0.6) -> None:
        self.rect(x, y, width, thickness, color)

    def text(
        self,
        x: float,
        baseline: float,
        value: str,
        font: str = HELVETICA,
        size: float = 9.0,
        color: str = "#000000",
        tracking: float = 0.0,
    ) -> None:
        """Draw one run. `baseline` is the text baseline, measured from the top."""
        red, green, blue = rgb(color)
        self._ops.append(
            b"BT "
            + f"{red:.4f} {green:.4f} {blue:.4f} rg /{font} {size:.2f} Tf "
            f"{tracking:.3f} Tc {x:.2f} {self._y(baseline):.2f} Td (".encode("ascii")
            + _escape(value)
            + b") Tj ET"
        )

    def stream(self) -> bytes:
        return b"\n".join(self._ops)


class Document:
    def __init__(self) -> None:
        self.pages: list[Page] = []

    def page(self) -> Page:
        page = Page()
        self.pages.append(page)
        return page

    def render(self) -> bytes:
        """Serialise to PDF bytes.

        Deterministic on purpose - no creation date, no ids - so the committed
        card can be diffed and so a test can rebuild it and compare bytes.
        """
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_ids = {}
        for alias, base in _BASE_FONTS.items():
            font_ids[alias] = add(
                f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} "
                f"/Encoding /WinAnsiEncoding >>".encode("ascii")
            )
        resources = "<< /Font << " + " ".join(
            f"/{alias} {font_ids[alias]} 0 R" for alias in _BASE_FONTS
        ) + " >> >>"

        pages_id = add(b"")  # Reserved: the page objects need its number first.
        page_ids = []
        for page in self.pages:
            compressed = zlib.compress(page.stream())
            content_id = add(
                f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode("ascii")
                + compressed
                + b"\nendstream"
            )
            page_ids.append(
                add(
                    f"<< /Type /Page /Parent {pages_id} 0 R "
                    f"/MediaBox [0 0 {PAGE_WIDTH:.0f} {PAGE_HEIGHT:.0f}] "
                    f"/Resources {resources} /Contents {content_id} 0 R >>".encode("ascii")
                )
            )
        objects[pages_id - 1] = (
            f"<< /Type /Pages /Count {len(page_ids)} /Kids ["
            + " ".join(f"{page_id} 0 R" for page_id in page_ids)
            + "] >>"
        ).encode("ascii")
        catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for index, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        xref_at = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode("ascii")
        out += (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n"
        ).encode("ascii")
        return bytes(out)
