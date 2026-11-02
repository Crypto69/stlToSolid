__version__ = '0.6.1'


def __getattr__(name):
    # `from stl_to_solid import run` costs the mesh stack only when asked for:
    # the job registry and the MCP server import this package and must start
    # in a blink.
    if name == 'run':
        from .pipeline import run
        return run
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
