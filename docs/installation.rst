Installation
============

Requirements
------------

Runtime requirements (always installed):

- Python >= 3.10
- systemrdl-compiler >= 1.30.1
- jinja2
- **NumPy** — hard dependency
- CMake >= 3.15 (for building generated modules)
- C++11 compatible compiler (for building generated modules)
- pybind11 (runtime dependency for generated code)

NumPy is a **hard runtime dependency**, not optional. Bursts, bulk reads, and
the buffer protocol return ``ndarray`` from arrays, memory regions, and
snapshot frames; pretending NumPy were optional would just push the same
import into user code. There is no list-fallback shim and no apology — if
``peakrdl-pybind11`` is installed, ``numpy`` is installed.

Using pip
---------

The easiest way to install PeakRDL-pybind11 is using pip:

.. code-block:: bash

   pip install peakrdl-pybind11

Optional extras
---------------

Several features have *soft* dependencies. They are not required to import or
use the core API, but they unlock additional functionality when present:

- **ipywidgets** — powers ``watch()`` live monitor widgets in Jupyter
  notebooks. Without it, ``watch()`` falls back to a plain text refresh loop.
- **watchdog** — required by the ``--watch`` CLI flag, which rebuilds and
  hot-reloads the bound module when the source RDL changes.
- **pandas** — enables ``Snapshot.to_dataframe()`` for tabular introspection
  of all readable fields in a snapshot.

These can be installed individually, or grouped via the ``notebook`` extras
bundle (recommended for interactive workflows):

.. code-block:: bash

   pip install peakrdl-pybind11[notebook]

.. note::

   The ``[notebook]`` extras group is the intended UX for grouping the
   notebook/interactive soft dependencies. The exact set of extras groups
   may evolve as the API stabilizes.

From Source
-----------

To install from source:

.. code-block:: bash

   git clone https://github.com/arnavsacheti/PeakRDL-pybind11.git
   cd PeakRDL-pybind11
   pip install -e .

Development Installation
------------------------

For development, install with test dependencies:

.. code-block:: bash

   git clone https://github.com/arnavsacheti/PeakRDL-pybind11.git
   cd PeakRDL-pybind11
   pip install -e .
   pip install pytest pytest-cov

Verifying Installation
----------------------

Verify the installation by running:

.. code-block:: bash

   peakrdl pybind11 --help

You should see the help message for the pybind11 exporter.
