"""The reader without a network: image preparation, answer parsing, the
repair round against a scripted provider, and the provider factory."""
import io
import json
import os

import pytest

pytest.importorskip('PIL')

from stl_to_solid.blueprint.providers import Provider, ReadError, make_provider
from stl_to_solid.blueprint.read_drawing import parse_recipe_text, prepare_image, read_drawing

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'sg90_recipe.json')


def _png_bytes(size=(2400, 1200), mode='RGB'):
    from PIL import Image
    b = io.BytesIO()
    Image.new(mode, size, 'white').save(b, 'PNG')
    return b.getvalue()


def test_prepare_image_downscales_and_keeps_png():
    from PIL import Image
    data, media = prepare_image(_png_bytes())
    assert media == 'image/png'
    im = Image.open(io.BytesIO(data))
    assert max(im.size) == 1568
    data, media = prepare_image(_png_bytes((400, 300), 'RGBA'))
    assert Image.open(io.BytesIO(data)).mode == 'RGB'
    with pytest.raises(ReadError):
        prepare_image(_png_bytes((100, 100)))
    with pytest.raises(ReadError):
        prepare_image(b'not an image at all')


def test_prepare_image_jpeg_stays_jpeg():
    from PIL import Image
    b = io.BytesIO()
    Image.new('RGB', (800, 600), 'white').save(b, 'JPEG')
    _, media = prepare_image(b.getvalue())
    assert media == 'image/jpeg'


def test_parse_recipe_text():
    assert parse_recipe_text('{"a": 1}') == {'a': 1}
    assert parse_recipe_text('```json\n{"a": 1}\n```') == {'a': 1}
    assert parse_recipe_text('Here it is:\n{"a": {"b": [1]}}\nDone.') == {'a': {'b': [1]}}
    with pytest.raises(ReadError):
        parse_recipe_text('no json here')


class Scripted(Provider):
    """Answers in order; records what it was asked."""
    def __init__(self, answers):
        super().__init__('k', 'm')
        self.answers = list(answers)
        self.calls = []

    def complete(self, system, image_bytes, media_type, text, schema, history=(), effort='high'):
        self.calls.append({'system': system, 'text': text, 'history': list(history), 'media': media_type,
                           'schema': schema, 'effort': effort})
        return self.answers.pop(0), {'input_tokens': 100, 'output_tokens': 50}, 'scripted-1'


def test_read_drawing_good_answer_no_repair():
    with open(FIX) as f:
        rec = f.read()
    p = Scripted([rec])
    out = read_drawing(_png_bytes((800, 600)), 'anthropic', 'k', client=p, hints='the top view has the boss')
    assert out['validation']['errors'] == [] and not out['repaired']
    assert out['usage'] == {'input_tokens': 100, 'output_tokens': 50, 'calls': 1}
    assert out['model'] == 'scripted-1' and out['provider'] == 'anthropic'
    assert out['recipe']['params'][0]['name'] == 'body_w'
    assert 'the top view has the boss' in p.calls[0]['text']
    assert p.calls[0]['effort'] == 'high' and out['effort'] == 'high'
    p2 = Scripted([rec])
    out2 = read_drawing(_png_bytes((800, 600)), 'openai', 'k', client=p2, effort='medium')
    assert p2.calls[0]['effort'] == 'medium' and out2['effort'] == 'medium'
    assert 'Origin = the minimum corner' in p.calls[0]['system']
    assert p.calls[0]['schema']['type'] == 'object'


def test_read_drawing_repairs_once():
    with open(FIX) as f:
        good = json.load(f)
    bad = json.loads(json.dumps(good))
    bad['overall']['w'] = 50
    p = Scripted([json.dumps(bad), json.dumps(good)])
    out = read_drawing(_png_bytes((800, 600)), 'openai', 'k', client=p)
    assert out['repaired'] and out['validation']['errors'] == []
    assert out['usage']['calls'] == 2
    hist = p.calls[1]['history']
    assert hist[0][0] == 'assistant' and hist[1][0] == 'user'
    assert 'along X' in hist[1][1] and 'corrected recipe' in hist[1][1]


def test_read_drawing_keeps_the_better_answer_when_repair_is_worse():
    with open(FIX) as f:
        good = json.load(f)
    bad = json.loads(json.dumps(good))
    bad['overall']['w'] = 50
    worse = json.loads(json.dumps(bad))
    worse['features'][0]['op'] = 'cut'
    p = Scripted([json.dumps(bad), json.dumps(worse)])
    out = read_drawing(_png_bytes((800, 600)), 'openai', 'k', client=p, repair=True)
    assert out['recipe']['overall']['w'] == 50 and len(out['validation']['errors']) == 1


def test_read_drawing_unparseable_answer():
    p = Scripted(['I cannot see the image.'])
    with pytest.raises(ReadError):
        read_drawing(_png_bytes((800, 600)), 'openai', 'k', client=p, repair=False)


def test_make_provider():
    assert make_provider('anthropic', 'k', 'claude-opus-5').name == 'anthropic'
    p = make_provider('deepseek', 'k', 'deepseek-flash', 'https://api.deepseek.com')
    assert p.name == 'deepseek' and p.base_url == 'https://api.deepseek.com'
    assert make_provider('custom', 'k', 'llava', 'http://localhost:11434/v1').name == 'openai'
    with pytest.raises(ReadError):
        make_provider('gemini', 'k', 'x')


class _FakeOpenAI:
    """Stands in for openai.OpenAI: records each chat request, answers `reply`."""
    requests = []
    reply = '{"ok": true}'

    def __init__(self, **kw):
        self.chat = self
        self.completions = self

    def create(self, **kw):
        _FakeOpenAI.requests.append(kw)
        from types import SimpleNamespace as NS
        msg = NS(content=_FakeOpenAI.reply, refusal=None)
        return NS(choices=[NS(message=msg, finish_reason='stop')], model=kw['model'],
                  usage=NS(prompt_tokens=10, completion_tokens=5))


@pytest.fixture
def fake_openai(monkeypatch):
    openai = pytest.importorskip('openai')
    _FakeOpenAI.requests = []
    _FakeOpenAI.reply = '{"ok": true}'
    monkeypatch.setattr(openai, 'OpenAI', _FakeOpenAI)
    return _FakeOpenAI


@pytest.mark.parametrize('model', ['deepseek-flash', 'deepseek-v4-pro'])
@pytest.mark.parametrize('effort,sent', [('low', 'low'), ('medium', 'high'), ('high', 'max')])
def test_deepseek_thinking_and_json(fake_openai, model, effort, sent):
    from stl_to_solid.blueprint.providers import DEEPSEEK_MAX_TOKENS
    p = make_provider('deepseek', 'k', model, 'https://api.deepseek.com')
    out, _, _ = p.complete('SYS', _png_bytes((80, 60)), 'image/png', 'read it', {'type': 'object'},
                           effort=effort)
    assert out == '{"ok": true}'
    (req,) = fake_openai.requests
    assert req['extra_body'] == {'thinking': {'type': 'enabled'}}
    assert req['reasoning_effort'] == sent
    assert req['response_format'] == {'type': 'json_object'}
    assert req['max_tokens'] == DEEPSEEK_MAX_TOKENS
    system = req['messages'][0]['content']
    assert system.startswith('SYS') and 'json' in system and '{"type":"object"}' in system
    image = req['messages'][1]['content'][0]['image_url']
    assert image['detail'] == 'original' and image['url'].startswith('data:image/png;base64,')


def test_deepseek_other_model_gets_no_thinking(fake_openai):
    p = make_provider('deepseek', 'k', 'deepseek-legacy', 'https://api.deepseek.com')
    p.complete('SYS', _png_bytes((80, 60)), 'image/png', 'read it', {'type': 'object'})
    (req,) = fake_openai.requests
    assert 'extra_body' not in req and 'reasoning_effort' not in req
    assert req['response_format'] == {'type': 'json_object'}


def test_openai_request_unchanged(fake_openai):
    p = make_provider('openai', 'k', 'gpt-5.5')
    p.complete('SYS', _png_bytes((80, 60)), 'image/png', 'read it', {'type': 'object'}, effort='medium')
    (req,) = fake_openai.requests
    assert req['response_format']['type'] == 'json_schema' and req['reasoning_effort'] == 'medium'
    assert 'extra_body' not in req and 'max_tokens' not in req
    assert req['messages'][0]['content'] == 'SYS'
    assert 'detail' not in req['messages'][1]['content'][0]['image_url']


def test_empty_answer_is_a_readable_error(fake_openai):
    fake_openai.reply = '  '
    p = make_provider('deepseek', 'k', 'deepseek-flash', 'https://api.deepseek.com')
    with pytest.raises(ReadError, match='empty answer'):
        p.complete('SYS', _png_bytes((80, 60)), 'image/png', 'read it', {'type': 'object'})
