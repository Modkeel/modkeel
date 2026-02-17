# VHS tape: modforge get demo
# Run with: vhs demo/get.tape
# Shows the one-command flow: check Modrinth → download mod + deps → done.
# Requires: modforge installed (pip install -e .)

Output demo/get.gif
Set Theme "Dracula"
Set FontSize 14
Set Width 1200
Set Height 600
Set Padding 20
Set TypingSpeed 50ms

# Get JEI in one step - downloads from Modrinth automatically
Type "modforge get 'JEI' -m 1.21.1 -l neoforge"
Enter
Sleep 12s
