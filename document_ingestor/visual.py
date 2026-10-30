"""PDF/image text extraction, with OCR fallback and image cleanup."""

from __future__ import annotations

import io
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

from .errors import ExtractionError


IS_VERCEL = os.getenv("VERCEL") == "1"
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "10" if IS_VERCEL else "500"))
MAX_IMAGE_FRAMES = int(os.getenv("MAX_IMAGE_FRAMES", "10" if IS_VERCEL else "500"))
MAX_OCR_PIXELS = int(os.getenv("MAX_OCR_PIXELS", str(20_000_000 if IS_VERCEL else 40_000_000)))
OCR_LANG = os.getenv("TESSERACT_LANG", "eng")


class OCRUnavailable(ExtractionError):
    def __init__(self, message: str = "OCR is unavailable. Install pytesseract and the Tesseract OCR engine, then retry."):
        super().__init__(
            "ocr_unavailable",
            message,
        )


def extract_pdf(path: Path) -> dict[str, Any]:
    try:
        import fitz
    except ImportError as exc:
        raise ExtractionError("dependency_missing", "PDF support requires PyMuPDF (fitz).") from exc

    try:
        document = fitz.open(path)
    except Exception as exc:
        raise ExtractionError("corrupt_pdf", "The PDF is damaged or could not be opened.") from exc

    pages: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        if document.is_encrypted:
            raise ExtractionError("encrypted_pdf", "The PDF is password-protected and cannot be processed.")
        if document.page_count > MAX_PDF_PAGES:
            raise ExtractionError("page_limit_exceeded", f"The PDF has {document.page_count} pages; the limit is {MAX_PDF_PAGES}.")

        for page_index in range(document.page_count):
            digital_text = ""
            try:
                page = document.load_page(page_index)
                digital_text = _normalize_text(page.get_text("text", sort=True))
                if _has_meaningful_text(digital_text):
                    pages.append({"page": page_index + 1, "extraction": "digital_text", "text": digital_text})
                    continue

                scale = min(300 / 72, 5000 / max(page.rect.width, page.rect.height, 1))
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                image_bytes = pixmap.tobytes("png")
                text, confidence = ocr_image(image_bytes)
                pages.append({
                    "page": page_index + 1,
                    "extraction": "ocr",
                    "text": text or digital_text,
                    "ocr_confidence": confidence,
                })
                if not text:
                    warnings.append(f"Page {page_index + 1}: OCR did not find readable text.")
            except OCRUnavailable as exc:
                pages.append({"page": page_index + 1, "extraction": "digital_text" if digital_text else "ocr", "text": digital_text, "status": "error"})
                warnings.append(f"Page {page_index + 1}: {exc}")
            except Exception as exc:
                pages.append({"page": page_index + 1, "extraction": "digital_text" if digital_text else "unknown", "text": digital_text, "status": "error"})
                warnings.append(f"Page {page_index + 1}: page processing failed ({_short_error(exc)}).")
    finally:
        document.close()
    return {"page_count": len(pages), "pages": pages, "warnings": warnings}


def extract_image(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image, ImageSequence
    except ImportError as exc:
        raise ExtractionError("dependency_missing", "Image support requires Pillow.") from exc

    Image.MAX_IMAGE_PIXELS = MAX_OCR_PIXELS
    try:
        with Image.open(path) as probe:
            probe.verify()
        image = Image.open(path)
    except Exception as exc:
        raise ExtractionError("corrupt_image", "The image is damaged or could not be decoded.") from exc

    pages: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        frame_count = getattr(image, "n_frames", 1)
        if frame_count > MAX_IMAGE_FRAMES:
            raise ExtractionError("frame_limit_exceeded", f"The image has {frame_count} frames; the limit is {MAX_IMAGE_FRAMES}.")
        for frame_index, frame in enumerate(ImageSequence.Iterator(image)):
            try:
                buffer = io.BytesIO()
                frame.convert("RGB").save(buffer, format="PNG")
                text, confidence = ocr_image(buffer.getvalue())
                pages.append({
                    "page": frame_index + 1,
                    "extraction": "ocr",
                    "text": text,
                    "ocr_confidence": confidence,
                })
                if not text:
                    warnings.append(f"Frame {frame_index + 1}: OCR did not find readable text.")
            except OCRUnavailable:
                raise
            except Exception as exc:
                pages.append({"page": frame_index + 1, "extraction": "ocr", "text": "", "status": "error"})
                warnings.append(f"Frame {frame_index + 1}: OCR failed ({_short_error(exc)}).")
    finally:
        image.close()
    return {"page_count": len(pages), "pages": pages, "warnings": warnings}


def ocr_image(image_bytes: bytes) -> tuple[str, float | None]:
    try:
        import pytesseract
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        from pytesseract import Output
    except ImportError as exc:
        raise OCRUnavailable("The Python package pytesseract is missing. Install the project requirements, then restart the service.") from exc

    _ensure_tesseract(pytesseract)
    with Image.open(io.BytesIO(image_bytes)) as source:
        source.load()
        image = source.convert("L")
    if image.width * image.height > MAX_OCR_PIXELS:
        ratio = (MAX_OCR_PIXELS / (image.width * image.height)) ** 0.5
        image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))))
    if max(image.size) < 2800:
        ratio = min(2.0, 2800 / max(image.size))
        image = image.resize((int(image.width * ratio), int(image.height * ratio)))

    enhanced = ImageOps.autocontrast(image)
    enhanced = ImageEnhance.Contrast(enhanced).enhance(1.35).filter(
        ImageFilter.UnsharpMask(radius=1.2, percent=150, threshold=3)
    )
    threshold = _otsu_threshold(enhanced)
    thresholded = enhanced.point(lambda pixel: 255 if pixel > threshold else 0)

    candidates: list[tuple[str, float]] = []
    for candidate in (enhanced, thresholded):
        for page_mode in (3, 6):
            data = pytesseract.image_to_data(
                candidate,
                lang=OCR_LANG,
                config=f"--oem 3 --psm {page_mode}",
                output_type=Output.DICT,
            )
            words: list[str] = []
            confidences: list[float] = []
            for raw_word, raw_confidence in zip(data.get("text", []), data.get("conf", [])):
                word = raw_word.strip()
                if not word:
                    continue
                words.append(word)
                try:
                    confidence = float(raw_confidence)
                    if confidence >= 0:
                        confidences.append(confidence)
                except (TypeError, ValueError):
                    pass
            text = _normalize_text(" ".join(words))
            score = (sum(confidences) / len(confidences)) if confidences else 0.0
            score += min(len(text), 500) / 1000
            candidates.append((text, score))
    best_text, best_score = max(candidates, key=lambda item: item[1], default=("", 0.0))
    confidence = max(0.0, min(100.0, best_score - min(len(best_text), 500) / 1000))
    return best_text, round(confidence, 1) if best_text else None


@lru_cache(maxsize=1)
def _ensure_tesseract(pytesseract: Any) -> None:
    candidate = os.getenv("TESSERACT_CMD", "").strip()
    if candidate:
        resolved = shutil.which(candidate)
        if resolved:
            pytesseract.pytesseract.tesseract_cmd = resolved
        elif Path(candidate).is_file():
            pytesseract.pytesseract.tesseract_cmd = candidate
    else:
        candidates = []
        discovered = shutil.which("tesseract")
        if discovered:
            candidates.append(Path(discovered))
        for base in (os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"), os.getenv("LOCALAPPDATA")):
            if base:
                candidates.append(Path(base) / "Tesseract-OCR" / "tesseract.exe")
                candidates.append(Path(base) / "Programs" / "Tesseract-OCR" / "tesseract.exe")
        project_work = Path(__file__).resolve().parents[1] / "work"
        candidates.extend(sorted(project_work.glob("tesseract-runtime*/engine/tesseract.exe"), reverse=True))
        executable = next((path for path in candidates if path.is_file()), None)
        if executable is not None:
            pytesseract.pytesseract.tesseract_cmd = str(executable)
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        raise OCRUnavailable(
            "The Tesseract OCR engine was not found or could not start. Install the Tesseract engine, add it to PATH, or set TESSERACT_CMD to its executable, then restart the service."
        ) from exc


def ocr_status() -> dict[str, Any]:
    try:
        import pytesseract
    except ImportError:
        return {
            "available": False,
            "message": "The Python package pytesseract is missing. Install the project requirements, then restart the service.",
        }
    try:
        _ensure_tesseract(pytesseract)
        return {"available": True, "version": str(pytesseract.get_tesseract_version())}
    except OCRUnavailable as exc:
        return {"available": False, "message": str(exc)}


def _has_meaningful_text(text: str) -> bool:
    alphanumeric = sum(char.isalnum() for char in text)
    return alphanumeric >= 20 or len(text.split()) >= 4


def _normalize_text(text: str) -> str:
    return "\n".join(line.strip() for line in text.replace("\x00", "").splitlines() if line.strip())


def _otsu_threshold(image: Any) -> int:
    histogram = image.histogram()[:256]
    total = sum(histogram)
    weighted_total = sum(index * count for index, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_variance = -1.0
    threshold = 127
    for index, count in enumerate(histogram):
        background_weight += count
        if background_weight == 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break
        background_sum += index * count
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_total - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            threshold = index
    return threshold


def _short_error(exc: Exception) -> str:
    message = " ".join(str(exc).split()) or exc.__class__.__name__
    return message[:180]

