"""端口占用预检测试（S1 完成定义：端口占用场景）。"""
import socket

import pytest

from launcher import PortInUseError, ensure_port_free


def test_ensure_port_free_raises_when_occupied() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        with pytest.raises(PortInUseError):
            ensure_port_free("127.0.0.1", port)


def test_ensure_port_free_passes_when_available() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # socket 已关闭，端口应可用
    ensure_port_free("127.0.0.1", port)
