"""用岗位名称与公司名称标识岗位，并恢复跨批次历史。"""

import json
import unicodedata


def normalized_name(value):
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def job_identity(job):
    title, company = normalized_name(job.get("title")), normalized_name(job.get("company"))
    if title and company:
        return ("title_company", title, company)
    # 字段缺失不能合并成“同一空公司”；仅在这种情况下退回平台 ID。
    job_id = job.get("id")
    if isinstance(job_id, str) and job_id.strip():
        return ("id", job_id.strip())
    raise ValueError("岗位缺少名称/公司组合及有效 ID")


def load_history(runs_folder, city=None):
    """所有已有记录都视为采集过；复测由 --include-seen 显式开启。"""
    keys, files = set(), 0
    for path in sorted(runs_folder.glob("*/jobs.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("运行结果必须是对象")
            if city is not None and payload.get("city") != city:
                continue
            jobs = payload["jobs"]
            if not isinstance(jobs, list):
                raise ValueError("jobs 必须是列表")
            for job in jobs:
                if not isinstance(job, dict):
                    raise ValueError("岗位记录必须是对象")
                keys.add(job_identity(job))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"无法读取历史结果 {path}：{exc}。请修复该文件后重试；"
                             "如需允许重复采集，可使用 --include-seen。") from exc
        files += 1
    return keys, files
