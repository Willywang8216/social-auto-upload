"""Transport-only fixtures for the bounded, checksum-verified restore facade."""
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from myUtils import outbound_http


def setup_restore_transport(case, media_bytes):
    digest = hashlib.sha256(media_bytes).hexdigest()
    real_resolve = outbound_http.resolve_public_addresses

    def addresses(host, port=443):
        if host == "cdn.example":
            return ["8.8.8.8"]
        return real_resolve(host, port)

    def probe(source, **_kwargs):
        if source.get("public_url"):
            outbound_http.assert_public_https_url(source["public_url"])
        return {"size_bytes": len(media_bytes), "remote_checksum": {"algorithm": "SHA256", "value": digest}}

    for mock in (patch("myUtils.media_restore.probe_source", side_effect=probe),
                 patch("myUtils.outbound_http.resolve_public_addresses", side_effect=addresses)):
        mock.start()
        case.addCleanup(mock.stop)


def register_public_file(case, artifact, media_bytes):
    """Register the exact synthetic selected output BEFORE attempting restoration."""
    path = Path(artifact["local_path"])
    refs = {str(path)}
    if path.is_relative_to(case.root):
        relative = path.relative_to(case.root).as_posix()
        refs.add(relative)
        if relative.startswith("videoFile/"):
            refs.add(relative.removeprefix("videoFile/"))
    with closing(sqlite3.connect(case.db_path)) as conn, conn:
        rows = conn.execute("SELECT id FROM file_records WHERE file_path IN (" + ",".join("?" for _ in refs) + ")", tuple(refs)).fetchall()
        if len(rows) > 1:
            raise AssertionError("Ambiguous synthetic output registration")
        digest, size = hashlib.sha256(media_bytes).hexdigest(), len(media_bytes)
        if rows:
            identifier = rows[0][0]
            conn.execute("UPDATE file_records SET storage_cdn_url=?,sha256=?,size_bytes=?,filesize=? WHERE id=?",
                         (artifact["public_url"], digest, size, size, identifier))
        else:
            identifier = conn.execute("INSERT INTO file_records(filename,file_path,filesize,sha256,size_bytes,storage_cdn_url) VALUES(?,?,?,?,?,?)",
                (path.name, str(path), size, digest, size, artifact["public_url"])).lastrowid
    artifact["file_record_id"] = identifier
