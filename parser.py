import shlex
from typing import List

class CommandParser:
    @staticmethod
    def parse(raw_input: str) -> List[str]:
        """
        Tokenizes raw string input into a list of arguments.
        Handles multi-word quoted strings and returns specific errors on failure.
        """
        if not raw_input or not raw_input.strip():
            return []

        try:
            tokens = shlex.split(raw_input)

            if tokens:
                tokens[0] = tokens[0].upper()

            return tokens

        except ValueError as e:
            # Instead of silently returning [], we capture the exact shlex error
            # (e.g., "No closing quotation") and pass it downstream.
            return ["ERROR", f"ERR parsing command: {str(e).lower()}"]
