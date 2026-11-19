# DUCK AI Chatter

OnlyFans AI chatter for macOS (DUCK TRAFF). The app checks fans through the OnlyMonster API every 30 s - 5 min;
replies are written by Claude (your Claude subscription) through the MCP server `duck-chatter`.
Features: fans CRM, follow-ups, PPV from the OnlyMonster Vault, broadcasts, daily Telegram report, fan simulator.

## Install on a new Mac
1. Install Claude Code and log in with your subscription. Install the GitHub CLI and log in: `brew install gh && gh auth login`.
2. `gh repo clone producklest-dot/duck-ai-chatter ~/Apps/duck-ai-chatter`
3. Open `~/Apps/duck-ai-chatter/DUCK AI Chatter.app` (first start installs components, 1-2 min;
   if macOS blocks it: right-click -> Open). Optionally drag it to the Dock.
4. In the app: Settings -> OnlyMonster key (+ Telegram), Models -> Load my models, paste each model's voice.
5. Home -> "Connect Claude", restart Claude once, then "Copy command" and paste it into Claude.

## Update
Double-click `update.command` (or `git pull`), then restart the app.

Keys stay in local `config.json` (not in git). App log: `~/Library/Logs/duck-ai-chatter.log`.
