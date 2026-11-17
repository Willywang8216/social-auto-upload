"""Preparation orchestrator. Produces exact assets, never approves or publishes."""
from __future__ import annotations

import contextlib
import hashlib
import os
import time
from pathlib import Path

from .brand import validate_kit
from .cards import FormulaRenderer, render_card
from .contracts import ACCOUNTS, asset_descriptor, digest, json_bytes, require, seal_manifest, validate_deliveries, validate_source
from .files import private_path, store_manifest, write_new
from .video import render_video, timing
from .writing import Rules, generate_storyboard


def check_capacity(root, db_path, *, budget=256 * 1024 * 1024):
    from myUtils.media_restore import check_capacity as admit
    # Includes staging/outputs and SQLite headroom; never prunes to admit work.
    admit(Path(root) / 'capacity', db_path=Path(db_path), input_bytes=budget, reserve_bytes=2 * 1024**3)
    if Path('/proc/meminfo').exists():
        available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))) * 1024
        require(available >= 4 * 1024**3, 'repurpose_ram_reserve')
    elif os.name == 'nt':
        import ctypes
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [(name, ctypes.c_ulonglong) for name in ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
        memory = Memory(); memory.length = ctypes.sizeof(Memory)
        require(ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)) and memory.available >= 4 * 1024**3, 'repurpose_ram_reserve')
    else:
        require(False, 'ram_reserve_unobservable')


def produce(source, kit, root, *, rules_root, db_path, deliveries=None, generate=None, checkpoint=None, deadline_seconds=1800):
    from myUtils import image_compliance, media_compliance, text_limits
    require(type(deadline_seconds) is int and 1 <= deadline_seconds <= 1800, 'preparation_deadline_bound')
    deadline = time.monotonic() + deadline_seconds
    def check():
        require(time.monotonic() < deadline, 'preparation_deadline_exceeded')
        if checkpoint is not None:
            checkpoint()
    check()
    source, kit = validate_source(source), validate_kit(kit)
    deliveries = validate_deliveries(deliveries if deliveries is not None else ['43:cards','43:video','58:cards','58:video','61:cards','125:cards'])
    root = private_path(root)
    require(root.parent.is_dir() and not root.exists(), 'preparation_output_must_be_new')
    check_capacity(root.parent, db_path)
    root.mkdir(mode=0o700)
    write_new(root / 'manuscript.md', source['manuscript'].encode('utf-8'))
    rules = Rules(rules_root)
    board, checks = generate_storyboard(source, rules, root, generate=generate, checkpoint=check)
    for account, drafts in board['captions'].items():
        for draft in drafts.values():
            text_limits.validate_post_text({'draft': {'message': draft['message']}}, ACCOUNTS[int(account)])
    seconds = timing(board['deck'])
    assets, layouts, frames = [], [], []
    needs_formulas = any('$' in line for name in ('deck', 'bluesky') for slide in board[name] for line in slide['lines'])
    with FormulaRenderer() if needs_formulas else contextlib.nullcontext() as formulas:
        for name in ('deck', 'bluesky'):
            slides = board[name]
            for index, slide in enumerate(slides, 1):
                for suffix, mime in (('png', 'image/png'), ('jpg', 'image/jpeg')):
                    check()
                    path = root / f'{name}-{index:02d}.{suffix}'
                    layout = render_card(slide, index, len(slides), kit, path, formulas=formulas)
                    layouts.append(layout)
                    assets.append(asset_descriptor(path, root=root, kind=name, mime=mime, width=layout['width'], height=layout['height']))
        for index, slide in enumerate(board['deck'], 1):
            path = root / f'video-slide-{index:02d}.png'
            check()
            layout = render_card(slide, index, len(board['deck']), kit, path, formulas=formulas, video=True)
            layouts.append(layout)
            assets.append(asset_descriptor(path, root=root, kind='video-slide', mime='image/png', width=1080, height=1920))
            frames.append(path)
    video_path = root / 'summary.mp4'
    from PIL import Image
    from .files import sync_file
    poster_path = root / 'summary-poster.jpg'
    with Image.open(frames[0]) as poster:
        poster.convert('RGB').save(poster_path, 'JPEG', quality=90, subsampling=0, optimize=True)
    sync_file(poster_path)
    assets.append(asset_descriptor(poster_path, root=root, kind='poster', mime='image/jpeg', width=1080, height=1920))
    check()
    video_facts = render_video(frames, seconds, video_path)
    check()
    assets.append(asset_descriptor(video_path, root=root, kind='summary', mime='video/mp4', **video_facts))
    companion = '\n\n'.join(s['title'] + '\n' + '\n'.join(s['lines']) for s in board['deck'])
    write_new(root / 'summary.txt', ('摘要影片\n\n' + companion + '\n\nhttps://iamwillywang.com/post/' + source['slug']).encode('utf-8'))
    assets.append(asset_descriptor(root / 'summary.txt', root=root, kind='companion', mime='text/plain'))
    plan = []
    for identifier in deliveries:
        account, kind = identifier.split(':'); account = int(account)
        if kind == 'cards':
            deck = 'bluesky' if account == 125 else 'deck'
            count = len(board[deck])
            names = [f'{deck}-{i:02d}.jpg' for i in range(1, count + 1)]
            for name in names:
                image_compliance.validate_image(root / name, platform=ACCOUNTS[account])
        else:
            names = ['summary.mp4']
            media_compliance.validate_video(video_path, platform=ACCOUNTS[account])
        check()
        plan.append({'id': identifier, 'account_id': account, 'format': kind, 'asset_names': names,
                     'message': board['captions'][str(account)][kind]['message']})
    checks += [{'name': 'layout', 'exit_code': 0, 'evidence_sha256': digest(layouts)},
               {'name': 'video-decode', 'exit_code': 0, 'evidence_sha256': digest(video_facts)}]
    for gate in checks:
        path = root / ('check-' + gate['name'] + '.json')
        write_new(path, json_bytes(gate))
        descriptor = asset_descriptor(path, root=root, kind='check', mime='application/json')
        assets.append(descriptor)
        gate['evidence_sha256'] = descriptor['sha256']
    validate_kit(kit); rules.verify()
    manifest = seal_manifest({'source': source, 'kit_version': digest(kit), 'rules_version': rules.version,
                              'storyboard': board, 'deliveries': plan, 'assets': assets, 'checks': checks})
    check()
    return store_manifest(root, manifest)


def run_one(store, rules_root, *, generate=None):
    from .contracts import loads, RepurposeError
    task = store.claim()
    if task is None:
        return None
    request = loads(task['request_json'])
    path = store.root / ('task-' + str(task['id']) + '-' + hashlib.sha256(task['lease_token'].encode()).hexdigest()[:16])
    try:
        manifest_path = produce(request['source'], store.kit(task['workspace_id'], request['kit_version']), path,
                                rules_root=rules_root, db_path=store.db_path, deliveries=request['deliveries'], generate=generate,
                                checkpoint=lambda: store.assert_lease(task))
        store.finish(task, manifest_path=manifest_path)
    except Exception as exc:
        reason = exc.code if isinstance(exc, RepurposeError) else 'preparation_failed'
        # No raw model/provider/config exception is exposed by the operational API.
        try:
            store.finish(task, reason=reason)
        except RepurposeError as lost:
            if lost.code != 'lease_lost':
                raise
            # Cancellation/expiry is observed, never reported as successful
            # preparation. The daemon may safely continue to the next task.
    return store.task(task['workspace_id'], task['id'])
