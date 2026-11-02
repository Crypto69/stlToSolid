"""Moved to stl_to_solid.service.freeform (the wheel does not ship backend/);
this name stays an alias of the same module, so monkeypatching and
importlib.reload see one object."""
import sys

from stl_to_solid.service import freeform as _module

sys.modules[__name__] = _module
