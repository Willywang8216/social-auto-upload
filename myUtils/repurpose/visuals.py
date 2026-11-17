"""Portable Teaching visual interface, extracted from the measured carousel flow.

This contains public layout logic only: no source prose, voice rules or asset paths.
The Teaching checkout keeps an exact copy; legacy rendering opts in explicitly.
"""
from __future__ import annotations
import re

VERSION = 'teaching-visuals-v1'
PALETTES = {
    'ink': {'background': '#1c2330', 'text': '#faf7f1', 'accent': '#c0633f', 'muted': '#b0aaa0'},
    'cream': {'background': '#faf7f1', 'text': '#1c2330', 'accent': '#c0633f', 'muted': '#685f54'},
}
ROLES = {
    'title': {'title_size': 84, 'body_size': 46, 'top': 330, 'treatment': 'title'},
    'explanation': {'title_size': 72, 'body_size': 46, 'top': 260, 'treatment': 'plain'},
    'example': {'title_size': 72, 'body_size': 46, 'top': 260, 'treatment': 'example-rule'},
    'action': {'title_size': 72, 'body_size': 46, 'top': 260, 'treatment': 'action-rule'},
}


def palette(theme):
    if not isinstance(theme, str) or theme not in PALETTES:
        raise ValueError('visual_theme')
    return dict(PALETTES[theme])


def card_spec(role, *, video=False):
    if not isinstance(role, str) or role not in ROLES or type(video) is not bool:
        raise ValueError('visual_role')
    spec = dict(ROLES[role])
    spec.update(version=VERSION, width=1080, height=1920 if video else 1350, margin=84,
                bottom=(1920 if video else 1350)-230, meta_size=32)
    if video:
        spec['top'] += 150
        spec['body_size'] = 50
    return spec


def measured_wrap(draw, text, font, width):
    """Measure real glyphs; keep Latin tokens when possible, split long ones safely.

    Borrowed from the carousel's greedy measured wrapping. Unlike its legacy
    path, this opt-in flow preserves every character and refuses oversized glyphs.
    It never clips, shrinks, truncates, or treats LaTeX as rendered mathematics.
    """
    if not isinstance(text, str) or type(width) is not int or width < 1:
        raise ValueError('visual_wrap_input')
    rows = []
    for paragraph in text.split('\n'):
        current = ''
        chunks = re.findall(r"[A-Za-z0-9$\\{}^_=+\-*/()\[\].,:;!?'\"%]+|\s+|[^A-Za-z0-9\s]", paragraph)
        for chunk in chunks:
            if draw.textlength(chunk, font=font) <= width:
                if current and draw.textlength(current + chunk, font=font) > width:
                    rows.append(current); current = ''
                current += chunk
            else:
                for char in chunk:
                    if draw.textlength(char, font=font) > width:
                        raise ValueError('glyph_overflow')
                    if current and draw.textlength(current + char, font=font) > width:
                        rows.append(current); current = ''
                    current += char
        if current:
            rows.append(current)
    return rows
