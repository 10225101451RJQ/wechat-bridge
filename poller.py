"""
PC poller for WeChat-Claude Bridge.
Polls server for pending messages, runs claude --print, posts reply back.

Usage:
    python poller.py
    python poller.py --server http://localhost:5000
    python poller.py --interval 3
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

import requests

DEFAULT_SERVER = "https://wechat-bridge.onrender.com"
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "bridge_secret_change_me")
CLAUDE_CMD = os.getenv("CLAUDE_CMD", r"C:\Users\Lenovo\AppData\Roaming\npm\claude.cmd")
WORK_DIR = r"F:\Claude code工作目录"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    server = args.server.rstrip("/")
    interval = args.interval

    print(f"Bridge Poller started")
    print(f"  Server:   {server}")
    print(f"  Interval: {interval}s")
    print()

    processed_ids = set()

    while True:
        try:
            resp = requests.get(
                f"{server}/api/messages/pending",
                headers={"X-Bridge-Secret": BRIDGE_SECRET},
                timeout=10,
            )
            resp.encoding = "utf-8"

            if resp.status_code != 200:
                print(f"[{ts()}] Server HTTP {resp.status_code}: {resp.text[:100]}")
                time.sleep(interval)
                continue

            data = resp.json()
            pending = data.get("messages", [])

            for msg in pending:
                msg_id = msg["id"]
                if msg_id in processed_ids:
                    continue
                processed_ids.add(msg_id)

                content = msg["content"]
                print(f"[{ts()}] Msg: {content}")

                reply = run_claude(content)
                preview = reply[:50].replace("\n", " ")
                print(f"[{ts()}] Reply: {preview}...")

                try:
                    r = requests.post(
                        f"{server}/api/messages/{msg_id}/reply",
                        json={"reply": reply},
                        headers={"X-Bridge-Secret": BRIDGE_SECRET},
                        timeout=15,
                    )
                    if r.status_code == 200:
                        print(f"[{ts()}] Sent OK ({len(reply)} chars)")
                    else:
                        print(f"[{ts()}] Send HTTP {r.status_code}")
                except Exception as e:
                    print(f"[{ts()}] Send error: {type(e).__name__}")

            if args.once and pending:
                break

        except requests.ConnectionError:
            print(f"[{ts()}] No connection to {server}")
        except Exception as e:
            print(f"[{ts()}] {type(e).__name__}: {str(e)[:100]}")

        if args.once:
            break
        time.sleep(interval)


def run_claude(prompt: str) -> str:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        subprocess.run(
            [CLAUDE_CMD, "--version"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
            env=env, cwd=WORK_DIR,
        )
    except Exception:
        return "[ERR] claude not found"

    try:
        result = subprocess.run(
            [CLAUDE_CMD, "--print", prompt],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
            env=env, cwd=WORK_DIR,
        )
        output = result.stdout.strip() or result.stderr.strip()
        if not output:
            output = "(no output)"
        if len(output) > 2500:
            output = output[:2497] + "..."
        return output
    except subprocess.TimeoutExpired:
        return "[ERR] timeout (2min)"
    except Exception as e:
        return f"[ERR] {type(e).__name__}"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


if __name__ == "__main__":
    main()
