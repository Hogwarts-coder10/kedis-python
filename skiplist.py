import random
from typing import Optional


class ZNode:
    """
    A single node in skip list chassis
    """

    def __init__(self, score: float, member: str, level: int):
        self.score = score
        self.member = member

        # Array of forward pointers (express lanes)
        self.forward: list[Optional["ZNode"]] = [None] * level


class SkipList:
    """
    A hybrid Hashmap + Probalisitic skip list.
    """

    def __init__(self):
        self.MAX_LEVEL = 16
        self.P = 0.5
        self.head = ZNode(float("-inf"), "", self.MAX_LEVEL)
        self.level = 1
        self.member_map = {}  # for O(1) lookups

    def _random_level(self):
        """Rolls the dice to determine if a node gets an express lane."""
        lvl = 1
        while random.random() < self.P and lvl < self.MAX_LEVEL:
            lvl += 1
        return lvl

    def insert(self, score: float, member: str) -> int:
        is_new = 1

        # 1. Check if driver is already on the grid
        if member in self.member_map:
            is_new = 0
            if self.member_map[member] == score:
                return 0
            self.remove(member)

        # 2. Add them to the memory map
        self.member_map[member] = score

        # UPGRADE: Pre-fill with self.head instead of None to satisfy Pylance
        update = [self.head] * self.MAX_LEVEL
        current = self.head

        for i in range(self.level - 1, -1, -1):
            nxt = current.forward[i]
            # UPGRADE: Assigning to 'nxt' proves to Pylance it is not None
            while nxt and (
                nxt.score < score or (nxt.score == score and nxt.member < member)
            ):
                current = nxt
                nxt = current.forward[i]
            update[i] = current

        lvl = self._random_level()
        if lvl > self.level:
            for i in range(self.level, lvl):
                update[i] = self.head
            self.level = lvl

        new_node = ZNode(score, member, lvl)
        for i in range(lvl):
            new_node.forward[i] = update[i].forward[i]
            update[i].forward[i] = new_node

        return is_new

    def remove(self, member: str) -> int:
        if member not in self.member_map:
            return 0

        score = self.member_map.pop(member)
        # UPGRADE: Pre-fill with self.head
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
                if update[i].forward[i] != target:
                    break
                update[i].forward[i] = target.forward[i]

            while self.level > 1 and self.head.forward[self.level - 1] is None:
                self.level -= 1
            return 1
        return 0

    def get_range(self, start: int, stop: int, withscores: bool = False) -> list:
        """Traverses the bottom lane (Level 0) which is perfectly sorted."""
        current = self.head.forward[0]
        elements = []

        while current:
            elements.append((current.member, current.score))
            current = current.forward[0]

        # Handle negative slicing constraints
        slice_items = elements[start:] if stop == -1 else elements[start : stop + 1]

        result = []
        for member, score in slice_items:
            result.append(member)
            if withscores:
                # Format float to remove trailing zeros (e.g., 84.0 -> 84)
                result.append(f"{score:g}")
        return result

    def __len__(self):
        return len(self.member_map)
