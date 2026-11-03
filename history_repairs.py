"""从实际入库状态选择待补齐字段的岗位，保留原关键词与岗位标识。"""

import re
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit
from job_history import normalized_name


def missing_fields(job):
    return {name for name in ("salary", "description")
            if not isinstance(job.get(name), str) or not job[name].strip()}


def valid_job_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.scheme == "https" and parsed.netloc == "www.zhipin.com"
            and re.fullmatch(r"/job_detail/[^/]+\.html", parsed.path) is not None)


def load_missing_jobs(database, city):
    """按所选城市修复所有关键词组；库不存在时没有待修复记录。"""
    if not database.exists():
        return {}
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='jobs' AND type='table'").fetchone()
        if not exists:
            return {}
        rows = connection.execute(
            "SELECT * FROM jobs WHERE city_key = ? AND "
            "(salary IS NULL OR trim(salary)='' OR description IS NULL OR trim(description)='') "
            "ORDER BY id", (normalized_name(city),)
        ).fetchall()
    groups = {}
    for row in rows:
        # 不把内部 SQLite ID 当成平台 ID，也不改变跨关键词的归属。
        job = {key: row[key] for key in ("title", "company", "location", "salary", "salary_status",
            "salary_source", "description", "description_status", "description_source", "url")}
        job["id"] = row["platform_job_id"]
        if valid_job_url(job["url"]):
            job["id"] = urlsplit(job["url"]).path.rsplit("/", 1)[1][:-5]
        review = row["description_ocr_review_required"]
        if review is not None:
            job["description_ocr_review_required"] = bool(review)
        job["repair_original_source"] = row["source_file"]
        groups.setdefault(row["keyword"], []).append(job)
    return groups
