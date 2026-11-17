"""Teaching visual kit. Font/logo bytes are pinned before material review."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PIL import Image, ImageFont

from .contracts import HASH, loads, json_bytes, require, text

from .visuals import PALETTES
FONT_CANDIDATES = {
    'serif': [r'C:\Windows\Fonts\NotoSerifTC-VF.ttf', '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc'],
    'sans': [r'C:\Windows\Fonts\NotoSansTC-VF.ttf', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'],
}


def file_ref(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'brand_asset_missing')
    with path.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'path': str(path.resolve()), 'sha256': sha}


def default_kit(theme='ink', *, logo=None):
    require(isinstance(theme, str) and theme in PALETTES, 'brand_theme')
    fonts = {}
    for kind, candidates in FONT_CANDIDATES.items():
        path = next((Path(p) for p in candidates if Path(p).is_file()), None)
        require(path is not None, 'teaching_cjk_font_missing')
        fonts[kind] = {**file_ref(path), 'index': 3 if path.suffix.lower() == '.ttc' else 0}
    return validate_kit({'schema_version': 1, 'theme': theme, 'fonts': fonts,
                         'logo': file_ref(logo) if logo else None, 'attribution': 'iamwillywang.com'})


def verify_ref(ref, max_bytes, *, font=False):
    required = {'path', 'sha256', 'index'} if font else {'path', 'sha256'}
    require(isinstance(ref, dict) and set(ref) == required and isinstance(ref['sha256'], str) and HASH.fullmatch(ref['sha256'])
            and text(ref['path'], 2000), 'brand_asset_reference')
    if font:
        require(type(ref['index']) is int and 0 <= ref['index'] <= 3, 'brand_font_index')
    path = Path(ref['path'])
    require(path.is_absolute() and '..' not in path.parts and path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= max_bytes, 'brand_asset_path')
    with path.open('rb') as stream:
        require(hashlib.file_digest(stream, 'sha256').hexdigest() == ref['sha256'], 'brand_asset_changed')
    return path


def validate_kit(config):
    require(isinstance(config, dict) and set(config) == {'schema_version', 'theme', 'fonts', 'logo', 'attribution'}, 'kit_fields')
    require(type(config['schema_version']) is int and config['schema_version'] == 1 and isinstance(config['theme'], str) and config['theme'] in PALETTES
            and config['attribution'] == 'iamwillywang.com', 'teaching_brand_only')
    require(isinstance(config['fonts'], dict) and set(config['fonts']) == {'serif', 'sans'}, 'kit_fonts')
    for ref in config['fonts'].values():
        font = load_font(ref, 48)
        missing = font.getmask('\uffff')
        sample = font.getmask('學')
        require((sample.size, bytes(sample)) != (missing.size, bytes(missing)), 'cjk_glyph_missing')
    if config['logo'] is not None:
        path = verify_ref(config['logo'], 8 * 1024 * 1024)
        with Image.open(path) as image:
            require(image.width * image.height <= 16_777_216 and image.format == 'PNG', 'logo_format')
            image.verify()
    return loads(json_bytes(config))


def load_font(ref, size):
    path = verify_ref(ref, 64 * 1024 * 1024, font=True)
    font = ImageFont.truetype(str(path), size, index=ref['index'])
    require('TC' in font.getname()[0].upper().replace(' ', ''), 'traditional_chinese_font_required')
    return font
