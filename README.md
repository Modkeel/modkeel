# ModForge - Minecraft Mod Auto-Compiler

Automatically finds, compiles, and verifies unofficial Minecraft mod forks for versions the original authors don't support.

## Purpose

This tool solves a specific problem in the Minecraft modding community: **getting mods for intermediate patch versions** (like 1.21.10) that official mod authors don't support.

### The Problem:
- Mojang releases Minecraft 1.21.10 (minor patch)
- NeoForge/Forge updates quickly
- **Major mods (Create, JEI, etc.) skip patch versions** and only update for major releases (1.20.1 → 1.21.1)
- Community creates **unofficial forks** with 1.21.10 support
- These forks exist only as **GitHub branches**, not on Modrinth/CurseForge
- You have to manually find, clone, compile, and validate each one

### The Solution:
ModForge **automatically**:
1. Checks Modrinth for pre-compiled JARs first
2. Finds compatible forks and branches on GitHub
3. Validates compatibility before compiling
4. Compiles mods from source with Gradle
5. Verifies the compiled JAR works (including Docker server testing)
6. Installs directly to your Minecraft instance
7. Generates detailed reports of success/failures

---

## Requirements

### System Requirements:
- **Python 3.10+**
- **Git** (must be in PATH)
- **Java Development Kit (JDK) 17 or 21** (required for Gradle compilation)
- **Docker** (optional, for headless server testing with `--docker-test`)

### Verify Requirements:
```bash
python --version  # Should be 3.10+
git --version
java -version     # Should be 17 or 21
```

---

## Installation

```bash
git clone https://github.com/juanzab/ModForge.git
cd ModForge
pip install -e .
```

This installs the `modforge` CLI command and all dependencies.

### Create your repository list:
```bash
# Copy the example
cp repos.txt my_mods.txt

# Edit with your favorite editor
nano my_mods.txt  # or notepad on Windows
```

---

## GitHub API Token (Recommended)

Without a token, you're limited to **60 API requests per hour**. With a token, you get **5000/hour**.

### How to Generate a Token:

1. Go to: https://github.com/settings/tokens
2. Click **"Generate new token"** -> **"Tokens (classic)"**
3. Name it: `ModForge`
4. Set expiration: `90 days` (or no expiration)
5. Select scopes:
   - **`public_repo`** (read public repositories)
   - That's it! No other permissions needed.
6. Click **"Generate token"**
7. **Copy the token immediately** (you won't see it again!)
8. Save it to a file: `echo "ghp_xxx..." > github_token.txt`

---

## Usage

### Compile Mods (main command):
```bash
modforge compile repos.txt \
  --mc-version 1.21.10 \
  --loader neoforge \
  --loader-version 64 \
  --github-token "$(cat github_token.txt)"
```

### Compile and Install to Instance:
```bash
modforge compile repos.txt \
  --mc-version 1.21.10 \
  --loader neoforge \
  --loader-version 64 \
  --instance "/path/to/minecraft/instance" \
  --github-token "$(cat github_token.txt)"
```

### Compile and Test in Docker:
```bash
modforge compile repos.txt \
  --mc-version 1.21.10 \
  --loader neoforge \
  --loader-version 64 \
  --docker-test \
  --github-token "$(cat github_token.txt)"
```

### Search Without Compiling:
```bash
modforge search "Create" \
  --mc-version 1.21.10 \
  --loader neoforge \
  --github-token "$(cat github_token.txt)"
```

### Check Status:
```bash
modforge status
```

### Save Report:
```bash
modforge compile repos.txt \
  --mc-version 1.21.10 \
  --loader neoforge \
  --loader-version 64 \
  --output-report compilation_report.txt \
  --github-token "$(cat github_token.txt)"
```

> **Legacy usage:** `python mod_auto_compiler.py ...` still works but is deprecated. Use `modforge compile` instead.

---

## Repository File Format

The `repos.txt` file contains one repository URL per line.

### Option 1: Base URL (Automatic Branch Detection)
```
https://github.com/PepperCode1/Continuity
https://github.com/mezz/JustEnoughItems
https://github.com/Creators-of-Create/Create
```

The script will automatically find the best matching branch for your target version.

### Option 2: URL with Branch (Specific Branch)
```
https://github.com/PepperCode1/Continuity/tree/1.21.10/dev
https://github.com/mezz/JustEnoughItems/tree/mc-1.21.10
https://github.com/Creators-of-Create/Create/tree/neoforge-1.21.10
```

The script will use only the specified branch.

### Comments:
```
# This is a comment
# https://github.com/disabled/repo  <- This will be ignored

https://github.com/active/repo
```

---

## How It Works

### Pipeline
For each repository in your list, ModForge runs:

1. **Modrinth Check** - Search for a pre-compiled JAR on Modrinth first (skip compilation if found)
2. **Fork Discovery** - Search GitHub for compatible forks and independent ports
3. **Branch Scoring** - Rank branches by version match, loader match, and freshness
4. **Pre-validation** - Check `gradle.properties` via GitHub API before cloning (saves time)
5. **Cross-loader Fallback** - If no NeoForge match, try Fabric forks via Sinytra Connector
6. **Compile** - Clone, run `gradlew build`, validate output JAR
7. **Multi-pass Dependencies** - Retry failed mods with mavenLocal() after other mods succeed
8. **Docker Testing** (opt-in) - Boot a headless MC server to verify mods load correctly

---

## Command-Line Reference

### `modforge compile`

| Argument | Required | Description | Example |
|----------|----------|-------------|---------|
| `repos_file` | Yes | Text file with repository URLs | `repos.txt` |
| `--mc-version`, `-m` | Yes | Minecraft version | `1.21.10` |
| `--loader`, `-l` | Yes | Mod loader type | `neoforge`, `forge`, `fabric` |
| `--loader-version`, `-lv` | Yes | Loader version | `64` |
| `--instance`, `-i` | No | Minecraft instance path | `/path/to/instance` |
| `--output-dir`, `-o` | No | Output directory (default: `out`) | `build/mods` |
| `--github-token`, `-t` | No | GitHub API token | `ghp_xxxxx` |
| `--strict` | No | Require exact MC version match | |
| `--no-cross-loader` | No | Disable Sinytra Connector fallback | |
| `--docker-test` | No | Test mods in Docker server | |
| `--docker-timeout` | No | Docker timeout seconds (default: 180) | `300` |
| `--output-report` | No | Save report to file | `report.txt` |
| `--log-file` | No | Write log to file | `modforge.log` |
| `--no-share` | No | Skip anonymous data sharing | |

### `modforge search`

| Argument | Required | Description | Example |
|----------|----------|-------------|---------|
| `query` | Yes | Mod name to search | `Create` |
| `--mc-version`, `-m` | Yes | Minecraft version | `1.21.10` |
| `--loader`, `-l` | No | Mod loader (default: neoforge) | `fabric` |
| `--github-token`, `-t` | No | GitHub API token | `ghp_xxxxx` |

### `modforge status`

No arguments. Shows version, config, cached loaders, and known NeoForge versions.

---

## Troubleshooting

### "gradle.properties not found"
**Cause:** The branch doesn't have Gradle build files.  
**Solution:** Try a different branch or check if the mod uses a different build system.

### "Compilation timeout (>10 minutes)"
**Cause:** The mod is very large or your system is slow.  
**Solution:** Compile manually with `./gradlew build` to see what's wrong.

### "GitHub API rate limit exceeded"
**Cause:** You've made >60 requests in an hour without a token.  
**Solution:** Use `--github-token` with a GitHub Personal Access Token.

### "minecraft_version is X, expected Y"
**Cause:** The branch is for a different Minecraft version.  
**Solution:** This is expected - the script will try the next branch automatically.

### "No JAR file found in build/libs"
**Cause:** Compilation succeeded but didn't produce a JAR.  
**Solution:** Check the build.gradle - some mods use unusual output paths.

### "JAR declares incompatible MC version"
**Cause:** The mods.toml has wrong Minecraft version dependency.  
**Solution:** The branch is misconfigured - try another branch.

---

## Advanced Usage

### Multiple Minecraft Versions
Run the compile command multiple times with different `--mc-version` and `--instance` arguments:

```bash
# For 1.21.10
modforge compile repos.txt -m 1.21.10 -l neoforge -lv 64 -i "instances/NeoCreate_1.21.10"

# For 1.21.11
modforge compile repos.txt -m 1.21.11 -l neoforge -lv 65 -i "instances/NeoCreate_1.21.11"
```

### Partial Repository Lists
You can split your repositories into multiple files:

```bash
# Essential mods
modforge compile essential_mods.txt -m 1.21.10 -l neoforge -lv 64

# Optional mods
modforge compile optional_mods.txt -m 1.21.10 -l neoforge -lv 64
```

---

## Contributing

Found a bug? Have a feature request?

1. Fork the repository
2. Create a feature branch
3. Submit a pull request

Issues: https://github.com/juanzab/ModForge/issues

---

## License

MIT License - Feel free to use, modify, and distribute.

---

## Tips

1. **Start with a small repository list** to test ModForge
2. **Use GitHub tokens** to avoid rate limits
3. **Use `modforge search`** to check Modrinth and GitHub before compiling
4. **Check the report** to see which mods failed and why
5. **Use `--docker-test`** to verify mods actually load in a server
6. **Keep your repos.txt updated** as new forks become available
