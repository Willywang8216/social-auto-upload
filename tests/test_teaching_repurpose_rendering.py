"""Offline equations and malformed v2 inputs. All sources and assets are fixtures."""
import copy
import hashlib
import os
from pathlib import Path

import pytest

from myUtils.repurpose import brand, cards, contracts, writing
from tests.repurpose_fixtures import board, source


def manifest():
    """Shape-only fixture: these are NOT real hashes or validated media bytes."""
    b = board(); assets=[]
    for name,(kind,mime) in contracts.expected_assets(b).items():
        item={'name':name,'kind':kind,'mime':mime,'sha256':'c'*64,'size_bytes':100}
        if mime.startswith('image/'):
            item.update(width=1080,height=1920 if kind in ('video-slide','poster') else 1350)
        elif mime=='video/mp4':
            item.update(width=1080,height=1920,fps=30,duration_seconds=60.0,video_codec='h264',audio_codec='aac',decoded=True)
        assets.append(item)
    plan=[]
    for identifier in contracts.DEFAULT_DELIVERIES:
        account,kind=identifier.split(':');account=int(account)
        names=['summary.mp4'] if kind=='video' else [f"{'bluesky' if account==125 else 'deck'}-{i:02d}.jpg" for i in range(1,5 if account==125 else 7)]
        plan.append(dict(id=identifier,account_id=account,format=kind,asset_names=names,message=b['captions'][str(account)][kind]['message']))
    return contracts.seal_manifest(dict(source=source(),kit_version='a'*64,rules_version='b'*64,storyboard=b,assets=assets,deliveries=plan,
                                       checks=[dict(name=n,exit_code=0,evidence_sha256='c'*64) for n in sorted(contracts.CHECK_NAMES)]))


def reseal(value):
    value['package_version']=contracts.digest({k:v for k,v in value.items() if k!='package_version'})
    return value


@pytest.mark.parametrize('plan',[[{},'58:cards','61:cards','125:cards'],[[],None,43,True]])
def test_malformed_destinations_are_contract_errors(plan):
    with pytest.raises(contracts.RepurposeError):contracts.validate_deliveries(plan)


@pytest.mark.parametrize('path,value',[(('deliveries',),[None]*6),(('deliveries',0,'asset_names'),[{}]),(('checks',),[None]*5),
                                     (('checks',0,'name'),{}),(('assets',0,'name'),[]),(('assets',0,'width'),True),
                                     (('assets',0,'mime'),{}),(('storyboard','deck',0,'role'),{}),
                                     (('storyboard','deck',0,'source_refs',0,'paragraph_id'),[])])
def test_malformed_material_never_raises_internal_type_error(path,value):
    data=manifest();pointer=data['material']
    for key in path[:-1]:pointer=pointer[key]
    pointer[path[-1]]=value
    with pytest.raises(contracts.RepurposeError):contracts.validate_manifest(reseal(data))


def test_failed_duplicate_checks_cannot_be_hidden():
    data=manifest();data['material']['checks'].append({**data['material']['checks'][0],'exit_code':1})
    with pytest.raises(contracts.RepurposeError,match='package_checks'):contracts.validate_manifest(reseal(data))


def test_wrong_card_order_and_missing_poster_refuse():
    data=manifest();data['material']['deliveries'][0]['asset_names'].reverse()
    with pytest.raises(contracts.RepurposeError,match='delivery_assets'):contracts.validate_manifest(reseal(data))
    data=manifest();data['material']['assets']=[a for a in data['material']['assets'] if a['kind']!='poster']
    with pytest.raises(contracts.RepurposeError,match='asset_count'):contracts.validate_manifest(reseal(data))


def test_source_rejects_integer_hash_and_surrogate():
    data=source();data['article_version']=int('1'*64)
    with pytest.raises(contracts.RepurposeError):contracts.validate_source(data)
    data=source();data['manuscript']='\ud800'
    with pytest.raises(contracts.RepurposeError):contracts.validate_source(data)


def test_numbers_signs_and_units_do_not_use_substring_evidence():
    data=board();data['deck'][0]['lines']=['有 98 個。'];data['deck'][0]['source_refs'][0]['quote']='有 198 個。'
    with pytest.raises(contracts.RepurposeError,match='unsupported_number'):writing.check_fidelity(data)
    data=board();data['deck'][0]['lines']=['有 -30 個。'];data['deck'][0]['source_refs'][0]['quote']='有 30 個。'
    with pytest.raises(contracts.RepurposeError,match='unsupported_number'):writing.check_fidelity(data)


def test_selected_font_is_traditional_chinese():
    kit=brand.default_kit()
    for ref in kit['fonts'].values():
        assert 'TC' in brand.load_font(ref,48).getname()[0].upper().replace(' ','')
        assert type(ref['index']) is int


def test_raw_latex_is_never_visible_fallback(tmp_path):
    slide=board()['deck'][0];slide['lines']=[r'\frac{1}{2}']
    with pytest.raises(contracts.RepurposeError,match='raw_latex_without_delimiter'):
        cards.render_card(slide,1,6,brand.default_kit(),tmp_path/'bad.png')
    assert not (tmp_path/'bad.png').exists()


def test_actual_offline_fraction_and_equation_card(tmp_path):
    assets=os.environ.get('REPURPOSE_TEST_KATEX_ROOT')
    if not assets:pytest.skip('Pinned local KaTeX resource fixture must be configured')
    with cards.FormulaRenderer(assets) as renderer:
        equation=renderer.render(r'\frac{x^2+1}{2}',color='#faf7f1',maximum_width=912)
        assert equation.mode=='RGBA' and 10<equation.width<912 and 10<equation.height<700
        assert equation.getbbox() is not None
        slide=board()['deck'][0];slide['lines']=[r'代入算式。$\frac{x^2+1}{2}$']
        facts=cards.render_card(slide,1,6,brand.default_kit(),tmp_path/'equation.png',formulas=renderer)
        assert facts['formula_renderer']=='katex-offline' and facts['bottom_used']<=facts['safe_area_bottom']
        for unsafe in (r'\href{https://example.invalid}{x}',r'\htmlClass{bad}{x}',r'\def\x{x}\x'):
            with pytest.raises(contracts.RepurposeError,match='unsafe_formula'):
                renderer.render(unsafe,color='#faf7f1',maximum_width=912)
        with pytest.raises(contracts.RepurposeError,match='formula_overflow'):
            renderer.render('x+'*90+'x',color='#faf7f1',maximum_width=200)
    assert renderer.manager is renderer.browser is None
