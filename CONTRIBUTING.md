# Contributing to ModForge

## Development Setup

```bash
git clone https://github.com/Modkeel/modkeel.git
cd ModForge
pip install -e .
```

## Running Tests

```bash
python3 -m pytest tests/ -v
```

All 133 tests must pass before submitting a PR.

## Project Structure

All new code goes into the `modforge/` package. The root `mod_auto_compiler.py` is a deprecated shim -- do not modify it.

See [CLAUDE.md](CLAUDE.md) for the full architecture breakdown.

## Code Style

- **Python 3.10+** target
- **100 character** line length
- **Type hints** on all public functions
- `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants
- All code, comments, docstrings, and error messages in **English**

## Pull Request Guidelines

1. Create a feature branch from `main`
2. Use [conventional commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `refactor:`, `docs:`, `test:`
3. Ensure all tests pass (`pytest tests/ -v`)
4. Keep PRs focused -- one feature or fix per PR

## Reporting Issues

Open an issue at [github.com/Modkeel/modkeel/issues](https://github.com/Modkeel/modkeel/issues) with:

- ModForge version (`modforge --version`)
- Python version
- OS and architecture
- Steps to reproduce
- Relevant log output (use `--log-file modforge.log`)
