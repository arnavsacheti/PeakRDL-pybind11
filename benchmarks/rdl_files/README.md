# Benchmark RDL Test Files

This directory contains SystemRDL test files for benchmarking the PeakRDL-pybind11 exporter.

## Test Files

### simple.rdl (3 registers)
**Purpose**: Baseline performance measurement
**Content**: Minimal register set with basic control and status registers
**Use Case**: Testing overhead and minimum export time

### medium.rdl (~20 registers)
**Purpose**: Moderate complexity testing
**Content**: 4 peripherals (UART, GPIO, SPI, Timer) with typical register sets
**Use Case**: Small embedded systems or IP blocks

### large.rdl (~70 registers)
**Purpose**: Multi-peripheral SoC testing
**Content**: 15+ peripherals including system control, multiple UARTs, GPIOs, SPIs, I2C, timers, DMA, interrupt controller
**Use Case**: Medium-sized SoC designs

### realistic_mcu.rdl (~288 registers, 1297 lines)
**Purpose**: Real-world microcontroller benchmarking
**Content**: Complete ARM Cortex-M style microcontroller with:
- **System Control**: Clock management, reset control, power management, peripheral clock enables
- **GPIO**: 5 ports (A-E) with full pin configuration (80 GPIO pins total)
- **UARTs**: 4 instances with full feature set (control, status, baud rate, data, flow control)
- **SPI**: 3 instances with CRC, DMA support, I2S mode
- **I2C**: 3 instances with multi-master, SMBus support
- **Timers**: 4 advanced timers with capture/compare, PWM, encoder modes
- **ADC**: 2 instances with 16 channels, watchdog, DMA support
- **DMA**: 8-channel controller with full configuration per channel
- **RTC**: Real-time clock with alarms, wakeup timer, calendar
- **Watchdog**: Independent watchdog timer
- **NVIC**: Nested vectored interrupt controller with priority management

**Inspired By**: STM32F4, NXP Kinetis, Nordic nRF, SiFive FE310
**Register Count**: 288 registers
**Use Case**: Real-world embedded microcontroller projects

## Register Count Summary

| File | Registers | Peripherals | Lines | Complexity |
|------|-----------|-------------|-------|------------|
| simple.rdl | 3 | 1 | 39 | Baseline |
| medium.rdl | ~20 | 4 | 114 | Small |
| large.rdl | ~70 | 15+ | 252 | Medium |
| realistic_mcu.rdl | 288 | 20+ | 1297 | Real-world |

## Performance Expectations

Based on benchmarks:

- **simple.rdl**: ~32ms export time
- **medium.rdl**: ~47ms export time  
- **large.rdl**: ~104ms export time
- **realistic_mcu.rdl**: ~858ms export time

Export time scales roughly linearly with register count at ~3ms per register for complex designs.

## Adding More Test Files

To add your own test RDL files:

1. Place the `.rdl` file in this directory
2. Add a corresponding test in `../test_benchmarks.py`
3. Update this README with file description
4. Document register count and complexity

For real-world designs, consider:
- Using actual production RDL files (with permission)
- Referencing open-source projects (OpenTitan, PULP, etc.)
- Creating representative synthetic designs

See `../REAL_WORLD_SOURCES.md` for information about obtaining real-world SystemRDL files.
