"""
Hardware control module for MicroPython.

Provides access to hardware-specific functions for GPIO, PWM, I2C, SPI,
UART, ADC, Timer, RTC, and other peripheral interfaces.

Example
-------
```python
    >>> from machine import Pin, PWM, I2C
    >>> 
    >>> # GPIO control
    >>> led = Pin(25, Pin.OUT)
    >>> led.toggle()
    >>> 
    >>> # PWM output
    >>> pwm = PWM(Pin(15))
    >>> pwm.freq(1000)
    >>> pwm.duty_u16(32768)
    >>> 
    >>> # I2C communication
    >>> i2c = I2C(0, scl=Pin(1), sda=Pin(0), freq=400000)
    >>> devices = i2c.scan()
```
"""

from typing import Callable, Optional, Union, Any

# Type aliases
_IRQHandler = Callable[["Pin"], None]


def freq(hz: Optional[int] = None) -> int:
    """
    Get or set the CPU frequency.

    This controls the CPU (and sometimes system) clock rate. Changing the CPU
    frequency can trade performance vs power consumption and may affect timing-
    sensitive code.

    Support and valid frequency values are port/board specific. On ports that
    support changing frequency, passing a value attempts to reconfigure the CPU
    clock. On ports that don't support changing frequency, providing ``hz`` may
    raise an exception.

    :param hz: New frequency in Hz (if setting). Use ``None`` to query only.

    :returns: Current CPU frequency in Hz.

    :raises ValueError: If the requested frequency is invalid (port-specific).
    :raises OSError: If the frequency cannot be set on this port/board.

    Example
    -------
    ```python
        >>> from machine import freq
        >>> 
        >>> print(freq())        # Get current frequency
        >>> freq(250_000_000)    # Set to 250MHz
        >>> 
        >>> # Restore previous frequency
        >>> old = freq()
        >>> try:
        ...     freq(old)
        ... except Exception:
        ...     # Some ports may not allow changing it
        ...     pass
    ```
    """
    ...


def unique_id() -> bytes:
    """
    Get the unique identifier of the board.

    The returned value is a board- or MCU-specific identifier. It is commonly
    used to derive a per-device name, a network identifier, or to seed a PRNG.
    The exact length and stability guarantees are port dependent.

    :returns: Unique ID as bytes (often 6 or 8 bytes, but port-specific).

    Example
    -------
    ```python
        >>> from machine import unique_id
        >>> 
        >>> uid = unique_id()
        >>> print(uid.hex())
    ```
    """
    ...


def reset() -> None:
    """
    Reset the device.

    This performs a full system reset (similar to pressing the reset button).
    After calling this function the board restarts and the current program
    does not continue.

    Example
    -------
    ```python
        >>> from machine import reset
        >>> 
        >>> reset()  # Device will restart
    ```
    """
    ...


def soft_reset() -> None:
    """
    Perform a soft reset of the interpreter.

    A soft reset restarts the MicroPython runtime (VM) without necessarily
    performing the same kind of full hardware reset as ``reset()``. The exact
    effect on peripherals and hardware state is port/board specific.

    Example
    -------
    ```python
        >>> from machine import soft_reset
        >>> 
        >>> soft_reset()
    ```
    """
    ...


def reset_cause() -> int:
    """
    Get the reset cause.

    The returned value can be compared to reset-cause constants such as
    ``PWRON_RESET`` or ``WDT_RESET``. Availability and meaning of constants
    is port dependent.

    :returns: Reset cause constant

    Example
    -------
    ```python
        >>> from machine import reset_cause, PWRON_RESET, WDT_RESET
        >>> 
        >>> cause = reset_cause()
        >>> if cause == PWRON_RESET:
        ...     print("Power-on reset")
    ```
    """
    ...


def bootloader(size: int = 0) -> None:
    """
    Enter the bootloader (UF2 mode on RP2).

    This is typically used to put the board into a firmware update mode.
    Behavior varies by port/board. On some ports the optional ``size``
    argument is used to control the USB MSC/UF2 presentation.

    :param size: Optional size parameter

    Example
    -------
    ```python
        >>> from machine import bootloader
        >>> 
        >>> bootloader()  # Enter UF2 mode
    ```
    """
    ...


def idle() -> None:
    """
    Wait for an interrupt, reducing power consumption.

    This function hints to the CPU that it can sleep until the next interrupt.
    It is commonly used in polling loops to reduce power consumption.

    Example
    -------
    ```python
        >>> from machine import idle
        >>> 
        >>> while True:
        ...     idle()  # Low-power wait
    ```
    """
    ...


def lightsleep(time_ms: Optional[int] = None) -> None:
    """
    Enter light sleep mode.

    Light sleep is a low-power state where the CPU stops or slows down, but the
    system can usually resume without a full reset. Wake sources and what state
    is retained are port/board specific.

    :param time_ms: Sleep duration in milliseconds (None = indefinite)

    Example
    -------
    ```python
        >>> from machine import lightsleep
        >>> 
        >>> lightsleep(5000)  # Sleep for 5 seconds
    ```
    """
    ...


def deepsleep(time_ms: Optional[int] = None) -> None:
    """
    Enter deep sleep mode.

    Deep sleep is a lower-power state than ``lightsleep()``. On many ports the
    device will reset on wake-up (i.e. the program restarts from boot). What
    memory/state is retained depends on the port and hardware.

    :param time_ms: Sleep duration in milliseconds (None = indefinite)

    Example
    -------
    ```python
        >>> from machine import deepsleep
        >>> 
        >>> deepsleep(10000)  # Deep sleep for 10 seconds
    ```
    """
    ...


def disable_irq() -> int:
    """
    Disable interrupt requests.

    Returns a value representing the previous IRQ state. Pass this value back
    to ``enable_irq()`` to restore the prior state.

    This is useful for short critical sections. Keep the protected code small:
    disabling interrupts for too long can break time-sensitive peripherals.

    :returns: Previous IRQ state (pass to enable_irq)

    Example
    -------
    ```python
        >>> from machine import disable_irq, enable_irq
        >>> 
        >>> state = disable_irq()
        >>> try:
        ...     # Critical section
        ...     pass
        ... finally:
        ...     enable_irq(state)
    ```
    """
    ...


def enable_irq(state: int = 1) -> None:
    """
    Re-enable interrupt requests.

    The recommended pattern is to call ``enable_irq()`` with the state returned
    by ``disable_irq()``. Passing ``1`` will enable IRQs unconditionally.

    :param state: IRQ state from disable_irq()

    Example
    -------
    ```python
        >>> from machine import disable_irq, enable_irq
        >>> 
        >>> state = disable_irq()
        >>> # Critical section
        >>> enable_irq(state)
    ```
    """
    ...


def mem8() -> Any:
    """Memory access at byte level.

    This provides a memory-mapped view for direct register/RAM access:
    ``mem8[addr]`` reads a byte and ``mem8[addr] = value`` writes a byte.

    Misaligned or invalid addresses can raise an exception or cause a crash,
    depending on the MCU and port.
    """
    ...


def mem16() -> Any:
    """Memory access at 16-bit level.

    Provides 16-bit (halfword) memory-mapped access via indexing.
    Addresses are typically expected to be 2-byte aligned.
    """
    ...


def mem32() -> Any:
    """Memory access at 32-bit level.

    Provides 32-bit (word) memory-mapped access via indexing.
    Addresses are typically expected to be 4-byte aligned.
    """
    ...


# Reset cause constants
PWRON_RESET: int
"""Power-on reset.

Indicates the last reset was caused by power-on, or a reset source treated as
equivalent to a power-on reset on this port.
"""

WDT_RESET: int
"""Watchdog timer reset.

Indicates the last reset was caused by the watchdog timer.
"""


class Pin:
    """
    GPIO pin control for digital I/O operations.

    Supports input/output modes, optional pull resistors, and (on many ports)
    edge-triggered interrupts.

    Pin numbering and which pins are valid is board specific. Some ports accept
    additional identifiers (e.g. strings like ``"LED"``), but this stub types
    the id as an integer.
    """

    # Mode constants
    IN: int
    """Input mode."""

    OUT: int
    """Output mode."""

    OPEN_DRAIN: int
    """Open-drain output mode."""

    ALT: int
    """Alternate function mode."""

    # Pull constants
    PULL_UP: int
    """Enable internal pull-up resistor."""

    PULL_DOWN: int
    """Enable internal pull-down resistor."""

    # IRQ trigger constants
    IRQ_RISING: int
    """Trigger on rising edge."""

    IRQ_FALLING: int
    """Trigger on falling edge."""

    def __init__(self, id: int, mode: int = -1, pull: int = -1,
                 *, value: int = None, alt: int = -1) -> None:
        """
        Create a new Pin object.

        The special value ``-1`` for ``mode``/``pull``/``alt`` typically means
        "leave unchanged" or "use a default" (port specific).

        :param id: GPIO pin number
        :param mode: Pin.IN, Pin.OUT, Pin.OPEN_DRAIN, or Pin.ALT
        :param pull: Pin.PULL_UP, Pin.PULL_DOWN, or None
        :param value: Initial output value (0 or 1)
        :param alt: Alternate function number

        Example
        -------
        ```python
            >>> from machine import Pin
            >>> 
            >>> # Output pin
            >>> led = Pin(25, Pin.OUT)
            >>> 
            >>> # Input with pull-up
            >>> btn = Pin(14, Pin.IN, Pin.PULL_UP)
            >>> 
            >>> # Output with initial value
            >>> relay = Pin(16, Pin.OUT, value=0)
        ```
        """
        ...

    def init(self, mode: int = -1, pull: int = -1,
             *, value: int = None, alt: int = -1) -> None:
        """
        Reinitialize the pin with new parameters.

        This is equivalent to re-creating the pin object, but can be more
        convenient when reusing the same ``Pin`` instance.

        :param mode: Pin.IN, Pin.OUT, Pin.OPEN_DRAIN, or Pin.ALT
        :param pull: Pin.PULL_UP, Pin.PULL_DOWN, or None
        :param value: Initial output value
        :param alt: Alternate function number

        Example
        -------
        ```python
            >>> pin = Pin(25)
            >>> pin.init(Pin.OUT)
            >>> pin.init(Pin.IN, Pin.PULL_UP)
        ```
        """
        ...

    def value(self, x: Optional[int] = None) -> Optional[int]:
        """
        Get or set the pin value.

        Reading returns ``0`` or ``1``. When setting, values are treated as
        boolean (typically ``0`` for low, non-zero for high).

        :param x: If provided, set pin to this value (0 or 1)

        :returns: Current pin value if no argument, None if setting

        Example
        -------
        ```python
            >>> pin = Pin(25, Pin.OUT)
            >>> pin.value(1)      # Set high
            >>> pin.value(0)      # Set low
            >>> v = pin.value()   # Read current value
        ```
        """
        ...

    def __call__(self, x: Optional[int] = None) -> Optional[int]:
        """
        Shorthand for value().

        :param x: If provided, set pin to this value

        :returns: Current pin value if no argument

        Example
        -------
        ```python
            >>> pin = Pin(25, Pin.OUT)
            >>> pin(1)    # Same as pin.value(1)
            >>> pin()     # Same as pin.value()
        ```
        """
        ...

    def on(self) -> None:
        """
        Set the pin to high (1).

        The pin must be configured for output for this to have an effect.

        Example
        -------
        ```python
            >>> led = Pin(25, Pin.OUT)
            >>> led.on()
        ```
        """
        ...

    def off(self) -> None:
        """
        Set the pin to low (0).

        The pin must be configured for output for this to have an effect.

        Example
        -------
        ```python
            >>> led = Pin(25, Pin.OUT)
            >>> led.off()
        ```
        """
        ...

    def toggle(self) -> None:
        """
        Toggle the pin value.

        Example
        -------
        ```python
            >>> led = Pin(25, Pin.OUT)
            >>> led.toggle()  # If off, turn on; if on, turn off
        ```
        """
        ...

    def high(self) -> None:
        """
        Set the pin to high (1). Alias for on().

        Example
        -------
        ```python
            >>> led = Pin(25, Pin.OUT)
            >>> led.high()
        ```
        """
        ...

    def low(self) -> None:
        """
        Set the pin to low (0). Alias for off().

        Example
        -------
        ```python
            >>> led = Pin(25, Pin.OUT)
            >>> led.low()
        ```
        """
        ...

    def irq(self, handler: Optional[_IRQHandler] = None,
            trigger: int = 0,
            *, wake: int = None, hard: bool = False) -> Optional[_IRQHandler]:
        """
        Configure an interrupt handler for the pin.

        Calling ``irq()`` registers (or updates) an IRQ handler that will be
        invoked when the configured edge(s) occur.

        If ``hard`` is True, the handler may run in a hard-interrupt context.
        In that case, avoid allocating memory and avoid operations that may
        allocate (e.g. building strings, printing, appending to lists). Use
        ``micropython.schedule()`` if you need to defer work to the VM.

        :param handler: Function called on interrupt, receives Pin as argument
        :param trigger: Pin.IRQ_RISING, Pin.IRQ_FALLING, or both (OR'd)
        :param wake: Power mode to wake from (board specific)
        :param hard: If True, use hard interrupt (no memory allocation)

        :returns: Previous handler if getting, None if setting

        Example
        -------
        ```python
            >>> def on_press(pin):
            ...     print(f"Pin {pin} triggered!")
            >>> 
            >>> btn = Pin(14, Pin.IN, Pin.PULL_UP)
            >>> btn.irq(trigger=Pin.IRQ_FALLING, handler=on_press)
            >>> 
            >>> # Disable interrupt
            >>> btn.irq(handler=None)
        ```
        """
        ...


class PWM:
    """
    Pulse Width Modulation control.

    Used for LED dimming, motor control, servo control, and tone generation.

    The exact resolution and supported frequencies depend on the port and the
    underlying hardware PWM implementation.
    """

    def __init__(self, pin: Pin, *, freq: int = 0, duty_u16: int = 0,
                 duty_ns: int = 0, invert: bool = False) -> None:
        """
        Create a PWM object on a pin.

        :param pin: Pin object to use for PWM
        :param freq: PWM frequency in Hz
        :param duty_u16: Duty cycle as 16-bit value (0-65535)
        :param duty_ns: Duty cycle in nanoseconds
        :param invert: Invert the PWM output

        Example
        -------
        ```python
            >>> from machine import Pin, PWM
            >>> 
            >>> pwm = PWM(Pin(15))
            >>> pwm.freq(1000)
            >>> pwm.duty_u16(32768)  # 50% duty
            >>> 
            >>> # Or initialize with parameters
            >>> pwm = PWM(Pin(15), freq=1000, duty_u16=32768)
        ```
        """
        ...

    def freq(self, value: Optional[int] = None) -> Optional[int]:
        """
        Get or set the PWM frequency.

        Note that changing the frequency may affect duty-cycle resolution on
        some ports.

        :param value: New frequency in Hz (if setting)

        :returns: Current frequency if no argument

        Example
        -------
        ```python
            >>> pwm.freq(1000)      # Set to 1kHz
            >>> print(pwm.freq())   # Get current frequency
        ```
        """
        ...

    def duty_u16(self, value: Optional[int] = None) -> Optional[int]:
        """
        Get or set duty cycle as 16-bit value.

        ``0`` means always off and ``65535`` means always on. Intermediate
        values approximate the requested duty cycle.

        :param value: Duty cycle 0-65535 (0=0%, 65535=100%)

        :returns: Current duty cycle if no argument

        Example
        -------
        ```python
            >>> pwm.duty_u16(0)       # 0% duty (off)
            >>> pwm.duty_u16(32768)   # 50% duty
            >>> pwm.duty_u16(65535)   # 100% duty (full on)
        ```
        """
        ...

    def duty_ns(self, value: Optional[int] = None) -> Optional[int]:
        """
        Get or set duty cycle in nanoseconds.

        Not all ports support ``duty_ns()``. Where supported, it can be useful
        for applications where absolute pulse width matters.

        :param value: Duty cycle in nanoseconds

        :returns: Current duty cycle in ns if no argument

        Example
        -------
        ```python
            >>> pwm.freq(1000)         # 1kHz = 1ms period
            >>> pwm.duty_ns(500000)    # 500us = 50% duty
        ```
        """
        ...

    def deinit(self) -> None:
        """
        Disable the PWM output.

        Example
        -------
        ```python
            >>> pwm.deinit()
        ```
        """
        ...


class ADC:
    """
    Analog to Digital Converter.

    Read analog voltage levels from GPIO pins.

    Supported pins, reference voltage, and calibration are port specific.
    """

    CORE_TEMP: int
    """Internal temperature sensor channel."""

    def __init__(self, pin: Union[Pin, int]) -> None:
        """
        Create an ADC object.

        :param pin: Pin object or GPIO number (26-29 on RP2)

        Example
        -------
        ```python
            >>> from machine import ADC, Pin
            >>> 
            >>> adc = ADC(Pin(26))
            >>> # Or using pin number
            >>> adc = ADC(26)
            >>> 
            >>> # Read internal temperature
            >>> temp_adc = ADC(ADC.CORE_TEMP)
        ```
        """
        ...

    def read_u16(self) -> int:
        """
        Read analog value as 16-bit unsigned integer.

        The returned value is scaled to the full 16-bit range (0..65535), even
        if the underlying ADC has fewer bits of resolution.

        :returns: ADC value 0-65535

        Example
        -------
        ```python
            >>> adc = ADC(26)
            >>> value = adc.read_u16()
            >>> voltage = value * 3.3 / 65535
            >>> print(f"Voltage: {voltage:.2f}V")
        ```
        """
        ...

    def read_uv(self) -> int:
        """
        Read analog value in microvolts.

        Availability and accuracy of microvolt readings are port specific.

        :returns: Voltage in microvolts

        Example
        -------
        ```python
            >>> adc = ADC(26)
            >>> uv = adc.read_uv()
            >>> voltage = uv / 1_000_000
            >>> print(f"Voltage: {voltage:.3f}V")
        ```
        """
        ...


class I2C:
    """
    I2C bus protocol for communicating with sensors and devices.

    Hardware I2C implementation using dedicated I2C peripherals.

    Addresses are typically 7-bit device addresses. Most methods raise
    ``OSError`` on bus errors or if a device does not respond.
    """

    def __init__(self, id: int, *, scl: Pin = None, sda: Pin = None,
                 freq: int = 400000) -> None:
        """
        Create an I2C object.

        :param id: I2C peripheral ID (0 or 1 on RP2)
        :param scl: Pin object for SCL (clock)
        :param sda: Pin object for SDA (data)
        :param freq: Clock frequency in Hz (default 400kHz)

        Example
        -------
        ```python
            >>> from machine import I2C, Pin
            >>> 
            >>> i2c = I2C(0, scl=Pin(1), sda=Pin(0), freq=400000)
            >>> 
            >>> # Scan for devices
            >>> devices = i2c.scan()
            >>> print([hex(d) for d in devices])
        ```
        """
        ...

    def scan(self) -> list[int]:
        """
        Scan for I2C devices on the bus.

        Returns a list of responding 7-bit addresses.

        :returns: List of addresses that responded

        Example
        -------
        ```python
            >>> devices = i2c.scan()
            >>> print(f"Found {len(devices)} devices")
            >>> for addr in devices:
            ...     print(f"  0x{addr:02x}")
        ```
        """
        ...

    def readfrom(self, addr: int, nbytes: int, stop: bool = True) -> bytes:
        """
        Read bytes from a device.

        If ``stop`` is False, the bus is left active (no STOP condition). This
        is useful for combined transactions on some devices.

        :param addr: I2C device address
        :param nbytes: Number of bytes to read
        :param stop: Generate STOP condition

        :returns: Bytes read from device

        Example
        -------
        ```python
            >>> data = i2c.readfrom(0x68, 7)
        ```
        """
        ...

    def readfrom_into(self, addr: int, buf: bytearray, stop: bool = True) -> None:
        """
        Read bytes from a device into a buffer.

        :param addr: I2C device address
        :param buf: Buffer to read into
        :param stop: Generate STOP condition

        Example
        -------
        ```python
            >>> buf = bytearray(7)
            >>> i2c.readfrom_into(0x68, buf)
        ```
        """
        ...

    def writeto(self, addr: int, buf: bytes, stop: bool = True) -> int:
        """
        Write bytes to a device.

        Returns the number of bytes written if successful.

        :param addr: I2C device address
        :param buf: Bytes to write
        :param stop: Generate STOP condition

        :returns: Number of bytes written

        Example
        -------
        ```python
            >>> i2c.writeto(0x68, b'\\x00')
        ```
        """
        ...

    def readfrom_mem(self, addr: int, memaddr: int, nbytes: int,
                     *, addrsize: int = 8) -> bytes:
        """
        Read from a device's memory/register.

        This performs a write of ``memaddr`` followed by a read of ``nbytes``.
        Many sensors expose registers this way.

        :param addr: I2C device address
        :param memaddr: Memory/register address
        :param nbytes: Number of bytes to read
        :param addrsize: Memory address size in bits (8 or 16)

        :returns: Bytes read from device

        Example
        -------
        ```python
            >>> # Read 6 bytes from register 0x3B
            >>> data = i2c.readfrom_mem(0x68, 0x3B, 6)
        ```
        """
        ...

    def readfrom_mem_into(self, addr: int, memaddr: int, buf: bytearray,
                          *, addrsize: int = 8) -> None:
        """
        Read from device memory into buffer.

        :param addr: I2C device address
        :param memaddr: Memory/register address
        :param buf: Buffer to read into
        :param addrsize: Memory address size in bits

        Example
        -------
        ```python
            >>> buf = bytearray(6)
            >>> i2c.readfrom_mem_into(0x68, 0x3B, buf)
        ```
        """
        ...

    def writeto_mem(self, addr: int, memaddr: int, buf: bytes,
                    *, addrsize: int = 8) -> None:
        """
        Write to a device's memory/register.

        :param addr: I2C device address
        :param memaddr: Memory/register address
        :param buf: Bytes to write
        :param addrsize: Memory address size in bits

        Example
        -------
        ```python
            >>> # Write 0x00 to register 0x6B
            >>> i2c.writeto_mem(0x68, 0x6B, b'\\x00')
        ```
        """
        ...

    def deinit(self) -> None:
        """
        Disable the I2C bus.

        Example
        -------
        ```python
            >>> i2c.deinit()
        ```
        """
        ...


class SoftI2C(I2C):
    """
    Software I2C implementation using bit-banging.

    Can use any GPIO pins but slower than hardware I2C.

    Because it is implemented in software, timing is more sensitive to system
    load and interrupt latency.
    """

    def __init__(self, scl: Pin, sda: Pin, *, freq: int = 400000,
                 timeout: int = 50000) -> None:
        """
        Create a software I2C object.

        :param scl: Pin object for SCL
        :param sda: Pin object for SDA
        :param freq: Clock frequency in Hz
        :param timeout: Timeout in microseconds

        Example
        -------
        ```python
            >>> from machine import SoftI2C, Pin
            >>> 
            >>> i2c = SoftI2C(scl=Pin(5), sda=Pin(4), freq=100000)
        ```
        """
        ...


class SPI:
    """
    SPI bus protocol for high-speed communication.

    Hardware SPI implementation using dedicated SPI peripherals.

    SPI mode is configured using ``polarity`` (CPOL) and ``phase`` (CPHA).
    """

    MSB: int
    """Most significant bit first."""

    LSB: int
    """Least significant bit first."""

    def __init__(self, id: int, baudrate: int = 1000000, *, polarity: int = 0,
                 phase: int = 0, bits: int = 8, firstbit: int = MSB,
                 sck: Pin = None, mosi: Pin = None, miso: Pin = None) -> None:
        """
        Create an SPI object.

        :param id: SPI peripheral ID (0 or 1 on RP2)
        :param baudrate: Clock speed in Hz
        :param polarity: Clock polarity (0 or 1)
        :param phase: Clock phase (0 or 1)
        :param bits: Bits per transfer (usually 8)
        :param firstbit: SPI.MSB or SPI.LSB
        :param sck: Pin for clock
        :param mosi: Pin for MOSI (Master Out Slave In)
        :param miso: Pin for MISO (Master In Slave Out)

        Example
        -------
        ```python
            >>> from machine import SPI, Pin
            >>> 
            >>> spi = SPI(0, baudrate=1000000, polarity=0, phase=0,
            ...           sck=Pin(2), mosi=Pin(3), miso=Pin(4))
        ```
        """
        ...

    def init(self, baudrate: int = 1000000, *, polarity: int = 0,
             phase: int = 0, bits: int = 8, firstbit: int = 0) -> None:
        """
        Reinitialize SPI with new parameters.

        :param baudrate: Clock speed in Hz
        :param polarity: Clock polarity
        :param phase: Clock phase
        :param bits: Bits per transfer
        :param firstbit: Bit order

        Example
        -------
        ```python
            >>> spi.init(baudrate=2000000, polarity=1, phase=1)
        ```
        """
        ...

    def read(self, nbytes: int, write: int = 0x00) -> bytes:
        """
        Read bytes while writing a constant value.

        SPI is full-duplex: while reading, the controller must transmit
        something. The ``write`` value specifies the byte sent for each byte
        received.

        :param nbytes: Number of bytes to read
        :param write: Byte value to write during read

        :returns: Bytes read

        Example
        -------
        ```python
            >>> data = spi.read(10)
            >>> data = spi.read(10, write=0xFF)
        ```
        """
        ...

    def readinto(self, buf: bytearray, write: int = 0x00) -> None:
        """
        Read bytes into a buffer.

        :param buf: Buffer to read into
        :param write: Byte value to write during read

        Example
        -------
        ```python
            >>> buf = bytearray(10)
            >>> spi.readinto(buf)
        ```
        """
        ...

    def write(self, buf: bytes) -> None:
        """
        Write bytes.

        :param buf: Bytes to write

        Example
        -------
        ```python
            >>> spi.write(b'\\x01\\x02\\x03')
        ```
        """
        ...

    def write_readinto(self, write_buf: bytes, read_buf: bytearray) -> None:
        """
        Write and read simultaneously.

        :param write_buf: Bytes to write
        :param read_buf: Buffer for read data

        Example
        -------
        ```python
            >>> write_buf = b'\\x00\\x00\\x00'
            >>> read_buf = bytearray(3)
            >>> spi.write_readinto(write_buf, read_buf)
        ```
        """
        ...

    def deinit(self) -> None:
        """
        Disable the SPI bus.

        Example
        -------
        ```python
            >>> spi.deinit()
        ```
        """
        ...


class SoftSPI(SPI):
    """
    Software SPI implementation using bit-banging.

    Can use any GPIO pins but slower than hardware SPI.

    Suitable for low-speed peripherals or when hardware SPI pins are not
    available.
    """

    def __init__(self, baudrate: int = 500000, *, polarity: int = 0,
                 phase: int = 0, bits: int = 8, firstbit: int = SPI.MSB,
                 sck: Pin = None, mosi: Pin = None, miso: Pin = None) -> None:
        """
        Create a software SPI object.

        :param baudrate: Clock speed in Hz
        :param polarity: Clock polarity
        :param phase: Clock phase
        :param bits: Bits per transfer
        :param firstbit: Bit order
        :param sck: Pin for clock
        :param mosi: Pin for MOSI
        :param miso: Pin for MISO

        Example
        -------
        ```python
            >>> from machine import SoftSPI, Pin
            >>> 
            >>> spi = SoftSPI(baudrate=100000, sck=Pin(10), 
            ...               mosi=Pin(11), miso=Pin(12))
        ```
        """
        ...


class UART:
    """
    UART/Serial communication interface.

    Provides asynchronous serial communication.

    Read methods may return ``None`` on timeout. Many ports raise ``OSError``
    for invalid configuration or I/O errors.
    """

    def __init__(self, id: int, baudrate: int = 9600, bits: int = 8,
                 parity: int = None, stop: int = 1, *,
                 tx: Pin = None, rx: Pin = None,
                 txbuf: int = 256, rxbuf: int = 256,
                 timeout: int = 0, timeout_char: int = 2,
                 flow: int = 0) -> None:
        """
        Create a UART object.

        :param id: UART peripheral ID (0 or 1 on RP2)
        :param baudrate: Baud rate
        :param bits: Data bits (5-8)
        :param parity: Parity (None, 0=even, 1=odd)
        :param stop: Stop bits (1 or 2)
        :param tx: Pin for TX
        :param rx: Pin for RX
        :param txbuf: TX buffer size
        :param rxbuf: RX buffer size
        :param timeout: Read timeout in ms
        :param timeout_char: Inter-character timeout in ms
        :param flow: Flow control (0=none)

        Example
        -------
        ```python
            >>> from machine import UART, Pin
            >>> 
            >>> uart = UART(0, baudrate=115200, tx=Pin(0), rx=Pin(1))
        ```
        """
        ...

    def init(self, baudrate: int = 9600, bits: int = 8, parity: int = None,
             stop: int = 1, **kwargs) -> None:
        """
        Reinitialize UART with new parameters.

        :param baudrate: Baud rate
        :param bits: Data bits
        :param parity: Parity
        :param stop: Stop bits

        Example
        -------
        ```python
            >>> uart.init(baudrate=9600)
        ```
        """
        ...

    def any(self) -> int:
        """
        Check if data is available to read.

        This returns the number of bytes currently available in the receive
        buffer.

        :returns: Number of bytes available

        Example
        -------
        ```python
            >>> if uart.any():
            ...     data = uart.read()
        ```
        """
        ...

    def read(self, nbytes: int = -1) -> Optional[bytes]:
        """
        Read bytes from UART.

        If ``nbytes`` is ``-1``, reads as many bytes as are available (subject
        to buffering and timeout behavior of the port).

        :param nbytes: Max bytes to read (-1 for all available)

        :returns: Bytes read or None if timeout

        Example
        -------
        ```python
            >>> data = uart.read(10)
            >>> data = uart.read()  # Read all available
        ```
        """
        ...

    def readline(self) -> Optional[bytes]:
        """
        Read a line ending with newline.

        :returns: Line including newline, or None if timeout

        Example
        -------
        ```python
            >>> line = uart.readline()
            >>> if line:
            ...     print(line.decode())
        ```
        """
        ...

    def readinto(self, buf: bytearray) -> Optional[int]:
        """
        Read bytes into a buffer.

        :param buf: Buffer to read into

        :returns: Number of bytes read or None if timeout

        Example
        -------
        ```python
            >>> buf = bytearray(10)
            >>> n = uart.readinto(buf)
        ```
        """
        ...

    def write(self, buf: bytes) -> Optional[int]:
        """
        Write bytes to UART.

        Returns the number of bytes accepted for transmission, or ``None`` on
        timeout (port dependent).

        :param buf: Bytes to write

        :returns: Number of bytes written or None if timeout

        Example
        -------
        ```python
            >>> uart.write(b'Hello\\n')
            >>> uart.write('Hello\\n'.encode())
        ```
        """
        ...

    def sendbreak(self) -> None:
        """
        Send a break condition.

        Example
        -------
        ```python
            >>> uart.sendbreak()
        ```
        """
        ...

    def flush(self) -> None:
        """
        Wait until all data is sent.

        Example
        -------
        ```python
            >>> uart.write(b'data')
            >>> uart.flush()  # Wait for transmission
        ```
        """
        ...

    def txdone(self) -> bool:
        """
        Check if transmission is complete.

        :returns: True if all data sent

        Example
        -------
        ```python
            >>> while not uart.txdone():
            ...     pass
        ```
        """
        ...

    def deinit(self) -> None:
        """
        Disable the UART.

        Example
        -------
        ```python
            >>> uart.deinit()
        ```
        """
        ...


class Timer:
    """
    Hardware timer for periodic callbacks.

    Provides precise timing and periodic function execution.

    Timer callbacks are often executed in interrupt context. Keep callbacks
    short and avoid memory allocation where required by the port.
    """

    ONE_SHOT: int
    """Timer fires once."""

    PERIODIC: int
    """Timer fires repeatedly."""

    def __init__(self, id: int = -1, *, mode: int = PERIODIC,
                 period: int = -1, freq: float = -1,
                 callback: Callable[["Timer"], None] = None) -> None:
        """
        Create a Timer object.

        :param id: Timer ID (-1 for virtual timer)
        :param mode: Timer.ONE_SHOT or Timer.PERIODIC
        :param period: Period in milliseconds
        :param freq: Frequency in Hz (alternative to period)
        :param callback: Function called on timer event

        Example
        -------
        ```python
            >>> from machine import Timer
            >>> 
            >>> def tick(t):
            ...     print("Timer fired!")
            >>> 
            >>> timer = Timer(period=1000, callback=tick)
            >>> 
            >>> # Using frequency
            >>> timer = Timer(freq=10, callback=tick)  # 10 Hz
        ```
        """
        ...

    def init(self, *, mode: int = PERIODIC, period: int = -1,
             freq: float = -1,
             callback: Callable[["Timer"], None] = None) -> None:
        """
        Reinitialize the timer.

        :param mode: Timer.ONE_SHOT or Timer.PERIODIC
        :param period: Period in milliseconds
        :param freq: Frequency in Hz
        :param callback: Function called on timer event

        Example
        -------
        ```python
            >>> timer.init(period=500, callback=my_callback)
        ```
        """
        ...

    def deinit(self) -> None:
        """
        Disable the timer.

        Example
        -------
        ```python
            >>> timer.deinit()
        ```
        """
        ...


class RTC:
    """
    Real-Time Clock for date and time keeping.

    Maintains current date and time, optionally with battery backup.

    Support for battery-backed timekeeping and the resolution/meaning of the
    ``subseconds`` field is port dependent.
    """

    def __init__(self) -> None:
        """
        Create an RTC object.

        Example
        -------
        ```python
            >>> from machine import RTC
            >>> 
            >>> rtc = RTC()
            >>> rtc.datetime((2026, 1, 7, 1, 12, 0, 0, 0))
        ```
        """
        ...

    def datetime(self, dt: tuple = None) -> tuple:
        """
        Get or set the date and time.

        The datetime tuple is ``(year, month, day, weekday, hours, minutes,
        seconds, subseconds)``. The meaning/range of ``weekday`` and
        ``subseconds`` can vary by port.

        :param dt: Tuple (year, month, day, weekday, hours, minutes, seconds, subseconds)

        :returns: Current datetime tuple if no argument

        Example
        -------
        ```python
            >>> rtc = RTC()
            >>> 
            >>> # Set datetime
            >>> rtc.datetime((2026, 1, 7, 1, 12, 0, 0, 0))
            >>> 
            >>> # Get datetime
            >>> dt = rtc.datetime()
            >>> year, month, day, wd, h, m, s, ss = dt
        ```
        """
        ...


class WDT:
    """
    Watchdog Timer for system recovery.

    Resets the system if not fed within timeout period.

    On many ports the watchdog cannot be disabled once started.
    """

    def __init__(self, timeout: int = 5000) -> None:
        """
        Create and start a watchdog timer.

        :param timeout: Timeout in milliseconds before reset

        Example
        -------
        ```python
            >>> from machine import WDT
            >>> 
            >>> wdt = WDT(timeout=5000)  # 5 second timeout
            >>> 
            >>> while True:
            ...     # Do work
            ...     wdt.feed()  # Reset watchdog
        ```
        """
        ...

    def feed(self) -> None:
        """
        Feed the watchdog to prevent reset.

        Example
        -------
        ```python
            >>> wdt.feed()
        ```
        """
        ...


class Signal:
    """
    Signal abstraction over Pin with inversion support.

    Provides a consistent interface regardless of active-high/low logic.

    This is handy for working with active-low LEDs, chip-select lines, and
    similar signals while keeping application logic consistent.
    """

    def __init__(self, pin: Pin, *, invert: bool = False) -> None:
        """
        Create a Signal object.

        :param pin: Underlying Pin object
        :param invert: If True, logic is inverted

        Example
        -------
        ```python
            >>> from machine import Pin, Signal
            >>> 
            >>> # Active-low LED (common on many boards)
            >>> led = Signal(Pin(25, Pin.OUT), invert=True)
            >>> led.on()   # Actually sets pin LOW
        ```
        """
        ...

    def value(self, x: Optional[int] = None) -> Optional[int]:
        """
        Get or set the signal value (with inversion applied).

        :param x: If provided, set signal to this value (0 or 1)

        :returns: Current signal value if no argument

        Example
        -------
        ```python
            >>> led.value(1)  # Turn on (respects inversion)
        ```
        """
        ...

    def on(self) -> None:
        """
        Set the signal active.

        Example
        -------
        ```python
            >>> led.on()
        ```
        """
        ...

    def off(self) -> None:
        """
        Set the signal inactive.

        Example
        -------
        ```python
            >>> led.off()
        ```
        """
        ...
