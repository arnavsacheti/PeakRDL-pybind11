"""
SSH Master for remote register access
"""

import subprocess

from . import MasterBase


class SSHMaster(MasterBase):
    """
    Master interface using SSH for remote memory access

    Uses devmem or similar tools on the remote system
    """

    def __init__(
        self,
        host: str,
        username: str | None = None,
        password: str | None = None,
        key_file: str | None = None,
        tool: str = "devmem",
    ) -> None:
        """
        Initialize SSH connection

        Args:
            host: SSH host to connect to
            username: SSH username (optional, uses current user if not specified)
            password: SSH password (optional, uses key-based auth if not specified)
            key_file: Path to SSH private key file (optional)
            tool: Memory access tool on remote system (default: "devmem")
        """
        self.host = host
        self.username = username
        self.password = password
        self.key_file = key_file
        self.tool = tool

        # Build base SSH command
        self.ssh_cmd = ["ssh"]
        if key_file:
            self.ssh_cmd.extend(["-i", key_file])
        if username:
            self.ssh_cmd.append(f"{username}@{host}")
        else:
            self.ssh_cmd.append(host)

        # Test connection
        self._test_connection()

    def _test_connection(self) -> None:
        """Test SSH connection"""
        try:
            result = subprocess.run(
                [*self.ssh_cmd, "echo", "test"], capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError(f"SSH connection test failed: {result.stderr}")
        except Exception as e:
            raise RuntimeError(f"Failed to connect via SSH to {self.host}: {e}") from e

    def _run_remote_command(self, command: str) -> str:
        """Execute a command on the remote system"""
        try:
            result = subprocess.run([*self.ssh_cmd, command], capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                raise RuntimeError(f"Remote command failed: {result.stderr}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Remote command timed out: {command}") from None
        except Exception as e:
            raise RuntimeError(f"Failed to execute remote command: {e}") from e

    def read(self, address: int, width: int) -> int:
        """
        Read memory via SSH using devmem

        Args:
            address: Memory address to read
            width: Width in bytes

        Returns:
            Value read from memory
        """
        # devmem uses bit width, not byte width
        bit_width = width * 8

        if self.tool == "devmem":
            # devmem <address> [width]
            cmd = f"{self.tool} 0x{address:x}"
            if width != 4:  # devmem defaults to 32-bit
                cmd += f" {bit_width}"
        else:
            # Custom tool - assume similar syntax
            cmd = f"{self.tool} read 0x{address:x} {width}"

        output = self._run_remote_command(cmd)

        # Parse output (devmem format: "0xVALUE")
        try:
            # Look for hex value in output
            for line in output.split("\n"):
                line = line.strip()
                if line.startswith("0x") or line.startswith("0X"):
                    return int(line, 16)

            # Try parsing as decimal
            return int(output)
        except ValueError as e:
            raise RuntimeError(f"Failed to parse remote read output: {output}: {e}") from e

    def write(self, address: int, value: int, width: int) -> None:
        """
        Write memory via SSH using devmem

        Args:
            address: Memory address to write
            value: Value to write
            width: Width in bytes
        """
        # devmem uses bit width, not byte width
        bit_width = width * 8

        if self.tool == "devmem":
            # devmem <address> [width] <value>
            cmd = f"{self.tool} 0x{address:x}"
            if width != 4:  # devmem defaults to 32-bit
                cmd += f" {bit_width}"
            cmd += f" 0x{value:x}"
        else:
            # Custom tool - assume similar syntax
            cmd = f"{self.tool} write 0x{address:x} 0x{value:x} {width}"

        self._run_remote_command(cmd)
