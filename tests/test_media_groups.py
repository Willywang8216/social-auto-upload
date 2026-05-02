"""Tests for media-group persistence."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

import db.createTable as create_table

if "conf" not in sys.modules:
    conf_module = types.ModuleType("conf")
    conf_module.BASE_DIR = str(Path(__file__).resolve().parent.parent)
    sys.modules["conf"] = conf_module

from myUtils import media_groups


class MediaGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        create_table.bootstrap(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _file_record(self, filename: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO file_records (filename, filesize, file_path)
                VALUES (?, ?, ?)
                """,
                (filename, 1.0, f"/tmp/{filename}"),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def test_create_media_group_and_items(self) -> None:
        video_id = self._file_record("clip.mp4")
        image_id = self._file_record("cover.jpg")
        group = media_groups.create_media_group(
            "Launch batch",
            notes="first pass",
            primary_video_file_id=video_id,
            db_path=self.db_path,
        )
        first = media_groups.add_media_group_item(
            group.id,
            video_id,
            role=media_groups.ROLE_VIDEO,
            db_path=self.db_path,
        )
        second = media_groups.add_media_group_item(
            group.id,
            image_id,
            role=media_groups.ROLE_IMAGE,
            db_path=self.db_path,
        )

        self.assertEqual(first.sort_order, 0)
        self.assertEqual(second.sort_order, 1)
        items = media_groups.list_media_group_items(group.id, db_path=self.db_path)
        self.assertEqual([item.role for item in items], ["video", "image"])

    def test_replace_items_resets_sort_order(self) -> None:
        file_a = self._file_record("a.jpg")
        file_b = self._file_record("b.jpg")
        group = media_groups.create_media_group("Batch", db_path=self.db_path)
        media_groups.add_media_group_item(group.id, file_a, db_path=self.db_path)

        items = media_groups.replace_media_group_items(
            group.id,
            [
                (file_b, media_groups.ROLE_IMAGE),
                (file_a, media_groups.ROLE_THUMBNAIL),
            ],
            db_path=self.db_path,
        )
        self.assertEqual(
            [(item.file_record_id, item.role, item.sort_order) for item in items],
            [(file_b, "image", 0), (file_a, "thumbnail", 1)],
        )

    def test_delete_media_group_cascades_items(self) -> None:
        file_id = self._file_record("clip.mp4")
        group = media_groups.create_media_group("Batch", db_path=self.db_path)
        media_groups.add_media_group_item(
            group.id,
            file_id,
            role=media_groups.ROLE_VIDEO,
            db_path=self.db_path,
        )

        media_groups.delete_media_group(group.id, db_path=self.db_path)
        self.assertEqual(media_groups.list_media_groups(db_path=self.db_path), [])
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM media_group_items"
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
