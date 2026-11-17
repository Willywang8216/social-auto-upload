"""Synthetic local-only repurpose fixtures. Never submit these to a public channel."""
import copy
import hashlib

from myUtils.repurpose.contracts import DATABASE_ID, DEFAULT_DELIVERIES, paragraphs


def source():
    manuscript = ('---\ntitle: 函數的輸入\nslug: teaching-queue-test-repurpose\ncategory: math\n'
                  'date: 2026.11.16\nreadtime: 3\n---\n\n'
                  '函數有允許輸入的範圍。定義域，就是允許輸入的集合。先確認條件，再代入算式。\n\n'
                  '每個允許輸入的值，對應一個輸出。這個條件不表示所有輸入都得到相同的輸出。\n\n'
                  '試著自己列出允許的輸入，檢查算式是否有不能代入的值。\n\n'
                  '在條件成立時，$f(x)=2x+1$。代入 $x=1$，得到 $f(1)=3$。')
    return {'database_id': DATABASE_ID, 'page_id': '00000000-0000-4000-8000-000000000004',
            'slug': 'teaching-queue-test-repurpose', 'title': '函數的輸入', 'article_version': 'a' * 64,
            'article_sha256': hashlib.sha256(manuscript.encode()).hexdigest(), 'manuscript': manuscript}


def board(article=None):
    article = article or source()
    blocks = paragraphs(article)
    def slide(identifier, role, title, line, ref):
        return {'id': identifier, 'role': role, 'title': title, 'lines': [line],
                'source_refs': [{'paragraph_id': blocks[ref]['id'], 'quote': blocks[ref]['text']}]}
    deck = [slide('deck-first', 'title', '函數的輸入', '函數有允許輸入的範圍。', 0),
            slide('deck-domain', 'explanation', '允許的集合', '定義域，就是允許輸入的集合。', 0),
            slide('deck-condition', 'explanation', '先確認條件', '先確認條件，再代入算式。', 0),
            slide('deck-output', 'explanation', '輸入和輸出', '每個允許輸入的值，對應一個輸出。', 1),
            slide('deck-caution', 'example', '輸出可能不同', '這個條件不表示所有輸入都得到相同的輸出。', 1),
            slide('deck-action', 'action', '自己試一次', '試著自己列出允許的輸入。', 2)]
    bsky = [slide('bsky-first', 'title', '看看輸入', '函數有允許輸入的範圍。', 0),
            slide('bsky-condition', 'explanation', '定義域', '定義域，就是允許輸入的集合。', 0),
            slide('bsky-output', 'example', '看看輸出', '每個允許輸入的值，對應一個輸出。', 1),
            slide('bsky-action', 'action', '檢查算式', '檢查算式是否有不能代入的值。', 2)]
    captions = {str(a): {kind: {'message': ('這段摘要影片談函數的輸入。' if kind == 'video' else '函數有允許輸入的範圍。') + '\nhttps://iamwillywang.com/post/' + article['slug'],
                               'source_refs': [{'paragraph_id': blocks[0]['id'], 'quote': blocks[0]['text']}]}
                       for kind in ('cards', 'video')} for a in (43, 58, 61, 125)}
    return {'deck': deck, 'bluesky': bsky, 'video_slide_ids': [s['id'] for s in deck], 'captions': captions}


def produce_verified_fixture(tmp_path_factory):
    """Local-only real PNG/JPEG/MP4 fixture; injected LLM, no external publication."""
    import json
    import os
    from pathlib import Path
    from types import SimpleNamespace
    import pytest
    from db.createTable import bootstrap
    from myUtils.repurpose import pipeline
    from myUtils.repurpose.brand import default_kit
    from myUtils.repurpose.files import verify_package
    rules = os.environ.get('REPURPOSE_TEST_RULES_ROOT')
    if not rules:
        pytest.skip('Real private rules bundle required; release gates must run with no skips')
    root = tmp_path_factory.mktemp('repurpose-real-media')
    database = root / 'preparation.sqlite3'; bootstrap(database)
    result = board(); calls = 0
    def generate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(content=json.dumps(result if calls <= 2 else {'supported':True,'issues':[]}, ensure_ascii=False))
    path = pipeline.produce(source(), default_kit(), root / 'package', rules_root=Path(rules), db_path=database, generate=generate)
    manifest = verify_package(path)
    assert next(a for a in manifest['material']['assets'] if a['name']=='summary.mp4')['decoded']
    return path, manifest


def prepare_local_store(tmp_path, fixture):
    """Migrate/register real bytes in a fresh DB; exact accounts are test rows only."""
    import shutil
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from db.createTable import bootstrap
    from myUtils import profiles
    from myUtils.repurpose.brand import default_kit
    from myUtils.repurpose.store import Store
    root = tmp_path / 'private'; root.mkdir()
    database = tmp_path / 'jobs.sqlite3'; bootstrap(database)
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO profiles(id,name,slug) VALUES(4,'Teaching local fixture','teaching-local-fixture')")
    for identifier, platform in ((43,'instagram'),(58,'facebook'),(61,'threads'),(125,'bluesky')):
        config = {'accessToken':'local-test-not-a-credential', 'accessTokenExpiresAt':(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),
                  'pageId':'page-1','igUserId':'ig-1','threadUserId':'threads-1',
                  'handle':'fixture.bsky.social','appPassword':'local-test-not-a-credential'}
        account = profiles.add_account(4,platform,'Local fixture '+str(identifier),auth_type='oauth',config=config,db_path=database)
        with sqlite3.connect(database) as conn:
            conn.execute('UPDATE accounts SET id=? WHERE id=?',(identifier,account.id))
    path, manifest = fixture
    target = root / 'package'; shutil.copytree(path.parent,target)
    selected = Store(database,root); version = selected.add_kit('legacy',default_kit())
    task = selected.enqueue('legacy','local-preparation',source(),version)
    lease = selected.claim(); selected.finish(lease,manifest_path=target / path.name)
    media = tmp_path / 'data' / 'publish'; media.mkdir(parents=True)
    return selected, selected.task('legacy',task['id']), manifest, media
