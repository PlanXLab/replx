# replx

[![PyPI version](https://badge.fury.io/py/replx.svg)](https://badge.fury.io/py/replx)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

`replx` is a CLI tool for MicroPython development that uses an agent-based architecture to connect and manage multiple CLI sessions and multiple boards simultaneously, improving connection efficiency and enabling parallel workflows.

---

## Core Architecture

```mermaid
flowchart LR
  CLI1[replx CLI A] <-->|UDP/IPC| AGENT[Agent Server]
  CLI2[replx CLI B] <-->|UDP/IPC| AGENT
  CLIN[replx CLI N] <-->|UDP/IPC| AGENT
  AGENT <-->|Serial| DEV1[Board A]
  AGENT <-->|Serial| DEV2[Board B]
  AGENT <-->|Serial| DEVN[Board N]

  BG[Background Process]:::note -.-> AGENT

  classDef note fill:#f8f9fa,stroke:#c9ccd1,color:#444;
```

Each CLI command communicates with a background Agent Server over UDP/IPC. The Agent Server handles all serial communication with the boards. This design allows multiple terminal sessions to share connection state while consistently handling FG/BG switching, status queries, and command execution.

### Session / FG / BG / Default

- **Session (SID)**: A unique work context created per terminal
- **FG (Foreground) connection**: The board used as the default target in the current session
- **BG (Background) connection**: Additional boards attached to the same session alongside FG
- **Default connection**: Board info saved by `setup`, reused when no port is specified

```mermaid
flowchart TB
  SID[Session SID]
  FG[FG Board<br/>Default board for session]
  BG1[BG Board 1]
  BG2[BG Board 2]
  DEF[Default Connection<br/>saved by setup]

  SID --> FG
  SID --> BG1
  SID --> BG2
  DEF -. Used when no FG exists .-> FG
```

Key rules:

1. A session can have **one FG + multiple BG** boards.
2. The FG board can be used without specifying a port in most commands.
3. To target a BG board, the port must be specified explicitly.
4. After `setup`, the saved default connection makes port-omitted execution straightforward.

### setup and Workspace Scope

`setup` is not just a connect command — it initializes the development environment relative to the **current VS Code workspace**.

```mermaid
flowchart TB
  ROOT[Filesystem Root<br/>C:\\ or /]
  WSA[Workspace A<br/>.vscode/.replx exists]
  WSB[Workspace B<br/>no parent config]
  SUB1[Subfolder A-1]
  SUB2[Subfolder A-2]
  NOTE_ROOT[Filesystem root<br/>cannot be a workspace]

  ROOT --> WSA
  ROOT --> WSB
  WSA --> SUB1
  WSA --> SUB2

  SUB1 -. run setup .-> WSA
  SUB2 -. run setup .-> WSA
  WSB -. run setup .-> WSB
  ROOT -. rule .-> NOTE_ROOT
```

1. Running `setup` configures MicroPython type hints, VS Code settings, and the default port (Default connection) for that workspace.
2. The filesystem root (`C:\`, `/`) cannot be used as a workspace.
3. A workspace can contain subfolders — running `setup` from a subfolder applies settings to the parent workspace scope.
4. If no workspace config exists in a parent path, each subdirectory is treated as an independent workspace with its own default port.

### Which board does a command target?

General commands (`run`, `ls`, `usage`, etc.) resolve the target board using the following flow:

```mermaid
flowchart TB
  CMD[Command<br/>run / ls / usage ...]
  HASPORT{Port specified?}
  HASFG{Session FG exists?}
  HASDEF{Saved Default exists?}
  USEPORT[Use specified port]
  USEFG[Use FG board]
  USEDEF[Connect via Default and use]
  ERR[No target<br/>run setup first]

  CMD --> HASPORT
  HASPORT -- yes --> USEPORT
  HASPORT -- no --> HASFG
  HASFG -- yes --> USEFG
  HASFG -- no --> HASDEF
  HASDEF -- yes --> USEDEF
  HASDEF -- no --> ERR
```

1. If a port is specified in the command, that port takes priority.
2. Otherwise, the session's FG board is used.
3. If no FG exists, a connection is attempted using the saved Default.

The recommended approach: **use `setup` to make a board the default**, and only specify a port when needed.

Both of the following forms work identically:

- `replx --port PORT setup`
- `replx PORT setup` (shorthand)

`PORT` depends on the OS:
- Windows: `COM4`
- Linux: `/dev/ttyACM0` or `/dev/ttyUSB0`
- macOS: `/dev/cu.usbmodem14101` or `/dev/cu.usbserial-0001`

### Two command groups

#### Commands that work without a port
These commands are primarily for querying or managing state, not direct board interaction. They operate independently or based on session context. Using `-p/--port` with these commands is an error.

- `replx scan` — discover connectable boards (no connection made)
- `replx status` — check session/FG/BG connection state
- `replx whoami` — show the FG board for the current session
- `replx shutdown` — shut down the agent and all connections
- `replx --help`, `replx COMMAND --help` — help
- `replx -v` — version

#### Commands that require a board connection
These commands communicate with the board. Except for `setup`, the port can be omitted if an FG or Default connection exists.

- Connection/session: `setup`, `fg`, `disconnect`
- Execution/interaction: `exec`, `run`, `repl`, `shell`
- Files: `ls`, `cat`, `get`, `put`, `cp`, `mv`, `rm`, `mkdir`, `touch`
- Board: `usage`, `reset`, `format`, `init`, `wifi`, `firmware`
- Package/compile: `pkg`, `mpy`

---

### Usage Scenarios

#### Scenario #1 — Start a new project
1. Run `replx scan` to identify the port
2. Run `replx PORT setup` to initialize the workspace and save the default connection
3. Run `replx ls` to confirm basic operation

#### Scenario #2 — Multiple terminals / multiple boards
1. Connect the required boards from terminals A and B
2. Run `replx status` to check FG/BG state per SID
3. Use `replx fg` or `replx PORT fg` to switch as needed

#### Scenario #3 — Quick code experimentation
- One-liner: `replx -c "print('hello')"`
- Run a file: `replx run app.py`
- Start REPL: `replx repl`

#### Scenario #4 — File and library management
- File sync: `put/get/ls/cat/...`
- Interactive file shell: `replx shell`
- Library update: `replx pkg search` → `replx pkg download` → `replx pkg update core.all`

#### Scenario #5 — End of session / cleanup
- Release port only: `replx disconnect`
- Full shutdown: `replx shutdown`

---

## Command Reference

### Command Summary

#### Connection / Session

| Command | Description |
|---|---|
| `setup` | Register the board as the default target and initialize the development environment. |
| `scan` | Discover available ports and board information. |
| `status` | Show FG/BG connection state per session. |
| `fg` | Switch the FG board for the current session. |
| `whoami` | Show the FG board the current session is targeting. |
| `disconnect` | Release the board connection for the current session. |
| `shutdown` | Shut down the agent and clean up all session connections. |

#### Execution / Interaction

| Command | Description |
|---|---|
| `exec` (`-c`) | Execute a short Python snippet immediately. |
| `run` | Run a local Python file on the board. |
| `repl` | Start an interactive REPL session. |
| `shell` | Open an interactive shell for board filesystem operations. |

#### File Operations

| Command | Description |
|---|---|
| `ls` | List files and directories at a board path. |
| `cat` | Print the contents of a board file. |
| `get` | Download a file or directory from the board to local. |
| `put` | Upload a local file or directory to the board. |
| `cp` | Copy files or directories within the board. |
| `mv` | Move or rename files or directories within the board. |
| `rm` | Delete files or directories on the board. |
| `mkdir` | Create a directory on the board. |
| `touch` | Create an empty file or update its timestamp. |

#### Board Management

| Command | Description |
|---|---|
| `usage` | Show storage and RAM usage with free space. |
| `reset` | Soft-reset the board. |
| `format` | Format the board filesystem. |
| `init` | Run initial setup scripts on the board. |
| `wifi` | Manage Wi-Fi configuration and connection state. |
| `firmware` | Check, download, or update board firmware. |

#### Package / Compile

| Command | Description |
|---|---|
| `pkg` | Search, download, and update MicroPython packages. |
| `mpy` | Compile `.py` files to `.mpy` bytecode. |

---

### Connection / Session

#### `setup`
The first command to run when starting a new project. After confirming the board connection, it creates `.vscode/tasks.json`, `.vscode/settings.json`, and `.vscode/launch.json`, configures the Pylance typehint path, and saves the current port as the workspace Default. A port is required. Using `clean` clears all existing connection history and keeps only the current port.
Settings apply at the workspace level; running from a subfolder applies them to the parent workspace scope. If no config exists in a parent path, each path is treated as an independent workspace with its own default port.
The filesystem root (`C:\`, `/`) cannot be used as a workspace.

Usage:
```sh
replx PORT setup
replx PORT setup clean
```

Examples:
```sh
replx COM3 setup
replx /dev/ttyACM0 setup
replx /dev/cu.usbmodem14101 setup
replx COM3 setup clean
```

#### `scan`
Use this to identify which port belongs to which board before connecting. Combines already-connected agent ports with freshly scanned ports and displays Port / Version / Core / Device / Manufacturer along with connected and Default status.

Usage:
```sh
replx scan
```

Examples:
```sh
replx scan
replx COM3 setup                  # setup using port found by scan
replx /dev/ttyACM0 setup          # Linux example
replx /dev/cu.usbmodem14101 setup # macOS example
```

Notes:
- `scan` only detects boards; it does not establish a connection.
- Using `-p/--port` with `scan` is an error.

#### `status`
Use this in a multi-terminal environment to inspect FG/BG connections per session at a glance. Shows the current session, foreground/background ports for each session, and the default port.

Usage:
```sh
replx status
```

Examples:
```sh
replx status
replx fg                        # switch foreground after checking status
```

#### `fg`
Switch the foreground board for the current session. `replx fg` presents an interactive list to choose from; `replx PORT fg` immediately promotes the specified port to FG. Subsequent commands (`ls`, `run`, `cat`, etc.) will target the new FG board.

Usage:
```sh
replx fg
replx PORT fg
```

Examples:
```sh
replx fg
replx COM19 fg
replx /dev/ttyACM0 fg
replx /dev/cu.usbmodem14101 fg
```

#### `whoami`
Instantly shows what FG board the current terminal session is targeting.

Usage:
```sh
replx whoami
```

Examples:
```sh
replx whoami
replx COM3 fg && replx whoami   # confirm target after switching FG
```

#### `disconnect`
Disconnects a specific port while keeping the agent process running. `replx disconnect` targets the current session's FG; `replx PORT disconnect` releases the specified port. When a connection is released, it is removed from all sessions that were referencing it.
If the FG is removed, one of the BG boards becomes FG. If no ports remain, the agent shuts down.

Usage:
```sh
replx disconnect
replx PORT disconnect
```

Examples:
```sh
replx disconnect
replx COM3 disconnect
replx /dev/ttyACM0 disconnect
replx /dev/cu.usbmodem14101 disconnect
```

#### `shutdown`
Use this when you are done working and want to clean up sessions, connections, and the agent in one step. The result varies by state: `Shutdown Complete` on success, `Already Shutdown` if already stopped, `Shutdown Failed` on error. The agent will restart automatically the next time a command requires it.

Usage:
```sh
replx shutdown
```

Examples:
```sh
replx shutdown
replx shutdown && replx status  # confirm state after shutdown
```

---

### Execution / Interaction

#### `exec` (`-c` alias)
Run a short MicroPython snippet without a file. Sends the `exec` command to the board once and returns standard output immediately. Multiple statements can be chained with semicolons (`;`).

Usage:
```sh
replx exec "CODE"
replx -c "CODE"
```

Examples:
```sh
replx -c "print('hello')"
replx -c "import os; print(os.listdir())"
replx exec "import machine; print(machine.freq())"
replx -c "import time; time.sleep(1); print('done')"
```

#### `run`
The primary command for running scripts. Default mode transfers a local file to the board and runs it interactively. `-d/--device` runs a file already stored on the board interactively. `-n/--non-interactive` detaches execution without waiting for I/O. `-e/--echo` echoes input to the terminal during interactive runs. `--non-interactive` and `--echo` cannot be used together.

Usage:
```sh
replx run SCRIPT
replx run -d SCRIPT
replx SCRIPT
```

Examples:
```sh
replx run main.py
replx main.py          # shorthand for .py files
replx run -d /test.py  # run board's /test.py interactively
replx run -n server.py # non-interactive (no I/O wait)
replx run -dn /main.py # run board's /main.py non-interactively
```

#### `repl`
Enter the board's Friendly REPL to experiment with code line by line. Output is displayed in real time. Type `exit()` to end the REPL session.

Usage:
```sh
replx repl
```

Examples:
```sh
replx repl
# Inside REPL:
>>> import os
>>> os.listdir()
>>> exit()
```

Key bindings:
- `Ctrl+C`: interrupt running code
- `Ctrl+D`: soft reset

#### `shell`
An interactive shell for continuous board filesystem management. Supports `ls/cat/cp/mv/rm/mkdir/touch` and `cd/pwd` relative to the current path. This is not a Python REPL. Inside the shell, `run` always operates on board files (`-d` mode), and `-n/-e` options are unavailable.

Usage:
```sh
replx shell
```

Available commands inside the shell:
```sh
# Files
ls cat cp mv rm mkdir touch

# Navigation
cd pwd clear

# Execution
exec run repl

# Other
usage wifi help exit
```

Examples:
```sh
replx shell
:/ > ls
:/ > cd lib
:/lib > cat boot.py
:/lib > exit
```

---

### File Operations

#### `ls`
List the contents of a board directory. Default shows a single-path listing; `-r/--recursive` outputs the full tree. Folder/file type icons and size information are shown, making it useful for pre/post-deploy comparisons.

Usage:
```sh
replx ls [PATH]
replx ls -r [PATH]
```

Examples:
```sh
replx ls
replx ls /lib
replx ls -r
replx ls -r /lib
```

#### `cat`
View the contents of a board file. Text files are shown as-is; binary files are displayed as a hex dump. `-n/--number` adds line numbers for text. `-L/--lines` selects a line range (`N:M`) for text or a byte range (`N:M`, `N:+M`) for binary.

Usage:
```sh
replx cat FILE
replx cat -n FILE
replx cat -L N:M FILE
```

Examples:
```sh
replx cat main.py
replx cat -n /lib/audio.py
replx cat -L 1:30 boot.py
replx cat -L 100:+64 app.mpy
```

#### `get`
Download files or directories from the board to local. Remote patterns (`*`, `?`) are resolved on the board side; directories are downloaded recursively. When downloading multiple sources, the last argument `LOCAL` must be a directory.

Usage:
```sh
replx get REMOTE LOCAL
replx get REMOTE... LOCAL
```

Examples:
```sh
replx get main.py ./
replx get / ./backup            # full board filesystem backup
replx get /a.py /b.py ./
replx get /lib/*.mpy ./compiled
```

#### `put`
Upload local files or directories to the board. Local wildcards (`*`, `?`) are expanded before upload; directories are transferred recursively. The last argument is always the remote destination path; remote directories are created automatically if needed.

Usage:
```sh
replx put LOCAL REMOTE
replx put LOCAL... REMOTE
```

Examples:
```sh
replx put main.py /
replx put ./lib/audio.py /lib
replx put a.py b.py /lib
replx put *.py /lib
```

#### `cp`
Copy files or directories within the board. Supports single files, directories, multiple sources, and patterns. `-r/--recursive` is required for directories; the destination must be a directory when copying multiple sources.

Usage:
```sh
replx cp SRC DEST
replx cp -r DIR DEST
```

Examples:
```sh
replx cp /main.py /backup.py
replx cp x.py y.py /backup
replx cp /lib/*.mpy /backup
replx cp -r /lib /lib_backup
```

#### `mv`
Move or rename files and directories within the board. Supports single moves, multiple sources, and patterns. `-r/--recursive` is required for directories. When moving multiple sources, verify the destination is a directory first.

Usage:
```sh
replx mv SRC DEST
replx mv -r DIR DEST
```

Examples:
```sh
replx mv /old.py /new.py
replx mv /main.py /backup
replx mv *.py /backup
replx mv -r /lib/audio /lib/sound
```

#### `rm`
Delete files or directories on the board. By default, a confirmation prompt is shown before deletion; `-f/--force` skips the prompt. `-r/--recursive` is required for directories. Pattern deletion (`*.py`, `/lib/*.mpy`) is supported.

Usage:
```sh
replx rm FILE
replx rm -r DIR
replx rm -f FILE
```

Examples:
```sh
replx rm /main.py
replx rm -f /main.py
replx rm /a.py /b.py
replx rm /lib/*.mpy
replx rm -rf /tmp
```

#### `mkdir`
Pre-create a directory structure on the board. Multiple paths can be created at once, including nested paths. Existing directories are silently retained.

Usage:
```sh
replx mkdir DIR
replx mkdir DIR...
```

Examples:
```sh
replx mkdir /lib
replx mkdir /tests
replx mkdir /lib/audio /lib/net
replx mkdir /a/b/c
```

#### `touch`
Create an empty file or ensure a file exists. Useful for setting up initial project templates with multiple files at once.

Usage:
```sh
replx touch FILE
replx touch FILE...
```

Examples:
```sh
replx touch /config.py
replx touch /a.py /b.py /c.py
replx touch /lib/__init__.py
```

---

### Board Management

#### `usage`
Check RAM (`mem`) and filesystem (`df`) usage simultaneously. Shows a usage bar alongside Used/Free/Total values, making it ideal for capacity checks before uploading.

Usage:
```sh
replx usage
replx PORT usage
```

Examples:
```sh
replx usage
replx COM3 usage
replx /dev/ttyACM0 usage
replx /dev/cu.usbmodem14101 usage
```

#### `reset`
Reset the board's execution state. Default is `--soft` (restart the interpreter); `--hard` performs a hardware reset followed by automatic reconnection. Using `--soft` and `--hard` together is an error.

Usage:
```sh
replx reset
replx reset --soft
replx reset --hard
```

Examples:
```sh
replx reset
replx reset --soft
replx reset --hard
```

Options:
- `--soft`: restart the Python interpreter (default)
- `--hard`: hardware reset + auto-reconnect

#### `format`
Completely wipe the board filesystem and start fresh. All user files including `boot.py`, `main.py`, and `lib/` are deleted. Some boards may not support the format command — check the error message if it fails.

Usage:
```sh
replx format
```

Examples:
```sh
replx get /*.py ./backup
replx get /lib ./backup
replx format
```

#### `init`
Restore a board to its initial state and reinstall required libraries. Internally runs `format` followed by sequential core/device package installation. The local package store must be prepared first — run `replx pkg download` before using this for the first time.

Usage:
```sh
replx init
```

Examples:
```sh
replx pkg download
replx init
```

Operation order:
1. Format filesystem
2. Install core libraries
3. Install device libraries

#### `wifi`
Manage the board's Wi-Fi status, connection, and boot auto-connect. `wifi connect SSID PW` saves credentials to the board (`wifi_config.py`) and connects immediately. `wifi connect` reconnects using saved settings. `wifi boot on|off` controls whether `boot.py` auto-connects on startup.

Usage:
```sh
replx wifi
replx wifi connect SSID PW
replx wifi connect
replx wifi scan
replx wifi off
replx wifi boot on|off
```

Examples:
```sh
replx wifi
replx wifi scan
replx wifi connect MySSID MyPassword
replx wifi connect
replx wifi boot on
replx wifi off
```

#### `firmware`
Download firmware locally or update via UF2 for supported boards. `download` only refreshes the local store; `update` puts the board into bootloader mode, locates the UF2 drive, and installs the firmware. `-f/--force` reinstalls even when the version matches. Currently officially supported boards: `ticle-lite`, `ticle-sensor`.

Usage:
```sh
replx firmware download
replx firmware update
replx firmware update -f
```

Options:
- `-f, --force`: force reinstall even if version is unchanged

Examples:
```sh
replx firmware download
replx firmware update
replx firmware update --force
```

---

### Package / Compile

#### `pkg`
Manages the package workflow (`search → download → update`) between the GitHub remote registry, local store, and board. `search [QUERY]` shows results scoped to the connected board's core/device. `download` fetches the remote registry to the local store. `update TARGET` installs packages to the board using `core.all`, `device.all`, `core.<file>`, `device.<file>`, or URL format. `clean` removes the current core/device entries from the local store. `--owner/--repo/--ref` specify the GitHub remote and apply to `search/download/update`. `-t/--target` specifies the board destination path for `update`.

Usage:
```sh
replx pkg SUBCOMMAND [args]
```

Examples:
```sh
replx pkg search
replx pkg search audio
replx pkg search --owner PlanXLab --repo replx_libs --ref main
replx pkg download
replx pkg download --owner PlanXLab --repo replx_libs --ref main
replx pkg update core.all
replx pkg update device.all
replx pkg update core.slip.py
replx pkg update device.termio.py
replx pkg update core.termio.py --target lib/ext
replx pkg update https://raw.githubusercontent.com/.../driver.py --target lib/ext
replx pkg clean
```

> Note: `pkg search/download/update` operates within the **core/device scope of the currently connected board**.

#### `mpy`
Compile `.py` files to `.mpy` bytecode. Automatically selects the target architecture based on the connected board's `core` and version. `-o/--output` can only be used when compiling a single file. `mpy-cross` must be installed locally before use.

Usage:
```sh
replx mpy FILES...
replx mpy main.py -o out.mpy
```

Examples:
```sh
replx mpy main.py
replx mpy main.py -o build/main.mpy
replx mpy *.py
replx mpy src/
```

---

## Troubleshooting

#### Connection / Session

| Symptom | Likely Cause | Resolution |
|---|---|---|
| `No active connections` | `setup` not run, agent stopped | `replx scan` → `replx PORT setup` → `replx status` |
| `Not connected` | No FG in current session | Check SID with `replx status`, then run `replx fg` or `replx PORT fg` |
| Board shows busy in another terminal | Another SID has it as FG/BG | Finish work in that terminal, or `disconnect`; use `shutdown` if needed |
| Port remains occupied | Agent still running | `replx disconnect` (individual) or `replx shutdown` (all) |

#### Execution (`exec/run/repl/shell`)

| Symptom | Likely Cause | Resolution |
|---|---|---|
| `run` fails immediately | Wrong file path | Local file: `run file.py`; board file: `run -d /path/file.py` |
| `--non-interactive` and `--echo` conflict | Invalid option combination | Use only one of the two |
| Hard to exit REPL | Unaware of exit method | Type `exit()` or press `Ctrl+D` |
| Unexpected path in shell | Current directory confusion | Check with `pwd`, navigate with `cd` |

#### File Operations

| Symptom | Likely Cause | Resolution |
|---|---|---|
| Wildcard behaves unexpectedly | Local vs. board expansion difference | `put` expands locally; `get/rm/cp/mv` resolve patterns on the board |
| Directory copy/move fails | Missing `-r` | Use `cp -r`, `mv -r`, `rm -r` |
| Multiple sources but destination is a file | Misunderstanding of last-arg rule | The last argument is always DEST; it must be a directory for multiple sources |
| Cannot recover after deletion | No backup | Back up with `replx get` before deleting |

#### Package / Compile

| Symptom | Likely Cause | Resolution |
|---|---|---|
| No `pkg search` results | Outside current board's core/device scope | Check board with `whoami`, switch board and search again |
| `pkg update` fails | Local store not prepared | Run `replx pkg download` first, then retry `replx pkg update ...` |
| `mpy` fails | `mpy-cross` not installed | `pip install mpy-cross` |
| Architecture error | Missing board connection info | Ensure connection is established (`setup` or auto-connect) |

#### Format / Init / Firmware

| Symptom | Likely Cause | Resolution |
|---|---|---|
| Files lost after `format/init` | Expected behavior (full wipe) | Back up with `get` before formatting; restore with `put`/`pkg update` |
| `firmware` unsupported error | Board not supported | Confirm board is a supported model (`ticle-*` series) |
| `wifi connect` fails | Wrong SSID/PW, AP not visible | `wifi scan` → re-enter credentials → try `wifi off` then retry |

---
