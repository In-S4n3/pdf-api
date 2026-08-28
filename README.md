# pdf-api

PDF processing API for [TudoPDF](https://tudopdf.app).

FastAPI microservice with PyMuPDF, pikepdf, Ghostscript, Tesseract OCR, and LibreOffice for server-side PDF processing.

## Quick Start

```bash
docker compose up --build
```

The API will be available at `http://localhost:8000`.

- Health check: `GET /health`
- V2 health check: `GET /v2/health`
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

## License

AGPL-3.0 -- see [LICENSE](LICENSE).
