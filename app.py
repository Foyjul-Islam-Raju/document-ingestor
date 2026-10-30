"""HTTP API for batch document extraction."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from document_ingestor.pipeline import process_file
from document_ingestor.spreadsheets import MAX_TABLE_CELLS, MAX_TABLE_COLUMNS, MAX_TABLE_ROWS
from document_ingestor.store import delete_document, get_document, initialize_store, save_documents, search_documents
from document_ingestor.visual import MAX_IMAGE_FRAMES, MAX_PDF_PAGES, ocr_status


IS_VERCEL = os.getenv("VERCEL") == "1"
DEFAULT_MAX_UPLOAD_BYTES = 3 * 1024 * 1024 if IS_VERCEL else 50 * 1024 * 1024
DEFAULT_MAX_BATCH_BYTES = 4 * 1024 * 1024 if IS_VERCEL else 200 * 1024 * 1024
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES)))
MAX_BATCH_BYTES = int(os.getenv("MAX_BATCH_BYTES", str(DEFAULT_MAX_BATCH_BYTES)))
if IS_VERCEL:
    # Vercel Function request bodies are capped at 4.5 MB, including multipart overhead.
    MAX_UPLOAD_BYTES = min(MAX_UPLOAD_BYTES, 3 * 1024 * 1024)
    MAX_BATCH_BYTES = min(MAX_BATCH_BYTES, 4 * 1024 * 1024)
MAX_FILES_PER_REQUEST = int(os.getenv("MAX_FILES_PER_REQUEST", "20"))
MAX_PARALLEL_EXTRACTIONS = max(1, int(os.getenv("MAX_PARALLEL_EXTRACTIONS", "1" if IS_VERCEL else "2")))
DEFAULT_TEMP_DIR = (
    Path(tempfile.gettempdir()) / "document-ingestor" / "tmp"
    if IS_VERCEL
    else Path(__file__).parent / "work" / "tmp"
)
TEMP_DIR = Path(os.getenv("INGEST_TEMP_DIR", str(DEFAULT_TEMP_DIR)))

app = FastAPI(
    title="Document Ingestion API",
    version="1.0.0",
    description=(
        "Extract text from PDFs and images with OCR, and structured rows from "
        "Excel workbooks and CSV files. Each uploaded file is processed independently."
    ),
)
_extraction_slots = asyncio.Semaphore(MAX_PARALLEL_EXTRACTIONS)


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
async def health() -> dict[str, str | int | bool]:
    return {
        "status": "ok",
        "is_vercel": IS_VERCEL,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_batch_bytes": MAX_BATCH_BYTES,
        "max_files_per_request": MAX_FILES_PER_REQUEST,
        "max_pdf_pages": MAX_PDF_PAGES,
        "max_image_frames": MAX_IMAGE_FRAMES,
        "max_table_cells": MAX_TABLE_CELLS,
        "max_table_rows": MAX_TABLE_ROWS,
        "max_table_columns": MAX_TABLE_COLUMNS,
        "storage_mode": "ephemeral" if IS_VERCEL else "local",
    }


@app.get("/health/ocr")
async def health_ocr() -> dict:
    return await asyncio.to_thread(ocr_status)


@app.on_event("startup")
async def startup() -> None:
    await asyncio.to_thread(initialize_store)


@app.get("/documents")
async def documents(
    q: str = Query(default="", max_length=200),
    file_type: str = Query(default="", max_length=12),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return await asyncio.to_thread(search_documents, q, file_type, limit, offset)


@app.get("/documents/{document_id}")
async def document_detail(document_id: str) -> dict:
    result = await asyncio.to_thread(get_document, document_id)
    if result is None:
        raise HTTPException(status_code=404, detail="This saved result was not found.")
    return result


@app.delete("/documents/{document_id}")
async def remove_document(document_id: str) -> dict[str, bool]:
    deleted = await asyncio.to_thread(delete_document, document_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="This saved result was not found.")
    return {"deleted": True}


@app.post("/ingest")
async def ingest(files: Annotated[list[UploadFile], File(description="Documents to extract")]):
    """Accept a batch and return one result for every submitted file."""
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one file.")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=413,
            detail=f"A request can contain at most {MAX_FILES_PER_REQUEST} files.",
        )

    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        temp_directory = tempfile.TemporaryDirectory(prefix="document-ingest-", dir=TEMP_DIR)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="Temporary upload storage is unavailable. Set INGEST_TEMP_DIR to a writable directory and restart.",
        ) from exc

    total_bytes = 0
    staged: list[tuple[Path, str, int]] = []
    try:
        with temp_directory as temp_dir:
            try:
                for index, upload in enumerate(files):
                    safe_name = _display_name(upload.filename or f"file-{index + 1}")
                    suffix = Path(safe_name).suffix.lower()
                    destination = Path(temp_dir) / f"upload-{index}{suffix}"
                    file_bytes = 0
                    with destination.open("wb") as out:
                        while chunk := await upload.read(1024 * 1024):
                            file_bytes += len(chunk)
                            total_bytes += len(chunk)
                            if file_bytes > MAX_UPLOAD_BYTES:
                                raise HTTPException(
                                    status_code=413,
                                    detail=f"{safe_name} exceeds the {MAX_UPLOAD_BYTES}-byte file limit.",
                                )
                            if total_bytes > MAX_BATCH_BYTES:
                                raise HTTPException(
                                    status_code=413,
                                    detail=f"The request exceeds the {MAX_BATCH_BYTES}-byte batch limit.",
                                )
                            out.write(chunk)
                    staged.append((destination, safe_name, file_bytes))

                async def extract_one(path: Path, filename: str) -> dict:
                    async with _extraction_slots:
                        return await asyncio.to_thread(process_file, path, filename)

                results = await asyncio.gather(
                    *(extract_one(path, name) for path, name, _ in staged)
                )
            finally:
                for upload in files:
                    await upload.close()
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="The upload could not be staged. Check that INGEST_TEMP_DIR is writable and has enough space.",
        ) from exc

    counts = {"success": 0, "partial": 0, "error": 0}
    for result, (_, _, file_size) in zip(results, staged):
        result["file_size_bytes"] = file_size
        counts[result["status"]] += 1
    history_saved = True
    try:
        await asyncio.to_thread(save_documents, results)
    except (OSError, sqlite3.Error):
        history_saved = False
    return {
        "files_received": len(results),
        "summary": counts,
        "results": results,
        "history_saved": history_saved,
    }


def _display_name(name: str) -> str:
    """Keep a safe, bounded display filename; never use it as a filesystem path."""
    cleaned = "".join(char for char in name if char >= " " and char not in "\x7f")
    cleaned = cleaned.replace("\\", "/").split("/")[-1]
    return cleaned[:255] or "unnamed-file"

