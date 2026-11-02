"""Remote mode: the same tools over Streamable HTTP, for a server on a NAS.

A remote server cannot see the client's disk, so files live in one shared
folder (--root). Clients get files in by copying into that folder, for example
a NAS share, or with PUT /files/<name>; tool results carry download URLs served
by GET /files/<path>. Every route but /healthz needs the bearer token.
"""
import hmac
import os
import sys

from . import config as C
from . import files as F
from .server import build_server

MAX_UPLOAD = int(os.environ.get('STLTOSOLID_MAX_UPLOAD', 200 * 1024 * 1024))
# a tool call's request body: a recipe or a script, never a file
MAX_BODY = 4 * 1024 * 1024
LOOPBACK = ('127.0.0.1', 'localhost', '::1')


class BearerAuth:
    """Plain ASGI, so it also covers the streaming MCP endpoint."""

    def __init__(self, app, token):
        self.app = app
        self.expected = ('Bearer ' + token).encode()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') == '/healthz':
            return await self.app(scope, receive, send)
        got = dict(scope.get('headers') or []).get(b'authorization', b'')
        if hmac.compare_digest(got, self.expected):
            return await self.app(scope, receive, send)
        await send({'type': 'http.response.start', 'status': 401,
                    'headers': [(b'content-type', b'text/plain'), (b'www-authenticate', b'Bearer')]})
        await send({'type': 'http.response.body', 'body': b'A valid bearer token is required.\n'})


def _file_path(cfg, rel):
    """The real path under the shared root, or None for anything outside it
    or inside the jobs scratch folder."""
    p = os.path.realpath(os.path.join(cfg.root, rel))
    if not F._inside(p, cfg.root) or F._inside(p, cfg.jobs_dir):
        return None
    return p


def add_routes(mcp, cfg):
    from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

    @mcp.custom_route('/healthz', methods=['GET'])
    async def healthz(_request):
        from stl_to_solid import __version__
        return JSONResponse({'ok': True, 'version': __version__})

    @mcp.custom_route('/files/{path:path}', methods=['GET'])
    async def download(request):
        p = _file_path(cfg, request.path_params['path'])
        if p is None or not os.path.isfile(p):
            return PlainTextResponse('No such file.\n', status_code=404)
        return FileResponse(p, filename=os.path.basename(p))

    @mcp.custom_route('/files/{name}', methods=['PUT'])
    async def upload(request):
        """Save the request body as a new file in the shared folder. It never
        replaces a file: a name already taken gets -2, -3..."""
        name = os.path.basename(request.path_params['name'])
        stem, ext = os.path.splitext(name)
        if ext.lower() not in F.MESH_EXTS + F.DRAWING_EXTS + ('.step', '.stp'):
            return PlainTextResponse(f'{ext or name} is not a mesh, a drawing or a STEP.\n', status_code=415)
        folder = os.path.join(cfg.root, 'uploads')
        os.makedirs(folder, exist_ok=True)
        dst = F.fresh_path(folder, F.safe_stem(stem) + ext.lower())
        size = 0
        with open(dst, 'wb') as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    out.close()
                    os.remove(dst)
                    return PlainTextResponse(f'Over the limit of {MAX_UPLOAD // (1 << 20)} MB.\n', status_code=413)
                out.write(chunk)
        return JSONResponse({'path': F.public(cfg, dst), 'bytes': size, 'url': F.url_for(cfg, dst)}, status_code=201)


def build_app(cfg, token, host='127.0.0.1'):
    from mcp.server.transport_security import TransportSecuritySettings
    mcp = build_server(cfg)
    add_routes(mcp, cfg)
    # The SDK's DNS-rebinding guard only knows loopback names. Behind a token a
    # browser page cannot use this server anyway, so a NAS's own name works.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False) if token else None
    app = mcp.streamable_http_app(host=host, max_request_body_size=MAX_BODY, transport_security=security)
    return BearerAuth(app, token) if token else app


def serve(args):
    import uvicorn
    token = args.token or os.environ.get('STLTOSOLID_MCP_TOKEN') or None
    if not token and not args.no_auth and args.host not in LOOPBACK:
        sys.exit(f'stltosolid-mcp: refusing to listen on {args.host} with no token. Set --token or '
                 'STLTOSOLID_MCP_TOKEN, or pass --no-auth if the network is yours alone.')
    base = args.base_url or os.environ.get('STLTOSOLID_MCP_URL') or f'http://{args.host}:{args.port}'
    cfg = C.from_args(out=args.out, root=args.root, allow_code=args.allow_code, http=True, base_url=base)
    print(f'stltosolid-mcp: serving {base}/mcp, shared folder {cfg.root}', file=sys.stderr)
    uvicorn.run(build_app(cfg, token, args.host), host=args.host, port=args.port, log_level='warning')
