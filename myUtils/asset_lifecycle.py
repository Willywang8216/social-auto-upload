"""Shared, conservative reference guards for media and account deletion.

Deletion is the one operation a retry cannot undo. These helpers answer a
single question before any bytes or rows are removed:

    "does any unresolved or uncertain work still need this?"

They never guess. A table or column absent from an older database, or a payload
that cannot be decoded, is treated as *still in use* so the caller refuses the
delete instead of silently destroying a live dependency.

The module is free of Flask and of provider clients so both the web process and
the worker can import it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

#: Target states that may still attach the referenced media. Terminal states
#: (succeeded / cancelled) are not blockers here; a separate delivery-evidence
#: check covers the "maybe already sent" case.
LIVE_TARGET_STATUSES: tuple[str, ...] = (
    "pending", "retrying", "running", "blocked", "awaiting_delivery", "needs_review",
)

#: Job states that can still (re)open work for their targets.
LIVE_JOB_STATUSES: tuple[str, ...] = (
    "pending", "running", "awaiting_delivery", "preparing",
)

#: Account namespaces. The two tables number independently, so an id must be
#: resolved in exactly one of them and never by "whichever matches first".
NAMESPACE_LEGACY = "legacy"      # user_info
NAMESPACE_STRUCTURED = "structured"  # accounts
NAMESPACES = (NAMESPACE_LEGACY, NAMESPACE_STRUCTURED)


class AssetInUse(ValueError):
    """Raised when unresolved work still references the asset being deleted."""


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _artifact_filenames(payload) -> set[str]:
    """Filenames referenced by a job payload's artifacts.

    Raises ``ValueError`` on a malformed payload so the caller reports an
    *unknown* reference rather than "no references".
    """
    if not isinstance(payload, dict):
        return set()
    names: set[str] = set()
    artifacts = payload.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise ValueError("payload artifacts must be a list")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("payload artifact must be an object")
        for key in ("local_path", "public_url", "remote_path", "path"):
            value = str(artifact.get(key) or "").strip()
            if value:
                names.add(Path(value).name)
    return names


def file_references(
    conn: sqlite3.Connection,
    *,
    file_record_id: int | None = None,
    file_path: str | None = None,
    filename: str | None = None,
) -> dict[str, list]:
    """Return every live reference to a file, grouped by source.

    Raises :class:`AssetInUse` when a reference cannot be evaluated, because
    "unreadable" is not evidence that the file is unused.
    """
    names: set[str] = set()
    if filename:
        names.add(Path(filename).name)
    if file_path:
        names.add(Path(file_path).name)

    references: dict[str, list] = {}

    # 1. Unresolved publish targets whose payload artifacts or file_ref name
    #    this file. Payloads are decoded rather than LIKE-matched so an
    #    unrelated numeric id cannot produce a false positive.
    if _has_table(conn, "publish_job_targets") and _has_table(conn, "publish_jobs"):
        placeholders = ", ".join("?" for _ in LIVE_TARGET_STATUSES)
        rows = conn.execute(
            f"SELECT t.id AS target_id, t.file_ref AS file_ref, j.payload_json AS payload_json "
            f"FROM publish_job_targets t JOIN publish_jobs j ON j.id = t.job_id "
            f"WHERE t.status IN ({placeholders})",
            LIVE_TARGET_STATUSES,
        ).fetchall()
        blockers: list[int] = []
        for row in rows:
            payload_names: set[str] = set()
            raw = row["payload_json"]
            if raw:
                try:
                    payload_names = _artifact_filenames(json.loads(raw))
                except (TypeError, ValueError) as exc:
                    raise AssetInUse(
                        "A queued job's media references could not be decoded; "
                        "refusing deletion until they are readable"
                    ) from exc
            file_ref = str(row["file_ref"] or "").strip()
            if file_ref and "/" not in file_ref and ":" not in file_ref:
                payload_names.add(Path(file_ref).name)
            if payload_names & names:
                blockers.append(int(row["target_id"]))
        if blockers:
            references["publish_targets"] = sorted(blockers)

    # 2. Campaign artifacts pointing at this exact file record.
    if file_record_id is not None and _has_table(conn, "campaign_artifacts"):
        if "source_file_record_id" in _columns(conn, "campaign_artifacts"):
            ids = [
                int(row[0]) for row in conn.execute(
                    "SELECT id FROM campaign_artifacts WHERE source_file_record_id = ?",
                    (int(file_record_id),),
                )
            ]
            if ids:
                references["campaign_artifacts"] = sorted(ids)

    # 3. Media-library groups that include this file record.
    if file_record_id is not None and _has_table(conn, "media_group_items"):
        if "file_record_id" in _columns(conn, "media_group_items"):
            ids = [
                int(row[0]) for row in conn.execute(
                    "SELECT id FROM media_group_items WHERE file_record_id = ?",
                    (int(file_record_id),),
                )
            ]
            if ids:
                references["media_group_items"] = sorted(ids)

    # 4. Media assets (the newer library) linked to this file record.
    if file_record_id is not None and _has_table(conn, "media_assets"):
        if "file_record_id" in _columns(conn, "media_assets"):
            ids = [
                int(row[0]) for row in conn.execute(
                    "SELECT id FROM media_assets WHERE file_record_id = ?",
                    (int(file_record_id),),
                )
            ]
            if ids:
                references["media_assets"] = sorted(ids)

    return references


def assert_file_deletable(
    conn: sqlite3.Connection,
    *,
    file_record_id: int | None = None,
    file_path: str | None = None,
    filename: str | None = None,
) -> None:
    """Raise :class:`AssetInUse` if unresolved work still needs this file."""
    references = file_references(
        conn,
        file_record_id=file_record_id,
        file_path=file_path,
        filename=filename,
    )
    if references:
        summary = ", ".join(f"{name}={len(ids)}" for name, ids in sorted(references.items()))
        raise AssetInUse(
            "This media is still referenced by unresolved work "
            f"({summary}); cancel or finish those targets first"
        )


def load_file_record(conn: sqlite3.Connection, file_record_id: int) -> dict | None:
    """Return one ``file_records`` row as a dict, or ``None``."""
    if not _has_table(conn, "file_records"):
        return None
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM file_records WHERE id = ?", (int(file_record_id),)
    ).fetchone()
    return {key: row[key] for key in row.keys()} if row is not None else None


def resolve_account_namespace(
    conn: sqlite3.Connection, account_id: int, *, kind: str
) -> dict | None:
    """Resolve an account row in exactly ONE namespace.

    ``kind`` is ``"legacy"`` (``user_info``) or ``"structured"`` (``accounts``).
    The caller decides which namespace an id belongs to; this helper never
    falls through from one table to the other, because the two tables number
    independently and an overlapping id would otherwise delete the wrong row.
    """
    if kind not in NAMESPACES:
        raise ValueError(f"kind must be one of {NAMESPACES!r}")
    table = "user_info" if kind == NAMESPACE_LEGACY else "accounts"
    if not _has_table(conn, table):
        return None
    conn.row_factory = sqlite3.Row
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(account_id),)).fetchone()
    if row is None:
        return None
    result = {key: row[key] for key in row.keys()}
    result["__namespace__"] = kind
    return result


def account_uncertain_targets(conn: sqlite3.Connection, account_id: int) -> list[int]:
    """Terminal targets for this account that may already have reached a provider.

    A ``cancelled``/``failed`` target is normally free to discard, but one
    whose checkpoint records that a submission started, or that carries a
    provider receipt, may have published anyway. Deleting the account would
    destroy the credentials needed to resolve that ambiguity, so the caller
    must refuse. An unreadable checkpoint counts as uncertain.
    """
    if not _has_table(conn, "publish_job_targets"):
        return []
    ref = f"account:{int(account_id)}"
    try:
        rows = conn.execute(
            "SELECT id, checkpoint_json FROM publish_job_targets "
            "WHERE account_ref = ? AND status IN ('cancelled', 'failed')",
            (ref,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise AssetInUse(
            "Could not verify this account's delivery history; refusing deletion"
        ) from exc
    uncertain: list[int] = []
    for row in rows:
        raw = row["checkpoint_json"]
        if not raw:
            continue
        try:
            checkpoint = json.loads(raw)
        except (TypeError, ValueError):
            uncertain.append(int(row["id"]))
            continue
        if not isinstance(checkpoint, dict):
            uncertain.append(int(row["id"]))
            continue
        receipt = checkpoint.get("provider_receipt")
        if checkpoint.get("submission_started") or (
            isinstance(receipt, dict) and receipt.get("status")
        ):
            uncertain.append(int(row["id"]))
    return sorted(uncertain)


def assert_account_deletable(conn: sqlite3.Connection, account_id: int) -> None:
    """Raise :class:`AssetInUse` when this account still has unresolved work."""
    uncertain = account_uncertain_targets(conn, account_id)
    if uncertain:
        preview = ", ".join(str(value) for value in uncertain[:5])
        raise AssetInUse(
            "This account has deliveries whose outcome is still unknown "
            f"(targets {preview}); resolve them before deleting the account"
        )
