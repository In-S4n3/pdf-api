# Source: https://docs.astral.sh/uv/guides/integration/docker/
FROM python:3.12-slim-bookworm

# Install uv from official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ARG INSTALL_DEV=false

# Install system dependencies (PDF processing tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ghostscript \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    tesseract-ocr-fra \
    tesseract-ocr-ita \
    tesseract-ocr-jpn \
    tesseract-ocr-por \
    tesseract-ocr-spa \
    libreoffice-writer-nogui \
    libreoffice-calc-nogui \
    libreoffice-impress-nogui \
    unpaper \
    fonts-liberation \
    fonts-dejavu \
    libglib2.0-0 \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

# Fail the build if the installed Ghostscript is older than the bookworm-security floor that
# fixes CVE-2024-29510 (deb12u4) and the pdfwrite CVE-2025-59798 (deb12u8). Do NOT gate on
# `gs --version` — bookworm ships upstream 10.00.0 and backports fixes without bumping it.
RUN dpkg --compare-versions "$(dpkg-query -W -f='${Version}' ghostscript)" ge 10.0.0~dfsg-11+deb12u8 \
    || (echo "Ghostscript below the CVE-patched bookworm-security floor" && exit 1)

# Set working directory
WORKDIR /app

# Environment optimizations
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PORT=8080

# Copy dependency files first (cached layer)
COPY pyproject.toml uv.lock ./

# Install Python dependencies
RUN if [ "$INSTALL_DEV" = "true" ]; then \
        uv sync --frozen --no-install-project; \
    else \
        uv sync --frozen --no-install-project --no-dev; \
    fi

# Copy application code
COPY . /app

# Final sync (installs the project itself)
RUN if [ "$INSTALL_DEV" = "true" ]; then \
        uv sync --frozen; \
    else \
        uv sync --frozen --no-dev; \
    fi

# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Fail the build fast if opencv/pdf2docx native deps can't import (arch/version drift guard)
RUN python -c "import cv2, pdf2docx, docx, openpyxl"

# Expose port
EXPOSE 8080

# Run with uvicorn
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
