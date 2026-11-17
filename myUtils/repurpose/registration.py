"""Register a verified package in the existing file/media/group tables atomically."""
from __future__ import annotations
from pathlib import Path

from .contracts import json_bytes, require
from .files import verify_package


def register_package(conn, task, manifest_path, moment):
    manifest = verify_package(manifest_path)
    profile = conn.execute('SELECT workspace_id FROM profiles WHERE id=4').fetchone()
    require(profile is not None and (profile['workspace_id'] or 'legacy') == task['workspace_id'], 'teaching_workspace_forbidden')
    workspace = profile['workspace_id']
    root = Path(manifest_path).parent
    cursor = conn.execute('''INSERT INTO media_groups(name,notes,profile_id,group_type,status,workspace_id)
        VALUES(?,?,4,'mixed','review_pending',?)''',
        ('Teaching repurpose ' + manifest['package_version'][:16],
         json_bytes({'owner':'teaching-repurpose','task_id':task['id'],'package_version':manifest['package_version'],'private':True}).decode(),workspace))
    group_id = cursor.lastrowid
    primary = None
    for order, asset in enumerate(manifest['material']['assets']):
        if not asset['mime'].startswith(('image/','video/')):
            continue  # Manuscript, writing rules and check logs remain private-only.
        path = root / asset['name']
        filename = f"teaching-repurpose-{task['id']}-{asset['sha256'][:12]}-{asset['name']}"
        file_id = conn.execute('''INSERT INTO file_records(filename,filesize,file_path,sha256,size_bytes,workspace_id)
            VALUES(?,?,?,?,?,?)''',(filename,asset['size_bytes']/1024**2,str(path),asset['sha256'],asset['size_bytes'],workspace)).lastrowid
        media_type = 'video' if asset['mime']=='video/mp4' else 'image'
        metadata = {'retention_owner':'teaching-repurpose','protected':True,'private':True,'task_id':task['id'],
                    'package_version':manifest['package_version'],'asset_name':asset['name'],
                    'article_provenance':{'page_id':manifest['material']['source']['page_id'],'article_sha256':manifest['material']['source']['article_sha256']}}
        asset_id = conn.execute('''INSERT INTO media_assets(original_filename,media_type,mime_type,local_original_path,local_processed_path,
            file_size,checksum,upload_status,processing_status,width,height,duration_seconds,metadata_json,file_record_id,workspace_id)
            VALUES(?,?,?,?,?,?,?,'private','processed',?,?,?,?,?,?)''',
            (filename,media_type,asset['mime'],str(path),str(path),asset['size_bytes'],asset['sha256'],asset['width'],asset['height'],
             asset.get('duration_seconds',0),json_bytes(metadata).decode(),file_id,workspace)).lastrowid
        role = 'video' if media_type=='video' else 'thumbnail' if asset['kind']=='poster' else 'image'
        conn.execute('INSERT INTO media_group_items(media_group_id,file_record_id,role,sort_order) VALUES(?,?,?,?)',(group_id,file_id,role,order))
        conn.execute('INSERT INTO repurpose_assets(task_id,asset_name,media_asset_id,file_record_id,media_group_id) VALUES(?,?,?,?,?)',
                     (task['id'],asset['name'],asset_id,file_id,group_id))
        if media_type=='video':primary=file_id
    require(primary is not None,'prepared_video_missing')
    conn.execute('UPDATE media_groups SET primary_video_file_id=? WHERE id=?',(primary,group_id))
    return manifest
