"""Memory-bounded subprocess entrypoint for Tier-1 pikepdf repair.

Run as: python -m app.services._repair_worker <in_path> <out_path> <meta_path>

Performs the pikepdf structure-repair + verdict classification that would
otherwise decompress an untrusted PDF's streams IN the API worker. The parent
(`repair_pdf`) spawns this under an RLIMIT_AS memory cap + a wall-clock timeout,
so a decompression bomb expands HERE — in a killable child — and never OOMs the
main worker. Writes a JSON envelope to <meta_path>:
    {"outcome": "ok", "headers": {..X-Repair-*..}}    # + repaired PDF bytes to <out_path>
    {"outcome": "escalate", "baseline": <int|null>}   # caller runs Ghostscript Tier-2
    {"outcome": "error", "status": 400, "code": "password_protected_pdf", "message": "..."}
If the child is killed (bomb) it writes no/partial meta -> the parent maps the kill.
"""
from __future__ import annotations

import io
import json
import sys

import pikepdf

# pdf_tools.PASSWORD_PROTECTED_MESSAGE, copied: this child imports pikepdf only
# (it runs under a memory cap). test_encrypted_pdf_steers_to_unlock keeps them equal.
_PW_MESSAGE = (
    "Este PDF está protegido por palavra-passe. "
    "Desbloqueie-o primeiro com a ferramenta Desbloquear PDF."
)


def _has_syntax_issues(content: bytes) -> bool:
    """True if the INPUT already has detectable syntax problems (drives already-healthy)."""
    try:
        pdf = pikepdf.open(io.BytesIO(content))
    except pikepdf.PdfError:
        return True
    try:
        return len(pdf.check_pdf_syntax()) > 0
    finally:
        pdf.close()


# qpdf warnings that mean content was lost, not just the xref rebuilt: an
# object read as null or string, a stream that no longer decodes.
_CONTENT_DAMAGE = ("treating", "unknown token", "decoding stream", "unexpected")


def _mupdf_page_count(content: bytes) -> int:
    """Pages the document declares, as MuPDF reads it (0 when unreadable).

    qpdf rebuilds a truncated file's page tree from what survives, so a
    60-page file cut in half read as 30 of 30 — "all pages recovered".
    """
    try:
        import pymupdf

        with pymupdf.open(stream=content, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return 0


def _classify(content: bytes) -> tuple[dict, bytes | None]:
    """Run Tier-1 repair + verdict. Returns (meta_dict, output_bytes_or_None)."""
    declared = _mupdf_page_count(content)
    try:
        pdf = pikepdf.open(io.BytesIO(content))
    except pikepdf.PasswordError:  # SIBLING of PdfError — own handler, FIRST
        return {"outcome": "error", "status": 400, "code": "password_protected_pdf",
                "message": _PW_MESSAGE}, None
    except pikepdf.PdfError:
        return {"outcome": "escalate", "baseline": declared or None}, None  # -> Tier 2

    try:
        try:
            # pages recoverable AT OPEN TIME (post-recovery), against what the file declares
            m = max(len(pdf.pages), declared)
        except Exception:
            return {"outcome": "escalate", "baseline": declared or None}, None
        try:
            buf = io.BytesIO()
            pdf.save(buf)  # rebuild xref/trailer, non-incremental
            out_bytes = buf.getvalue()
        except pikepdf.PdfError:
            return {"outcome": "escalate", "baseline": m}, None  # save failed -> Tier 2
        content_damaged = any(
            marker in warning.lower()
            for warning in pdf.get_warnings()
            for marker in _CONTENT_DAMAGE
        )
    finally:
        pdf.close()

    input_dirty = _has_syntax_issues(content)
    reopened = pikepdf.open(io.BytesIO(out_bytes))
    try:
        output_dirty = len(reopened.check_pdf_syntax()) > 0
        n = len(reopened.pages)
    finally:
        reopened.close()

    if output_dirty or n == 0:  # residual damage save() couldn't fix -> deeper repair
        return {"outcome": "escalate", "baseline": m}, None

    if n < m:
        status = "partial"          # real page loss ONLY
    elif not input_dirty:
        status = "already-healthy"  # input was already clean
    else:
        status = "repaired"
    return {"outcome": "ok", "headers": {
        "X-Repair-Status": status,
        "X-Repair-Method": "pikepdf",
        "X-Repair-Pages": f"{n}/{m}",
        # Structure rebuilt, but some objects came back as null (e.g. a font):
        # pages may render with missing text. The status alone said "repaired".
        "X-Repair-Warnings": "true" if content_damaged else "false",
    }}, out_bytes


def main() -> None:
    in_path, out_path, meta_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(in_path, "rb") as fh:
        content = fh.read()
    meta, out_bytes = _classify(content)
    if out_bytes is not None:  # write the PDF BEFORE meta — meta is the completion signal
        with open(out_path, "wb") as fh:
            fh.write(out_bytes)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)


if __name__ == "__main__":
    main()
