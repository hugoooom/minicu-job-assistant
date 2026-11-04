"""本地补采预算与字段重试时间；不通过重复访问验证网页状态。"""

import sqlite3
import time
from contextlib import closing

from history_repairs import missing_fields, valid_job_url
from job_history import normalized_name
from job_lifecycle import platform_id
from locations import LOCATION_COLUMNS, has_detail_location


def required_fields(job):
    fields = missing_fields(job)
    if job.get("location_refresh_required") and not has_detail_location(job):
        fields.add("location")
    return fields


def reuse_fields(source, target):
    """只补目标缺失字段，保留其原有成功字段、关键词和身份。"""
    for field in required_fields(target):
        if field == "location":
            if not has_detail_location(source):
                continue
            keys = ("location", "location_status", *LOCATION_COLUMNS)
        else:
            if field in missing_fields(source):
                continue
            keys = [key for key in source if key == field or key.startswith(field + "_")]
        target.update({key: source[key] for key in keys if key in source})


class RepairPolicy:
    def __init__(self, database, city, clock=time.time):
        self.database, self.city, self.clock = database, normalized_name(city), clock
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS repair_attempts (
                city_key TEXT NOT NULL, platform_job_id TEXT NOT NULL, field TEXT NOT NULL,
                next_retry_at REAL NOT NULL, outcome TEXT NOT NULL,
                PRIMARY KEY(city_key, platform_job_id, field))""")

    def record(self, job, field, outcome="attempted"):
        target = platform_id(job.get("url"))
        if not target:
            return
        # 成功数据由 jobs 表保存；中断也保留尝试记录，避免重启立即再请求。
        delay = 7 * 86400 if outcome == "not_provided" else 86400
        with closing(sqlite3.connect(self.database)) as connection, connection:
            if outcome == "ok":
                connection.execute("DELETE FROM repair_attempts WHERE city_key=? AND platform_job_id=? AND field=?",
                                   (self.city, target, field))
            else:
                connection.execute("INSERT OR REPLACE INTO repair_attempts VALUES (?,?,?,?,?)",
                                   (self.city, target, field, self.clock() + delay, outcome))

    def select(self, groups, limit=30):
        with closing(sqlite3.connect(self.database)) as connection:
            cooldowns = {(target, field): retry for target, field, retry in connection.execute(
                "SELECT platform_job_id,field,next_retry_at FROM repair_attempts WHERE city_key=?", (self.city,))}
        selected, targets = {}, set()
        report = {"cooldown_deferred": 0, "budget_deferred": 0, "unique_urls": 0}
        now = self.clock()
        for keyword, jobs in groups.items():
            for job in jobs:
                target = platform_id(job.get("url"))
                fields = required_fields(job)
                due = {field for field in fields if cooldowns.get((target, field), 0) <= now}
                if fields and not due and valid_job_url(job.get("url")):
                    report["cooldown_deferred"] += 1
                    continue
                if target and target not in targets:
                    if len(targets) >= limit:
                        report["budget_deferred"] += 1
                        continue
                    targets.add(target)
                job["_repair_fields"] = sorted(due)
                selected.setdefault(keyword, []).append(job)
        report["unique_urls"] = len(targets)
        return selected, report
