"""Inter-process control surface: local JSON socket + Native Messaging host."""

from .server import IPCClient, IPCServer, is_running, read_endpoint, write_endpoint

__all__ = ["IPCClient", "IPCServer", "is_running", "read_endpoint", "write_endpoint"]
