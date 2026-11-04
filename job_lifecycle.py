"""失效岗位的明确页面信号和本地删除记录，防止旧批次导入后复活。"""

import json
import re
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit

from job_history import normalized_name


TERMINAL_MARKERS = ("职位已关闭", "职位已下架", "职位已下线", "职位已失效", "职位不存在",
    "职位已被删除", "职位已删除", "岗位已关闭", "岗位已下架", "岗位不存在", "岗位已删除",
    "停止招聘", "结束招聘", "招聘已结束", "已关闭", "页面不存在", "页面已被删除")


class JobUnavailable(ValueError):
    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__(evidence["matched_text"])


def platform_id(url):
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != "www.zhipin.com":
        return None
    match = re.fullmatch(r"/job_detail/([^/]+)\.html", parsed.path)
    return match.group(1) if match else None


def terminal_reason(expected_url, actual_url, status_text, error_text, has_body=False, has_cards=False):
    """不从 JD/推荐内容推断失效，不把跳到另一个岗位当成原岗位删除。"""
    expected_id, actual_id = platform_id(expected_url), platform_id(actual_url)
    if not expected_id or (actual_id is not None and actual_id != expected_id):
        return None
    if actual_id == expected_id:
        status = "".join(status_text.split())
        if any(word in status for word in ("招聘中", "未停止招聘", "未结束招聘", "未关闭", "未下架", "未删除", "未失效")):
            return None
        if len(status) <= 60:
            for marker in TERMINAL_MARKERS:
                if marker in status:
                    return {"kind": "closed", "matched_text": status}
    if has_body or has_cards:
        return None
    parsed = urlsplit(actual_url or "")
    if parsed.netloc != "www.zhipin.com" or not (actual_id == expected_id or parsed.path in (
            "/404.html", "/web/common/error.html", "/web/common/404.html")):
        return None
    for line in error_text.splitlines():
        line = line.strip()
        normalized = re.sub(r"\s+", "", line)
        if len(line) <= 120 and re.match(
                r"^(?:抱歉[，,！!]?)?(?:该|此|当前|您访问的|你访问的|您要访问的)?(?:职位|岗位|页面)"
                r"(?:已被删除|已删除|不存在|已关闭|已下架|已下线|已失效|已停止招聘|已结束招聘)", normalized):
            return {"kind": "unavailable", "matched_text": line}
    return None


def ensure_deleted_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS deleted_jobs (
        city_key TEXT NOT NULL, platform_job_id TEXT NOT NULL,
        reason TEXT NOT NULL, evidence_json TEXT NOT NULL, original_rows_json TEXT NOT NULL,
        deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(city_key, platform_job_id)
    )""")


def delete_unavailable_job(database, city, job, evidence):
    target = platform_id(job.get("url"))
    if target is None:
        raise ValueError("无法确认删除目标的岗位 URL。")
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            ensure_deleted_schema(connection)
            rows = connection.execute("SELECT * FROM jobs WHERE city_key=?", (normalized_name(city),)).fetchall()
            selected = [dict(row) for row in rows if platform_id(row["url"]) == target
                        or (not row["url"] and row["platform_job_id"] == target)]
            connection.execute("""INSERT INTO deleted_jobs
                (city_key,platform_job_id,reason,evidence_json,original_rows_json) VALUES (?,?,?,?,?)
                ON CONFLICT(city_key,platform_job_id) DO UPDATE SET
                reason=excluded.reason, evidence_json=excluded.evidence_json""",
                (normalized_name(city), target, evidence["matched_text"],
                 json.dumps(evidence, ensure_ascii=False), json.dumps(selected, ensure_ascii=False)))
            connection.executemany("DELETE FROM jobs WHERE id=?", [(row["id"],) for row in selected])
    return len(selected)


def exclude_deleted_rows(connection, rows):
    deleted = {(row[0], row[1]) for row in connection.execute("SELECT city_key,platform_job_id FROM deleted_jobs")}
    return [row for row in rows if (row[2], platform_id(row[16]) or row[5]) not in deleted]
