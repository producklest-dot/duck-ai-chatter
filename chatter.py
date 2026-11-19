#!/usr/bin/env python3
"""DUCK AI Chatter - daemon.

Polls OnlyMonster every N seconds, puts new fan messages into an inbox and
serves a local web UI + JSON API on 127.0.0.1:8796. It writes NOTHING by
itself: replies come from a Claude session (your subscription) through the
MCP server (mcp_server.py), and this daemon enforces the safety rules before
anything reaches a fan.
"""
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(APP_DIR, "config.json")
STATE_PATH = os.path.join(APP_DIR, "state.json")
LOG_PATH = os.path.join(APP_DIR, "chatter.log")
PORT = int(os.environ.get("DUCK_CHATTER_PORT", "8796"))

OM_BASE = "https://omapi.onlymonster.ai"
HISTORY = 20
PAUSE_HOURS = 48
DEAD_HOURS = 24
MIN_TURNS_BEFORE_TEASE = 2
CASCADE_GAP = (2, 6)
STAGES = ["greeting", "rapport", "flirt", "tease"]
TEASE_RE = re.compile(r"(see|look at|show you|check out)\s+(how|what)?\s*i\s+(look|am wearing|'m wearing|look like|got on)"
                      r"|how i look|what i'?m wearing|see me (right )?now", re.I)
DEFAULT_HANDOFF = ["meet up", "meet me", "phone number", "whatsapp", "telegram", "snapchat", "my address",
                   "police", "underage", "refund", "chargeback", "lawyer", "kill myself", "suicide"]
AI_QUESTION_RE = re.compile(r"\b(are|r) (you|u) (a |an )?(bot|ai|robot|real( person)?|human|chatgpt|automated)\b"
                            r"|\bis this (a |an )?(bot|ai|real)\b|\btalking to (a |an )?(bot|ai)\b", re.I)

PLAYBOOK = """You reply to OnlyFans fans on behalf of the creator, in her voice (voice file below).
Goal: warm the fan up and, after at least {min_turns} replies, ask THE QUESTION ("Want to see how I look today?").
After that question a human takes over. You never sell, never send content, never talk prices.

Funnel: greeting -> rapport (max 2 exchanges) -> flirt -> tease (THE QUESTION).
If the fan himself asks for content or the price - tease right away (buying_intent=true).

Style: a cascade of 1-3 SEPARATE short messages (3-12 words), 0-2 emojis total, end with a hook or question,
match his language. No "wait, I'm getting ready". Never promise meetings, calls, other apps, phone numbers.
Free content / refuses to pay -> one short playful line, wants_free=true.

Honesty: never claim to be a human and never deny being AI. If the fan sincerely asks whether he is talking
to a bot / AI / a real person, do NOT answer it yourself - call handoff(reason="asked if AI").

Send with send_reply(profile_id, fan_id, messages, stage, heat, notes, asked_to_see, buying_intent, wants_free).
If send_reply returns rejected=true, read "reason", rewrite and call it again once.
"""


def now():
    return datetime.now().strftime("%H:%M:%S")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        out = dict(default)
        out.update(d)
        return out
    except Exception:
        return dict(default)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def http_json(method, url, headers=None, body=None, timeout=60):
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw[:500]}
    except Exception as e:
        return 0, {"message": str(e)}


def clean(t):
    return re.sub(r"<[^>]+>", "", t or "").strip()


def read_tone(path):
    if not path or not os.path.exists(path):
        return ""
    if path.lower().endswith(".docx"):
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml)).strip()
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def looks_like_json(s):
    s = s.strip()
    return s.startswith("{") or s.startswith("[") or '"messages"' in s


# --------------------------------------------------------------------------- OnlyMonster
class OnlyMonster:
    def __init__(self, token):
        self.token = token
        self._last = {}
        self._lock = threading.Lock()

    def _call(self, method, path, body=None, aid=None):
        if aid is not None:
            with self._lock:
                wait = 1.1 - (time.time() - self._last.get(aid, 0))
                if wait > 0:
                    time.sleep(wait)
                self._last[aid] = time.time()
        status, data = 0, {}
        for attempt in range(4):
            status, data = http_json(method, OM_BASE + path, {"x-om-auth-token": self.token}, body)
            if status == 429:
                time.sleep(2 + attempt * 2)
                continue
            break
        return status, data

    def _get(self, path, aid=None):
        status, data = self._call("GET", path, aid=aid)
        if status != 200:
            raise RuntimeError(f"OnlyMonster {status} ({path.split('?')[0]}): {data.get('message', data)}")
        return data

    def accounts(self):
        return self._get("/api/v0/accounts?limit=100").get("accounts", [])

    def fans(self, aid, limit):
        return self._get(f"/api/v0/accounts/{aid}/fans?limit={limit}", aid).get("fan_ids", [])

    def messages(self, aid, chat_id, limit):
        return self._get(f"/api/v0/accounts/{aid}/chats/{chat_id}/messages?limit={limit}&order=desc", aid).get("items", [])

    def send(self, aid, chat_id, text):
        body = {"text": text, "price": None, "locked_text": False, "media": []}
        return self._call("POST", f"/api/v0/accounts/{aid}/chats/{chat_id}/messages", body=body, aid=aid)


# --------------------------------------------------------------------------- core
class Chatter:
    def __init__(self):
        self.cfg = load_json(CFG_PATH, {"om_token": "", "tg_token": "", "tg_chat": "", "poll_seconds": 60,
                                         "fans_per_cycle": 25, "muted": [], "handoff_keywords": [], "profiles": []})
        self.state = load_json(STATE_PATH, {"profiles": {}, "inbox": {}, "stats": {}})
        self.lock = threading.RLock()
        self.logs = []
        self.running = False
        self.wake = threading.Event()
        self.last_poll = 0
        self.last_claude = 0
        self.next_poll = 0
        self.status = "stopped"
        threading.Thread(target=self.loop, daemon=True).start()

    # ---- persistence / log
    def save(self):
        with self.lock:
            save_json(STATE_PATH, self.state)

    def save_cfg(self):
        with self.lock:
            save_json(CFG_PATH, self.cfg)

    def log(self, msg):
        line = f"{now()} {msg}"
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-400:]
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(datetime.now().strftime("%Y-%m-%d ") + line + "\n")
        except OSError:
            pass

    def stat(self, k, n=1):
        d = self.state["stats"].setdefault(datetime.now().strftime("%Y-%m-%d"), {})
        d[k] = round(d.get(k, 0) + n, 2)

    def om(self):
        return OnlyMonster(self.cfg.get("om_token", ""))

    def profile(self, pid):
        for p in self.cfg["profiles"]:
            if str(p["id"]) == str(pid):
                return p
        raise ValueError(f"unknown profile {pid}")

    def pstate(self, pid):
        s = self.state["profiles"].setdefault(str(pid), {})
        for k, v in (("seen", {}), ("fans", {}), ("dead", {}), ("initialized", False)):
            s.setdefault(k, v)
        return s

    def fan(self, pid, fid):
        return self.pstate(pid)["fans"].setdefault(str(fid), {"stage": "greeting", "notes": "", "turns": 0,
                                                              "paused": False, "paused_at": 0, "heat": 0})

    def is_muted(self, pid, fid):
        return f"{pid}:{fid}" in self.cfg["muted"] or str(fid) in self.cfg["muted"]

    def is_paused(self, f):
        return f.get("paused") and time.time() - f.get("paused_at", 0) < PAUSE_HOURS * 3600

    def handoff_words(self):
        return [w.lower() for w in (self.cfg.get("handoff_keywords") or DEFAULT_HANDOFF)]

    def tg(self, text):
        tok, chat = self.cfg.get("tg_token"), self.cfg.get("tg_chat")
        if not tok or not chat:
            return
        st, d = http_json("POST", f"https://api.telegram.org/bot{tok}/sendMessage", None,
                          {"chat_id": chat, "text": text, "disable_web_page_preview": True})
        if st != 200:
            self.log(f"Telegram error {st}: {d.get('description', d)}")

    # ---- polling
    def loop(self):
        while True:
            if self.running and time.time() >= self.next_poll:
                try:
                    self.poll_all()
                except Exception as e:
                    self.log(f"Poll error: {e}")
                self.last_poll = time.time()
                self.next_poll = time.time() + max(15, int(self.cfg.get("poll_seconds", 60)))
                self.save()
            if self.running:
                self.status = f"running - next check in {max(0, int(self.next_poll - time.time()))}s"
            self.wake.wait(1)
            self.wake.clear()

    def poll_all(self):
        if not self.cfg.get("om_token"):
            self.log("No OnlyMonster key - open Settings.")
            return
        om = self.om()
        for p in self.cfg["profiles"]:
            if p.get("enabled") and p.get("id"):
                try:
                    self.poll_profile(om, p)
                except Exception as e:
                    self.log(f"[{p['name']}] {e}")

    def poll_profile(self, om, p):
        pid = str(p["id"])
        ps = self.pstate(pid)
        first = not ps["initialized"]
        if first:
            self.log(f"[{p['name']}] first run: remembering current chats, old messages are skipped.")
        for fid in om.fans(pid, int(self.cfg.get("fans_per_cycle", 25))):
            fid = str(fid)
            t0 = ps["dead"].get(fid)
            if t0 and time.time() - t0 < DEAD_HOURS * 3600:
                continue
            try:
                items = om.messages(pid, fid, HISTORY)
            except RuntimeError as e:
                if " 404 " in str(e) or " 403 " in str(e):
                    ps["dead"][fid] = time.time()
                    continue
                raise
            if not items:
                continue
            last = items[0]
            if first:
                ps["seen"][fid] = last["id"]
                continue
            seen = ps["seen"].get(fid)
            if seen is None:
                mine = [m["id"] for m in items if m.get("is_sent_by_me")]
                seen = max(mine) if mine else 0
            new = [m for m in items if m["id"] > seen and not m.get("is_sent_by_me")]
            ps["seen"][fid] = last["id"]
            if new and not last.get("is_sent_by_me"):
                self.on_new(p, fid, items, new)
        if first:
            ps["initialized"] = True

    def on_new(self, p, fid, items, new):
        pid = str(p["id"])
        f = self.fan(pid, fid)
        incoming = " / ".join(clean(m.get("text")) or "[media]" for m in reversed(new))
        self.log(f"[{p['name']}] fan {fid}: {incoming[:160]}")
        if self.is_muted(pid, fid):
            self.log("   muted - skipped")
            return
        if self.is_paused(f):
            self.log("   handed to a human - skipped")
            return
        f["paused"] = False
        low = incoming.lower()
        flags = [k for k in self.handoff_words() if k in low]
        if AI_QUESTION_RE.search(incoming):
            flags.append("asked if AI")
        if flags:
            self.do_handoff(p, fid, f"stop words: {', '.join(flags)}", incoming)
            return
        history = []
        for m in reversed(items[:HISTORY]):
            text = clean(m.get("text"))[:600]
            if m.get("media_count"):
                text = ("[media] " + text).strip()
            if text:
                history.append({"from": "creator" if m.get("is_sent_by_me") else "fan", "text": text,
                                "at": m.get("created_at", "")})
        with self.lock:
            self.state["inbox"][f"{pid}:{fid}"] = {
                "profile_id": pid, "profile": p["name"], "fan_id": fid, "incoming": incoming,
                "history": history, "stage": f["stage"], "turns": f.get("turns", 0), "heat": f.get("heat", 0),
                "notes": f.get("notes", ""), "queued_at": time.time()}

    def do_handoff(self, p, fid, why, incoming=""):
        pid = str(p["id"])
        f = self.fan(pid, fid)
        f["paused"], f["paused_at"] = True, time.time()
        self.state["inbox"].pop(f"{pid}:{fid}", None)
        self.stat("handoff")
        self.log(f"[{p['name']}] HANDOFF fan {fid}: {why}")
        self.tg(f"{p['name']} (@{p.get('username', '')}) - fan {fid} needs a human\nWhy: {why}\n"
                f"Heat: {f.get('heat', 0)}/10\nLast from him: {incoming[:300]}\nNotes: {(f.get('notes') or '-')[:300]}\n"
                f"https://onlyfans.com/my/chats/chat/{fid}")

    # ---- API used by MCP
    def get_inbox(self, limit=10, from_claude=True):
        if from_claude:
            self.last_claude = time.time()
        with self.lock:
            items = sorted(self.state["inbox"].values(), key=lambda x: x["queued_at"])[:limit]
            out = []
            for it in items:
                p = self.profile(it["profile_id"])
                out.append(dict(it, min_turns_before_tease=MIN_TURNS_BEFORE_TEASE,
                                test_mode=bool(p.get("test_mode", True))))
            return out

    def tone(self, pid):
        p = self.profile(pid)
        return {"profile": p["name"], "username": p.get("username", ""), "voice": read_tone(p.get("tone_file", "")),
                "playbook": PLAYBOOK.format(min_turns=MIN_TURNS_BEFORE_TEASE)}

    def send_reply(self, pid, fid, messages, stage="", heat=0, notes="", asked_to_see=False,
                   buying_intent=False, wants_free=False):
        self.last_claude = time.time()
        p = self.profile(pid)
        fid = str(fid)
        f = self.fan(pid, fid)
        key = f"{pid}:{fid}"
        incoming = self.state["inbox"].get(key, {}).get("incoming", "")
        if pid == "demo":
            p["test_mode"] = True
        if self.is_muted(pid, fid) or self.is_paused(f):
            self.state["inbox"].pop(key, None)
            return {"rejected": True, "reason": "fan is muted or handed to a human - do not reply"}
        msgs = [re.sub(r"\s+", " ", str(m)).strip()[:400] for m in (messages or []) if str(m).strip()][:3]
        if not msgs:
            return {"rejected": True, "reason": "no messages"}
        if any(looks_like_json(m) for m in msgs):
            return {"rejected": True, "reason": "a message looks like JSON - send plain text only"}
        bad = [w for w in self.handoff_words() if any(w in m.lower() for m in msgs)]
        if bad:
            return {"rejected": True, "reason": f"messages contain forbidden words {bad} - rewrite"}
        if re.search(r"\b(i'?m|i am) (a )?(real|human|not a bot|not ai)\b", " ".join(msgs), re.I):
            return {"rejected": True, "reason": "never claim to be human - call handoff instead"}
        asked = bool(asked_to_see) or stage == "tease" or any(TEASE_RE.search(m) for m in msgs)
        if asked and f.get("turns", 0) < MIN_TURNS_BEFORE_TEASE and not buying_intent:
            return {"rejected": True, "reason": f"too early for THE QUESTION: only {f.get('turns', 0)} replies sent, "
                    f"need {MIN_TURNS_BEFORE_TEASE}. Rewrite as small talk / light flirt, no invitation to see her."}
        if stage in STAGES:
            f["stage"] = stage
        if notes:
            f["notes"] = (f.get("notes", "") + " " + str(notes)).strip()[-1200:]
        try:
            f["heat"] = max(1, min(10, int(heat or 0)))
        except (TypeError, ValueError):
            pass
        test = bool(p.get("test_mode", True))
        if test:
            self.log(f"[{p['name']}] TEST -> fan {fid} [{f['stage']} heat {f['heat']}]: " + " | ".join(msgs))
        else:
            sent = []
            om = self.om()
            for i, text in enumerate(msgs):
                st, d = om.send(pid, fid, text)
                if st == 400 and d.get("code") == "RESTRICTED_WORDS_DETECTED":
                    words = d.get("details", {}).get("words", [])
                    return {"rejected": True, "reason": f"OnlyFans blocked words {words}; already sent {sent}. "
                            "Send only the remaining messages without those words."}
                if st in (403, 404):
                    self.pstate(pid)["dead"][fid] = time.time()
                    self.state["inbox"].pop(key, None)
                    return {"rejected": True, "reason": "chat unavailable (fan blocked/unsubscribed) - skip"}
                if st not in (200, 202):
                    return {"rejected": True, "reason": f"OnlyMonster error {st}: {d}", "sent": sent}
                sent.append(text)
                if i < len(msgs) - 1:
                    time.sleep(random.uniform(*CASCADE_GAP))
            self.log(f"[{p['name']}] SENT -> fan {fid} [{f['stage']} heat {f['heat']}]: " + " | ".join(sent))
        f["turns"] = f.get("turns", 0) + 1
        self.stat("replies")
        self.state["inbox"].pop(key, None)
        result = {"ok": True, "test_mode": test, "turns": f["turns"]}
        if wants_free:
            self.cfg["muted"].append(key)
            self.save_cfg()
            self.stat("free")
            self.log(f"[{p['name']}] fan {fid} wants free - muted")
            result["muted"] = True
        elif asked or buying_intent:
            self.stat("leads")
            self.do_handoff(p, fid, "asked THE QUESTION" if asked else "fan wants to buy / asked price", incoming)
            result["handed_off"] = True
        self.save()
        return result

    def skip(self, pid, fid, reason=""):
        self.state["inbox"].pop(f"{pid}:{fid}", None)
        self.log(f"skipped fan {fid}: {reason}")
        self.save()
        return {"ok": True}

    def handoff(self, pid, fid, reason):
        p = self.profile(pid)
        inc = self.state["inbox"].get(f"{pid}:{fid}", {}).get("incoming", "")
        self.do_handoff(p, str(fid), reason, inc)
        self.save()
        return {"ok": True}

    def mute(self, pid, fid, on=True):
        key = f"{pid}:{fid}"
        with self.lock:
            if on and key not in self.cfg["muted"]:
                self.cfg["muted"].append(key)
                self.state["inbox"].pop(key, None)
            if not on:
                self.cfg["muted"] = [m for m in self.cfg["muted"] if m not in (key, str(fid))]
                self.fan(pid, fid)["paused"] = False
        self.save_cfg()
        self.save()
        return {"ok": True, "muted": on}

    DEMO_LINES = ["hey gorgeous, how's your night going?", "hi there :) where are you from?",
                  "you look amazing in your last post", "how much for a video?", "are you a real person or a bot?",
                  "can you send me something for free?", "what's your whatsapp?", "just got home from work, so tired"]

    def demo_message(self, text=""):
        """Fake fan message for the Demo model - nothing touches OnlyMonster."""
        if not any(str(p["id"]) == "demo" for p in self.cfg["profiles"]):
            self.cfg["profiles"].insert(0, {"id": "demo", "name": "Demo model", "username": "demo", "enabled": False,
                                            "test_mode": True, "tone_file": os.path.join(APP_DIR, "tone_example.txt")})
            self.save_cfg()
        p = self.profile("demo")
        ps = self.pstate("demo")
        fid = str(random.randint(100, 999)) if not text else "demo-fan"
        text = text or random.choice(self.DEMO_LINES)
        mid = int(time.time() * 1000)
        items = [{"id": mid, "text": text, "is_sent_by_me": False, "created_at": datetime.now().isoformat()}]
        ps["seen"][fid] = mid
        self.on_new(p, fid, items, items)
        self.save()
        return {"ok": True, "fan_id": fid, "text": text}

    def summary(self):
        today = self.state["stats"].get(datetime.now().strftime("%Y-%m-%d"), {})
        return {"running": self.running, "status": self.status, "poll_seconds": self.cfg.get("poll_seconds"),
                "inbox": len(self.state["inbox"]), "today": today,
                "claude_seen_ago_s": int(time.time() - self.last_claude) if self.last_claude else None,
                "profiles": [{"id": p["id"], "name": p["name"], "username": p.get("username"), "enabled": p.get("enabled"),
                              "test_mode": p.get("test_mode", True), "tone_file": p.get("tone_file", "")}
                             for p in self.cfg["profiles"]],
                "muted": self.cfg["muted"], "log": self.logs[-60:]}

    def set_running(self, on):
        self.running = bool(on)
        self.next_poll = 0
        self.status = "running" if on else "stopped"
        self.log("Started" if on else "Stopped")
        self.wake.set()
        return {"ok": True, "running": self.running}


# --------------------------------------------------------------------------- web
C = None
SECRET_FIELDS = ("om_token", "tg_token")


def public_cfg():
    d = dict(C.cfg)
    for k in SECRET_FIELDS:
        d[k] = ("set" if d.get(k) else "")
    return d


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(APP_DIR, "ui.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/logo.jpg":
                with open(os.path.join(APP_DIR, "logo.jpg"), "rb") as f:
                    return self.send(200, f.read(), "image/jpeg")
            if u.path == "/api/status":
                return self.send(200, C.summary())
            if u.path == "/api/config":
                return self.send(200, public_cfg())
            if u.path == "/api/inbox":
                return self.send(200, C.get_inbox(int(q.get("limit", 10)), "ui" not in q))
            if u.path == "/api/tone":
                return self.send(200, C.tone(q["profile_id"]))
            if u.path == "/api/accounts":
                return self.send(200, C.om().accounts())
            if u.path == "/api/fan":
                return self.send(200, C.fan(q["profile_id"], q["fan_id"]))
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": str(e)})

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"error": "bad origin"})
        u = urlparse(self.path)
        try:
            b = self.body()
            if u.path == "/api/run":
                return self.send(200, C.set_running(b.get("on")))
            if u.path == "/api/check_now":
                C.next_poll = 0
                C.wake.set()
                return self.send(200, {"ok": True})
            if u.path == "/api/config":
                with C.lock:
                    for k, v in b.items():
                        if k in SECRET_FIELDS and v in ("", "set"):
                            continue
                        if k in ("om_token", "tg_token", "tg_chat", "poll_seconds", "fans_per_cycle", "handoff_keywords", "profiles"):
                            C.cfg[k] = v
                C.save_cfg()
                return self.send(200, {"ok": True})
            if u.path == "/api/reply":
                return self.send(200, C.send_reply(b["profile_id"], b["fan_id"], b.get("messages"), b.get("stage", ""),
                                                   b.get("heat", 0), b.get("notes", ""), b.get("asked_to_see", False),
                                                   b.get("buying_intent", False), b.get("wants_free", False)))
            if u.path == "/api/demo":
                return self.send(200, C.demo_message(b.get("text", "")))
            if u.path == "/api/skip":
                return self.send(200, C.skip(b["profile_id"], b["fan_id"], b.get("reason", "")))
            if u.path == "/api/handoff":
                return self.send(200, C.handoff(b["profile_id"], b["fan_id"], b.get("reason", "")))
            if u.path == "/api/mute":
                return self.send(200, C.mute(b["profile_id"], b["fan_id"], b.get("on", True)))
            if u.path == "/api/test_mode":
                p = C.profile(b["profile_id"])
                p["test_mode"] = bool(b.get("on"))
                C.save_cfg()
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except Exception as e:
            self.send(500, {"error": str(e)})


def main():
    global C
    C = Chatter()
    C.log(f"DUCK AI Chatter on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
