"""原生 CDP 对照：检查连接、Page、Network 和 DOM，不启用 Runtime。"""

import base64
import json
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

import websocket

from inspect_cdp import OUTPUT, PORT, PROFILE, build_command, ensure_port_free, find_edge, wait_for_cdp


def short_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def list_pages():
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as response:
        return [item for item in json.load(response) if item.get("type") == "page"]


class RawCDP:
    def __init__(self, connection):
        self.ws = connection
        self.number = 0

    def call(self, method, params=None, timeout=10):
        self.number += 1
        command_id = self.number
        self.ws.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.ws.settimeout(min(1, max(0.01, deadline - time.monotonic())))
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise ConnectionError("浏览器关闭了 CDP 连接。")
            message = json.loads(raw)
            if message.get("id") != command_id:
                continue  # 事件通知和命令响应可能交错；不将事件误当响应。
            if "error" in message:
                raise RuntimeError(f"{method} 失败：{message['error']}")
            return message.get("result", {})
        raise TimeoutError(f"{method} 等待响应超时。")


def save_dom(cdp, output):
    document = cdp.call("DOM.getDocument", {"depth": 0})
    root = document["root"]
    document_url = root.get("documentURL", "")
    if document_url.startswith(("about:blank", "edge-error:")):
        raise RuntimeError("读取时页面已变为空白或错误页，未保存为岗位页面。")
    result = cdp.call("DOM.getOuterHTML", {"nodeId": root["nodeId"]})
    html = result.get("outerHTML", "")
    if not html.strip():
        raise RuntimeError("页面 HTML 为空。")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "raw-page.html"
    path.write_text(html, encoding="utf-8")
    print(f"DOM 已保存：{path}，共 {len(html)} 个字符。")
    print("文档地址：", short_url(document_url))
    return path


def capture_first_salary(cdp, output):
    document = cdp.call("DOM.getDocument", {"depth": 0})
    cards = cdp.call("DOM.querySelectorAll", {
        "nodeId": document["root"]["nodeId"], "selector": "li.job-card-box"
    }).get("nodeIds", [])
    print(f"当前页面匹配到 {len(cards)} 张岗位卡片。")
    if not cards:
        raise RuntimeError("没有找到岗位卡片，请确认当前显示的是岗位列表。")
    card_id = cards[0]
    card_html = cdp.call("DOM.getOuterHTML", {"nodeId": card_id})["outerHTML"]
    (output / "raw-first-card.html").write_text(card_html, encoding="utf-8")
    salary_id = cdp.call("DOM.querySelector", {
        "nodeId": card_id, "selector": ".job-salary"
    }).get("nodeId", 0)
    if not salary_id:
        raise RuntimeError("第一张卡片中没有找到薪资元素。")
    cdp.call("DOM.scrollIntoViewIfNeeded", {"nodeId": salary_id})
    box = cdp.call("DOM.getBoxModel", {"nodeId": salary_id})["model"]["border"]
    xs, ys = box[0::2], box[1::2]
    x, y = min(xs), min(ys)
    width, height = max(xs) - x, max(ys) - y
    if width <= 0 or height <= 0:
        raise RuntimeError("薪资元素没有可截图的区域。")
    image = cdp.call("Page.captureScreenshot", {
        "format": "png", "captureBeyondViewport": True,
        "clip": {"x": x, "y": y, "width": width, "height": height, "scale": 1}
    })
    path = output / "raw-salary.png"
    path.write_bytes(base64.b64decode(image["data"]))
    print("第一张卡片薪资截图已保存：", path)
    return path


def observe(label, target_id):
    input(f"{label}：观察页面是否跳转，然后回到终端按回车……")
    target = next((item for item in list_pages() if item.get("id") == target_id), None)
    if target is None:
        print("目标标签页已经关闭。本轮到此结束。")
        return False
    url = target.get("url", "")
    print("当前地址：", short_url(url))
    if url.startswith("about:blank") or url.startswith("edge-error:"):
        print("目标已经变为空白或错误页。本轮到此结束。")
        return False
    return input("页面仍正常请输入 y；已异常请输入 n：").strip().lower() == "y"


def main():
    ensure_port_free(PORT)
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        build_command(find_edge(), PROFILE, PORT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    )
    wait_for_cdp(PORT)
    print("请手动进入 BOSS 岗位列表。此时尚未连接任何网页 CDP 会话。")
    if input("页面正常按回车；已异常输入 n：").strip().lower() == "n":
        return
    pages = list_pages()
    for index, page in enumerate(pages, start=1):
        print(f"{index}. {short_url(page.get('url', ''))}")
    choice = int(input("请选择岗位标签页编号："))
    if not 1 <= choice <= len(pages):
        raise ValueError("编号不在列表内。")
    target = pages[choice - 1]
    url = target.get("url", "")
    if not url.startswith(("https://", "http://")):
        raise ValueError("所选标签页不是正常网页，请先打开岗位页面。")

    print("阶段 1：只建立原生 WebSocket 连接，不发送页面初始化命令。", flush=True)
    connection = websocket.create_connection(
        target["webSocketDebuggerUrl"], timeout=10, suppress_origin=True
    )
    try:
        cdp = RawCDP(connection)
        if not observe("阶段 1", target["id"]):
            return

        print("阶段 2：启用 Page，并读取页面框架地址。", flush=True)
        cdp.call("Page.enable")
        tree = cdp.call("Page.getFrameTree")
        print("框架地址：", short_url(tree["frameTree"]["frame"].get("url", "")))
        if not observe("阶段 2", target["id"]):
            return

        print("阶段 3：启用 Network 事件监听，不发送业务请求。", flush=True)
        cdp.call("Network.enable")
        if not observe("阶段 3", target["id"]):
            return
        print("三个阶段均正常。这个结果仅证明本次原生 CDP 对照未触发跳转。")

        print("阶段 4：读取当前页面 DOM，保存 HTML。", flush=True)
        save_dom(cdp, OUTPUT)
        if not observe("阶段 4", target["id"]):
            return
        print("DOM 读取后页面仍正常。下一步可以从保存的结构中定位岗位容器。")

        print("阶段 5：定位第一张岗位卡片，并截取其中的薪资区域。", flush=True)
        capture_first_salary(cdp, OUTPUT)
    finally:
        connection.close()
        print("已断开 CDP；测试浏览器请手动关闭。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, websocket.WebSocketException) as exc:
        print("运行未完成：", exc)
