"""
PC 端轮询脚本

每 5 秒检查一次待处理消息 → claude --print 执行 → 回传结果
保持此脚本在后台运行，Claude Code 必须已登录。

使用方式:
    python poller.py                            # 默认轮询
    python poller.py --server http://localhost:5000   # 指定服务器
    python poller.py --interval 3               # 3 秒轮询间隔
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

import requests

# 默认定时上线服务器
DEFAULT_SERVER = "https://task-reminder-fj2e.onrender.com"

BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "bridge_secret_change_me")
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")


def main():
    parser = argparse.ArgumentParser(description="WeChat-Claude Bridge Poller")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Bridge server URL")
    parser.add_argument("--interval", type=int, default=5, help="Polling interval in seconds")
    parser.add_argument("--once", action="store_true", help="Process one message and exit")
    args = parser.parse_args()

    server = args.server.rstrip("/")
    interval = args.interval

    print(f"Bridge Poller started")
    print(f"  Server:   {server}")
    print(f"  Interval: {interval}s")
    print(f"  Claude:   {CLAUDE_CMD}")
    print()

    processed_ids = set()  # 避免重复执行

    while True:
        try:
            resp = requests.get(
                f"{server}/api/messages/pending",
                headers={"X-Bridge-Secret": BRIDGE_SECRET},
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"[{timestamp()}] Server unreachable (HTTP {resp.status_code})")
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
                from_user = msg.get("from_user", "?")
                print(f"[{timestamp()}] New message from {from_user}: {content}")

                # 调用 Claude Code 执行
                reply = run_claude(content)

                # 回传结果
                try:
                    r = requests.post(
                        f"{server}/api/messages/{msg_id}/reply",
                        json={"reply": reply},
                        headers={"X-Bridge-Secret": BRIDGE_SECRET},
                        timeout=15,
                    )
                    if r.status_code == 200 and r.json().get("ok"):
                        print(f"[{timestamp()}] Replied OK ({len(reply)} chars)")
                    else:
                        print(f"[{timestamp()}] Reply failed: {r.text}")
                except Exception as e:
                    print(f"[{timestamp()}] Reply error: {e}")

            if args.once and pending:
                break

        except requests.ConnectionError:
            print(f"[{timestamp()}] Cannot connect to {server}")
        except Exception as e:
            print(f"[{timestamp()}] Error: {e}")

        if args.once:
            break
        time.sleep(interval)


def run_claude(prompt: str) -> str:
    """调用 claude --print 执行指令并返回输出"""
    try:
        # 检测 claude 是否可用
        result = subprocess.run(
            [CLAUDE_CMD, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return f"[错误] claude 命令不可用: {result.stderr.strip()}"
    except FileNotFoundError:
        return "[错误] 找不到 claude 命令，请确认 Claude Code 已安装并在 PATH 中"
    except subprocess.TimeoutExpired:
        return "[错误] claude --version 超时"

    # 执行指令
    try:
        result = subprocess.run(
            [CLAUDE_CMD, "--print", prompt],
            capture_output=True,
            text=True,
            timeout=120,  # 最多等 2 分钟
            encoding="utf-8",
        )
        output = result.stdout.strip() or result.stderr.strip()
        if not output:
            output = "(执行完毕，无输出)"

        # 去掉可能输出的权限提示等噪音
        if len(output) > 2500:
            output = output[:2497] + "..."

        return output
    except subprocess.TimeoutExpired:
        return "[错误] 执行超时（超过 2 分钟）"
    except Exception as e:
        return f"[错误] 执行异常: {e}"


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


if __name__ == "__main__":
    main()
