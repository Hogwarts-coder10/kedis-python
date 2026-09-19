"""
Before/after benchmark for skiplist.py's own optimizations:
__slots__, faster coin flip, fewer dict lookups, local var caching.

Both versions are defined in this one file so there's no import
ambiguity — OLD is the original skiplist.py, NEW is the optimized one.

Run: python bench_skiplist_optimizations.py
"""

import random
import string
import time
import statistics


# ============================================================
# OLD — original skiplist.py, unmodified
# ============================================================

class OldZNode:
    def __init__(self, score, member, level):
        self.score = score
        self.member = member
        self.forward = [None] * level
        self.span = [0] * level


class OldSkipList:
    def __init__(self):
        self.MAX_LEVEL = 16
        self.P = 0.5
        self.head = OldZNode(float("-inf"), "", self.MAX_LEVEL)
        self.level = 1
        self.member_map = {}

    def _random_level(self):
        lvl = 1
        while random.random() < self.P and lvl < self.MAX_LEVEL:
            lvl += 1
        return lvl

    def insert(self, score, member):
        is_new = 1
        if member in self.member_map:
            is_new = 0
            if self.member_map[member] == score:
                return 0
            self.remove(member)

        self.member_map[member] = score

        update = [self.head] * self.MAX_LEVEL
        rank = [0] * self.MAX_LEVEL
        current = self.head

        for i in range(self.level - 1, -1, -1):
            rank[i] = rank[i + 1] if i < self.level - 1 else 0
            nxt = current.forward[i]
            while nxt and (
                nxt.score < score or (nxt.score == score and nxt.member < member)
            ):
                rank[i] += current.span[i]
                current = nxt
                nxt = current.forward[i]
            update[i] = current

        lvl = self._random_level()

        if lvl > self.level:
            for i in range(self.level, lvl):
                rank[i] = 0
                update[i] = self.head
                update[i].span[i] = len(self.member_map) - 1
            self.level = lvl

        new_node = OldZNode(score, member, lvl)

        for i in range(lvl):
            new_node.forward[i] = update[i].forward[i]
            update[i].forward[i] = new_node
            distance_to_new_node = rank[0] - rank[i]
            new_node.span[i] = update[i].span[i] - distance_to_new_node
            update[i].span[i] = distance_to_new_node + 1

        for i in range(lvl, self.level):
            update[i].span[i] += 1

        return is_new

    def remove(self, member):
        if member not in self.member_map:
            return 0

        score = self.member_map.pop(member)
        update = [self.head] * self.MAX_LEVEL
        current = self.head

        for i in range(self.level - 1, -1, -1):
            nxt = current.forward[i]
            while nxt and (
                nxt.score < score or (nxt.score == score and nxt.member < member)
            ):
                current = nxt
                nxt = current.forward[i]
            update[i] = current

        target = current.forward[0]

        if target and target.member == member and target.score == score:
            for i in range(self.level):
                if update[i].forward[i] == target:
                    update[i].span[i] += target.span[i] - 1
                    update[i].forward[i] = target.forward[i]
                else:
                    update[i].span[i] -= 1

            while self.level > 1 and self.head.forward[self.level - 1] is None:
                self.level -= 1
            return 1

        return 0

    def __len__(self):
        return len(self.member_map)


# ============================================================
# NEW — import the optimized version from the actual file
# ============================================================

from skiplist import SkipList as NewSkipList


# ============================================================
# Benchmark harness
# ============================================================

N = 20_000
MEMBER_LEN = 12


def make_members(n):
    return ["".join(random.choices(string.ascii_letters, k=MEMBER_LEN)) for _ in range(n)]


def make_scores(n):
    return [random.uniform(0, 1_000_000) for _ in range(n)]


def bench_insert(cls, members, scores):
    sl = cls()
    start = time.perf_counter()
    for m, s in zip(members, scores):
        sl.insert(s, m)
    return time.perf_counter() - start, sl


def bench_remove(sl, members):
    start = time.perf_counter()
    for m in members:
        sl.remove(m)
    return time.perf_counter() - start


def run_trials(fn, *args, trials=5):
    times = []
    result = None
    for _ in range(trials):
        t, result = fn(*args)
        times.append(t)
    return statistics.median(times), result


def main():
    print(f"N = {N:,} members, member length = {MEMBER_LEN}\n")

    members = make_members(N)
    scores = make_scores(N)

    old_insert_time, _ = run_trials(bench_insert, OldSkipList, members, scores)
    new_insert_time, _ = run_trials(bench_insert, NewSkipList, members, scores)

    def report(label, old_t, new_t):
        factor = old_t / new_t if new_t > 0 else float("inf")
        verb = "speedup" if factor >= 1 else "slowdown"
        print(f"{label}")
        print(f"  old: {old_t*1000:8.2f} ms")
        print(f"  new: {new_t*1000:8.2f} ms")
        print(f"  {verb}: {factor:.2f}x\n")

    report("INSERT", old_insert_time, new_insert_time)

    old_sl = OldSkipList()
    for m, s in zip(members, scores):
        old_sl.insert(s, m)

    new_sl = NewSkipList()
    for m, s in zip(members, scores):
        new_sl.insert(s, m)

    shuffled = members[:]
    random.shuffle(shuffled)

    old_remove_time = bench_remove(old_sl, shuffled)
    new_remove_time = bench_remove(new_sl, shuffled)

    report("REMOVE", old_remove_time, new_remove_time)


if __name__ == "__main__":
    main()
