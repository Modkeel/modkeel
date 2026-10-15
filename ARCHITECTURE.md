# Architecture

A map of the Modkeel CLI's code, for people and AI assistants reading it. How to install and use
it is in [README.md](README.md); how to contribute is in [CONTRIBUTING.md](CONTRIBUTING.md).

## The shape

```
commands/   one module per subcommand (Typer + Rich): terminal input and output only
   |
core/       the engine: get, move, add, undo, search as plain functions; progress as typed
   |        events, questions as decisions with safe defaults; serve --stdio speaks it as JSON
   v
resolve.py  walks the sources in order until one delivers a JAR that passes the evidence
   |
sources.py -> modrinth.py, curseforge.py, github.py, pipeline.py (clone + Gradle)
evidence.py -> linkage.py, memberlink.py, mixinscan.py, docker.py (cheapest check first)
```

The same engine serves the terminal and the desktop app ([Modkeel/app](https://github.com/Modkeel/app)
starts `modkeel serve --stdio`), so both always do the same thing.

## How a mod gets resolved

For a mod, a Minecraft version and a loader, `resolve.py` tries each strategy in `SOURCE_ORDER`
(`sources.py`); the first JAR that passes wins, and the trail of what was tried is printed and
kept:

1. **official**: the author's build on Modrinth for that version (also the other loader's build
   when a bridge like Sinytra Connector makes it run).
2. **curseforge**: the author's build on CurseForge, looked up through api.modkeel.com, which
   holds the API key so the CLI never does; skipped while that service is off.
3. **official_source**: the author's own branch for that exact version, prebuilt or compiled
   (`compile` only, since it needs the repository).
4. **older_official**: the author's build for an older version, kept only if its bytecode still
   links against the target (classes, members, mixin targets). Never with `--strict`.
5. **fork**: forks and community ports on GitHub, scored for trust and version match, checked
   remotely (`gradle.properties`) before any clone, then built.
6. **relaxed_official**: an older build refused only by its declared version range: the range is
   widened, and the JAR is kept only if linkage passes and a real server boots with it.

Each candidate goes through `evidence.py` in cost order (metadata, linkage, mixins, server boot):
every check is passed, failed or not run, and each strategy says which ones it requires.
When part of a pack has nothing for the target, `target.py` proposes the nearest Minecraft
version where more of it runs, and asks first (`--fallback ask|auto|never`).

Before compiling anything, `prebuild.py` skips builds whose outcome is already known: a release
or CI artifact that already exists (checked by `linkage.py`), dead Maven repositories, a Gradle
and JDK mismatch, or source that references Minecraft symbols missing in the target
(`symbols.py`, `mappings.py`, `javascan.py`).

## modkeel/ (the package)

| Module | What it does |
|--------|--------------|
| `cli.py` | Entry point (`modkeel = modkeel.cli:app`): builds the Typer app and registers `commands/` |
| `commands/` | `get`, `search`, `move`, `instances`, `compile`, `recommend`, `status`, `login`, `token`, `serve`; `_shared.py` holds the console, token resolution and shared options |
| `core/engine.py` | `get_mod`, `retarget_pack`: a run's flow without terminal I/O |
| `core/move.py` | A mods folder moved to another version: JARs identified by hash, each one delivered, reused or unknown |
| `core/install.py` | A mod into a launcher instance through a staging folder; skips what is there, records each add in `history.jsonl`, `undo` restores replaced files |
| `core/catalog.py` | Modrinth search hits with their status on a version; an instance's mods with no build for its version |
| `core/events.py`, `decisions.py`, `text.py` | Typed progress events, questions with safe defaults, the terminal rendering of both |
| `core/wire.py` | The JSON-lines protocol of `modkeel serve --stdio` |
| `resolve.py`, `sources.py` | The resolver and the strategies above; a new way to find JARs is one class and one entry |
| `evidence.py` | The checks, cheapest first |
| `target.py` | The nearest Minecraft version where more of a pack runs |
| `relax.py` | Widens a JAR's declared Minecraft range, strips signatures and marks it |
| `modrinth.py` | Modrinth search, download, dependencies; waits out its rate limit |
| `curseforge.py` | CurseForge by file fingerprint, through api.modkeel.com |
| `github.py`, `validation.py`, `buildinfo.py` | Forks, branches, contributor trust; branch scoring; versions and loaders read from a branch's build files |
| `pipeline.py`, `build.py`, `jdk.py` | Clone and Gradle build with the mod's own wrapper, JAR selection and validation; the JDK the target needs (installed, or Temurin downloaded once) |
| `prebuild.py` | The pre-build gate |
| `linkage.py`, `memberlink.py`, `mixinscan.py` | Bytecode checks: does a JAR's code still link against the target version (classes, methods, fields), and do its mixins still apply |
| `symbols.py`, `mappings.py`, `javascan.py` | Minecraft's symbol table from Mojang mappings (and Fabric intermediary); imports and mixin targets read from Java source |
| `docker.py` | A real server (`itzg/minecraft-server`) booted with the delivered JARs |
| `instances.py`, `newinstance.py` | The player's launcher instances (Prism, PolyMC, Modrinth App, CurseForge, ATLauncher, GDLauncher, FTB App, official launcher), read only; a moved pack as a new Prism instance beside the old one |
| `scanner.py`, `recommend.py` | A mods folder's JAR metadata; version and loader recommendations for it |
| `ghauth.py`, `config.py` | GitHub sign-in by device flow (public client id, no scopes); settings in `~/.modkeel/config.toml` |
| `loaders.py`, `version.py`, `models.py` | Every loader-specific fact in one place; Maven and Fabric version ranges; data classes |
| `crowdsource.py` | Anonymous build reports; off (`MODKEEL_API_URL` is empty in `constants.py`) |

`mod_auto_compiler.py` is a deprecated shim that delegates to the package.

## tests/

pytest, one file per area (`test_resolve.py`, `test_evidence.py`, `test_wire.py`,
`test_install.py`, ...). Network calls are patched; `python -m pytest tests/ -q`.

## What it connects to

GitHub (API, codeload, raw), Modrinth, Mojang and loader metadata (launchermeta, Fabric and
NeoForge Maven), Prism Launcher's metadata (only for a new instance), Adoptium (only to download
a JDK) and api.modkeel.com for CurseForge lookups (it receives file fingerprints). Nothing else,
and no analytics.

## Where to start reading

- `modkeel get`: `commands/get.py` -> `core/engine.py` `get_mod` -> `resolve.py` -> `sources.py`.
- `modkeel move`: `commands/move.py` -> `core/move.py`.
- The desktop app's side: `core/wire.py`.
