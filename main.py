import sys
from store import KedisStore
from parser import CommandParser
from commands import CommandHandler

def main():
    print("🚀 Kedis-Python Architecture Prototype Started.")
    print("Type 'exit', 'quit', or press Ctrl+C to shut down.\n")

    # Initialize the core engine and routing layer
    store = KedisStore()
    store.load()
    handler = CommandHandler(store)

    # The main Event Loop
    while True:
        try:
            # 1. READ
            # Using a custom prompt to mimic a database CLI
            raw_input = input("kedis > ")

            # Intercept graceful shutdown commands before parsing
            if raw_input.strip().lower() in ['exit', 'quit']:
                print("Shutting down Kedis...")
                break

            # 2. PARSE
            tokens = CommandParser.parse(raw_input)

            # Handle empty inputs (user just hit enter)
            if not tokens:
                continue

            # Handle the specific syntax errors we built into the parser last time
            if tokens[0] == "ERROR":
                print(tokens[1])
                continue

            # 3. EVALUATE & PRINT
            response = handler.execute(tokens)
            print(response)

        except KeyboardInterrupt:
            # Safely handle Ctrl+C without throwing a massive traceback
            print("\nShutting down Kedis...")
            break
        except Exception as e:
            # Catch-all for unexpected crashes to keep the server loop alive
            print(f"(error) ERR internal server error: {str(e)}")

if __name__ == "__main__":
    main()
