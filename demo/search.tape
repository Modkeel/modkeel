# VHS tape: modforge search demo
# Run with: vhs demo/search.tape
# Requires: modforge installed (pip install -e .), github_token.txt in project root
# Pre-req: run once to create ~/.modforge/config.toml (avoids interactive prompt)

Output demo/search.gif
Set Theme "Dracula"
Set FontSize 14
Set Width 1200
Set Height 800
Set Padding 20
Set TypingSpeed 50ms

# Search for JEI on MC 1.21.1 - shows Modrinth match + pre-filtered GitHub forks
Type "modforge search 'JEI' -m 1.21.1 -l neoforge -t $(cat github_token.txt)"
Enter
Sleep 30s
