"""The command's front door: says what to install when the mcp extra is
missing, instead of a traceback about a module nobody has heard of."""
import sys

INSTALL = ('The MCP server needs the mcp extra. Install it with:\n'
           '  pip install "stltosolid[mcp]"\n'
           'or run it directly with uv:\n'
           '  uvx --python 3.12 --from "stltosolid[mcp]" stltosolid-mcp')


def main(argv=None):
    try:
        from .server import main as serve
    except ModuleNotFoundError as e:
        if (e.name or '').split('.')[0] in ('mcp', 'mcp_types', 'anyio', 'pydantic'):
            sys.exit(INSTALL)
        raise
    serve(argv)
