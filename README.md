# Minecraft Mod Auto-Compiler

Automatically detects, compiles, and installs Minecraft mods from GitHub repositories for specific versions and mod loaders.

## 🎯 Purpose

This tool solves a specific problem in the Minecraft modding community: **getting mods for intermediate patch versions** (like 1.21.10) that official mod authors don't support.

### The Problem:
- Mojang releases Minecraft 1.21.10 (minor patch)
- NeoForge/Forge updates quickly
- **Major mods (Create, JEI, etc.) skip patch versions** and only update for major releases (1.20.1 → 1.21.1)
- Community creates **unofficial forks** with 1.21.10 support
- These forks exist only as **GitHub branches**, not on Modrinth/CurseForge
- You have to manually find, clone, compile, and validate each one

### The Solution:
This script **automatically**:
1. ✅ Finds compatible branches from community forks
2. ✅ Validates compatibility before compiling
3. ✅ Compiles mods from source
4. ✅ Verifies the compiled JAR works
5. ✅ Installs directly to your Minecraft instance
6. ✅ Generates detailed reports of success/failures

---

## 📋 Requirements

### System Requirements:
- **Python 3.8+**
- **Git** (must be in PATH)
- **Java Development Kit (JDK) 17 or 21** (required for Gradle compilation)

### Python Dependencies:
```bash
pip install requests toml
```

### Verify Requirements:
```bash
# Check Python
python --version  # Should be 3.8+

# Check Git
git --version

# Check Java
java -version  # Should be 17 or 21

# Check Gradle (will be downloaded automatically by gradlew)
```

---

## 🚀 Installation

1. **Download the script:**
```bash
git clone <this-repo>
cd mod-auto-compiler
```

2. **Install Python dependencies:**
```bash
pip install requests toml
```

3. **Create your repository list:**
```bash
# Copy the example
cp repos.txt my_mods.txt

# Edit with your favorite editor
nano my_mods.txt  # or notepad on Windows
```

---

## 🔑 GitHub API Token (Recommended)

Without a token, you're limited to **60 API requests per hour**. With a token, you get **5000/hour**.

### How to Generate a Token:

1. Go to: https://github.com/settings/tokens
2. Click **"Generate new token"** → **"Tokens (classic)"**
3. Name it: `Mod Auto-Compiler`
4. Set expiration: `90 days` (or no expiration)
5. Select scopes:
   - ✅ **`public_repo`** (read public repositories)
   - That's it! No other permissions needed.
6. Click **"Generate token"**
7. **Copy the token immediately** (you won't see it again!)
8. Save it somewhere safe

### Token Format:
```
ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## 📝 Usage

### Basic Command:
```bash
python mod_auto_compiler.py \
  --mc-version 1.21.10 \
  --loader neoforge \
  --loader-version 64 \
  --instance "C:\Users\Juan\AppData\Roaming\.minecraft\instances\NeoCreate_1.21.10" \
  repos.txt
```

### With GitHub Token (Recommended):
```bash
python mod_auto_compiler.py \
  --mc-version 1.21.10 \
  --loader neoforge \
  --loader-version 64 \
  --instance "C:\Users\Juan\AppData\Roaming\.minecraft\instances\NeoCreate_1.21.10" \
  --github-token ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  repos.txt
```

### With Report Output:
```bash
python mod_auto_compiler.py \
  --mc-version 1.21.10 \
  --loader neoforge \
  --loader-version 64 \
  --instance "C:\Users\Juan\AppData\Roaming\.minecraft\instances\NeoCreate_1.21.10" \
  --github-token ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --output-report compilation_report.txt \
  repos.txt
```

---

## 📁 Repository File Format

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

## 🔍 How It Works

### 1. Branch Detection & Scoring
The script analyzes all branches in a repository and scores them:

**High Score (Highest Priority):**
- Exact version match: `1.21.10` in branch name → +1000 points
- Partial version match: `1.21` in branch name → +500 points
- Loader mention: `neoforge` in branch name → +200 points

**Medium Score:**
- Common patterns: `mc-1.21` → +100 points
- Development branches: `dev`, `feature` → +50 points

**Low Score:**
- Main/master branches → +10 points

**Penalties:**
- Branches older than 6 months → -100 points

### 2. Validation Steps (Per Branch)
For each candidate branch, the script:

1. **Clones the repository** (shallow clone, single branch)
2. **Validates `gradle.properties`**:
   - Checks `minecraft_version = 1.21.10`
   - Checks `neoforge_version` exists (for NeoForge)
3. **Validates `build.gradle`**:
   - Confirms target version is mentioned
4. **Compiles with Gradle**:
   - Runs `./gradlew build --no-daemon`
   - Timeout: 10 minutes
5. **Validates compiled JAR**:
   - Extracts and parses `mods.toml`
   - Verifies Minecraft version dependency
   - Checks JAR size (>10KB minimum)
6. **Installs to mods folder** if all checks pass

### 3. Fallback Strategy
If the first branch fails, the script tries the next one in order of score. It continues until:
- ✅ A branch compiles successfully, OR
- ❌ All relevant branches have been tried

---

## 📊 Output & Reports

### Console Output:
```
================================================================================
📦 Processing: https://github.com/PepperCode1/Continuity
================================================================================
  📍 Repository: PepperCode1/Continuity
  ⭐ Stars: 1234 | 🍴 Forks: 56
  🔍 Fetching branches...
  📊 Found 15 branches
  🎯 Identified 5 relevant branches

  🌿 Trying branch [1/5]: 1.21.10/dev (score: 1200)
    📥 Cloning...
    🔍 Validating gradle.properties...
    ✅ gradle.properties validation passed
    🔍 Validating build.gradle...
    ✅ build.gradle checked (version in properties)
    🔨 Compiling...
    ✅ Compilation successful
    🔍 Validating JAR...
    ✅ JAR validation passed
    📋 Mod: continuity v3.0.0
    💾 Installed to: C:\...\mods\continuity-3.0.0.jar

  ✅ SUCCESS: continuity v3.0.0 from branch '1.21.10/dev'
```

### Final Report:
```
================================================================================
📊 COMPILATION REPORT
================================================================================

✅ Successful: 8/10
❌ Failed: 2/10

--------------------------------------------------------------------------------
✅ SUCCESSFULLY COMPILED MODS:
--------------------------------------------------------------------------------

📦 https://github.com/PepperCode1/Continuity
   🌿 Branch: 1.21.10/dev
   📋 Mod: continuity v3.0.0
   💾 JAR: C:\...\mods\continuity-3.0.0.jar

[... more successful mods ...]

--------------------------------------------------------------------------------
❌ FAILED COMPILATIONS:
--------------------------------------------------------------------------------

📦 https://github.com/SomeAuthor/BrokenMod
   ❌ Error: All 3 branches failed compilation/validation

================================================================================
🎯 Target: Minecraft 1.21.10 with Neoforge 64
📁 Install Path: C:\...\mods
================================================================================
```

---

## 🛠️ Command-Line Arguments

| Argument | Required | Description | Example |
|----------|----------|-------------|---------|
| `repos_file` | ✅ Yes | Text file with repository URLs | `repos.txt` |
| `--mc-version` | ✅ Yes | Minecraft version | `1.21.10` |
| `--loader` | ✅ Yes | Mod loader type | `neoforge`, `forge`, `fabric` |
| `--loader-version` | ✅ Yes | Loader version | `64` |
| `--instance` | ✅ Yes | Minecraft instance path | `C:\...\instances\MyInstance` |
| `--github-token` | ❌ No | GitHub API token | `ghp_xxxxx` |
| `--output-report` | ❌ No | Report file path | `report.txt` |

---

## ⚠️ Troubleshooting

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

## 🎓 Advanced Usage

### Custom Branch Naming Patterns
If you're working with forks that use unusual branch names, you can modify the `score_branch()` function in the script to match your patterns.

### Multiple Minecraft Versions
Run the script multiple times with different `--mc-version` and `--instance` arguments:

```bash
# For 1.21.10
python mod_auto_compiler.py --mc-version 1.21.10 --instance "instances/NeoCreate_1.21.10" repos.txt

# For 1.21.11
python mod_auto_compiler.py --mc-version 1.21.11 --instance "instances/NeoCreate_1.21.11" repos.txt
```

### Partial Repository Lists
You can split your repositories into multiple files:

```bash
# Essential mods
python mod_auto_compiler.py [...] essential_mods.txt

# Optional mods
python mod_auto_compiler.py [...] optional_mods.txt
```

---

## 🤝 Contributing

Found a bug? Have a feature request? Want to improve branch detection heuristics?

1. Fork the repository
2. Create a feature branch
3. Submit a pull request

---

## 📜 License

MIT License - Feel free to use, modify, and distribute.

---

## 🙏 Acknowledgments

- Minecraft modding community for creating amazing mods
- GitHub for providing the API
- NeoForge/Forge teams for maintaining the mod loader

---

## 💡 Tips

1. **Start with a small repository list** to test the script
2. **Use GitHub tokens** to avoid rate limits
3. **Check the report** to see which mods failed and why
4. **Manually compile stubborn mods** if the script can't handle them
5. **Keep your repos.txt updated** as new forks become available

---

**Happy Modding! 🎮**
