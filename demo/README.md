# Demo GIFs

This directory contains [VHS](https://github.com/charmbracelet/vhs) tape scripts for generating demo GIFs.

## Prerequisites

Install VHS:

```bash
# macOS
brew install vhs

# Linux (go install)
go install github.com/charmbracelet/vhs@latest

# Or download from GitHub releases
```

VHS also requires [ttyd](https://github.com/tsl0922/ttyd) and [ffmpeg](https://ffmpeg.org/).

## Generating GIFs

```bash
# Generate all GIFs
vhs demo/status.tape
vhs demo/search.tape
vhs demo/get.tape
vhs demo/compile.tape

# Or generate one at a time
vhs demo/status.tape    # Fast, no token needed
vhs demo/search.tape    # Fast, no token needed (Modrinth only)
vhs demo/get.tape       # Fast, no token needed (downloads from Modrinth)
vhs demo/compile.tape   # Slow, needs saved token and repos.txt
```

## Tape Files

| File | Description | Requirements |
|------|-------------|-------------|
| `status.tape` | Shows `modforge --version` and `modforge status` | None |
| `search.tape` | Smart search: Modrinth found, skips fork search | None |
| `get.tape` | One-command download from Modrinth | None |
| `compile.tape` | Full compilation pipeline | Saved token, `demo_repos.txt` |

## Output

Generated GIFs are saved to `demo/*.gif`. These are referenced in the main README.
