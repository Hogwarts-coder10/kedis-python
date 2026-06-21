# Contributing to Kedis-Python

First off, thank you for considering contributing to Kedis-Python! 🚀

Kedis-Python is a Redis-inspired in-memory database built for learning database internals, networking, persistence, and systems programming concepts.

## Before You Start

Please:

1. Read the README.
2. Check existing issues before creating a new one.
3. Open a discussion for major architectural changes.
4. Keep contributions focused and well-tested.

---

## Development Setup

### Clone the Repository

```bash
git clone https://github.com/<your-username>/kedis-python.git
cd kedis-python
```

### Create Virtual Environment

```bash
python -m venv .venv
```

Activate:

Linux/macOS:

```bash
source .venv/bin/activate
```

Windows:

```powershell
.venv\Scripts\activate
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Running Kedis

Standalone Mode:

```bash
python main.py
```

TCP Server Mode:

```bash
python server.py
```

---

## Running Tests

```bash
pytest
```

Before submitting a Pull Request:

* Ensure all tests pass.
* Add tests for new features.
* Verify existing functionality remains intact.

---

## Coding Guidelines

### General Principles

* Prefer readability over cleverness.
* Keep functions focused on a single responsibility.
* Add comments when logic is non-obvious.
* Avoid unnecessary abstractions.

### Naming

Use descriptive names:

Good:

```python
load_snapshot()
```

Bad:

```python
ls()
```

---

## Persistence Changes

Changes affecting:

* AOF
* Snapshotting
* Recovery
* Storage Engine

must be tested carefully.

Please verify:

* Server restart recovery
* Data integrity
* AOF compaction behavior
* Snapshot loading

---

## Networking Changes

Changes affecting TCP mode should be tested with:

* Multiple clients
* Concurrent requests
* INFO command
* HELP command
* Connection failures

---

## Benchmarking

When reporting benchmark results, include:

* Thread count
* Total operations
* Persistence mode
* Hardware information

Example:

```text
Threads: 10
Operations: 200,000
Persistence: appendfsync everysec
Throughput: 21,492 req/sec
```

---

## Pull Request Checklist

Before opening a PR:

* [ ] Code runs successfully
* [ ] Tests pass
* [ ] New tests added where appropriate
* [ ] Documentation updated
* [ ] README updated if needed
* [ ] No unnecessary files included

---

## Reporting Bugs

When opening an issue, include:

* Operating System
* Python Version
* Kedis Version
* Steps to Reproduce
* Expected Behavior
* Actual Behavior
* Error Logs (if available)

---

## Project Goals

Kedis-Python aims to explore:

* In-memory databases
* Persistence mechanisms
* Networking
* Concurrency
* Data structures
* Database internals

Educational value is prioritized alongside performance.

---

Thank you for helping improve Kedis-Python! 🚀
