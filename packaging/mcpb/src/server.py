"""Claude Desktop starts this with `uv run`; the server itself comes from the
stltosolid package named in pyproject.toml."""
from stl_to_solid_mcp.server import main

if __name__ == '__main__':
    main()
