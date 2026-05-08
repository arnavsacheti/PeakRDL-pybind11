SystemRDL Feature Matrix
========================

This page surveys SystemRDL features against the **planned** Python API described
in ``docs/IDEAL_API_SKETCH.md``. The sketch is the source of truth: it is
aspirational, and code is catching up to it. Status values mean:

- ``implemented`` — shipped today and behaves as the sketch describes.
- ``partial`` — exists but does not yet match the sketch's surface or semantics.
- ``planned`` — defined by the sketch; the exporter does not yet emit it.

Most rows are ``planned``. Rows are organized by SystemRDL category and reference
the conceptual docs (in ``docs/concepts/``) where applicable.

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
     - partial
     - Hierarchy works; ``.info`` namespace and uniform metadata are planned. See :doc:`concepts/hierarchy`.
   * - ``regfile``
     - ``RegFile`` node; ``soc.dma.channel`` (when arrayed)
     - partial
     - Grouping works; array indexing semantics per :doc:`concepts/arrays` are planned.
   * - ``reg``
     - ``Reg`` node; ``reg.read()``, ``reg.write(value)``, ``reg.modify(**fields)``, ``reg.poke(value)``
     - partial
     - Primitive ops exist; typed ``RegisterValue`` returns and the
       ``modify`` semantics from §3 of the sketch are planned.
   * - ``field``
     - ``Field`` node; ``field.read()``, ``field.write(v)``, ``field.bits[i]``
     - partial
     - Field read/write exists; bit-slice access (``field.bits[5]``) and
       typed ``FieldValue`` returns are planned.
   * - ``mem``
     - ``Mem`` node; ``mem[i]``, ``mem[i:j]``, ``MemView``, ``.copy()``, ``.read()``,
       ``read_into``, buffer-protocol/``np.asarray``
     - partial
     - List-like memory exists; the NumPy-aware ``MemView`` surface is planned.
       See :doc:`concepts/memory`.
   * - ``signal``
     - ``Signal`` node, metadata only via ``.info``
     - planned
     - Signals appear in the tree as metadata-only nodes per §2 of the sketch.
   * - ``constraint``
     - none
     - planned
     - Out of scope for the runtime API; ignored during export.

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
     - partial
     - Read and RMW work; ``AccessMode`` enum on ``.info`` is planned.
   * - ``sw = r`` (read-only)
     - ``field.read()``; ``field.write(v)`` raises ``AccessError``
     - planned
     - Access-mode enforcement and the ``AccessError`` exception per §19 are planned.
   * - ``sw = w`` (write-only)
     - ``field.write(v)``; ``field.read()`` raises ``AccessError``
     - planned
     - Same as above.
   * - ``sw = na`` (no software access)
     - hidden from autocomplete, raises on access
     - planned
     - Access-mode enforcement is planned.

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
     - partial
     - Flags exist on field objects; uniform ``.info`` namespace is planned.

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
     - planned
     - Reset values are parsed but not yet written back. See §12.3 of the sketch.

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
     - planned
     - Today only the ``is_enum`` UDP triggers ``IntEnum`` generation. Native ``encode``
       support is planned per §8.1.

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
     - ``soc.lut.entry[i]``, ``soc.lut.entry[:]``, ``ArrayView``,
       ``.read() -> ndarray[uint32]``
     - planned
     - See :doc:`concepts/arrays`. Today array instantiation is not handled.
   * - Regfile array (``regfile rf[N]``)
     - ``soc.dma.channel[3]``, iteration, slice
     - planned
     - Indexing semantics per §7.1 of the sketch.
   * - Multi-dim array (``reg r[A][B]``)
     - ``soc.regblock.my_reg[2, 5]``, ``.shape``
     - planned
     - Tuple indexing per §7.2.
   * - Field array (``mode[16]``)
     - ``FieldArray`` with slice semantics
     - planned
     - Uncommon but supported per §7.4.
   * - Address stride
     - resolved via ``info.address`` per element
     - planned
     - The exporter will respect RDL ``addressing`` so each element has a unique
       address.

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
     - planned
     - See :doc:`concepts/aliases`. Today aliases are not modeled.

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
     - planned
     - The marquee feature. See :doc:`concepts/interrupts`.
   * - Per-source ops
     - ``soc.uart.interrupts.tx_done.is_pending()``, ``.enable()``, ``.disable()``,
       ``.clear()``, ``.acknowledge()``, ``.fire()``, ``.wait(timeout=)``,
       ``.aiowait(...)``, ``.poll(...)``
     - planned
     - Per §9.1.
   * - Group ops
     - ``soc.uart.interrupts.pending()``, ``.enabled()``, ``.clear_all()``,
       ``.disable_all()``, ``.snapshot()``
     - planned
     - Per §9.2.
   * - Top-level tree
     - ``soc.interrupts.tree()``, ``soc.interrupts.pending()``,
       ``soc.interrupts.wait_any(timeout=)``
     - planned
     - Per §9.3.
   * - Detection / overrides
     - ``--interrupt-pattern`` flag; ``InterruptGroup.manual(state=, enable=, test=)``
     - planned
     - Per §9.4.
   * - ``enable`` / ``mask`` / ``haltmask`` / ``haltenable``
     - mapped onto ``InterruptGroup`` controls
     - planned
     - Modifier properties feed the group surface.

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
     - planned
     - Signals expose location, name, desc and any UDPs; no read/write.
   * - Component port declarations
     - none
     - planned
     - Out of scope for the register-access surface.

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
     - planned
     - See :doc:`concepts/specialized` and §12.1 of the sketch.
   * - ``incrthreshold`` / ``incrsaturate``
     - ``c.threshold()``, ``c.is_saturated()``
     - planned
     - Driven by RDL counter properties.

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
     - planned
     - See :doc:`concepts/specialized`. Per §11/§12.2 of the sketch.

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
     - planned
     - HW writes only update if currently zero. Surface is metadata-only per §11.1.

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
     - planned
     - Marks the field as volatile so caching is refused per §13.4.

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
     - planned
     - See :doc:`concepts/side_effects`. Per §11 of the sketch.
   * - ``onread = rset``
     - ``info.on_read = ReadEffect.RSET``
     - planned
     - Symmetric to ``rclr``.
   * - ``onread = ruser``
     - ``info.on_read = ReadEffect.RUSER``
     - planned
     - User-defined read effect, surfaced via ``.info``.

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
     - planned
     - See :doc:`concepts/side_effects`.
   * - ``onwrite = woset`` (W1S)
     - ``info.on_write = WriteEffect.W1S``; ``field.set()`` writes 1
     - planned
     - Symmetric to ``woclr``.
   * - ``onwrite = wzc`` (W0C)
     - ``info.on_write = WriteEffect.WZC``; ``field.clear()`` writes 0
     - planned
     - Inverted W1C.
   * - ``onwrite = wzs`` (W0S)
     - ``info.on_write = WriteEffect.WZS``; ``field.set()`` writes 0
     - planned
     - Inverted W1S.
   * - ``onwrite = wclr``
     - ``info.on_write = WriteEffect.WCLR``; ``field.clear()`` writes anything
     - planned
     - Any-write-clears.
   * - ``onwrite = wset``
     - ``info.on_write = WriteEffect.WSET``; ``field.set()`` writes anything
     - planned
     - Any-write-sets.
   * - ``onwrite = wuser``
     - ``info.on_write = WriteEffect.WUSER``
     - planned
     - User-defined write effect, surfaced via ``.info``.

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
     - planned
     - Per §11.1; metadata only on the field.

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
     - planned
     - Indicates who wins on collision. Per §4.2.

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
     - planned
     - Surface as access-mode metadata.
   * - ``swacc``
     - ``info.swacc``
     - planned
     - Notification metadata.
   * - ``swmod``
     - ``info.swmod``
     - planned
     - Notification metadata.

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
     - planned
     - See :doc:`concepts/specialized`. Per §12.4 of the sketch.

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
     - planned
     - Per §22 decision 5: endianness/access-width are hidden, but bursts and
       traces preserve them.
   * - ``regwidth``
     - ``info.regwidth``
     - partial
     - Available today; uniform ``.info`` namespace is planned.
   * - ``accesswidth``
     - hidden by default; available via ``.info.accesswidth``
     - planned
     - Per §22 decision 5.

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
     - partial
     - Sizing works; uniform attribute name (``depth``) is planned.
   * - ``memwidth``
     - ``mem.word_width``
     - partial
     - Width works; uniform attribute name is planned.
   * - ``mem`` slicing / NumPy
     - ``mem[i:j]`` returns ``MemView``; ``np.asarray(mem)``;
       ``mem.read_into(buf)``; ``mem.window(...)``
     - planned
     - See :doc:`concepts/memory`. Per §6.
   * - ``mem`` ``sw`` / ``hw`` access
     - access-mode enforcement via ``AccessError``
     - planned
     - Same model as register access modes.

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
     - planned
     - See §18 of the sketch.

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
     - planned
     - Per §3.2 and §22 decision 1.
   * - Format helpers
     - ``v.hex()``, ``v.hex(group=4)``, ``v.bin()``, ``v.table()``
     - planned
     - Per §3.2.
   * - Multi-field RMW
     - ``reg.modify(**fields)``; ``with reg as r: r.enable = 1``
     - partial
     - Modify and context manager exist; sketch-faithful semantics planned.
   * - Skip-readback writes
     - ``reg.poke(value)``
     - planned
     - One-bus-write escape hatch.
   * - Bit-slice access
     - ``field.bits[5]``, ``field.bits[0:8]``
     - planned
     - Per §3.2.
   * - Discovery
     - ``soc.find(addr)``, ``soc.find_by_name(...)``, ``soc.walk(kind=Reg)``,
       path-string indexing
     - planned
     - See :doc:`concepts/hierarchy`.
   * - Metadata namespace
     - ``.info.{address, path, fields, tags, is_volatile, is_interrupt_source,
       precedence, paritycheck, on_read, on_write, alias_kind, reset}``
     - planned
     - Per §4.2.
   * - Rich repr
     - ``__repr__``, ``__str__``, ``_repr_pretty_``, ``_repr_html_``,
       ``soc.dump()``, ``soc.tree()``, ``reg.watch(period=)``
     - planned
     - Per §5.
   * - Wait / poll
     - ``field.wait_for(value, timeout=)``, ``reg.wait_until(predicate, timeout=)``,
       ``field.read(n=)`` (sample), ``field.histogram(n=)``
     - planned
     - Per §14.
   * - Snapshots
     - ``soc.snapshot()``, ``snap.diff(other)``, ``snap.to_json()``,
       ``soc.restore(snap, dry_run=)``
     - planned
     - Per §15.
   * - NumPy interop
     - ``np.asarray(mem)``, ``ArrayView.read() -> ndarray``,
       ``snap.to_dataframe()``
     - planned
     - NumPy is a hard dependency per §22 decision 2.
   * - Observation hooks
     - ``soc.observers.add_read(...)``, ``soc.observers.add_write(...)``,
       ``with soc.observe() as obs:``
     - planned
     - Per §16.2.
   * - Master backends
     - ``MockMaster``, ``OpenOCDMaster``, ``SSHMaster``, ``SimMaster``,
       ``ReplayMaster``, ``RecordingMaster``, ``CallbackMaster``
     - partial
     - Several masters exist today; ``Replay``/``Recording``/``Sim`` are planned.
   * - Per-region routing
     - ``soc.attach_master(master, where="peripherals.*")``
     - planned
     - Per §13.1.
   * - Reified transactions
     - ``Read``, ``Write``, ``Burst``; ``master.execute(txns)``;
       ``with soc.batch() as b:``
     - planned
     - Per §13.2.
   * - Barriers / fences
     - ``soc.barrier()``, ``soc.barrier(scope="all")``, ``soc.global_barrier()``,
       ``soc.set_barrier_policy("auto" | "none" | "strict" | "auto-global")``
     - planned
     - Per §13.3 and §22 decision 7.
   * - Caching policy
     - ``reg.cache_for(seconds)``, ``reg.invalidate_cache()``, ``soc.cached(window=)``
     - planned
     - Per §13.4. Refused on volatile or side-effecting reads.
   * - Retry / recovery
     - ``master.set_retry_policy(retries=, backoff=, on=, on_giveup=)``;
       ``BusError(addr, op, master, retries, underlying)``
     - planned
     - Per §13.5.
   * - Tracing & replay
     - ``with soc.trace() as t:``, ``t.save("session.json")``, ``ReplayMaster.from_file``
     - planned
     - Per §13.6.
   * - Concurrency
     - ``with soc.lock():``, ``async with soc.async_session():`` exposing
       ``aread``/``awrite``/``amodify``
     - planned
     - Per §13.8 and §22 decision 3.
   * - Hot reload
     - ``--watch``, ``soc.reload()``; opt-in with loud warning
     - planned
     - Per §21 and §22 decision 6.
   * - Generated stubs
     - exhaustive ``.pyi`` with ``Register[FieldDict]``, ``Unpack[TypedDict]``,
       ``Annotated[int, Range(...)]``, ``Literal['rclr']``
     - partial
     - Stub generation exists; exhaustive typing per §17 is planned.
   * - Schema export
     - ``schema.json`` reflective tree alongside the generated module
     - planned
     - Drives docs and web tools per §20.

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
     - planned
     - Per §19. Carries path and absolute address.
   * - Out-of-range field value
     - ``ValueError`` with bit width and valid range
     - planned
     - Per §19.
   * - Unknown field name in ``modify``
     - ``AttributeError`` with did-you-mean suggestion
     - planned
     - Per §19.
   * - Side-effecting read inside ``no_side_effects()``
     - ``SideEffectError``
     - planned
     - Per §11 and §19.
   * - ``peek()`` on a master that cannot peek
     - ``NotSupportedError``
     - planned
     - Per §19.
   * - Bus error
     - ``BusError(addr, op, master, retries, underlying)``
     - planned
     - Per §19.
   * - Address routing miss
     - ``RoutingError(addr, "no master attached for ...")``
     - planned
     - Per §19.
