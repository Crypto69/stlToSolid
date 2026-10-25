"""Read a drawing image into a recipe or a script: prepare the picture,
ask the provider, validate or check the answer, then look-and-fix.

Modes. 'recipe' is the parametric language (validator, table, Fusion
script). 'code' is a CadQuery script for parts the recipe cannot draw
(freeform.py); it gives a STEP and a preview, no Fusion script. 'auto'
asks the model a quick question first (TRIAGE) and picks one.

Rounds. With a `builder` (backend.blueprint.check_builder: the warm
preview helper for a recipe, a one-shot worker for code) the answer is
built and drawn from four sides, and the pictures go back to the model
next to the drawing: list the discrepancies, return the whole answer
corrected. Up to `rounds` such looks; a script that does not run gets up
to FIX_BUDGET text-only repairs besides. The loop stops when the model
says it matches, when a correction repeats or fails more checks than
before, or when the budget is spent. The result carries the answer even
when checks still fail, so the user can fix the numbers or the script."""
import io
import json
import re
import time
from dataclasses import dataclass, field

from .freeform import check_code
from .prompts import (CODE_FAILED_PROMPT, CODE_SYSTEM_PROMPT, CODE_USER_PROMPT, CRITIQUE_CODE_TAIL,
                      CRITIQUE_INTRO, CRITIQUE_RECIPE_TAIL, REPAIR_PROMPT, SYSTEM_PROMPT,
                      TRIAGE_PROMPT, TRIAGE_SYSTEM_PROMPT, USER_PROMPT)
from .providers import ReadError, make_provider
from .schema import (CODE_SCHEMA, CRITIQUE_CODE_SCHEMA, CRITIQUE_RECIPE_SCHEMA, SCHEMA, TRIAGE_SCHEMA,
                     normalize, normalize_freeform)
from .validate import validate

MAX_SIDE = 1568           # px: what the vision models resample to anyway
MIN_SIDE = 200
MAX_INPUT = 20 * 1024 * 1024
MODES = ('auto', 'recipe', 'code')
MAX_ROUNDS = 3
FIX_BUDGET = 2            # repairs for a script that does not run, besides the look rounds


@dataclass
class BuildCheck:
    """What the loop learns from building the current answer."""
    ok: bool
    error: str = ''
    bbox: dict = None
    volume_mm3: float = None
    solids: int = None
    warnings: list = field(default_factory=list)
    renders: list = field(default_factory=list)      # [(view, png_bytes, caption)]


def prepare_image(data):
    """(bytes, media_type): rotated by EXIF, RGB, the longest side at most
    MAX_SIDE px, JPEG (q92) when the source was a JPEG else PNG (line art
    stays crisp)."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    if len(data) > MAX_INPUT:
        raise ReadError(f'The image is larger than {MAX_INPUT // (1 << 20)} MB.', 'image')
    try:
        im = Image.open(io.BytesIO(data))
        fmt = (im.format or '').upper()
        im = ImageOps.exif_transpose(im)
        w, h = im.size
    except UnidentifiedImageError:
        raise ReadError('The file is not an image the server can read (JPEG, PNG or WebP).', 'image')
    except OSError as e:
        raise ReadError(f'The image could not be read ({e}).', 'image')
    if min(w, h) < MIN_SIDE:
        raise ReadError(f'The image is only {w} x {h} pixels; the labels would be unreadable.', 'image')
    if im.mode not in ('RGB', 'L'):
        bg = Image.new('RGB', im.size, 'white')
        try:
            bg.paste(im.convert('RGBA'), mask=im.convert('RGBA').split()[-1])
            im = bg
        except Exception:
            im = im.convert('RGB')
    if max(w, h) > MAX_SIDE:
        s = MAX_SIDE / float(max(w, h))
        im = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    out = io.BytesIO()
    if fmt == 'JPEG':
        im.convert('RGB').save(out, 'JPEG', quality=92)
        return out.getvalue(), 'image/jpeg'
    im.save(out, 'PNG', optimize=True)
    return out.getvalue(), 'image/png'


_FENCE = re.compile(r'^\s*```(?:json)?\s*|\s*```\s*$', re.IGNORECASE)


def parse_recipe_text(text):
    """The JSON object in a model answer (fences and chatter tolerated)."""
    t = _FENCE.sub('', text or '').strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    a, b = t.find('{'), t.rfind('}')
    if a >= 0 and b > a:
        try:
            return json.loads(t[a:b + 1])
        except ValueError:
            pass
    raise ReadError('The model\'s answer was not a JSON recipe.', 'parse')


def _critique_effort(effort):
    """The hard reading is done; a comparison needs less thought."""
    return 'medium' if effort == 'high' else effort


def _render_parts(renders):
    parts = []
    for view, png, caption in renders:
        parts.append(('text', caption))
        parts.append(('image', png, 'image/png'))
    return parts


def _discrepancies(ans):
    out = []
    for d in (ans.get('discrepancies') or []) if isinstance(ans, dict) else []:
        if isinstance(d, dict):
            out.append({k: str(d.get(k) or '') for k in ('where', 'what', 'fix')})
        if len(out) >= 20:
            break
    return out


def _said(disc):
    return '; '.join((d['where'] + ': ' if d['where'] else '') + d['what'] for d in disc[:3])


def read_drawing(image_bytes, provider, api_key, model=None, base_url=None, hints='',
                 timeout=300.0, repair=True, client=None, effort='high', mode='recipe', rounds=0,
                 builder=None, progress=None, allow_code=True):
    """-> {'mode': 'recipe' | 'code', 'recipe': dict | None, 'freeform': dict | None,
    'raw_text', 'usage': {input_tokens, output_tokens, calls}, 'model', 'provider',
    'repaired', 'effort', 'rounds_asked', 'rounds': [per-round records], 'triage',
    'validation': {errors, warnings}, 'seconds'}.
    `client` (a Provider), `builder` (callable(kind, answer) -> BuildCheck) and
    `progress` (callable(line)) are injectable."""
    t0 = time.time()
    img, media = prepare_image(image_bytes)
    p = client or make_provider(provider, api_key, model, base_url, timeout)
    say = progress or (lambda line: None)
    mode = mode if mode in MODES else 'auto'
    rounds = max(0, min(int(rounds or 0), MAX_ROUNDS))
    if mode == 'code' and not allow_code:
        raise ReadError('Writing the part as code is switched off on this server.', 'api')
    hint = f'\n\nNotes from the user: {hints.strip()}' if hints and hints.strip() else ''
    drawing = ('image', img, media)
    usage = {'input_tokens': 0, 'output_tokens': 0, 'calls': 0}
    state = {'model': None}

    def ask(system, parts, schema, eff, history=()):
        out, u, m = p.complete_parts(system, parts, schema, effort=eff, history=history)
        usage['calls'] += 1
        for k in ('input_tokens', 'output_tokens'):
            if u.get(k) is not None:
                usage[k] += int(u[k])
        state['model'] = m
        return out

    triage = None
    if mode == 'auto':
        if not allow_code:
            mode = 'recipe'
        else:
            say('deciding how to build it: as a recipe or as code…')
            try:
                t = parse_recipe_text(ask(TRIAGE_SYSTEM_PROMPT, [drawing, ('text', TRIAGE_PROMPT + hint)],
                                          TRIAGE_SCHEMA, 'low'))
            except ReadError:
                t = {}
            can = bool(t.get('recipe_can_express', True)) if isinstance(t, dict) else True
            triage = {'recipe_can_express': can, 'why': str(t.get('why') or '') if isinstance(t, dict) else '',
                      'units': t.get('units') if isinstance(t, dict) else None}
            mode = 'recipe' if can else 'code'
            say(f'{"a recipe" if can else "code"}: {triage["why"] or "the model gave no reason"}')
    log = []
    if mode == 'recipe':
        out = _read_recipe(ask, drawing, hint, effort, repair, rounds, builder, say, log)
    else:
        out = _read_code(ask, drawing, hint, effort, rounds, builder, say, log)
    out.update({'mode': mode, 'usage': usage, 'model': state['model'], 'provider': provider,
                'effort': effort, 'rounds_asked': rounds, 'rounds': log, 'triage': triage,
                'seconds': round(time.time() - t0, 1)})
    return out


def _key(recipe):
    return json.dumps(recipe, sort_keys=True)


def _read_recipe(ask, drawing, hint, effort, repair, rounds, builder, say, log):
    text = USER_PROMPT + hint
    say('reading the drawing into a recipe…')
    raw = ask(SYSTEM_PROMPT, [drawing, ('text', text)], SCHEMA, effort)
    recipe = normalize(parse_recipe_text(raw))
    rep = validate(recipe)
    repaired = False
    if rep.errors and repair:
        say(f'{len(rep.errors)} check(s) failed; asking for a repair…')
        errs = '\n'.join(f'- {e}' for e in rep.errors[:12])
        raw2 = ask(SYSTEM_PROMPT, [drawing, ('text', text)], SCHEMA, effort,
                   history=(('assistant', raw), ('user', REPAIR_PROMPT.format(errors=errs))))
        try:
            recipe2 = normalize(parse_recipe_text(raw2))
        except ReadError:
            recipe2 = None
        if recipe2 is not None:
            rep2 = validate(recipe2)
            # keep the better of the two
            if len(rep2.errors) <= len(rep.errors):
                recipe, rep, raw, repaired = recipe2, rep2, raw2, True
    seen = {_key(recipe)}
    for k in range(1, rounds + 1):
        if builder is None or rep.errors:
            break
        say(f'check {k} of {rounds}: building the part and drawing its views…')
        chk = builder('recipe', recipe)
        entry = {'round': k, 'kind': 'look'}
        if not chk.ok:
            entry['build_error'] = chk.error
            log.append(entry)
            say(f'check {k}: the build failed ({chk.error}); stopping the checks')
            break
        entry['built'] = {'size': chk.bbox['size'] if chk.bbox else None, 'solids': chk.solids}
        tail = CRITIQUE_RECIPE_TAIL.format(recipe=json.dumps(recipe, separators=(',', ':')))
        parts = [drawing] + _render_parts(chk.renders) + [('text', CRITIQUE_INTRO + tail + hint)]
        say(f'check {k} of {rounds}: comparing the views with the drawing…')
        try:
            raw3 = ask(SYSTEM_PROMPT, parts, CRITIQUE_RECIPE_SCHEMA, _critique_effort(effort))
            ans = parse_recipe_text(raw3)
        except ReadError as e:
            entry['error'] = str(e)
            log.append(entry)
            say(f'check {k}: no usable answer ({e}); keeping the recipe as it is')
            break
        disc = _discrepancies(ans)
        entry['discrepancies'] = disc
        entry['done'] = bool(ans.get('done'))
        if ans.get('done') or not isinstance(ans.get('recipe'), dict):
            entry['done'] = True
            log.append(entry)
            say(f'check {k}: the model finds the build matches the drawing'
                + (f' ({_said(disc)})' if disc else ''))
            break
        new = normalize(ans['recipe'])
        rep2 = validate(new)
        if len(rep2.errors) > len(rep.errors):
            entry['rejected'] = (f'the corrected recipe failed {len(rep2.errors)} check(s): '
                                 + '; '.join(rep2.errors[:3]))
            log.append(entry)
            say(f'check {k}: the correction failed more checks than before; keeping the earlier recipe')
            break
        key = _key(new)
        if key in seen:
            entry['rejected'] = 'the correction repeats an earlier recipe'
            entry['done'] = True
            log.append(entry)
            say(f'check {k}: the correction repeats an earlier recipe; stopping')
            break
        seen.add(key)
        recipe, rep, raw = new, rep2, raw3
        entry['changed'] = True
        log.append(entry)
        say(f'check {k}: {len(disc)} discrepanc{"y" if len(disc) == 1 else "ies"} fixed — {_said(disc)}')
    warnings = list(rep.warnings) + [f'the recipe cannot express: {x}' for x in recipe.get('cannot_express', [])]
    return {'recipe': recipe, 'freeform': None, 'raw_text': raw, 'repaired': repaired,
            'validation': {'errors': list(rep.errors), 'warnings': warnings}}


def _read_code(ask, drawing, hint, effort, rounds, builder, say, log):
    say('reading the drawing and writing the script…')
    raw = ask(CODE_SYSTEM_PROMPT, [drawing, ('text', CODE_USER_PROMPT + hint)], CODE_SCHEMA, effort)
    ff = normalize_freeform(parse_recipe_text(raw))
    log.append({'round': 0, 'kind': 'first', 'code': ff['code']})
    fixes = looks = 0
    errors, warnings = [], []
    seen = {ff['code'].strip()}
    last_ok, last_ok_code = None, None

    def ask_fix(error):
        nonlocal raw
        say(f'the script failed; asking for a fix ({fixes} of {FIX_BUDGET})…')
        raw = ask(CODE_SYSTEM_PROMPT,
                  [drawing, ('text', CODE_FAILED_PROMPT.format(error=error, code=ff['code']) + hint)],
                  CRITIQUE_CODE_SCHEMA, effort)
        return normalize_freeform(parse_recipe_text(raw), previous=ff)

    while True:
        static = check_code(ff['code'])
        if static:
            err = 'the script was refused: ' + '; '.join(static[:6])
            log.append({'round': fixes + 1, 'kind': 'fix', 'error': err})
            if fixes >= FIX_BUDGET:
                errors = [err]
                break
            fixes += 1
            ff = ask_fix(err)
            continue
        if builder is None:
            break                                   # nothing to run it with here; the worker will
        if last_ok_code != ff['code']:
            say('running the script…' if looks == 0 else f'check {looks}: running the corrected script…')
            chk = builder('code', ff)
            if not chk.ok:
                log.append({'round': fixes + 1, 'kind': 'fix', 'error': chk.error})
                if fixes >= FIX_BUDGET:
                    errors = [chk.error]
                    break
                fixes += 1
                ff = ask_fix(chk.error)
                continue
            last_ok, last_ok_code = chk, ff['code']
            warnings = list(chk.warnings)
            say('the script built %d solid%s, %s mm' % (
                chk.solids or 0, '' if chk.solids == 1 else 's',
                ' x '.join('%.1f' % v for v in chk.bbox['size']) if chk.bbox else '?'))
        if looks >= rounds:
            break
        looks += 1
        entry = {'round': looks, 'kind': 'look',
                 'built': {'size': last_ok.bbox['size'] if last_ok.bbox else None, 'solids': last_ok.solids}}
        parts = [drawing] + _render_parts(last_ok.renders) + [
            ('text', CRITIQUE_INTRO + CRITIQUE_CODE_TAIL.format(code=ff['code']) + hint)]
        say(f'check {looks} of {rounds}: comparing the views with the drawing…')
        try:
            raw3 = ask(CODE_SYSTEM_PROMPT, parts, CRITIQUE_CODE_SCHEMA, _critique_effort(effort))
            ans = parse_recipe_text(raw3)
        except ReadError as e:
            entry['error'] = str(e)
            log.append(entry)
            say(f'check {looks}: no usable answer ({e}); keeping the script as it is')
            break
        disc = _discrepancies(ans)
        entry['discrepancies'] = disc
        entry['done'] = bool(ans.get('done'))
        new_code = ans.get('code') if isinstance(ans.get('code'), str) else ''
        if ans.get('done') or not new_code.strip() or new_code.strip() == ff['code'].strip():
            entry['done'] = True
            log.append(entry)
            say(f'check {looks}: the model finds the build matches the drawing'
                + (f' ({_said(disc)})' if disc else ''))
            break
        if new_code.strip() in seen:
            entry['rejected'] = 'the correction repeats an earlier script'
            entry['done'] = True
            log.append(entry)
            say(f'check {looks}: the correction repeats an earlier script; stopping')
            break
        seen.add(new_code.strip())
        ff = normalize_freeform(ans, previous=ff)
        raw = raw3
        entry['changed'] = True
        entry['code'] = ff['code']          # every version stays recoverable from read.json
        log.append(entry)
        say(f'check {looks}: {len(disc)} discrepanc{"y" if len(disc) == 1 else "ies"} — {_said(disc)}')
    return {'recipe': None, 'freeform': ff, 'raw_text': raw, 'repaired': fixes > 0,
            'validation': {'errors': errors, 'warnings': warnings}}
