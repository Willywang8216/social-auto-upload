"""Tests for watermark configuration and processing service."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from myUtils import watermark_service


@pytest.fixture()
def tmp_db(tmp_path):
    db = tmp_path / "test.db"
    from db.createTable import bootstrap
    bootstrap(db)
    return db


class TestWatermarkConfigCRUD:
    def test_create_and_get(self, tmp_db):
        config = watermark_service.create_watermark_config(
            name="test-watermark",
            watermark_type="text",
            text="© My Brand",
            opacity=0.5,
            db_path=tmp_db,
        )
        assert config.id > 0
        assert config.name == "test-watermark"
        assert config.text == "© My Brand"
        assert config.opacity == 0.5
        assert config.enabled is True

        fetched = watermark_service.get_watermark_config(config.id, db_path=tmp_db)
        assert fetched.id == config.id
        assert fetched.text == "© My Brand"

    def test_list_configs(self, tmp_db):
        watermark_service.create_watermark_config(name="wm1", db_path=tmp_db)
        watermark_service.create_watermark_config(name="wm2", db_path=tmp_db)
        configs = watermark_service.list_watermark_configs(db_path=tmp_db)
        assert len(configs) >= 2

    def test_update_config(self, tmp_db):
        config = watermark_service.create_watermark_config(name="old", db_path=tmp_db)
        updated = watermark_service.update_watermark_config(
            config.id, name="new", opacity=0.8, db_path=tmp_db
        )
        assert updated.name == "new"
        assert updated.opacity == 0.8

    def test_delete_config(self, tmp_db):
        config = watermark_service.create_watermark_config(name="del", db_path=tmp_db)
        watermark_service.delete_watermark_config(config.id, db_path=tmp_db)
        with pytest.raises(ValueError, match="not found"):
            watermark_service.get_watermark_config(config.id, db_path=tmp_db)

    def test_allowed_positions_roundtrip(self, tmp_db):
        positions = ["top_left", "bottom_right", "center"]
        config = watermark_service.create_watermark_config(
            allowed_positions=positions, db_path=tmp_db
        )
        fetched = watermark_service.get_watermark_config(config.id, db_path=tmp_db)
        assert fetched.allowed_positions == positions

    def test_video_dynamic_position_defaults(self, tmp_db):
        config = watermark_service.create_watermark_config(
            video_dynamic_position=True,
            video_position_change_min_seconds=2,
            video_position_change_max_seconds=4,
            db_path=tmp_db,
        )
        assert config.video_dynamic_position is True
        assert config.video_position_change_min_seconds == 2
        assert config.video_position_change_max_seconds == 4


class TestBuildDynamicOverlayTimeline:
    def test_basic_timeline(self):
        timeline = watermark_service.build_dynamic_overlay_timeline(
            10.0,
            allowed_positions=["top_left", "bottom_right"],
            min_window=2,
            max_window=3,
            seed=42,
        )
        assert len(timeline) > 0
        # Should cover the full duration
        assert timeline[-1]["end"] >= 10.0
        # Each segment should have a valid position
        for seg in timeline:
            assert seg["position"] in ["top_left", "bottom_right"]
            assert seg["start"] < seg["end"]

    def test_empty_duration(self):
        timeline = watermark_service.build_dynamic_overlay_timeline(
            0.0, allowed_positions=["top_left"]
        )
        assert timeline == []

    def test_single_position(self):
        timeline = watermark_service.build_dynamic_overlay_timeline(
            5.0,
            allowed_positions=["center"],
            min_window=1,
            max_window=2,
            seed=0,
        )
        for seg in timeline:
            assert seg["position"] == "center"


class TestDetectMediaType:
    def test_image_types(self):
        assert watermark_service is not None  # Import check
        from myUtils.media_asset_service import detect_media_type, MEDIA_TYPE_IMAGE, MEDIA_TYPE_VIDEO
        assert detect_media_type("photo.jpg") == MEDIA_TYPE_IMAGE
        assert detect_media_type("photo.png") == MEDIA_TYPE_IMAGE
        assert detect_media_type("photo.webp") == MEDIA_TYPE_IMAGE

    def test_video_types(self):
        from myUtils.media_asset_service import detect_media_type, MEDIA_TYPE_VIDEO
        assert detect_media_type("video.mp4") == MEDIA_TYPE_VIDEO
        assert detect_media_type("video.mov") == MEDIA_TYPE_VIDEO
        assert detect_media_type("video.mkv") == MEDIA_TYPE_VIDEO


class TestPositionResolution:
    def test_named_positions(self):
        x, y = watermark_service._resolve_position("top_left", 1920, 1080, 100, 50, 24)
        assert x == 24
        assert y == 24

        x, y = watermark_service._resolve_position("bottom_right", 1920, 1080, 100, 50, 24)
        assert x == 1920 - 100 - 24
        assert y == 1080 - 50 - 24

        x, y = watermark_service._resolve_position("center", 1920, 1080, 100, 50, 24)
        assert x == (1920 - 100) // 2
        assert y == (1080 - 50) // 2
