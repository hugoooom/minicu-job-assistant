"""自动采集当前搜索结果首屏岗位和详情；首次登录由使用者完成。"""

import argparse
import base64
import json
import subprocess
import time
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener

import websocket
from rapidocr import RapidOCR

from extract_first_job import CardParser, needs_ocr
from inspect_cdp import OUTPUT, PORT, PROFILE, build_command, ensure_port_free, find_edge, wait_for_cdp
from inspect_raw_cdp import RawCDP, list_pages


STYLE_NAMES = ["display", "visibility", "opacity", "font-size"]
CITY_CODES = {"广州": "101280100", "深圳": "101280600"}


class AccessGate(RuntimeError):
    pass


def browser_info():
    with build_opener(ProxyHandler({})).open(
        f"http://127.0.0.1:{PORT}/json/version", timeout=2
    ) as response:
        return json.load(response)


def ensure_browser():
    try:
        info = browser_info()
    except OSError:
        ensure_port_free(PORT)
        PROFILE.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            build_command(find_edge(), PROFILE, PORT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
        wait_for_cdp(PORT)
        info = browser_info()
    if not info.get("webSocketDebuggerUrl"):
        raise RuntimeError("端口上不是可用的浏览器 CDP 服务。")
    return info


def root_connection():
    return websocket.create_connection(
        browser_info()["webSocketDebuggerUrl"], timeout=10, suppress_origin=True
    )


def open_task_tab():
    connection = root_connection()
    try:
        target_id = RawCDP(connection).call("Target.createTarget", {"url": "about:blank"})["targetId"]
    finally:
        connection.close()
    for _ in range(20):
        target = next((page for page in list_pages() if page["id"] == target_id), None)
        if target:
            return target
        time.sleep(0.1)
    raise RuntimeError("未找到新建的任务标签页。")


def close_task_tab(target_id):
    connection = root_connection()
    try:
        RawCDP(connection).call("Target.closeTarget", {"targetId": target_id})
    finally:
        connection.close()


def document(cdp):
    return cdp.call("DOM.getDocument", {"depth": 0})["root"]


def query(cdp, root_id, selector):
    return cdp.call("DOM.querySelector", {"nodeId": root_id, "selector": selector}).get("nodeId", 0)


def outer_html(cdp, node_id):
    return cdp.call("DOM.getOuterHTML", {"nodeId": node_id})["outerHTML"]


def rendered_text(snapshot, backend_id=None):
    """按渲染布局读取文本，排除样式隐藏的节点及其后代。"""
    strings = snapshot["strings"]
    for doc in snapshot["documents"]:
        nodes, layout = doc["nodes"], doc["layout"]
        if backend_id is not None and backend_id not in nodes["backendNodeId"]:
            continue
        root = nodes["backendNodeId"].index(backend_id) if backend_id is not None else 0
        parents = nodes["parentIndex"]
        hidden = set()
        for index, node in enumerate(layout["nodeIndex"]):
            values = [strings[value] for value in layout["styles"][index]]
            styles = dict(zip(STYLE_NAMES, values))
            if (styles.get("display") == "none" or styles.get("visibility") in ("hidden", "collapse")
                    or styles.get("opacity") == "0" or styles.get("font-size") == "0px"):
                hidden.add(node)

        def included(node):
            found = False
            while node >= 0:
                if node in hidden:
                    return False
                if node == root:
                    found = True
                node = parents[node]
            return found

        pieces = []
        for index, node in enumerate(layout["nodeIndex"]):
            if not included(node):
                continue
            name = strings[nodes["nodeName"][node]]
            if name == "BR":
                pieces.append("\n")
            elif name == "#text":
                bounds = layout["bounds"][index]
                if bounds[2] > 0 and bounds[3] > 0:
                    pieces.append(strings[layout["text"][index]])
        text = "".join(pieces)
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return ""


def snapshot(cdp):
    return cdp.call("DOMSnapshot.captureSnapshot", {"computedStyles": STYLE_NAMES})


def check_gate(text):
    for marker in ("登录查看完整内容", "请完成安全验证", "您的环境存在异常", "访问过于频繁"):
        if marker in text:
            raise AccessGate(f"页面要求处理：{marker}；已停止本轮采集。")


def navigate(cdp, url):
    result = cdp.call("Page.navigate", {"url": url})
    if result.get("errorText"):
        raise RuntimeError(f"页面导航失败：{result['errorText']}")


def wait_cards(cdp, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root = document(cdp)
            cards = cdp.call("DOM.querySelectorAll", {
                "nodeId": root["nodeId"], "selector": "li.job-card-box"
            }).get("nodeIds", [])
            if cards:
                return cards
        except RuntimeError:
            pass  # 导航时节点会失效，重新获取根节点。
        time.sleep(0.4)
    raise TimeoutError("20 秒内未出现岗位列表；可能需要登录、处理页面提示或调整选择器。")


def salary_screenshot(cdp, node_id, path):
    cdp.call("DOM.scrollIntoViewIfNeeded", {"nodeId": node_id})
    box = cdp.call("DOM.getBoxModel", {"nodeId": node_id})["model"]["border"]
    xs, ys = box[0::2], box[1::2]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    if width <= 0 or height <= 0:
        raise ValueError("薪资区域不可见。")
    result = cdp.call("Page.captureScreenshot", {
        "format": "png", "captureBeyondViewport": True,
        "clip": {"x": min(xs), "y": min(ys), "width": width, "height": height, "scale": 1}
    })
    path.write_bytes(base64.b64decode(result["data"]))


def read_list(cdp, nodes, folder):
    jobs, seen, engine = [], set(), None
    for index, node in enumerate(nodes, start=1):
        parser = CardParser()
        html = outer_html(cdp, node)
        parser.feed(html)
        job = parser.fields()
        url = urljoin("https://www.zhipin.com", parser.href)
        parsed = urlsplit(url)
        if parsed.hostname != "www.zhipin.com" or not parsed.path.startswith("/job_detail/"):
            raise ValueError("岗位卡片缺少有效的 BOSS 详情链接。")
        job_id = PurePosixPath(parsed.path).stem
        if job_id in seen:
            continue
        seen.add(job_id)
        job.update(id=job_id, url=url, description=None, description_status="pending")
        (folder / f"{job_id}-card.html").write_text(html, encoding="utf-8")
        try:
            if needs_ocr(job["salary_raw"]):
                salary_id = query(cdp, node, ".job-salary")
                if not salary_id:
                    raise ValueError("缺少薪资元素。")
                path = folder / f"{job_id}-salary.png"
                salary_screenshot(cdp, salary_id, path)
                if engine is None:
                    engine = RapidOCR(params={"Global.log_level": "warning"})
                result = engine(str(path), use_det=False, use_cls=False, use_rec=True)
                if not result.txts or not any(text.strip() for text in result.txts):
                    raise ValueError("薪资 OCR 结果为空。")
                job.update(salary=" ".join(result.txts), salary_source="OCR",
                           salary_ocr_scores=[float(score) for score in result.scores])
            else:
                job.update(salary=job["salary_raw"], salary_source="DOM")
        except (RuntimeError, ValueError, OSError) as exc:
            job.update(salary=None, salary_source="error", salary_error=str(exc))
        job.pop("salary_raw", None)
        jobs.append(job)
        print(f"列表 {index}/{len(nodes)}：{job['title']}，薪资 {job.get('salary')}", flush=True)
    return jobs


def read_description(cdp, job, folder, timeout=20):
    navigate(cdp, job["url"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root = document(cdp)
            current = urlsplit(root.get("documentURL", ""))
            if current.path != urlsplit(job["url"]).path:
                time.sleep(0.4)
                continue
            description_id = query(cdp, root["nodeId"], ".job-detail-body .desc, .job-sec-text")
            if description_id:
                backend = cdp.call("DOM.describeNode", {"nodeId": description_id})["node"]["backendNodeId"]
                data = snapshot(cdp)
                check_gate(rendered_text(data))
                text = rendered_text(data, backend)
                if len(text) >= 40:
                    if needs_ocr(text):
                        raise AccessGate("职位描述含私用区字符，尚不能确认正文正确，停止本轮采集。")
                    (folder / f"{job['id']}-description.txt").write_text(text, encoding="utf-8")
                    return text
        except AccessGate:
            raise
        except RuntimeError:
            pass
        time.sleep(0.4)
    check_gate(rendered_text(snapshot(cdp)))
    raise TimeoutError("未获取到与岗位 ID 对应的职位描述；可能是跳转、内容未加载或选择器变化。")


def save_jobs(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="Python")
    parser.add_argument("--city", choices=CITY_CODES, default="广州")
    parser.add_argument("--limit", type=int, choices=range(1, 11), default=3, metavar="1-10")
    parser.add_argument("--no-prompt", action="store_true", help="列表未加载时直接报错，不等待登录")
    args = parser.parse_args()
    ensure_browser()
    folder = OUTPUT / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    path = folder / "jobs.json"
    target = open_task_tab()
    connection = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
    payload = {"keyword": args.keyword, "city": args.city, "status": "running", "jobs": []}
    try:
        cdp = RawCDP(connection)
        cdp.call("Page.enable")
        list_url = "https://www.zhipin.com/web/geek/job?" + urlencode({
            "query": args.keyword, "city": CITY_CODES[args.city]
        })
        print("自动打开搜索页……", flush=True)
        navigate(cdp, list_url)
        try:
            nodes = wait_cards(cdp)
        except TimeoutError:
            check_gate(rendered_text(snapshot(cdp)))
            if args.no_prompt:
                raise
            input("列表未出现。请检查测试窗口；如需登录，请手动完成，完成后按回车继续：")
            navigate(cdp, list_url)
            nodes = wait_cards(cdp)
        payload["jobs"] = read_list(cdp, nodes[:args.limit], folder)
        save_jobs(path, payload)
        for index, job in enumerate(payload["jobs"], start=1):
            print(f"详情 {index}/{len(payload['jobs'])}：{job['title']}", flush=True)
            try:
                job["description"] = read_description(cdp, job, folder)
                job["description_source"] = "CDP_DOMSnapshot"
                job["description_status"] = "ok"
                print(f"  已保存职位描述，{len(job['description'])} 字符。", flush=True)
            except AccessGate as exc:
                job.update(description_status="blocked", description_error=str(exc))
                raise
            except (RuntimeError, ValueError, OSError, websocket.WebSocketException) as exc:
                job.update(description_status="error", description_error=str(exc))
                print("  本岗位详情未完成：", exc, flush=True)
            save_jobs(path, payload)
            if index < len(payload["jobs"]):
                time.sleep(2)
        successes = sum(job["description_status"] == "ok" for job in payload["jobs"])
        payload["status"] = "complete" if successes == len(payload["jobs"]) else "partial"
    except (RuntimeError, ValueError, OSError, websocket.WebSocketException) as exc:
        payload.update(status="stopped", error=str(exc))
        print("本轮停止：", exc, flush=True)
    finally:
        save_jobs(path, payload)
        connection.close()
        try:
            close_task_tab(target["id"])
        except (OSError, RuntimeError, websocket.WebSocketException):
            pass
    print("结果已保存：", path)
    print("详情成功数：", sum(job["description_status"] == "ok" for job in payload["jobs"]))
    return 0 if payload["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
