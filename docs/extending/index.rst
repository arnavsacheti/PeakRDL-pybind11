Extending PeakRDL-pybind11
==========================

PeakRDL-pybind11 ships three extension points sibling-unit code uses to add
behaviour without modifying the core. Downstream tooling authors use the same
seams.

.. toctree::
   :maxdepth: 1

   runtime
   exporter
   cli

Where to start
--------------

- :doc:`runtime` — register hook callbacks via
  ``peakrdl_pybind11.runtime._registry`` to enhance generated register/field
  classes, attach helpers post-create, or extend masters at attach time.
- :doc:`exporter` — drop a plugin module into
  ``peakrdl_pybind11.exporter_plugins`` to add a codegen pass (interrupt
  detection, schema emission, custom output formats) without modifying
  ``exporter.py``.
- :doc:`cli` — drop a module into ``peakrdl_pybind11.cli`` to add CLI flags to
  ``peakrdl pybind11`` (preempt the export, run something post-export, or both).
