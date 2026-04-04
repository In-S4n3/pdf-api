# pdf-api

PDF processing API for [TudoPDF](https://tudopdf.app).

FastAPI microservice with PyMuPDF, pikepdf, Ghostscript, Tesseract OCR, and LibreOffice for server-side PDF processing.

## Quick Start

```bash
docker compose up --build
```

The API will be available at `http://localhost:8000`.

- Health check: `GET /health`
- API docs: `GET /docs`
- Echo test: `POST /echo` (multipart file upload)

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

## License

AGPL-3.0 -- see [LICENSE](LICENSE).
