"""Where the server may read and write, and what it may do. Settled once at
start-up from the command line and the environment."""
import os
from dataclasses import dataclass

DEFAULT_OUT = os.path.join('~', 'stlToSolid', 'mcp-output')


def _flag(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


@dataclass(frozen=True)
class Config:
    out_root: str                # every file the server writes goes under here
    allow_code: bool = False     # register build_cadquery
    root: str | None = None      # when set, input paths must be inside this folder too
    http: bool = False           # remote mode: paths are relative to `root`, files have URLs
    base_url: str | None = None  # how a client reaches this server's /files route
    xray_budget_s: float = 240.0 # seconds an X-Ray script may spend fitting
    default_wait_s: float = 45.0 # seconds a long tool blocks before handing back a job id

    @property
    def jobs_dir(self):
        return os.path.join(self.out_root, '.jobs')


def from_args(out=None, root=None, allow_code=False, http=False, base_url=None):
    root = root or os.environ.get('STLTOSOLID_MCP_ROOT') or None
    if http and not root:
        root = out or os.environ.get('STLTOSOLID_OUT') or DEFAULT_OUT
    if root:
        root = os.path.realpath(os.path.expanduser(root))
    out_root = out or os.environ.get('STLTOSOLID_OUT') or (root if http else DEFAULT_OUT)
    return Config(
        out_root=os.path.realpath(os.path.expanduser(out_root)),
        allow_code=bool(allow_code) or _flag('STLTOSOLID_MCP_ALLOW_CODE'),
        root=root, http=http, base_url=base_url,
        xray_budget_s=float(os.environ.get('STLTOSOLID_XRAY_BUDGET', 240)),
        default_wait_s=float(os.environ.get('STLTOSOLID_MCP_WAIT', 45)))
