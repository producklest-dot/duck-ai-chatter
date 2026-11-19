#!/bin/bash
# Connect DUCK AI Chatter to Claude (run once)
cd "$(dirname "$0")"
claude mcp add --scope user duck-chatter -- "$HOME/.duck-ai-chatter-venv/bin/python" "$(pwd)/mcp_server.py" && echo "Done. Restart Claude."
read -p "Enter to close"
