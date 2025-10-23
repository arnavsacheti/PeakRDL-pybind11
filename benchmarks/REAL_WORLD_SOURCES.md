# Real-World SystemRDL Test Cases

This document provides information about obtaining real-world SystemRDL files for more extensive benchmarking.

## Available Sources

### 1. OpenTitan (Recommended)

OpenTitan is an open-source silicon root of trust project with extensive, well-documented register maps.

**Source**: https://github.com/lowRISC/opentitan

**Register Definitions**:
- Location: `hw/ip/*/data/*.hjson`
- Format: Hjson (JSON for humans)
- Complexity: Varies from simple GPIO to complex cryptographic modules

**Notable IP Blocks**:
- `aes`: AES encryption engine (~50 registers)
- `gpio`: General Purpose I/O (~10 registers)
- `uart`: UART controller (~15 registers)
- `spi_device`: SPI device controller (~40 registers)
- `hmac`: HMAC engine (~30 registers)
- `otbn`: OpenTitan Big Number accelerator (~20 registers)
- `rv_timer`: RISC-V timer (~30 registers)

**Conversion to SystemRDL**:
OpenTitan uses the `reggen` tool which can export to SystemRDL format. However, direct conversion may require manual adjustments.

**Example Usage**:
```bash
# Clone OpenTitan
git clone https://github.com/lowRISC/opentitan.git

# Register maps are in:
cd opentitan/hw/ip/gpio/data
cat gpio.hjson

# Convert to SystemRDL (manual or tool-assisted)
# Then place in benchmarks/rdl_files/opentitan_gpio.rdl
```

### 2. PULP Platform

The Parallel Ultra-Low-Power (PULP) platform provides open-source RISC-V processors and SoCs.

**Source**: https://github.com/pulp-platform

**Register Definitions**:
- Various IP cores with register maps
- Format: Various (SystemVerilog, C headers, documentation)

**Notable Projects**:
- PULPissimo: Complete SoC with many peripherals
- APB peripherals: UART, GPIO, SPI, I2C, Timer

### 3. CHIPS Alliance

The Common Hardware for Interfaces, Processors and Systems (CHIPS) Alliance develops open-source hardware.

**Source**: https://github.com/chipsalliance

**Notable Projects**:
- Rocket Chip: RISC-V processor generator
- VeeR: High-performance RISC-V cores

### 4. FuseSoC Cores

FuseSoC is a package manager for reusable hardware components.

**Source**: https://github.com/fusesoc

**Core Library**: https://github.com/fusesoc/fusesoc-cores

Many cores include register definitions that can be converted to SystemRDL.

### 5. OHWR (CERN Open Hardware Repository)

CERN maintains an open hardware repository with various IP cores.

**Source**: https://ohwr.org/

**Notable Projects**:
- GN4124 PCIe bridge
- SPEC (Simple PCIe FMC Carrier)
- Various timing and control modules

**Register Definitions**: Often in Verilog/VHDL with accompanying documentation

### 6. Renode Platform Definitions

Renode includes register definitions for various platforms.

**Source**: https://github.com/renode/renode

**Register Definitions**:
- Location: `platforms/cpus/*/regs/*.repl`
- Format: Renode-specific format
- Coverage: Wide variety of ARM, RISC-V, and other architectures

### 7. LiteX SoC Builder

LiteX is a Python-based SoC builder with many built-in peripherals.

**Source**: https://github.com/enjoy-digital/litex

**Register Definitions**:
- Auto-generated from Python descriptions
- Export formats: CSV, JSON, SVD
- Can be converted to SystemRDL

## Benchmark Test File Structure

When adding real-world test cases, follow this structure:

```
benchmarks/rdl_files/
├── simple.rdl          # Baseline (3 regs)
├── medium.rdl          # Medium complexity (~20 regs)
├── large.rdl           # Large complexity (~70 regs)
├── opentitan/          # OpenTitan IP cores
│   ├── gpio.rdl
│   ├── uart.rdl
│   └── aes.rdl
├── pulp/               # PULP platform IPs
│   └── apb_uart.rdl
└── custom/             # Your own test cases
    └── my_soc.rdl
```

## Creating Test Cases from Real Projects

### Method 1: Manual Conversion

1. Find register specification (usually in documentation or CSV/JSON)
2. Convert to SystemRDL format following the specification
3. Validate with SystemRDL compiler
4. Add to benchmarks

### Method 2: Using ip-xact

Many projects provide IP-XACT register descriptions:

```python
from systemrdl.importer import IP_XACT_Importer

importer = IP_XACT_Importer()
root = importer.import_file("component.xml")
# Then export to RDL if needed
```

### Method 3: Using SystemRDL Compiler Tools

```python
from systemrdl import RDLCompiler

rdl = RDLCompiler()
# Import from various formats supported by plugins
```

## Recommended Test Cases for Benchmarking

Based on complexity and real-world usage:

| Test Case | Source | Register Count | Complexity | Purpose |
|-----------|--------|----------------|------------|---------|
| Simple GPIO | Generic | 3-5 | Low | Baseline |
| UART Basic | Generic | 10-15 | Low | Common peripheral |
| SPI Controller | Generic | 15-25 | Medium | Moderate complexity |
| DMA Controller | Generic | 30-50 | Medium | Multi-channel design |
| AES Engine | OpenTitan | 40-60 | High | Cryptographic block |
| Full SoC | OpenTitan/PULP | 200+ | Very High | System-level |

## Adding a New Test Case

1. **Obtain RDL file**: From one of the sources above or create your own

2. **Validate**: Ensure it compiles with SystemRDL compiler
   ```bash
   python -c "from systemrdl import RDLCompiler; rdl = RDLCompiler(); rdl.compile_file('your_file.rdl')"
   ```

3. **Add to benchmarks**: Place in `benchmarks/rdl_files/`

4. **Create test**: Add corresponding test in `test_benchmarks.py`
   ```python
   def test_export_opentitan_gpio(self, benchmark, benchmark_dir):
       """Benchmark OpenTitan GPIO export"""
       rdl_file = benchmark_dir / "opentitan" / "gpio.rdl"
       # ... benchmark code
   ```

5. **Document**: Add description in test docstring including:
   - Source project
   - Register count
   - Complexity characteristics

## Licensing Considerations

When using register definitions from real projects:

1. **Check License**: Ensure the register definitions are open-source
2. **Attribution**: Include source attribution in comments
3. **Compliance**: Follow license requirements (Apache, MIT, GPL, etc.)
4. **Derivative Works**: Be aware if your use constitutes a derivative work

## Example: Converting OpenTitan GPIO

Here's an example of how to create a test case from OpenTitan GPIO:

1. Clone OpenTitan
2. Locate `hw/ip/gpio/data/gpio.hjson`
3. Convert to SystemRDL (manual or tool-assisted)
4. Save as `benchmarks/rdl_files/opentitan/gpio.rdl`
5. Add test:

```python
def test_export_opentitan_gpio(self, benchmark, benchmark_dir):
    """Benchmark OpenTitan GPIO export (32 GPIO pins, ~10 registers)
    
    Source: https://github.com/lowRISC/opentitan
    License: Apache-2.0
    """
    rdl_file = benchmark_dir / "opentitan" / "gpio.rdl"
    
    def export_gpio():
        with tempfile.TemporaryDirectory() as tmpdir:
            rdl = RDLCompiler()
            rdl.compile_file(str(rdl_file))
            root = rdl.elaborate()
            
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="opentitan_gpio")
    
    benchmark(export_gpio)
```

## Performance Expectations

Based on typical real-world designs:

| Design Size | Register Count | Export Time (estimate) | Build Time (estimate) |
|-------------|----------------|------------------------|------------------------|
| Small IP | 5-20 | < 50ms | 5-10s |
| Medium IP | 20-50 | 50-150ms | 10-30s |
| Large IP | 50-100 | 150-300ms | 30-60s |
| Full SoC | 200+ | 300-1000ms | 60-300s |

Note: Build times depend heavily on:
- CPU cores available
- Compiler optimization level
- Use of split bindings
- System load

## Contributing

To contribute new real-world test cases:

1. Ensure proper licensing
2. Document the source
3. Create pull request with:
   - RDL file(s)
   - Benchmark test(s)
   - Documentation update

## Resources

- SystemRDL Specification: https://www.accellera.org/downloads/standards/systemrdl
- PeakRDL Toolchain: https://github.com/SystemRDL
- OpenTitan Documentation: https://opentitan.org/
- PULP Platform: https://pulp-platform.org/
