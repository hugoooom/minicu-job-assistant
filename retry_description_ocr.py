"""只用本地完整截图重试失败的 JD OCR，不启动浏览器、不需要重新登录。"""

import argparse
import json
from pathlib import Path

from collect_jobs import collection_complete, save_jobs, update_report
from description_ocr import CapturedDescription, DescriptionOCRWorker
from runtime_check import check_ocr_runtime
from turn_csv import export_csv


def retry_saved_descriptions(path, job_id=None):
    path = Path(path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = [job for job in payload["jobs"]
                  if job.get("description_capture_manifest")
                  and (job["id"] == job_id if job_id is not None
                       else job.get("description_status") in ("error", "ocr_pending", "not_processed"))]
    if not candidates:
        raise ValueError("没有符合条件且带完整截图清单的岗位；旧版截图不支持此重试入口。")
    worker = DescriptionOCRWorker()
    for job in candidates:
        try:
            manifest = path.parent / job["description_capture_manifest"]
            text, metadata = worker(CapturedDescription(job["id"], manifest))
            (path.parent / f"{job['id']}-description.txt").write_text(text, encoding="utf-8")
            job.update(metadata)
            job.update(description=text, description_status="ok", description_characters=len(text))
            job.pop("description_error", None)
        except Exception as exc:
            job.update(description_status="error", description_error=f"本地 OCR 重试失败：{exc}")
        update_report(payload)
        payload["status"] = "complete" if collection_complete(payload) else "partial"
        save_jobs(path, payload)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs_json", type=Path, help="本轮 jobs.json 的路径，原文件将更新")
    parser.add_argument("--job-id", help="只重试指定岗位；省略时处理有完整截图的失败/待识别岗位")
    args = parser.parse_args()
    try:
        check_ocr_runtime()
        payload = retry_saved_descriptions(args.jobs_json, args.job_id)
        print("JSON 已更新：", args.jobs_json)
        print("表格已更新：", export_csv(args.jobs_json))
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        print("重试或表格导出失败：", exc)
        return 1
    return 0 if payload["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
