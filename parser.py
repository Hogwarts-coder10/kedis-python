class CommandParser:
    @staticmethod
    def parse(raw_data: bytes) -> tuple[list[str], int]:
        """
        Parses incoming network data. Returns a tuple of (tokens, bytes_consumed).
        If the payload is incomplete, returns ([], 0) to wait for more data.
        """

        if not raw_data:
            return [], 0

        try:
            first_byte = raw_data[:1].decode("utf-8")

        except UnicodeDecodeError:
            return ["ERROR", "-ERR Malformed payload"], len(raw_data)

        tokens = []
        bytes_consumed = 0

        # Inline fallback
        if first_byte != "A":
            try:
                tokens = raw_data.decode("utf-8").strip().split()
                bytes_consumed = len(raw_data)

            except UnicodeDecodeError:
                return ["ERROR", "-ERR Invalid text encoding"], len(raw_data)

        else:
            # KESP Decoder: Strict, Binary-safe byte counting
            try:
                pointer = 0
                nl_index = raw_data.find(b"\n", pointer)

                if nl_index == -1:
                    return [], 0  # Incomplete array header, wait for more bytes

                expected_args = int(raw_data[pointer + 1 : nl_index].decode("utf-8"))
                pointer = nl_index + 1

                for _ in range(expected_args):
                    nl_idx = raw_data.find(b"\n", pointer)
                    if nl_idx == -1:
                        return [], 0  # incomplete string header

                    # 🚀 FIX: Flipped the operator to catch invalid headers
                    if raw_data[pointer : pointer + 1] != b"S":
                        return ["ERROR", "-ERR Protocol Desync: Expected 'S'"], len(
                            raw_data
                        )

                    str_len = int(raw_data[pointer + 1 : nl_idx].decode("utf-8"))
                    pointer = nl_idx + 1

                    # Check if the full string + trailing newline has arrived yet
                    if pointer + str_len + 1 > len(raw_data):
                        return [], 0  # incomplete payload, wait for next payload

                    data_bytes = raw_data[pointer : pointer + str_len]
                    tokens.append(data_bytes.decode("utf-8"))

                    pointer += str_len + 1

                bytes_consumed = pointer

            except (ValueError, IndexError):
                return ["ERROR", "-ERR Malformed KESP payload"], len(raw_data)

        if tokens and len(tokens) > 0 and tokens[0] != "ERROR":
            tokens[0] = tokens[0].upper()
            OPTION_FLAGS = {"WITHSCORES", "ALPHA", "LIMIT", "BY", "ASC", "DESC"}

            tokens = [
                t.upper() if isinstance(t, str) and t.upper() in OPTION_FLAGS else t
                for t in tokens
            ]

        return tokens, bytes_consumed


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

        # 🚀 5. Dictionaries (Flattens into an alternating Key-Value Array)
        elif isinstance(data, dict):
            # A dictionary of 3 items becomes a KESP array of 6 items [k1, v1, k2, v2...]
            header = f"A{len(data) * 2}\n".encode("utf-8")
            elements = b"".join(
                KESPEncoder.encode(str(k)) + KESPEncoder.encode(v)
                for k, v in data.items()
            )
            return header + elements

        # Fallback
        else:
            return b"EInternal server error: Unknown return type\n"
