"""Local SQLite library for extracted document results."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_DATABASE = (
    Path(tempfile.gettempdir()) / "document-ingestor" / "documents.sqlite3"
    if os.getenv("VERCEL") == "1"
    else Path(__file__).resolve().parents[1] / "work" / "documents.sqlite3"
)
DATABASE_PATH = Path(os.getenv("INGEST_DB_PATH", str(DEFAULT_DATABASE)))


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_store() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('success', 'partial', 'error')),
                created_at TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL DEFAULT 0,
                page_count INTEGER NOT NULL DEFAULT 0,
                sheet_count INTEGER NOT NULL DEFAULT 0,
                warnings_json TEXT NOT NULL,
                error_message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS documents_created_at ON documents(created_at DESC)"
        )


def save_documents(results: list[dict[str, Any]]) -> list[str]:
    """Persist extracted JSON only; original uploaded files are never retained."""
    identifiers = [str(uuid4()) for _ in results]
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for document_id, result in zip(identifiers, results):
        stored_result = dict(result)
        stored_result["document_id"] = document_id
        result["document_id"] = document_id
        error = result.get("error") or {}
        rows.append(
            (
                document_id,
                str(result.get("filename") or "unnamed-file"),
                str(result.get("file_type") or "unknown"),
                str(result.get("status") or "error"),
                timestamp,
                int(result.get("file_size_bytes") or 0),
                int(result.get("page_count") or len(result.get("pages") or [])),
                int(result.get("sheet_count") or len(result.get("sheets") or [])),
                json.dumps(result.get("warnings") or [], ensure_ascii=False),
                str(error.get("message") or ""),
                json.dumps(stored_result, ensure_ascii=False, separators=(",", ":")),
            )
        )
    with _connect() as connection:
        connection.executemany(
            """
            INSERT INTO documents (
                id, filename, file_type, status, created_at, file_size_bytes,
                page_count, sheet_count, warnings_json, error_message, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return identifiers


def search_documents(
    query: str = "", file_type: str = "", limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if query.strip():
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(filename LIKE ? ESCAPE '\\' OR payload_json LIKE ? ESCAPE '\\')")
        parameters.extend((f"%{escaped}%", f"%{escaped}%"))
    if file_type:
        clauses.append("file_type = ?")
        parameters.append(file_type)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _connect() as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM documents" + where, parameters
        ).fetchone()[0]
        counts = {"success": 0, "partial": 0, "error": 0}
        for row in connection.execute(
            "SELECT status, COUNT(*) AS amount FROM documents" + where + " GROUP BY status",
            parameters,
        ):
            counts[row["status"]] = row["amount"]
        records = connection.execute(
            """
            SELECT id, filename, file_type, status, created_at, file_size_bytes,
                   page_count, sheet_count, warnings_json, error_message
            FROM documents
            """ + where + " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            [*parameters, max(1, min(limit, 200)), max(0, offset)],
        ).fetchall()
    documents = []
    for row in records:
        item = dict(row)
        item["warnings"] = json.loads(item.pop("warnings_json"))
        documents.append(item)
    return {"documents": documents, "total": total, "summary": counts, "limit": max(1, min(limit, 200)), "offset": max(0, offset)}


def get_document(document_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT payload_json, created_at, file_size_bytes FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
    if row is None:
        return None
    result = json.loads(row["payload_json"])
    result["created_at"] = row["created_at"]
    result["file_size_bytes"] = row["file_size_bytes"]
    return result


def delete_document(document_id: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        return cursor.rowcount > 0
