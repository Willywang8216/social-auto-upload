"""Readability-timed, silent H.264 summary; bounded encoding and actual decode checks."""
from __future__ import annotations

import math
import os
import shutil
import subprocess
from pathlib import Path

from .contracts import loads, require
from .files import write_new


def timing(slides):
    # Editorial reading budget, not a claim about universal human reading speed.
    times = [max(7, math.ceil((len(s['title']) + sum(len(line) for line in s['lines'])) / 4 + 2)) for s in slides]
    if sum(times) < 45:
        times[-1] += 45 - sum(times)
    require(all(t <= 20 for t in times) and 45 <= sum(times) <= 90, 'summary_reading_time_overflow')
    return times


def tool(name):
    value = shutil.which(name)
    require(value is not None, 'video_tool_missing')
    return value


def run(command, *, timeout):
    return subprocess.run(command, capture_output=True, timeout=timeout, check=False)


def render_video(paths, seconds, output, *, runner=run):
    require(len(paths) == len(seconds) and 6 <= len(paths) <= 8 and 45 <= sum(seconds) <= 90, 'video_plan')
    output = Path(output)
    require(not output.exists(), 'video_path_exists')
    command = [tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-n', '-filter_complex_threads', '2']
    for path, length in zip(paths, seconds):
        require(Path(path).is_file(), 'video_slide_missing')
        command += ['-loop', '1', '-framerate', '30', '-t', str(length), '-i', str(path)]
    command += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
    filters = []
    for index, length in enumerate(seconds):
        filters.append(f'[{index}:v]format=yuv420p,setsar=1,fade=t=in:st=0:d=0.25,fade=t=out:st={length - .25}:d=0.25[v{index}]')
    filters.append(''.join(f'[v{i}]' for i in range(len(paths))) + f'concat=n={len(paths)}:v=1:a=0[out]')
    command += ['-filter_complex', ';'.join(filters), '-map', '[out]', '-map', f'{len(paths)}:a',
                '-c:v', 'libx264', '-profile:v', 'high', '-pix_fmt', 'yuv420p', '-preset', 'fast',
                '-crf', '20', '-r', '30', '-threads', '2', '-c:a', 'aac', '-b:a', '128k',
                '-t', str(sum(seconds)), '-movflags', '+faststart', str(output)]
    result = runner(command, timeout=900)
    require(result.returncode == 0, 'video_encode_failed')
    probe = runner([tool('ffprobe'), '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(output)], timeout=30)
    require(probe.returncode == 0, 'video_probe_failed')
    facts = loads(probe.stdout)
    videos = [stream for stream in facts['streams'] if stream['codec_type'] == 'video']
    audios = [stream for stream in facts['streams'] if stream['codec_type'] == 'audio']
    require(len(videos) == len(audios) == 1 and videos[0]['codec_name'] == 'h264' and videos[0]['pix_fmt'] == 'yuv420p'
            and videos[0]['width'] == 1080 and videos[0]['height'] == 1920 and videos[0]['r_frame_rate'] == '30/1'
            and audios[0]['codec_name'] == 'aac', 'video_format')
    duration = float(facts['format']['duration'])
    require(abs(duration - sum(seconds)) <= .1, 'video_duration_changed')
    decoded = runner([tool('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-xerror', '-threads', '2',
                      '-i', str(output), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'], timeout=300)
    require(decoded.returncode == 0, 'video_decode_failed')
    with output.open('r+b') as stream:
        os.fsync(stream.fileno())
    return {'width': 1080, 'height': 1920, 'fps': 30, 'duration_seconds': duration,
            'video_codec': 'h264', 'audio_codec': 'aac', 'decoded': True}
