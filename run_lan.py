"""局域网分享模式：让同一个 WiFi 下的同学能用。

和 run.bat 的区别只有两点：
  1. 监听 0.0.0.0（否则只有本机能连）
  2. 自动生成一个访问口令（否则同一网络里谁都能打开，用你的 API 额度）

用法：
    python run_lan.py            # 随机口令，每次启动都变
    set ACCESS_CODE=abcd1234 && python run_lan.py    # 固定口令
"""

from __future__ import annotations

import os
import secrets
import socket
import sys

PORT = int(os.environ.get("PORT", "8000"))


def lan_ip() -> str:
    """拿本机局域网地址。

    不靠 hostname 解析——多网卡（虚拟机、VPN、WSL）时经常返回 127.0.0.1。
    连一个外网地址但不真发包，路由表会告诉你默认出口是哪块网卡。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    code = os.environ.get("ACCESS_CODE", "").strip()
    fresh = not code
    if fresh:
        # 6 位数：够挡路人，手输也不烦。这不是密码，不需要 32 位熵
        code = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
        os.environ["ACCESS_CODE"] = code

    ip = lan_ip()
    line = "─" * 52
    print(f"\n课程助手 · 局域网分享模式\n{line}")
    print(f"  你自己打开    http://127.0.0.1:{PORT}      不需要口令")
    print(f"  发给同学      http://{ip}:{PORT}")
    print(f"  访问口令      {code}")
    if fresh:
        print(f"                （本次随机生成，关掉窗口就失效）")
    print(line)
    print("  打不开的话，按顺序查这三条：")
    print("    1. 是否连的同一个 WiFi —— 校园网常开设备隔离，互相不通")
    print("    2. Windows 防火墙第一次会弹窗，必须点「允许访问」")
    print("    3. 还是不行，用管理员 PowerShell 放行端口：")
    print(f"       netsh advfirewall firewall add rule name=\"course-assistant\" "
          f"dir=in action=allow protocol=TCP localport={PORT}")
    print(f"{line}")
    print("  改模型配置只有本机能做，同学那边看不到配置入口。")
    print("  每个人的历史互不可见：给每人一个口令（逗号分隔）")
    print("       set ACCESS_CODE=abc123,def456,ghi789 && python run_lan.py")
    print("  Ctrl+C 停止\n")
    sys.stdout.flush()

    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
