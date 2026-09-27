"""The vision providers behind Blueprint: one adapter class per API
style, and a registry of the providers the panel offers.

Adapters (subclasses of Provider) turn the one neutral request into an
API call: complete() takes (system, image, text, schema, history,
effort) and returns (text, usage, model); list_models() returns the
models that take images. AnthropicProvider uses its SDK (structured
output by JSON schema). OpenAICompatibleProvider covers any
OpenAI-style chat endpoint (Ollama, OpenRouter... by base URL) and tries
strict JSON schema, then json_object, then plain text, since compatible
servers differ in what they accept; OpenAIProvider and DeepSeekProvider
are small variants of it that change only hooks (the image-model
filter; DeepSeek's json_object with the schema in the prompt, thinking,
effort scale and image detail).

The registry (PROVIDERS, filled by register()) holds one ProviderSpec
per provider: its label, adapter, default model and endpoint.
make_provider() is the factory. A new OpenAI-compatible provider is one
register() call; a new API style is an adapter class plus that call.

Failures come back as ReadError with a sentence for the user; the key
is used for the one client and never repeated in any message. This is
the only module that imports the provider SDKs, lazily, inside methods.
"""
import base64
import json
import re
from dataclasses import dataclass


class ReadError(RuntimeError):
    """A sentence for the user (errors.describe returns it verbatim)."""
    readable = True

    def __init__(self, message, kind='read'):
        super().__init__(message)
        self.kind = kind


# the model's thinking counts against this too, so it is generous; the
# request streams so the HTTP connection is not held open for one answer
MAX_TOKENS = 64000
# DeepSeek at max effort thought for 88k tokens on the SG90 sheet (and ran
# out at 64k); its ceiling is 384k
DEEPSEEK_MAX_TOKENS = 256000


class Provider:
    name = 'base'
    sdk = None          # the pip package the adapter imports

    def __init__(self, api_key, model, base_url=None, timeout=300.0):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

    def complete(self, system, image_bytes, media_type, text, schema, history=(), effort='high'):
        """`history`: (role, text) pairs after the first user turn, for the
        repair round. `effort`: 'low' | 'medium' | 'high', how hard the model
        thinks (Anthropic output_config.effort; OpenAI reasoning_effort,
        dropped for a server that rejects it). Returns
        (text, {'input_tokens', 'output_tokens'}, model)."""
        raise NotImplementedError

    def list_models(self):
        """[{'id', 'label', 'created'}] the key can use that take images,
        newest first, for the panel's dropdown. Raises ReadError."""
        raise NotImplementedError


EFFORTS = ('low', 'medium', 'high')


class AnthropicProvider(Provider):
    name = 'anthropic'
    sdk = 'anthropic'

    def list_models(self):
        """Filtered to models that take images (the API says so)."""
        import anthropic
        try:
            client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=1)
            out = []
            for m in client.models.list():
                caps = getattr(m, 'capabilities', None)
                if not getattr(getattr(caps, 'image_input', None), 'supported', True):
                    continue
                created = getattr(m, 'created_at', None)
                out.append({'id': m.id, 'label': getattr(m, 'display_name', None) or m.id,
                            'created': created.isoformat() if hasattr(created, 'isoformat') else str(created or '')})
        except anthropic.AuthenticationError:
            raise ReadError('Anthropic refused the API key. Check the key in the Blueprint panel.', 'key')
        except anthropic.APIStatusError as e:
            raise ReadError(f'Anthropic answered {e.status_code}: {_short(e.message)}', 'api')
        except anthropic.APIConnectionError:
            raise ReadError('Could not reach the Anthropic API from the server (network or timeout).', 'network')
        out.sort(key=lambda x: x['created'], reverse=True)
        return out

    def complete(self, system, image_bytes, media_type, text, schema, history=(), effort='high'):
        import anthropic
        effort = effort if effort in EFFORTS else 'high'
        client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=2)
        b64 = base64.standard_b64encode(image_bytes).decode('ascii')
        messages = [{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': b64}},
            {'type': 'text', 'text': text}]}]
        messages += [{'role': r, 'content': t} for r, t in history]
        kwargs = dict(model=self.model, max_tokens=MAX_TOKENS,
                      system=[{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
                      messages=messages)
        try:
            try:
                with client.messages.stream(
                        output_config={'effort': effort,
                                       'format': {'type': 'json_schema', 'schema': schema}},
                        **kwargs) as stream:
                    resp = stream.get_final_message()
            except TypeError:
                # an older SDK without output_config: the prompt alone asks for JSON
                with client.messages.stream(**kwargs) as stream:
                    resp = stream.get_final_message()
        except anthropic.AuthenticationError:
            raise ReadError('Anthropic refused the API key. Check the key in the Blueprint panel.', 'key')
        except anthropic.PermissionDeniedError as e:
            raise ReadError(f'Anthropic refused the request: {e.message}', 'key')
        except anthropic.NotFoundError:
            raise ReadError(f'Anthropic has no model called {self.model!r}. Edit the model name.', 'model')
        except anthropic.RateLimitError:
            raise ReadError('Anthropic is rate-limiting this key; wait a minute and read again.', 'rate')
        except anthropic.APIStatusError as e:
            raise ReadError(f'Anthropic answered {e.status_code}: {_short(e.message)}', 'api')
        except anthropic.APIConnectionError:
            raise ReadError('Could not reach the Anthropic API from the server (network or timeout).', 'network')
        stop = getattr(resp, 'stop_reason', None)
        if stop == 'refusal':
            raise ReadError('The model declined to read this image.', 'refusal')
        u = getattr(resp, 'usage', None)
        if stop == 'max_tokens':
            raise ReadError('The answer was cut off after %s output tokens; try a drawing with fewer '
                            'features.' % getattr(u, 'output_tokens', '?'), 'truncated')
        out = ''.join(b.text for b in resp.content if getattr(b, 'type', '') == 'text')
        usage = {'input_tokens': getattr(u, 'input_tokens', None),
                 'output_tokens': getattr(u, 'output_tokens', None)}
        return out, usage, getattr(resp, 'model', self.model)


class OpenAICompatibleProvider(Provider):
    """Any OpenAI-style chat endpoint; the subclasses below change hooks only."""
    name = 'openai-compatible'
    sdk = 'openai'

    def _keep_model(self, model_id):
        """Whether a listed model belongs in the dropdown. A compatible server
        only gives ids and says nothing about images, so all of them."""
        return True

    def list_models(self):
        import openai
        try:
            client = openai.OpenAI(api_key=self.api_key or 'none', base_url=self.base_url,
                                   timeout=self.timeout, max_retries=1)
            models = list(client.models.list())
        except openai.AuthenticationError:
            raise ReadError(f'{_where(self)} refused the API key. Check the key in the Blueprint panel.', 'key')
        except openai.APIStatusError as e:
            raise ReadError(f'{_where(self)} answered {e.status_code}: {_short(_msg(e))}', 'api')
        except openai.APIConnectionError:
            raise ReadError(f'Could not reach {_where(self)} from the server (network or timeout).', 'network')
        out = [{'id': m.id, 'label': m.id, 'created': int(getattr(m, 'created', 0) or 0)}
               for m in models if getattr(m, 'id', None) and self._keep_model(m.id)]
        # a dated snapshot ("gpt-5.5-2026-04-23") next to its undated alias is noise
        ids = {m['id'] for m in out}
        out = [m for m in out if not (_DATED.search(m['id']) and _DATED.sub('', m['id']) in ids)]
        out.sort(key=lambda x: (x['created'], x['id']), reverse=True)
        return out

    def _system(self, system, schema):
        return system

    def _formats(self, schema):
        """response_format values to try in order (None: plain text)."""
        return [
            {'type': 'json_schema', 'json_schema': {'name': 'recipe', 'schema': schema, 'strict': True}},
            {'type': 'json_object'},
            None,
        ]

    def _knobs(self, effort):
        """(reasoning_effort or None, other request kwargs)."""
        return effort, {}

    def _image(self, url):
        return {'url': url}

    def complete(self, system, image_bytes, media_type, text, schema, history=(), effort='high'):
        import openai
        effort = effort if effort in EFFORTS else 'high'
        client = openai.OpenAI(api_key=self.api_key or 'none', base_url=self.base_url,
                               timeout=self.timeout, max_retries=2)
        b64 = base64.standard_b64encode(image_bytes).decode('ascii')
        messages = [{'role': 'system', 'content': self._system(system, schema)},
                    {'role': 'user', 'content': [
                        {'type': 'image_url', 'image_url': self._image(f'data:{media_type};base64,{b64}')},
                        {'type': 'text', 'text': text}]}]
        messages += [{'role': r, 'content': t} for r, t in history]
        resp = None
        # the OpenAI reasoning knob; a compatible server that does not know it
        # gets the same request without it
        reasoning, extra = self._knobs(effort)
        send_effort = reasoning is not None
        attempts = self._formats(schema)
        while attempts:
            fmt = attempts[0]
            kwargs = dict(model=self.model, messages=messages, **extra)
            if fmt is not None:
                kwargs['response_format'] = fmt
            if send_effort:
                kwargs['reasoning_effort'] = reasoning
            try:
                resp = client.chat.completions.create(**kwargs)
                break
            except openai.AuthenticationError:
                raise ReadError(f'{_where(self)} refused the API key. Check the key in the Blueprint panel.', 'key')
            except openai.PermissionDeniedError as e:
                raise ReadError(f'{_where(self)} refused the request: {_short(_msg(e))}', 'key')
            except openai.NotFoundError as e:
                raise ReadError(f'{_where(self)} has no model called {self.model!r} ({_short(_msg(e))}). '
                                'Edit the model name.', 'model')
            except openai.RateLimitError:
                raise ReadError(f'{_where(self)} is rate-limiting this key; wait a minute and read again.', 'rate')
            except openai.BadRequestError as e:
                msg = _msg(e)
                low = msg.lower()
                if send_effort and ('reasoning' in low or 'effort' in low):
                    send_effort = False               # same format, without the knob
                    continue
                # a server that does not know this response_format: try the next
                if fmt is not None and any(k in low for k in
                                           ('response_format', 'json_schema', 'json_object', 'schema',
                                            'strict', 'unsupported', 'not supported', 'invalid_request')):
                    attempts.pop(0)
                    continue
                raise ReadError(f'{_where(self)} rejected the request: {_short(msg)}', 'api')
            except openai.APIStatusError as e:
                raise ReadError(f'{_where(self)} answered {e.status_code}: {_short(_msg(e))}', 'api')
            except openai.APIConnectionError:
                raise ReadError(f'Could not reach {_where(self)} from the server (network or timeout).', 'network')
        if resp is None:
            raise ReadError(f'{_where(self)} accepted none of the JSON output modes.', 'api')
        choice = resp.choices[0] if getattr(resp, 'choices', None) else None
        if choice is None:
            raise ReadError(f'{_where(self)} returned no answer.', 'api')
        msg = choice.message
        if getattr(msg, 'refusal', None):
            raise ReadError('The model declined to read this image: ' + _short(msg.refusal), 'refusal')
        if getattr(choice, 'finish_reason', None) == 'length':
            raise ReadError('The answer was cut off (the model ran out of room, often while thinking); '
                            'try a lower Effort or a drawing with fewer features.', 'truncated')
        out = msg.content or ''
        if isinstance(out, list):            # some servers return content parts
            out = ''.join(p.get('text', '') if isinstance(p, dict) else getattr(p, 'text', '') for p in out)
        if not out.strip():
            # DeepSeek's JSON mode is documented to do this now and then
            raise ReadError(f'{_where(self)} returned an empty answer; read the drawing again.', 'api')
        u = getattr(resp, 'usage', None)
        usage = {'input_tokens': getattr(u, 'prompt_tokens', None),
                 'output_tokens': getattr(u, 'completion_tokens', None)}
        return out, usage, getattr(resp, 'model', None) or self.model


_DATED = re.compile(r'-\d{4}-\d{2}-\d{2}$')
# OpenAI only gives model ids; these are the families that take images in a
# chat completion, and the words that mark models that do not
OPENAI_VISION_PREFIXES = ('gpt-4o', 'gpt-4.1', 'gpt-4.5', 'gpt-5', 'o1', 'o3', 'o4', 'chatgpt-')
OPENAI_SKIP = ('audio', 'realtime', 'transcribe', 'tts', 'search', 'image', 'embedding', 'moderation',
               'codex', 'computer-use', 'instruct', 'preview-2024', 'whisper', 'dall-e', 'babbage', 'davinci')


class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI itself: the dropdown keeps the families that take images."""
    name = 'openai'

    def _keep_model(self, model_id):
        return model_id.startswith(OPENAI_VISION_PREFIXES) and not any(w in model_id for w in OPENAI_SKIP)


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek's hosted API (api-docs.deepseek.com). JSON mode is
    json_object only (no json_schema), so the schema goes into the system
    prompt, which must also say "json". Only deepseek-flash takes images,
    so the dropdown keeps only Flash and deepseek-v4-pro is refused before
    any call. Flash gets thinking switched on explicitly (its default, but
    the request should not depend on that), with DeepSeek's effort scale
    low / high / max: the panel's medium has no match and rounds up, and
    its high asks for max. max_tokens is set, so thinking plus a long
    recipe is not cut off at a server default. The image goes at detail
    'original' (full resolution)."""
    name = 'deepseek'
    EFFORT = {'low': 'low', 'medium': 'high', 'high': 'max'}

    def _thinking_model(self):
        return 'flash' in (self.model or '').lower()

    def _keep_model(self, model_id):
        return 'flash' in model_id.lower()

    def complete(self, system, image_bytes, media_type, text, schema, history=(), effort='high'):
        if 'pro' in (self.model or '').lower().split('-'):
            raise ReadError(f'{self.model} does not take images, so it cannot read a drawing. '
                            'Pick deepseek-flash.', 'model')
        return super().complete(system, image_bytes, media_type, text, schema, history, effort)

    def _system(self, system, schema):
        return (system + '\n\nAnswer with one json object and nothing else: the recipe, '
                'following this JSON schema exactly:\n' + json.dumps(schema, separators=(',', ':')))

    def _formats(self, schema):
        return [{'type': 'json_object'}]

    def _image(self, url):
        # full resolution: 'low' would shrink the sheet to 512 px and lose the labels
        return {'url': url, 'detail': 'original'}

    def _knobs(self, effort):
        extra = {'max_tokens': DEEPSEEK_MAX_TOKENS}
        if not self._thinking_model():
            return None, extra
        extra['extra_body'] = {'thinking': {'type': 'enabled'}}
        return self.EFFORT[effort], extra


def _msg(e):
    m = getattr(e, 'message', None) or str(e)
    body = getattr(e, 'body', None)
    if isinstance(body, dict):
        err = body.get('error')
        if isinstance(err, dict) and err.get('message'):
            m = err['message']
    return str(m)


def _short(m, n=300):
    m = ' '.join(str(m).split())
    return m if len(m) <= n else m[:n - 1] + '…'


def _where(p):
    return f'the server at {p.base_url}' if p.base_url else 'OpenAI'


# ---------------------------------------------------------------- registry

@dataclass(frozen=True)
class ProviderSpec:
    """One provider the panel offers."""
    key: str                    # id in requests, and STLTOSOLID_<KEY>_API_KEY on the server
    label: str
    adapter: type               # a Provider subclass
    default_model: str
    base_url: str = None        # None: the SDK's default endpoint
    needs_key: bool = True
    custom_url: bool = False    # the user types the base URL (and the model)
    timeout: float = 300.0      # seconds one read may take
    note: str = ''


PROVIDERS = {}                  # key -> ProviderSpec, in the panel's order


def register(spec):
    PROVIDERS[spec.key] = spec
    return spec


def get_spec(provider):
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ReadError(f'unknown provider {provider!r}', 'api') from None


def make_provider(provider, api_key, model, base_url=None, timeout=300.0):
    """The adapter for a registered provider. Only a custom_url provider
    takes the request's base URL; the others always use their own."""
    spec = get_spec(provider)
    url = ((base_url or '').strip() or None) if spec.custom_url else spec.base_url
    return spec.adapter(api_key, model, url, timeout)


register(ProviderSpec('anthropic', 'Anthropic (Claude)', AnthropicProvider, 'claude-opus-5'))
register(ProviderSpec('openai', 'OpenAI', OpenAIProvider, 'gpt-5.5',
                      note='Model names change; edit the model if the API says it does not exist.'))
register(ProviderSpec('deepseek', 'DeepSeek', DeepSeekProvider, 'deepseek-flash',
                      base_url='https://api.deepseek.com', timeout=1200,
                      note='Only deepseek-flash reads images, so it is the one offered. Thinking is '
                           "switched on; Effort high asks for DeepSeek's max (a read takes about 6–7 min)."))
register(ProviderSpec('custom', 'Custom (OpenAI-compatible URL)', OpenAICompatibleProvider, '',
                      needs_key=False, custom_url=True,
                      note='Any OpenAI-compatible server: base URL and model are yours to type. Ollama '
                           '(http://localhost:11434/v1) with a vision model keeps the drawing local.'))
