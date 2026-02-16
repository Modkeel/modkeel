# VHS tape: modforge compile demo
# Run with: vhs demo/compile.tape
# Requires: modforge installed, github_token.txt, repos.txt with 2-3 small mods
# Note: This runs a real compilation and may take several minutes.

Output demo/compile.gif
Set Theme "Dracula"
Set FontSize 14
Set Width 1200
Set Height 800
Set Padding 20
Set TypingSpeed 50ms

Type "cat repos.txt"
Enter
Sleep 2s

Type "modforge compile repos.txt -m 1.21.10 -l neoforge -lv 64 -t $(cat github_token.txt)"
Enter
Sleep 60s
