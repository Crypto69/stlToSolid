"""Moved to stl_to_solid.service.worker (the wheel does not ship backend/).
This name stays an alias of the same module: a frozen build's entry point
imports `backend.worker`, and `python -m backend.worker` still works."""
import sys

from stl_to_solid.service import worker as _module

if __name__ == '__main__':
    _module.main()
else:
    sys.modules[__name__] = _module
