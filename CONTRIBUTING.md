# Contributing to Telegram Sendmail

Contributions are welcome. This document defines the standards, workflow, and
expectations for all submissions to this project.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Environment](#development-environment)
- [Toolchain & Quality Standards](#toolchain--quality-standards)
- [Running Tests](#running-tests)
- [Pull Request Process](#pull-request-process)
- [Reporting Issues](#reporting-issues)
- [Commit Message Convention](#commit-message-convention)

## Code of Conduct

All interactions must remain professional and respectful. Harassment,
dismissive language, or personal attacks of any kind are not tolerated. Conduct
concerns may be raised via a private issue or by contacting the maintainer
directly through the contact information on the GitHub profile.

## Getting Started

**Prerequisites:** Python 3.10 or later, `git`, and `pip`.

```bash
# 1. Fork the repository on GitHub, then clone the fork
git clone https://github.com/<username>/telegram-sendmail.git
cd telegram-sendmail

# 2. Create an isolated virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install the package in editable mode with all dev dependencies
pip install -e ".[dev]"

# 4. Install and activate the pre-commit hooks
pre-commit install
```

Step 3 installs the package from the `src/` layout in editable mode so changes
to `src/telegram_sendmail/` are immediately reflected without reinstalling. The
`[dev]` extra installs the complete quality toolchain described below.

After this setup, every `git commit` will automatically run the full quality
pipeline on staged files via pre-commit.

## Development Environment

The project uses the standard `src/` layout. Source files reside under
`src/telegram_sendmail/`. Tests reside under `tests/`. Source files must not
be placed outside the `src/` tree.

Key conventions:

- `src/telegram_sendmail/py.typed` is a PEP 561 marker file indicating that
  this package ships inline type annotations.
- The authoritative version string is defined once in
  `src/telegram_sendmail/__init__.py` (`__version__`). Hatchling reads it from
  there. The version string must not be duplicated anywhere else in the
  codebase.
- Configuration for all tools lives exclusively in `pyproject.toml`. Additional
  tool-specific config files (`setup.cfg`, `tox.ini`, `.flake8`, etc.) are not
  permitted.

The `[dev]` dependency group installs everything required for development:

| Tool             | Purpose                                 |
|------------------|-----------------------------------------|
| `ruff`           | Linting, formatting, and import sorting |
| `mypy`           | Static type checking (strict mode)      |
| `pytest`         | Test runner                             |
| `pytest-cov`     | Coverage measurement                    |
| `requests-mock`  | HTTP transport-layer mocking            |
| `types-requests` | Type stubs for `requests` (MyPy)        |
| `pre-commit`     | Git hook manager                        |

### Binary Builds — PyInstaller

The release pipeline compiles the package into a standalone binary using
[PyInstaller](https://pyinstaller.org/). Contributors working on imports,
entry points, or packaging must be aware of the following constraints:

- **Hidden imports:** modules imported dynamically (e.g. via
  `importlib.import_module`) rather than through a static `import` statement
  are not detected by PyInstaller's dependency analyser. A `--hidden-import`
  flag or a `.spec` file hook is required for any such import.
- **Data files:** non-Python assets referenced at runtime (e.g. via
  `importlib.resources` or path construction relative to `__file__`) must be
  declared explicitly in the PyInstaller spec or they will not be bundled. The
  current codebase has no such assets, but contributors adding them must
  account for this.
- **`__file__` assumptions:** inside a frozen binary, `__file__` does not
  point to a `.py` source file on disk. Any runtime path construction that
  relies on `__file__` will behave differently inside the compiled binary
  versus a standard Python installation. Use `importlib.resources` for
  accessing package data instead.
- **Smoke-testing:** the release CI runs `telegram-sendmail --version` against
  the compiled binary as a basic import sanity check. A PR that introduces an
  import or packaging change that causes the binary to fail at startup will be
  caught at this step.

To verify a local binary build before opening a PR that affects imports or
packaging:

```bash
pip install pyinstaller
pyinstaller --onefile --name telegram-sendmail --strip \
    src/telegram_sendmail/__main__.py
./dist/telegram-sendmail --version
```

## Toolchain & Quality Standards

All toolchain configuration lives in `pyproject.toml`. The pre-commit pipeline
enforces these checks on every commit; CI enforces them on every push. A
contribution will not be merged if any check fails.

### Linting & Formatting — Ruff

Ruff is the sole tool for formatting, linting, and import sorting.
The `pyproject.toml` configuration enables rule sets
including `pyflakes`, `pycodestyle`, `pyupgrade`, `pylint`, and
`flake8-pytest-style`.

```bash
# Check for errors and auto-fix what is safe to fix automatically
ruff check --fix .

# Format all source files
ruff format .
```

The pre-commit hook runs both commands automatically on staged files.

### Type Checking — MyPy (Strict Mode)

All new code must pass MyPy under `strict` mode:

- Every function and method requires complete type annotations on parameters
  and return values.
- `Any` is disallowed unless no viable alternative exists. If used, a comment
  explaining the necessity is mandatory.
- `# type: ignore` comments are forbidden without a specific error code and a
  documented rationale (e.g.,
  `# type: ignore[misc]  # html2text callback signature is untyped upstream`).

```bash
mypy src/ tests/
```

### Running All Checks at Once

```bash
pre-commit run --all-files
```

## Running Tests

Tests use `pytest` and reside in the `tests/` directory. Shared fixtures are
provided in `tests/conftest.py`. No test may make live network calls or write
to real filesystem paths outside `pytest`'s `tmp_path`.

```bash
# Full test suite with coverage report
pytest

# Single module
pytest tests/test_parser.py

# Verbose output
pytest -v

# Keyword filter
pytest -k "smtp"
```

The global coverage gate is set at **80%**. Critical logic in `client.py`,
`parser.py`, and `smtp.py` is enforced at **90%** by CI. New code should
meet or exceed these thresholds. To verify per-module thresholds locally
before pushing, run the enforcement script after the test suite:

```bash
# Enforce per-module coverage thresholds (requires a prior pytest run)
python scripts/module_coverage.py
```

### Test Design Standards

- Tests must exercise **real behaviour**, not mirror the implementation's
  assumptions. A mock or fixture that encodes the same incorrect assumption
  as the source code is a tautological test — it provides false confidence.
- Tests for filesystem operations (spool writes, config file reads) must use
  `tmp_path` and must be skipped on UID 0 where they depend on DAC permission
  enforcement, because root bypasses file permission checks on Linux.
- `requests-mock` is the standard mechanism for mocking HTTP calls. It patches
  at the transport layer so the full `requests.Session` → `HTTPAdapter`
  pipeline runs, except for the actual network call.
- Fixtures in `conftest.py` must be genuinely shared across multiple test
  modules. Single-use fixtures — those consumed by only one test function or
  one test class — must be defined locally in the relevant test module or as
  a local helper within the test itself. Accumulating single-use fixtures in
  `conftest.py` inflates the shared namespace, increases fixture discovery
  overhead, and makes the file harder to reason about for contributors
  unfamiliar with the codebase.

## Pull Request Process

1. **Branch from `main`** using a descriptive name:
   `feat/retry-on-telegram-timeout`, `fix/smtp-dot-stuffing-edge-case`.

2. **Keep PRs focused.** One logical change per PR. Unrelated improvements
   observed while working on a fix should be submitted as separate PRs.

3. **Update `CHANGELOG.md`** under the `[Unreleased]` section following the
   [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

4. **Ensure the full pre-commit pipeline passes** before opening the PR:

   ```bash
   pre-commit run --all-files
   pytest
   ```

5. **Write or update tests** for any logic added or modified.

6. **Complete the PR description** — explain *what* changed and *why*. Link
   to the relevant issue if one exists.

7. A PR requires **one approval** from the maintainer before merging. Requested
   changes should be addressed in new commits rather than force-pushes so the
   review history is preserved.

8. **Squash-merging** is the preferred strategy to maintain a linear `main` branch
   history.

## Reporting Issues

Before opening an issue, check the existing
[Issues](https://github.com/theodiv/telegram-sendmail/issues) to avoid
duplicates, and inspect `journalctl -t telegram-sendmail -f` for relevant
log output.

When opening an issue, include:

- The version (`telegram-sendmail --version`)
- Python version (`python3 --version`) and Linux distribution
- Installation method (pre-built binary or source)
- Whether the issue manifests in pipe mode or SMTP mode (`-bs`)
- Sanitised log output (bot token and chat ID must be removed before posting)
- Steps to reproduce

## Commit Message Convention

This project follows the
[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
specification. Compliance is enforced automatically on every pull request.

```
<type>(<scope>): <short summary>

[optional body]

[optional footer: Closes #<issue>]
```

**Types:** `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `ci`.

**Scope** (optional): `config`, `parser`, `client`, `smtp`, `spool`, `cli`,
`build`.

**Examples:**

```
feat(client): add exponential backoff retry on Telegram 429 responses

fix(smtp): handle bare LF in DATA stream without panicking

docs: document update-alternatives symlink strategy in README

build: add hidden-import hook for dynamic logging backend

chore: bump pre-commit hook revisions
```

The short summary must use the imperative mood ("add", "fix", "remove") and
must not end with a period.
