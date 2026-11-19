#!/bin/bash
# Pull the latest version from GitHub
cd "$(dirname "$0")" && git pull --ff-only && echo "Updated. Restart DUCK AI Chatter." ; read -p "Enter to close"
