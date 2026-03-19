# 🚀 Kedis — In-Memory Key-Value Store

Kedis is a lightweight Redis-inspired in-memory database built to explore core system design concepts such as data storage, command parsing, and performance optimization.

---

## 🧠 Motivation

Modern systems rely heavily on fast, in-memory data stores like Redis.
Kedis is built to understand how such systems work internally by implementing key components from scratch.

---

## ⚙️ Features (Planned)

* 🔑 Basic key-value operations:

  * `SET key value`
  * `GET key`
  * `DEL key`
  * `EXISTS key`

* ⏳ Expiry support:

  * `EXPIRE key seconds`
  * `TTL key`

* 🧩 Modular architecture:

  * Command parsing layer
  * Execution layer
  * Storage layer

* 💾 Persistence (optional):

  * Save and load data from disk

---

## 🏗️ Architecture

```
User Input → Parser → Command Handler → In-Memory Store → Response
```

---

## 📁 Project Structure

```
kedis/
│
├── main.py          # Entry point (CLI loop)
├── parser.py        # Parses user commands
├── commands.py      # Command implementations
├── store.py         # In-memory data storage
├── utils.py         # Helper functions
└── README.md
```

---

## 🚦 Getting Started

### Prerequisites

* Python 3.x

### Run the project

```bash
python main.py
```

---

## 💡 Example Usage

```
SET name Karthik
GET name
DEL name
EXISTS name
```

---

## 🔍 Learning Goals

This project focuses on:

* Understanding in-memory data systems
* Designing modular software architecture
* Implementing command-driven interfaces
* Exploring performance trade-offs in system design

---

## 🚀 Future Improvements

* 🔌 TCP server support for multiple clients
* ⚡ Concurrency handling
* 📊 Advanced data structures (lists, sets, etc.)
* 🧠 Optimizations inspired by Redis

---

## 🤝 Contributing

This is a personal learning project, but suggestions and improvements are welcome.

---

## 📌 Author

V SS Karthik

---

> “Built to understand systems, not just use them.”
