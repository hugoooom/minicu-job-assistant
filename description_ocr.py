"""对已经定位的职位正文进行分段截图和多行 OCR，不读取整页导航及推荐内容。"""

import base64
import json
from difflib import SequenceMatcher

from rapidocr import RapidOCR


_engine = None


def visible_bounds(cdp, backend_id, viewport):
    """将视口与正文的可滚动/裁剪祖先相交，避免截取容器外的其他内容。"""
    data = cdp.call("DOMSnapshot.captureSnapshot", {"computedStyles": ["overflow-x", "overflow-y"]})
    left, top, right, bottom = 0, 0, viewport["clientWidth"], viewport["clientHeight"]
    for doc in data["documents"]:
        nodes, layout = doc["nodes"], doc["layout"]
        if backend_id not in nodes["backendNodeId"]:
            continue
        index = nodes["backendNodeId"].index(backend_id)
        mapping = {node: i for i, node in enumerate(layout["nodeIndex"])}
        index = nodes["parentIndex"][index]
        while index >= 0:
            if index in mapping:
                position = mapping[index]
                styles = [data["strings"][item] for item in layout["styles"][position]]
                x, y, width, height = layout["bounds"][position]
                x, y = x - viewport["pageX"], y - viewport["pageY"]
                if styles and styles[0] in ("auto", "scroll", "hidden", "clip"):
                    left, right = max(left, x), min(right, x + width)
                if len(styles) > 1 and styles[1] in ("auto", "scroll", "hidden", "clip"):
                    top, bottom = max(top, y), min(bottom, y + height)
            index = nodes["parentIndex"][index]
        return left, top, right, bottom
    raise ValueError("正文节点已变化，无法确认截图范围。")


def merge_lines(lines):
    """只按物理位置合并分段重叠区，保留正文中不同位置的重复句子。"""
    merged = []
    for line in sorted(lines, key=lambda item: (item["y"], item["x"])):
        duplicate = None
        for index in range(len(merged) - 1, -1, -1):
            other = merged[index]
            if line["y"] - other["y"] > max(12, line["height"]):
                break
            overlap = max(0, min(line["x"] + line["width"], other["x"] + other["width"])
                          - max(line["x"], other["x"]))
            if (abs(line["y"] - other["y"]) <= max(5, min(line["height"], other["height"]) * 0.6)
                    and overlap >= min(line["width"], other["width"]) * 0.5
                    and SequenceMatcher(None, line["text"], other["text"]).ratio() >= 0.6):
                duplicate = index
                break
        if duplicate is None:
            merged.append(line)
        elif (len(line["text"]), line["score"]) > (len(merged[duplicate]["text"]), merged[duplicate]["score"]):
            merged[duplicate] = line
    return sorted(merged, key=lambda item: (item["y"], item["x"]))


def extract_description(cdp, node_id, folder, job_id, geometry_reader, guard=None, resolve_node=None):
    global _engine
    if _engine is None:
        _engine = RapidOCR(params={"Global.log_level": "warning"})
    if guard:
        guard()
    if resolve_node:
        node_id = resolve_node()
    backend = cdp.call("DOM.describeNode", {"nodeId": node_id})["node"]["backendNodeId"]
    initial = geometry_reader(cdp, node_id)["viewport_rect"]
    total_height, offset, all_lines, images, captures = initial["height"], 0, [], [], []
    for number in range(1, 31):
        if number > 1:
            if guard:
                guard()
            if resolve_node:
                node_id = resolve_node()
                backend = cdp.call("DOM.describeNode", {"nodeId": node_id})["node"]["backendNodeId"]
        viewport = cdp.call("Page.getLayoutMetrics")["cssLayoutViewport"]
        tile_height = min(480, max(120, viewport["clientHeight"] - 160), total_height - offset)
        cdp.call("DOM.scrollIntoViewIfNeeded", {
            "nodeId": node_id,
            "rect": {"x": 0, "y": offset, "width": initial["width"], "height": tile_height},
        })
        geometry = geometry_reader(cdp, node_id)
        element, scroll = geometry["viewport_rect"], geometry["page_scroll"]
        if abs(element["height"] - total_height) > 2:
            raise ValueError("正文布局在截图期间变化，未保存可能缺行的 OCR 结果。")
        viewport = cdp.call("Page.getLayoutMetrics")["cssLayoutViewport"]
        bounds = visible_bounds(cdp, backend, viewport)
        left, top = max(bounds[0], element["x"]), max(bounds[1], element["y"] + offset)
        right = min(bounds[2], element["x"] + element["width"])
        bottom = min(bounds[3], element["y"] + offset + tile_height)
        relative_top, relative_bottom = top - element["y"], bottom - element["y"]
        if right - left < 40 or bottom - top < 30 or relative_top > offset + 3:
            raise ValueError("正文分段未完整进入可见区域，未截取可能错位的内容。")
        clip = {"x": left + scroll["x"], "y": top + scroll["y"],
                "width": right - left, "height": bottom - top, "scale": 2}
        image = cdp.call("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": True, "clip": clip,
        })
        path = folder / f"{job_id}-description-{number:02}.png"
        path.write_bytes(base64.b64decode(image["data"]))
        images.append(path.name)
        captures.append({"image": path.name, "clip": clip, "relative_top": relative_top})
        # 正文是多行文字，必须启用检测，不能复用薪资的单行模式。
        result = _engine(str(path), use_det=True, use_cls=True, use_rec=True)
        if not result.txts or result.boxes is None:
            raise ValueError(f"正文第 {number} 段 OCR 未识别到文字；截图已保留。")
        for text, score, box in zip(result.txts, result.scores, result.boxes):
            if not text.strip():
                continue
            xs, ys = [float(point[0]) for point in box], [float(point[1]) for point in box]
            all_lines.append({"text": text.strip(), "score": float(score),
                              "x": left - element["x"] + min(xs) / 2,
                              "y": relative_top + (min(ys) + max(ys)) / 4,
                              "width": (max(xs) - min(xs)) / 2,
                              "height": (max(ys) - min(ys)) / 2})
        if relative_bottom >= total_height - 1:
            break
        next_offset = relative_bottom - 48
        if next_offset <= offset + 1:
            raise ValueError("正文滚动没有推进，未保存不完整的 OCR 结果。")
        offset = next_offset
    else:
        raise ValueError("正文超过 30 个截图分段，本岗位未标为成功。")

    lines = merge_lines(all_lines)
    text = "\n".join(line["text"] for line in lines)
    if len(text) < 40:
        raise ValueError("正文 OCR 内容过短，未标为成功。")
    metadata = {"captures": captures, "lines": lines}
    (folder / f"{job_id}-description-ocr.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    scores = [line["score"] for line in lines]
    return text, {"description_source": "OCR", "description_ocr_images": images,
                  "description_ocr_review_required": True,
                  "description_ocr_mean_score": sum(scores) / len(scores),
                  "description_ocr_min_score": min(scores)}
