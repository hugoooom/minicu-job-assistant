import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from job_db import initialize_database, turn_db


class SQLiteImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "jobs.db"

    def batch(self, name, jobs, city="广州", keyword="Python"):
        path = self.root / name / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"city": city, "keyword": keyword, "jobs": jobs},
                                   ensure_ascii=False), encoding="utf-8")
        return path

    def job(self, **changes):
        job = {"id": "example", "title": "Python 实习生", "company": "测试公司",
               "salary": "150元/天", "salary_status": "ok", "salary_source": "DOM",
               "description": "职责：开发接口\n要求：Python、SQL", "description_status": "ok",
               "description_source": "DOM"}
        job.update(changes)
        return job

    def rows(self):
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute("SELECT * FROM jobs ORDER BY id")]
        finally:
            connection.close()

    def test_multiple_batches_and_reimport(self):
        first = self.batch("first", [self.job()])
        second = self.batch("second", [self.job(id="changed-id", salary="180元/天"),
                                       self.job(id="other", company="另一公司")])
        self.assertEqual(turn_db(first, self.db)["inserted"], 1)
        self.assertEqual(turn_db(second, self.db)["inserted"], 1)
        self.assertEqual(turn_db(second, self.db)["inserted"], 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[0]["salary"], "180元/天")

    def test_city_and_keyword_are_independent_groups(self):
        paths = [self.batch("a", [self.job()]),
                 self.batch("b", [self.job()], city="深圳"),
                 self.batch("c", [self.job()], keyword="AI")]
        report = turn_db(paths, self.db)
        self.assertEqual(report["total"], 3)
        self.assertEqual(len(report["groups"]), 3)

    def test_normalized_names_and_keyword_merge(self):
        first = self.batch("a", [self.job()])
        second = self.batch("b", [self.job(title="Ｐｙｔｈｏｎ  实习生", company=" 测试公司 ")],
                            city=" 广州 ", keyword="ＰＹＴＨＯＮ")
        turn_db([first, second], self.db)
        self.assertEqual(len(self.rows()), 1)

    def test_failed_import_cannot_erase_good_fields(self):
        first = self.batch("a", [self.job()])
        failed = self.batch("b", [self.job(salary=None, salary_status="error",
                                           description=None, description_status="error")])
        turn_db(first, self.db)
        turn_db(failed, self.db)
        row = self.rows()[0]
        self.assertEqual(row["salary"], "150元/天")
        self.assertEqual(row["salary_status"], "ok")
        self.assertIn("开发接口", row["description"])
        self.assertEqual(row["description_status"], "ok")

    def test_new_failed_record_keeps_failure_status(self):
        path = self.batch("a", [self.job(salary=None, salary_status="error",
                                         description=None, description_status="blocked")])
        turn_db(path, self.db)
        row = self.rows()[0]
        self.assertIsNone(row["description"])
        self.assertEqual(row["description_status"], "blocked")

    def test_ocr_review_flag_survives_failed_update(self):
        first = self.batch("a", [self.job(description_source="OCR", description_ocr_review_required=True)])
        second = self.batch("b", [self.job(description=None, description_status="error")])
        turn_db([first, second], self.db)
        self.assertEqual(self.rows()[0]["description_ocr_review_required"], 1)
        self.assertEqual(self.rows()[0]["description_source"], "OCR")

    def test_missing_names_fall_back_to_platform_id(self):
        path = self.batch("a", [self.job(id="one", title=None, company=None),
                                self.job(id="two", title=None, company=None)])
        self.assertEqual(turn_db(path, self.db)["total"], 2)

    def test_directory_import_and_empty_batch(self):
        self.batch("a", [self.job()])
        self.batch("b", [])
        report = turn_db(self.root, self.db)
        self.assertEqual(report["files"], 2)
        self.assertEqual(report["total"], 1)

    def test_bad_batch_does_not_partially_import(self):
        first = self.batch("a", [self.job()])
        turn_db(first, self.db)
        bad = self.batch("bad", [self.job(id="two", company="新公司"), {}])
        with self.assertRaises(ValueError):
            turn_db(bad, self.db)
        self.assertEqual(len(self.rows()), 1)

    def test_input_cannot_be_database(self):
        path = self.batch("a", [self.job()])
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            turn_db(path, path)
        self.assertEqual(path.read_bytes(), original)

    def test_incompatible_existing_schema_is_preserved(self):
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO jobs(title) VALUES ('keep')")
        connection.commit()
        connection.close()
        with self.assertRaises(ValueError):
            initialize_database(self.db)
        self.assertEqual(self.rows()[0]["title"], "keep")

    def test_cli_import_and_init(self):
        script = Path(__file__).resolve().parents[1] / "job_db.py"
        first = subprocess.run([sys.executable, str(script), "--init", "--db", str(self.db)],
                               capture_output=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        path = self.batch("a", [self.job()])
        result = subprocess.run([sys.executable, str(script), "--input", str(path), "--db", str(self.db)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.rows()), 1)


if __name__ == "__main__":
    unittest.main()
