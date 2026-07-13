import shlex
from typing import List


class CommandParser:
    @staticmethod
    def parse(raw_data: bytes) -> list[str]:
        """
        Parses incoming network data. Supports both the strict Kedis Serialization Protocol (KESP)
        and legacy inline commands for raw ncat testing.
        """
        if not raw_data:
            return []

        # Peek at the very first byte. If it's not 'A' (Array), fallback to raw text parsing.
        try:
            first_byte = raw_data[:1].decode("utf-8")
        except UnicodeDecodeError:
            return ["ERROR", "-ERR Malformed payload"]

        # 🚦 INLINE FALLBACK: For manual testing via ncat (e.g., typing 'SET engine active')
        if first_byte != "A":
            try:
                return raw_data.decode("utf-8").strip().split()
            except UnicodeDecodeError:
                return ["ERROR", "-ERR invalid text encoding"]

        # 🚀 KESP DECODER: Strict, Binary-Safe Byte Counting
        tokens = []
        try:
            pointer = 0

            # 1. Read the Array Header (e.g., "A3\n")
            nl_idx = raw_data.find(b"\n", pointer)
            if nl_idx == -1:
                return ["ERROR", "-ERR Incomplete KESP Array"]

            expected_args = int(raw_data[pointer + 1 : nl_idx].decode("utf-8"))
            pointer = nl_idx + 1  # Move pointer past the \n

            # 2. Loop exactly 'expected_args' times
            for _ in range(expected_args):
                # Read String Header (e.g., "S6\n")
                nl_idx = raw_data.find(b"\n", pointer)
                if nl_idx == -1:
                    return ["ERROR", "-ERR Incomplete KESP String"]

                if raw_data[pointer : pointer + 1] != b"S":
                    return ["ERROR", "-ERR Protocol desync: Expected 'S'"]

                str_len = int(raw_data[pointer + 1 : nl_idx].decode("utf-8"))
                pointer = nl_idx + 1

                # 3. Extract exact bytes (The Binary-Safe Magic)
                # We do NOT search for a newline here. We slice exactly 'str_len' bytes.
                data_bytes = raw_data[pointer : pointer + str_len]
                tokens.append(data_bytes.decode("utf-8"))

                # Move pointer past the data and its trailing protocol \n
                pointer = pointer + str_len + 1

            return tokens

        except (ValueError, IndexError):
            return ["ERROR", "-ERR Malformed KESP payload"]


class KESPEncoder:
    @staticmethod
    def encode(data) -> bytes:
        """
        Translates raw Python objects from the router into strict KESP network bytes.
        """
        # 1. Null / Missing Data
        if data is None:
            return b"N\n"

        # 2. Integers
        elif isinstance(data, int):
            return f"I{data}\n".encode("utf-8")

        # 3. Strings & Status Messages
        elif isinstance(data, str):
            # Check for simple protocol statuses
            if data in ["OK", "+OK", "+QUEUED"]:
                val = data if data.startswith("+") else f"+{data}"
                return f"{val}\n".encode("utf-8")

            # Check for errors
            elif data.startswith("-ERR") or data.startswith("ERROR"):
                clean_err = data.replace("-ERR ", "").replace("ERROR ", "")
                return f"E{clean_err}\n".encode("utf-8")

            # Otherwise, it's a standard Binary-Safe Bulk String
            else:
                encoded_str = data.encode("utf-8")
                return f"S{len(encoded_str)}\n".encode("utf-8") + encoded_str + b"\n"

        # 4. Arrays (Lists/Sets)
        elif isinstance(data, (list, set, tuple)):
            header = f"A{len(data)}\n".encode("utf-8")
            # Recursively encode every element inside the array
            elements = b"".join(KESPEncoder.encode(item) for item in data)
            return header + elements

        # Fallback
        else:
            return b"EInternal server error: Unknown return type\n"
