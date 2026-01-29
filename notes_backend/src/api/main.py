from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


def _parse_csv_env(value: Optional[str]) -> List[str]:
    """Parse comma-separated env var values into a list, stripping whitespace."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _get_db_path() -> str:
    """
    Resolve the SQLite database path.

    Prefers SQLITE_DB env var if present (provided by the database container).
    Otherwise falls back to the known workspace location used by the SQLite container.
    """
    env_path = os.getenv("SQLITE_DB")
    if env_path:
        return env_path

    # Fallback path (works in this monorepo workspace layout)
    # NOTE: We avoid hard-coding config when possible, but provide a safe fallback
    # so the app works even if SQLITE_DB isn't wired into the backend container env.
    return "/home/kavia/workspace/code-generation/simple-notes-app-207251-207262/database/myapp.db"


def _get_connection() -> sqlite3.Connection:
    """Create a SQLite connection with row dict access."""
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema() -> None:
    """Ensure the notes table exists (id/title/content/created_at/updated_at)."""
    with _get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              content TEXT NOT NULL,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_updated_at ON notes(updated_at);")
        conn.commit()


class NoteBase(BaseModel):
    """Common fields for notes."""

    title: str = Field(..., min_length=1, max_length=200, description="Note title")
    content: str = Field(..., min_length=1, description="Note content/body")


class NoteCreate(NoteBase):
    """Payload for creating a note."""


class NoteUpdate(BaseModel):
    """Payload for updating a note (partial updates supported)."""

    title: Optional[str] = Field(None, min_length=1, max_length=200, description="Updated note title")
    content: Optional[str] = Field(None, min_length=1, description="Updated note content/body")


class Note(NoteBase):
    """A persisted note."""

    id: int = Field(..., description="Unique note ID")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")


openapi_tags = [
    {"name": "Health", "description": "Service health endpoints."},
    {"name": "Notes", "description": "CRUD operations for notes."},
]

app = FastAPI(
    title="Simple Notes API",
    description="Backend API for a simple notes app (title + content) backed by SQLite.",
    version="1.0.0",
    openapi_tags=openapi_tags,
)

# CORS: allow the React frontend origins (from env); safe fallback is "*".
allowed_origins = _parse_csv_env(os.getenv("ALLOWED_ORIGINS")) or ["*"]
allowed_headers = _parse_csv_env(os.getenv("ALLOWED_HEADERS")) or ["*"]
allowed_methods = _parse_csv_env(os.getenv("ALLOWED_METHODS")) or ["*"]
cors_max_age = int(os.getenv("CORS_MAX_AGE", "600"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=allowed_methods,
    allow_headers=allowed_headers,
    max_age=cors_max_age,
)


@app.on_event("startup")
def _startup() -> None:
    """Initialize database schema on service startup."""
    _ensure_schema()


@app.get("/", tags=["Health"], summary="Health check")
# PUBLIC_INTERFACE
def health_check() -> dict[str, str]:
    """Health check endpoint.

    Returns:
        JSON message indicating the service is running.
    """
    return {"message": "Healthy"}


def _row_to_note(row: sqlite3.Row) -> Note:
    """Convert sqlite row -> Note model."""
    return Note(
        id=int(row["id"]),
        title=row["title"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
        updated_at=datetime.fromisoformat(row["updated_at"]) if isinstance(row["updated_at"], str) else row["updated_at"],
    )


@app.get(
    "/notes",
    response_model=List[Note],
    tags=["Notes"],
    summary="List notes",
    description="List notes ordered by most recently updated first.",
)
# PUBLIC_INTERFACE
def list_notes(
    limit: int = Query(100, ge=1, le=500, description="Maximum number of notes to return"),
    offset: int = Query(0, ge=0, description="Number of notes to skip"),
) -> List[Note]:
    """List notes.

    Args:
        limit: Max number of notes to return (1..500)
        offset: Offset for pagination

    Returns:
        A list of notes ordered by updated_at desc.
    """
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, title, content, created_at, updated_at
            FROM notes
            ORDER BY datetime(updated_at) DESC, id DESC
            LIMIT ? OFFSET ?;
            """,
            (limit, offset),
        ).fetchall()
    return [_row_to_note(r) for r in rows]


@app.get(
    "/notes/{note_id}",
    response_model=Note,
    tags=["Notes"],
    summary="Get a note",
    description="Get a single note by its ID.",
)
# PUBLIC_INTERFACE
def get_note(note_id: int) -> Note:
    """Get a note by ID.

    Args:
        note_id: Note ID

    Returns:
        The note.

    Raises:
        HTTPException: 404 if note does not exist.
    """
    with _get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, title, content, created_at, updated_at
            FROM notes
            WHERE id = ?;
            """,
            (note_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return _row_to_note(row)


@app.post(
    "/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    tags=["Notes"],
    summary="Create a note",
    description="Create a new note with title and content.",
)
# PUBLIC_INTERFACE
def create_note(payload: NoteCreate) -> Note:
    """Create a note.

    Args:
        payload: NoteCreate payload (title, content)

    Returns:
        The newly created note.
    """
    with _get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO notes (title, content)
            VALUES (?, ?);
            """,
            (payload.title, payload.content),
        )
        note_id = int(cur.lastrowid)
        conn.commit()

        row = conn.execute(
            """
            SELECT id, title, content, created_at, updated_at
            FROM notes
            WHERE id = ?;
            """,
            (note_id,),
        ).fetchone()

    # row should exist after insert
    return _row_to_note(row)


@app.put(
    "/notes/{note_id}",
    response_model=Note,
    tags=["Notes"],
    summary="Update a note",
    description="Update an existing note (title/content). Fields omitted are left unchanged.",
)
# PUBLIC_INTERFACE
def update_note(note_id: int, payload: NoteUpdate) -> Note:
    """Update a note by ID.

    Args:
        note_id: Note ID to update
        payload: Partial update payload. At least one of title/content should be provided.

    Returns:
        Updated note.

    Raises:
        HTTPException: 400 if no fields provided; 404 if note not found.
    """
    if payload.title is None and payload.content is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of 'title' or 'content' must be provided",
        )

    with _get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM notes WHERE id = ?;",
            (note_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")

        conn.execute(
            """
            UPDATE notes
            SET
              title = COALESCE(?, title),
              content = COALESCE(?, content),
              updated_at = CURRENT_TIMESTAMP
            WHERE id = ?;
            """,
            (payload.title, payload.content, note_id),
        )
        conn.commit()

        row = conn.execute(
            """
            SELECT id, title, content, created_at, updated_at
            FROM notes
            WHERE id = ?;
            """,
            (note_id,),
        ).fetchone()

    return _row_to_note(row)


@app.delete(
    "/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Notes"],
    summary="Delete a note",
    description="Delete a note by its ID.",
)
# PUBLIC_INTERFACE
def delete_note(note_id: int) -> Response:
    """Delete a note.

    Args:
        note_id: Note ID to delete

    Returns:
        204 No Content on success.

    Raises:
        HTTPException: 404 if note not found.
    """
    with _get_connection() as conn:
        cur = conn.execute("DELETE FROM notes WHERE id = ?;", (note_id,))
        conn.commit()

    if cur.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")

    return Response(status_code=status.HTTP_204_NO_CONTENT)
