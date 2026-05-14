"""End-to-end integration test for register- and regfile-array support (issue #138).

Builds RDL fixtures with 1-D register and regfile arrays, compiles
the C++ extension via cmake/pybind11, then exercises the runtime
surface (``soc.lut[i]``, ``soc.channel[i].config``, slice access,
iteration, ``shape``/``stride``, independent per-entry writes,
identity stability).

Gated on cmake + pybind11 availability the same way
``test_encode_integration.py`` and ``test_native_masters_integration.py``
are: missing tooling → ``pytest.skip``. The full build takes ~6 minutes
the first time; the module-scoped fixtures below make every test share
a single build per RDL fixture.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

ARRAY_RDL = """
addrmap simple_array_soc {
    reg {
        field { sw=rw; hw=r; } data[31:0] = 0;
    } lut[8] @ 0x100;
};
"""

# Phase 2 (#138): arrayed regfile at the SoC root. Each ``channel[i]``
# holds two child registers (``config`` and ``stat``); the stride
# between channels is the regfile size (8 bytes).
REGFILE_ARRAY_RDL = """
addrmap dma_soc {
    regfile {
        reg {
            field { sw=rw; hw=r; } enable[0:0] = 0;
        } config @ 0x0;
        reg {
            field { sw=r; hw=w; } status[0:0];
        } stat @ 0x4;
    } channel[4] @ 0x100;
};
"""

# Phase 1 + Phase 2 side by side. Catches list-ordering regressions on
# the unified ``nodes["arrays"]`` list.
MIXED_ARRAY_RDL = """
addrmap mixed_array_soc {
    reg {
        field { sw=rw; hw=r; } data[31:0] = 0;
    } lut[8] @ 0x0;
    regfile {
        reg {
            field { sw=rw; hw=r; } enable[0:0] = 0;
        } config @ 0x0;
    } channel[2] @ 0x100;
};
"""

# Phase 3 (#138): multi-dim register + regfile arrays in one SoC,
# plus a 1-D register array to confirm 1-D and multi-dim coexist.
# Sharing one fixture keeps the cmake build cost bounded (one build
# instead of three).
MULTIDIM_RDL = """
addrmap multidim_soc {
    reg {
        field { sw=rw; hw=r; } data[31:0] = 0;
    } matrix[4][8] @ 0x100;

    regfile {
        reg {
            field { sw=rw; hw=r; } enable[0:0] = 0;
        } config @ 0x0;
    } channel[2][3] @ 0x300;

    reg {
        field { sw=rw; hw=r; } data[31:0] = 0;
    } lut[4] @ 0x500;
};
"""

# Phase 3 (#138): 3-D register array. Exercised lightly — the 2-D
# tests above cover the full surface; this confirms the codegen
# extends past 2-D.
CUBE_RDL = """
addrmap cube_soc {
    reg {
        field { sw=rw; hw=r; } data[31:0] = 0;
    } cube[2][3][4] @ 0x100;
};
"""


def _build_test_module(
    workdir: Path,
    rdl_text: str = ARRAY_RDL,
    soc_name: str = "simple_array_soc",
):
    """Export + cmake build + import. Returns module or None on failure."""
    rdl_path = workdir / f"{soc_name}.rdl"
    rdl_path.write_text(rdl_text)

    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()

    output_dir = workdir / "out"
    output_dir.mkdir()
    Pybind11Exporter().export(root.top, str(output_dir), soc_name=soc_name)

    build_dir = output_dir / "build"
    build_dir.mkdir()

    env = os.environ.copy()
    try:
        import pybind11
        env["pybind11_DIR"] = pybind11.get_cmake_dir()
    except ImportError:
        return None

    if subprocess.run(
        [
            "cmake",
            "-S", str(output_dir),
            "-B", str(build_dir),
            f"-DPython_EXECUTABLE={sys.executable}",
        ],
        capture_output=True,
        text=True,
        env=env,
    ).returncode != 0:
        return None
    if subprocess.run(
        ["cmake", "--build", str(build_dir), "--config", "Release"],
        capture_output=True,
        text=True,
        env=env,
    ).returncode != 0:
        return None

    so_files = list(build_dir.glob("**/*.so")) + list(build_dir.glob("**/*.pyd"))
    if not so_files:
        return None
    pkg_dir = output_dir / soc_name
    pkg_dir.mkdir(exist_ok=True)
    shutil.copy(so_files[0], pkg_dir)

    sys.path.insert(0, str(output_dir))
    spec = importlib.util.spec_from_file_location(
        soc_name, str(pkg_dir / "__init__.py")
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[soc_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - diagnostic only
        print(f"Failed to import generated module: {exc}")
        return None
    return module


@pytest.fixture(scope="module")
def soc_module(tmp_path_factory):
    """Build the C++ module once per test module run.

    Skips every dependent test when cmake/pybind11 isn't available; the
    unit-level tests in ``test_array_codegen.py`` still pin the codegen
    shape without needing the build.
    """
    workdir = tmp_path_factory.mktemp("array_integration")
    module = _build_test_module(workdir)
    if module is None:
        pytest.skip("Could not build test module (cmake/pybind11 unavailable)")
    return module


@pytest.fixture
def soc(soc_module):
    """A freshly-created SoC with the in-memory MockMaster attached."""
    s = soc_module.create()
    s.attach_master(soc_module.MockMaster())
    return s


@pytest.fixture(scope="module")
def regfile_array_module(tmp_path_factory):
    """Build the regfile-array fixture C++ module once per test module run."""
    workdir = tmp_path_factory.mktemp("regfile_array_integration")
    module = _build_test_module(workdir, rdl_text=REGFILE_ARRAY_RDL, soc_name="dma_soc")
    if module is None:
        pytest.skip("Could not build test module (cmake/pybind11 unavailable)")
    return module


@pytest.fixture
def dma_soc(regfile_array_module):
    """A freshly-created SoC with the in-memory MockMaster attached."""
    s = regfile_array_module.create()
    s.attach_master(regfile_array_module.MockMaster())
    return s


@pytest.fixture(scope="module")
def mixed_array_module(tmp_path_factory):
    """Build the mixed-array fixture C++ module."""
    workdir = tmp_path_factory.mktemp("mixed_array_integration")
    module = _build_test_module(
        workdir, rdl_text=MIXED_ARRAY_RDL, soc_name="mixed_array_soc"
    )
    if module is None:
        pytest.skip("Could not build test module (cmake/pybind11 unavailable)")
    return module


@pytest.fixture
def mixed_soc(mixed_array_module):
    s = mixed_array_module.create()
    s.attach_master(mixed_array_module.MockMaster())
    return s


class TestArraySurface:
    """The user-facing surface from the issue description."""

    def test_length(self, soc) -> None:
        """``len(soc.lut)`` returns the array size."""
        assert len(soc.lut) == 8

    def test_indexed_write_then_read_round_trip(self, soc) -> None:
        """``soc.lut[3].write(0x42)`` followed by ``read()`` round-trips."""
        soc.lut[3].write(0x42)
        assert int(soc.lut[3].read()) == 0x42

    def test_entries_are_independent(self, soc) -> None:
        """A write to one entry doesn't affect any other entry."""
        soc.lut[0].write(0xAAAA)
        soc.lut[1].write(0xBBBB)
        soc.lut[7].write(0xCCCC)
        assert int(soc.lut[0].read()) == 0xAAAA
        assert int(soc.lut[1].read()) == 0xBBBB
        assert int(soc.lut[7].read()) == 0xCCCC
        # Untouched entries stay at the field's reset value (0).
        assert int(soc.lut[2].read()) == 0

    def test_indexed_access_returns_same_instance(self, soc) -> None:
        """``soc.lut[3] is soc.lut[3]`` — pybind11 ``reference_internal``."""
        a = soc.lut[3]
        b = soc.lut[3]
        assert a is b, "expected identical instance on repeat indexing"

    def test_iteration(self, soc) -> None:
        """``list(soc.lut)`` yields 8 entry handles."""
        entries = list(soc.lut)
        assert len(entries) == 8

    def test_slice_returns_subset(self, soc) -> None:
        """``soc.lut[2:5]`` returns a 3-element view/sequence."""
        sliced = soc.lut[2:5]
        # ``ArrayView`` slices return ``ArrayView``; the raw C++ binding
        # slice returns ``list``. Either way, ``len`` is 3.
        assert len(sliced) == 3

    def test_shape_is_one_dim_tuple(self, soc) -> None:
        """``soc.lut.shape == (8,)`` for a 1-D Phase 1 array."""
        assert tuple(soc.lut.shape) == (8,)

    def test_stride_is_entry_size(self, soc) -> None:
        """``soc.lut.stride == 4`` (4 bytes per 32-bit register)."""
        assert int(soc.lut.stride) == 4

    def test_entry_addresses_follow_stride(self, soc) -> None:
        """Each entry has its own address: ``base + i * stride``."""
        base = soc.lut[0].offset
        for i in range(8):
            assert soc.lut[i].offset == base + i * 4

    def test_install_array_properties_idempotent(self, soc_module, soc) -> None:
        """Re-running ``_install_array_properties`` doesn't double-wrap.

        The exporter writes the runtime module to two paths
        (``out/__init__.py`` and ``out/<soc>/__init__.py``); some import
        configurations would exec both. The wrapper carries the
        ``_peakrdl_array_wrapper`` marker so a second invocation skips
        already-wrapped attributes.
        """
        from peakrdl_pybind11.runtime.arrays import ArrayView
        soc_module._install_array_properties()
        # After the second swap, the attribute lookup still returns an
        # ArrayView (not a wrapper-of-wrapper or a raw C++ binding).
        assert isinstance(soc.lut, ArrayView)
        # And reads/writes still work end-to-end.
        soc.lut[5].write(0xDEAD)
        assert int(soc.lut[5].read()) == 0xDEAD

    def test_lut_is_array_view(self, soc) -> None:
        """``soc.lut`` is wrapped by the runtime in an :class:`ArrayView`.

        Pins the user-facing contract: Phase 1 substitutes an ArrayView
        on every ``create()``. The raw C++ ``<entry>_array_t`` binding
        is reachable via the runtime's private descriptor cache (see
        ``_NATIVE_ARRAY_DESCRIPTORS``) but the public attribute is
        the wrapper.
        """
        from peakrdl_pybind11.runtime.arrays import ArrayView
        assert isinstance(soc.lut, ArrayView), (
            f"expected ArrayView, got {type(soc.lut).__name__}"
        )


class TestRegfileArraySurface:
    """Phase 2 (#138) — arrayed regfile end-to-end.

    Mirrors the Phase 1 register-array surface, plus per-entry member
    access (``channel[i].config.enable.write(1)``) and per-entry
    independence (writes to entry 1 don't disturb entry 2).
    """

    def test_length(self, dma_soc) -> None:
        assert len(dma_soc.channel) == 4

    def test_shape_is_one_dim_tuple(self, dma_soc) -> None:
        assert tuple(dma_soc.channel.shape) == (4,)

    def test_stride_is_regfile_size(self, dma_soc) -> None:
        """Stride between channels = regfile size = 2 regs × 4 bytes = 8."""
        assert int(dma_soc.channel.stride) == 8

    def test_indexed_access_returns_same_instance(self, dma_soc) -> None:
        """``dma_soc.channel[3] is dma_soc.channel[3]``."""
        a = dma_soc.channel[3]
        b = dma_soc.channel[3]
        assert a is b

    def test_iteration(self, dma_soc) -> None:
        entries = list(dma_soc.channel)
        assert len(entries) == 4

    def test_slice_returns_subset(self, dma_soc) -> None:
        sliced = dma_soc.channel[2:4]
        assert len(sliced) == 2

    def test_per_entry_member_access(self, dma_soc) -> None:
        """``channel[i].config.enable.write(1)`` round-trips via the bus."""
        dma_soc.channel[3].config.enable.write(1)
        assert int(dma_soc.channel[3].config.enable.read()) == 1

    def test_entries_are_independent(self, dma_soc) -> None:
        """Writes to one channel don't disturb the others."""
        dma_soc.channel[1].config.enable.write(1)
        assert int(dma_soc.channel[1].config.enable.read()) == 1
        # Channel 2's register stays at its reset value (0).
        assert int(dma_soc.channel[2].config.enable.read()) == 0
        # And channel 0/3 too.
        assert int(dma_soc.channel[0].config.enable.read()) == 0
        assert int(dma_soc.channel[3].config.enable.read()) == 0

    def test_entry_addresses_follow_stride(self, dma_soc) -> None:
        """``channel[i].offset == base + i * stride``."""
        base = int(dma_soc.channel[0].offset)
        stride = int(dma_soc.channel.stride)
        for i in range(4):
            assert int(dma_soc.channel[i].offset) == base + i * stride

    def test_inner_register_addresses_follow_channel(self, dma_soc) -> None:
        """The inner registers' addresses scale per channel.

        ``channel[i].config.offset == channel_base + i*stride + 0x0``
        ``channel[i].stat.offset == channel_base + i*stride + 0x4``
        """
        chan_base = int(dma_soc.channel[0].offset)
        chan_stride = int(dma_soc.channel.stride)
        for i in range(4):
            assert int(dma_soc.channel[i].config.offset) == chan_base + i * chan_stride
            assert int(dma_soc.channel[i].stat.offset) == chan_base + i * chan_stride + 0x4

    def test_channel_is_array_view(self, dma_soc) -> None:
        """``dma_soc.channel`` is wrapped in an :class:`ArrayView`."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        assert isinstance(dma_soc.channel, ArrayView)


class TestMixedArraysIntegration:
    """Phase 1 + Phase 2 side-by-side end-to-end."""

    def test_both_arrays_work(self, mixed_soc) -> None:
        """Both ``soc.lut[i]`` (reg array) and ``soc.channel[i]``
        (regfile array) work in the same SoC.
        """
        # Register array: P1 surface.
        assert len(mixed_soc.lut) == 8
        mixed_soc.lut[3].write(0xDEAD)
        assert int(mixed_soc.lut[3].read()) == 0xDEAD

        # Regfile array: P2 surface.
        assert len(mixed_soc.channel) == 2
        mixed_soc.channel[1].config.enable.write(1)
        assert int(mixed_soc.channel[1].config.enable.read()) == 1
        # Independence across the two arrays.
        assert int(mixed_soc.lut[3].read()) == 0xDEAD
        assert int(mixed_soc.channel[0].config.enable.read()) == 0

    def test_both_arrays_are_array_views(self, mixed_soc) -> None:
        from peakrdl_pybind11.runtime.arrays import ArrayView
        assert isinstance(mixed_soc.lut, ArrayView)
        assert isinstance(mixed_soc.channel, ArrayView)


# ---------------------------------------------------------------------------
# Phase 3 (#138) — multi-dim register + regfile arrays.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def multidim_module(tmp_path_factory):
    """Build the multi-dim fixture C++ module once per test module run.

    Holds a 2-D register array, a 2-D regfile array, and a 1-D
    register array side by side so the same build covers every
    multi-dim integration assertion.
    """
    workdir = tmp_path_factory.mktemp("multidim_integration")
    module = _build_test_module(
        workdir, rdl_text=MULTIDIM_RDL, soc_name="multidim_soc"
    )
    if module is None:
        pytest.skip("Could not build test module (cmake/pybind11 unavailable)")
    return module


@pytest.fixture
def multidim_soc(multidim_module):
    s = multidim_module.create()
    s.attach_master(multidim_module.MockMaster())
    return s


@pytest.fixture(scope="module")
def cube_module(tmp_path_factory):
    """Build a 3-D register array fixture; thin coverage for ``N > 2``."""
    workdir = tmp_path_factory.mktemp("cube_integration")
    module = _build_test_module(
        workdir, rdl_text=CUBE_RDL, soc_name="cube_soc"
    )
    if module is None:
        pytest.skip("Could not build test module (cmake/pybind11 unavailable)")
    return module


@pytest.fixture
def cube_soc(cube_module):
    s = cube_module.create()
    s.attach_master(cube_module.MockMaster())
    return s


class TestMultiDimRegisterArraySurface:
    """``reg matrix[4][8]`` — the canonical Phase 3 surface."""

    def test_shape_is_2d_tuple(self, multidim_soc) -> None:
        """``soc.matrix.shape == (4, 8)``."""
        assert tuple(multidim_soc.matrix.shape) == (4, 8)

    def test_len_returns_outer_dim(self, multidim_soc) -> None:
        """``len(soc.matrix) == 4`` — Python convention is outer-dim length."""
        assert len(multidim_soc.matrix) == 4

    def test_tuple_indexing_round_trip(self, multidim_soc) -> None:
        """``soc.matrix[2, 5].write(0x42)`` then read returns 0x42."""
        multidim_soc.matrix[2, 5].write(0x42)
        assert int(multidim_soc.matrix[2, 5].read()) == 0x42

    def test_chained_indexing_equals_tuple_indexing(self, multidim_soc) -> None:
        """``soc.matrix[2][5] is soc.matrix[2, 5]``.

        Both hit the same flat index ``2 * 8 + 5 = 21`` and the
        underlying pybind11 ``reference_internal`` returns the same
        entry object both times.
        """
        a = multidim_soc.matrix[2][5]
        b = multidim_soc.matrix[2, 5]
        assert a is b

    def test_per_entry_address_follows_row_major(self, multidim_soc) -> None:
        """``soc.matrix[a, b].offset == base + a * stride_a + b * stride_b``.

        Base = ``0x100``; ``stride_a = 32`` (8 inner * 4 entry); ``stride_b = 4``.
        """
        base = int(multidim_soc.matrix[0, 0].offset)
        for a in range(4):
            for b in range(8):
                assert int(multidim_soc.matrix[a, b].offset) == base + a * 32 + b * 4

    def test_entries_are_independent(self, multidim_soc) -> None:
        """Writes to ``[2, 5]`` don't disturb other entries."""
        multidim_soc.matrix[2, 5].write(0xDEAD)
        # Untouched entries still read 0 (their reset value).
        assert int(multidim_soc.matrix[2, 4].read()) == 0
        assert int(multidim_soc.matrix[2, 6].read()) == 0
        assert int(multidim_soc.matrix[1, 5].read()) == 0
        assert int(multidim_soc.matrix[3, 5].read()) == 0
        # And the original write survives the reads above.
        assert int(multidim_soc.matrix[2, 5].read()) == 0xDEAD

    def test_int_indexing_returns_array_view_subset(self, multidim_soc) -> None:
        """``soc.matrix[2]`` returns a length-8 ``ArrayView`` of the row."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        row = multidim_soc.matrix[2]
        assert isinstance(row, ArrayView)
        assert tuple(row.shape) == (8,)
        assert len(row) == 8

    def test_slicing_along_inner_axis(self, multidim_soc) -> None:
        """``soc.matrix[:, 3]`` returns a length-4 1-D ``ArrayView``.

        Slicing along axis 1 with a scalar on axis 0 collapses to 1-D
        of length 4 (one element per row of the outer axis).
        """
        from peakrdl_pybind11.runtime.arrays import ArrayView
        col = multidim_soc.matrix[:, 3]
        assert isinstance(col, ArrayView)
        assert tuple(col.shape) == (4,)
        assert len(col) == 4

    def test_slicing_along_outer_axis(self, multidim_soc) -> None:
        """``soc.matrix[1:3, :]`` returns a 2x8 ``ArrayView``."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        sub = multidim_soc.matrix[1:3, :]
        assert isinstance(sub, ArrayView)
        assert tuple(sub.shape) == (2, 8)
        assert len(sub) == 2

    def test_matrix_is_array_view(self, multidim_soc) -> None:
        """The user-facing attribute is an ``ArrayView`` (not raw C++)."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        assert isinstance(multidim_soc.matrix, ArrayView)

    def test_install_array_properties_idempotent_multidim(
        self, multidim_module, multidim_soc
    ) -> None:
        """Re-running ``_install_array_properties`` on a multi-dim module
        stays idempotent. The ``_strip_soc_root`` change (``if`` → ``while``
        to handle the multi-``[]`` suffix on multi-dim paths) is the
        load-bearing piece here.
        """
        from peakrdl_pybind11.runtime.arrays import ArrayView
        multidim_module._install_array_properties()
        assert isinstance(multidim_soc.matrix, ArrayView)
        # Round-trip still works after the second swap.
        multidim_soc.matrix[2, 5].write(0x99)
        assert int(multidim_soc.matrix[2, 5].read()) == 0x99


class TestMultiDimRegfileArraySurface:
    """``regfile channel[2][3]`` — multi-dim regfile array end-to-end."""

    def test_shape_is_2d_tuple(self, multidim_soc) -> None:
        assert tuple(multidim_soc.channel.shape) == (2, 3)

    def test_tuple_indexing_into_regfile_member(self, multidim_soc) -> None:
        """``soc.channel[0, 1].config.enable.write(1)`` round-trips."""
        multidim_soc.channel[0, 1].config.enable.write(1)
        assert int(multidim_soc.channel[0, 1].config.enable.read()) == 1

    def test_regfile_entries_independent(self, multidim_soc) -> None:
        """Writes to ``channel[1, 2]`` don't disturb ``channel[0, 0]``."""
        multidim_soc.channel[1, 2].config.enable.write(1)
        assert int(multidim_soc.channel[0, 0].config.enable.read()) == 0
        assert int(multidim_soc.channel[1, 2].config.enable.read()) == 1

    def test_chained_equals_tuple_for_regfile(self, multidim_soc) -> None:
        """``soc.channel[0][1] is soc.channel[0, 1]``."""
        a = multidim_soc.channel[0][1]
        b = multidim_soc.channel[0, 1]
        assert a is b

    def test_regfile_member_addresses_follow_strides(self, multidim_soc) -> None:
        """``channel[a, b].config.offset == base + a * stride_a + b * stride_b``.

        Each channel holds one 4-byte register; ``stride_b = 4``;
        ``stride_a = 3 * 4 = 12``. Base = 0x300.
        """
        base = int(multidim_soc.channel[0, 0].config.offset)
        for a in range(2):
            for b in range(3):
                assert (
                    int(multidim_soc.channel[a, b].config.offset)
                    == base + a * 12 + b * 4
                )

    def test_channel_is_array_view(self, multidim_soc) -> None:
        from peakrdl_pybind11.runtime.arrays import ArrayView
        assert isinstance(multidim_soc.channel, ArrayView)


class TestMixedDimensionsIntegration:
    """1-D and multi-dim arrays coexist in the same SoC."""

    def test_one_d_and_multi_dim_side_by_side(self, multidim_soc) -> None:
        """A 1-D ``lut[4]`` next to a 2-D ``matrix[4][8]`` and 2-D ``channel[2][3]``."""
        # 1-D still works.
        assert tuple(multidim_soc.lut.shape) == (4,)
        multidim_soc.lut[2].write(0xCAFE)
        assert int(multidim_soc.lut[2].read()) == 0xCAFE
        # 2-D register array works.
        assert tuple(multidim_soc.matrix.shape) == (4, 8)
        multidim_soc.matrix[1, 2].write(0xBEEF)
        assert int(multidim_soc.matrix[1, 2].read()) == 0xBEEF
        # 2-D regfile array works.
        assert tuple(multidim_soc.channel.shape) == (2, 3)
        multidim_soc.channel[0, 1].config.enable.write(1)
        assert int(multidim_soc.channel[0, 1].config.enable.read()) == 1


class TestCubeIntegration:
    """3-D register array — thin coverage; full 2-D is the load-bearing case."""

    def test_3d_shape(self, cube_soc) -> None:
        assert tuple(cube_soc.cube.shape) == (2, 3, 4)

    def test_3d_tuple_indexing_round_trip(self, cube_soc) -> None:
        cube_soc.cube[1, 2, 3].write(0x55)
        assert int(cube_soc.cube[1, 2, 3].read()) == 0x55

    def test_3d_chained_equals_tuple(self, cube_soc) -> None:
        """``cube[1][2][3] is cube[1, 2, 3]``."""
        a = cube_soc.cube[1][2][3]
        b = cube_soc.cube[1, 2, 3]
        assert a is b

    def test_3d_per_entry_address(self, cube_soc) -> None:
        """Address chain: ``base + a*48 + b*16 + c*4``.

        With base=0x100, stride_a=48 (=3*16), stride_b=16 (=4*4),
        stride_c=4 (entry size). ``cube[1, 2, 3].offset == 0x100 + 48 + 32 + 12 = 0x100 + 92 = 0x15c``.
        """
        base = int(cube_soc.cube[0, 0, 0].offset)
        assert int(cube_soc.cube[1, 2, 3].offset) == base + 48 + 2 * 16 + 3 * 4
