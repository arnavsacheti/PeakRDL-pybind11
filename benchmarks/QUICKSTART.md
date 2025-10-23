# Benchmarking Quick Start Guide

This guide helps you get started with benchmarking PeakRDL-pybind11.

## Installation

Install benchmark dependencies:

```bash
# Basic benchmarks (export only)
pip install pytest-benchmark

# Full benchmarks (including build tests)
pip install pytest-benchmark build
```

Or use the dependency groups:

```bash
# For export benchmarks
uv pip install --group benchmark

# For all benchmarks including build tests
uv pip install --group build-bench
```

## Quick Start

### 1. Run Fast Benchmarks (Recommended)

Run only the fast export benchmarks:

```bash
python benchmarks/run_benchmarks.py fast
```

This takes about 15-20 seconds and measures export performance.

### 2. Run All Export Benchmarks

```bash
python benchmarks/run_benchmarks.py export
```

### 3. Run Scalability Tests

See how performance scales with register count:

```bash
python benchmarks/run_benchmarks.py scaling
```

### 4. Run Everything

To run all benchmarks (may take several minutes):

```bash
python benchmarks/run_benchmarks.py all
```

## Understanding Results

After running benchmarks, you'll see output like:

```
Name (time in ms)              Min      Max     Mean  StdDev  Rounds
-----------------------------------------------------------------
test_export_simple_rdl     30.76    42.00   31.93    2.20      27
test_export_medium_rdl     47.66    78.24   51.45    6.16      22
test_export_large_rdl     108.13   159.47  123.69   22.18       7
```

**Key Metrics**:
- **Min**: Fastest run (best case)
- **Mean**: Average time (most important)
- **Max**: Slowest run (worst case)
- **StdDev**: Variability (lower is better)

## Common Commands

### Save Results for Later Comparison

```bash
pytest benchmarks/ --benchmark-only --benchmark-save=my-baseline
```

### Compare with Previous Results

```bash
pytest benchmarks/ --benchmark-only --benchmark-compare=0001
```

### Generate Histogram

```bash
pytest benchmarks/ --benchmark-only --benchmark-histogram=histogram
```

This creates `histogram.svg` with visual results.

### Run Specific Benchmark

```bash
# Just the simple export test
pytest benchmarks/test_benchmarks.py::TestExportBenchmarks::test_export_simple_rdl --benchmark-only

# All export tests
pytest benchmarks/test_benchmarks.py::TestExportBenchmarks --benchmark-only
```

## Benchmark Categories

- **Export**: RDL → pybind11 conversion time
- **Scaling**: Performance vs. register count
- **Memory**: Peak memory usage
- **Build** (slow): Distribution file build time

## Skip Slow Tests

Build benchmarks are marked as `slow` because they compile C++:

```bash
# Skip build benchmarks
pytest benchmarks/ --benchmark-only -m "not slow"

# Or use the fast command
python benchmarks/run_benchmarks.py fast
```

## Expected Performance

Typical results on modern laptop:

| Design Size | Registers | Export Time | Build Time |
|-------------|-----------|-------------|------------|
| Small | 3-20 | < 50ms | 5-15s |
| Medium | 20-50 | 50-150ms | 15-30s |
| Large | 50-100 | 100-300ms | 30-90s |

If your results differ significantly, check:
- System load (close other applications)
- CPU/RAM availability
- Compiler optimization settings

## Troubleshooting

### "pytest-benchmark not found"

```bash
pip install pytest-benchmark
```

### Build benchmarks fail

Build benchmarks require:
- CMake 3.15+
- C++11 compiler (gcc, clang, or MSVC)
- pybind11

Skip them with `-m "not slow"` if not needed.

### Results vary widely

- Close other applications
- Run multiple times: benchmarks automatically average
- Use `--benchmark-disable-gc` to reduce variability

## Advanced Usage

### Calibrate for Your System

```bash
pytest benchmarks/ --benchmark-only --benchmark-calibration-precision=100
```

### Minimum/Maximum Rounds

```bash
# More rounds for accuracy (slower)
pytest benchmarks/ --benchmark-only --benchmark-min-rounds=10

# Fewer rounds for speed
pytest benchmarks/ --benchmark-only --benchmark-min-rounds=3
```

### JSON Output

```bash
pytest benchmarks/ --benchmark-only --benchmark-json=results.json
```

Parse with tools or share with team.

## CI/CD Integration

The GitHub Actions workflow automatically runs benchmarks on:
- Pull requests that modify benchmarks or core code
- Weekly (Sunday 00:00 UTC)
- Manual trigger

View results in Actions tab.

## Next Steps

- Read [benchmarks/README.md](README.md) for detailed docs
- See [benchmarks/RESULTS.md](RESULTS.md) for result interpretation
- Check [benchmarks/REAL_WORLD_SOURCES.md](REAL_WORLD_SOURCES.md) for RDL examples

## Getting Help

If you have questions or issues:

1. Check [benchmarks/README.md](README.md) for detailed information
2. Review [pytest-benchmark docs](https://pytest-benchmark.readthedocs.io/)
3. Open an issue with benchmark results and system info
