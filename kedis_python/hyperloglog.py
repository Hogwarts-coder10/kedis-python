# kedis_python/hyperloglog.py
import hashlib
import math

P = 14                      # index bits -> 16384 registers (same as Redis)
M = 1 << P
Q = 64 - P                  # bits left for the rank = 50
ALPHA = 0.7213 / (1 + 1.079 / M)
_POW = [2.0 ** -i for i in range(Q + 2)]   # 2^-rank lookup, ranks go 0..51


def _hash64(element) -> int:
    if isinstance(element, str):
        element = element.encode("utf-8")
    # NOT Python's hash(): it is randomized per process, so registers
    # saved in a snapshot would be meaningless after a restart.
    return int.from_bytes(hashlib.blake2b(element, digest_size=8).digest(), "big")


class HyperLogLog:
    __slots__ = ("registers", "_cached", "_dirty")

    def __init__(self, registers=None):
        if registers is None:
            self.registers = bytearray(M)
        else:
            if len(registers) != M:
                raise ValueError(f"expected {M} registers, got {len(registers)}")
            self.registers = bytearray(registers)
        self._cached = 0
        self._dirty = registers is not None

    def add(self, element) -> bool:
        """True only if a register changed (this is PFADD's return value)."""
        h = _hash64(element)
        idx = h & (M - 1)                   # low P bits pick the bucket
        w = h >> P                          # remaining 50 bits
        rank = Q - w.bit_length() + 1       # leading zeros in 50-bit field + 1
        if rank > self.registers[idx]:
            self.registers[idx] = rank
            self._dirty = True
            return True
        return False

    def count(self) -> int:
        if not self._dirty:
            return self._cached
        regs = self.registers
        total = sum(_POW[r] for r in regs)
        est = ALPHA * M * M / total
        zeros = regs.count(0)
        if est <= 2.5 * M and zeros:        # small-range: linear counting
            est = M * math.log(M / zeros)
        self._cached = int(round(est))
        self._dirty = False
        return self._cached

    def merge(self, other: "HyperLogLog") -> bool:
        """Register-wise max. True if anything changed."""
        changed = False
        a, b = self.registers, other.registers
        for i in range(M):
            if b[i] > a[i]:
                a[i] = b[i]
                changed = True
        if changed:
            self._dirty = True
        return changed

    def copy(self) -> "HyperLogLog":
        return HyperLogLog(self.registers)

    def to_bytes(self) -> bytes:
        return bytes(self.registers)

    @classmethod
    def from_bytes(cls, data: bytes) -> "HyperLogLog":
        return cls(data)
