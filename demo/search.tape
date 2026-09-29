# VHS tape: modkeel search demo
# Run with: vhs demo/search.tape
# Shows smart search: Modrinth found → skips fork search entirely.
# Uses --no-prompt to avoid interactive download prompt in GIF.
# Requires: modkeel installed (pip install -e .)

Output demo/search.gif
Set Shell "pwsh"
Set Theme "Dracula"
Set FontSize 14
Set Width 1200
Set Height 600
Set Padding 20
Set TypingSpeed 50ms

# Search for JEI - found on Modrinth, no fork search needed
Type "modkeel search 'JEI' -m 1.21.1 -l neoforge --no-prompt"
Enter
Sleep 8s
