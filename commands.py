
class Cmd:
    CONNECT = 'connect'
    DISCONNECT_PORT = 'disconnect_port'
    SESSION_SETUP = 'session_setup'
    SESSION_DISCONNECT = 'session_disconnect'
    SESSION_SWITCH_FG = 'session_switch_fg'
    SESSION_INFO = 'session_info'
    SET_DEFAULT = 'set_default'
    RELEASE = 'release'

    STATUS = 'status'
    SHUTDOWN = 'shutdown'
    PING = 'ping'
    RESET = 'reset'

    EXEC = 'exec'
    RUN = 'run'
    RUN_STOP = 'run_stop'
    RUN_INTERACTIVE = 'run_interactive'

    REPL_ENTER = 'repl_enter'
    REPL_EXIT = 'repl_exit'
    REPL_WRITE = 'repl_write'
    REPL_READ = 'repl_read'

    LS = 'ls'
    LS_RECURSIVE = 'ls_recursive'
    CAT = 'cat'
    STAT = 'stat'
    IS_DIR = 'is_dir'

    RM = 'rm'
    RMDIR = 'rmdir'
    MKDIR = 'mkdir'
    CP = 'cp'
    MV = 'mv'
    TOUCH = 'touch'
    FORMAT = 'format'

    GET_FILE = 'get_file'
    GET_TO_LOCAL = 'get_to_local'
    GETDIR_TO_LOCAL = 'getdir_to_local'
    GET_FILE_BATCH = 'get_file_batch'
    PUT_FILE = 'put_file'
    PUT_FROM_LOCAL = 'put_from_local'
    PUT_FROM_LOCAL_STREAMING = 'put_from_local_streaming'
    PUTDIR_FROM_LOCAL = 'putdir_from_local'
    PUTDIR_FROM_LOCAL_STREAMING = 'putdir_from_local_streaming'
    PUT_FILE_BATCH = 'put_file_batch'

    MEM = 'mem'
    DF = 'df'

    I2C_BUS_SET = 'i2c_bus_set'
    I2C_BUS_GET = 'i2c_bus_get'
    I2C_BUS_CLEAR = 'i2c_bus_clear'

    UART_BUS_SET = 'uart_bus_set'
    UART_BUS_GET = 'uart_bus_get'
    UART_BUS_CLEAR = 'uart_bus_clear'

    SPI_BUS_SET = 'spi_bus_set'
    SPI_BUS_GET = 'spi_bus_get'
    SPI_BUS_CLEAR = 'spi_bus_clear'

class CmdGroups:
    NON_REPL = frozenset({
        Cmd.CONNECT, Cmd.RELEASE, Cmd.DISCONNECT_PORT,
        Cmd.STATUS, Cmd.SHUTDOWN, Cmd.PING, Cmd.RUN_STOP,
        Cmd.SESSION_INFO, Cmd.SESSION_SETUP, Cmd.SESSION_DISCONNECT,
        Cmd.SESSION_SWITCH_FG, Cmd.SET_DEFAULT,
        Cmd.I2C_BUS_SET, Cmd.I2C_BUS_GET, Cmd.I2C_BUS_CLEAR,
        Cmd.UART_BUS_SET, Cmd.UART_BUS_GET, Cmd.UART_BUS_CLEAR,
        Cmd.SPI_BUS_SET, Cmd.SPI_BUS_GET, Cmd.SPI_BUS_CLEAR,
    })

    READ_ONLY = frozenset({
        Cmd.SHUTDOWN, Cmd.PING, Cmd.SESSION_INFO, Cmd.STATUS,
        Cmd.EXEC, Cmd.LS, Cmd.LS_RECURSIVE, Cmd.CAT, Cmd.STAT,
        Cmd.IS_DIR, Cmd.MEM, Cmd.DF,
        Cmd.GET_FILE, Cmd.GET_TO_LOCAL, Cmd.GETDIR_TO_LOCAL,
        Cmd.GET_FILE_BATCH,
        Cmd.I2C_BUS_GET,
        Cmd.UART_BUS_GET,
        Cmd.SPI_BUS_GET,
    })

    SESSION = frozenset({
        Cmd.SESSION_SETUP, Cmd.SESSION_DISCONNECT, Cmd.SESSION_SWITCH_FG,
        Cmd.CONNECT, Cmd.DISCONNECT_PORT
    })

    PERSISTENT_BUSY = frozenset({
        Cmd.RUN_INTERACTIVE, Cmd.REPL_ENTER
    })

    STREAMING = frozenset({
        Cmd.PUT_FROM_LOCAL_STREAMING, Cmd.PUTDIR_FROM_LOCAL_STREAMING,
        Cmd.GETDIR_TO_LOCAL,
        Cmd.RUN_INTERACTIVE, Cmd.RUN_STOP
    })

    REPL = frozenset({
        Cmd.REPL_ENTER, Cmd.REPL_EXIT, Cmd.REPL_WRITE, Cmd.REPL_READ
    })

    DETACHED_ALLOW = frozenset({
        Cmd.RUN_STOP, Cmd.RESET, Cmd.STATUS, Cmd.PING,
        Cmd.SHUTDOWN, Cmd.SESSION_INFO,
        Cmd.SESSION_DISCONNECT, Cmd.DISCONNECT_PORT, Cmd.RELEASE
    })

NON_REPL_COMMANDS = CmdGroups.NON_REPL
READ_ONLY_COMMANDS = CmdGroups.READ_ONLY
