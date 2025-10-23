"""
OpenOCD Master for JTAG/SWD debugging
"""

import socket

from . import MasterBase


class OpenOCDMaster(MasterBase):
    """
    Master interface using OpenOCD TCL server

    Connects to OpenOCD's TCL interface for reading/writing memory
    """

    def __init__(self, host: str = "localhost", port: int = 6666, timeout: float = 5.0) -> None:
        """
        Initialize OpenOCD connection

        Args:
            host: OpenOCD server host
            port: OpenOCD TCL server port (default 6666)
            timeout: Socket timeout in seconds
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.socket: socket.socket | None = None
        self._connect()

    def _connect(self) -> None:
        """Establish connection to OpenOCD"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)
            self.socket.connect((self.host, self.port))
            # Read the welcome message
            self._recv()
        except Exception as e:
            raise RuntimeError(f"Failed to connect to OpenOCD at {self.host}:{self.port}: {e}") from e

    def _send(self, command: str) -> str:
        """Send a command to OpenOCD and return the response"""
        if self.socket is None:
            raise RuntimeError("Not connected to OpenOCD")

        try:
            # Send command with newline
            self.socket.sendall((command + "\n").encode("utf-8"))
            return self._recv()
        except Exception as e:
            raise RuntimeError(f"OpenOCD command failed: {e}") from e

    def _recv(self) -> str:
        """Receive response from OpenOCD"""
        if self.socket is None:
            raise RuntimeError("Not connected to OpenOCD")

        data = b""
        while True:
            try:
                chunk = self.socket.recv(4096)
                if not chunk:
                    break
                data += chunk
                # OpenOCD ends responses with a specific marker
                if data.endswith(b"\x1a"):
                    break
            except TimeoutError:
                break

        return data.decode("utf-8", errors="ignore").rstrip("\x1a")

    def read(self, address: int, width: int) -> int:
        """
        Read memory via OpenOCD

        Args:
            address: Memory address to read
            width: Width in bytes

        Returns:
            Value read from memory
        """
        # Determine OpenOCD memory access command based on width
        if width == 1:
            cmd = f"mdb 0x{address:x}"
        elif width == 2:
            cmd = f"mdh 0x{address:x}"
        elif width == 4:
            cmd = f"mdw 0x{address:x}"
        elif width == 8:
            cmd = f"mdd 0x{address:x}"
        else:
            raise ValueError(f"Unsupported width: {width}")

        response = self._send(cmd)

        # Parse response (format: "0xADDRESS: VALUE")
        try:
            parts = response.split(":")
            if len(parts) >= 2:
                value_str = parts[1].strip().split()[0]
                return int(value_str, 16)
        except Exception as e:
            raise RuntimeError(f"Failed to parse OpenOCD response: {response}: {e}") from e

        return 0

    def write(self, address: int, value: int, width: int) -> None:
        """
        Write memory via OpenOCD

        Args:
            address: Memory address to write
            value: Value to write
            width: Width in bytes
        """
        # Determine OpenOCD memory access command based on width
        if width == 1:
            cmd = f"mwb 0x{address:x} 0x{value:x}"
        elif width == 2:
            cmd = f"mwh 0x{address:x} 0x{value:x}"
        elif width == 4:
            cmd = f"mww 0x{address:x} 0x{value:x}"
        elif width == 8:
            cmd = f"mwd 0x{address:x} 0x{value:x}"
        else:
            raise ValueError(f"Unsupported width: {width}")

        self._send(cmd)

    def halt(self) -> None:
        """Halt the target"""
        self._send("halt")

    def resume(self) -> None:
        """Resume the target"""
        self._send("resume")

    def reset(self, halt: bool = False) -> None:
        """Reset the target"""
        if halt:
            self._send("reset halt")
        else:
            self._send("reset")

    def close(self) -> None:
        """Close the connection"""
        if self.socket:
            try:
                self._send("exit")
            except Exception:
                pass
            self.socket.close()
            self.socket = None

    def __del__(self) -> None:
        """Cleanup on deletion"""
        self.close()
