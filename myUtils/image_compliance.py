"""Measured, immutable still-image preparation before scheduling.

Actual bytes are decoded with Pillow, EXIF-transposed and converted to sRGB.
Only bounded scale/pad operations are used; animation is rejected for review,
never flattened to its first frame. A complete set is advertised only after
EVERY final file passes dimensions, ratio, format and measured-byte checks.
"""
from __future__ import annotations

import io
import json
import math
import shutil
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

from myUtils import media_compliance as media, platform_limits as pl

MAX_DECODE_PIXELS = 40_000_000  # operational memory bound, not a provider maximum
MAX_ENCODE_ATTEMPTS = 12
MAX_SET_IMAGES = 1000
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


@dataclass(frozen=True)
class PreparedImage:
    path: Path
    metadata: dict


def effective_limits(platform: str, *, account_config: dict | None = None,
                     limits: dict | None = None, backup_compatible: bool = False) -> dict:
    return media.tighten_limits(pl.image_preparation_limits(
        platform, account_config=account_config, backup_compatible=backup_compatible), limits)


def _decode(path: Path):
    """Return an independent sRGB image and the ACTUAL original encoding info."""
    try:
        from PIL import Image, ImageCms, ImageOps
        if not path.is_file() or path.stat().st_size <= 0:
            raise media.MediaPreparationError("Image source is missing or empty")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as opened:
                if opened.width * opened.height > MAX_DECODE_PIXELS:
                    raise media.MediaPreparationError("Image exceeds the bounded decoder pixel budget")
                if getattr(opened, "n_frames", 1) != 1:
                    raise media.MediaPreparationError("Animated image requires review; no frames may be silently dropped")
                opened.load()
                orientation = opened.getexif().get(274, 1)
                info = {"format": opened.format, "mode": opened.mode, "orientation": orientation,
                        "size_bytes": path.stat().st_size, "color_space": "sRGB"}
                image = ImageOps.exif_transpose(opened).copy()
                profile = opened.info.get("icc_profile")
                if profile:
                    source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
                    info["color_space"] = ("sRGB" if "srgb" in ImageCms.getProfileName(source_profile).lower() else "other")
                    alpha = image.getchannel("A") if image.mode == "RGBA" else None
                    image = ImageCms.profileToProfile(image.convert("RGB") if alpha else image,
                                                      source_profile, ImageCms.createProfile("sRGB"), outputMode="RGB")
                    if alpha is not None:
                        image.putalpha(alpha)
                elif image.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
                    info["color_space"] = "other"
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "transparency" in opened.info or image.mode == "LA" else "RGB")
                info.update(width=image.width, height=image.height)
                return image, info
    except media.MediaPreparationError:
        raise
    except Exception as exc:
        raise media.MediaPreparationError(f"Cannot decode image: {type(exc).__name__}") from exc


def probe_image(path: str | Path) -> dict:
    """Read-only probe of real bytes; does not trust a suffix or stored metadata."""
    image, info = _decode(Path(path).expanduser().resolve())
    image.close()
    return info


def validation_errors(path: Path, info: dict, limits: dict) -> list[str]:
    errors = []
    if info["size_bytes"] <= 0:
        errors.append("empty image")
    if limits["max_bytes"] is not None and info["size_bytes"] > limits["max_bytes"]:
        errors.append("size")
    width, height = info["width"], info["height"]
    if not (limits["min_width"] <= width <= limits["max_width"]
            and limits["min_height"] <= height <= limits["max_height"]):
        errors.append("dimensions")
    if limits["max_dimension_sum"] is not None and width + height > limits["max_dimension_sum"]:
        errors.append("dimension sum")
    if ((limits["min_aspect_ratio"] is not None and width / height < limits["min_aspect_ratio"])
            or (limits["max_aspect_ratio"] is not None and width / height > limits["max_aspect_ratio"])):
        errors.append("aspect ratio")
    allowed_suffixes = {"JPEG": {".jpg", ".jpeg"}, "PNG": {".png"}}
    if info["format"] not in limits["formats"] or path.suffix.lower() not in allowed_suffixes.get(info["format"], set()):
        errors.append("image encoding")
    if info["orientation"] not in {None, 1}:
        errors.append("EXIF orientation")
    if info["color_space"] != "sRGB" or info["mode"] not in {"RGB", "RGBA"}:
        errors.append("sRGB color")
    return errors


def validate_image(path: str | Path, *, platform: str, account_config: dict | None = None,
                   limits: dict | None = None, expected_sha256: str | None = None,
                   backup_compatible: bool = False) -> dict:
    """Read-only immutable-byte gate; no encode, file writes or database writes."""
    path = Path(path).expanduser().resolve()
    constraints = effective_limits(platform, account_config=account_config, limits=limits,
                                   backup_compatible=backup_compatible)
    info = probe_image(path)
    errors = validation_errors(path, info, constraints)
    if errors:
        raise media.MediaPreparationError(f"Image needs preprocessing for {platform}: {', '.join(errors)}")
    if expected_sha256 is not None:
        media.verify_checksum(path, expected_sha256)
    return info


def _canvas(image, limits: dict, shrink: float):
    from PIL import Image
    ratio = image.width / image.height
    ratio = max(limits["min_aspect_ratio"] or ratio, ratio)
    ratio = min(limits["max_aspect_ratio"] or ratio, ratio)
    width = max(image.width, math.ceil(image.height * ratio))
    height = max(image.height, math.ceil(image.width / ratio))
    factor = min(1.0, limits["max_width"] / width, limits["max_height"] / height) * shrink
    if limits["max_dimension_sum"] is not None:
        factor = min(factor, limits["max_dimension_sum"] / (width + height))
    width = max(limits["min_width"], int(width * factor), 1)
    height = max(limits["min_height"], int(height * factor), 1)
    if limits["min_aspect_ratio"] is not None:
        width = max(width, math.ceil(height * limits["min_aspect_ratio"]))
    if limits["max_aspect_ratio"] is not None:
        height = max(height, math.ceil(width / limits["max_aspect_ratio"]))
    if (width > limits["max_width"] or height > limits["max_height"]
            or (limits["max_dimension_sum"] is not None and width + height > limits["max_dimension_sum"])):
        raise media.MediaPreparationError("Image cannot fit the requested padded canvas")
    contained = image.copy()
    contained.thumbnail((width, height), Image.Resampling.LANCZOS)
    mode = "RGBA" if contained.mode == "RGBA" else "RGB"
    canvas = Image.new(mode, (width, height), (255, 255, 255, 0) if mode == "RGBA" else "white")
    canvas.paste(contained, ((width - contained.width) // 2, (height - contained.height) // 2))
    contained.close()
    return canvas


def _encode(image, *, fmt: str, quality: int) -> bytes:
    from PIL import Image, ImageCms
    working = image
    if fmt == "JPEG" and image.mode == "RGBA":
        working = Image.new("RGB", image.size, "white")
        working.paste(image, mask=image.getchannel("A"))
    try:
        buffer = io.BytesIO()
        kwargs = {"icc_profile": ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()}
        if fmt == "JPEG":
            kwargs.update(quality=quality, optimize=True, progressive=False)
        else:
            kwargs.update(optimize=True, compress_level=9)
        from myUtils.media_prep import encode_slot
        with encode_slot():
            working.save(buffer, format=fmt, **kwargs)
        value = buffer.getvalue()
        if not value:
            raise media.MediaPreparationError("Image encoder returned no bytes")
        return value
    except media.MediaPreparationError:
        raise
    except Exception as exc:
        raise media.MediaPreparationError(f"Image encoder failed: {type(exc).__name__}") from exc
    finally:
        if working is not image:
            working.close()


def prepare_images(sources: list[str | Path], out_dir: str | Path, *, platform: str,
                   account_config: dict | None = None, limits: dict | None = None,
                   backup_compatible: bool = False, provenance: list[dict | None] | None = None) -> list[PreparedImage]:
    """Prepare a COMPLETE ordered immutable generation (batch posts separately)."""
    if not sources or len(sources) > MAX_SET_IMAGES:
        raise media.MediaPreparationError("No image set or image set exceeds the bounded preparation count")
    constraints = effective_limits(platform, account_config=account_config, limits=limits,
                                   backup_compatible=backup_compatible)
    paths = [Path(source).expanduser().resolve() for source in sources]
    origins = provenance if provenance is not None else [None] * len(paths)
    if len(origins) != len(paths):
        raise media.MediaPreparationError("Image provenance does not match the complete set")
    generation = Path(out_dir).expanduser().resolve() / pl.PREPARATION_POLICY_VERSION / uuid.uuid4().hex
    generation.mkdir(parents=True, exist_ok=False)
    parts = []
    try:
        for index, (source, origin) in enumerate(zip(paths, origins), 1):
            if not source.is_file() or source.stat().st_size <= 0:
                raise media.MediaPreparationError("Image source is missing or empty")
            input_sha = media.sha256_file(source)
            image, _ = _decode(source)
            try:
                if origin:
                    media.verify_checksum(source, origin.get("sha256"))
                fmt = "PNG" if image.mode == "RGBA" and "PNG" in constraints["formats"] else constraints["formats"][0]
                hard = constraints["max_bytes"]
                target = min(constraints.get("target_bytes") or int(hard * 0.95), int(hard * 0.95)) if hard else None
                output = generation / f"image{index}of{len(paths)}.{'jpg' if fmt == 'JPEG' else 'png'}"
                for attempt in range(MAX_ENCODE_ATTEMPTS):
                    scale = 0.8 ** (attempt // 4 if fmt == "JPEG" else attempt)
                    canvas = _canvas(image, constraints, scale)
                    try:
                        encoded = _encode(canvas, fmt=fmt, quality=(90, 82, 74, 66)[attempt % 4])
                    finally:
                        canvas.close()
                    if target is None or len(encoded) <= target:
                        with output.open("xb") as stream:
                            stream.write(encoded)
                        break
                else:
                    raise media.MediaPreparationError("Image cannot fit its measured byte budget after bounded encodes")
            finally:
                image.close()
            info = validate_image(output, platform=platform, account_config=account_config,
                                  limits=constraints, backup_compatible=backup_compatible)
            metadata = {"role": "image", "preparation_policy_version": pl.PREPARATION_POLICY_VERSION,
                        "preparation_group": generation.name, "prepared_for": [platform],
                        "source_sha256": (origin or {}).get("source_sha256", input_sha),
                        "source_path": (origin or {}).get("source_path", str(source)),
                        "input_path": str(source), "input_sha256": input_sha,
                        "sha256": media.sha256_file(output), "size_bytes": info["size_bytes"],
                        "width": info["width"], "height": info["height"], "image_format": info["format"],
                        "color_space": "sRGB", "image_index": index, "image_count": len(paths),
                        "preparation_limits": constraints}
            if origin:
                metadata["reprepared_from"] = {"path": str(source), "sha256": input_sha,
                                               "preparation_group": origin.get("preparation_group")}
            parts.append(PreparedImage(output, metadata))
        for source, part in zip(paths, parts):
            media.verify_checksum(source, part.metadata["input_sha256"])
            validate_image(part.path, platform=platform, account_config=account_config, limits=constraints,
                           expected_sha256=part.metadata["sha256"], backup_compatible=backup_compatible)
        with (generation / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump({"preparation_policy_version": pl.PREPARATION_POLICY_VERSION,
                       "preparation_group": generation.name, "preparation_limits": constraints,
                       "images": [{"path": str(part.path), **part.metadata} for part in parts]},
                      stream, sort_keys=True, indent=2)
        return parts
    except BaseException:
        shutil.rmtree(generation, ignore_errors=True)
        raise


def prepare_image(src: str | Path, out_dir: str | Path, *, platform: str,
                  account_config: dict | None = None, limits: dict | None = None,
                  backup_compatible: bool = False, provenance: dict | None = None) -> PreparedImage:
    return prepare_images([src], out_dir, platform=platform, account_config=account_config,
        limits=limits, backup_compatible=backup_compatible, provenance=[provenance] if provenance else None)[0]


def select_prepared_images(artifacts: list[dict], platform: str) -> list[dict]:
    """Select the latest WHOLE image generation; never fill gaps from older sets."""
    groups = {}
    for artifact in artifacts:
        metadata = artifact.get("metadata") or {}
        if platform in (metadata.get("prepared_for") or []):
            groups.setdefault(metadata.get("preparation_group"), []).append(artifact)
    if not groups or None in groups or "" in groups:
        raise media.MediaPreparationRequired(f"No complete prepared image generation for {platform}")
    group = max(groups.values(), key=lambda items: max(int(item.get("id") or 0) for item in items))
    count = group[0]["metadata"].get("image_count")
    if type(count) is not int or not 1 <= count <= MAX_SET_IMAGES:
        raise media.MediaPreparationError("Invalid prepared image count")
    selected = {}
    for artifact in group:
        metadata = artifact["metadata"]
        checksum = metadata.get("sha256")
        if (not isinstance(checksum, str) or len(checksum) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in checksum)):
            raise media.MediaPreparationError("Prepared image has no valid checksum")
        index = metadata.get("image_index")
        if type(index) is not int or not 1 <= index <= count or metadata.get("image_count") != count:
            raise media.MediaPreparationError("Inconsistent prepared image set")
        prior = selected.get(index)
        if prior and prior["metadata"].get("sha256") != metadata.get("sha256"):
            raise media.MediaPreparationError("Prepared image index refers to different bytes")
        if not prior or artifact.get("public_url"):
            selected[index] = artifact
    if set(selected) != set(range(1, count + 1)):
        raise media.MediaPreparationError("Incomplete prepared image generation")
    return [selected[index] for index in range(1, count + 1)]


def ordered_batches(items: list, platform: str, *, account_config: dict | None = None,
                    limits: dict | None = None, backup_compatible: bool = False) -> list[list]:
    cap = effective_limits(platform, account_config=account_config, limits=limits,
                           backup_compatible=backup_compatible)["max_items"]
    for item in items:
        if isinstance(item, dict):
            planned = (item.get("metadata") or {}).get("preparation_limits") or {}
            if not isinstance(planned, dict):
                raise media.MediaPreparationError("Invalid stored image preparation constraints")
            cap = media.tighten_limits({"max_items": cap},
                  {"max_items": planned.get("max_items")})["max_items"]
    return [items[start:start + cap] for start in range(0, len(items), cap)]


def materialise_srgb(src: str | Path, output: str | Path) -> Path:
    """Immutable lossless intermediate before a transform that strips EXIF/ICC.

    This is NOT an upload allowance or a final publishable artifact. The final
    route-specific byte/geometry gate must still run after watermarking.
    """
    source, output = Path(src).resolve(), Path(output).resolve()
    checksum = media.sha256_file(source)
    image, _ = _decode(source)
    created = False
    try:
        encoded = _encode(image, fmt="PNG", quality=90)
        if not encoded:
            raise media.MediaPreparationError("Image encoder returned no bytes")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            created = True
            stream.write(encoded)
        probe_image(output)
        media.verify_checksum(source, checksum)
        return output
    except BaseException:
        # Only remove the intermediate we just created, never an existing file.
        if created:
            output.unlink(missing_ok=True)
        raise
    finally:
        image.close()
