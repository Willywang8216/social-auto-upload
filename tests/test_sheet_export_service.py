"""Tests for sheet export service."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from myUtils import sheet_export_service


@pytest.fixture()
def tmp_db(tmp_path):
    db = tmp_path / "test.db"
    from db.createTable import bootstrap
    bootstrap(db)
    return db


class TestSheetColumnOrder:
    def test_exactly_20_columns(self):
        assert len(sheet_export_service.SHEET_COLUMN_ORDER) == 20

    def test_column_order(self):
        expected = [
            "Message", "Link", "ImageURL", "VideoURL",
            "Month(1-12)", "Day(1-31)", "Year", "Hour", "Minute(0-59)",
            "PinTitle", "Category", "Watermark", "HashtagGroup",
            "VideoThumbnailURL", "CTAGroup", "FirstComment",
            "Story(YorN)", "PinterestBoard", "AltText", "PostPreset",
        ]
        assert sheet_export_service.SHEET_COLUMN_ORDER == expected


class TestValidateSheetRow:
    def test_image_url_and_video_url_mutual_exclusion(self):
        row = {
            "Message": "test",
            "ImageURL": "https://example.com/img.jpg",
            "VideoURL": "https://example.com/video.mp4",
        }
        errors = sheet_export_service.validate_sheet_row(row)
        assert any("cannot both be populated" in e for e in errors)

    def test_max_image_urls(self):
        urls = ",".join([f"https://example.com/img{i}.jpg" for i in range(5)])
        row = {"ImageURL": urls}
        errors = sheet_export_service.validate_sheet_row(row)
        assert any("Maximum 4" in e for e in errors)

    def test_valid_four_images(self):
        urls = ",".join([f"https://example.com/img{i}.jpg" for i in range(4)])
        row = {"ImageURL": urls}
        errors = sheet_export_service.validate_sheet_row(row)
        img_errors = [e for e in errors if "image" in e.lower()]
        assert len(img_errors) == 0

    def test_video_url_should_be_mp4(self):
        row = {"VideoURL": "https://example.com/video.avi"}
        errors = sheet_export_service.validate_sheet_row(row)
        assert any(".mp4" in e for e in errors)

    def test_video_url_mp4_valid(self):
        row = {"VideoURL": "https://example.com/video.mp4"}
        errors = sheet_export_service.validate_sheet_row(row)
        mp4_errors = [e for e in errors if ".mp4" in e]
        assert len(mp4_errors) == 0

    def test_platform_char_limit(self):
        row = {"Message": "x" * 300}
        errors = sheet_export_service.validate_sheet_row(row, platform="twitter")
        assert any("280" in e for e in errors)

    def test_schedule_fields_all_or_none(self):
        row = {"Month(1-12)": "6", "Day(1-31)": "15"}
        errors = sheet_export_service.validate_sheet_row(row)
        assert any("scheduling" in e.lower() for e in errors)

    def test_schedule_fields_all_filled(self):
        row = {
            "Month(1-12)": "6", "Day(1-31)": "15", "Year": "2026",
            "Hour": "10", "Minute(0-59)": "30",
        }
        errors = sheet_export_service.validate_sheet_row(row)
        schedule_errors = [e for e in errors if "scheduling" in e.lower()]
        assert len(schedule_errors) == 0

    def test_schedule_fields_all_empty(self):
        row = {
            "Month(1-12)": "", "Day(1-31)": "", "Year": "",
            "Hour": "", "Minute(0-59)": "",
        }
        errors = sheet_export_service.validate_sheet_row(row)
        schedule_errors = [e for e in errors if "scheduling" in e.lower()]
        assert len(schedule_errors) == 0


class TestBuildSheetRowFromPost:
    def test_basic_row(self):
        post = {
            "message": "Hello",
            "link": "https://example.com",
            "image_urls": "https://a.com/1.jpg,https://b.com/2.jpg",
            "video_url": "",
            "scheduled_month": "6",
            "scheduled_day": "15",
            "scheduled_year": "2026",
            "scheduled_hour": "10",
            "scheduled_minute": "30",
            "title": "My Title",
            "category": "teaching",
            "watermark_name": "default",
            "hashtag_group": "brand-a",
            "video_thumbnail_url": "",
            "cta_group": "follow",
            "first_comment": "Check this out!",
            "story_flag": True,
            "pinterest_board": "",
            "alt_text": "Image description",
            "post_preset": "standard",
        }
        row = sheet_export_service.build_sheet_row_from_post(post)
        assert row["Message"] == "Hello"
        assert row["ImageURL"] == "https://a.com/1.jpg,https://b.com/2.jpg"
        assert row["Story(YorN)"] == "Y"
        assert row["PinTitle"] == "My Title"


class TestBuildSheetRows:
    def test_excludes_telegram_patreon_discord(self):
        posts = [
            {"platform": "twitter", "message": "t"},
            {"platform": "telegram", "message": "tg"},
            {"platform": "patreon", "message": "p"},
            {"platform": "discord", "message": "d"},
            {"platform": "instagram", "message": "ig"},
        ]
        rows = sheet_export_service.build_sheet_rows(posts)
        assert len(rows) == 2
        messages = [r["Message"] for r in rows]
        assert "t" in messages
        assert "ig" in messages

    def test_includes_all_when_overridden(self):
        posts = [
            {"platform": "twitter", "message": "t"},
            {"platform": "telegram", "message": "tg"},
        ]
        rows = sheet_export_service.build_sheet_rows(posts, exclude_platforms=set())
        assert len(rows) == 2


class TestGenerateCSV:
    def test_csv_has_header(self):
        rows = [{"Message": "Hello", "Link": "", "ImageURL": "", "VideoURL": "",
                 "Month(1-12)": "", "Day(1-31)": "", "Year": "", "Hour": "",
                 "Minute(0-59)": "", "PinTitle": "", "Category": "", "Watermark": "",
                 "HashtagGroup": "", "VideoThumbnailURL": "", "CTAGroup": "",
                 "FirstComment": "", "Story(YorN)": "", "PinterestBoard": "",
                 "AltText": "", "PostPreset": ""}]
        csv_str = sheet_export_service.generate_csv(rows)
        reader = csv.DictReader(io.StringIO(csv_str))
        assert reader.fieldnames == sheet_export_service.SHEET_COLUMN_ORDER

    def test_csv_data_rows(self):
        rows = [{"Message": "Test post", "Link": "https://example.com",
                 "ImageURL": "", "VideoURL": "",
                 "Month(1-12)": "", "Day(1-31)": "", "Year": "", "Hour": "",
                 "Minute(0-59)": "", "PinTitle": "", "Category": "", "Watermark": "",
                 "HashtagGroup": "", "VideoThumbnailURL": "", "CTAGroup": "",
                 "FirstComment": "", "Story(YorN)": "", "PinterestBoard": "",
                 "AltText": "", "PostPreset": ""}]
        csv_str = sheet_export_service.generate_csv(rows)
        lines = csv_str.strip().split("\n")
        assert len(lines) == 2  # header + 1 data row
        assert "Test post" in lines[1]

    def test_csv_bytes_has_bom(self):
        rows = []
        csv_bytes = sheet_export_service.generate_csv_bytes(rows)
        assert csv_bytes.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM


class TestGenerateSheetName:
    def test_format(self):
        from datetime import datetime
        name = sheet_export_service.generate_sheet_name("my-brand", date=datetime(2026, 6, 15))
        assert name == "2026-06-15_my-brand"


class TestSheetExportCRUD:
    def test_create_and_list(self, tmp_db):
        sheet_export_service.create_sheet_export(
            sheet_name="test-sheet",
            status="completed",
            db_path=tmp_db,
        )
        exports = sheet_export_service.list_sheet_exports(db_path=tmp_db)
        assert len(exports) >= 1
        assert exports[0].sheet_name == "test-sheet"