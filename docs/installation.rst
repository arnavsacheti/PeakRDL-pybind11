Installation
============

Requirements
------------

- Python >= 3.10
- systemrdl-compiler >= 1.30.1
- jinja2
- CMake >= 3.15 (for building generated modules)
- C++11 compatible compiler (for building generated modules)
- pybind11 (runtime dependency for generated code)

Using pip
---------

The easiest way to install PeakRDL-pybind11 is using pip:

.. code-block:: bash

   pip install peakrdl-pybind11

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
