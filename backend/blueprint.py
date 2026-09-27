"""Blueprint's server side: the drawing upload (an image, not a mesh),
which vision providers can read it and where their keys come from, and
the glue between the reader (stl_to_solid.blueprint.read_drawing) and
the job registry. The geometry lives in stl_to_solid.blueprint.

Keys: the browser sends the user's own key per request (header
X-Api-Key) and it is used for that one call and dropped; it is never
written to disk or logged. With no header, a server-side key from the
environment is used when set (an opt-in for a private install: on a
shared NAS anyone on the LAN could spend it).
"""
import os

from stl_to_solid.blueprint import providers as _providers

IMAGE_EXTS = ('jpg', 'jpeg', 'png', 'webp')
IMAGE_MEDIA = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}
MAX_IMAGE = int(os.environ.get('STLTOSOLID_MAX_IMAGE', 20 * 1024 * 1024))
# seconds one read may take: STLTOSOLID_BLUEPRINT_TIMEOUT for every provider
# when set, else the provider's own (DeepSeek thinks for minutes at max effort)
_READ_TIMEOUT_ENV = os.environ.get('STLTOSOLID_BLUEPRINT_TIMEOUT')
READ_TIMEOUT_S = float(_READ_TIMEOUT_ENV or 300)

# The providers themselves (label, adapter, default model, endpoint) are
# the registry in stl_to_solid.blueprint.providers; this module adds the
# server's side of them: keys from the environment, timeouts, the config.
PROVIDERS = _providers.PROVIDERS

NO_KEY_MESSAGE = ('No API key for {label}: paste one in the Blueprint panel (it stays in your '
                  'browser), or set {env} on the server.')


def spec(provider):
    """The ProviderSpec, or None for a name nobody registered."""
    return PROVIDERS.get(provider)


def env_var(provider):
    return f'STLTOSOLID_{provider.upper()}_API_KEY'


def server_key(provider):
    if provider not in PROVIDERS:
        return None
    return os.environ.get(env_var(provider)) or None


def read_timeout(provider):
    if _READ_TIMEOUT_ENV:
        return READ_TIMEOUT_S
    return float(PROVIDERS[provider].timeout)


def resolve_key(provider, header_key):
    """The key to use: the request's, else the server's, else None."""
    k = (header_key or '').strip()
    if k:
        return k
    return server_key(provider)


def no_key_message(provider):
    return NO_KEY_MESSAGE.format(label=PROVIDERS[provider].label, env=env_var(provider))


def config():
    """What the panel needs to offer the providers."""
    return {
        'providers': [{
            'key': k, 'label': p.label, 'default_model': p.default_model,
            'base_url': p.base_url, 'server_key': server_key(k) is not None,
            'needs_key': p.needs_key, 'custom_url': p.custom_url, 'note': p.note, 'env': env_var(k),
        } for k, p in PROVIDERS.items()],
        'max_image_mb': MAX_IMAGE // (1 << 20),
    }


def sdk_available(provider):
    """None when the provider's SDK imports, else the pip hint."""
    sdk = PROVIDERS[provider].adapter.sdk
    try:
        __import__(sdk)
    except ImportError:
        return f'The server is missing the {sdk} package: pip install {sdk}'
    return None


def verify_image(path):
    """(width, height) of a real image, else ValueError with a sentence.
    PIL's verify() leaves the file object unusable, so it is reopened."""
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            w, h = im.size
    except UnidentifiedImageError:
        raise ValueError('the file is not an image the server can read (JPEG, PNG or WebP)')
    except OSError as e:
        raise ValueError(f'the image could not be read ({e})')
    if w < 64 or h < 64:
        raise ValueError(f'the image is only {w} x {h} pixels; the labels would be unreadable')
    return w, h


def list_models(provider, api_key, base_url=None, timeout=30.0):
    """[{'id', 'label', 'created'}], newest first, for the panel's
    dropdown: the provider's adapter lists the models that take images.
    Raises ReadError with a sentence."""
    return _providers.make_provider(provider, api_key, '', base_url, timeout).list_models()
