"""ULID 生成：单调递增、字典序可比（主§8 内部 ID 约定）。"""
import os
import threading
import time

_LOCK = threading.Lock()
_LAST_TS = 0
_LAST_RAND = 0

_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford Base32
_TIME_LEN = 10
_RAND_LEN = 16


def _encode_time(ts: int) -> str:
    chars = []
    for _ in range(_TIME_LEN):
        ts, rem = divmod(ts, 32)
        chars.append(_ENCODING[rem])
    return "".join(reversed(chars))


def _encode_random(rand: int) -> str:
    chars = []
    for _ in range(_RAND_LEN):
        rand, rem = divmod(rand, 32)
        chars.append(_ENCODING[rem])
    return "".join(reversed(chars))


def new_ulid() -> str:
    """生成单调递增 ULID：同毫秒内随机部分递增，保证字典序即时间序。"""
    global _LAST_TS, _LAST_RAND
    with _LOCK:
        ts = int(time.time() * 1000)
        if ts == _LAST_TS:
            _LAST_RAND += 1
            rand = _LAST_RAND
            if rand >= 32**_RAND_LEN:  # 溢出：等待下一毫秒
                while (ts := int(time.time() * 1000)) == _LAST_TS:
                    time.sleep(0.001)
                _LAST_TS = ts
                rand = int.from_bytes(os.urandom(10), "big")
        else:
            _LAST_TS = ts
            rand = int.from_bytes(os.urandom(10), "big")
        _LAST_RAND = rand
        return _encode_time(ts) + _encode_random(rand)
