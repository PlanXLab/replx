# replx - Modern MicroPython CLI

[![PyPI version](https://badge.fury.io/py/replx.svg)](https://badge.fury.io/py/replx)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**replx** is a fast, modern command-line tool for MicroPython development. Upload files in seconds with **batch mode**, manage your device with intuitive commands, and integrate seamlessly with VS Code.

## Key Features

- **Blazing Fast** - Batch upload mode with 84% speed improvement
- **Smart File Sync** - Upload files & directories with automatic `.mpy` compilation  
- **VS Code Integration** - One command to set up your entire dev environment
- **Device Discovery** - Auto-detect connected MicroPython boards
- **Library Management** - Install packages from GitHub with versioning
- **Beautiful UI** - Rich terminal output with progress bars and panels
- **Interactive Shell** - Built-in shell for quick device management

---

## Quick Start

### Installation
```bash
pip install replx
```

**Requirements:**
- Python 3.10 or newer
- MicroPython device connected via USB
- Supported OS: Windows, Linux, macOS

### First Steps

**Find your device:**
```bash
replx scan
```

**Set up VS Code environment:**
```bash
replx -p COM3 env
```

**Run a script:**
```bash
replx run hello.py
```

That's it! 

---

## Common Workflows

### Upload & Run a Script
```bash
# Upload and execute in one command
replx run my_script.py

# Or use the shortcut (auto-detects .py files)
replx my_script.py -e
```

### Sync Your Project
```bash
# Upload entire directory with automatic compilation
replx put ./src lib/

# Install all libraries
replx install

# Update library cache from GitHub
replx update
```

### Interactive Development
```bash
# Open interactive REPL
replx repl

# Or use the built-in shell
replx shell
```

---

## Command Reference

### Global Options

| Option | Description |
|--------|-------------|
| `-p, --port` | Serial port name (or set `SERIAL_PORT` env var) |
| `-c, --command` | Execute raw command on device |
| `-v, --version` | Show version and exit |
| `--help` | Display help message |

**Tip:** Create `.vscode/.env` with `SERIAL_PORT=COM3` to avoid typing `-p` every time!

---

### Device Discovery

#### `scan` - Detect connected boards
```bash
replx scan              # Show all devices
replx scan --raw        # Show full firmware banner
```

**Example output:**
```
📟 COM10   v1.25  2025-01-15  ticle
📟 COM9    v1.24  2024-12-20  esp32
```

#### `port` - View/set serial port
```bash
replx port              # Show current port
replx port COM9         # Set new port
```

---

### File Operations

#### `get` - Download from device
```bash
replx get /main.py                    # Display file content
replx get /main.py ./backup_main.py   # Save to local file
```

#### `put` - Upload to device
```bash
replx put main.py                     # Upload to device root
replx put app/ lib/                   # Upload entire directory
replx put config.json /data/          # Upload to specific path
```

**Tip:** Directories are created automatically. Python files are compiled to `.mpy` for faster execution!

#### `ls` - List files
```bash
replx ls                # List root directory
replx ls /lib           # List specific directory
```

**Example output:**
```
  1024  📁  lib
   512  🐍  main.py
   256  📄  config.json
```

#### `rm` - Remove files/directories
```bash
replx rm old_script.py
replx rm /lib/old_module    # Removes recursively
```

#### `mkdir` - Create directory
```bash
replx mkdir /data
replx mkdir /lib/sensors
```

---

### Execute & Debug

#### `run` - Execute script on device
```bash
replx run test.py              # Run with default settings
replx run sensor.py -e         # Run with echo enabled
replx run app.py -n            # Non-interactive mode

# Shortcut: auto-detects .py extension
replx test.py
```

**Options:**
- `-e, --echo` - Show typed characters (for interactive scripts)
- `-n, --non-interactive` - Run without REPL (faster for automation)

#### `repl` - Interactive REPL
```bash
replx repl
```
Press `Ctrl+C` to exit.

---

### Library Management

#### `update` - Sync library cache
```bash
replx update                    # Update for connected device
replx update ticle              # Update specific device
replx update --owner PlanXLab --repo replx_libs --ref main
```

Updates local cache (`~/.replx/`) from GitHub registry.

#### `install` - Install libraries
```bash
replx install                   # Install all (core + device libs)
replx install core/             # Install only core libraries
replx install device/           # Install only device-specific libs
replx install ./mylib.py        # Install single local file
replx install https://raw.../sensor.py  # Install from URL
```

**Install targets:**

| SPEC | What gets installed |
|------|---------------------|
| *(empty)* | All core + device libraries |
| `core/` | Core libraries only (→ `/lib/`) |
| `device/` | Device-specific libraries (→ `/lib/<device>/`) |
| `./foo.py` | Single file (→ `/lib/foo.mpy`) |
| `./app/` | Directory contents (→ `/app/*.mpy`) |
| `https://...` | File from URL (→ `/lib/*.mpy`) |

#### `search` - Find libraries
```bash
replx search                    # List all available
replx search bme680             # Search specific library
replx search sensor --owner PlanXLab
```

**Example output:**
```
SCOPE   TARGET  VER   FILE
core    RP2350  1.5   src/machine.py
device  ticle   2.1*  src/sensors/bme680.py
```
`*` indicates newer version available

---

### Device Management

#### `mem` - Show memory usage
```bash
replx mem
```
```
Total:   512 KByte (524288)
Used:    128 KByte (131072)
Free:    384 KByte (393216)
Usage:   25.0 %
```

#### `df` - Show filesystem usage
```bash
replx df
```
```
Total:  2048 KByte (2097152)
Used:    512 KByte (524288)
Free:   1536 KByte (1572864)
Usage:   25.0 %
```

#### `reset` - Soft reset device
```bash
replx reset
```

#### `format` - Format filesystem
```bash
replx format
```
**Warning:** This erases all files on the device!

---

### Development Tools

#### `env` - Setup VS Code environment
```bash
replx -p COM3 env               # Auto-detect device
replx -p COM3 env ticle         # Force specific device
```

Creates `.vscode/` with:
- `.env` - Serial port configuration
- `tasks.json` - Build task with `replx` runner (press `Ctrl+Shift+B`)
- `settings.json` - Python path & linting config
- `launch.json` - Debug configuration
- Type hints for device-specific APIs

**Generated `.vscode/.env`:**
```bash
SERIAL_PORT=COM3
```

#### `shell` - Interactive shell
```bash
replx shell
```

**Built-in commands:**
```
clear, ls, cd, get, put, rm, mkdir, df, repl, pwd, exit
```

**Example session:**
```bash
📟 ticle:/ > ls
  512  🐍  main.py
  
📟 ticle:/ > cd lib

📟 ticle:/lib > pwd
/lib

📟 ticle:/lib > get config.json
{"version": "1.0"}

📟 ticle:/lib > exit
```

---

## Error Messages

replx automatically reformats MicroPython tracebacks with local file paths:

```
────────────────────────── Traceback ──────────────────────────
  File "C:\projects\myapp\sensor.py", line 22
    sensor.read()
ValueError: I2C bus error
```

---

## Performance

**Batch Upload Optimization:**
- **Before:** 32 seconds for 10 files (multiple REPL sessions)
- **After:** 5 seconds for 10 files (single REPL session)
- **Improvement:** 84% faster! 

**How it works:**
- Pre-compiles all Python files to `.mpy`
- Opens single REPL session
- Uploads all files in batch
- Closes REPL session once

---

## Configuration

### Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `SERIAL_PORT` | Default serial port | `COM3` or `/dev/ttyACM0` |
| `BAUD_RATES` | Serial baud rate | `115200` (default) |

### Auto-loaded `.vscode/.env`

replx automatically loads `.vscode/.env` if present in current or parent directories:

```bash
SERIAL_PORT=COM10
BAUD_RATES=115200
```

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| `No device connected` | Wrong port or device unplugged | Run `replx scan` to find port |
| `Could not enter raw REPL` | Device busy or in error state | Reset device and retry |
| `Permission denied` | Port access restricted | Linux/Mac: Add user to `dialout` group<br>Windows: Check driver installation |
| `Timeout during upload` | Serial buffer overflow | Reduce file size or check USB cable |
| `No such command 'x'` | Typo in command name | Run `replx --help` to see commands |

---

## Advanced Usage

### Direct Command Execution
```bash
# Execute Python code directly
replx -c "import machine; print(machine.unique_id())"

# One-liner device info
replx -c "import sys; print(sys.implementation)"
```

### Custom GitHub Repository
```bash
# Use your own library repo
replx update --owner myorg --repo my_micropython_libs --ref develop
replx install core/
```

### Batch Operations
```bash
# Upload multiple directories
replx put ./lib lib/
replx put ./config /config
replx put ./data /data

# Run tests
replx run tests/test_sensor.py -n
replx run tests/test_display.py -n
```

---

## Supported Devices

replx works with any MicroPython device that supports raw REPL mode:

- Raspberry Pi Pico / Pico W / Pico 2
- ESP32 / ESP32-S3 / ESP32-C6
- RP2350-based boards
- Custom MicroPython boards

**Tested on:**
- ticle (RP2350)
- ESP32 DevKit
- Raspberry Pi Pico W
- pyboard

---

## Auto-Update Check

replx checks PyPI for new versions on startup (max once per day):

```bash
New version available: 1.2.0
Run: pip install --upgrade replx
```

Suppressed for: `search`, `update`, `scan`, `port` commands.

---

## License

MIT License - see [LICENSE](LICENSE) file for details.

---

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

**Development setup:**
```bash
git clone https://github.com/PlanXLab/replx.git
cd replx
pip install -e .
```

---

## Support

- **Issues:** [GitHub Issues](https://github.com/PlanXLab/replx/issues)
- **Documentation:** This README
- **Discussions:** [GitHub Discussions](https://github.com/PlanXLab/replx/discussions)

---

## Acknowledgments

Built with:
- [Typer](https://typer.tiangolo.com/) - Beautiful CLI framework
- [Rich](https://rich.readthedocs.io/) - Terminal formatting
- [pySerial](https://pyserial.readthedocs.io/) - Serial communication
- [mpy-cross](https://github.com/micropython/micropython) - MicroPython cross-compiler

---

**Made with by PlanX Lab**
