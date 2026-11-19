"""DUCK AI Chatter - desktop window. Starts the daemon (if not running) and shows the panel."""
import os, sys, threading, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webview

URL = "http://127.0.0.1:8796"

def alive():
    try:
        urllib.request.urlopen(URL + "/api/status", timeout=1); return True
    except Exception:
        return False

if not alive():
    import chatter
    threading.Thread(target=chatter.main, daemon=True).start()
    for _ in range(50):
        if alive(): break
        time.sleep(0.1)
webview.create_window("DUCK AI Chatter", URL, width=1280, height=860, min_size=(820, 600))
webview.start()
