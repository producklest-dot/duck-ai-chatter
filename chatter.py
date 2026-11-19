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

Inbox item "kind":
- reply: the fan wrote, answer him.
- followup: he went quiet after your last message. Send ONE short light nudge to restart the chat, no tease.
- broadcast: the agency asked to write to this fan first. Follow "task", keep it short and personal.

Paid content (only if the item has ppv_on=true): when the fan is hot (asked to see you, asks for content or
the price, heat >= 7) offer ONE item from get_vault with send_ppv(profile_id, fan_id, item_id, message).
Pick the item that fits what he asked for; message = 1 short teasing line, no price in the text.
Never offer to a fan who wanted free stuff, never two offers in a row without his reply.
If ppv_on=false, never offer - ask THE QUESTION and the chat goes to a human.
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

    def send(self, aid, chat_id, text, price=None, media=None):
        body = {"text": text, "price": price, "locked_text": False, "media": media or []}
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

    def stat(self, k, n=1, pid=None):
        day = datetime.now().strftime("%Y-%m-%d")
        d = self.state["stats"].setdefault(day, {})
        d[k] = round(d.get(k, 0) + n, 2)
        if pid:
            dp = self.state.setdefault("stats_p", {}).setdefault(day, {}).setdefault(str(pid), {})
            dp[k] = round(dp.get(k, 0) + n, 2)

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
                try:
                    self.check_followups()
                except Exception as e:
                    self.log(f"Follow-up error: {e}")
                self.last_poll = time.time()
                self.next_poll = time.time() + max(15, int(self.cfg.get("poll_seconds", 60)))
                self.save()
            try:
                self.maybe_report()
            except Exception as e:
                self.log(f"Report error: {e}")
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
            self.touch(pid, fid, items)
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

    def touch(self, pid, fid, items):
        """Remember the latest chat state for the fan card and follow-ups."""
        f = self.fan(pid, fid)
        last = items[0]
        f["last_id"] = last["id"]
        f["last_from_me"] = bool(last.get("is_sent_by_me"))
        f["last_text"] = (clean(last.get("text")) or ("[media]" if last.get("media_count") else ""))[:200]
        try:
            f["last_at"] = datetime.fromisoformat(str(last.get("created_at", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            f["last_at"] = time.time()
        f["chat"] = [{"from": "creator" if m.get("is_sent_by_me") else "fan",
                      "text": (clean(m.get("text")) or ("[media]" if m.get("media_count") else ""))[:400],
                      "at": m.get("created_at", ""), "price": m.get("price")} for m in reversed(items[:HISTORY])]

    def history_of(self, items):
        out = []
        for m in reversed(items[:HISTORY]):
            text = clean(m.get("text"))[:600]
            if m.get("media_count"):
                text = ("[media] " + text).strip()
            if text:
                out.append({"from": "creator" if m.get("is_sent_by_me") else "fan", "text": text, "at": m.get("created_at", "")})
        return out

    def queue(self, p, fid, kind, incoming="", history=None, task=""):
        pid = str(p["id"])
        f = self.fan(pid, fid)
        if history is None:
            history = [dict(h) for h in f.get("chat", []) if h.get("text")]
        with self.lock:
            self.state["inbox"][f"{pid}:{fid}"] = {
                "profile_id": pid, "profile": p["name"], "fan_id": fid, "kind": kind, "task": task,
                "incoming": incoming, "history": history, "stage": f["stage"], "turns": f.get("turns", 0),
                "heat": f.get("heat", 0), "notes": f.get("notes", ""), "tags": f.get("tags", []),
                "spent": f.get("spent", 0), "queued_at": time.time()}

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
        f["replied_since_offer"] = True
        history = [dict(h) for h in f.get("chat", []) if h.get("text")] if pid == "demo" else self.history_of(items)
        self.queue(p, fid, "reply", incoming, history)

    def do_handoff(self, p, fid, why, incoming=""):
        pid = str(p["id"])
        f = self.fan(pid, fid)
        f["paused"], f["paused_at"] = True, time.time()
        self.state["inbox"].pop(f"{pid}:{fid}", None)
        self.stat("handoff", 1, pid)
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
                out.append(dict(it, kind=it.get("kind", "reply"), min_turns_before_tease=MIN_TURNS_BEFORE_TEASE,
                                test_mode=bool(p.get("test_mode", True)), ppv_on=bool(p.get("ppv_on")) and bool(p.get("vault"))))
            return out

    def tone(self, pid):
        p = self.profile(pid)
        voice = (p.get("voice") or "").strip() or read_tone(p.get("tone_file", ""))
        return {"profile": p["name"], "username": p.get("username", ""), "voice": voice,
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
            f.setdefault("chat", []).extend({"from": "creator", "text": m, "at": datetime.now().isoformat()} for m in msgs)
            f["chat"] = f["chat"][-HISTORY:]
            f["last_from_me"], f["last_at"] = True, time.time()
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
        f["followup_for"] = None
        self.stat("replies", 1, pid)
        self.state["inbox"].pop(key, None)
        result = {"ok": True, "test_mode": test, "turns": f["turns"]}
        if wants_free:
            self.cfg["muted"].append(key)
            self.save_cfg()
            f["wants_free"] = True
            self.stat("free", 1, pid)
            self.log(f"[{p['name']}] fan {fid} wants free - muted")
            result["muted"] = True
        elif (asked or buying_intent) and p.get("ppv_on") and p.get("vault") and not f.get("wants_free"):
            self.stat("leads", 1, pid)
            result["next"] = "fan is hot - on his next message offer an item with send_ppv"
        elif asked or buying_intent:
            self.stat("leads", 1, pid)
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
        if not p.get("vault"):
            p["ppv_on"] = True
            p["vault"] = [{"id": "d1", "title": "Bikini on the beach (5 photos)", "price": 12, "media": ""},
                          {"id": "d2", "title": "Shower video 3 min", "price": 25, "media": ""}]
            self.save_cfg()
        ps = self.pstate("demo")
        fid = "demo-fan"
        text = text or random.choice(self.DEMO_LINES)
        f = self.fan("demo", fid)
        if text == "__reset__" or self.is_paused(f) or self.is_muted("demo", fid):
            ps["fans"].pop(fid, None)
            self.cfg["muted"] = [m for m in self.cfg["muted"] if m != "demo:demo-fan"]
            self.state["inbox"].pop("demo:demo-fan", None)
            self.log("[Demo model] demo-fan: new chat")
            if text == "__reset__":
                self.save()
                return {"ok": True, "reset": True}
        mid = int(time.time() * 1000)
        items = [{"id": mid, "text": text, "is_sent_by_me": False, "created_at": datetime.now().isoformat()}]
        ps["seen"][fid] = mid
        prev = self.fan("demo", fid).get("chat", [])
        self.touch("demo", fid, items)
        self.fan("demo", fid)["chat"] = (prev + self.fan("demo", fid)["chat"])[-HISTORY:]
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

    # ---- follow-ups
    def check_followups(self):
        now_t = time.time()
        for p in self.cfg["profiles"]:
            hrs = float(p.get("followup_hours") or 0)
            if not p.get("enabled") or hrs <= 0:
                continue
            pid = str(p["id"])
            ps = self.pstate(pid)
            for fid, f in ps["fans"].items():
                if (not f.get("last_from_me") or not f.get("turns") or f.get("wants_free") or self.is_paused(f)
                        or self.is_muted(pid, fid) or f"{pid}:{fid}" in self.state["inbox"]
                        or f.get("followup_for") == f.get("last_id") or now_t - f.get("last_at", now_t) < hrs * 3600
                        or now_t - f.get("last_at", 0) > 7 * 86400):
                    continue
                f["followup_for"] = f.get("last_id")
                self.queue(p, fid, "followup", task=f"He has been quiet for {int((now_t - f['last_at']) / 3600)} h after your last message.")
                self.log(f"[{p['name']}] follow-up queued for fan {fid}")

    # ---- paid content
    def vault(self, pid):
        p = self.profile(pid)
        return {"ppv_on": bool(p.get("ppv_on")), "items": p.get("vault", [])}

    def send_ppv(self, pid, fid, item_id, message):
        self.last_claude = time.time()
        p = self.profile(pid)
        fid = str(fid)
        f = self.fan(pid, fid)
        key = f"{pid}:{fid}"
        if not p.get("ppv_on"):
            return {"rejected": True, "reason": "paid content is off for this model - do not offer"}
        item = next((x for x in p.get("vault", []) if str(x.get("id")) == str(item_id)), None)
        if not item:
            return {"rejected": True, "reason": "unknown item_id - use get_vault"}
        if f.get("wants_free") or self.is_muted(pid, fid) or self.is_paused(f):
            return {"rejected": True, "reason": "this fan must not get offers"}
        if f.get("offers") and not f.get("replied_since_offer", True):
            return {"rejected": True, "reason": "he has not answered your last offer yet - just chat"}
        text = re.sub(r"\s+", " ", str(message or "")).strip()[:300] or "made this just for you 😏"
        price = float(item.get("price") or 0)
        if price < 3:
            return {"rejected": True, "reason": "item has no valid price - ask the agency to fix the vault"}
        media = [m.strip() for m in str(item.get("media", "")).split(",") if m.strip()]
        if p.get("test_mode", True):
            self.log(f"[{p['name']}] TEST PPV -> fan {fid}: {item.get('title')} ${price:g} | {text}")
        else:
            st, d = self.om().send(pid, fid, text, price=price, media=media)
            if st not in (200, 202):
                return {"rejected": True, "reason": f"OnlyMonster error {st}: {d}"}
            self.log(f"[{p['name']}] PPV SENT -> fan {fid}: {item.get('title')} ${price:g} | {text}")
        f.setdefault("offers", []).append({"item": item.get("id"), "title": item.get("title"), "price": price, "at": time.time(), "bought": False})
        f["replied_since_offer"] = False
        f["turns"] = f.get("turns", 0) + 1
        self.state["inbox"].pop(key, None)
        self.stat("ppv_offers", 1, pid)
        self.tg(f"{p['name']}: offered \"{item.get('title')}\" ${price:g} to fan {fid}\nhttps://onlyfans.com/my/chats/chat/{fid}")
        self.save()
        return {"ok": True, "test_mode": bool(p.get("test_mode", True))}

    # ---- fans (CRM)
    def fans(self, pid):
        out = []
        for fid, f in self.pstate(pid)["fans"].items():
            out.append({"fan_id": fid, "stage": f.get("stage"), "heat": f.get("heat", 0), "turns": f.get("turns", 0),
                        "spent": f.get("spent", 0), "tags": f.get("tags", []), "notes": f.get("notes", ""),
                        "last_at": f.get("last_at"), "last_text": f.get("last_text", ""), "last_from_me": f.get("last_from_me"),
                        "paused": bool(self.is_paused(f)), "muted": self.is_muted(pid, fid), "wants_free": bool(f.get("wants_free")),
                        "offers": f.get("offers", []), "chat": f.get("chat", [])})
        out.sort(key=lambda x: -(x["last_at"] or 0))
        return out

    def fan_update(self, pid, fid, b):
        f = self.fan(pid, fid)
        if "notes" in b:
            f["notes"] = str(b["notes"])[:2000]
        if "tags" in b:
            f["tags"] = [str(t).strip() for t in b["tags"] if str(t).strip()][:10]
        if b.get("unpause"):
            f["paused"] = False
        if "mute" in b:
            self.mute(pid, fid, bool(b["mute"]))
        if b.get("bought") is not None:
            i = int(b["bought"])
            offers = f.get("offers", [])
            if 0 <= i < len(offers) and not offers[i].get("bought"):
                offers[i]["bought"] = True
                f["spent"] = round(f.get("spent", 0) + offers[i]["price"], 2)
                self.stat("purchases", 1, pid)
                self.stat("revenue", offers[i]["price"], pid)
        if b.get("spent_add"):
            f["spent"] = round(f.get("spent", 0) + float(b["spent_add"]), 2)
            self.stat("revenue", float(b["spent_add"]), pid)
        self.save()
        return {"ok": True}

    # ---- broadcasts
    def segment(self, pid, seg):
        now_t = time.time()
        out = []
        for x in self.fans(pid):
            if x["muted"] or x["paused"] or x["wants_free"]:
                continue
            if seg.get("stage") and x["stage"] != seg["stage"]:
                continue
            if seg.get("tag") and seg["tag"] not in x["tags"]:
                continue
            if seg.get("silent_days") and now_t - (x["last_at"] or now_t) < float(seg["silent_days"]) * 86400:
                continue
            if seg.get("min_spent") and x["spent"] < float(seg["min_spent"]):
                continue
            out.append(x["fan_id"])
        return out

    def broadcast(self, pid, seg, text, personalize, item_id=None):
        p = self.profile(pid)
        fans = self.segment(pid, seg)
        if not fans:
            return {"ok": False, "count": 0, "error": "no fans in this group"}
        if personalize:
            for fid in fans:
                self.queue(p, fid, "broadcast", task=text)
            self.log(f"[{p['name']}] broadcast: {len(fans)} fans queued for Claude")
            return {"ok": True, "count": len(fans), "mode": "claude"}
        item = next((x for x in p.get("vault", []) if str(x.get("id")) == str(item_id)), None) if item_id else None

        def run():
            sent = 0
            for fid in fans:
                if p.get("test_mode", True):
                    self.log(f"[{p['name']}] TEST BROADCAST -> fan {fid}: {text}" + (f" + PPV {item['title']} ${item['price']}" if item else ""))
                else:
                    media = [m.strip() for m in str((item or {}).get("media", "")).split(",") if m.strip()]
                    st, d = self.om().send(pid, fid, text, price=float(item["price"]) if item else None, media=media)
                    if st not in (200, 202):
                        self.log(f"[{p['name']}] broadcast to {fid} failed: {st}")
                        continue
                    time.sleep(random.uniform(8, 20))
                sent += 1
            self.stat("broadcast", sent, pid)
            self.log(f"[{p['name']}] broadcast done: {sent}/{len(fans)}")
            self.save()
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "count": len(fans), "mode": "text"}

    # ---- reports
    def report_text(self, day=None):
        day = day or datetime.now().strftime("%Y-%m-%d")
        per = self.state.get("stats_p", {}).get(day, {})
        lines = [f"Звіт DUCK AI Chatter за {day}"]
        for p in self.cfg["profiles"]:
            d = per.get(str(p["id"]))
            if not d:
                continue
            lines.append(f"\n{p['name']}: відповідей {d.get('replies', 0):g}, гарячих {d.get('leads', 0):g}, "
                         f"людині {d.get('handoff', 0):g}, PPV запропоновано {d.get('ppv_offers', 0):g}, "
                         f"куплено {d.get('purchases', 0):g}, дохід ${d.get('revenue', 0):g}, розсилка {d.get('broadcast', 0):g}")
        if len(lines) == 1:
            lines.append("Сьогодні активності не було.")
        return "\n".join(lines)

    def maybe_report(self):
        h = self.cfg.get("report_hour")
        if h in (None, "", -1):
            return
        now_d = datetime.now()
        day = now_d.strftime("%Y-%m-%d")
        if now_d.hour >= int(h) and self.state.get("last_report") != day:
            self.state["last_report"] = day
            self.tg(self.report_text(day))
            self.log("Daily report sent to Telegram")
            self.save()

    def setup(self):
        """Checklist for the Home screen."""
        import shutil, subprocess
        profs = [p for p in self.cfg["profiles"] if p["id"] != "demo"]
        active = [p for p in profs if p.get("enabled")]
        mcp_ok = False
        claude = shutil.which("claude") or "/opt/homebrew/bin/claude"
        try:
            r = subprocess.run([claude, "mcp", "get", "duck-chatter"], capture_output=True, text=True, timeout=20)
            mcp_ok = r.returncode == 0
        except Exception:
            pass
        return {"onlymonster": bool(self.cfg.get("om_token")), "models": len(profs), "active": len(active),
                "voice": all((p.get("voice") or p.get("tone_file")) for p in active) if active else False,
                "telegram": bool(self.cfg.get("tg_token") and self.cfg.get("tg_chat")),
                "claude_connected": mcp_ok,
                "claude_working": bool(self.last_claude and time.time() - self.last_claude < 300),
                "live": [p["name"] for p in active if not p.get("test_mode", True)]}

    def connect_claude(self):
        import shutil, subprocess, sys
        claude = shutil.which("claude") or "/opt/homebrew/bin/claude"
        py = os.path.join(os.path.expanduser("~"), ".duck-ai-chatter-venv", "bin", "python")
        if not os.path.exists(py):
            py = sys.executable
        subprocess.run([claude, "mcp", "remove", "duck-chatter", "-s", "user"], capture_output=True, timeout=30)
        r = subprocess.run([claude, "mcp", "add", "--scope", "user", "duck-chatter", "--", py,
                            os.path.join(APP_DIR, "mcp_server.py")], capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0
        self.log("Claude connected (MCP duck-chatter)" if ok else f"Claude connect failed: {r.stderr[:200]}")
        return {"ok": ok, "error": "" if ok else (r.stderr or r.stdout)[:300]}

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
            if u.path == "/api/fans":
                return self.send(200, C.fans(q["profile_id"]))
            if u.path == "/api/vault":
                return self.send(200, C.vault(q["profile_id"]))
            if u.path == "/api/report":
                return self.send(200, {"text": C.report_text()})
            if u.path == "/api/segment":
                seg = {k: q[k] for k in ("stage", "tag", "silent_days", "min_spent") if q.get(k)}
                return self.send(200, {"count": len(C.segment(q["profile_id"], seg))})
            if u.path == "/api/setup":
                return self.send(200, C.setup())
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
                        if k in ("om_token", "tg_token", "tg_chat", "poll_seconds", "fans_per_cycle", "handoff_keywords", "profiles", "report_hour"):
                            C.cfg[k] = v
                C.save_cfg()
                return self.send(200, {"ok": True})
            if u.path == "/api/reply":
                return self.send(200, C.send_reply(b["profile_id"], b["fan_id"], b.get("messages"), b.get("stage", ""),
                                                   b.get("heat", 0), b.get("notes", ""), b.get("asked_to_see", False),
                                                   b.get("buying_intent", False), b.get("wants_free", False)))
            if u.path == "/api/ppv":
                return self.send(200, C.send_ppv(b["profile_id"], b["fan_id"], b["item_id"], b.get("message", "")))
            if u.path == "/api/fan_update":
                return self.send(200, C.fan_update(b["profile_id"], b["fan_id"], b))
            if u.path == "/api/broadcast":
                return self.send(200, C.broadcast(b["profile_id"], b.get("segment", {}), b.get("text", ""),
                                                  bool(b.get("personalize")), b.get("item_id")))
            if u.path == "/api/report_now":
                C.tg(C.report_text())
                return self.send(200, {"ok": True})
            if u.path == "/api/connect_claude":
                return self.send(200, C.connect_claude())
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
