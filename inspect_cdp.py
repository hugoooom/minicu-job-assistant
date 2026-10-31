"""独立启动 Edge，手动确认访问状态后，再通过 CDP 连接并读取页面。"""

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


PORT = 9333
ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
PROFILE = OUTPUT / "cdp-edge-profile"


def find_edge():
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("没有找到 Microsoft Edge 的安装文件。")


def ensure_port_free(port):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"本地端口 {port} 已被占用。请关闭上次的 CDP 测试浏览器后重试。"
            ) from exc


def build_command(executable, profile, port):
    return [
        str(executable),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]


def wait_for_cdp(port, timeout=20):
    opener = build_opener(ProxyHandler({}))  # 本机连接不经过系统 HTTP 代理。
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                if json.load(response).get("webSocketDebuggerUrl"):
                    return
        except (OSError, URLError, ValueError):
            pass
        time.sleep(0.25)
    raise TimeoutError("未能连接 Edge 的本地调试端口，请检查浏览器是否正常启动。")


def main():
    ensure_port_free(PORT)
    executable = find_edge()
    PROFILE.mkdir(parents=True, exist_ok=True)
    command = build_command(executable, PROFILE, PORT)
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    )
    wait_for_cdp(PORT)

    print("阶段 1：Edge 已独立启动，Playwright 尚未连接。", flush=True)
    print("请在这个新窗口中手动打开 BOSS，正常登录并进入岗位列表。")
    print("本次使用独立资料目录：", PROFILE)
    answer = input("页面正常请按回车；如果此时已经跳到空白页，输入 n 后回车：")
    if answer.strip().lower() == "n":
        print("已记录：在 Playwright 连接之前就无法稳定访问。")
        print("本次不连接，测试浏览器请手动关闭。")
        return

    with sync_playwright() as p:
        print("阶段 2：现在开始通过 CDP 连接。", flush=True)
        browser = p.chromium.connect_over_cdp(
            f"http://127.0.0.1:{PORT}", timeout=15000, no_defaults=True
        )
        context = browser.contexts[0]
        pages = [page for page in context.pages if not page.is_closed()]
        if not pages:
            raise RuntimeError("连接成功，但没有可读取的标签页。")
        for index, page in enumerate(pages, start=1):
            print(f"{index}. {page.url}")
        choice = int(input("请输入岗位页编号（只有一个标签页时输入 1）："))
        if not 1 <= choice <= len(pages):
            raise ValueError("编号不在列表内。")
        page = pages[choice - 1]

        input("观察连接后页面是否跳转，然后按回车尝试保存当前页面：")
        try:
            text = page.locator("body").inner_text(timeout=10000)
            (OUTPUT / "cdp-page.txt").write_text(text, encoding="utf-8")
            print(f"文字已保存，共 {len(text)} 个字符。")
        except PlaywrightError as exc:
            print("读取文字失败：", exc)
        try:
            page.screenshot(path=str(OUTPUT / "cdp-page.png"), scale="css", timeout=10000)
            print("截图已保存。")
        except PlaywrightError as exc:
            print("截图失败：", exc)
        print("读取后的网址：", page.url)
        print("输出目录：", OUTPUT)

    # 退出 Playwright 连接；不调用 browser.close() 或 context.close()。
    print("程序结束，测试浏览器请手动关闭，之后可以再次运行本脚本。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, PlaywrightError) as exc:
        print("运行未完成：", exc)
