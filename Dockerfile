# Source: https://docs.astral.sh/uv/guides/integration/docker/
FROM python:3.12-slim-bookworm

# Install uv from official image (pinned; the digest is the multi-arch index)
COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /uvx /bin/

ARG INSTALL_DEV=false

# Install system dependencies (PDF processing tools)
# Carlito/Caladea are metric-compatible with Word's Calibri/Cambria; Droid Sans Fallback draws
# Chinese and Japanese (Ghostscript's own Recommends). tini is PID 1 (see ENTRYPOINT).
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
    fonts-crosextra-carlito \
    fonts-crosextra-caladea \
    fonts-droid-fallback \
    libglib2.0-0 \
    libxcb1 \
    tini \
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
# No package cache in the image: it was 375 MB in /root/.cache/uv
ENV UV_NO_CACHE=1
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

# The python image ships no .pyc and the app user cannot write them: precompile once here
RUN python -m compileall -q /usr/local/lib/python3.12 /app/app

# Run unprivileged; code stays root-owned. RUFF_NO_CACHE: CI's lint step cannot write /app/.ruff_cache
RUN useradd --create-home --no-log-init --uid 10001 app
USER app
ENV RUFF_NO_CACHE=true

# Fail the build fast if opencv/pdf2docx native deps can't import (arch/version drift guard)
RUN python -c "import cv2, pdf2docx, docx, openpyxl"

# Expose port
EXPOSE 8080

# tini as PID 1 forwards SIGTERM to uvicorn and reaps LibreOffice's leftover helpers
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run with uvicorn; exec makes it tini's direct child, ${PORT} stays Cloud Run's contract
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
