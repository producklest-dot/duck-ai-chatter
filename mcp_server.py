"""DUCK AI Chatter - MCP server (stdio). Lets a Claude session (subscription) read the inbox,
write replies and control the chatter daemon running on 127.0.0.1:8796."""
import json
import os
import urllib.request

from mcp.server.fastmcp import FastMCP

BASE = f"http://127.0.0.1:{os.environ.get('DUCK_CHATTER_PORT', '8796')}"
mcp = FastMCP("duck-chatter")


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.URLError as e:
        return {"error": f"DUCK AI Chatter is not running ({e}). Start it: open ~/Apps/duck-ai-chatter/START.command"}


@mcp.tool()
def status() -> dict:
    """Daemon state: running, poll interval, inbox size, today's stats, profiles, recent log."""
    return _req("GET", "/api/status")


@mcp.tool()
def get_inbox(limit: int = 10) -> list:
    """Fans waiting for a reply (oldest first): profile_id, fan_id, incoming text, chat history,
    stage, turns, heat, notes, test_mode. Process each with send_reply / skip / handoff."""
    return _req("GET", f"/api/inbox?limit={limit}")


@mcp.tool()
def get_voice(profile_id: str) -> dict:
    """The model's voice file (tone of voice) and the reply playbook. Read once per profile before replying."""
    return _req("GET", f"/api/tone?profile_id={profile_id}")


@mcp.tool()
def send_reply(profile_id: str, fan_id: str, messages: list[str], heat: int = 3,
               notes: str = "", wants_free: bool = False) -> dict:
    """Send 1-3 short, friendly, NON-sexual messages to a fan (log only in test mode).
    Safe-chat mode: sexual, sales, money and age topics are blocked by the program.
    If it returns rejected=true, call handoff for that fan instead of rewriting around the filter."""
    return _req("POST", "/api/reply", {"profile_id": profile_id, "fan_id": fan_id, "messages": messages,
                                       "stage": "chat", "heat": heat, "notes": notes, "wants_free": wants_free})


@mcp.tool()
def skip(profile_id: str, fan_id: str, reason: str = "") -> dict:
    """Remove a fan from the inbox without replying."""
    return _req("POST", "/api/skip", {"profile_id": profile_id, "fan_id": fan_id, "reason": reason})


@mcp.tool()
def handoff(profile_id: str, fan_id: str, reason: str) -> dict:
    """Pass the chat to a human: bot goes silent there for 48h, Telegram alert is sent."""
    return _req("POST", "/api/handoff", {"profile_id": profile_id, "fan_id": fan_id, "reason": reason})


@mcp.tool()
def mute(profile_id: str, fan_id: str, on: bool = True) -> dict:
    """Silence (on=true) or un-silence (on=false) a fan."""
    return _req("POST", "/api/mute", {"profile_id": profile_id, "fan_id": fan_id, "on": on})


@mcp.tool()
def fan_info(profile_id: str, fan_id: str) -> dict:
    """Stored stage, turns, heat, notes, pause state for a fan."""
    return _req("GET", f"/api/fan?profile_id={profile_id}&fan_id={fan_id}")


@mcp.tool()
def set_running(on: bool) -> dict:
    """Start (true) or stop (false) polling OnlyMonster."""
    return _req("POST", "/api/run", {"on": on})


@mcp.tool()
def check_now() -> dict:
    """Poll OnlyMonster right now instead of waiting for the interval."""
    return _req("POST", "/api/check_now", {})


@mcp.tool()
def set_test_mode(profile_id: str, on: bool) -> dict:
    """Test mode on = replies only go to the log, nothing is sent to fans."""
    return _req("POST", "/api/test_mode", {"profile_id": profile_id, "on": on})


@mcp.tool()
def set_poll_interval(seconds: int) -> dict:
    """How often the daemon checks fans (min 15 s)."""
    return _req("POST", "/api/config", {"poll_seconds": max(15, int(seconds))})


@mcp.prompt()
def work_shift() -> str:
    """One pass of the chatter shift."""
    return ("Run one DUCK AI Chatter pass: call get_inbox. For each fan: get_voice for its profile (once), "
            "read the history, then send_reply with light non-sexual small talk, or handoff when the chat is sexual, "
            "about content/money, or hints the fan may be under 18. "
            "If the inbox is empty, say 'inbox empty' in one line.")


if __name__ == "__main__":
    mcp.run()
