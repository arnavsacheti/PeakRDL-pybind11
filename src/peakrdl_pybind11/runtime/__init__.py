"""Pure-Python runtime helpers attached to generated SoC modules.

The runtime package collects the post-import enhancements that wrap each
generated register/field class with the high-level surface described in
``docs/IDEAL_API_SKETCH.md``: typed return values, bit-level proxies,
multi-field RMW shims, etc. Modules in this package are intentionally free
of generated-code references so they can be imported and unit-tested
without compiling C++.
"""

from __future__ import annotations
