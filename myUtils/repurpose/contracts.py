"""Portable, strict v2 contract. Hashes identify material, never grant approval."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from pathlib import Path

DATABASE_ID = '3cffcdc6-586f-81e3-9ecb-c63e62f11d4c'
PROFILE_ID = 4
ACCOUNTS = {43: 'instagram', 58: 'facebook', 61: 'threads', 125: 'bluesky'}
DEFAULT_DELIVERIES = ['43:cards', '43:video', '58:cards', '58:video', '61:cards', '125:cards']
MAX_JSON = 512 * 1024
MAX_ARTICLE = 512 * 1024
MAX_ASSET = 128 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
CHECK_NAMES = {'voice', 'humanizer', 'source-fidelity', 'layout', 'video-decode'}
HASH = re.compile(r'[a-f0-9]{64}\Z')
SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')


class RepurposeError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require(condition, code):
    if not condition:
        raise RepurposeError(code)


def json_bytes(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise RepurposeError('invalid_json') from exc
    require(len(raw) <= MAX_JSON, 'json_too_large')
    return raw


def digest(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def loads(raw):
    require(isinstance(raw, (str, bytes)), 'invalid_json')
    try:
        require(len(raw.encode('utf-8') if isinstance(raw, str) else raw) <= MAX_JSON, 'json_too_large')
    except UnicodeError as exc:
        raise RepurposeError('invalid_json') from exc
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'duplicate_json_key')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: require(False, 'nonfinite_json'))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RepurposeError('invalid_json') from exc


def text(value, maximum=1000, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or not (empty or value.strip()):
        return False
    try:
        value.encode('utf-8')
    except UnicodeError:
        return False
    return not any(ord(c) < 32 and c not in '\n\t' for c in value)


def checksum(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def validate_source(value):
    required = {'database_id', 'page_id', 'slug', 'title', 'article_version', 'article_sha256', 'manuscript'}
    require(isinstance(value, dict) and set(value) == required, 'source_fields')
    require(value['database_id'] == DATABASE_ID, 'wrong_database')
    try:
        require(str(uuid.UUID(value['page_id'])) == value['page_id'], 'page_id')
    except (ValueError, TypeError, AttributeError) as exc:
        raise RepurposeError('page_id') from exc
    require(text(value['slug'], 160) and SLUG.fullmatch(value['slug']), 'slug')
    require(text(value['title'], 200) and checksum(value['article_version']) and checksum(value['article_sha256']), 'source_identity')
    manuscript = value['manuscript']
    require(text(manuscript, MAX_ARTICLE) and len(manuscript.encode('utf-8')) <= MAX_ARTICLE and '\r' not in manuscript, 'manuscript')
    require(hashlib.sha256(manuscript.encode('utf-8')).hexdigest() == value['article_sha256'], 'source_hash')
    require(value['slug'] not in {'amazon-to-teaching-journey', 'huajiang-2026-final-english'}, 'withdrawn_source')
    return loads(json_bytes(value))


def paragraphs(source):
    """Stable source blocks; definitions/code/equations remain source, not instructions."""
    validate_source(source)
    body = source['manuscript']
    if body.startswith('---\n'):
        marker = body.find('\n---\n', 4)
        require(marker >= 0, 'frontmatter_unclosed')
        body = body[marker + 5:]
    blocks = [block.strip() for block in re.split(r'\n\s*\n', body) if block.strip()]
    require(1 <= len(blocks) <= 1000, 'source_blocks')
    return [{'id': f'p{i:04d}', 'text': block, 'sha256': hashlib.sha256(block.encode('utf-8')).hexdigest()}
            for i, block in enumerate(blocks, 1)]


def validate_deliveries(values):
    require(isinstance(values, list) and 4 <= len(values) <= 8 and all(isinstance(v, str) for v in values), 'delivery_plan')
    unique = set(values)
    require(len(values) == len(unique), 'delivery_plan')
    allowed = {f'{a}:{kind}' for a in ACCOUNTS for kind in ('cards', 'video')}
    require(unique <= allowed and {f'{a}:cards' for a in ACCOUNTS} <= unique, 'delivery_scope')
    return list(values)


def validate_storyboard(value, source):
    require(isinstance(value, dict) and set(value) == {'deck', 'bluesky', 'video_slide_ids', 'captions'}, 'storyboard_fields')
    refs = {block['id']: block['text'] for block in paragraphs(source)}
    ids = set()
    for name, minimum, maximum in (('deck', 6, 8), ('bluesky', 4, 4)):
        slides = value[name]
        require(isinstance(slides, list) and minimum <= len(slides) <= maximum, 'slide_count')
        for slide in slides:
            require(isinstance(slide, dict) and set(slide) == {'id', 'role', 'title', 'lines', 'source_refs'}, 'slide_fields')
            require(text(slide['id'], 40) and re.fullmatch(r'[a-z0-9-]+', slide['id']) and slide['id'] not in ids, 'slide_id')
            ids.add(slide['id'])
            require(isinstance(slide['role'], str) and slide['role'] in {'title', 'explanation', 'example', 'action'}, 'slide_role')
            require(text(slide['title'], 60), 'slide_title')
            require(isinstance(slide['lines'], list) and 1 <= len(slide['lines']) <= 4 and all(text(line, 180) for line in slide['lines']), 'slide_lines')
            validate_refs(slide['source_refs'], refs)
            require(len(set(slide['lines'])) == len(slide['lines']), 'duplicate_slide_lines')
        require(slides[0]['role'] == 'title' and slides[-1]['role'] == 'action', 'deck_order')
    require(value['video_slide_ids'] == [slide['id'] for slide in value['deck']], 'video_must_cover_approved_deck')
    captions = value['captions']
    require(isinstance(captions, dict) and set(captions) == {str(a) for a in ACCOUNTS}, 'caption_accounts')
    canonical = 'https://iamwillywang.com/post/' + source['slug']
    for drafts in captions.values():
        require(isinstance(drafts, dict) and set(drafts) == {'cards', 'video'}, 'caption_formats')
        for kind, draft in drafts.items():
            require(isinstance(draft, dict) and set(draft) == {'message', 'source_refs'}, 'caption_fields')
            require(text(draft['message'], 2200) and canonical in draft['message'], 'caption_link')
            validate_refs(draft['source_refs'], refs)
            if kind == 'video':
                require('摘要' in draft['message'], 'summary_label')
    return loads(json_bytes(value))


def validate_refs(values, source_blocks):
    require(isinstance(values, list) and 1 <= len(values) <= 12, 'source_refs')
    seen = set()
    for ref in values:
        require(isinstance(ref, dict) and set(ref) == {'paragraph_id', 'quote'}, 'source_ref_fields')
        pid, quote = ref['paragraph_id'], ref['quote']
        require(text(pid, 40) and pid in source_blocks and text(quote, 1600) and quote in source_blocks[pid], 'unverified_source_reference')
        require((pid, quote) not in seen, 'duplicate_source_reference')
        seen.add((pid, quote))


def asset_descriptor(path, *, root, kind, mime, **facts):
    path, root = Path(path), Path(root)
    require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()), 'asset_path')
    size = path.stat().st_size
    require(0 < size <= MAX_ASSET, 'asset_size')
    with path.open('rb') as stream:
        checksum_value = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'name': path.relative_to(root).as_posix(), 'kind': kind, 'mime': mime, 'sha256': checksum_value, 'size_bytes': size, **facts}


def expected_assets(board):
    expected = {}
    for kind in ('deck', 'bluesky', 'video-slide'):
        count = len(board['deck' if kind == 'video-slide' else kind])
        for i in range(1, count + 1):
            for suffix in (('png',) if kind == 'video-slide' else ('png', 'jpg')):
                expected[f'{kind}-{i:02d}.{suffix}'] = (kind, 'image/png' if suffix == 'png' else 'image/jpeg')
    expected.update({'summary.mp4': ('summary', 'video/mp4'), 'summary-poster.jpg': ('poster', 'image/jpeg'),
                     'summary.txt': ('companion', 'text/plain')})
    expected.update({f'check-{name}.json': ('check', 'application/json') for name in CHECK_NAMES})
    return expected


def seal_manifest(material):
    manifest = {'kind': 'teaching-repurpose-package', 'schema_version': 2, 'publication_authorized': False, 'material': material}
    manifest['package_version'] = digest(manifest)
    return validate_manifest(manifest)


def validate_manifest(value):
    require(isinstance(value, dict) and set(value) == {'kind', 'schema_version', 'publication_authorized', 'material', 'package_version'}, 'package_fields')
    require(value['kind'] == 'teaching-repurpose-package' and type(value['schema_version']) is int and value['schema_version'] == 2
            and value['publication_authorized'] is False, 'package_never_authorizes')
    require(checksum(value['package_version']) and digest({k: v for k, v in value.items() if k != 'package_version'}) == value['package_version'], 'package_hash')
    m = value['material']
    require(isinstance(m, dict) and set(m) == {'source', 'kit_version', 'rules_version', 'storyboard', 'deliveries', 'assets', 'checks'}, 'material_fields')
    validate_source(m['source'])
    require(checksum(m['kit_version']) and checksum(m['rules_version']), 'material_version')
    validate_storyboard(m['storyboard'], m['source'])
    require(isinstance(m['deliveries'], list) and all(isinstance(d, dict) and set(d) == {'id', 'account_id', 'format', 'asset_names', 'message'} for d in m['deliveries']), 'delivery_fields')
    validate_deliveries([d['id'] for d in m['deliveries']])
    assets = m['assets']; expected = expected_assets(m['storyboard'])
    require(isinstance(assets, list) and len(assets) == len(expected) and all(isinstance(a, dict) for a in assets), 'asset_count')
    names = set(); basic = {'name', 'kind', 'mime', 'sha256', 'size_bytes'}
    for a in assets:
        require(basic <= set(a), 'asset_fields')
        name = a['name']
        require(text(name, 180) and name in expected and name not in names, 'asset_name')
        names.add(name)
        require((a['kind'], a['mime']) == expected[name], 'asset_mime')
        require(checksum(a['sha256']) and type(a['size_bytes']) is int and 0 < a['size_bytes'] <= MAX_ASSET, 'asset_facts')
        fields = basic
        if a['mime'].startswith('image/'):
            fields = basic | {'width', 'height'}
            require(type(a.get('width')) is type(a.get('height')) is int and a['width'] == 1080 and a['height'] == (1920 if a['kind'] in {'video-slide','poster'} else 1350), 'image_dimensions')
        elif a['mime'] == 'video/mp4':
            fields = basic | {'width','height','fps','duration_seconds','video_codec','audio_codec','decoded'}
            require(type(a.get('width')) is type(a.get('height')) is type(a.get('fps')) is int and (a['width'],a['height'],a['fps']) == (1080,1920,30)
                    and a.get('video_codec') == 'h264' and a.get('audio_codec') == 'aac' and a.get('decoded') is True, 'video_facts')
            require(type(a.get('duration_seconds')) in (float,int) and math.isfinite(a['duration_seconds']) and 45 <= a['duration_seconds'] <= 90.1, 'video_duration')
        require(set(a) == fields, 'asset_fields')
    require(names == set(expected) and sum(a['size_bytes'] for a in assets) <= MAX_PACKAGE_BYTES, 'package_asset_budget')
    for d in m['deliveries']:
        require(type(d['account_id']) is int and d['account_id'] in ACCOUNTS and isinstance(d['format'], str)
                and d['id'] == f"{d['account_id']}:{d['format']}", 'delivery_identity')
        if d['format'] == 'video':
            required_names = ['summary.mp4']
        else:
            kind = 'bluesky' if d['account_id'] == 125 else 'deck'
            required_names = [f'{kind}-{i:02d}.jpg' for i in range(1,len(m['storyboard'][kind])+1)]
        require(d['asset_names'] == required_names, 'delivery_assets')
        require(d['message'] == m['storyboard']['captions'][str(d['account_id'])][d['format']]['message'], 'delivery_copy')
    checks = m['checks']
    require(isinstance(checks, list) and len(checks) == len(CHECK_NAMES)
            and all(isinstance(c,dict) and set(c) == {'name','exit_code','evidence_sha256'} and text(c['name'],40) for c in checks)
            and {c['name'] for c in checks} == CHECK_NAMES, 'package_checks')
    lookup = {a['name']:a for a in assets}
    require(all(type(c['exit_code']) is int and c['exit_code'] == 0 and checksum(c['evidence_sha256'])
                and lookup['check-'+c['name']+'.json']['sha256'] == c['evidence_sha256'] for c in checks), 'package_checks_failed')
    return loads(json_bytes(value))
