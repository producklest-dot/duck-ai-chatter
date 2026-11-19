#!/bin/bash
# DUCK AI Chatter - start the daemon and open the panel
cd "$(dirname "$0")"
if ! curl -s -o /dev/null http://127.0.0.1:8796/api/status; then
  nohup /Volumes/CLAUDE/2_Apps/_venvs/duck-chatter/bin/python chatter.py >> daemon.out 2>&1 &
  sleep 1
fi
open http://127.0.0.1:8796
