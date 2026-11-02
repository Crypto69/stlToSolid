"""Build the Claude Desktop bundle (dist/stltosolid-<version>.mcpb) and fill
server.json's MCPB entry when given the release URL it will live at.

    python tools/build_mcpb.py                       # build and validate
    python tools/build_mcpb.py --release-url URL     # also write its sha256 into server.json

Version numbers come from stl_to_solid/__init__.py, so a release cannot ship a
bundle that asks for a package version other than its own.
Needs node (npx) for the official mcpb packer.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'packaging', 'mcpb')
MCPB_CLI = '@anthropic-ai/mcpb@2.1.2'


def version():
    with open(os.path.join(ROOT, 'stl_to_solid', '__init__.py')) as f:
        return re.search(r"__version__ = '([^']+)'", f.read()).group(1)


def npx(*args, cwd):
    subprocess.run(['npx', '--yes', MCPB_CLI, *args], cwd=cwd, check=True, shell=sys.platform == 'win32')


def build(ver, out_dir):
    out_dir = os.path.abspath(out_dir)        # the packer runs from a scratch folder
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, 'bundle')
        shutil.copytree(SRC, d)
        shutil.copy(os.path.join(ROOT, 'LICENSE'), d)
        path = os.path.join(d, 'manifest.json')
        with open(path) as f:
            manifest = json.load(f)
        manifest['version'] = ver
        with open(path, 'w') as f:
            json.dump(manifest, f, indent=2)
        path = os.path.join(d, 'pyproject.toml')
        with open(path) as f:
            text = f.read()
        text = re.sub(r'^version = ".*"$', f'version = "{ver}"', text, flags=re.M)
        text = re.sub(r'stltosolid\[mcp\]==[^"]+', f'stltosolid[mcp]=={ver}', text)
        with open(path, 'w') as f:
            f.write(text)
        npx('validate', 'manifest.json', cwd=d)
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f'stltosolid-{ver}.mcpb')
        npx('pack', d, out, cwd=tmp)
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def update_server_json(ver, url, digest):
    path = os.path.join(ROOT, 'server.json')
    with open(path) as f:
        doc = json.load(f)
    doc['version'] = ver
    pkgs = [p for p in doc['packages'] if p['registryType'] != 'mcpb']
    for p in pkgs:
        p['version'] = ver
    pkgs.append({'registryType': 'mcpb', 'identifier': url, 'version': ver, 'fileSha256': digest,
                 'transport': {'type': 'stdio'}})
    doc['packages'] = pkgs
    with open(path, 'w') as f:
        json.dump(doc, f, indent=2)
        f.write('\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default=os.path.join(ROOT, 'dist'))
    ap.add_argument('--release-url', help='where the bundle will be downloaded from')
    args = ap.parse_args()
    ver = version()
    out = build(ver, args.out)
    digest = sha256(out)
    print(f'{out}  {os.path.getsize(out)} bytes  sha256 {digest}')
    if args.release_url:
        update_server_json(ver, args.release_url, digest)
        print('server.json updated')


if __name__ == '__main__':
    main()
