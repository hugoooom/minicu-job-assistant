"""自动采集搜索列表与详情；先确认登录，按需滚动加载。"""

import argparse
import base64
import json
import hashlib
import re
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import unescape
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener

import websocket
from rapidocr import RapidOCR
from salary import normalize_salary
from job_history import job_identity, load_history
from turn_csv import export_csv
from runtime_check import check_ocr_runtime
from description_ocr import CapturedDescription, DescriptionOCRWorker, capture_description
from search_regions import CITY_CODES, DISTRICTS, search_url, selected_districts
from district_progress import DistrictProgress
from job_db import turn_db
from history_repairs import load_missing_jobs, missing_fields, valid_job_url

from extract_first_job import CardParser, needs_ocr
from inspect_cdp import OUTPUT, PORT, PROFILE, build_command, ensure_port_free, find_edge, wait_for_cdp
from inspect_raw_cdp import RawCDP, list_pages


STYLE_NAMES = ["display", "visibility", "opacity", "font-size"]


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


def screenshot_geometry(box, metrics, padding=8):
    """元素四边形是视口坐标，截图 clip 使用文档坐标，需要加上页面滚动量。"""
    xs, ys = box[0::2], box[1::2]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    if width <= 0 or height <= 0:
        raise ValueError("薪资区域不可见。")
    viewport = metrics["cssLayoutViewport"]
    scroll_x, scroll_y = viewport["pageX"], viewport["pageY"]
    left, top = max(0, min(xs) + scroll_x - padding), max(0, min(ys) + scroll_y - padding)
    right, bottom = max(xs) + scroll_x + padding, max(ys) + scroll_y + padding
    return {
        "viewport_rect": {"x": min(xs), "y": min(ys), "width": width, "height": height},
        "page_scroll": {"x": scroll_x, "y": scroll_y},
        "clip": {"x": left, "y": top, "width": right - left, "height": bottom - top, "scale": 2},
    }


def stable_screenshot_geometry(cdp, node_id, timeout=3):
    deadline, previous, stable = time.monotonic() + timeout, None, 0
    while time.monotonic() < deadline:
        before = cdp.call("Page.getLayoutMetrics")
        box = cdp.call("DOM.getBoxModel", {"nodeId": node_id})["model"]["border"]
        after = cdp.call("Page.getLayoutMetrics")
        # 如果读取边界期间仍在滚动，这份边界和滚动偏移不能混用。
        if before["cssLayoutViewport"] != after["cssLayoutViewport"]:
            stable, previous = 0, None
            time.sleep(0.05)
            continue
        geometry = screenshot_geometry(box, after)
        signature = tuple(round(value, 2) for group in (geometry["clip"], geometry["page_scroll"])
                          for value in group.values())
        stable = stable + 1 if signature == previous else 1
        if stable >= 3:
            return geometry
        previous = signature
        time.sleep(0.05)
    raise ValueError("滚动或布局仍在变化，未截取不确定区域。")


def salary_screenshot(cdp, node_id, path):
    cdp.call("DOM.scrollIntoViewIfNeeded", {"nodeId": node_id})
    geometry = stable_screenshot_geometry(cdp, node_id)
    result = cdp.call("Page.captureScreenshot", {
        "format": "png", "captureBeyondViewport": True,
        "clip": geometry["clip"]
    })
    path.write_bytes(base64.b64decode(result["data"]))
    path.with_suffix(".capture.json").write_text(
        json.dumps(geometry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return geometry


def read_list(cdp, nodes, folder, skip_keys=None, limit=None, engine_state=None, on_job=None,
              observed_keys=None):
    jobs, seen = [], set(skip_keys or [])
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
        job.update(id=job_id, url=url)
        key = job_identity(job)
        if observed_keys is not None:
            observed_keys.add(key)
        if key in seen:
            continue
        seen.add(key)
        job.update(description=None, description_status="pending")
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


def read_description(cdp, job, folder, timeout=20, capture_only=False, navigate_page=True):
    if navigate_page:
        navigate(cdp, job["url"])
    deadline = time.monotonic() + timeout
    candidate, candidate_since = None, None
    last_reason = "页面尚未就绪"
    while time.monotonic() < deadline:
        try:
            root = document(cdp)
            current = urlsplit(root.get("documentURL", ""))
            if current.path != urlsplit(job["url"]).path:
                candidate, candidate_since = None, None
                last_reason = "当前页面 URL 与目标岗位不一致"
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
                text = rendered_text(data, backend).strip()
                now = time.monotonic()
                loading = text.strip(" .…。，\t\r\n").casefold() in ("加载中", "正在加载", "loading")
                if text and not needs_ocr(text) and not loading:
                    if text != candidate:
                        candidate, candidate_since = text, now
                    # 短正文也可能完整；稍等文字稳定，避免把刚加载的片段当作正文。
                    dom_ready = len(text) >= 40 or now - candidate_since >= 1
                    last_reason = f"正文共 {len(text)} 字符，等待文字稳定"
                else:
                    candidate, candidate_since = None, None
                    dom_ready = False
                    last_reason = "正文仍在加载" if loading else "正文为空或含混淆字符"
                ocr_ready = needs_ocr(text) and (
                    bool(text) or now >= deadline - timeout + 2
                )
                if dom_ready or ocr_ready:
                    if needs_ocr(text):
                        from description_ocr import extract_description

                        print("  正文含混淆字符或无法读取，改用分段多行 OCR……", flush=True)

                        def guard():
                            state = authentication_state(cdp)
                            if state != "authenticated":
                                raise LoginRequired("正文 OCR 期间未确认登录，等待重新登录。")

                        def resolve_description_node():
                            fresh_root = document(cdp)
                            if urlsplit(fresh_root.get("documentURL", "")).path != urlsplit(job["url"]).path:
                                raise ValueError("正文截图时岗位页面已变化。")
                            fresh_node = query(cdp, fresh_root["nodeId"], ".job-detail-body .desc, .job-sec-text")
                            if not fresh_node:
                                raise ValueError("正文节点已消失。")
                            return fresh_node

                        try:
                            if capture_only:
                                return capture_description(
                                    cdp, description_id, folder, job["id"], stable_screenshot_geometry,
                                    guard, resolve_description_node
                                )
                            text, metadata = extract_description(
                                cdp, description_id, folder, job["id"], stable_screenshot_geometry,
                                guard, resolve_description_node
                            )
                        except AccessGate:
                            raise
                        except (RuntimeError, OSError) as exc:
                            raise ValueError(f"本岗位正文 OCR 失败：{exc}") from exc
                        if needs_ocr(text):
                            raise ValueError("本岗位 OCR 结果仍含混淆字符，未标为成功。")
                        job.update(metadata)
                    else:
                        job["description_source"] = "CDP_DOMSnapshot"
                    (folder / f"{job['id']}-description.txt").write_text(text, encoding="utf-8")
                    return text
            else:
                candidate, candidate_since = None, None
                last_reason = "未找到职位描述容器"
        except AccessGate:
            raise
        except RuntimeError as exc:
            candidate, candidate_since = None, None
            last_reason = f"读取页面失败：{exc}"
        time.sleep(0.4)
    check_gate(rendered_text(snapshot(cdp)))
    raise TimeoutError(f"未获取到与岗位 ID 对应的职位描述：{last_reason}。")


def save_jobs(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_description_with_login(cdp, job, folder, no_prompt=False, capture_only=False, navigate_page=True):
    try:
        return read_description(cdp, job, folder, capture_only=capture_only, navigate_page=navigate_page)
    except LoginRequired:
        require_login(cdp, job["url"], no_prompt=no_prompt)
        return read_description(cdp, job, folder, capture_only=capture_only, navigate_page=navigate_page)


def scroll_list(cdp, nodes):
    last = nodes[-1]
    cdp.call("DOM.scrollIntoViewIfNeeded", {"nodeId": last})
    box = cdp.call("DOM.getBoxModel", {"nodeId": last})["model"]["border"]
    viewport = cdp.call("Page.getLayoutMetrics")["cssVisualViewport"]
    # 元素边界和鼠标事件都使用视口坐标，不重复减去页面滚动偏移。
    x = (min(box[0::2]) + max(box[0::2])) / 2
    y = (min(box[1::2]) + max(box[1::2])) / 2
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


def list_scroll_budget(historical_count, explicit_limit=None):
    """保留 12 次基础预算，按约 15 条/屏增加翻过历史岗位的余量。"""
    if explicit_limit is not None:
        return explicit_limit
    return 12 + (historical_count + 14) // 15


def collect_list(cdp, payload, folder, path, list_url, no_prompt, max_scrolls, excluded_keys=None,
                 district=None, shared_state=None):
    excluded_keys = set(excluded_keys or ())
    seen = excluded_keys | {job_identity(job) for job in payload["jobs"]}
    shared_state = shared_state if shared_state is not None else {}
    observed, unchanged, scrolls = set(), 0, 0
    engine_state = shared_state.setdefault("engine_state", [None])
    history_observed = shared_state.setdefault("history_observed", set())
    previous_scrolls = payload.get("list_scroll_count", 0)
    payload["list_scroll_limit"] = max_scrolls
    payload["list_scroll_count"] = previous_scrolls

    def save_one(job):
        if district is not None:
            job.update(search_district=district, search_district_code=DISTRICTS[payload["city"]][district])
        payload["jobs"].append(job)
        seen.add(job_identity(job))
        update_report(payload)
        save_jobs(path, payload)

    finish_district = district is not None and payload.get("finish_district", False)
    empty_rounds = payload.get("empty_rounds", 3)
    while finish_district or len(payload["jobs"]) < payload["requested_count"]:
        try:
            nodes = wait_cards(cdp)
        except LoginRequired:
            require_login(cdp, list_url, no_prompt)
            nodes = wait_cards(cdp)
        before = len(payload["jobs"])
        observed_before = len(observed)
        read_list(cdp, nodes, folder, skip_keys=seen,
                  limit=None if finish_district else payload["requested_count"] - before,
                  engine_state=engine_state, on_job=save_one, observed_keys=observed)
        history_observed.update(observed & excluded_keys)
        payload["historical_skipped_count"] = len(history_observed)
        save_jobs(path, payload)
        if not finish_district and len(payload["jobs"]) >= payload["requested_count"]:
            return "requested_limit_reached"
        # 前几屏可能全是历史岗位；只要看到了不同的岗位标识，就继续向后加载。
        unchanged = unchanged + 1 if len(observed) == observed_before else 0
        if unchanged >= empty_rounds:
            return "no_new_results_after_scroll"
        if scrolls >= max_scrolls:
            return "max_scrolls_reached"
        print(f"当前取得 {len(payload['jobs'])}/{payload['requested_count']} 条，"
              f"已跳过 {payload['historical_skipped_count']} 个历史岗位，继续加载列表……", flush=True)
        scroll_list(cdp, nodes)
        scrolls += 1
        payload["list_scroll_count"] = previous_scrolls + scrolls
        time.sleep(1)
    return "requested_limit_reached"


def wait_search_scope(cdp, city, keyword, district, timeout=12):
    """防止跳转/旧列表被错误登记到另一个城市或区。"""
    expected = parse_qs(urlsplit(search_url(city, keyword, district)).query)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root = document(cdp)
            actual = parse_qs(urlsplit(root.get("documentURL", "")).query)
            if all(actual.get(key) == value for key, value in expected.items()):
                city_id = query(cdp, root["nodeId"], ".cur-city-label")
                area_id = query(cdp, root["nodeId"], '[ka^="sel-business-"].active')
                # 标签文本可以含嵌套元素；用现有 HTMLParser 去掉标签。
                city_text = unescape(re.sub("<[^>]+>", "", outer_html(cdp, city_id))) if city_id else ""
                area_text = unescape(re.sub("<[^>]+>", "", outer_html(cdp, area_id))) if area_id else ""
                if city_text.strip() == city and district in area_text and "(全)" in area_text:
                    return
            check_gate(rendered_text(snapshot(cdp)))
        except AccessGate:
            raise
        except RuntimeError:
            pass
        time.sleep(0.4)
    raise TimeoutError(f"未确认搜索范围为 {city}·{district}，未读取可能属于其他区的岗位。")


def collect_districts(cdp, payload, folder, path, names, no_prompt, max_scrolls, excluded_keys,
                      progress=None, after_district=None):
    shared_state = {}
    payload["district_runs"] = []
    payload["list_scroll_count"] = 0
    for number, district in enumerate(names, start=1):
        if len(payload["jobs"]) >= payload["requested_count"]:
            return "requested_limit_reached"
        url = search_url(payload["city"], payload["keyword"], district)
        summary = {"district": district, "code": DISTRICTS[payload["city"]][district],
                   "url": url, "status": "running"}
        payload["district_runs"].append(summary)
        if progress is not None:
            progress.save(district)
        before, scrolls_before = len(payload["jobs"]), payload["list_scroll_count"]
        print(f"分区 {number}/{len(names)}：{payload['city']}·{district}，"
              f"本轮合计 {before}/{payload['requested_count']} 条。", flush=True)
        try:
            navigate(cdp, url)
            require_login(cdp, url, no_prompt)
            wait_search_scope(cdp, payload["city"], payload["keyword"], district)
            summary["stop_reason"] = collect_list(
                cdp, payload, folder, path, url, no_prompt, max_scrolls, excluded_keys,
                district=district, shared_state=shared_state
            )
            summary["status"] = "finished"
            if after_district is not None:
                after_district()
        except AccessGate as exc:
            summary.update(status="blocked", error=str(exc))
            raise
        except TimeoutError as exc:
            summary.update(status="error", error=str(exc), stop_reason="district_not_ready")
            print(f"  {district} 页面未就绪，记录错误并尝试下一个区：{exc}", flush=True)
        except BaseException as exc:
            summary.update(status="interrupted", error=str(exc) or type(exc).__name__)
            raise
        finally:
            summary.update(collected_count=len(payload["jobs"]) - before,
                           scroll_count=payload["list_scroll_count"] - scrolls_before)
            update_report(payload)
            save_jobs(path, payload)
        if payload.get("finish_district") and summary.get("stop_reason") == "max_scrolls_reached":
            print(f"  {district} 已达到滚动安全上限，本轮先停在此区，下次仍从这里开始。", flush=True)
            return "district_scroll_budget_reached"
        if number < len(names) and len(payload["jobs"]) < payload["requested_count"]:
            time.sleep(2)
    if len(payload["jobs"]) >= payload["requested_count"]:
        return "requested_limit_reached"
    return "districts_finished_with_errors" if any(r["status"] == "error" for r in payload["district_runs"]) else "districts_exhausted"


def collect_details(cdp, payload, folder, path, no_prompt=False, max_pending=2):
    """单页面串行采集，单本地 OCR 工作器；只有主线程更新岗位和总文件。"""
    if max_pending < 1:
        raise ValueError("OCR 待处理上限必须至少为 1。")
    started = time.perf_counter()
    pending = []
    jobs = [job for job in payload["jobs"] if job["description_status"] == "pending"]

    def persist():
        update_report(payload)
        save_jobs(path, payload)

    def accept(job, text, metadata):
        if needs_ocr(text):
            raise ValueError("正文为空或仍含混淆字符。")
        (folder / f"{job['id']}-description.txt").write_text(text, encoding="utf-8")
        job.update(metadata)
        job.update(description=text, description_status="ok", description_characters=len(text))
        job.pop("description_error", None)
        print(f"  已保存 {job['title']} 的职位描述，{len(text)} 字符。", flush=True)

    def finish(item):
        job, future = item
        try:
            text, metadata = future.result()
            accept(job, text, metadata)
        except Exception as exc:
            # 工作器异常只归属该岗位；不能让一个模型/图片错误丢弃整批结果。
            job.update(description_status="error", description_error=f"本地 OCR 失败：{exc}")
            print(f"  {job['title']} 本地 OCR 失败：{exc}", flush=True)
        persist()

    worker = DescriptionOCRWorker()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="jd-ocr") as executor:
        try:
            for index, job in enumerate(jobs, start=1):
                for item in pending[:]:
                    if item[1].done():
                        finish(item)
                        pending.remove(item)
                # 提交前限流：连同正在执行的任务，最多保留两份待处理 JD。
                while len(pending) >= max_pending:
                    finish(pending[0])
                    pending.pop(0)
                print(f"详情 {index}/{len(jobs)}：{job['title']}", flush=True)
                page_started = time.perf_counter()
                try:
                    result = read_description_with_login(cdp, job, folder, no_prompt, capture_only=True)
                    if isinstance(result, CapturedDescription):
                        if result.job_id != job["id"]:
                            raise ValueError("截图岗位标识与当前任务不一致。")
                        job.update(description_status="ocr_pending", description_source="OCR",
                                   description_capture_manifest=result.manifest_path.name)
                        persist()
                        pending.append((job, executor.submit(worker, result)))
                        print("  本岗位截图已全部保存，后台识别中……", flush=True)
                    else:
                        accept(job, result, {"description_source": "CDP_DOMSnapshot"})
                except AccessGate as exc:
                    job.update(description_status="blocked", description_error=str(exc))
                    raise
                except (RuntimeError, ValueError, OSError, websocket.WebSocketException) as exc:
                    job.update(description_status="error", description_error=str(exc))
                    print("  本岗位详情未完成：", exc, flush=True)
                finally:
                    job["description_page_seconds"] = time.perf_counter() - page_started
                    persist()
                if index < len(jobs):
                    # 保持原有页面访问间隔；工作器可以利用这段时间识别。
                    time.sleep(2)
        finally:
            # 登录/访问受阻或用户中断时，也回收已经完整截图的本地任务。
            interrupted = False
            for item in pending:
                try:
                    finish(item)
                except KeyboardInterrupt:
                    if interrupted:
                        raise
                    interrupted = True
                    print("正在保存已截图岗位的 OCR 结果，请等待本地识别完成……", flush=True)
                    finish(item)
            payload["detail_pipeline_seconds"] = payload.get("detail_pipeline_seconds", 0) + time.perf_counter() - started
            persist()
            if interrupted:
                raise KeyboardInterrupt()


def read_detail_salary(cdp, job, folder, engine_state, timeout=15):
    """从已有详情 URL 补薪资；只截取薪资节点，并确认登录及目标 URL。"""
    navigate(cdp, job["url"])
    deadline = time.monotonic() + timeout
    last_reason = "尚未出现可见薪资"
    while time.monotonic() < deadline:
        try:
            root = document(cdp)
            data = snapshot(cdp)
            page_text = rendered_text(data)
            check_gate(page_text)
            if urlsplit(root.get("documentURL", "")).path == urlsplit(job["url"]).path:
                if authentication_state(cdp, root, page_text) != "authenticated":
                    raise LoginRequired("历史岗位详情未确认登录，不能读取游客预览薪资。")
                node = query(cdp, root["nodeId"], ".job-banner .salary, .info-primary .salary")
                if node:
                    backend = cdp.call("DOM.describeNode", {"nodeId": node})["node"]["backendNodeId"]
                    raw = rendered_text(data, backend).strip()
                    source, scores = "DOM", None
                    observed = raw
                    if needs_ocr(raw):
                        image = folder / f"{job['id']}-salary-retry.png"
                        salary_screenshot(cdp, node, image)
                        if engine_state[0] is None:
                            engine_state[0] = RapidOCR(params={"Global.log_level": "warning"})
                        result = engine_state[0](str(image), use_det=False, use_cls=False, use_rec=True)
                        if not result.txts:
                            raise ValueError("历史岗位薪资 OCR 为空，截图已保存。")
                        observed, source = " ".join(result.txts), "OCR"
                        scores = [float(score) for score in result.scores]
                    fields = normalize_salary(observed, raw)
                    if fields["salary_status"] not in ("ok", "negotiable"):
                        raise ValueError("历史岗位薪资仍缺少可确认的单位，未覆盖原值。")
                    if urlsplit(document(cdp).get("documentURL", "")).path != urlsplit(job["url"]).path:
                        raise ValueError("薪资识别期间页面已跳转，未保存结果。")
                    fields["salary_source"] = source
                    if scores is not None:
                        fields["salary_ocr_scores"] = scores
                    return fields
        except AccessGate:
            raise
        except RuntimeError as exc:
            # 页面刚加载时节点会重建；重新定位，不能把一次失效当作永久失败。
            last_reason = str(exc)
        time.sleep(0.4)
    raise TimeoutError(f"历史岗位 URL 未就绪或没有可见薪资，可能已下架或发生跳转：{last_reason}")


def repair_missing_history(cdp, groups, payload, folder, path, database, no_prompt=False):
    """修复单独记账，不加入本轮新增 jobs；每个缺失字段每轮重试一次。"""
    report = {"requested": sum(len(jobs) for jobs in groups.values()), "attempted": 0,
              "repaired": 0, "failed": 0, "skipped": 0, "files": [], "database_errors": []}
    payload["history_repair"] = report
    engine_state = [None]
    for keyword, jobs in groups.items():
        group_folder = folder / "repairs" / hashlib.sha256(keyword.encode("utf-8")).hexdigest()[:16]
        group_folder.mkdir(parents=True, exist_ok=True)
        group_path = group_folder / "jobs.json"
        report["files"].append(str(group_path.relative_to(folder)))
        batch = {"city": payload["city"], "keyword": keyword, "kind": "history_repair",
                 "requested_count": len(jobs), "jobs": jobs, "status": "running"}
        try:
            for job in jobs:
                if not valid_job_url(job.get("url")):
                    job["repair_error"] = "缺少有效的 BOSS 详情 URL，未自动访问。"
                    report["skipped"] += 1
                    continue
                report["attempted"] += 1
                print(f"历史修复 {report['attempted']}/{report['requested']}：{job.get('title')}", flush=True)
                page_ready = False
                try:
                    if "salary" in missing_fields(job):
                        try:
                            try:
                                fields = read_detail_salary(cdp, job, group_folder, engine_state)
                            except LoginRequired:
                                require_login(cdp, job["url"], no_prompt)
                                fields = read_detail_salary(cdp, job, group_folder, engine_state)
                            job.update(fields)
                            job.pop("salary_retry_error", None)
                            page_ready = True
                        except AccessGate:
                            raise
                        except (RuntimeError, ValueError, OSError, websocket.WebSocketException) as exc:
                            job["salary_retry_error"] = str(exc)
                    if "description" in missing_fields(job):
                        try:
                            text = read_description_with_login(cdp, job, group_folder, no_prompt,
                                                               navigate_page=not page_ready)
                            job.update(description=text, description_status="ok", description_characters=len(text))
                            job.pop("description_error", None)
                        except AccessGate:
                            raise
                        except (RuntimeError, ValueError, OSError, websocket.WebSocketException) as exc:
                            job.update(description_status="error", description_error=str(exc))
                    if not missing_fields(job):
                        report["repaired"] += 1
                    else:
                        report["failed"] += 1
                finally:
                    update_report(batch)
                    save_jobs(group_path, batch)
                    save_jobs(path, payload)
                time.sleep(2)
            batch["status"] = "complete" if all(not missing_fields(j) for j in jobs) else "partial"
        except BaseException:
            batch["status"] = "stopped"
            raise
        finally:
            update_report(batch)
            import_collected_jobs(group_path, batch, database)
            report["database_status"] = batch["database"]["status"]
            if batch["database"]["status"] != "ok":
                report["database_errors"].append({"keyword": keyword, "error": batch["database"]["error"]})
            save_jobs(path, payload)
    print(f"历史修复：成功 {report['repaired']}，仍缺失 {report['failed']}，无有效 URL {report['skipped']}。", flush=True)


def import_collected_jobs(path, payload, database):
    """入库失败保留 JSON，允许下一次回调或结束时重试。"""
    save_jobs(path, payload)
    try:
        report = turn_db(path, database)
        payload["database"] = {"path": str(database), "status": "ok",
                               "processed": report["processed"], "total": report["total"]}
        print(f"数据库已更新：{database}，本轮 {report['processed']} 条，库内共 {report['total']} 条。", flush=True)
    except (OSError, ValueError, sqlite3.Error) as exc:
        payload["database"] = {"path": str(database), "status": "error", "error": str(exc)}
        print(f"自动入库失败，JSON 已保留，可稍后重试：{exc}", flush=True)
    save_jobs(path, payload)


def collection_complete(payload):
    target_met = payload["fully_extracted_count"] >= payload["requested_count"]
    exhausted = payload.get("list_stop_reason") == "districts_exhausted"
    bounded = payload.get("list_stop_reason") == "district_scroll_budget_reached"
    district_errors = any(row.get("status") != "finished" for row in payload.get("district_runs", []))
    return not bounded and not district_errors and (target_met or exhausted) and payload["fully_extracted_count"] == len(payload["jobs"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="Python")
    parser.add_argument("--city", choices=CITY_CODES, default="广州")
    parser.add_argument("--limit", type=int, default=200, help="默认目标 200 条；分区模式完成当前区后再判断是否停止")
    parser.add_argument("--hard-limit", action="store_true", help="严格达到 limit 即停止，允许中途截断当前区")
    parser.add_argument("--empty-rounds", type=int, default=3, help="连续多少轮未看到不同岗位时结束该区，默认 3")
    parser.add_argument("--restart-districts", action="store_true", help="忽略续采位置，从指定分区范围的第一个区开始")
    parser.add_argument("--db", type=Path, help="自动入库的 SQLite 文件，默认 output/jobs.db")
    parser.add_argument("--skip-repair", action="store_true", help="本轮跳过数据库中缺失薪资/正文的 URL 重试")
    parser.add_argument("--max-scrolls", type=int, help="最多滚动加载次数；默认按当前城市历史岗位数自动增加")
    parser.add_argument("--no-prompt", action="store_true", help="未确认登录时直接退出，不采集游客数据")
    parser.add_argument("--include-seen", action="store_true", help="允许再次采集历史岗位，用于复测")
    parser.add_argument("--close-tab", action="store_true", help="结束时关闭程序创建的任务标签页；默认保留现场")
    regions = parser.add_mutually_exclusive_group()
    regions.add_argument("--district", nargs="+", help="仅采集指定区，例如 --district 荔湾区 天河区；默认依次采集全市各区")
    regions.add_argument("--citywide", action="store_true", help="使用原来的全市单列表模式")
    args = parser.parse_args()
    if args.max_scrolls is not None and args.max_scrolls < 0:
        parser.error("--max-scrolls 不能小于 0")
    if args.limit < 1 or args.empty_rounds < 1:
        parser.error("--limit 和 --empty-rounds 必须大于 0")
    try:
        districts = [] if args.citywide else selected_districts(args.city, args.district)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        check_ocr_runtime()
    except RuntimeError as exc:
        print(exc, flush=True)
        return 1
    try:
        excluded_keys, history_files = (set(), 0) if args.include_seen else load_history(OUTPUT / "runs", args.city)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"{'允许重复采集' if args.include_seen else '跨批次去重'}："
          f"读取 {args.city} 的 {history_files} 份历史结果，按名称与公司排除 {len(excluded_keys)} 个岗位。", flush=True)
    progress = None
    if districts:
        try:
            progress = DistrictProgress(OUTPUT / "district-progress.json", args.city, args.keyword, districts)
            districts = progress.order(districts, restart=args.restart_districts)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(f"读取分区进度失败：{exc}")
        print(f"从 {districts[0]} 开始；{'严格按条数停止' if args.hard_limit else '先完成当前区，再判断是否达到本轮目标'}。", flush=True)
    database = (args.db if args.db is not None else OUTPUT / "jobs.db").resolve()
    try:
        missing_jobs = {} if args.skip_repair else load_missing_jobs(database, args.city)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(f"读取历史修复队列失败：{exc}；可用 --skip-repair 暂时跳过")
    # 即便旧 JSON 被移动，仅存在数据库的修复对象也不能在本轮重复计入新增。
    excluded_keys.update(job_identity(job) for jobs in missing_jobs.values() for job in jobs)
    print(f"待按 URL 修复 {sum(len(jobs) for jobs in missing_jobs.values())} 个历史岗位，不占新增名额。", flush=True)
    max_scrolls = list_scroll_budget(len(excluded_keys), args.max_scrolls)
    if districts and args.max_scrolls is None:
        max_scrolls = max(60, max_scrolls)
    print(f"{'每区' if districts else '列表'}滚动上限：{max_scrolls} 次（{'手动指定' if args.max_scrolls is not None else '按历史数量自动估算'}）；"
          f"连续 {args.empty_rounds} 轮未看到不同岗位时结束当前列表。", flush=True)
    ensure_browser()
    folder = OUTPUT / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    path = folder / "jobs.json"
    target = open_task_tab()
    connection = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
    payload = {"keyword": args.keyword, "city": args.city, "status": "running",
               "requested_count": args.limit, "jobs": [],
               "deduplication": "within_run" if args.include_seen else "history",
               "deduplication_fields": ["title", "company"],
               "deduplication_city": args.city,
               "history_file_count": history_files, "historical_job_count": len(excluded_keys),
               "historical_skipped_count": 0, "list_scroll_limit": max_scrolls,
               "list_scroll_budget_mode": "manual" if args.max_scrolls is not None else "automatic",
               "search_mode": "districts" if districts else "citywide", "districts": districts,
               "finish_district": bool(districts) and not args.hard_limit, "empty_rounds": args.empty_rounds}
    try:
        cdp = RawCDP(connection)
        cdp.call("Page.enable")
        if missing_jobs:
            repair_missing_history(cdp, missing_jobs, payload, folder, path, database, args.no_prompt)
        def after_district():
            collect_details(cdp, payload, folder, path, args.no_prompt)
            import_collected_jobs(path, payload, database)

        if districts:
            payload["list_stop_reason"] = collect_districts(
                cdp, payload, folder, path, districts, args.no_prompt, max_scrolls, excluded_keys,
                progress=progress, after_district=after_district
            )
        else:
            list_url = search_url(args.city, args.keyword)
            print("自动打开搜索页……", flush=True)
            navigate(cdp, list_url)
            require_login(cdp, list_url, args.no_prompt)
            payload["list_stop_reason"] = collect_list(
                cdp, payload, folder, path, list_url, args.no_prompt, max_scrolls, excluded_keys
            )
        print(f"列表取得 {len(payload['jobs'])}/{args.limit} 条，停止原因：{payload['list_stop_reason']}", flush=True)
        if payload["list_stop_reason"] == "max_scrolls_reached":
            print(f"滚动次数已用完；本轮跳过 {payload['historical_skipped_count']} 个历史岗位。"
                  f"可用 --max-scrolls {max_scrolls + 12} 增加下一轮预算，仍会保留历史去重。", flush=True)
        save_jobs(path, payload)
        collect_details(cdp, payload, folder, path, args.no_prompt)
        update_report(payload)
        payload["status"] = "complete" if collection_complete(payload) else "partial"
    except (KeyboardInterrupt, ImportError, RuntimeError, ValueError, OSError, websocket.WebSocketException) as exc:
        payload.update(status="stopped", error=str(exc) or "用户中断采集。")
        print("本轮停止：", exc, flush=True)
    finally:
        for job in payload["jobs"]:
            if job["description_status"] in ("pending", "ocr_pending"):
                job.update(description_status="not_processed",
                           description_error=payload.get("error", "本轮尚未处理到此岗位。"))
        update_report(payload)
        save_jobs(path, payload)
        import_collected_jobs(path, payload, database)
        connection.close()
        if args.close_tab:
            try:
                print("本轮结束，关闭程序创建的任务标签页。", flush=True)
                close_task_tab(target["id"])
            except (OSError, RuntimeError, websocket.WebSocketException):
                pass
        else:
            print("本轮结束，保留任务标签页，方便检查当前页面。", flush=True)
    print("结果已保存：", path)
    csv_ok = True
    try:
        print("表格已保存：", export_csv(path))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        csv_ok = False
        print(f"表格导出失败，JSON 已保留：{exc}", flush=True)
    print(f"请求 {args.limit} 条，列表 {payload['collected_count']} 条，"
          f"薪资成功 {payload['salary_success_count']} 条，详情成功 {payload['description_success_count']} 条。")
    return 0 if payload["status"] == "complete" and csv_ok and payload["database"]["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
