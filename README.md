# pdf-api

PDF processing API for [TudoPDF](https://tudopdf.app).

FastAPI microservice with PyMuPDF, pikepdf, Ghostscript, Tesseract OCR, and LibreOffice for server-side PDF processing.

## Quick Start

```bash
docker compose up --build
```

The API will be available at `http://localhost:8000`.

- Health check: `GET /health` / `GET /v2/health` — `{"status": "ok"}` only (no versions, no subprocesses)
- API docs in development: `GET /docs`
- Echo test: `POST /echo` (multipart file upload)
- V2 tools: `POST /v2/<tool>`

## Development

```bash
# Build and start with hot reload
docker compose up --build

# Run tests inside container
docker compose exec api pytest

# Format and lint
docker compose exec api ruff check app/ tests/
docker compose exec api ruff format app/ tests/
```

## V2 Contract

The new HTTP contract lives under `/v2/*`.

- V1 remains available for backwards compatibility.
- V2 keeps multipart uploads and binary responses but adds:
  - typed option validation
  - consistent structured error responses
  - `X-Request-ID` on every response

Frontend migration notes live in `docs/frontend-v2-migration.md`.

`POST /v2/fill-form` is deprecated because TudoPDF now fills forms entirely in
the browser. It remains available without a removal date and sends the standard
`Deprecation` and `Link` response headers so consumers can migrate safely.

V1, `/v2/echo` and `/v2/fill-form` stay mounted on purpose (decision of
2026-09-23): TudoPDF calls none of them, but the deploy smoke test posts to
`/v2/echo` to prove the API key fails closed.

## Limits a caller should know

- Uploads up to 20 MiB; an oversized `Content-Length` or a missing/wrong key is
  refused before the body is read. Responses stop at 30 MiB (`output_too_large`):
  Cloud Run drops a non-streamed HTTP/1 response above 32 MiB.
- OCR (the only tool with a long budget: ocrmypdf is killed at 200 s, every
  other tool at 45 s; Cloud Run's request timeout is 285 s): at most 28 pages
  that need OCR per job, and at most 420 megapixels as OCRmyPDF will render
  them — colour counts twice, a black-and-white (1-bit) scan half, and any page
  with text or a drawing renders at 400 dpi. It runs 4 pages at a time, one per
  vCPU, so each worker gets at most 105 Mpx: 28 grey A4 scans at 300 dpi, 24
  colour ones, or 12 A4 photos with captions. A page counts at least 15 Mpx
  (a Tesseract round), so a huge page cannot hide 27 small ones. The measurements and arithmetic
  are in `pdf_tools.py`. Too many pages answer `too_many_pages` with the number
  that fits; one page above 105 Mpx (an A1 colour scan at 300 dpi) answers
  `page_too_large`.
  Born-digital pages are skipped; a file with nothing to recognise answers
  `already_searchable`.
- Rendering (PDF to image) stays within 40 Mpx per page: 300 dpi up to A2, less
  for larger pages. Embedded images above 150 Mpx are refused (`image_too_large`).
- Protect passwords are at most 127 UTF-8 bytes — every PDF reader truncates there.
- Compress answers `422 compress_no_gain` when the result is not smaller.

## License

AGPL-3.0 -- see [LICENSE](LICENSE).
