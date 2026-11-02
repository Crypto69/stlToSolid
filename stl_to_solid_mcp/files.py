"""Paths: what a tool may read, where its results go, and never overwriting
anything. Every refusal is a sentence a model can act on."""
import os
import re
import shutil

MESH_EXTS = ('.stl', '.obj', '.ply', '.off', '.3mf', '.glb', '.gltf')
DRAWING_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
_SAFE = re.compile(r'[^\w.-]+')


class PathRefused(ValueError):
    """A path the server will not touch; the message says why."""


def _inside(path, folder):
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def resolve_input(cfg, path, exts=None, what='file'):
    """The real absolute path of an existing input file, or PathRefused."""
    if not path or not str(path).strip():
        raise PathRefused(f'No {what} path was given.')
    p = os.path.expanduser(str(path).strip())
    if not os.path.isabs(p):
        if not cfg.root:
            raise PathRefused(f'Give the {what} as an absolute path, not "{path}": this server does '
                              "not know the client's working folder.")
        p = os.path.join(cfg.root, p)
    p = os.path.realpath(p)
    if cfg.root and not (_inside(p, cfg.root) or _inside(p, cfg.out_root)):
        raise PathRefused(f'{path} is outside the folder this server may read ({cfg.root}).')
    if not os.path.isfile(p):
        raise PathRefused(f'There is no {what} at {path}.')
    if exts and os.path.splitext(p)[1].lower() not in exts:
        raise PathRefused(f'{os.path.basename(p)} is not a {what}. Expected one of: {", ".join(exts)}.')
    return p


def safe_stem(name):
    stem = os.path.splitext(os.path.basename(str(name)))[0]
    return _SAFE.sub('_', stem).strip('_.') or 'part'


def resolve_out_dir(cfg, out_dir, stem):
    """The folder a tool's results go in, created. The default is one folder
    per part under the output root. An explicit folder must stay inside the
    root when the server is remote or restricted."""
    if out_dir:
        d = os.path.expanduser(str(out_dir).strip())
        if not os.path.isabs(d):
            d = os.path.join(cfg.out_root, d)
        d = os.path.realpath(d)
        if (cfg.root or cfg.http) and not _inside(d, cfg.out_root):
            raise PathRefused(f'{out_dir} is outside the output folder ({cfg.out_root}).')
    else:
        d = os.path.join(cfg.out_root, stem)
    os.makedirs(d, exist_ok=True)
    return d


def fresh_path(folder, name):
    """`folder/name`, or `name-2`, `name-3`... when it exists: a tool never
    overwrites a file, including one from its own last run."""
    base, ext = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f'{base}-{n}{ext}')
        n += 1
    return candidate


def copy_fresh(src, folder, name):
    dst = fresh_path(folder, name)
    shutil.copyfile(src, dst)
    return dst


def public(cfg, path):
    """A path as the client should see it: absolute on the same machine,
    relative to the shared root on a remote server."""
    if cfg.http and cfg.root and _inside(path, cfg.root):
        return os.path.relpath(path, cfg.root).replace(os.sep, '/')
    return path


def url_for(cfg, path):
    """The download URL of a result on a remote server, else None."""
    if not (cfg.http and cfg.base_url and cfg.root and _inside(path, cfg.root)):
        return None
    return cfg.base_url.rstrip('/') + '/files/' + os.path.relpath(path, cfg.root).replace(os.sep, '/')
