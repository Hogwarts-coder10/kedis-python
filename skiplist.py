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

        # Array of distances (odometers) to the next node in each lane
        self.span = [0] * level


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
        """
        Inserts a new member or updates an existing member's score.
        Maintains O(log N) complexity while dynamically updating node spans
        to support O(log N + M) rank-based range queries.
        """
        is_new = 1

        # 1. Handle existing members: remove and re-insert to maintain sorting
        if member in self.member_map:
            is_new = 0
            if self.member_map[member] == score:
                return 0
            self.remove(member)

        self.member_map[member] = score

        update = [self.head] * self.MAX_LEVEL
        rank = [0] * self.MAX_LEVEL
        current = self.head

        # 2. Traverse the skip list to find the insertion point.
        # Track the cumulative rank (distance) traversed at each level.
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

        # 3. Determine the probabilistic level for the new node
        lvl = self._random_level()

        # 4. Initialize new levels if the random level exceeds current max level
        if lvl > self.level:
            for i in range(self.level, lvl):
                rank[i] = 0
                update[i] = self.head
                # The initial span of a new level covers the entire existing list
                update[i].span[i] = len(self.member_map) - 1
            self.level = lvl

        # 5. Splice the new node into the forward pointer chains
        new_node = ZNode(score, member, lvl)

        for i in range(lvl):
            new_node.forward[i] = update[i].forward[i]
            update[i].forward[i] = new_node

            # 6. Recalculate spans for the intersected levels.
            # Distribute the previous node's span between the new node and the target node.
            distance_to_new_node = rank[0] - rank[i]

            new_node.span[i] = update[i].span[i] - distance_to_new_node
            update[i].span[i] = distance_to_new_node + 1

        # 7. Increment the span of all levels above the new node
        # that bypass it completely.
        for i in range(lvl, self.level):
            update[i].span[i] += 1

        return is_new

    def remove(self, member: str) -> int:
        """
        Removes a member from the skip list while maintaining O(log N) complexity.
        Updates the span of all intersected and bypassing levels to preserve rank integrity.
        """
        if member not in self.member_map:
            return 0

        score = self.member_map.pop(member)
        update = [self.head] * self.MAX_LEVEL
        current = self.head

        # 1. Traverse to find the target node and track all predecessor nodes
        for i in range(self.level - 1, -1, -1):
            nxt = current.forward[i]
            while nxt and (
                nxt.score < score or (nxt.score == score and nxt.member < member)
            ):
                current = nxt
                nxt = current.forward[i]
            update[i] = current

        target = current.forward[0]

        # 2. Re-route pointers and recalculate spans
        if target and target.member == member and target.score == score:
            for i in range(self.level):
                if update[i].forward[i] == target:
                    # The target is in this level's path.
                    # Bypass the target and absorb its remaining span.
                    update[i].span[i] += target.span[i] - 1
                    update[i].forward[i] = target.forward[i]
                else:
                    # The target is below this level.
                    # Decrement the span because the bypassed node is removed.
                    update[i].span[i] -= 1

            # 3. Clean up empty upper levels if the highest nodes are removed
            while self.level > 1 and self.head.forward[self.level - 1] is None:
                self.level -= 1
            return 1

        return 0

    def get_range(self, start: int, stop: int, withscores: bool = False) -> list:
        """
        Retrieves a range of members by rank in O(log N + M) time complexity.
        Utilizes span attributes to bypass O(N) linear traversal.
        """
        length = len(self.member_map)
        if length == 0:
            return []

        # 1. Normalize negative indices (e.g., -1 means the last element)
        if start < 0:
            start = max(0, length + start)
        if stop < 0:
            stop = max(0, length + stop)

        # 2. Bounds checking
        if start > stop or start >= length:
            return []

        # Clamp the stop index to the end of the list
        stop = min(stop, length - 1)

        current = self.head
        traversed = 0
        target_rank = start + 1  # Ranks are 1-indexed relative to the head

        # 3. The O(log N) Fast-Forward Search
        # Jump using the span values until we are at the node right before the target
        for i in range(self.level - 1, -1, -1):
            while current.forward[i] and (traversed + current.span[i] < target_rank):
                traversed += current.span[i]
                current = current.forward[i]

        # Step onto the actual start node
        current = current.forward[0]

        # 4. The O(M) Collection Walk
        # Walk exactly (stop - start + 1) steps at the base level
        elements = []
        steps_remaining = (stop - start) + 1

        while current and steps_remaining > 0:
            elements.append(current.member)
            if withscores:
                elements.append(f"{current.score:g}")

            current = current.forward[0]
            steps_remaining -= 1

        return elements

    def __len__(self):
        return len(self.member_map)
