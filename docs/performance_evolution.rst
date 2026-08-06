Performance evolution
=====================

Performance is one of the design inputs, alongside API coverage, generated-code
readability, package portability, and runtime ergonomics.  These plots track
those trade-offs across representative release milestones using the same
three-register SystemRDL input at every point.  The input file is unchanged
across all measured tags, so movement comes from the generator and runtime—not
from a changing register map.

.. note::
   v0.2.0 is the first comparable point: v0.1.0 does not produce a
   buildable reference package under the shared toolchain, so it is omitted
   instead of mixing missing build and runtime values into the plots.

Pipeline cost
-------------

.. figure:: _static/benchmarks/release-pipeline.svg
   :alt: Four line charts showing generation time, wheel build time, generation peak memory, and build peak memory from v0.2.0 through v0.8.5.
   :width: 100%

   End-to-end generation and clean wheel-build cost for each release milestone.

The generated surface grew substantially while clean build time stayed close
to 5–6 seconds on the reference machine.  From v0.2.0 to v0.8.5, generation
rose from 25 ms to 81 ms and build peak RSS from 350 MiB to 527 MiB; generation
peak RSS moved much less, from 45 MiB to 50 MiB.  For this series, compiler
memory—not generation memory—accounts for the meaningful capacity change.  The
intentionally small input isolates per-release overhead; it is not a
register-count scaling study.

Runtime and artifact cost
-------------------------

.. figure:: _static/benchmarks/release-runtime-size.svg
   :alt: Two line charts showing register read and write latency and generated source and wheel size from v0.2.0 through v0.8.5.
   :width: 100%

   Direct register-access latency and generated artifact size for the same input.

The v0.4 read spike records an instructive intermediate design: that release
rebuilt field metadata by inspecting the generated object on every read.  v0.5
moved the layout to generated lookup tables, cutting the measured read from
6.6 microseconds to 1.6 microseconds.  Later releases add richer values,
batching, runtime services, and the broader target API; by v0.8.5 the generated
sources are 123 KiB (about 5× v0.2.0), while the compiled wheel is 139 KiB
(about 1.4×).

100k+ scale envelope
--------------------

Release comparisons isolate fixed overhead with a tiny input.  This separate
stress sweep measures the current exporter across the full range from 1,000
registers / 5,000 fields through 100,001 registers / 500,005 fields.  Each
register is unique, contains five fields, and occupies one contiguous 32-bit
address slot; regfiles group 256 registers only to create realistic binding
chunks.

.. figure:: _static/benchmarks/scale-envelope.svg
   :alt: Three line charts showing compile and export time, generation peak memory, and generated source size from one thousand through more than one hundred thousand registers.
   :width: 100%

   Current-exporter scaling over the entire contiguous synthetic address region.

The upper point validates every node and every address from ``0x0`` through
``0x61a80`` before recording a result.  Compile and elaboration take 13.8
seconds, export takes 44.3 seconds, and full-region validation takes 0.33
seconds.  The export emits 391 binding chunks and 2.99 GiB of source while the
worker peaks at 6.56 GiB RSS.

Native compilation is already the limiting stage well below that envelope.  A
clean 1,000-register / 5,000-field control build takes 421 seconds, peaks at
5.41 GiB RSS, and produces an 11.77 MiB wheel.  A 100k-register wheel build is
therefore deliberately not launched as part of the checked-in sweep: its input
alone is nearly 3 GiB of generated C++ and Python source.  The benchmark keeps
wheel building opt-in through ``--build-max-registers`` so dedicated build
hosts can extend that boundary without conflating an unmeasured value with the
generation results.  A native read/write or transaction sweep across all 100k
registers would require that same full build, so no 100k runtime latency is
reported either.

Sparse 2 TiB address span
-------------------------

The same 1,000, 10,000, and 100,001-register shapes were also spread evenly
from ``0x0`` through ``0x200_0000_0000`` (2 TiB).  The benchmark checks the
absolute address of every generated register, including the exact upper
endpoint, rather than inferring the span from the first and last nodes.

.. figure:: _static/benchmarks/sparse-address-comparison.svg
   :alt: Three line charts comparing generation time, peak memory, and source size for contiguous layouts and sparse layouts spanning two tebibytes.
   :width: 100%

   Sparse address distance has little effect once register and field counts are held constant.

At the upper point, only 400,004 bytes contain registers within a
2,199,023,255,556-byte inclusive region: an address density of
``1.819e-7``, or about ``0.0000182%``.  Compile and elaboration take 13.95
seconds, full-address validation takes 0.365 seconds, and export takes 43.97
seconds.  The worker peaks at 6.90 GiB RSS and emits 3.00 GiB of source in the
same 391 binding chunks.  The corresponding contiguous export takes 44.30
seconds, peaks at 6.56 GiB, and emits 2.99 GiB.  The modest differences are
consistent with run-to-run variation and slightly longer address literals;
there is no memory or output growth proportional to the holes.

This result shows that generator cost follows populated node and field count,
not the numeric address span.  Generated access paths use 64-bit addresses, so
the 2 TiB endpoint is representable.  It does not imply that a contiguous 2 TiB
``MmapMaster`` backing is practical or portable, nor does it supply native
transaction latency at the 100k point: that measurement still requires the
full multi-gibibyte source build described above.

Methodology and reproduction
----------------------------

These are directional comparisons, not performance guarantees.  Absolute
times depend on the host, Python, compiler, CMake, pybind11, and system load.
All releases in a graph are collected serially in one run on the same host.

The benchmark code lives in the repository:

* `Release-history collector <https://github.com/arnavsacheti/PeakRDL-pybind11/blob/main/benchmarks/collect_release_metrics.py>`_
* `100k and sparse-region runner <https://github.com/arnavsacheti/PeakRDL-pybind11/blob/main/benchmarks/benchmark_scale_envelope.py>`_
* `Scale correctness and stress tests <https://github.com/arnavsacheti/PeakRDL-pybind11/blob/main/benchmarks/test_scale_envelope.py>`_
* `Checked benchmark results <https://github.com/arnavsacheti/PeakRDL-pybind11/tree/main/benchmarks/results>`_
* `Documentation graph renderer <https://github.com/arnavsacheti/PeakRDL-pybind11/blob/main/benchmarks/render_release_metrics.py>`_

.. list-table:: Measurement definitions
   :header-rows: 1
   :widths: 24 76

   * - Metric
     - Definition
   * - Generation
     - Median of seven clean SystemRDL compile, elaborate, and export runs.
   * - Build
     - One clean Release wheel build through ``python -m build`` and Ninja.
   * - Read / write
     - Median of seven 20,000-operation runs through the same Python-backed master; reported per call.
   * - Source size
     - All files emitted by the exporter before any build files are created.
   * - Wheel size
     - Size of the platform wheel produced by the clean build.
   * - Peak memory
     - Maximum resident set size for the generation worker or build process tree; includes tool and interpreter baselines.
   * - 100k+ sweep
     - One clean worker per size; verifies register count, field count, first address, last address, and contiguous region size before exporting.
   * - Sparse sweep
     - One clean worker per size; verifies every absolute register address over the exact 2 TiB span, occupied bytes, and address density before exporting.

The checked-in measurements were collected on an Apple M3 Max with macOS
26.6, CPython 3.13.2, Apple Clang, CMake 4.3.3, and pybind11 3.0.4.  Recreate
the JSON and SVGs with:

.. code-block:: console

   uv run --group benchmark --with scikit-build-core==0.10.7 --with ninja \
     python benchmarks/collect_release_metrics.py
   uv run --group benchmark --with scikit-build-core==0.10.7 --with ninja \
     python benchmarks/benchmark_scale_envelope.py --build-max-registers 1000
   uv run python benchmarks/benchmark_scale_envelope.py \
     --sizes 1000 10000 100001 --max-address 0x200_0000_0000 \
     --output benchmarks/results/sparse_scale_envelope.json
   uv run python benchmarks/render_release_metrics.py

The opt-in pytest stress case recreates the 100,001-register export and its
structural assertions:

.. code-block:: console

   PEAKRDL_RUN_100K_STRESS=1 uv run --group benchmark \
     pytest benchmarks/test_scale_envelope.py -m stress

Raw observations and environment metadata live in
``benchmarks/results/release_history.json`` and
``benchmarks/results/scale_envelope.json``, with the sparse observations in
``benchmarks/results/sparse_scale_envelope.json``.  Keep those files and the
generated SVGs in the same change when refreshing the figures.
