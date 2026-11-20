"""DUCK AI Chatter - desktop window. The daemon runs on its own (LaunchAgent), so closing the window keeps the bot working."""
import os, subprocess, sys, time, urllib.request
import webview

URL = "http://127.0.0.1:8796"
HERE = os.path.dirname(os.path.abspath(__file__))


def alive():
    try:
        urllib.request.urlopen(URL + "/api/status", timeout=1); return True
    except Exception:
        return False


if not alive():
    subprocess.Popen([sys.executable, os.path.join(HERE, "chatter.py")], cwd=HERE, start_new_session=True,
                     stdout=open(os.path.join(HERE, "daemon.out"), "a"), stderr=subprocess.STDOUT)
    for _ in range(80):
        if alive(): break
        time.sleep(0.1)
webview.create_window("DUCK AI Chatter", URL, width=1280, height=860, min_size=(820, 600))
webview.start()
