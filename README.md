## 🚀 Kedis-Python — Redis-Inspired Architecture Prototype
Overview
Kedis-Python is a lightweight Redis-inspired in-memory key-value store built in Python to explore system architecture and internal design concepts behind modern in-memory databases.

This project focuses on understanding:
- command parsing
- modular system design
- storage abstraction
- execution flow
- extensible architecture

before moving toward a lower-level implementation in C.

# 🧠 Motivation
Modern systems heavily rely on fast in-memory databases like Redis for caching, message brokering, and high-speed data access.

Kedis-Python is designed as an architecture-first prototype to understand how such systems work internally by implementing core components from scratch.

The goal is not to clone Redis completely, but to deeply understand:
- how commands flow through a system
- how storage layers interact
- how modular architectures are structured
- how scalable backend systems are designed

# ⚙️ Features (Planned)
🔑 Core Key-Value Operations
- SET key value
- GET key
- DEL key
- EXISTS key

⏳ Expiry Support
- EXPIRE key seconds
- TTL key

🧩 Modular Architecture
- Parser Layer
- Command Execution Layer
- Storage Layer
- Utility Layer

💾 Persistence (Optional)
- Save in-memory data to disk
- Reload saved data during startup

# 🏗️ Architecture
User Input → Parser → Command Handler → In-Memory Store → Response
📁 Project Structure
kedis-python/
│
├── main.py          # Entry point / CLI loop
├── parser.py        # Command parser
├── commands.py      # Command implementations
├── store.py         # In-memory storage engine
├── utils.py         # Utility/helper functions
└── README.md

# 🚦 Getting Started
Prerequisites:
- Python 3.x

Run the project:
python main.py


# 💡 Example Usage

- SET name Karthik
- GET name
- EXISTS name
- DEL name


# 🎯 Learning Goals
- understanding in-memory data systems
- designing modular software architecture
- implementing command-driven systems
- exploring backend system design
- preparing for lower-level systems implementation
  
# 🚀 Future Improvements
- TCP server support
- Multi-client handling
- Concurrency support
- Advanced data structures
- Redis-inspired optimizations
- C implementation after architecture validation
  
# 🤝 Contributing
This is primarily a personal learning project, but ideas, suggestions, and improvements are welcome.

📌 Author
V SS Karthik
  
>"Built to understand systems, not just use them.”
