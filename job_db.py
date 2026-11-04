"""将一份或多份岗位 JSON 合并到本地 SQLite，无需启动数据库服务。"""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from job_history import job_identity, normalized_name
from job_lifecycle import ensure_deleted_schema, exclude_deleted_rows


DEFAULT_DB = Path(__file__).resolve().parent / "output" / "jobs.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    city TEXT NOT NULL,
    keyword TEXT NOT NULL COLLATE NOCASE,
    city_key TEXT NOT NULL,
    keyword_key TEXT NOT NULL,
    job_key TEXT NOT NULL,
    platform_job_id TEXT,
    title TEXT,
    company TEXT,
    location TEXT,
    salary TEXT,
    salary_status TEXT,
    salary_source TEXT,
    description TEXT,
    description_status TEXT,
    description_source TEXT,
    description_ocr_review_required INTEGER,
    url TEXT,
    source_file TEXT NOT NULL,
    first_imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (city_key, keyword_key, job_key)
);
"""

UPSERT = """
INSERT INTO jobs (
    city, keyword, city_key, keyword_key, job_key, platform_job_id,
    title, company, location, salary, salary_status, salary_source,
    description, description_status, description_source,
    description_ocr_review_required, url, source_file
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (city_key, keyword_key, job_key) DO UPDATE SET
    platform_job_id = COALESCE(excluded.platform_job_id, jobs.platform_job_id),
    title = COALESCE(excluded.title, jobs.title),
    company = COALESCE(excluded.company, jobs.company),
    location = COALESCE(excluded.location, jobs.location),
    salary = COALESCE(excluded.salary, jobs.salary),
    salary_status = CASE WHEN excluded.salary IS NOT NULL OR jobs.salary IS NULL
        THEN excluded.salary_status ELSE jobs.salary_status END,
    salary_source = CASE WHEN excluded.salary IS NOT NULL OR jobs.salary IS NULL
        THEN excluded.salary_source ELSE jobs.salary_source END,
    description = COALESCE(excluded.description, jobs.description),
    description_status = CASE WHEN excluded.description IS NOT NULL OR jobs.description IS NULL
        THEN excluded.description_status ELSE jobs.description_status END,
    description_source = CASE WHEN excluded.description IS NOT NULL OR jobs.description IS NULL
        THEN excluded.description_source ELSE jobs.description_source END,
    description_ocr_review_required = CASE WHEN excluded.description IS NOT NULL OR jobs.description IS NULL
        THEN excluded.description_ocr_review_required ELSE jobs.description_ocr_review_required END,
    url = COALESCE(excluded.url, jobs.url),
    source_file = excluded.source_file,
    last_imported_at = CURRENT_TIMESTAMP;
"""


def text_or_none(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def input_files(input_paths):
    if isinstance(input_paths, (str, Path)):
        input_paths = [input_paths]
    files = set()
    for value in input_paths:
        path = Path(value).resolve()
        if path.is_dir():
            matches = list(path.rglob("jobs.json"))
            if not matches:
                raise ValueError(f"目录中没有找到 jobs.json：{path}")
            files.update(matches)
        elif path.is_file():
            files.add(path)
        else:
            raise ValueError(f"输入路径不存在：{path}")
    if not files:
        raise ValueError("请至少提供一份岗位 JSON。")
    return sorted(files)


def load_rows(files):
    rows = []
    for path in files:
        try:
            with path.open(encoding="utf-8-sig") as stream:
                data = json.load(stream)
            if not isinstance(data, dict):
                raise ValueError("JSON 最外层必须是对象。")
            city, keyword = text_or_none(data.get("city")), text_or_none(data.get("keyword"))
            if not city or not keyword:
                raise ValueError("批次缺少有效的 city 或 keyword，无法确定分组。")
            if not isinstance(data.get("jobs"), list):
                raise ValueError("jobs 必须是列表。")
            for index, job in enumerate(data["jobs"], start=1):
                if not isinstance(job, dict):
                    raise ValueError(f"第 {index} 条岗位不是对象。")
                if job.get("repair_status") == "deleted":
                    continue
                identity = json.dumps(job_identity(job), ensure_ascii=False)
                salary = text_or_none(job.get("salary"))
                salary_status = text_or_none(job.get("salary_status")) or ("ok" if salary else "missing")
                if salary_status not in ("ok", "negotiable"):
                    salary = None
                description = text_or_none(job.get("description"))
                description_status = text_or_none(job.get("description_status")) or "missing"
                if description_status != "ok" or not description:
                    description = None
                    if description_status == "ok":
                        description_status = "missing"
                review = job.get("description_ocr_review_required")
                rows.append((
                    city, keyword, normalized_name(city), normalized_name(keyword), identity,
                    text_or_none(job.get("id")), text_or_none(job.get("title")),
                    text_or_none(job.get("company")), text_or_none(job.get("location")),
                    salary, salary_status, text_or_none(job.get("salary_source")),
                    description, description_status, text_or_none(job.get("description_source")),
                    int(review) if isinstance(review, bool) else None,
                    text_or_none(job.get("url")), str(path),
                ))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"无法导入 {path}：{exc}") from exc
    return rows


def ensure_schema(connection):
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()
    if exists:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        if not {"city_key", "keyword_key", "job_key", "source_file", "last_imported_at"} <= columns:
            raise ValueError("已有 jobs 表结构不兼容，请通过 --db 指定一个新的数据库文件。")
    connection.execute(SCHEMA)
    ensure_deleted_schema(connection)


def initialize_database(db_path=DEFAULT_DB):
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            ensure_schema(connection)
    return path


def turn_db(input_path, db_path=DEFAULT_DB):
    files = input_files(input_path)
    path = Path(db_path).resolve()
    if path in files:
        raise ValueError("数据库路径不能与输入 JSON 文件相同。")
    rows = load_rows(files)  # 所有文件验证通过后才开始入库。
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            ensure_schema(connection)
            filtered_rows = exclude_deleted_rows(connection, rows)
            before = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            connection.executemany(UPSERT, filtered_rows)
            after = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            groups = connection.execute(
                "SELECT city, keyword, COUNT(*) FROM jobs "
                "GROUP BY city_key, keyword_key ORDER BY city_key, keyword_key"
            ).fetchall()
    return {"database": str(path), "files": len(files), "processed": len(filtered_rows),
            "suppressed_deleted": len(rows) - len(filtered_rows),
            "inserted": after - before, "total": after, "groups": groups}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--input", nargs="+", help="一份或多份 JSON，也可传入包含批次的目录")
    modes.add_argument("--init", action="store_true", help="只创建数据库和表")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 文件，默认 output/jobs.db")
    args = parser.parse_args()
    try:
        if args.init:
            print("数据库已准备：", initialize_database(args.db))
            return 0
        report = turn_db(args.input, args.db)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"入库未完成：{exc}\n")
    print("数据库：", report["database"])
    print(f"读取 {report['files']} 个批次、{report['processed']} 条记录；"
          f"新增 {report['inserted']} 条，数据库共 {report['total']} 条。")
    if report["suppressed_deleted"]:
        print(f"已按删除记录跳过 {report['suppressed_deleted']} 条失效岗位，未重新入库。")
    for city, keyword, count in report["groups"]:
        print(f"  {city} / {keyword}：{count} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
