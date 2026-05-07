"""Runtime support utilities for PeakRDL-pybind11 generated SoC modules.

This subpackage holds pure-Python helpers that are imported (or invoked)
by both the host-side runtime and the generated bindings:

* ``hot_reload`` — ``soc.reload()``, generation/staleness tracking, and the
  active-context guard used by transaction-style context managers.

The submodule layout intentionally avoids cycles: each module is independently
importable and only depends on the standard library.
"""

from __future__ import annotations
