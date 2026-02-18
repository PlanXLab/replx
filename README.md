# replx - Modern MicroPython CLI with Agent Architecture

[![PyPI version](https://badge.fury.io/py/replx.svg)](https://badge.fury.io/py/replx)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**replx** is a fast, modern command-line tool for MicroPython development. Version 1.0 introduces a **multi-agent architecture** that maintains persistent connections to your device, eliminating connection overhead and enabling instant command execution.

## Key Features

- **Agent Architecture** - Background agent maintains persistent device connections
- **Zero Connection Overhead** - Agent handles all serial communication, instant command execution
- **Multi-Session Support** - Connect to multiple devices simultaneously
- **Smart File Sync** - Upload files & directories with automatic `.mpy` compilation  
- **VS Code Integration** - One command to set up your entire dev environment
- **Device Discovery** - Auto-detect connected MicroPython boards with device info
- **Library Management** - Install packages from GitHub with versioning and caching
- **WiFi Management** - Configure and manage WiFi connections (ESP32, etc.)
- **Firmware Updates** - Automatic firmware download and flashing (TiCLE, Pico)
- **Beautiful UI** - Rich terminal output with progress bars, panels, and colors
- **Interactive Shell** - Built-in shell with Unix-like commands for quick device management
- **Cross-Platform** - Windows, Linux, macOS with automatic port name handling

---

## Quick Start

### Installation
```bash
pip install replx
```

**Requirements:**
- Python 3.10 or newer
- MicroPython device (TiCLE, ESP32, Pico, etc.)
- Supported OS: Windows, Linux, macOS

### First Steps

**1. Find your device:**
```bash
replx scan
```

**2. Setup workspace and connect:**
```bash
replx --port COM10 setup
```
This command:
- Starts the background agent
- Connects to your device
- Creates VS Code configuration files
- Downloads type stubs for your device

**3. Run commands instantly:**
```bash
replx run hello.py      # Execute script
replx ls                # List files
replx repl              # Interactive REPL
```

**4. Release the port when done:**
```bash
replx shutdown
```

---

## Agent Architecture

The agent architecture provides significant performance improvements:

```
+-------------+    UDP/IPC    +---------------+      Serial      +----------+
|  replx CLI  |<------------>| Agent Server  |<---------------->|  Device  |
+-------------+               +---------------+                  +----------+
                              (Background Process)
```

### How It Works

1. **`replx setup`** starts a background agent that connects to your device
2. The agent maintains a persistent serial connection
3. All CLI commands communicate with the agent via UDP
4. Commands execute instantly without connection overhead

### Agent Management

#### Release the port
```bash
replx shutdown
```
Stops the agent and releases the serial port for other applications.
The next replx command will automatically restart the agent and reconnect.

---

## Common Workflows

### Upload & Run a Script
```bash
# Upload and execute in one command
replx run my_script.py

# Or use the shortcut (auto-detects .py files)
replx my_script.py

# Run with echo enabled for interactive scripts
replx -e my_script.py 
```

### Sync Your Project
```bash
# Upload files as-is (no compilation)
replx put ./src /

# Download and install libraries (with .mpy compilation)
replx pkg download          # Download to local cache
replx pkg update core/      # Compile and install to device
replx pkg update ./mylib.py # Compile and install local file
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
| `-p, --port` | Serial port name (or set via `.vscode/.replx`) |
| `-c, --command` | Execute raw command on device |
| `-v, --version` | Show version and exit |
| `--help` | Display help message |

**Tip:** After running `replx --port COM10 setup`, the port is saved and you don't need to specify it again.

---

### Connection & Session Management

#### `setup` - Initialize workspace and connect
```bash
replx --port COM10 setup
replx -p /dev/ttyACM0 setup
```

Creates `.vscode/` with:
- `.env` - Connection configuration
- `tasks.json` - Build task with `replx` runner (press `Ctrl+Shift+B`)
- `settings.json` - Python path & linting config
- Type stubs for device-specific APIs

#### `status` - Show session status
```bash
replx status
```

Shows all active agent sessions with connection state, device info, and memory usage.

#### `whoami` - Display current connection info
```bash
replx whoami
```

Shows currently connected device details (port, version, core, device).

#### `fg` - Switch foreground connection
```bash
replx fg                # Interactive: select from list
replx COM10 fg          # Switch to specific port
replx fg 1              # Switch by number
```

Switch between multiple connected devices in multi-session mode.

#### `disconnect` - Disconnect specific device
```bash
replx disconnect        # Interactive: select device
replx disconnect COM10  # Disconnect specific port
```

Disconnects a device but keeps agent running.

#### `shutdown` - Stop all agents
```bash
replx shutdown
```

Stops all agent processes and releases all ports. Alias: `free`

---

### Device Discovery

#### `scan` - Detect connected boards
```bash
replx scan
```

**Example output:**
```
PORT     VER     DATE        DEVICE
COM10    v1.27   2025-01-15  TiCLE
COM9     v1.26   2024-12-20  esp32
```

---

### File Operations

#### `ls` - List files
```bash
replx ls                # List root directory
replx ls /lib           # List specific directory
replx ls -r /           # List recursively (tree view)
```

**Options:**
- `-r, --recursive` - Show directory tree

#### `cat` - Display file content
```bash
replx cat /main.py              # Show file contents
replx cat -n /main.py           # Show with line numbers
replx cat -L 10:20 /main.py     # Show lines 10 to 20
```

**Options:**
- `-n, --number` - Show line numbers
- `-L, --lines N:M` - Line range (text) or byte range (binary)

#### `get` - Download from device
```bash
replx get /main.py ./           # Download to current directory
replx get /lib/*.py ./backup/   # Download multiple files
replx get / ./backup            # Download entire filesystem
replx get /lib ./backup         # Download directory
```

Supports wildcards and recursive directory downloads.

#### `put` - Upload to device
```bash
replx put main.py /             # Upload to device root
replx put ./src /lib            # Upload directory
replx put *.py /lib             # Upload multiple files
```

**Note:** Files are uploaded as-is without compilation. For compiled .mpy upload, use `replx pkg update`.

#### `rm` - Remove files/directories
```bash
replx rm /test.py               # Remove single file (asks confirm)
replx rm -f /test.py            # Remove without confirmation
replx rm -r /lib/backup         # Remove directory recursively
replx rm /*.pyc                 # Remove with wildcard
replx rm file1.py file2.py      # Remove multiple files
```

**Options:**
- `-r, --recursive` - Remove directories recursively
- `-f, --force` - Skip confirmation prompt

#### `mkdir` - Create directory
```bash
replx mkdir /data
replx mkdir /lib/sensors
```

#### `cp` - Copy files on device
```bash
replx cp /main.py /backup.py        # Copy file
replx cp -r /lib /lib_backup        # Copy directory
replx cp /*.py /backup/             # Copy with wildcards
```

**Options:**
- `-r, --recursive` - Copy directories recursively

#### `mv` - Move/rename files
```bash
replx mv /old.py /new.py            # Rename file
replx mv /test.py /backup/          # Move to directory
replx mv /*.py /lib/                # Move multiple files
```

#### `touch` - Create empty file
```bash
replx touch /config.json
```

---

### Execute & Debug

#### `run` - Execute script on device
```bash
replx run test.py              # Run local script
replx run -d main.py           # Run from device storage
replx run -e sensor.py         # Run with echo enabled
replx run -n app.py            # Non-interactive (detach)
```

**Options:**
- `-d, --device` - Run script from device storage (not local)
- `-e, --echo` - Show typed characters (for interactive scripts)
- `-n, --non-interactive` - Run without interaction (detach)

**Shortcut:** Files ending in `.py` auto-invoke `run`:
```bash
replx test.py           # Same as: replx run test.py
replx -e test.py        # Same as: replx run -e test.py
```

#### `repl` - Interactive REPL
```bash
replx repl
```
Type `exit` and press Enter to exit.

#### `exec` - Execute single command
```bash
replx exec "print('hello')"
replx -c "import machine; print(machine.freq())"
```

---

### Device Management

#### `usage` - Show memory and storage usage
```bash
replx usage
```

**Example output:**
```
Memory
   [==================          ] 62%
   Used: 128 KB  Free: 78 KB  Total: 206 KB

Storage
   [========                    ] 25%
   Used: 512 KB  Free: 1536 KB  Total: 2048 KB
```

#### `reset` - Soft reset device
```bash
replx reset                # Soft reset (default)
replx reset --soft         # Soft reset explicitly
replx reset --hard         # Hard reset with auto-reconnect
```

**Options:**
- `--soft` - Soft reset (default) - restarts Python interpreter, preserves WiFi
- `--hard` - Hard reset - full hardware reset like RESET button, WiFi disconnects

#### `format` - Format filesystem
```bash
replx format
```
**Warning:** This erases all files on the device!

#### `init` - Format and install libraries
```bash
replx init
```

Convenience command that combines:
1. `replx pkg download` - Download libraries to local cache (if needed)
2. `replx format` - Format device filesystem
3. `replx pkg update core/` and `device/` - Install core and device libraries

Perfect for setting up a fresh device.

#### `wifi` - WiFi management
```bash
replx wifi status              # Show WiFi connection status
replx wifi connect SSID [PW]   # Connect to WiFi
replx wifi save SSID [PW]      # Save WiFi credentials
replx wifi boot on             # Enable WiFi on boot
replx wifi boot off            # Disable WiFi on boot
replx wifi off                 # Disconnect WiFi
replx wifi scan                # Scan available networks
```

Manage WiFi connections for ESP32 and other WiFi-capable devices.

#### `firmware` - Firmware management (experimental)
```bash
replx firmware download        # Download latest firmware
replx firmware update          # Update device firmware
replx firmware update --force  # Force update even if same version
```

Automatic firmware download and flashing for supported devices (TiCLE, Pico).

---

### Library Management

#### `pkg download` - Download libraries to cache
```bash
replx pkg download              # Download for connected device
```

Downloads libraries from GitHub registry to local cache (`~/.replx/`) without installing to device.

**Options:**
- `--owner` - GitHub repository owner (default: PlanXLab)
- `--repo` - GitHub repository name (default: replx_libs)
- `--ref` - Git reference/branch (default: main)

#### `pkg update` - Install libraries to device

```bash
replx pkg update SPEC              # Install library/file to device
replx pkg update --target PATH     # Specify install location
```

Compiles Python files to .mpy and installs to device.

**Update targets:**

| SPEC | What gets installed |
|------|---------------------|
| `core/` | Core libraries (compiled) |
| `device/` | Device-specific libraries (compiled) |
| `./foo.py` | Single file compiled to `/lib/foo.mpy` |
| `./mylib/` | Directory compiled to `/lib/mylib/*.mpy` |
| `https://...` | File from URL compiled to `/lib/*.mpy` |

**Examples:**
```bash
replx pkg update core/             # Install only core libraries
replx pkg update device/           # Install only device-specific libs
replx pkg update ./mylib.py        # Compile and install single file
replx pkg update ./mylib_dir       # Compile and install directory
replx pkg update https://raw.../sensor.py  # Download and install from URL
replx pkg update ws2812 --target lib/ticle # Install to custom path
```

**Note:** Python files (.py) are automatically compiled to .mpy before upload for faster execution and less memory usage.

#### `pkg search` - Find libraries
```bash
replx pkg search                # List all available  
replx pkg search bme680         # Search specific library
```

Search for available libraries in the registry.

**Example output:**
```
SCOPE    TARGET       VER    FILE
core     RP2350       1.2    ain.py
core     RP2350       2.0    ble.py
device   ticle_lite   2.0    button.py
device   ticle_lite   3.0    sr04.py
```

#### `pkg` - Package management command
```bash
replx pkg search [PATTERN]      # Search libraries
replx pkg download              # Download to local cache
replx pkg update SPEC           # Compile and install to device
replx pkg clean                 # Remove current core/device from cache
```

**Subcommands:**
- `search` - Search available libraries
- `download` - Download libraries to local cache without installing
- `update` - Compile .py to .mpy and install to device
- `clean` - Remove current core/device libraries from cache

**Examples:**
```bash
replx pkg search                # List all available
replx pkg search bme680         # Search specific library
replx pkg download              # Download libs for current device
replx pkg update core/          # Install core libraries
replx pkg update ./mylib.py     # Compile and install local file
replx pkg clean                 # Remove current core/device from cache
```

#### `mpy` - Compile Python to .mpy
```bash
replx mpy input.py              # Compile to input.mpy
replx mpy input.py -o out.mpy   # Specify output file
replx mpy *.py                  # Compile multiple files
replx mpy src/                  # Compile directory
```

Compile Python files to .mpy bytecode for faster execution. The compiled file is saved locally.

**Supported architectures:**
- `armv7emsp` - RP2350
- `xtensa` - ESP32
- `xtensawin` - ESP32-S3

**Note:** Architecture is auto-detected from connected device. To compile and upload in one step, use `replx pkg update ./myfile.py`

---

### Interactive Shell

#### `shell` - Enter interactive shell mode
```bash
replx shell
```

**Built-in commands:**
```
File Operations:     ls, cat, get, put, rm, cp, mv, mkdir, touch
Device Management:   usage, reset
Execution:           exec, run, repl
Navigation:          cd, pwd
Utility:             clear, exit, help
```

**Features:**
- Unix-like command syntax
- Tab completion (if supported by terminal)
- Command history
- Current directory tracking (`cd`, `pwd`)
- Colored output and icons

**Example session:**
```
TiCLE:/ > ls
  512  main.py
 1024  lib/
  
TiCLE:/ > cd lib

TiCLE:/lib > ls
  256  config.py
  128  sensor.py

TiCLE:/lib > cat config.json
{"version": "1.0"}

TiCLE:/lib > cd ..

TiCLE:/ > exec "print('Hello from shell')"
Hello from shell

TiCLE:/ > exit
```

**Tips:**
- Use `help` to see available commands
- Use `clear` to clear the screen
- `Ctrl+C` to cancel current command
- `Ctrl+D` or `exit` to quit shell

---

## Error Messages

replx automatically reformats MicroPython tracebacks with local file paths:

```
-------------------------- Traceback --------------------------
  File "C:\projects\myapp\sensor.py", line 22
    sensor.read()
ValueError: I2C bus error
```

---

## Configuration

### Auto-loaded `.vscode/.replx`

replx automatically loads `.vscode/.replx` if present in current or parent directories.
This file is created by `replx setup`:

```ini
[COM10]
CORE=RP2350
DEVICE=ticle_lite
AGENT_PORT=49152

[DEFAULT]
CONNECTION=COM10
```

---

## Supported Devices

replx works with any MicroPython device that supports raw REPL mode:

### Primary Support
- **TiCLE** (RP2350) - Hanback Electronics educational board
- **TiCLE-Lite** - Compact version
- **TiCLE-Sensor** - Sensor-focused variant

### Also Compatible
- Raspberry Pi Pico / Pico W / Pico 2
- ESP32 / ESP32-S3 / ESP32-C6
- RP2040/RP2350-based boards
- Custom MicroPython boards

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| `No device connected` | Wrong port or device unplugged | Run `replx scan` to find port |
| `Could not enter raw REPL` | Device busy or in error state | Reset device and retry |
| `Permission denied` | Port access restricted | Linux/Mac: Add user to `dialout` group. Windows: Check driver |
| `Timeout during upload` | Serial buffer overflow | Reduce file size or check USB cable |

---

## Advanced Usage

### Direct Command Execution
```bash
# Execute Python code directly
replx -c "import machine; print(machine.unique_id())"

# One-liner system info
replx -c "import sys; print(sys.implementation)"
```

### Custom GitHub Repository
```bash
# Use your own library repo
replx pkg update --owner myorg --repo my_micropython_libs --ref develop
replx pkg update core/
```

### Batch Operations
```bash
# Upload multiple directories
replx put ./lib /lib
replx put ./config /config

# Run tests
replx run tests/test_sensor.py -n
replx run tests/test_display.py -n
```

---

## Auto-Update Check

replx checks PyPI for new versions on startup (max once per day):

```
New version available: 2.1.0
Run: pip install --upgrade replx
```

**Suppressed for:** `--help`, `--version`, and `scan` command

**Disable check:** Set environment variable `REPLX_NO_UPDATE_CHECK=1`

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
- [Typer](https://typer.tiangolo.com/) - CLI framework
- [Rich](https://rich.readthedocs.io/) - Terminal formatting
- [pySerial](https://pyserial.readthedocs.io/) - Serial communication
- [mpy-cross](https://github.com/micropython/micropython) - MicroPython cross-compiler

---

**Made with love by PlanX Lab**

