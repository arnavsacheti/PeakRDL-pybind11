# Ideal Python API Sketch — PeakRDL-pybind11

> Status: **design sketch**, not a contract. Written to think clearly about what Python ↔ hardware interaction *should* feel like for users of generated register bindings, ignoring what is currently shipped.

---

## 0. Who is this for?

Five user roles drive the design. Anything that helps a Python-fluent person who is *not* a hardware engineer stay productive on hardware is a feature.

| Role          | What they do most                                              | What hurts most today                          |
|---------------|----------------------------------------------------------------|------------------------------------------------|
| Bring-up eng. | Poke registers from REPL, verify hardware came up              | "Does this register need RMW or write?"        |
| Test author   | Write directed/random tests, wait for events                   | Polling boilerplate, magic constants           |
| FW dev        | Mirror the C driver's behavior in Python for co-simulation     | Mismatched semantics with `mmio_*` helpers     |
| Lab automator | Long-running scripts in Jupyter, log + replay sessions         | No transcript, no diffing, weak notebook UX    |
| Debugger      | Attached over JTAG/SWD, want a state dump and snapshot         | Each tool has its own register browser         |

**One-line goal:** *make `dir(soc)` and a docstring enough to drive the chip from the REPL or a notebook.*

---

## 1. Design principles

1. **Hierarchy mirrors RDL.** No flat namespaces, no string paths required for the common case. `soc.uart[0].control.enable` is the canonical form.
2. **Side effects are loud, not silent.** A read that clears, a write that pulses — the API must surface these in `repr`, in autocomplete, and in errors.
3. **One transaction per primitive op.** `reg.write(v)` is exactly one bus write. `reg.read()` is exactly one bus read. RMW only happens where the *abstraction makes it unavoidable* — namely `field.write(v)` and `reg.modify(**fields)` — and those names are chosen so that RMW is the obvious reading.
4. **Values are typed.** A field with `encode = BaudRate` reads back as `BaudRate.BAUD_115200`, not `2`. A 1-bit field reads back as a `bool`-compatible int. Bulk reads return `numpy` arrays.
5. **The bus is replaceable, observable, and explicit.** Same code runs against mock, JTAG, SSH, simulator, recorded trace. Transactions are reified objects. Barriers, retries, and caching policy are first-class.
6. **`help()`, `repr()`, `dir()`, and `_repr_html_()` answer real questions.** A user landing in a REPL or a notebook should be able to navigate the chip without a datasheet open.
7. **Stubs are exhaustive.** Generated `.pyi` declares enum types, field literals, named-arg overloads — IDE completion is the spec.
8. **Performance is a first-class feature.** A million-register snapshot, a 16k-entry memory dump, a tight poll loop — all should be O(1) Python overhead per word.

---

## 2. Mental model

A generated module is a **typed tree of nodes** that mirrors the RDL hierarchy. Every node knows:

- Its **path** and **absolute address**.
- Its **kind**: `AddrMap`, `RegFile`, `Reg`, `Field`, `Mem`, `InterruptGroup`, `Alias`, `Signal`.
- Its **metadata**: name, description, RDL properties, source location.
- Its **bus binding**: which master serves which address ranges.

Nodes are *descriptors*, not values. They produce values by reading. Values produced by reads are typed wrappers (`RegisterValue`, `FieldValue`) that behave like ints but carry decode info — print well, compare to enums, and round-trip cleanly back into writes.

```
SoC
├── AddrMap            (peripherals)
│   ├── RegFile        (uart[0..3])
│   │   ├── Reg        (control)
│   │   │   ├── Field  (enable, baudrate, parity)
│   │   │   └── ...
│   │   ├── Reg        (status)            ← side-effect: rclr
│   │   └── InterruptGroup    (auto-detected from intr_state/enable/test trio)
│   └── Mem            (sram)              ← buffer-protocol, ndarray, slice
└── Master             (the bus)
```

---

## 3. Core read/write surface

### 3.1 The four primitive ops

```python
reg.read()              # → RegisterValue   (1 bus read)
reg.write(value)        # raw write         (1 bus write, no read)
reg.modify(**fields)    # RMW              (1 read + 1 write)
reg.poke(value)         # explicit "I know what I'm doing", same as .write()
```

`field.read()` reads the parent register and slices. `field.write(v)` is shorthand for `reg.modify(field=v)` — a single-field RMW. The name `write` on a field is intentional: a field write *cannot* be a single bus write on a multi-field register, so the operation is named to read clearly without lying about the cost (one read, one write).

### 3.2 Returns are typed values, not bare ints

```python
v = soc.uart.control.read()
print(v)
# UartControl(0x00000022)
#   enable[0]    = 1
#   baudrate[3:1]= BaudRate.BAUD_19200  (1)
#   parity[5:4]  = Parity.NONE          (0)

v == 0x22                     # True, RegisterValue is int-compatible
v.enable                      # 1 (also v["enable"])
v.baudrate                    # <BaudRate.BAUD_19200: 1>
v.replace(enable=0)           # → new RegisterValue with field swapped
soc.uart.control.write(v)     # round-trips
```

Format helpers, because users always want them:

```python
v.hex()                       # "0x00000022"
v.hex(group=4)                # "0x0000_0022"
v.bin()                       # "0b00000000_00000000_00000000_00100010"
v.bin(group=8, fields=True)   # annotates groups with field boundaries
print(reg, fmt="bin")          # alt-format on the live read
soc.uart.control.read().table()   # ASCII table of fields, ready for logs
```

`RegisterValue` and `FieldValue` are **immutable and hashable** — safe as dict keys for snapshots, coverage maps, and golden-state checks. We pay one allocation per read for that guarantee; the alternative (mutable shared state pretending to be a value) leaks bugs. Both, plus `Snapshot`, are picklable and JSON-serializable for distributed test harnesses and CI artefacts.

Mutation goes through `.replace(**fields)` (returns a new value) and never through assignment.

Field-level reads:

```python
soc.uart.control.baudrate.read()   # → BaudRate.BAUD_19200
soc.uart.intr.tx_done.read()       # → bool (1-bit field)

# Individual bit access within a multi-bit field — common when a field is
# really N independent flags packed together (e.g. 16-bit `direction`).
soc.gpio.direction.bits[5].read()       # bool
soc.gpio.direction.bits[5].write(1)     # RMW that touches one bit only
soc.gpio.direction.bits[0:8].read()     # ndarray[bool], length 8
soc.gpio.direction.bits[:].write(0xFF00)  # bitmask to bool array
```

### 3.3 Multi-field atomic update

```python
# Single RMW. kwargs map to fields by name, type-checked against pyi.
soc.uart.control.modify(enable=1, baudrate=BaudRate.BAUD_115200, parity=Parity.NONE)

# Compose from a value (no read needed on host)
soc.uart.control.write(UartControl.build(enable=1, baudrate=2))   # all unset → reset value

# Context manager: stage many writes, flush on exit (existing pattern, kept).
with soc.uart.control as r:
    r.enable     = 1
    r.baudrate   = BaudRate.BAUD_115200
    if r.parity.read() == Parity.NONE: r.parity = Parity.EVEN
# 1 read + 1 write hits the bus on exit.
```

`r.enable = 1` is sugar for `r.enable.stage(1)` inside the context. Outside a context, the same syntax raises — no accidental "I thought that wrote to hardware" moments.

### 3.4 Attribute writes outside contexts

To kill the most common footgun (`reg.enable = 1` while expecting a bus transaction), bare attribute assignment on a register **outside a context manager** raises with a hint:

```python
soc.uart.control.enable = 1
# AttributeError: assigning to a field outside a context. Use:
#   soc.uart.control.enable.write(1)         # RMW
#   soc.uart.control.modify(enable=1)        # RMW
#   with soc.uart.control as r: r.enable = 1 # batched
```

A `--strict-fields=false` build-time opt is provided for teams porting C drivers that expect attribute-assign-as-RMW. Default is strict. The opt-out emits a `DeprecationWarning` at module import *and* at every loose assignment, because silent RMW on bare attribute-assignment is the single most common source of "I thought that wrote" test bugs. The warning is annoying on purpose. Per-instance toggling intentionally does not exist — the policy is a single bit, set at build.

---

## 4. Hierarchy & discovery

### 4.1 Navigation

```python
soc.peripherals.uart[0].control            # by index
soc.peripherals.uart["uart0"]              # by name
soc.peripherals.uart.uart0                 # if names are valid identifiers
soc["peripherals.uart[0].control"]         # path string (escape hatch)
soc.find(0x4000_1004)                      # → soc.peripherals.uart[0].status
soc.find_by_name("control", glob=True)     # → list of all matches
list(soc.walk())                           # breadth-first iterator over leaves
list(soc.walk(kind=Reg))                   # filtered
```

### 4.2 Metadata, always one attribute away

Every node exposes a uniform `.info` namespace so attribute autocompletion isn't polluted:

```python
soc.uart.control.info.name              # "Control Register"
soc.uart.control.info.desc              # "UART control and configuration"
soc.uart.control.info.address           # 0x4000_1000
soc.uart.control.info.offset            # 0x000 (within parent)
soc.uart.control.info.regwidth          # 32
soc.uart.control.info.access            # AccessMode.RW
soc.uart.control.info.reset             # 0x0
soc.uart.control.info.fields            # OrderedDict[str, FieldInfo]
soc.uart.control.info.path              # "peripherals.uart[0].control"
soc.uart.control.info.rdl_node          # underlying systemrdl AST node (None if stripped)
soc.uart.control.info.source            # ("uart.rdl", 41)
soc.uart.control.info.tags              # custom user-defined properties (UDPs)

# Field-specific
f = soc.uart.control.enable
f.info.precedence       # Precedence.SW | HW   (who wins on collision)
f.info.paritycheck      # bool — RDL parity protection
f.info.is_volatile      # True if hwclr/hwset/sticky/counter — value can change without sw
f.info.is_interrupt_source   # has the `intr` property
```

### 4.3 `repr`, `print`, `help`

```python
>>> soc.uart.control
<Reg uart[0].control @ 0x40001000  rw  reset=0x00000000>
  [0]    enable    rw  "Enable UART"
  [3:1]  baudrate  rw  encode=BaudRate  "Baudrate selection"
  [5:4]  parity    rw  encode=Parity    "Parity mode"

>>> print(soc.uart.control)
peripherals.uart[0].control = 0x00000022  @ 0x40001000
  [0]    enable    = 1                       "Enable UART"
  [3:1]  baudrate  = BaudRate.BAUD_19200 (1) "Baudrate selection"
  [5:4]  parity    = Parity.NONE         (0) "Parity mode"

>>> help(soc.uart.control.baudrate)
Field uart[0].control.baudrate, bits [3:1], rw
"Baudrate selection (0=9600, 1=19200, 2=115200)"
encode = BaudRate {BAUD_9600=0, BAUD_19200=1, BAUD_115200=2}
on_read  = none      on_write = none
```

`soc.dump()` and `soc.uart.dump()` walk and pretty-print the whole subtree, with current values if a master is attached. `soc.tree()` prints just the structure.

---

## 5. Jupyter & rich display

Lab automators and bring-up engineers spend most of their day in Jupyter. The notebook surface is a primary output, not a courtesy. Every node implements both `_repr_pretty_` (IPython terminal) and `_repr_html_` (notebook) — the difference between "thorough" and "this would actually win users" lives here.

### 5.1 Rich repr per node

```python
soc.uart.control            # → renders as an HTML table in a notebook:
```

```
┌──────────────────────────── uart[0].control @ 0x40001000  rw  reset=0x00000000 ──────────────────────────┐
│ Bits   │ Field    │ Value (decoded)              │ Access │ On-read │ On-write │ Description           │
├────────┼──────────┼──────────────────────────────┼────────┼─────────┼──────────┼───────────────────────┤
│ [0]    │ enable   │ 1                            │ rw     │ —       │ —        │ Enable UART           │
│ [3:1]  │ baudrate │ BaudRate.BAUD_19200 (1)      │ rw     │ —       │ —        │ Baudrate selection    │
│ [5:4]  │ parity   │ Parity.NONE (0)              │ rw     │ —       │ —        │ Parity mode           │
└────────┴──────────┴──────────────────────────────┴────────┴─────────┴──────────┴───────────────────────┘
```

- Access modes color-coded: `rw` blue, `ro` grey, `wo` orange, `na` red strikethrough.
- Side-effect badges (⚠ rclr, ↻ singlepulse, ✱ sticky, ⚡ volatile) inline with each field.
- Click a row → expand to show full RDL source location and any UDPs.
- `soc.uart.dump()` in a notebook renders as a nested collapsible tree.

### 5.2 Memory regions in notebooks

```python
soc.ram                     # → hex/ASCII dump table with click-to-edit cells
soc.ram[0:0x100].render()    # explicit render with byte-grouping options
```

The default mem widget shows 16-byte rows, hex bytes left, ASCII right, addresses on the side, and supports range-select for copy.

### 5.3 Diff & snapshot rendering

```python
snap2.diff(snap1)           # → side-by-side table, changed cells highlighted, added/removed rows shown
                             #    sorted by path, filterable by access mode or by node kind
```

### 5.4 Interrupt group widget

```python
soc.uart.interrupts         # → matrix view: rows = sources, columns = State / Enable / Test / Pending(=State&Enable)
                             #    pending sources highlighted; click to clear/enable/fire
```

### 5.5 Live monitors (`watch()`)

The most-asked-for pattern in tester forums: a refreshing widget for a register or set of registers, powered by `ipywidgets`.

```python
w = soc.uart.control.watch(period=0.1)         # polls every 100 ms, updates HTML in place
w = soc.snapshot(["uart.*", "gpio.*"]).watch()  # multi-register live dashboard
w.stop()                                         # explicit teardown
```

`watch()` respects the side-effect rules from §11 — you can't `watch()` an `rclr` register without `allow_destructive=True`.

### 5.6 Plain-terminal IPython

`_repr_pretty_` produces the same content as §4.3 but with terminal color, aligned columns, and side-effect markers in a way that survives copy-paste into a bug report.

---

## 6. Memory regions

SystemRDL `mem` is a region with no fields — just words. The Python view models it as a sliceable, NumPy-aware buffer.

```python
mem.size_bytes        # 0x4000
mem.depth             # 0x1000 entries
mem.word_width        # 32 bits
mem.base_address      # 0x40000

# Element access (word index, not byte address — matches RDL semantics)
mem[10]                       # one read
mem[10] = 0xDEADBEEF           # one write

# Slicing returns a LIVE view (numpy-idiomatic). Each element access hits the bus.
mem[10:20]                     # → MemView, ndarray-like, live
mem[10:20].copy()              # one-burst snapshot → ndarray[uint32]  (use this for tight loops)
mem[10:20].read()              # alias for .copy(), symmetric with reg.read()
mem[10:20] = [1,2,3,...]       # one-burst bulk write (writes are always coalesced)
mem[:] = 0                     # zero-fill
del mem[100:200]               # NotImplementedError (no concept of deletion)

# Byte-level escape hatch
mem.read_bytes(offset=0, n=64)
mem.write_bytes(offset=0, data=b"\xde\xad\xbe\xef")

# NumPy interop, full memoryview/buffer protocol
import numpy as np
arr = np.asarray(mem)              # ndarray view; reads/writes go to bus on access
np.copyto(arr[0:256], pattern)     # bulk write
checksum = np.bitwise_xor.reduce(arr[0:1024])

# Bulk transfer with explicit zero-copy
buf = np.empty(1024, dtype=np.uint32)
mem.read_into(buf, offset=0)       # 1 burst, 1 fill
mem.write_from(buf, offset=0)

# Mapped context for high-frequency access
with mem.window(offset=0, length=256) as w:
    for i in range(256): w[i] = i  # buffered, flushed on exit

# Streaming for huge memories
for chunk in mem.iter_chunks(size=4096):
    process(chunk)
```

Masters declare a "burst" capability; the API uses bursts when available and falls back to per-word loops with a tqdm-friendly progress hook. `mem.read(...).meta.transactions` reports actual bus traffic, so users can verify they got a burst.

---

## 7. Arrays

SystemRDL arrays apply to regs, regfiles, fields, addrmaps. The Python model treats every array as a *typed* Sequence, with multi-dim support via tuple indexing.

### 7.1 Single dimension

```python
soc.dma.channel               # ChannelArray, len=8
soc.dma.channel[3]            # one channel
soc.dma.channel[-1]           # last
soc.dma.channel[2:6]          # ChannelSlice (still bus-bound)
list(soc.dma.channel)         # iterate
3 in soc.dma.channel          # by index
```

### 7.2 Multi-dimension

```python
# rdl: reg my_reg[4][16];
soc.regblock.my_reg[2, 5]              # tuple index — natural for users
soc.regblock.my_reg[2][5]              # also supported
soc.regblock.my_reg.shape              # (4, 16)
```

### 7.3 Bulk reads / writes

Indexing a *register array* with a slice returns an `ArrayView` that:

- Reads are coalesced into bursts when the master supports it.
- Returns `ndarray[uint{regwidth}]` for a single field/reg array.
- Returns a structured ndarray when multiple fields are projected.

```python
# Read all 64 entries of a lookup table
vals = soc.lut.entry[:].read()                # ndarray[uint32], shape (64,)

# Read one field across the array
enables = soc.dma.channel[:].config.enable.read()   # ndarray[bool], shape (8,)

# Write same value to all
soc.lut.entry[:] = 0

# Write per-element
soc.lut.entry[:] = np.arange(64)

# Modify one field across the array
soc.dma.channel[:].config.modify(enable=0)    # 8 RMWs (or 1 burst-RMW if supported)

# Filter
[c for c in soc.dma.channel if c.config.enable.read()]
```

### 7.4 Field arrays

Conceptually rare but supported (e.g. 16 mode bits as `mode[16]`). They expose as `FieldArray` with the same slice semantics.

---

## 8. Enums & flags

### 8.1 Enums (`encode = MyEnum`)

The exporter emits a real `enum.IntEnum` per RDL enum, namespaced under the owning module:

```python
from MySoC.uart import BaudRate, Parity

BaudRate.BAUD_115200            # IntEnum member, value=2
list(BaudRate)                  # iterate

soc.uart.control.baudrate.read()                    # → BaudRate.BAUD_19200
soc.uart.control.baudrate.write(BaudRate.BAUD_115200)
soc.uart.control.baudrate.write(2)                  # int still accepted
soc.uart.control.baudrate.write("BAUD_115200")      # by name (debug-friendly)
soc.uart.control.baudrate.choices                   # → BaudRate (the type)
```

Out-of-range values raise on write with a list of valid options.

### 8.2 Flags (1-bit clusters)

Two complementary surfaces:

**Per-bit (typed bool):**
```python
soc.system.periph_clk_en1.uart0_clk_en.write(1)
soc.system.periph_clk_en1.uart0_clk_en.read()    # True
```

**Whole-register set/clear (when many bits are toggled at once):**
```python
soc.system.periph_clk_en1.set("uart0_clk_en", "spi0_clk_en", "i2c0_clk_en")
soc.system.periph_clk_en1.clear("uart0_clk_en")
soc.system.periph_clk_en1.toggle("uart0_clk_en")
soc.system.periph_clk_en1.bits()                  # set of names with bit=1
soc.system.periph_clk_en1.modify(uart0_clk_en=1, spi0_clk_en=1)   # canonical
```

When a register has a `*_FLAGS` UDP or every field is 1-bit, the exporter additionally emits an `IntFlag`:

```python
from MySoC.system import PeriphClkEn1Flags
mask = PeriphClkEn1Flags.UART0_CLK_EN | PeriphClkEn1Flags.SPI0_CLK_EN
soc.system.periph_clk_en1.write(mask)
soc.system.periph_clk_en1.read() & PeriphClkEn1Flags.UART0_CLK_EN
```

---

## 9. Interrupts — the marquee feature

SystemRDL marks interrupt-bearing fields with the `intr` property. The familiar `INTR_STATE` / `INTR_ENABLE` / `INTR_TEST` trio appears in OpenTitan, ARM, RISC-V CLIC, and many vendor SoCs. The exporter detects this trio (or a configurable variation) and synthesizes an **InterruptGroup** node.

### 9.1 Per-source operations

```python
irq = soc.uart.interrupts.tx_done

irq.is_pending()        # read state bit
irq.is_enabled()         # read enable bit
irq.enable()             # set enable bit (modify, not full write)
irq.disable()
irq.clear()              # do the right thing per RDL: woclr → write 1; rclr → read; wzc → write 0
irq.acknowledge()        # alias for .clear()
irq.fire()               # write INTR_TEST bit (sw self-trigger)

irq.wait(timeout=1.0)               # blocks until pending or timeout
irq.wait_clear(timeout=1.0)         # block until *not* pending
await irq.aiowait(timeout=1.0)      # asyncio variant
irq.poll(period=0.001, timeout=1.0) # explicit period

# Subscription (master-driven if backend supports interrupts; polling otherwise)
unsubscribe = irq.on_fire(lambda: print("tx_done!"))
```

### 9.2 Group operations

```python
soc.uart.interrupts.pending()     # frozenset of IRQ objects with state==1
soc.uart.interrupts.enabled()     # frozenset
soc.uart.interrupts.clear_all()
soc.uart.interrupts.disable_all()
soc.uart.interrupts.enable(set_={"tx_done","rx_overflow"})
soc.uart.interrupts.snapshot()    # dict[name, (state, enable)]

# Iterate & ack the standard ISR pattern
for irq in soc.uart.interrupts.pending():
    handle(irq)
    irq.clear()
```

### 9.3 Top-level interrupt tree

```python
soc.interrupts                # global view across all blocks
soc.interrupts.tree()         # print tree of all IRQs and their state
soc.interrupts.pending()      # all pending across the SoC
soc.interrupts.wait_any(timeout=1.0)   # → first pending IRQ object
```

### 9.4 Detection rules (configurable)

- Default: a register named `INTR_STATE`/`intr_status`/`*_INT_STATUS` whose fields all have `intr` property triggers the trio search.
- Pair partners by suffix (`_ENABLE`, `_MASK`, `_TEST`, `_RAW`).
- Fields are matched **by name** across the trio.
- An RDL `--interrupt-pattern` flag lets users override the matcher (regex or callable).

If detection fails, the exporter still emits per-field state (`field.is_interrupt_source` is True) and the user can build their own group:

```python
my_irq = InterruptGroup.manual(
    state=soc.foo.IRQ_STAT,
    enable=soc.foo.IRQ_EN,
    test=soc.foo.IRQ_TEST,
)
```

---

## 10. Linked / aliased registers

SystemRDL `alias` lets multiple register definitions point at the same address. The API treats one as canonical and the rest as views.

```python
soc.uart.control                 # primary
soc.uart.control_alt             # alias
soc.uart.control_alt.target      # → soc.uart.control
soc.uart.control.aliases         # (soc.uart.control_alt,)
soc.uart.control_alt.is_alias    # True

# Reads/writes go to the same address, but field set may differ.
# Repr shows the alias relationship clearly.
>>> soc.uart.control_alt
<Reg uart.control_alt @ 0x40001000  alias-of=uart.control  rw>
```

Some chips use this for scrambled/secured views (read-only mirror, partial mask). The API surfaces `info.alias_kind` ∈ `{full, sw_view, hw_view, scrambled}` from RDL or UDPs.

---

## 11. Read-side and write-side effects

### 11.1 The classes

| RDL property        | Meaning                                                 | Surface                          |
|---------------------|---------------------------------------------------------|----------------------------------|
| `onread = rclr`     | Read clears the field                                   | `read()` warns or peek required  |
| `onread = rset`     | Read sets the field                                     | same                             |
| `onwrite = woclr`   | Write 1 clears (W1C)                                    | `clear()` writes 1               |
| `onwrite = woset`   | Write 1 sets (W1S)                                      | `set()` writes 1                 |
| `onwrite = wzc/wzs` | Write 0 clears/sets                                     | inverted                         |
| `onwrite = wclr`    | Any write clears                                        | `.clear()` writes anything       |
| `singlepulse`       | Field self-clears after 1 cycle                         | `pulse()`                        |
| `hwclr`/`hwset`     | Hardware can change without sw write                    | `.is_volatile`                   |
| `sticky`/`stickybit`| Hardware writes only update if currently zero           | metadata only                    |
| `paritycheck`       | Parity bit appended for RAS                             | `info.paritycheck = True`        |

### 11.2 Surface

```python
f = soc.intr_status.tx_done
f.info.on_read     # ReadEffect.NONE | RCLR | RSET | RUSER
f.info.on_write    # WriteEffect.NONE | W1C | W1S | WZC | WZS | WCLR | WSET | WUSER
f.info.is_volatile # True if hwclr/hwset/sticky/counter — value can change without sw

f.read()           # standard read; if rclr, this CLEARS the bit. Logged at INFO.
f.peek()           # read without clearing IF the master supports it; else raises
                    # (some buses literally cannot peek; we don't pretend)
f.clear()          # W1C → writes 1; rclr → does a read; wclr → writes 0; raises if no clear path
f.acknowledge()    # alias of clear() — reads better in ISR code
f.set()            # symmetric: W1S → writes 1; raises if not settable
f.pulse()          # singlepulse → writes 1, hardware clears
```

The exporter flags side-effecting reads in autocompletion and `repr`:

```
>>> soc.system.reset_status
<Reg system.reset_status @ 0x40000018  ro  reset=0x00>  ⚠ side-effecting reads (rclr)
  [0] por_flag        ro  rclr  "Power-on-reset latched flag"
  ...
```

A user trying to read a `rclr` register inside a `with soc.no_side_effects()` block raises — useful for debug dumps that must not change state.

---

## 12. Counters, single-pulse, reset, lock, external

### 12.1 Counters

```python
c = soc.peripheral.event_counter
c.value()             # current count (read)
c.reset()             # clear (per RDL — could be wclr or hwreset)
c.threshold()         # if `incrthreshold` set
c.is_saturated()      # if `incrsaturate`
c.increment(by=1)     # if sw-incr supported (writes the incrvalue field)
c.decrement(by=1)     # if `decr` supported
```

### 12.2 Singlepulse

```python
soc.dma.channel[0].control.start.pulse()    # writes 1; hardware clears
```

### 12.3 Reset semantics

```python
soc.uart.control.reset_value       # 0x0
soc.uart.control.is_at_reset()     # read & compare
soc.uart.reset_all(rw_only=True)   # write reset values to writable regs
soc.reset_all()                    # whole tree, with safety check on side-effecting fields
```

### 12.4 Lock

A field whose write is gated by a lock register:

```python
soc.gpio_a.lckr.lock(["pin0", "pin5"])   # programs LCK + sets LCKK key per RDL `lock`
soc.gpio_a.lckr.is_locked("pin0")        # True
soc.gpio_a.lckr.unlock_sequence()        # explicit, vendor-specific UDP
```

### 12.5 External regs

`external reg` declarations create the same `Reg` node; the only difference is the master may dispatch to a separate region. The user shouldn't notice.

---

## 13. The bus / master layer

### 13.1 Composable masters

```python
from peakrdl_pybind11.masters import (
    MockMaster, OpenOCDMaster, SSHMaster, SimMaster, ReplayMaster, RecordingMaster
)

soc = MySoC.create(master=OpenOCDMaster("localhost:6666"))

# Per-region routing
soc = MySoC.create()
soc.attach_master(jtag, where="peripherals.*")
soc.attach_master(mem_master, where="ram")
soc.attach_master(MockMaster(), where=lambda node: node.info.is_external)
```

### 13.2 Transactions are objects

```python
from peakrdl_pybind11 import Read, Write, Burst

txns = [
    Read(0x4000_1000),
    Write(0x4000_1004, 0x42),
    Burst(0x4000_2000, count=128, op="read"),
]
results = soc.master.execute(txns)   # exposed for users who want to script the bus

with soc.batch() as b:
    b.uart.control.write(1)
    b.uart.data.write(0x55)
# All sent at exit; if the master supports queuing, this is one command.
```

### 13.3 Barriers / fences

Many masters queue or coalesce writes; some buses post writes asynchronously. A barrier forces all in-flight writes to drain before the next read.

```python
# Default semantics: per-master. Flushing every master on every barrier is expensive
# when masters serve disjoint regions.
soc.uart.barrier()                     # barriers the master(s) serving the uart subtree
soc.master.barrier()                   # explicit single-master barrier
soc.barrier()                          # barriers the master(s) used in the current call site

# SoC-wide is opt-in, for the rare case where a read on master B depends on a write
# that went out via master A.
soc.barrier(scope="all")               # every attached master fences in turn
soc.global_barrier()                   # alias of the above; reads better in scripts

# Auto-barrier policy (per-master by default)
soc.set_barrier_policy("auto")         # barrier before any read-after-write — same master only (default)
soc.set_barrier_policy("none")         # opt out, faster but you must barrier yourself
soc.set_barrier_policy("strict")       # barrier before every read AND every write
soc.set_barrier_policy("auto-global")  # auto-barrier extends across all masters (slow, paranoid)
```

### 13.4 Read coalescing / cache policy

A `read_within` policy lets tight polling loops avoid re-issuing identical reads:

```python
soc.uart.status.cache_for(50e-3)       # within 50 ms, read returns cached value
soc.uart.status.invalidate_cache()
with soc.cached(window=10e-3): ...      # block-scoped caching
```

Cache is only legal where `info.is_volatile` is False *and* `info.on_read` is None — the exporter refuses to attach a cache to side-effecting reads, and the master ignores it for those regs.

### 13.5 Bus error recovery

```python
soc.master.set_retry_policy(
    retries=3,
    backoff=0.05,           # exponential
    on=("timeout", "nack"), # which underlying errors to retry
    on_giveup="raise",      # or "log" or "panic"
)

# Per-call override
soc.uart.control.read(retries=10)

# Global panic handler (e.g., reconnect JTAG and replay last N txns)
soc.master.on_disconnect(lambda m: m.reconnect())
```

`BusError` carries the failed transaction, the retry count, and the underlying exception — enough for a CI run to triage why it died.

### 13.6 Tracing & replay

```python
with soc.trace() as t:
    soc.uart.control.write(0x42)
    soc.uart.status.read()
print(t)
# 2 transactions, 8 bytes
#   wr  @0x40001000  0x00000042   (uart.control)
#   rd  @0x40001004  → 0x00000001 (uart.status)

t.save("session.json")
soc2 = MySoC.create(master=ReplayMaster.from_file("session.json"))   # replay/regression

# Recording wraps any master
soc.attach_master(RecordingMaster(jtag, file="run.log"))
```

### 13.7 Mock with hooks (test-driven)

```python
mock = MockMaster()
mock.on_read(soc.uart.intr_status, lambda addr: 0b101)
mock.on_write(soc.uart.data, lambda addr, val: stdout.append(val))
mock.preload(soc.ram, np.arange(1024, dtype=np.uint32))
```

Mock supports the volatile/clear semantics (RCLR, W1C) so test code can be written against the same API used in production.

### 13.8 Concurrency

- Master holds a re-entrant lock by default; multi-threaded callers don't tear up shared state.
- `with soc.lock():` for "must not be interleaved" sequences.
- `async with soc.async_session():` exposes `aread`/`awrite`/`amodify` on every node.

---

## 14. Wait / poll / predicate — the test-author's Swiss army knife

```python
# Wait until a single field equals a value
soc.uart.status.tx_ready.wait_for(True, timeout=1.0)
soc.uart.status.tx_ready.wait_for(True, timeout=1.0, period=0.001, jitter=True)

# Predicate over a register value
soc.uart.status.wait_until(lambda v: v.tx_ready and not v.error, timeout=1.0)

# Convenience for IRQs (covered in §9 too)
soc.uart.interrupts.tx_done.wait(timeout=1.0)

# Sample N reads (debouncing / glitch detection)
samples = soc.adc.sample.read(n=100)               # ndarray, length 100
hist = soc.adc.sample.histogram(n=1000)            # Counter

# Async equivalents
await soc.uart.status.tx_ready.await_for(True, timeout=1.0)
```

`wait_for` is the single most common test idiom; it deserves dedicated ergonomics, including:
- A descriptive timeout error showing the last value seen and what was expected.
- Optional capture of all sampled values for post-mortem.

---

## 15. Snapshots, diff, save/restore

```python
snap1 = soc.snapshot()                      # SocSnapshot — flat dict + structured view
do_thing()
snap2 = soc.snapshot()
print(snap2.diff(snap1))
# 3 differences
#   uart.control            0x00000000 → 0x00000022
#   uart.status.tx_ready    0          → 1
#   ram[0x40..0x60]         <changed>

soc.restore(snap1, dry_run=True)           # show what would change
soc.restore(snap1)                          # write back

# Subset
snap = soc.uart.snapshot()                  # only the uart subtree
snap.to_json("uart-state.json")
snap2 = SocSnapshot.from_json("uart-state.json")

# Pickle round-trips for distributed / multi-process tests
import pickle
data = pickle.dumps(snap)
```

Snapshots respect side effects: by default they `peek()`, and abort if any required read is destructive. `soc.snapshot(allow_destructive=True)` overrides.

---

## 16. NumPy, bulk, and observation hooks

### 16.1 NumPy interop

NumPy is a **hard runtime dependency** — bursts, bulk reads, and the buffer protocol are non-negotiable for designs of any real size, and the public surface already returns `ndarray` from arrays and memory. Pretending it's optional would just push the import to user code and force everyone to write the same shim.

Every container that holds homogeneously-typed words (memory, register array, field array) implements the buffer protocol and `__array__`:

```python
import numpy as np
arr = np.asarray(soc.ram)               # ndarray view onto live memory
arr[0:1024] = np.zeros(1024, dtype=np.uint32)
np.bitwise_xor.reduce(arr[0:64])        # whole-region checksum

# Pandas dataframe of all readable fields, one snapshot
df = soc.snapshot().to_dataframe()
df.query("on_read == 'rclr'")           # introspect side effects
```

For huge designs, `soc.iter_readable()` lazily yields every readable register without materializing the full snapshot.

### 16.2 Observation hooks (verification IP, coverage, audit)

Every read and every write passes through a hook chain. Coverage tools, audit logs, and assertion frameworks subscribe rather than wrapping the master.

```python
soc.observers.add_read(lambda evt: cov.record(evt.path, evt.value))
soc.observers.add_write(lambda evt: audit.log(evt))

# Scoped
with soc.observe() as obs:
    run_test()
print(obs.coverage_report())      # which regs/fields were read or written

# Filtered
soc.observers.add_read(my_handler, where="uart.*")
```

The hook chain is what `RecordingMaster`, `--coverage`, and the live notebook widget all use — one mechanism, three users.

---

## 17. Naming, paths, and the pyi promise

- All identifiers are RDL names, with a deterministic transform for SystemRDL keywords (`class` → `class_`).
- Generated `.pyi` stubs declare:
  - Every node as a class (no `Any` escape hatches).
  - Every enum as `enum.IntEnum`.
  - Every register as a `Register[FieldDict]` generic so IDEs surface field names on `read()`.
  - Every method that takes `**fields` declared via `Unpack[TypedDict]` — IDEs autocomplete field names with their types.
  - Side-effects annotated with `Literal['rclr']`, etc.
- Stubs are exhaustive enough that mypy/pyright can catch:
  - Unknown field name in `modify(...)`
  - Out-of-range value (via `Annotated[int, Range(0, 7)]` or `Literal`)
  - Wrong enum type
- Docstrings on every node carry the RDL `name`/`desc` so `help()` is useful in the REPL.

---

## 18. Custom user-defined properties (UDPs)

RDL UDPs become typed accessors on `info.tags`:

```python
soc.uart.control.info.tags.security_class    # "RW0"
soc.uart.control.info.tags.life_cycle         # ["dev", "prod"]
```

Common UDPs (security domain, integrity, swaccess refinements) get first-class wrappers if the exporter is given a `--udp-config` mapping. Otherwise they're transparent strings.

---

## 19. Error model

| Situation                                                | Result                                           |
|----------------------------------------------------------|--------------------------------------------------|
| Write to read-only field                                 | `AccessError("uart.status.tx_ready is sw=r")`    |
| Read from write-only field                               | `AccessError`                                    |
| Out-of-range value for field                             | `ValueError` with bit width and valid range      |
| Unknown field name in `modify(...)`                      | `AttributeError` with did-you-mean suggestion    |
| `read()` of an `rclr` field inside `no_side_effects()`   | `SideEffectError`                                |
| `peek()` of a field on a master that can't peek          | `NotSupportedError("master cannot peek rclr")`   |
| Bus error                                                | `BusError(addr, op, master, retries, underlying)`|
| Address routing miss                                     | `RoutingError(addr, "no master attached for ...")|

Every error includes the *path* and *absolute address* in the message. Stack traces are short — the API uses `__tracebackhide__`-like patterns to skip its own frames.

---

## 20. Generated-code surface (one Python module = one chip)

```
mychip/
  __init__.py         # exports `create`, `wrap_master`, `Soc`
  _native.so          # pybind11 module
  __init__.pyi        # exhaustive types
  enums.py            # all IntEnums (re-exported per peripheral)
  interrupts.py       # InterruptGroup wrappers
  signals.py          # signal definitions (mostly metadata)
  schema.json         # reflective JSON of the whole tree (drives docs, web tools)
  CMakeLists.txt
```

`schema.json` is the secret weapon: every other tool (web register browser, search, GUIs) consumes it without re-parsing RDL. Same shape as the snapshot dict.

---

## 21. CLI / REPL niceties

```
peakrdl pybind11 --explore mychip       # spawn a REPL with `soc` already created
peakrdl pybind11 --diff snapA snapB     # text/HTML diff of two snapshots
peakrdl pybind11 --replay session.json  # replay a recorded session against a target
peakrdl pybind11 --watch input.rdl      # rebuild & reload the bound module on RDL changes
```

Inside the REPL, `?soc.uart.control` (IPython) shows full metadata, and `??soc.uart.control` shows the RDL source.

**Hot reload.** `--watch` (and `soc.reload()` from inside a notebook) is opt-in. On reload, the runtime emits a warning, invalidates outstanding `RegisterValue`/`Snapshot` instances, refuses to swap if a context manager is active, and reattaches the existing master to the freshly built tree. **Hardware bus state is not affected** — only the host-side bindings get replaced. Users who want stricter behavior set `peakrdl.reload.policy = "fail"` to abort instead of warning.

---

## 22. Decisions, with rationale

The eight tradeoffs that opened in the v1 draft, all resolved.

1. **`RegisterValue` immutable & hashable.** *Decided: yes.* Cost: one allocation per read. Win: safe as dict keys for snapshots, coverage maps, and golden-state checks; and impossible to accidentally mutate a stale read. Mutation happens via `.replace(**fields)`, never by attribute.

2. **NumPy: hard dependency.** *Decided: hard.* It's already in the public surface (`ndarray` returned from arrays, memory, snapshot frames), it's ubiquitous in lab and CI environments, and a "graceful list fallback" only pushes the same import into user code. No shim, no apology.

3. **Sync first, async parallel.** *Decided: sync first.* The primary surface is synchronous; `soc.async_view()` returns the dual namespace where every node has `aread`/`awrite`/`amodify`/`aiowait`. Async is added later — designed in, not glued on.

4. **`mem` slicing returns a live view.** *Decided: view.* `mem[i:j]` is a `MemView` (numpy-idiomatic — slicing a `memmap` gives you a view, not a copy). `.copy()` and `.read()` produce a one-burst snapshot for tight loops where every Python attribute access becoming a bus transaction would be a footgun.

5. **Endianness and access-width: hidden, with bursts respecting both.** *Decided.* The user sees ints regardless of `regwidth`/`accesswidth`/`lsb0` vs `msb0`. Burst routines, mock master replays, and recording all carry the original word-width and endian metadata so traces round-trip correctly across hosts.

6. **Hot reload: opt-in, with a loud warning.** *Decided.* `--watch` and `soc.reload()` exist for the REPL/notebook workflow. On reload the runtime warns, invalidates outstanding `RegisterValue`/`Snapshot` handles, refuses if a context manager is active, and re-binds the master to the new tree. Hardware bus state is untouched. `peakrdl.reload.policy = "fail"` for users who'd rather crash than risk silently.

7. **Barriers: per-master by default, SoC-wide on demand.** *Decided.* Auto-barriers fire on the same master only — flushing every master on every read-after-write is wasteful when masters serve disjoint regions. Cross-master ordering is a real but rare requirement, declared explicitly via `soc.barrier(scope="all")`, `soc.global_barrier()`, or the `auto-global` policy for the paranoid.

8. **`--strict-fields=false` exists, with a noisy warning.** *Decided.* The escape hatch is provided for teams porting C drivers that depend on attribute-assign-as-RMW. Default is strict. Opt-out emits a `DeprecationWarning` at import and on every loose assignment — silent RMW is the leading source of "I thought that wrote" bugs, and the noise is the price of admission.

---

## 23. Still open — second-order questions surfaced by §22

These follow from the v1 decisions and want their own pass before v1 ships:

- **Should `mem.copy()` return `ndarray` or a `MemSnapshot`** that remembers its source for diffing? Likely the latter, with `.values` for the raw ndarray.
- **Async dual surface — same node objects with `a*` methods, or a parallel `AsyncSoc` tree?** Parallel tree is easier to type but doubles the binding surface.
- **Cache invalidation across `soc.reload()`** — should outstanding `RegisterValue`s raise a `StaleHandleError` on use, or compare-by-value across the new tree? Lean toward raise.
- **`--strict-fields=false` deprecation timeline.** If the warning fires forever, it's a permanent feature; if it fires for two minor versions then the opt is removed, that's a real deprecation.

---

## 24. Quick gallery — what code looks like

### Bring-up
```python
soc = MyChip.create(master=OpenOCDMaster())
print(soc.uart.control)
soc.uart.control.modify(enable=1, baudrate=BaudRate.BAUD_115200)
soc.uart.data.write(ord('A'))
soc.uart.interrupts.tx_done.wait(timeout=1.0)
soc.uart.interrupts.tx_done.clear()
```

### Test
```python
def test_uart_loopback(soc):
    with soc.uart.control as r:
        r.enable    = 1
        r.loopback  = 1
        r.baudrate  = BaudRate.BAUD_115200

    soc.uart.data.write(0xA5)
    soc.uart.status.rx_ready.wait_for(True, timeout=0.1)
    assert soc.uart.data.read() == 0xA5

    soc.uart.interrupts.clear_all()
```

### Bulk memory init
```python
soc.ram[:] = 0
soc.ram[0:64] = np.arange(64, dtype=np.uint32) * 4
checksum = np.bitwise_xor.reduce(np.asarray(soc.ram)[0:1024])
```

### Snapshot diff in CI
```python
snap_before = soc.snapshot()
run_test()
snap_after  = soc.snapshot()
delta = snap_after.diff(snap_before)
delta.assert_only_changed("uart.intr_state.*", "uart.data")
```

### Mock-driven unit test
```python
mock = MockMaster()
mock.on_read(soc.adc.sample, lambda _: random.randint(0, 4095))
soc.attach_master(mock)

readings = soc.adc.sample.read(n=1000)
assert 1500 < readings.mean() < 2500
```

### Notebook live monitor
```python
soc.uart.control.watch(period=0.1)        # widget updates in place
soc.snapshot(["uart.*"]).watch()          # multi-register dashboard
```

### Coverage in CI
```python
with soc.observe() as obs:
    run_full_test_suite()
obs.coverage_report().to_html("coverage.html")
```

---

## 25. What's intentionally *not* here

- **A whole new RTL simulator.** This is a Python-side API; the bus is pluggable.
- **Sub-cycle timing.** Bus transactions are atomic from the Python view. Cycle-accurate use cases live in cocotb/SystemVerilog.
- **DSL for sequences.** YAML/JSON test specs are out of scope; users can build them on top.
- **Auto-generated documentation site.** Punt to `peakrdl-html`; we feed it via `schema.json`.
