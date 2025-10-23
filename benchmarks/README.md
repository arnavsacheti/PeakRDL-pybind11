# PeakRDL-pybind11 Benchmarks

This directory contains performance benchmarks for the PeakRDL-pybind11 exporter.

## Overview

The benchmarks measure performance across several dimensions:

1. **Export Performance**: Time to convert RDL → pybind11 C++ code
2. **Build Performance**: Time to build distribution files (sdist/wheel)
3. **Memory Usage**: Peak memory consumption during export
4. **Scalability**: How performance scales with register count

## Test RDL Files

The benchmarks use three complexity levels:

- **simple.rdl**: 3 registers (baseline performance)
- **medium.rdl**: ~20 registers across 4 peripherals (UART, GPIO, SPI, Timer)
- **large.rdl**: ~70 registers across 15+ peripherals (realistic SoC)

## Running Benchmarks

### Basic Usage

Run all benchmarks:
```bash
pytest benchmarks/ --benchmark-only
```

Run only export benchmarks (fast):
```bash
pytest benchmarks/test_benchmarks.py::TestExportBenchmarks --benchmark-only
```

Skip slow build benchmarks:
```bash
pytest benchmarks/ --benchmark-only -m "not slow"
```

### Advanced Options

Save benchmark results to JSON:
```bash
pytest benchmarks/ --benchmark-only --benchmark-json=results.json
```

Compare with previous results:
```bash
pytest benchmarks/ --benchmark-only --benchmark-compare=0001
```

Generate HTML report:
```bash
pytest benchmarks/ --benchmark-only --benchmark-histogram=histogram
```

Only run a specific benchmark:
```bash
pytest benchmarks/test_benchmarks.py::TestExportBenchmarks::test_export_large_rdl --benchmark-only
```

### Verbose Output

See detailed statistics:
```bash
pytest benchmarks/ --benchmark-only --benchmark-verbose
```

## Benchmark Categories

### TestExportBenchmarks

Measures the performance of RDL compilation and export to pybind11 modules:

- `test_export_simple_rdl`: 3 registers (baseline)
- `test_export_medium_rdl`: ~20 registers
- `test_export_large_rdl`: ~70 registers
- `test_export_large_rdl_with_splitting`: Large with register-count splitting
- `test_export_large_rdl_hierarchical_split`: Large with hierarchical splitting

### TestBuildBenchmarks (marked as `slow`)

Measures the time to build distribution packages:

- `test_build_sdist_*`: Source distribution (.tar.gz) build times
- `test_build_wheel_*`: Wheel distribution (.whl) build times

**Note**: These tests require `python-build` to be installed:
```bash
pip install build
```

Build benchmarks also require CMake and a C++ compiler.

### TestMemoryBenchmarks

Measures peak memory consumption during export:

- `test_memory_export_large`: Memory usage for large RDL export

### TestScalabilityBenchmarks

Tests how performance scales with different register counts:

- `test_scaling_with_register_count`: 10 registers (baseline)
- `test_scaling_50_registers`: 50 registers
- `test_scaling_100_registers`: 100 registers

## Expected Results

Typical performance on a modern laptop (for reference):

| Operation | Simple | Medium | Large |
|-----------|--------|--------|-------|
| Export | ~10-50ms | ~50-100ms | ~100-300ms |
| Build (wheel) | ~5-10s | ~10-20s | ~20-60s |

Note: Build times heavily depend on:
- CPU cores available (parallel compilation)
- Compiler optimization settings
- Whether split bindings are used

## Interpreting Results

pytest-benchmark provides several statistics:

- **min**: Fastest execution time
- **max**: Slowest execution time
- **mean**: Average execution time
- **stddev**: Standard deviation (variability)
- **rounds**: Number of iterations run
- **iterations**: Number of calls per round

Lower values are better. Watch for:
- Large stddev indicates inconsistent performance
- Compare mean values between different approaches
- Use min for best-case, max for worst-case scenarios

## Tips for Accurate Benchmarking

1. **Minimize background processes**: Close unnecessary applications
2. **Run multiple times**: Benchmarks automatically run multiple rounds
3. **Use consistent hardware**: Don't compare results from different machines
4. **Consider warmup**: First run may be slower due to cold caches
5. **Check system load**: High CPU/memory usage affects results

## Customizing Benchmarks

### Adding New Test Cases

To add a new RDL test case:

1. Create RDL file in `benchmarks/rdl_files/`
2. Add test method following existing patterns
3. Use `benchmark` fixture for timing

Example:
```python
def test_export_custom(self, benchmark, benchmark_dir):
    """Benchmark export of custom RDL file"""
    rdl_file = benchmark_dir / "custom.rdl"
    
    def export_custom():
        with tempfile.TemporaryDirectory() as tmpdir:
            rdl = RDLCompiler()
            rdl.compile_file(str(rdl_file))
            root = rdl.elaborate()
            
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="custom")
    
    benchmark(export_custom)
```

## Real-World RDL Sources

For benchmarking with real-world register maps, consider:

### OpenTitan

OpenTitan is an open-source silicon root of trust project with extensive register maps:
- GitHub: https://github.com/lowRISC/opentitan
- Register definitions in `hw/ip/*/data/*.hjson`
- Can be converted to SystemRDL format

### CERN SoC Projects

CERN develops various SoC projects with register definitions:
- OHWR (Open Hardware Repository): https://ohwr.org/
- Various IP cores with register maps

### Renode

Renode simulation framework includes register definitions:
- GitHub: https://github.com/renode/renode
- Platform descriptions include register maps

### Using External RDL Files

To benchmark with external RDL files:

1. Place RDL file in `benchmarks/rdl_files/`
2. Add corresponding test in `test_benchmarks.py`
3. Document register count and complexity in test docstring

## Continuous Integration

To run benchmarks in CI without slowdowns:

```bash
# Fast export-only benchmarks
pytest benchmarks/ --benchmark-only -m "not slow" --benchmark-disable-gc

# Save results for tracking over time
pytest benchmarks/ --benchmark-only --benchmark-json=ci_results.json
```

## Troubleshooting

### "pytest-benchmark not found"
```bash
pip install pytest-benchmark
```

### "python-build not found" (for build benchmarks)
```bash
pip install build
```

### Build benchmarks fail
Ensure you have:
- CMake 3.15+
- C++11 compatible compiler
- pybind11 headers

### Benchmarks too slow
- Skip slow tests with `-m "not slow"`
- Reduce rounds: `--benchmark-min-rounds=1`
- Disable GC: `--benchmark-disable-gc`

## Further Reading

- pytest-benchmark docs: https://pytest-benchmark.readthedocs.io/
- PeakRDL-pybind11 README: ../README.md
- Compilation optimization guide: ../COMPILATION_OPTIMIZATIONS.md
