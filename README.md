```
    __  ___          ______
   /  |/  /___  ____/ / __/___  _________ ____
  / /|_/ / __ \/ __  / /_/ __ \/ ___/ __ `/ _ \
 / /  / / /_/ / /_/ / __/ /_/ / /  / /_/ /  __/
/_/  /_/\____/\__,_/_/  \____/_/   \__, /\___/
                                  /____/
```

> **Compile the mods Mojang left behind**

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Version](https://img.shields.io/badge/version-1.0.0-orange)
![Tests](https://img.shields.io/badge/tests-133_passing-brightgreen)

---

ModForge is a CLI tool that finds, compiles, and verifies unofficial Minecraft mod forks for versions the original authors don't support. When Mojang releases a patch version (like 1.21.10), major mods skip it -- but community forks exist on GitHub as uncompiled branches. ModForge finds them, builds them, and validates the output.

## Quick Start

```bash
git clone https://github.com/Modkeel/modkeel.git
cd ModForge && pip install -e .
modforge compile repos.txt -m 1.21.10 -l neoforge -lv 64 -t "$(cat github_token.txt)"
```

## Quick Demo

### Search for a mod
![modforge search](demo/search.gif)

### Compile mods from a list
![modforge compile](demo/compile.gif)

### Check status
![modforge status](demo/status.gif)

> GIFs generated with [VHS](https://github.com/charmbracelet/vhs). See [`demo/README.md`](demo/README.md) to regenerate.

## How It Works

For each repository in your list, ModForge runs:

1. **Modrinth Check** -- Search for a pre-compiled JAR first (skip compilation if found)
2. **Fork Discovery** -- Search GitHub for compatible forks and independent ports
3. **Branch Scoring** -- Rank branches by version match, loader match, and freshness
4. **Pre-validation** -- Check `gradle.properties` via GitHub API before cloning
5. **Cross-loader Fallback** -- If no NeoForge match, try Fabric forks via Sinytra Connector
6. **Compile** -- Clone, run `gradlew build`, validate output JAR
7. **Multi-pass Dependencies** -- Retry failed mods with `mavenLocal()` after others succeed
8. **Docker Testing** (opt-in) -- Boot a headless MC server to verify mods load

## Requirements

- **Python 3.10+**
- **Git** (in PATH)
- **JDK 17 or 21** (for Gradle compilation)
- **Docker** (optional, for `--docker-test`)

```bash
python --version  # 3.10+
git --version
java -version     # 17 or 21
```

## Installation

```bash
git clone https://github.com/Modkeel/modkeel.git
cd ModForge
pip install -e .
```

This installs the `modforge` CLI command and all dependencies.

## Usage

### Compile Mods

```bash
modforge compile repos.txt \
  --mc-version 1.21.10 \
  --loader neoforge \
  --loader-version 64 \
  --github-token "$(cat github_token.txt)"
```

### Compile and Install to Instance

```bash
modforge compile repos.txt \
  -m 1.21.10 -l neoforge -lv 64 \
  --instance "/path/to/minecraft/instance" \
  -t "$(cat github_token.txt)"
```

### Compile and Test in Docker

```bash
modforge compile repos.txt \
  -m 1.21.10 -l neoforge -lv 64 \
  --docker-test \
  -t "$(cat github_token.txt)"
```

### Search Without Compiling

```bash
modforge search "Create" -m 1.21.10 -l neoforge -t "$(cat github_token.txt)"
```

### Check Status

```bash
modforge status
```

> **Legacy:** `python mod_auto_compiler.py ...` still works but is deprecated. Use `modforge compile` instead.

## Repository File Format

The `repos.txt` file contains one repository URL per line:

```
# Base URL -- ModForge finds the best branch automatically
https://github.com/Creators-of-Create/Create
https://github.com/mezz/JustEnoughItems

# Specific branch
https://github.com/PepperCode1/Continuity/tree/1.21.10/dev

# Comments start with #
```

## Command Reference

### `modforge compile`

| Option | Description | Default |
|--------|-------------|---------|
| `repos_file` | Text file with repository URLs | (required) |
| `-m`, `--mc-version` | Minecraft version | (required) |
| `-l`, `--loader` | Mod loader: `neoforge`, `forge`, `fabric` | (required) |
| `-lv`, `--loader-version` | Loader version number | (required) |
| `-i`, `--instance` | Minecraft instance path | -- |
| `-o`, `--output-dir` | Output directory | `out` |
| `-t`, `--github-token` | GitHub API token | -- |
| `--strict` | Require exact MC version match | off |
| `--no-cross-loader` | Disable Sinytra Connector fallback | off |
| `--docker-test` | Test mods in Docker server | off |
| `--docker-timeout` | Docker timeout (seconds) | 180 |
| `--output-report` | Save report to file | -- |
| `--log-file` | Write log to file | -- |
| `--no-share` | Skip anonymous data sharing | off |

### `modforge search`

| Option | Description | Default |
|--------|-------------|---------|
| `query` | Mod name to search | (required) |
| `-m`, `--mc-version` | Minecraft version | (required) |
| `-l`, `--loader` | Mod loader | `neoforge` |
| `-t`, `--github-token` | GitHub API token | -- |

### `modforge status`

No arguments. Shows version, config, cached loaders, and known NeoForge versions.

## GitHub API Token

Without a token you're limited to 60 API requests/hour. With a token: 5,000/hour.

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. **Generate new token** > **Tokens (classic)**
3. Select scope: **`public_repo`** (only permission needed)
4. Save the token: `echo "ghp_xxx..." > github_token.txt`

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `gradle.properties not found` | Branch lacks Gradle files | Try a different branch |
| `Compilation timeout (>10 min)` | Large mod or slow machine | Build manually with `./gradlew build` |
| `GitHub API rate limit exceeded` | No token or too many requests | Use `--github-token` |
| `minecraft_version is X, expected Y` | Branch targets wrong version | Expected -- ModForge tries the next branch |
| `No JAR file found in build/libs` | Unusual output path | Check `build.gradle` |
| `JAR declares incompatible MC version` | Misconfigured `mods.toml` | Try another branch |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and PR guidelines.

## License

MIT License -- see [LICENSE](LICENSE) for details.
