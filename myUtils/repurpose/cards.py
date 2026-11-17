"""Measured CJK layout with offline KaTeX formula composition; no generated artwork."""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .brand import PALETTES, validate_kit, load_font
from .contracts import require
from .visuals import card_spec, measured_wrap


class FormulaRenderer:
    def __init__(self, assets_root=None):
        root = Path(assets_root) if assets_root else Path(__file__).parent / 'katex'
        if not root.is_dir() and assets_root is None:
            root = Path(__file__).resolve().parents[2] / 'sau_frontend/node_modules/katex/dist'
        require(root.is_dir() and (root / 'katex.min.js').is_file(), 'formula_renderer_not_installed')
        self.script = (root / 'katex.min.js').read_text(encoding='utf-8')
        css = (root / 'katex.min.css').read_text(encoding='utf-8')
        def font(match):
            name = match.group(1).strip('"\'')
            path = root / name
            require(path.resolve().is_relative_to(root.resolve()) and path.is_file() and path.stat().st_size < 1024 * 1024, 'formula_font')
            mime = {'.woff2':'font/woff2','.woff':'font/woff','.ttf':'font/ttf'}.get(path.suffix)
            require(mime is not None, 'formula_font_type')
            return 'url(data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode() + ')'
        self.css = re.sub(r'url\(([^)]+)\)', font, css)
        self.manager = self.browser = None

    def __enter__(self):
        from patchright.sync_api import sync_playwright
        self.manager = sync_playwright().start()
        try:
            self.browser = self.manager.chromium.launch(headless=True, timeout=30000)
        except Exception:
            self.manager.stop()
            self.manager = None
            raise
        return self

    def __exit__(self, *_):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.manager:
                self.manager.stop()
            self.manager = self.browser = None

    def render(self, latex, *, color, maximum_width, font_size=50):
        require(isinstance(latex, str) and 0 < len(latex) <= 1000 and not re.search(r'\\(?:href|url|html\w*|includegraphics|require|def|gdef|newcommand)\b', latex), 'unsafe_formula')
        require(isinstance(color,str) and re.fullmatch(r'#[a-fA-F0-9]{6}',color) and type(font_size) is int and 24 <= font_size <= 96
                and type(maximum_width) is int and 1 <= maximum_width <= 1000 and self.browser is not None, 'formula_layout')
        page = self.browser.new_page(viewport={'width': 1080, 'height': 1000}, device_scale_factor=1)
        try:
            page.route('**/*', lambda route: route.abort())
            page.set_content('<!doctype html><meta charset="utf-8"><style>' + self.css +
                             'body{margin:0;background:transparent}#math{display:inline-block;padding:10px;font-size:' + str(font_size) + 'px;color:' + color + '}</style><div id="math"></div>')
            # Patchright evaluates in an isolated world. Run the trusted renderer
            # and its call in the same script world, then observe only DOM flags.
            command = self.script + '\n;(() => {const node=document.getElementById("math");try{window.katex.render(' + json.dumps(latex) + ',node,{displayMode:true,throwOnError:true,trust:false,strict:"error",maxExpand:100,maxSize:10});document.fonts.ready.then(()=>{node.dataset.ready="yes";});}catch(e){node.dataset.error="yes";node.dataset.ready="yes";}})();'
            page.add_script_tag(content=command)
            page.wait_for_function('document.getElementById("math").dataset.ready === "yes"',timeout=30000)
            require(page.locator('#math').get_attribute('data-error') is None, 'invalid_formula')
            box = page.locator('#math').bounding_box()
            require(box and 0 < box['width'] <= maximum_width and box['height'] <= 700, 'formula_overflow')
            raw = page.locator('#math').screenshot(omit_background=True)
            return Image.open(io.BytesIO(raw)).convert('RGBA')
        finally:
            page.close()


def wrap(draw, text, font, width):
    """Shared measured wrapping; invalid glyphs hold the prepared package."""
    try:
        return measured_wrap(draw, text, font, width)
    except ValueError as exc:
        require(False, str(exc))


def glyphs(font, text):
    missing = font.getmask('\uffff')
    signature = missing.size, bytes(missing)
    for char in set(text):
        if char.isspace():
            continue
        mask = font.getmask(char)
        require((mask.size, bytes(mask)) != signature, 'text_glyph_missing')


def render_card(slide, index, count, kit, output, *, video=False, formulas=None):
    validate_kit(kit)
    try:
        spec = card_spec(slide['role'], video=video)
    except (KeyError, ValueError):
        require(False, 'visual_role')
    width, height = spec['width'], spec['height']
    colors = PALETTES[kit['theme']]
    image = Image.new('RGB', (width, height), colors['background'])
    draw = ImageDraw.Draw(image)
    serif = load_font(kit['fonts']['serif'], spec['title_size'])
    body = load_font(kit['fonts']['sans'], spec['body_size'])
    small = load_font(kit['fonts']['sans'], spec['meta_size'])
    margin, y, bottom = spec['margin'], spec['top'], spec['bottom']
    if spec['treatment'] == 'example-rule':
        draw.rectangle((margin-20, y-20, width-margin+20, bottom+20), outline=colors['accent'], width=2)
    elif spec['treatment'] == 'action-rule':
        draw.rectangle((margin, y-38, margin+180, y-30), fill=colors['accent'])
    draw.rectangle((0, 0, 16, height), fill=colors['accent'])
    header = '威威學長 · 摘要影片' if video else '威威學長 · 教學筆記'
    glyphs(small, header + kit['attribution'])
    draw.text((margin, 100 if not video else 260), header, font=small, fill=colors['accent'])
    draw.text((width - margin - 100, 100 if not video else 260), f'{index}/{count}', font=small, fill=colors['muted'])
    require('$' not in slide['title'] and '\\' not in slide['title'], 'formula_title_requires_plain_label')
    glyphs(serif, slide['title'])
    for row in wrap(draw, slide['title'], serif, width - 2 * margin):
        require(y + 100 <= bottom, 'title_overflow')
        draw.text((margin, y), row, font=serif, fill=colors['text'])
        y += 100
    y += 30
    for line in slide['lines']:
        require(line.count('$') % 2 == 0, 'formula_unclosed')
        for block in re.split(r'(\$[^$]+\$)', line):
            if not block:
                continue
            if block.startswith('$'):
                require(formulas is not None, 'formula_renderer_required')
                formula = formulas.render(block[1:-1], color=colors['text'], maximum_width=width - 2 * margin)
                require(y + formula.height <= bottom, 'formula_safe_area')
                image.paste(formula, (margin, y), formula)
                y += formula.height + 18
            else:
                require(not re.search(r'\\(?:frac|sqrt|sum|int|begin|alpha|beta|pi|left|right|cdot|times|mathbb|mathrm|theta)\b',block), 'raw_latex_without_delimiter')
                glyphs(body, block)
                for row in wrap(draw, block, body, width - 2 * margin):
                    require(y + 70 <= bottom, 'body_overflow')
                    draw.text((margin, y), row, font=body, fill=colors['text'])
                    y += 70
        y += 18
    draw.text((margin, height - 135), kit['attribution'], font=small, fill=colors['accent'])
    if kit['logo']:
        with Image.open(kit['logo']['path']) as mark:
            mark = mark.convert('RGBA')
            mark.thumbnail((230, 100), Image.Resampling.LANCZOS)
            image.paste(mark, (width - margin - mark.width, height - 145), mark)
    output = Path(output)
    require(not output.exists(), 'render_path_exists')
    if output.suffix == '.jpg':
        image.save(output, 'JPEG', quality=90, subsampling=0, optimize=True)
    else:
        image.save(output, 'PNG')
    from .files import sync_file
    sync_file(output)
    with Image.open(output) as verify:
        verify.load()
        require(verify.size == (width, height), 'render_dimensions')
    return {'width': width, 'height': height, 'bottom_used': y, 'safe_area_bottom': bottom,
            'cjk_glyphs_verified': True, 'formula_renderer': 'katex-offline' if formulas else None, 'layout': spec}
