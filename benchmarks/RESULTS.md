# Benchmark Results Summary

This document contains sample benchmark results and analysis guidelines.

## System Information

Benchmarks should be run on a consistent system. Record:
- OS: Linux/macOS/Windows
- CPU: Model and core count
- RAM: Total available
- Python version
- Compiler version (for build benchmarks)

## Export Performance Benchmarks

### Typical Results (Reference System: Ubuntu Linux, 8-core CPU)

| Test Case | Min (ms) | Mean (ms) | Max (ms) | Registers | Complexity |
|-----------|----------|-----------|----------|-----------|------------|
| Simple | 30-35 | 32-35 | 40-45 | 3 | Baseline |
| Medium | 45-50 | 46-52 | 70-80 | ~20 | 4 peripherals |
| Large | 90-110 | 100-125 | 140-160 | ~70 | 15+ peripherals |
| Large + Splitting | 95-115 | 115-130 | 160-175 | ~70 | Split every 10 |
| Large + Hierarchical | 125-145 | 145-160 | 195-215 | ~70 | Split by hierarchy |

### Observations

1. **Linear Scaling**: Export time scales roughly linearly with register count
2. **Splitting Overhead**: Split bindings add 5-15% overhead due to additional file generation
3. **Hierarchical Impact**: Hierarchical splitting is slightly slower but provides better organization

### Scalability Results

| Register Count | Mean Export Time (ms) | Time per Register (ms) |
|----------------|----------------------|------------------------|
| 10 | 34-36 | 3.4-3.6 |
| 50 | 60-65 | 1.2-1.3 |
| 100 | 95-105 | 0.95-1.05 |

**Conclusion**: The exporter becomes more efficient per-register as designs scale up, likely due to fixed overhead amortization.

## Memory Usage

| Test Case | Peak Memory (MB) | Notes |
|-----------|------------------|-------|
| Large Export | 5-10 | Includes RDL compilation + export |

Memory usage is modest, well within typical development environments.

## Build Performance Benchmarks

**Note**: Build benchmarks are marked as `slow` and require:
- python-build
- CMake 3.15+
- C++11 compiler

### Expected Build Times (Reference: 8-core CPU, parallel build)

| Distribution Type | Simple | Medium | Large | Notes |
|-------------------|--------|--------|-------|-------|
| Source (sdist) | 5-10s | 10-15s | 15-25s | Tar.gz creation |
| Wheel | 8-15s | 15-30s | 30-90s | C++ compilation |

**Build Time Factors**:
1. **CPU Cores**: More cores = faster parallel compilation
2. **Compiler**: GCC, Clang, MSVC have different performance
3. **Split Bindings**: Can significantly speed up builds via parallelism
4. **Optimization Level**: -O1 vs -O3 affects compile time

### Compilation Optimization Impact

From split bindings on large projects:
- **No split** (single file): 60-120s compilation
- **Split by hierarchy**: 30-60s compilation (2x faster)
- **Split by count** (every 10): 25-50s compilation (2.5x faster)

Parallel compilation (`make -j8`) is most effective with split bindings.

## Performance Recommendations

### For Small Projects (< 20 registers)
- No need for split bindings
- Export time: negligible (< 50ms)
- Build time: fast (< 15s)
- **Recommendation**: Use default settings

### For Medium Projects (20-100 registers)
- Consider split bindings for faster compilation
- Export time: acceptable (50-150ms)
- Build time: moderate (15-60s)
- **Recommendation**: Use `--split-by-hierarchy` for cleaner organization

### For Large Projects (100+ registers)
- Always use split bindings
- Export time: still fast (100-300ms)
- Build time: significant benefit from splitting (2-3x faster)
- **Recommendation**: Use `--split-bindings 50` or `--split-by-hierarchy`

## Interpreting Benchmark Statistics

### Key Metrics

- **Min**: Best-case performance (warmed up, no interference)
- **Max**: Worst-case performance (includes outliers)
- **Mean**: Average performance (most representative)
- **StdDev**: Variability (lower is more consistent)

### What to Look For

1. **Low StdDev**: Indicates consistent, predictable performance
2. **Mean close to Min**: Suggests minimal overhead
3. **Large Max**: May indicate system interference (other processes)

### Comparing Runs

When comparing different approaches:
1. Look at Mean for typical performance
2. Check StdDev for consistency
3. Run multiple times to confirm trends
4. Use same hardware/conditions for fair comparison

## Continuous Monitoring

### Tracking Performance Over Time

1. **Baseline**: Establish baseline with current version
   ```bash
   pytest benchmarks/ --benchmark-only --benchmark-save=baseline
   ```

2. **After Changes**: Run and compare
   ```bash
   pytest benchmarks/ --benchmark-only --benchmark-compare=0001
   ```

3. **CI Integration**: GitHub workflow runs benchmarks automatically

### Performance Regression Detection

Watch for:
- Mean time increase > 10% without code changes (system issue)
- Mean time increase > 20% after code changes (potential regression)
- StdDev increase (new source of variability)

## Real-World Validation

### Testing with Production Designs

For production validation:
1. Export your actual RDL files
2. Measure export + build time
3. Compare with benchmark predictions
4. Report significant deviations

### Expected vs Actual

If actual performance differs significantly from benchmarks:
- Check system resources (CPU, RAM)
- Verify compiler optimization settings
- Look for unusual RDL features (deep nesting, many arrays)
- File issue with example if reproducible regression

## Benchmark Maintenance

### When to Update Benchmarks

- Major algorithm changes
- New optimization features
- Adding real-world test cases
- Performance regression fixes

### Adding New Benchmarks

See `benchmarks/README.md` for guide on adding:
- New RDL test files
- New benchmark test cases
- Real-world design examples

## Performance Goals

Based on benchmarks, the project aims to:

1. **Export**: < 1ms per register (currently ~1ms for large designs)
2. **Build**: < 1s per register with split bindings
3. **Memory**: < 100MB for designs with 1000+ registers
4. **Scalability**: Linear or better scaling to 1000+ registers

## Reporting Performance Issues

If you encounter performance issues:

1. Run benchmarks on your system
2. Save results: `pytest benchmarks/ --benchmark-only --benchmark-json=results.json`
3. Include system info (OS, CPU, Python version)
4. Attach your RDL file (if possible)
5. File issue with benchmark data

## Further Reading

- [Benchmark README](README.md): Complete benchmark documentation
- [Real-World Sources](REAL_WORLD_SOURCES.md): Production RDL examples
- [Compilation Optimizations](../COMPILATION_OPTIMIZATIONS.md): Build optimization guide
- [pytest-benchmark docs](https://pytest-benchmark.readthedocs.io/): Tool documentation
