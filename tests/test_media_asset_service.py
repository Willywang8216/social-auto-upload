"""Tests for media asset service."""

from __future__ import annotations

from pathlib import Path

import pytest

from myUtils import media_asset_service


@pytest.fixture()
def tmp_db(tmp_path):
    db = tmp_path / "test.db"
    from db.createTable import bootstrap
    bootstrap(db)
    return db


class TestDetectMediaType:
    def test_image(self):
        assert media_asset_service.detect_media_type("photo.jpg") == media_asset_service.MEDIA_TYPE_IMAGE
        assert media_asset_service.detect_media_type("photo.PNG") == media_asset_service.MEDIA_TYPE_IMAGE
        assert media_asset_service.detect_media_type("photo.webp") == media_asset_service.MEDIA_TYPE_IMAGE

    def test_video(self):
        assert media_asset_service.detect_media_type("video.mp4") == media_asset_service.MEDIA_TYPE_VIDEO
        assert media_asset_service.detect_media_type("video.MOV") == media_asset_service.MEDIA_TYPE_VIDEO

    def test_audio(self):
        assert media_asset_service.detect_media_type("audio.mp3") == media_asset_service.MEDIA_TYPE_AUDIO
        assert media_asset_service.detect_media_type("audio.wav") == media_asset_service.MEDIA_TYPE_AUDIO

    def test_unknown_defaults_to_video(self):
        assert media_asset_service.detect_media_type("file.xyz") == media_asset_service.MEDIA_TYPE_VIDEO


class TestMediaAssetCRUD:
    def test_create_and_get(self, tmp_db):
        asset = media_asset_service.create_media_asset(
            original_filename="test.mp4",
            local_original_path="/tmp/test.mp4",
            media_type="video",
            file_size=1024,
            db_path=tmp_db,
        )
        assert asset.id > 0
        assert asset.original_filename == "test.mp4"
        assert asset.media_type == "video"
        assert asset.upload_status == "pending"
        assert asset.processing_status == "pending"

        fetched = media_asset_service.get_media_asset(asset.id, db_path=tmp_db)
        assert fetched.id == asset.id

    def test_list_assets(self, tmp_db):
        media_asset_service.create_media_asset(
            original_filename="a.mp4", db_path=tmp_db
        )
        media_asset_service.create_media_asset(
            original_filename="b.jpg", media_type="image", db_path=tmp_db
        )
        all_assets = media_asset_service.list_media_assets(db_path=tmp_db)
        assert len(all_assets) >= 2

    def test_list_filter_by_type(self, tmp_db):
        media_asset_service.create_media_asset(
            original_filename="v.mp4", media_type="video", db_path=tmp_db
        )
        media_asset_service.create_media_asset(
            original_filename="i.jpg", media_type="image", db_path=tmp_db
        )
        videos = media_asset_service.list_media_assets(media_type="video", db_path=tmp_db)
        assert all(a.media_type == "video" for a in videos)

    def test_update_asset(self, tmp_db):
        asset = media_asset_service.create_media_asset(
            original_filename="test.mp4", db_path=tmp_db
        )
        updated = media_asset_service.update_media_asset(
            asset.id,
            upload_status="uploaded",
            public_url="https://cdn.example.com/test.mp4",
            db_path=tmp_db,
        )
        assert updated.upload_status == "uploaded"
        assert updated.public_url == "https://cdn.example.com/test.mp4"

    def test_update_content_analysis(self, tmp_db):
        asset = media_asset_service.create_media_asset(
            original_filename="test.mp4", db_path=tmp_db
        )
        analysis = {"topic": "cooking", "mood": "happy"}
        updated = media_asset_service.update_media_asset(
            asset.id, content_analysis=analysis, db_path=tmp_db
        )
        assert updated.content_analysis == analysis

    def test_delete_asset(self, tmp_db):
        asset = media_asset_service.create_media_asset(
            original_filename="del.mp4", db_path=tmp_db
        )
        media_asset_service.delete_media_asset(asset.id, db_path=tmp_db)
        with pytest.raises(ValueError, match="not found"):
            media_asset_service.get_media_asset(asset.id, db_path=tmp_db)


class TestComputeChecksum:
    def test_checksum(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        checksum = media_asset_service.compute_checksum(f)
        assert len(checksum) == 64  # SHA-256 hex
        # Same content should produce same checksum
        assert media_asset_service.compute_checksum(f) == checksum


class TestBuildRclonePath:
    def test_path_format(self, tmp_db):
        asset = media_asset_service.create_media_asset(
            original_filename="my video.mp4", db_path=tmp_db
        )
        path = media_asset_service.build_rclone_path(
            asset, "my-brand", remote_root="Scripts-ssh-ssl-keys/SocialUpload"
        )
        assert "my-brand" in path
        assert f"{asset.id}_my_video.mp4" in path


class TestBatchCreate:
    def test_create_batch(self, tmp_db, tmp_path):
        files = []
        for i in range(3):
            f = tmp_path / f"file{i}.mp4"
            f.write_bytes(b"\x00" * 100)
            files.append(str(f))

        assets = media_asset_service.create_batch_assets(files, db_path=tmp_db)
        assert len(assets) == 3
        for a in assets:
            assert a.file_size == 100
            assert a.checksum  # Should have checksum
