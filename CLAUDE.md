# Repository Guidelines

This file contains foundational instructions and conventions for developing in this repository. AI agents and developers must strictly adhere to these rules. The architecture and the implementation plan are described in [project_architecture.md](project_architecture.md).

## Table of Contents
- [1. Directory Structure](#1-directory-structure)
- [2. Dependencies](#2-dependencies)
- [3. Environment Execution](#3-environment-execution)
- [4. Code Quality & Formatting](#4-code-quality--formatting)
- [5. Language & Documentation Style](#5-language--documentation-style)
- [6. Linting & Validation](#6-linting--validation)
- [7. Security Best Practices](#7-security-best-practices)
- [8. Data Collection Rules](#8-data-collection-rules)
- [9. Error Handling](#9-error-handling)
- [10. Version Control & Committing](#10-version-control--committing)

## 1. Directory Structure
- The `src` directory is strictly reserved for the core logic of the application.
- Do not place auxiliary code in `src`. Place supporting files in meaningfully named directories (e.g., `scripts`, `config`).
- Maintain a separate `tests` directory for unit and integration tests to keep the core codebase clean.
- Datasets and pipeline state live in `data/` and are written only by the pipeline.

## 2. Dependencies
- Always maintain a `pyproject.toml` and a `uv.lock` file at the root of the project.
- Any time a new Python dependency is introduced, it must be added to the `dependencies` array in `pyproject.toml`.
- After modifying dependencies, always run `uv lock` to update the `uv.lock` file and ensure reproducible builds.

## 3. Environment Execution
- Run local scripts and tests with `uv run` (e.g., `uv run python src/main.py`, `uv run pytest`), so that they use the locked dependencies.

## 4. Code Quality & Formatting
- Avoid hardcoding paths without environment checks; use relative paths or standard library path resolution (e.g., `pathlib`).
- Use the standard Python `logging` module instead of `print()` statements for proper log leveling, observability, and debugging.
- Implement Python type hinting (PEP 484) across all function signatures and class definitions to improve readability and catch type errors early.
- Follow PEP 8 style guidelines for Python code.

## 5. Language & Documentation Style
- **Tone:** Maintain a sober, objective, and professional tone across all documentation (including `README.md`).
- **Vocabulary:** Use only as many adjectives as strictly required. Do not use superlatives (e.g., "fastest", "best", "most") or filler words.
- **Emojis:** Do not use emojis anywhere in the codebase, documentation, or user interfaces.
- **Language:** Everything in the project must be written in English (documentation, markdown files, code comments, variable names, etc.). German proper names (parties, institutions, outlets) keep their original spelling.
- **Table of Contents:** Every markdown file containing more than one paragraph must include a Table of Contents.
- **Docstrings:** All modules, classes, and public functions must include clear, structured docstrings (e.g., Google-style) detailing their purpose, arguments, and return types.

## 6. Linting & Validation
- Code must pass `ruff check` before being finalized.
- Always verify that configuration files (e.g., `.yml`, `.toml` or `.json` files) remain structurally compliant and correctly reflect any changes made to the codebase (such as script renames or moving files).

## 7. Security Best Practices
- Never hardcode secrets, passwords, or API keys in the repository. Always rely on environment variables or the `.env` file, which is listed in `.gitignore`.
- The repository is public. New secret variables are documented by name in the configuration table of `README.md`, never with values.

## 8. Data Collection Rules
- All HTTP requests go through `src/web.py` (`WebClient`), which applies the fetch rules.
- Never bypass bot protection. A source that returns HTTP 401 or 403, a challenge page or a CAPTCHA is set to `blocked` and is not fetched again automatically. Do not add headless browsers.
- Respect `robots.txt` for every website request. Only documented APIs that are offered for programmatic use (DAWUM, abgeordnetenwatch, DIP, Wikimedia, Wikidata) are called with `respect_robots=False`, under their API terms and rate limits.
- Respect text-and-data-mining reservations (TDMRep) of every website.
- Do not store full texts of articles or press releases. Text fetched for annotation is discarded after use; only titles, URLs, metadata and derived variables are stored.
- Store only public actors (office holders, candidates, parties, organizations). Names of private persons are not stored.
- Every record carries the license of its source in the `license` field.

## 9. Error Handling
- Use specific exception handling (`try...except` blocks) instead of catching generic `Exception`s where possible, and provide informative error messages.

## 10. Version Control & Committing
- Never run `git commit` or `git push`, and never commit or push through GitHub tools or APIs, without the user's explicit approval of that specific commit. Prepare the changes locally, show the diff and the proposed commit message, and wait for approval. Approval of one commit does not extend to later commits.
- Commit messages follow the Conventional Commits format: `type(optional-scope)!: description`.
