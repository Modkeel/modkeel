# Contributing to Modkeel

## Development Setup

```bash
git clone https://github.com/Modkeel/modkeel.git
cd modkeel
pip install -e ".[dev]"   # the CLI plus pytest and ruff
```

## Running Tests and Lint

```bash
python3 -m pytest tests/ -q
ruff check modkeel tests
```

Both must pass before submitting a PR. CI runs them on Python 3.10, 3.11 and 3.12
([`.github/workflows/cli.yml`](.github/workflows/cli.yml)).

## Project Structure

All new code goes into the `modkeel/` package. The root `mod_auto_compiler.py` is a deprecated shim -- do not modify it.

The package is composition-based: `Pipeline` (`pipeline.py`) orchestrates one client per concern.

| Module | Role |
|--------|------|
| `cli.py` | Typer commands: `compile`, `search`, `get`, `token`, `status`, `recommend` |
| `pipeline.py` | Clone, compile, multi-pass dependency retry, Docker test, report |
| `github.py`, `validation.py`, `buildinfo.py` | Fork discovery, branch scoring, reading build targets remotely |
| `prebuild.py`, `symbols.py`, `mappings.py`, `javascan.py` | Skip builds whose outcome is knowable before cloning |
| `linkage.py`, `memberlink.py`, `mixinscan.py` | Bytecode checks: does a JAR really target the requested version |
| `build.py` | Gradle build and JAR validation |
| `modrinth.py`, `recommend.py`, `scanner.py` | Modrinth lookups, mods-folder scanning, version/loader recommendations |
| `docker.py` | Headless server boot test |
| `loaders.py`, `version.py`, `models.py`, `config.py` | Loader data, version ranges, data classes, user config |

Tests patch `modkeel.<module>.<object>`, never the deprecated shim.

## Code Style

- **Python 3.10+** target
- **100 character** line length
- **Type hints** on all public functions
- `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants
- All code, comments, docstrings, and error messages in **English**

## Pull Request Guidelines

1. Create a feature branch from `main`
2. Use [conventional commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `refactor:`, `docs:`, `test:`
3. Ensure tests and lint pass (`pytest tests/ -q`, `ruff check modkeel tests`)
4. Keep PRs focused -- one feature or fix per PR

## Reporting Issues

Open an issue at [github.com/Modkeel/modkeel/issues](https://github.com/Modkeel/modkeel/issues) with:

- Modkeel version (`modkeel --version`)
- Python version
- OS and architecture
- Steps to reproduce
- Relevant log output (use `--log-file modkeel.log`)
