# VHS tape: modforge compile demo
# Run with: vhs demo/compile.tape
# Shows the compile panel with banner, repo list, and initial pipeline output.
# Uses a small demo repos file for a quick demo.
# Requires: modforge installed (pip install -e .), github_token.txt in project root
# Pre-req: run once to create ~/.modforge/config.toml (avoids interactive prompt)

Output demo/compile.gif
Set Theme "Dracula"
Set FontSize 14
Set Width 1200
Set Height 800
Set Padding 20
Set TypingSpeed 50ms

# Show the repos file
Type "cat demo/demo_repos.txt"
Enter
Sleep 2s

# Run compile with token and --no-share to keep output clean
Type "modforge compile demo/demo_repos.txt -m 1.21.1 -l neoforge -lv 21.1.94 -t $(cat github_token.txt) --no-share"
Enter
Sleep 30s
