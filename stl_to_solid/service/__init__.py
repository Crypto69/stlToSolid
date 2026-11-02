"""The conversion service: the job registry, the worker subprocess, mesh and
STEP analysis, section tracing and the Blueprint preview helper. It has no
web framework in it and imports nothing heavy, so every front end shares it:
the web API (backend/) and the MCP server (stl_to_solid_mcp/)."""
