SystemRDL Feature Matrix
========================

This page surveys SystemRDL features against the **planned** Python API described
in ``docs/IDEAL_API_SKETCH.md``. The sketch is the source of truth: it is
aspirational, and code is catching up to it. Status values mean:

- ``implemented`` — shipped today and behaves as the sketch describes.
- ``partial`` — exists but does not yet match the sketch's surface or semantics.
- ``planned`` — defined by the sketch; the exporter does not yet emit it.

Nearly all runtime-surface rows are now ``implemented``; the remaining
``partial`` rows cluster around arbitrary UDP typed-wrapper mapping and
exhaustive ``.pyi`` stub generation. Rows are organized by SystemRDL
category and reference the conceptual docs (in ``docs/concepts/``) where
applicable.

Structural Components
---------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``addrmap``
     - ``AddrMap`` node; ``soc.peripherals.uart``; ``.info.address``
     - implemented
     - Hierarchy works and the uniform ``.info`` namespace (``info.address``, ``info.regwidth``, ``info.path``, ``info.fields``, ``info.tags``) ships via ``runtime/info.py``. See :doc:`concepts/hierarchy`.
   * - ``regfile``
     - ``RegFile`` node; ``soc.dma.channel`` (when arrayed)
     - implemented
     - Grouping and array indexing both ship via the ``ArrayView`` surface in ``runtime/arrays.py`` (int/slice/tuple indexing, field projection, bulk read/write). See :doc:`concepts/arrays`.
   * - ``reg``
     - ``Reg`` node; ``reg.read()``, ``reg.write(value)``, ``reg.modify(**fields)``, ``reg.poke(value)``
     - implemented
     - Typed ``RegisterValue`` returns and the ``modify(**fields)`` kwargs surface ship via ``runtime/_default_shims.py`` (reads return ``RegisterValue``; ``modify`` accepts both ``(value, mask)`` and ``**fields``).
   * - ``field``
     - ``Field`` node; ``field.read()``, ``field.write(v)``, ``field.bits[i]``
     - implemented
     - Bit-slice access (``field.bits[i]`` / ``field.bits[a:b]``) ships via ``runtime/bits.py`` and ``field.read()`` returns a typed ``FieldValue`` from ``runtime/values.py``.
   * - ``mem``
     - ``Mem`` node; ``mem[i]``, ``mem[i:j]``, ``MemView``, ``.copy()``, ``.read()``,
       ``read_into``, buffer-protocol/``np.asarray``
     - implemented
     - The NumPy-aware :class:`MemView` (live slicing, ``__array__`` / ``np.asarray(mem)``, ``.copy()`` / ``.read()`` / ``read_into`` / ``window``) ships via ``runtime/mem_view.py`` (wired by ``attach_mem_view``).
       See :doc:`concepts/memory`.
   * - ``signal``
     - ``Signal`` node, metadata only via ``.info``
     - implemented
     - Frozen :class:`Signal` dataclasses ship via ``runtime/signals.py`` (attached to their parent node by the ``register_signals`` post-create hook emitted from ``templates/runtime.py.jinja``).
   * - ``constraint``
     - none
     - planned
     - Intentionally out of scope for the register-access runtime; the RDL ``constraint`` declaration is ignored during export.

Software Access (``sw``)
------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``sw = rw``
     - ``field.read()``, ``field.write(v)``; ``info.access = AccessMode.RW``
     - implemented
     - Reads/writes and the typed ``AccessMode`` enum on ``.info`` both ship via ``runtime/info.py`` (the ``Info.access`` field coerces RDL tokens to ``AccessMode.RW``/``R``/``W``/``NA``).
   * - ``sw = r`` (read-only)
     - ``field.read()``; ``field.write(v)`` raises ``AccessError``
     - implemented
     - Writes to read-only fields raise :class:`AccessError` via ``runtime/_default_shims.py`` (the ``_enhanced_field_write`` gate fires before the bus is touched).
   * - ``sw = w`` (write-only)
     - ``field.write(v)``; ``field.read()`` raises ``AccessError``
     - implemented
     - Reads from write-only fields raise :class:`AccessError` via ``runtime/_default_shims.py`` (the ``_enhanced_field_read`` gate blocks the raw fast path too).
   * - ``sw = na`` (no software access)
     - hidden from autocomplete, raises on access
     - implemented
     - Both reads and writes raise :class:`AccessError` via ``runtime/_default_shims.py`` when ``is_readable`` and ``is_writable`` are both false on the generated field.

Hardware Access (``hw``)
------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``hw = rw / r / w / na``
     - ``info.is_hw_readable``, ``info.is_hw_writable``
     - implemented
     - ``info.is_hw_readable`` / ``info.is_hw_writable`` ship on the uniform ``Info`` namespace in ``runtime/info.py``.

Reset Values
------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``reset`` (field/register)
     - ``info.reset``; ``reg.reset_value``; ``reg.is_at_reset()``;
       ``soc.reset_all()``, ``soc.uart.reset_all(rw_only=True)``
     - implemented
     - ``info.reset`` ships via ``runtime/info.py``; ``reg.reset_value`` / ``reg.is_at_reset()`` and ``soc.reset_all(rw_only=…)`` ship via ``runtime/specialized.py`` (``attach_reset_helpers`` / ``attach_soc_reset_all``).

Encoding (``encode = MyEnum``)
------------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``encode``
     - Generated ``enum.IntEnum``; ``field.read()`` returns ``BaudRate.BAUD_19200``;
       ``field.write(BaudRate.BAUD_115200)``; ``field.choices``
     - implemented
     - Per-field RDL ``encode`` IntEnum classes are emitted by ``templates/runtime.py.jinja`` and threaded through :class:`FieldValue` decoding and ``field.choices`` by the ``encode`` metadata pipe in ``runtime/_default_shims.py``.

Arrays
------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - Register array (``reg my_reg[N]``)
     - ``soc.lut[i]``, ``soc.lut[:]``, ``ArrayView``,
       ``.read() -> ndarray[uint32]``, ``soc.lut.info.shape``,
       ``soc.walk(kind="array")``
     - implemented
     - Phases 1-5 of the Tier 3 plan (issue #138) plus the addrmap-array
       follow-up (PR #139). 1-D register arrays (Phase 1) plus regfile
       (Phase 2), multi-dim (Phase 3), field (Phase 4), and addrmap
       arrays. Phase 5 wires every array kind into the rest of the
       runtime metadata surface: :class:`ArrayInfo` on ``arr.info``;
       ``soc.walk(kind="array")``; single-line array rendering in
       ``soc.tree()`` / ``soc.dump()`` (with opt-in
       ``show_array_entries=True`` for per-entry expansion);
       snapshot synthesizes ``soc.lut[i]`` paths per entry; schema
       export emits ``kind="array"`` nodes with nested entry shape.
       Codegen seam: the C++ ``ArrayBase`` template in
       ``templates/descriptors/base_classes.hpp.jinja`` plus the
       per-array typedef partials in ``templates/descriptors/arrays.hpp.jinja``
       (registers), ``regfile_arrays.hpp.jinja`` (regfiles), and
       ``addrmap_arrays.hpp.jinja`` (addrmaps); Python wrapping via
       ``runtime/arrays.py:wrap_array`` and the ``_wrap_arrays`` hook
       emitted by ``templates/runtime.py.jinja``.
   * - Regfile array (``regfile rf[N]``)
     - ``soc.dma.channel[3]``, iteration, slice
     - implemented
     - Phase 2 of the Tier 3 plan (issue #138). 1-D regfile arrays at the SoC root or
       under non-arrayed regfiles. Per-entry member access works
       (``soc.channel[3].config.enable.write(1)``). Multi-dim and arrays
       nested inside another array remain planned. Codegen seam: the
       per-array typedef partial in
       ``templates/descriptors/regfile_arrays.hpp.jinja`` (included after
       ``regfiles.hpp.jinja`` so the entry type is complete before
       ``std::vector<<rf>_t>`` instantiates); unified ``nodes["arrays"]``
       list in ``exporter.py`` with a ``kind`` discriminator; runtime
       wrapping shares ``_ARRAY_PATHS`` with register arrays.
   * - Multi-dim array (``reg r[A][B]``)
     - ``soc.regblock.my_reg[2, 5]``, ``.shape``
     - implemented
     - Phase 3 of the Tier 3 plan (issue #138). N-D register and regfile arrays
       emit ``N`` nested ``ArrayBase`` subclasses (one per axis); the Python
       ``ArrayView`` wraps the flattened element list and reports the full
       multi-dim ``shape`` tuple. Tuple indexing (``soc.matrix[2, 5]``) and
       chained indexing (``soc.matrix[2][5]``) reach the same flat entry per
       row-major. Per-axis strides are computed from ``systemrdl``'s
       innermost ``array_stride`` and multiplied outward by inner-axis sizes.
       Codegen seam: the per-axis emission loop in
       ``templates/descriptors/arrays.hpp.jinja`` /
       ``templates/descriptors/regfile_arrays.hpp.jinja`` plus the shared
       ``_array_binding_macros.jinja`` partial.
   * - Field array (``mode[16]``)
     - ``field.bits[i]`` / ``field.bits[a:b]`` with slice semantics
     - implemented
     - Phase 4 of the Tier 3 plan (issue #138). SystemRDL has no separate
       ``field foo[N]`` array syntax — ``mode[16]`` declares a **width-16
       field**, and the per-bit handles ship through the ``BitsAccessor``
       on the (multi-bit) field. ``runtime/bits.py:BitsAccessor`` covers
       int / slice / iteration / ``__setitem__`` and routes writes through
       a single RMW on the parent register. The sketch's ``FieldArray``
       name and this runtime's ``BitsAccessor`` describe the same feature.
       See :doc:`concepts/arrays` §"Field arrays".
   * - Address stride
     - resolved via ``info.address`` per element
     - implemented
     - Phase 3 of the Tier 3 plan (issue #138). The C++ ``ArrayBase`` ctor
       pre-fills the per-level stride from the exporter's ``strides`` list
       and recomputes per-entry absolute addresses at construction time;
       arrayed regfile / multi-dim register paths both walk
       ``raw_address_offset`` because ``address_offset`` raises on
       un-indexed array nodes. The runtime's ``_array_base_address``
       Jinja filter feeds the same metadata into ``_REGISTER_INFO``.

Aliases (``alias``)
-------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``alias`` register
     - ``soc.uart.control_alt.target``, ``soc.uart.control.aliases``,
       ``info.alias_kind ∈ {full, sw_view, hw_view, scrambled}``,
       ``info.is_alias``
     - implemented
     - Alias relationship (``alt.target`` / ``primary.aliases`` / ``info.alias_kind``) ships via ``runtime/aliases.py`` (wired by ``apply_alias_relationship`` and the post-create attach hook).

Interrupts (``intr``)
---------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``intr`` field property
     - ``field.info.is_interrupt_source``;
       ``InterruptGroup`` synthesized from ``INTR_STATE`` / ``INTR_ENABLE`` / ``INTR_TEST``
     - implemented
     - ``info.is_interrupt_source`` ships via ``runtime/info.py``; the synthesized :class:`InterruptGroup` ships via ``runtime/interrupts.py`` (built from detected state/enable/test partners and bound under ``soc.<block>.interrupts``).
   * - Per-source ops
     - ``soc.uart.interrupts.tx_done.is_pending()``, ``.enable()``, ``.disable()``,
       ``.clear()``, ``.acknowledge()``, ``.fire()``, ``.wait(timeout=)``,
       ``.aiowait(...)``, ``.poll(...)``
     - implemented
     - :class:`InterruptSource` ships in ``runtime/interrupts.py`` (``is_pending``, ``enable``/``disable``, ``clear``, ``acknowledge``, ``fire``, ``wait``, ``aiowait``, ``poll``, ``on_fire``).
   * - Group ops
     - ``soc.uart.interrupts.pending()``, ``.enabled()``, ``.clear_all()``,
       ``.disable_all()``, ``.snapshot()``
     - implemented
     - :class:`InterruptGroup` ships in ``runtime/interrupts.py`` with ``pending``, ``enabled``, ``clear_all``, ``disable_all``, ``enable``, ``snapshot`` over the per-source set.
   * - Top-level tree
     - ``soc.interrupts.tree()``, ``soc.interrupts.pending()``,
       ``soc.interrupts.wait_any(timeout=)``
     - implemented
     - :class:`InterruptTree` ships in ``runtime/interrupts.py`` (``pending`` / ``wait_any`` / ``tree``); installed on the SoC by the ``interrupts_post_create`` hook.
   * - Detection / overrides
     - ``--interrupt-pattern`` flag; ``InterruptGroup.manual(state=, enable=, test=)``
     - implemented
     - The ``--interrupt-pattern`` flag ships in ``__peakrdl__.py`` and feeds the detector; manual overrides ship as :meth:`InterruptGroup.manual` in ``runtime/interrupts.py``.
   * - ``enable`` / ``mask`` / ``haltmask`` / ``haltenable``
     - mapped onto ``InterruptGroup`` controls
     - implemented
     - All four partner registers fold into :class:`InterruptGroup` via the detector in ``exporter_plugins/feature_detection.py`` and the per-source ``mask`` / ``unmask`` / ``halt_mask`` / ``halt_enable`` verbs in ``runtime/interrupts.py``.

Signals (``signal``)
--------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``signal`` declaration
     - ``Signal`` node, metadata-only via ``.info``
     - implemented
     - Frozen :class:`Signal` dataclasses (name, width, sw/hw access, ``activelow``, ``async``, any UDPs) ship via ``runtime/signals.py`` and are attached to their parent node by the ``register_signals`` hook emitted from ``templates/runtime.py.jinja``.
   * - Component port declarations
     - none
     - planned
     - Intentionally out of scope for the register-access surface; ports are ignored during export.

Counters
--------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``counter``
     - ``c.value()``, ``c.reset()``, ``c.threshold()``, ``c.is_saturated()``,
       ``c.increment(by=)``, ``c.decrement(by=)``
     - implemented
     - The :class:`Counter` wrapper ships via ``runtime/specialized.py`` (``attach_counter`` installs it on counter-flavoured registers via the post-create seam).
   * - ``incrthreshold`` / ``incrsaturate``
     - ``c.threshold()``, ``c.is_saturated()``
     - implemented
     - ``Counter.threshold`` / ``.is_saturated`` consume the RDL ``incrthreshold`` / ``incrsaturate`` properties via ``runtime/specialized.py``.

Single-pulse
------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``singlepulse``
     - ``field.pulse()`` — writes 1; hardware self-clears
     - implemented
     - ``field.pulse()`` ships via ``runtime/specialized.py`` (``attach_pulse`` installs it on singlepulse fields; routed through ``runtime/side_effects.py`` for non-singlepulse fields).

Sticky / Stickybit
------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``sticky`` / ``stickybit``
     - ``info.is_volatile``; metadata only
     - implemented
     - ``info.is_volatile`` ships on the uniform ``Info`` namespace in ``runtime/info.py`` (set when any of ``hwclr`` / ``hwset`` / ``sticky`` / ``stickybit`` / ``counter`` is declared).

HW-side effects (``hwclr`` / ``hwset``)
---------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``hwclr`` / ``hwset``
     - ``info.is_volatile = True``
     - implemented
     - ``info.is_volatile`` ships via ``runtime/info.py`` and is consumed by the cache-policy refusal in ``runtime/bus_policies.py``.

Read-side effects (``onread``)
------------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``onread = rclr``
     - ``info.on_read = ReadEffect.RCLR``; ``field.read()`` warns / clears,
       ``field.peek()`` reads without clearing if master supports it
     - implemented
     - ``info.on_read = "rclr"`` ships via ``runtime/info.py``; the matching ``peek`` / ``clear`` / ``no_side_effects`` verbs ship via ``runtime/side_effects.py``.
   * - ``onread = rset``
     - ``info.on_read = ReadEffect.RSET``
     - implemented
     - ``info.on_read = "rset"`` ships via ``runtime/info.py``; the destructive-read guard in ``runtime/side_effects.py`` treats it identically to ``rclr``.
   * - ``onread = ruser``
     - ``info.on_read = ReadEffect.RUSER``
     - implemented
     - ``info.on_read = "ruser"`` ships via ``runtime/info.py`` and is included in the destructive-read effect set in ``runtime/side_effects.py``.

Write-side effects (``onwrite``)
--------------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``onwrite = woclr`` (W1C)
     - ``info.on_write = WriteEffect.W1C``; ``field.clear()`` writes 1
     - implemented
     - ``info.on_write = "woclr"`` ships via ``runtime/info.py``; ``field.clear()`` writes 1 via ``runtime/side_effects.py``.
   * - ``onwrite = woset`` (W1S)
     - ``info.on_write = WriteEffect.W1S``; ``field.set()`` writes 1
     - implemented
     - ``info.on_write = "woset"`` ships via ``runtime/info.py``; ``field.set()`` writes 1 via ``runtime/side_effects.py``.
   * - ``onwrite = wzc`` (W0C)
     - ``info.on_write = WriteEffect.WZC``; ``field.clear()`` writes 0
     - implemented
     - ``info.on_write = "wzc"`` ships via ``runtime/info.py``; ``field.clear()`` writes 0 via ``runtime/side_effects.py``.
   * - ``onwrite = wzs`` (W0S)
     - ``info.on_write = WriteEffect.WZS``; ``field.set()`` writes 0
     - implemented
     - ``info.on_write = "wzs"`` ships via ``runtime/info.py``; ``field.set()`` writes 0 via ``runtime/side_effects.py``.
   * - ``onwrite = wclr``
     - ``info.on_write = WriteEffect.WCLR``; ``field.clear()`` writes anything
     - implemented
     - ``info.on_write = "wclr"`` ships via ``runtime/info.py``; ``field.clear()`` writes 0 (any value satisfies the effect) via ``runtime/side_effects.py``.
   * - ``onwrite = wset``
     - ``info.on_write = WriteEffect.WSET``; ``field.set()`` writes anything
     - implemented
     - ``info.on_write = "wset"`` ships via ``runtime/info.py``; ``field.set()`` writes all-ones via ``runtime/side_effects.py``.
   * - ``onwrite = wuser``
     - ``info.on_write = WriteEffect.WUSER``
     - implemented
     - ``info.on_write = "wuser"`` ships via ``runtime/info.py``; user-defined write effects surface as metadata without an automatic verb in ``runtime/side_effects.py``.

Parity Check
------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``paritycheck``
     - ``info.paritycheck = True``
     - implemented
     - ``info.paritycheck`` ships on the uniform ``Info`` namespace in ``runtime/info.py`` (extracted from the RDL ``paritycheck`` property).

Precedence
----------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``precedence = sw | hw``
     - ``info.precedence = Precedence.SW`` / ``Precedence.HW``
     - implemented
     - ``info.precedence`` ships on the uniform ``Info`` namespace in ``runtime/info.py`` (RDL ``precedence`` mapped to ``"sw"`` / ``"hw"`` tokens).

SW notification (``swwe`` / ``swwel`` / ``swacc`` / ``swmod``)
--------------------------------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``swwe`` / ``swwel`` (write enable)
     - metadata only via ``.info``
     - implemented
     - ``info.swwe`` / ``info.swwel`` ship on the uniform :class:`Info` namespace in ``runtime/info.py`` (coerced from the RDL property to ``bool`` for unconditional gates or a signal-path ``str`` for conditional ones).
   * - ``swacc``
     - ``info.swacc``
     - implemented
     - ``info.swacc`` ships on the uniform :class:`Info` namespace in ``runtime/info.py`` (extracted as a bool from the RDL ``swacc`` notification property).
   * - ``swmod``
     - ``info.swmod``
     - implemented
     - ``info.swmod`` ships on the uniform :class:`Info` namespace in ``runtime/info.py`` (extracted as a bool from the RDL ``swmod`` notification property).

Lock
----

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``lock``
     - ``soc.gpio_a.lckr.lock([...])``, ``soc.gpio_a.lckr.is_locked(name)``,
       ``soc.gpio_a.lckr.unlock_sequence()``
     - implemented
     - The :class:`LockController` (``lock`` / ``is_locked`` / ``unlock_sequence``) ships via ``runtime/specialized.py`` (installed by ``attach_lock_controller``).

Bit ordering & widths
---------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``msb0`` / ``lsb0``
     - hidden — the user always sees little-endian ints
     - implemented
     - Hidden by design per §22 decision 5 of the sketch — the runtime always presents little-endian ints; bit ordering is preserved only on bursts and traces.
   * - ``regwidth``
     - ``info.regwidth``
     - implemented
     - ``info.regwidth`` ships today via the uniform ``Info`` namespace in ``runtime/info.py``.
   * - ``accesswidth``
     - hidden by default; available via ``.info.accesswidth``
     - implemented
     - ``info.accesswidth`` ships on the uniform ``Info`` namespace in ``runtime/info.py`` (extracted from the systemrdl node).

Memory regions
--------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``mementries``
     - ``mem.depth``
     - implemented
     - ``mem.depth`` ships as a property on the generated mem class via ``runtime/mem_view.py``.
   * - ``memwidth``
     - ``mem.word_width``
     - implemented
     - ``mem.word_width`` ships as a property on the generated mem class via ``runtime/mem_view.py``.
   * - ``mem`` slicing / NumPy
     - ``mem[i:j]`` returns ``MemView``; ``np.asarray(mem)``;
       ``mem.read_into(buf)``; ``mem.window(...)``
     - implemented
     - Slicing, ``np.asarray(mem)``, ``mem.read_into(buf)``, and the buffered ``mem.window(...)`` context manager all ship via ``runtime/mem_view.py``.
   * - ``mem`` ``sw`` / ``hw`` access
     - access-mode enforcement via ``AccessError``
     - implemented
     - Reads from write-only mems and writes to read-only mems raise :class:`AccessError` before the bus is touched via the gates in ``runtime/mem_view.py``.

User-Defined Properties (UDPs)
------------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - RDL feature
     - API surface
     - Status
     - Notes
   * - ``is_flag`` (legacy UDP)
     - generated ``IntFlag`` per register
     - implemented
     - Today's mechanism. Planned to subsume into encode-driven flags per §8.2.
   * - ``is_enum`` (legacy UDP)
     - generated ``IntEnum`` per register
     - implemented
     - Today's mechanism. Planned to subsume into ``encode`` per §8.1.
   * - Arbitrary UDPs
     - ``info.tags.<name>``; ``--udp-config`` mapping for typed wrappers
     - partial
     - ``info.tags.<name>`` ships as a permissive ``TagsNamespace`` on the uniform :class:`Info` in ``runtime/info.py``; the ``--udp-config`` typed-wrapper mapping is not yet wired in the CLI.

Generated API surface
---------------------

These are the planned runtime surfaces, indexed for cross-reference.

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - Runtime feature
     - API surface
     - Status
     - Notes
   * - Typed values
     - ``RegisterValue``, ``FieldValue`` — immutable, hashable, picklable;
       ``.replace(**fields)``
     - implemented
     - Ships via ``runtime/values.py`` — ``RegisterValue`` / ``FieldValue`` are immutable ``int`` subclasses with ``.replace(**fields)`` and pickle round-trips.
   * - Format helpers
     - ``v.hex()``, ``v.hex(group=4)``, ``v.bin()``, ``v.table()``
     - implemented
     - ``.hex(group=…)`` / ``.bin(group=…, fields=…)`` / ``.table()`` ship on :class:`RegisterValue` in ``runtime/values.py``.
   * - Multi-field RMW
     - ``reg.modify(**fields)``; ``with reg as r: r.enable = 1``
     - implemented
     - ``reg.modify(**fields)`` and ``reg.write_fields(**fields)`` ship via ``runtime/_default_shims.py`` (``_make_write_fields`` collapses N writes to one RMW with did-you-mean diagnostics and access-mode gating).
   * - Skip-readback writes
     - ``reg.poke(value)``
     - implemented
     - ``reg.poke(value)`` aliases the bus write in ``runtime/_default_shims.py``; the matching ``with reg.write_only():`` context lives on ``RegisterWriteOnlyContext`` in the generated C++ base class.
   * - Bit-slice access
     - ``field.bits[5]``, ``field.bits[0:8]``
     - implemented
     - ``field.bits[i]`` / ``field.bits[a:b]`` ship via ``runtime/bits.py`` (``BitsAccessor`` plus single-bit and range proxies; writes RMW the parent register).
   * - Discovery
     - ``soc.find(addr)``, ``soc.find_by_name(...)``, ``soc.walk(kind=Reg)``,
       path-string indexing
     - implemented
     - ``soc.find`` / ``soc.find_by_name`` / ``soc.walk(kind=…)`` ship via ``runtime/routing.py`` (attached to every SoC by the post-create ``attach_discovery`` hook).
   * - Metadata namespace
     - ``.info.{address, path, fields, tags, is_volatile, is_interrupt_source,
       precedence, paritycheck, on_read, on_write, alias_kind, reset}``
     - implemented
     - The uniform :class:`Info` namespace ships via ``runtime/info.py`` (carrying ``address``/``path``/``fields``/``tags``/``is_volatile``/``is_interrupt_source``/``precedence``/``paritycheck``/``on_read``/``on_write``/``alias_kind``/``reset`` plus the new ``accesswidth`` and ``is_hw_readable`` / ``is_hw_writable`` flags).
   * - Rich repr
     - ``__repr__``, ``__str__``, ``_repr_pretty_``, ``_repr_html_``,
       ``soc.dump()``, ``soc.tree()``, ``reg.watch(period=)``
     - implemented
     - ``soc.tree()`` / ``soc.dump()`` and the ``_repr_html_`` / ``_repr_pretty_`` / ``watch()`` surface ship via ``runtime/widgets.py`` (attached by ``attach_widgets`` + the post-create ``_attach_tree_dump_to_soc`` hook).
   * - Wait / poll
     - ``field.wait_for(value, timeout=)``, ``reg.wait_until(predicate, timeout=)``,
       ``field.read(n=)`` (sample), ``field.histogram(n=)``
     - implemented
     - ``wait_for`` / ``wait_until`` / ``sample`` / ``histogram`` ship via ``runtime/wait_poll.py`` (attached to register and field classes through ``_attach_poll_methods``).
   * - Snapshots
     - ``soc.snapshot()``, ``snap.diff(other)``, ``snap.to_json()``,
       ``soc.restore(snap, dry_run=)``
     - implemented
     - ``soc.snapshot()`` / ``soc.restore(snap, dry_run=)`` plus ``Snapshot.diff`` / ``.to_json`` ship via ``runtime/snapshot.py`` (bound onto each SoC by ``attach_snapshot``).
   * - NumPy interop
     - ``np.asarray(mem)``, ``ArrayView.read() -> ndarray``,
       ``snap.to_dataframe()``
     - implemented
     - ``np.asarray(mem)`` and ``ArrayView.read() -> ndarray`` ship via ``runtime/mem_view.py`` and ``runtime/arrays.py``; ``snap.to_dataframe()`` ships via ``runtime/snapshot.py``. Phase 5 of the Tier 3 plan (issue #138) routes array entries through the dataframe with synthesized ``"soc.lut[i]"`` paths per entry.
   * - Observation hooks
     - ``soc.observers.add_read(...)``, ``soc.observers.add_write(...)``,
       ``with soc.observe() as obs:``
     - implemented
     - ``soc.observers.add_read`` / ``.add_write`` and ``with soc.observe() as obs:`` ship via ``runtime/observers.py`` (``ObserverChain`` wraps the master and emits :class:`Event` records with ``where=`` filtering).
   * - Master backends
     - ``MockMaster``, ``OpenOCDMaster``, ``SSHMaster``, ``SimMaster``,
       ``ReplayMaster``, ``RecordingMaster``, ``CallbackMaster``
     - implemented
     - All seven backends ship: ``MockMaster`` / ``CallbackMaster`` / the native ``MmapMaster`` (``templates/descriptors/base_classes.hpp.jinja``, ``templates/bindings_main.cpp.jinja``), ``RecordingMaster`` / ``ReplayMaster`` (``masters/recording_replay.py`` with a configurable ``FlushPolicy``), the behavioral ``SimMaster`` (``masters/sim.py``), plus ``OpenOCDMaster`` (``masters/openocd.py``) and ``SSHMaster`` (``masters/ssh.py``).
   * - Per-region routing
     - ``soc.attach_master(master, where="peripherals.*")``
     - implemented
     - ``soc.attach_master(master, where=…)`` ships via ``runtime/routing.py`` (``Router`` + ``attach_router`` post-create hook accepts glob strings, ``(start, end)`` ranges, and callable predicates).
   * - Reified transactions
     - ``Read``, ``Write``, ``Burst``; ``master.execute(txns)``;
       ``with soc.batch() as b:``
     - implemented
     - ``Read`` / ``Write`` / ``Burst`` dataclasses, ``master.execute(txns)`` and ``with soc.batch() as b:`` ship via ``runtime/transactions.py``.
   * - Barriers / fences
     - ``soc.barrier()``, ``soc.barrier(scope="all")``, ``soc.global_barrier()``,
       ``soc.set_barrier_policy("auto" | "none" | "strict" | "auto-global")``
     - implemented
     - ``BarrierPolicy`` (``barrier(scope="self"|"all")``, ``global_barrier``, ``set_barrier_policy``) ships via ``runtime/bus_policies.py`` (installed onto every master by ``install`` / the master-extension registry).
   * - Caching policy
     - ``reg.cache_for(seconds)``, ``reg.invalidate_cache()``, ``soc.cached(window=)``
     - implemented
     - ``reg.cache_for(seconds)`` / ``reg.invalidate_cache()`` / ``with soc.cached(window=…):`` ship via ``runtime/caching.py`` (TTL slot table; volatile / side-effecting registers are refused upfront).
   * - Retry / recovery
     - ``master.set_retry_policy(retries=, backoff=, on=, on_giveup=)``;
       ``BusError(addr, op, master, retries, underlying)``
     - implemented
     - ``RetryPolicy.set_retry_policy(...)`` ships via ``runtime/bus_policies.py`` and raises :class:`BusError` (``runtime/errors.py``) with addr/op/master/retries/underlying once retries are exhausted.
   * - Tracing & replay
     - ``with soc.trace() as t:``, ``t.save("session.json")``, ``ReplayMaster.from_file``
     - implemented
     - ``soc.trace()`` ships via ``runtime/trace.py`` (wraps the active master in a ``RecordingMaster``); ``RecordingMaster`` / ``ReplayMaster.from_file`` ship in ``masters/recording_replay.py``.
   * - Concurrency
     - ``with soc.lock():``, ``async with soc.async_session():`` exposing
       ``aread``/``awrite``/``amodify``
     - implemented
     - ``with soc.lock():`` ships as a re-entrant per-SoC :class:`threading.RLock` via ``runtime/locking.py`` (attached by ``attach_lock``); ``async with soc.async_session() as s:`` (``aread`` / ``awrite`` / ``amodify`` / ``aiowait``) ships via ``runtime/async_session.py``.
   * - Hot reload
     - ``--watch``, ``soc.reload()``; opt-in with loud warning
     - implemented
     - ``soc.reload()`` ships via ``runtime/hot_reload.py`` (generation counter, stale-handle guard, post-create ``attach_reload`` hook); the ``--watch`` CLI subcommand ships via ``cli/watch.py``.
   * - Generated stubs
     - exhaustive ``.pyi`` with ``Register[FieldDict]``, ``Unpack[TypedDict]``,
       ``Annotated[int, Range(...)]``, ``Literal['rclr']``
     - partial
     - ``Annotated[int, Range(0, max)]`` per-field write overloads and ``Literal[...]`` overloads ship in ``templates/stubs.pyi.jinja``; still missing per §17: ``Unpack[TypedDict]`` on ``write_fields(**fields)`` and tuple-index overloads on array ``__getitem__`` with per-axis ``Annotated[int, Range(...)]`` (deferred at the T3 P5 commit).
   * - Schema export
     - ``schema.json`` reflective tree alongside the generated module
     - implemented
     - ``schema.json`` is emitted alongside the generated module by ``exporter_plugins/feature_detection.py`` and consumed by ``runtime/schema.py``.

Errors
------

.. list-table::
   :header-rows: 1
   :widths: 25 35 15 25

   * - Situation
     - Exception
     - Status
     - Notes
   * - Write to read-only / read from write-only
     - ``AccessError``
     - implemented
     - :class:`AccessError` ships via ``runtime/errors.py`` and is raised by the field/register gates in ``runtime/_default_shims.py``.
   * - Out-of-range field value
     - ``ValueError`` with bit width and valid range
     - implemented
     - ``runtime/values.py`` (``_coerce_field_value``) raises :class:`ValueError` with the field name, width, and ``[0, max]`` range on every ``modify`` / ``write_fields`` / ``replace``.
   * - Unknown field name in ``modify``
     - ``AttributeError`` with did-you-mean suggestion
     - implemented
     - Both the ``modify`` and ``write_fields`` shims in ``runtime/_default_shims.py`` raise :class:`AttributeError` with a ``did_you_mean`` suggestion drawn from the field spec.
   * - Side-effecting read inside ``no_side_effects()``
     - ``SideEffectError``
     - implemented
     - :class:`SideEffectError` ships via ``runtime/errors.py`` and is raised by the destructive-read guard in ``runtime/side_effects.py`` (``check_read_allowed`` / ``no_side_effects`` context).
   * - ``peek()`` on a master that cannot peek
     - ``NotSupportedError``
     - implemented
     - :class:`NotSupportedError` ships via ``runtime/errors.py`` and is raised by ``peek`` in ``runtime/side_effects.py`` when the active master lacks a peek seam.
   * - Bus error
     - ``BusError(addr, op, master, retries, underlying)``
     - implemented
     - :class:`BusError(address, op, master, retries, underlying)` ships via ``runtime/errors.py`` and is raised by :class:`RetryPolicy` in ``runtime/bus_policies.py`` once retries are exhausted.
   * - Address routing miss
     - ``RoutingError(addr, "no master attached for ...")``
     - implemented
     - :class:`RoutingError(address, message)` ships via ``runtime/errors.py`` (re-exported from ``runtime/routing.py``) and is raised when no rule matches.
