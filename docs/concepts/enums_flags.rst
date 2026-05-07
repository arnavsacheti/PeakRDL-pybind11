Enums & Flags
=============

PeakRDL-pybind11 turns SystemRDL ``encode`` enums and 1-bit field clusters into
real Python types so that reads and writes feel idiomatic and stay
self-documenting. Two surfaces cover almost every case:

* **Typed enums** for fields with ``encode = MyEnum``.
* **Flag-register conveniences** for registers whose fields are all single-bit,
  including a generated ``IntFlag`` for bit-mask composition.

Enums via ``encode = MyEnum``
-----------------------------

The exporter emits a real :class:`enum.IntEnum` per RDL enum, namespaced under
the owning module. Reads decode automatically; writes accept the enum, an
``int``, or the member name (handy at the REPL).

.. code-block:: python

   from MySoC.uart import BaudRate, Parity

   soc.uart.control.baudrate.read()                    # → BaudRate.BAUD_19200
   soc.uart.control.baudrate.write(BaudRate.BAUD_115200)
   soc.uart.control.baudrate.write(2)
   soc.uart.control.baudrate.write("BAUD_115200")
   soc.uart.control.baudrate.choices                   # → BaudRate (the type)

Out-of-range values raise on write, with the exception listing the valid
options.

Per-bit flags
-------------

Single-bit fields read as ``bool``-compatible values, and accept ``0``/``1`` (or
``True``/``False``) on write:

.. code-block:: python

   soc.system.periph_clk_en1.uart0_clk_en.write(1)
   soc.system.periph_clk_en1.uart0_clk_en.read()    # True

This is the right tool when you want to flip exactly one bit and don't care
about the rest of the register.

Whole-register set / clear / toggle
-----------------------------------

When you want to manipulate several flag bits at once, every flag-style
register exposes a small set of name-based helpers:

.. code-block:: python

   soc.system.periph_clk_en1.set("uart0_clk_en", "spi0_clk_en", "i2c0_clk_en")
   soc.system.periph_clk_en1.clear("uart0_clk_en")
   soc.system.periph_clk_en1.toggle("uart0_clk_en")
   soc.system.periph_clk_en1.bits()                  # set of names with bit=1
   soc.system.periph_clk_en1.modify(uart0_clk_en=1, spi0_clk_en=1)   # canonical

``modify(**fields)`` is the canonical form: it expresses intent declaratively
and round-trips cleanly through the read-modify-write path.

IntFlag auto-generation
-----------------------

When a register carries a ``*_FLAGS`` UDP, *or* every field in the register is
1-bit, the exporter additionally emits an :class:`enum.IntFlag` named after the
register (e.g. ``PeriphClkEn1Flags``). Use it whenever bit-masks beat
keyword-by-keyword spelling:

.. code-block:: python

   from MySoC.system import PeriphClkEn1Flags

   mask = PeriphClkEn1Flags.UART0_CLK_EN | PeriphClkEn1Flags.SPI0_CLK_EN
   soc.system.periph_clk_en1.write(mask)
   soc.system.periph_clk_en1.read() & PeriphClkEn1Flags.UART0_CLK_EN

The ``IntFlag`` composes with ``|``, ``&``, and ``~`` like the standard library
type, and writes accept either the flag value or a plain ``int``.

See also
--------

* :doc:`/values_and_io`
