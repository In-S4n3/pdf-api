"""Programmatic fixtures for the pdf_to_xlsx (PDF -> .xlsx) tests.

Every fixture is built in-memory with PyMuPDF so the test suite is fully
self-contained (no opaque committed binaries). Run this module directly to
also drop the .pdf artifacts into this directory for manual inspection:

    uv run python tests/fixtures/gen_excel_fixtures.py

The KEY RISK for this tool is that `page.find_tables()` only detects a table
when the PDF carries real vector borders (default strategy "lines"). Every
table fixture therefore *draws* its grid lines, not just aligned text.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf


def _draw_table(page: pymupdf.Page, origin: tuple[float, float], rows: list[list[str]],
                col_w: float = 150.0, row_h: float = 40.0) -> None:
    """Draw a bordered grid with `rows` text, one string per cell.

    Draws every grid line as a separate stroke and inserts each cell's text so
    find_tables()'s "lines" strategy reconstructs the same rows on extract().
    """
    x0, y0 = origin
    nrows = len(rows)
    ncols = max(len(r) for r in rows)
    for r in range(nrows + 1):
        y = y0 + r * row_h
        page.draw_line((x0, y), (x0 + ncols * col_w, y), width=0.8, color=(0, 0, 0))
    for c in range(ncols + 1):
        x = x0 + c * col_w
        page.draw_line((x, y0), (x, y0 + nrows * row_h), width=0.8, color=(0, 0, 0))
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            page.insert_text((x0 + c * col_w + 5, y0 + r * row_h + 25), val, fontsize=11)


def tables_pdf() -> bytes:
    """A text PDF with one real bordered 3x3 table -> happy path."""
    doc = pymupdf.open()
    page = doc.new_page()
    _draw_table(page, (80, 100), [
        ["Produto", "Qtd", "Preco"],
        ["Caneta", "10", "2.50"],
        ["Caderno", "5", "4.00"],
    ])
    out = doc.tobytes()
    doc.close()
    return out


def three_tables_pdf() -> bytes:
    """Three bordered tables across two pages -> 3-sheet workbook, page order."""
    doc = pymupdf.open()
    p1 = doc.new_page()
    _draw_table(p1, (60, 80), [["A1", "A2"], ["A3", "A4"]])
    _draw_table(p1, (60, 320), [["B1", "B2"], ["B3", "B4"]])
    p2 = doc.new_page()
    _draw_table(p2, (60, 80), [["C1", "C2"], ["C3", "C4"]])
    out = doc.tobytes()
    doc.close()
    return out


def prose_pdf() -> bytes:
    """Text-only PDF, no vector lines -> no tables detected (422)."""
    doc = pymupdf.open()
    page = doc.new_page()
    lines = [
        "Este e um documento de texto corrido, sem quaisquer tabelas.",
        "Contem varios paragrafos com bastante texto selecionavel para",
        "garantir que nao e confundido com um PDF digitalizado. A",
        "ferramenta deve responder no_tables_detected para este ficheiro.",
        "Mais uma linha de prosa para aumentar a densidade de caracteres.",
    ]
    for i, ln in enumerate(lines):
        page.insert_text((72, 100 + i * 24), ln, fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


def sparse_table_pdf() -> bytes:
    """One tiny bordered 2x2 table of short numeric cells.

    Regression guard: total selectable text (<10 chars) is below word's scanned
    gate, but a table IS detected so it must return 200, never scanned_pdf.
    """
    doc = pymupdf.open()
    page = doc.new_page()
    _draw_table(page, (100, 120), [["1", "2"], ["3", "4"]], col_w=60, row_h=30)
    out = doc.tobytes()
    doc.close()
    return out


def injection_table_pdf() -> bytes:
    """A bordered table whose cells carry formula/error injection payloads."""
    doc = pymupdf.open()
    page = doc.new_page()
    _draw_table(page, (60, 120), [
        ["=1+1", "#REF!"],
        ["normal", "42"],
    ], col_w=140, row_h=36)
    out = doc.tobytes()
    doc.close()
    return out


def blank_pdf(pages: int = 1) -> bytes:
    """Image-less blank pages -> zero text, zero tables -> scanned_pdf (422)."""
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    out = doc.tobytes()
    doc.close()
    return out


def complex_paths_pdf(n_paths: int = 30) -> bytes:
    """A page with many separate vector strokes -> high get_cdrawings() count.

    Used with a lowered MAX_PATHS_PER_PAGE to exercise the complexity guard.
    Strokes are disjoint so they never form a detectable table.
    """
    doc = pymupdf.open()
    page = doc.new_page()
    for i in range(n_paths):
        y = 50 + i * 4
        page.draw_line((50, y), (60, y), width=0.5, color=(0, 0, 0))
    out = doc.tobytes()
    doc.close()
    return out


def main() -> None:
    here = Path(__file__).parent
    (here / "tables.pdf").write_bytes(tables_pdf())
    (here / "prose.pdf").write_bytes(prose_pdf())
    (here / "sparse-table.pdf").write_bytes(sparse_table_pdf())
    print(f"Wrote tables.pdf, prose.pdf, sparse-table.pdf to {here}")


if __name__ == "__main__":
    main()
