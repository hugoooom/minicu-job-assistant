"""自动采集搜索列表与详情；先确认登录，按需滚动加载。"""

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
from salary import normalize_salary

from extract_first_job import CardParser, needs_ocr
from inspect_cdp import OUTPUT, PORT, PROFILE, build_command, ensure_port_free, find_edge, wait_for_cdp
from inspect_raw_cdp import RawCDP, list_pages


STYLE_NAMES = ["display", "visibility", "opacity", "font-size"]
CITY_CODES = {"广州": "101280100", "深圳": "101280600"}


class AccessGate(RuntimeError):
    pass


class LoginRequired(AccessGate):
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
    for marker in ("登录查看完整内容", "请登录后查看", "登录后查看完整"):
        if marker in text:
            raise LoginRequired(f"需要登录：{marker}")
    for marker in ("请完成安全验证", "您的环境存在异常", "访问过于频繁"):
        if marker in text:
            raise AccessGate(f"页面要求处理：{marker}；已停止本轮采集。")


def navigate(cdp, url):
    result = cdp.call("Page.navigate", {"url": url})
    if result.get("errorText"):
        raise RuntimeError(f"页面导航失败：{result['errorText']}")


def authentication_state(cdp, root=None, page_text=None):
    root = root or document(cdp)
    text = rendered_text(snapshot(cdp)) if page_text is None else page_text
    try:
        check_gate(text)
    except LoginRequired:
        return "logged_out"
    # 游客也有职位列表。必须出现可见的账户入口和退出登录节点才确认登录。
    account = query(cdp, root["nodeId"], ".user-nav .nav-figure")
    if account and query(cdp, account, "a.link-logout"):
        try:
            box = cdp.call("DOM.getBoxModel", {"nodeId": account})["model"]
            if box["width"] > 0 and box["height"] > 0:
                return "authenticated"
        except RuntimeError:
            pass
    return "unconfirmed"


def wait_authenticated(cdp, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if authentication_state(cdp) == "authenticated":
                return True
        except AccessGate:
            raise
        except RuntimeError:
            pass
        time.sleep(0.5)
    return False


def require_login(cdp, return_url, no_prompt=False):
    if wait_authenticated(cdp, 6):
        return
    if no_prompt:
        raise LoginRequired("未确认已登录，--no-prompt 模式已停止，不采集游客数据。")
    print("尚未确认登录。请在本次 Edge 任务页点击登录/注册并完成登录。", flush=True)
    input("登录完成后回到终端按回车；程序会重新验证登录状态：")
    navigate(cdp, return_url)
    if not wait_authenticated(cdp, 20):
        raise LoginRequired("仍未检测到有效登录状态，未继续采集。")
    print("已确认登录，继续采集。", flush=True)


def wait_cards(cdp, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root = document(cdp)
            cards = cdp.call("DOM.querySelectorAll", {
                "nodeId": root["nodeId"], "selector": "li.job-card-box"
            }).get("nodeIds", [])
            if cards:
                if authentication_state(cdp, root) != "authenticated":
                    raise LoginRequired("列表已出现，但当前未确认已登录。")
                return cards
        except AccessGate:
            raise
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
        "clip": {"x": max(0, min(xs) - 3), "y": max(0, min(ys) - 3),
                 "width": width + 6, "height": height + 6, "scale": 2}
    })
    path.write_bytes(base64.b64decode(result["data"]))


def read_list(cdp, nodes, folder, skip_ids=None, limit=None, engine_state=None, on_job=None):
    jobs, seen = [], set(skip_ids or [])
    engine_state = engine_state if engine_state is not None else [None]
    for index, node in enumerate(nodes, start=1):
        if limit is not None and len(jobs) >= limit:
            break
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
                if engine_state[0] is None:
                    engine_state[0] = RapidOCR(params={"Global.log_level": "warning"})
                result = engine_state[0](str(path), use_det=False, use_cls=False, use_rec=True)
                if not result.txts or not any(text.strip() for text in result.txts):
                    raise ValueError("薪资 OCR 结果为空。")
                job.update(salary=" ".join(result.txts), salary_source="OCR",
                           salary_ocr_scores=[float(score) for score in result.scores])
            else:
                job.update(salary=job["salary_raw"], salary_source="DOM")
            job.update(normalize_salary(job["salary"], job["salary_raw"]))
        except (RuntimeError, ValueError, OSError) as exc:
            job.update(salary=None, salary_source="error", salary_status="error",
                       salary_unit=None, salary_period=None, salary_error=str(exc))
        job.pop("salary_raw", None)
        jobs.append(job)
        if on_job:
            on_job(job)
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
                check_gate(rendered_text(snapshot(cdp)))
                time.sleep(0.4)
                continue
            description_id = query(cdp, root["nodeId"], ".job-detail-body .desc, .job-sec-text")
            if description_id:
                backend = cdp.call("DOM.describeNode", {"nodeId": description_id})["node"]["backendNodeId"]
                data = snapshot(cdp)
                page_text = rendered_text(data)
                check_gate(page_text)
                if authentication_state(cdp, root, page_text) != "authenticated":
                    raise LoginRequired("详情已出现，但未确认已登录，不能将游客预览视作完整描述。")
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


def read_description_with_login(cdp, job, folder, no_prompt=False):
    try:
        return read_description(cdp, job, folder)
    except LoginRequired:
        require_login(cdp, job["url"], no_prompt=no_prompt)
        return read_description(cdp, job, folder)


def scroll_list(cdp, nodes):
    last = nodes[-1]
    cdp.call("DOM.scrollIntoViewIfNeeded", {"nodeId": last})
    box = cdp.call("DOM.getBoxModel", {"nodeId": last})["model"]["border"]
    viewport = cdp.call("Page.getLayoutMetrics")["cssVisualViewport"]
    x = (min(box[0::2]) + max(box[0::2])) / 2 - viewport["pageX"]
    y = (min(box[1::2]) + max(box[1::2])) / 2 - viewport["pageY"]
    x = max(1, min(x, viewport["clientWidth"] - 1))
    y = max(1, min(y, viewport["clientHeight"] - 1))
    cdp.call("Input.dispatchMouseEvent", {
        "type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": 650
    })


def update_report(payload):
    jobs = payload["jobs"]
    payload["collected_count"] = len(jobs)
    payload["missing_count"] = max(0, payload["requested_count"] - len(jobs))
    payload["description_success_count"] = sum(job["description_status"] == "ok" for job in jobs)
    payload["salary_success_count"] = sum(job.get("salary_status") in ("ok", "negotiable") for job in jobs)
    payload["fully_extracted_count"] = sum(
        job["description_status"] == "ok" and job.get("salary_status") in ("ok", "negotiable") for job in jobs
    )


def collect_list(cdp, payload, folder, path, list_url, no_prompt, max_scrolls):
    seen, engine_state, unchanged, scrolls = set(), [None], 0, 0

    def save_one(job):
        payload["jobs"].append(job)
        seen.add(job["id"])
        update_report(payload)
        save_jobs(path, payload)

    while len(payload["jobs"]) < payload["requested_count"]:
        try:
            nodes = wait_cards(cdp)
        except LoginRequired:
            require_login(cdp, list_url, no_prompt)
            nodes = wait_cards(cdp)
        before = len(payload["jobs"])
        read_list(cdp, nodes, folder, skip_ids=seen,
                  limit=payload["requested_count"] - before,
                  engine_state=engine_state, on_job=save_one)
        if len(payload["jobs"]) >= payload["requested_count"]:
            return "requested_limit_reached"
        unchanged = unchanged + 1 if len(payload["jobs"]) == before else 0
        if unchanged >= 3:
            return "no_new_results_after_scroll"
        if scrolls >= max_scrolls:
            return "max_scrolls_reached"
        print(f"当前取得 {len(payload['jobs'])}/{payload['requested_count']} 条，继续加载列表……", flush=True)
        scroll_list(cdp, nodes)
        scrolls += 1
        time.sleep(1)
    return "requested_limit_reached"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="Python")
    parser.add_argument("--city", choices=CITY_CODES, default="广州")
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=3, metavar="1-100")
    parser.add_argument("--max-scrolls", type=int, default=12, help="最多滚动加载次数，默认 12")
    parser.add_argument("--no-prompt", action="store_true", help="未确认登录时直接退出，不采集游客数据")
    args = parser.parse_args()
    if args.max_scrolls < 0:
        parser.error("--max-scrolls 不能小于 0")
    ensure_browser()
    folder = OUTPUT / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    path = folder / "jobs.json"
    target = open_task_tab()
    connection = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
    payload = {"keyword": args.keyword, "city": args.city, "status": "running",
               "requested_count": args.limit, "jobs": []}
    try:
        cdp = RawCDP(connection)
        cdp.call("Page.enable")
        list_url = "https://www.zhipin.com/web/geek/job?" + urlencode({
            "query": args.keyword, "city": CITY_CODES[args.city]
        })
        print("自动打开搜索页……", flush=True)
        navigate(cdp, list_url)
        require_login(cdp, list_url, args.no_prompt)
        payload["list_stop_reason"] = collect_list(
            cdp, payload, folder, path, list_url, args.no_prompt, args.max_scrolls
        )
        print(f"列表取得 {len(payload['jobs'])}/{args.limit} 条，停止原因：{payload['list_stop_reason']}", flush=True)
        save_jobs(path, payload)
        for index, job in enumerate(payload["jobs"], start=1):
            print(f"详情 {index}/{len(payload['jobs'])}：{job['title']}", flush=True)
            try:
                job["description"] = read_description_with_login(cdp, job, folder, args.no_prompt)
                job["description_source"] = "CDP_DOMSnapshot"
                job["description_status"] = "ok"
                job["description_characters"] = len(job["description"])
                print(f"  已保存职位描述，{len(job['description'])} 字符。", flush=True)
            except AccessGate as exc:
                job.update(description_status="blocked", description_error=str(exc))
                raise
            except (RuntimeError, ValueError, OSError, websocket.WebSocketException) as exc:
                job.update(description_status="error", description_error=str(exc))
                print("  本岗位详情未完成：", exc, flush=True)
            update_report(payload)
            save_jobs(path, payload)
            if index < len(payload["jobs"]):
                time.sleep(2)
        update_report(payload)
        payload["status"] = "complete" if payload["fully_extracted_count"] == args.limit else "partial"
    except (RuntimeError, ValueError, OSError, websocket.WebSocketException) as exc:
        payload.update(status="stopped", error=str(exc))
        print("本轮停止：", exc, flush=True)
    finally:
        for job in payload["jobs"]:
            if job["description_status"] == "pending":
                job.update(description_status="not_processed",
                           description_error=payload.get("error", "本轮尚未处理到此岗位。"))
        update_report(payload)
        save_jobs(path, payload)
        connection.close()
        try:
            close_task_tab(target["id"])
        except (OSError, RuntimeError, websocket.WebSocketException):
            pass
    print("结果已保存：", path)
    print(f"请求 {args.limit} 条，列表 {payload['collected_count']} 条，"
          f"薪资成功 {payload['salary_success_count']} 条，详情成功 {payload['description_success_count']} 条。")
    return 0 if payload["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
