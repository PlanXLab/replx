"""
Network configuration.

Configure WiFi and Ethernet interfaces.

This module is highly port/board dependent: available interfaces, supported
security modes, and configuration options can vary.

Example
-------
```python
    >>> import network
    >>> 
    >>> # Connect to WiFi
    >>> wlan = network.WLAN(network.STA_IF)
    >>> wlan.active(True)
    >>> wlan.connect('MySSID', 'password')
    >>> 
    >>> # Wait for connection
    >>> while not wlan.isconnected():
    ...     pass
    >>> print(wlan.ifconfig())
```
"""

from typing import Optional, Tuple, Union


# Interface modes
STA_IF: int
"""Station (client) interface."""

AP_IF: int
"""Access point interface."""

# Authentication modes
AUTH_OPEN: int
"""Open (no authentication)."""

AUTH_WEP: int
"""WEP authentication."""

AUTH_WPA_PSK: int
"""WPA-PSK authentication."""

AUTH_WPA2_PSK: int
"""WPA2-PSK authentication."""

AUTH_WPA_WPA2_PSK: int
"""WPA/WPA2-PSK mixed."""

# Status values
STAT_IDLE: int
"""Idle, not connected."""

STAT_CONNECTING: int
"""Connecting."""

STAT_WRONG_PASSWORD: int
"""Wrong password."""

STAT_NO_AP_FOUND: int
"""Access point not found."""

STAT_CONNECT_FAIL: int
"""Connection failed."""

STAT_GOT_IP: int
"""Connected, got IP."""


class WLAN:
    """
    WiFi interface controller.

    The WLAN interface is typically used in one of two modes:

    - Station mode (``STA_IF``): connect to an existing access point.
    - Access point mode (``AP_IF``): create an access point for other clients.

    Many operations may raise ``OSError`` if the underlying driver reports an
    error (e.g. authentication failure, unsupported configuration, etc.).

    Example
    -------
    ```python
        >>> import network
        >>> 
        >>> # Station mode (connect to AP)
        >>> wlan = network.WLAN(network.STA_IF)
        >>> wlan.active(True)
        >>> wlan.connect('MyNetwork', 'password')
        >>> 
        >>> while not wlan.isconnected():
        ...     pass
        >>> 
        >>> ip, subnet, gateway, dns = wlan.ifconfig()
        >>> print(f"IP: {ip}")
    ```
    """

    def __init__(self, interface_id: int) -> None:
        """
        Create WLAN interface.

        The returned object is usually a singleton per interface id.

        :param interface_id: STA_IF or AP_IF

        Example
        -------
        ```python
            >>> import network
            >>> 
            >>> sta = network.WLAN(network.STA_IF)
            >>> ap = network.WLAN(network.AP_IF)
        ```
        """
        ...

    def active(self, is_active: bool = None) -> Optional[bool]:
        """
        Get or set interface active state.

        An interface generally needs to be active before scanning, connecting,
        or creating an access point.

        :param is_active: True to activate (None to query)

        :returns: Current state if querying

        Example
        -------
        ```python
            >>> import network
            >>> 
            >>> wlan = network.WLAN(network.STA_IF)
            >>> wlan.active(True)   # Activate
            >>> wlan.active()       # Query: True
        ```
        """
        ...

    def connect(self, ssid: str, key: str = None, *, bssid: bytes = None) -> None:
        """
        Connect to WiFi network.

        Connection is usually asynchronous: after calling ``connect()``, poll
        ``isconnected()`` or check ``status()`` until the interface reports it
        is connected.

        :param ssid: Network name
        :param key: Password (None for open)
        :param bssid: Specific AP MAC address

        Example
        -------
        ```python
            >>> import network
            >>> 
            >>> wlan = network.WLAN(network.STA_IF)
            >>> wlan.active(True)
            >>> wlan.connect('MyNetwork', 'password123')
        ```
        """
        ...

    def disconnect(self) -> None:
        """
        Disconnect from network.

        This requests disconnection; depending on the port it may take a short
        time for the interface to actually drop the link.

        Example
        -------
        ```python
            >>> import network
            >>> 
            >>> wlan.disconnect()
        ```
        """
        ...

    def isconnected(self) -> bool:
        """
        Check connection status.

        In station mode this typically indicates both link + IP configuration.

        :returns: True if connected with IP

        Example
        -------
        ```python
            >>> import network
            >>> 
            >>> if wlan.isconnected():
            ...     print("Connected!")
        ```
        """
        ...

    def status(self, param: str = None) -> Union[int, str]:
        """
        Get connection status.

        Without arguments, returns a numeric status (often one of the
        ``STAT_*`` constants). Some ports support string parameters such as
        ``'rssi'``.

        :param param: Specific parameter (None for general status)

        :returns: Status value or parameter

        Example
        -------
        ```python
            >>> import network
            >>> 
            >>> status = wlan.status()
            >>> if status == network.STAT_GOT_IP:
            ...     print("Connected")
            >>> 
            >>> rssi = wlan.status('rssi')
            >>> print(f"Signal: {rssi} dBm")
        ```
        """
        ...

    def ifconfig(self, config: Tuple[str, str, str, str] = None) -> Optional[Tuple[str, str, str, str]]:
        """
        Get or set IP configuration.

        With no argument, returns a 4-tuple ``(ip, netmask, gateway, dns)``.
        Passing a tuple configures a static IP configuration.

        :param config: (ip, subnet, gateway, dns) tuple

        :returns: Current config if querying

        Example
        -------
        ```python
            >>> import network
            >>> 
            >>> # Get config
            >>> ip, subnet, gw, dns = wlan.ifconfig()
            >>> print(f"IP: {ip}")
            >>> 
            >>> # Set static IP
            >>> wlan.ifconfig(('192.168.1.100', '255.255.255.0', 
            ...                '192.168.1.1', '8.8.8.8'))
        ```
        """
        ...

    def config(self, **kwargs) -> Union[str, bytes, int, None]:
        """
        Get or set configuration parameters.

        Parameters: mac, ssid, channel, hidden, password,
                   authmode, hostname, txpower, pm

        Supported keys vary by port and by interface mode (STA vs AP). When
        requesting a value, some ports use ``wlan.config('param')``.

        :param kwargs: Parameters to set

        :returns: Parameter value if getting

        Example
        -------
        ```python
            >>> import network
            >>> 
            >>> # Get MAC address
            >>> mac = wlan.config('mac')
            >>> 
            >>> # Set hostname
            >>> wlan.config(hostname='my-pico')
            >>> 
            >>> # AP mode config
            >>> ap = network.WLAN(network.AP_IF)
            >>> ap.config(ssid='MyAP', password='12345678')
        ```
        """
        ...

    def scan(self) -> list[Tuple[bytes, bytes, int, int, int, bool]]:
        """
        Scan for WiFi networks.

        Returns a list of tuples containing:
        ``(ssid, bssid, channel, rssi, authmode, hidden)``.

        The SSID is returned as bytes; decode with ``ssid.decode()``.

        :returns: List of (ssid, bssid, channel, rssi, authmode, hidden)

        Example
        -------
        ```python
            >>> import network
            >>> 
            >>> wlan = network.WLAN(network.STA_IF)
            >>> wlan.active(True)
            >>> 
            >>> for ssid, bssid, ch, rssi, auth, hidden in wlan.scan():
            ...     print(f"{ssid.decode()}: {rssi}dBm")
        ```
        """
        ...


class LAN:
    """
    Ethernet LAN interface (if available).

    Availability and constructor parameters are port dependent.

    Example
    -------
    ```python
        >>> import network
        >>> 
        >>> lan = network.LAN()
        >>> lan.active(True)
        >>> 
        >>> while not lan.isconnected():
        ...     pass
        >>> print(lan.ifconfig())
    ```
    """

    def __init__(
        self,
        *,
        mdc: int = None,
        mdio: int = None,
        phy_type: int = None,
        phy_addr: int = None
    ) -> None:
        """
        Create LAN interface.

        :param mdc: MDC pin
        :param mdio: MDIO pin
        :param phy_type: PHY type
        :param phy_addr: PHY address

        Example
        -------
        ```python
            >>> import network
            >>> 
            >>> lan = network.LAN()
        ```
        """
        ...

    def active(self, is_active: bool = None) -> Optional[bool]:
        """Get or set active state.

        Activate the interface before attempting to use it.
        """
        ...

    def isconnected(self) -> bool:
        """Check if connected.

        Returns True if the link is up and the interface has network
        connectivity (exact semantics are port dependent).
        """
        ...

    def ifconfig(self, config: Tuple[str, str, str, str] = None) -> Optional[Tuple[str, str, str, str]]:
        """Get or set IP configuration.

        With no argument, returns ``(ip, netmask, gateway, dns)``.
        With a tuple argument, sets a static IP configuration.
        """
        ...

    def config(self, **kwargs) -> Union[str, bytes, int, None]:
        """Get or set parameters.

        Supported parameters vary by port.
        """
        ...


def hostname(name: str = None) -> Optional[str]:
    """
    Get or set system hostname.

    The hostname is used by some network stacks for DHCP and/or mDNS.

    :param name: New hostname (None to query)

    :returns: Current hostname if querying

    Example
    -------
    ```python
        >>> import network
        >>> 
        >>> network.hostname('my-pico')
        >>> print(network.hostname())  # 'my-pico'
    ```
    """
    ...


def country(code: str = None) -> Optional[str]:
    """
    Get or set WiFi country code.

    The country code affects regulatory settings such as allowed channels and
    transmit power. Support is port dependent.

    :param code: 2-letter country code

    :returns: Current code if querying

    Example
    -------
    ```python
        >>> import network
        >>> 
        >>> network.country('US')
        >>> network.country('KR')
    ```
    """
    ...
