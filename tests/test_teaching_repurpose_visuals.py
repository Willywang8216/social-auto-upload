"""Shared visual rules, not publication authority."""
import pytest
from myUtils.repurpose import visuals

class Draw:
    def textlength(self, text, font=None): return len(text) * 10


def test_all_role_specs_have_safe_dimensions_and_readable_floors():
    specs = [visuals.card_spec(role) for role in ('title','explanation','example','action')]
    assert len({s['treatment'] for s in specs}) == 4
    for spec in specs:
        assert (spec['width'], spec['height']) == (1080,1350)
        assert spec['body_size'] >= 46 and spec['title_size'] >= 72
        assert spec['top'] < spec['bottom']
    assert visuals.card_spec('title',video=True)['height'] == 1920
    for bad in ('unknown', None, True):
        with pytest.raises(ValueError): visuals.card_spec(bad)
    with pytest.raises(ValueError): visuals.card_spec('title',video=1)


def test_measured_wrap_preserves_long_tokens_and_conditions_without_clipping():
    text = 'x<=100 條件不能省略（定義域） ABCdefghijklmnop'
    rows = visuals.measured_wrap(Draw(),text,None,50)
    assert ''.join(rows) == text and all(len(row) <= 5 for row in rows)
    with pytest.raises(ValueError,match='glyph_overflow'): visuals.measured_wrap(Draw(),'學',None,5)
    assert visuals.measured_wrap(Draw(),'abc\ndef',None,40) == ['abc','def']


def test_palettes_are_copy_only_and_teaching_scoped():
    modified = visuals.palette('ink');modified['background'] = 'changed'
    assert visuals.palette('ink')['background'] == '#1c2330'
    assert visuals.palette('cream')['background'] == '#faf7f1'
    with pytest.raises(ValueError): visuals.palette('other-brand')
