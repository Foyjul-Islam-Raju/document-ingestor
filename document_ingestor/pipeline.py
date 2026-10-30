"""Per-file dispatch and shared result contracts for document extraction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ExtractionError
from .spreadsheets import extract_csv, extract_xls, extract_xlsx
from .visual import extract_image, extract_pdf


SUPPORTED = {
    ".pdf": ("pdf", extract_pdf),
    ".jpg": ("jpeg", extract_image),
    ".jpeg": ("jpeg", extract_image),
    ".png": ("png", extract_image),
    ".tif": ("tiff", extract_image),
    ".tiff": ("tiff", extract_image),
    ".xlsx": ("xlsx", extract_xlsx),
    ".xls": ("xls", extract_xls),
    ".csv": ("csv", extract_csv),
}


def process_file(path: Path, filename: str) -> dict[str, Any]:
    ext = Path(filename).suffix.lower()
    descriptor = SUPPORTED.get(ext)
    if descriptor is None:
        return _error_result(filename, "unknown", "unsupported_file_type", "Supported formats are PDF, JPG, PNG, TIFF, XLSX, XLS, and CSV.")

    file_type, extractor = descriptor
    result: dict[str, Any] = {
        "filename": filename,
        "file_type": file_type,
        "status": "success",
        "warnings": [],
    }
    try:
        extracted = extractor(path)
        result.update(extracted)
        if result.get("warnings") or any(page.get("status") == "error" for page in result.get("pages", [])):
            result["status"] = "partial"
        return result
    except ExtractionError as exc:
        return _error_result(filename, file_type, exc.code, str(exc))
    except Exception as exc:  # isolate malformed inputs and library-specific failures
        message = _safe_error_message(exc)
        return _error_result(filename, file_type, "corrupt_or_unreadable_file", message)


def _error_result(filename: str, file_type: str, code: str, message: str) -> dict[str, Any]:
    return {
        "filename": filename,
        "file_type": file_type,
        "status": "error",
        "warnings": [],
        "error": {"code": code, "message": message},
    }


def _safe_error_message(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    if not message:
        return "The file could not be read. It may be damaged or use an unsupported encoding."
    return message[:500]

