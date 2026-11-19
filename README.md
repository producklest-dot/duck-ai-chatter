# DUCK AI Chatter

OnlyFans AI chatter for macOS. The app checks fans through the OnlyMonster API every 30 s - 5 min;
replies are written by Claude (your Claude subscription) through the MCP server `duck-chatter`.

## Install
1. `git clone https://github.com/producklest-dot/duck-ai-chatter.git ~/Apps/duck-ai-chatter`
2. Open `DUCK AI Chatter.app` (first start installs components, 1-2 min; if macOS blocks it: right-click -> Open).
3. Run `install-mcp.command` once, restart Claude.
4. In the app: Keys -> OnlyMonster key + Telegram, Models -> Load accounts, set a voice file.
5. In Claude paste the command from "Copy step 2" (`/loop ...`).

## Update
Double-click `update.command` (or `git pull`), then restart the app.

## Test without fans
"Send fake fan message" creates a fan for "Demo model" - always test mode, nothing goes to OnlyFans.

Rules enforced by the app: no tease before the 3rd reply, stop words -> human, "are you a bot?" -> human,
wants free -> mute, hot lead -> Telegram + 48 h silence. Keys stay in local `config.json` (not in git).
