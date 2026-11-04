import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs as app
import job_db
from history_repairs import load_missing_jobs
from locations import LOCATION_COLUMNS, address_parts, coordinates, location_fields


def job():
    return {"id": "test", "title": "岗位", "company": "公司", "location": "广州",
            "salary": "150元/天", "salary_status": "ok", "description": "有效正文",
            "description_status": "ok", "url": "https://www.zhipin.com/job_detail/test.html"}


class LocationTests(unittest.TestCase):
    def seed(self, folder, current):
        path = folder / "source.json"
        app.save_jobs(path, {"city": "广州", "keyword": "Python", "jobs": [current]})
        job_db.turn_db(path, folder / "jobs.db")
        return path

    def test_explicit_address_parts_and_coordinate_validation(self):
        self.assertEqual(address_parts("广州番禺区淘商城C座"), ("广州", "番禺区"))
        self.assertEqual(address_parts("广东省深圳市南山区科技园"), ("深圳", "南山区"))
        self.assertEqual(address_parts("天河路1号"), (None, None))
        self.assertEqual(address_parts("广州开发区科学大道"), ("广州", None))
        self.assertEqual(coordinates("113.366906,22.931281"), (113.366906, 22.931281))
        for bad in (None, "", "0,0", "200,50", "113,95", "nan,20", "113,20,5"):
            self.assertEqual(coordinates(bad), (None, None))

    def test_detail_address_survives_old_card_import_and_keeps_internal_id(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, current = Path(directory), job()
            old = self.seed(folder, current)
            current.update(location_fields("广州番禺区淘商城C座", raw_coordinates="113.366906,22.931281"), card_location="广州")
            detail = folder / "detail.json"
            app.save_jobs(detail, {"city": "广州", "keyword": "Python", "jobs": [current]})
            job_db.turn_db(detail, folder / "jobs.db")
            job_db.turn_db(old, folder / "jobs.db")
            with closing(sqlite3.connect(folder / "jobs.db")) as con:
                row = con.execute("SELECT id,city,location,card_location,location_city,location_district,longitude,latitude,coordinate_system FROM jobs").fetchone()
            self.assertEqual(row, (1, "广州", "广州番禺区淘商城C座", "广州", "广州", "番禺区", 113.366906, 22.931281, "unknown"))
            self.assertEqual(load_missing_jobs(folder / "jobs.db", "广州", include_locations=True), {})

    def test_old_database_is_migrated_without_recreating_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.db"
            with closing(sqlite3.connect(path)) as con:
                con.executescript(job_db.SCHEMA)
                con.execute("INSERT INTO jobs(id,city,keyword,city_key,keyword_key,job_key,title,location,source_file) VALUES (42,'广州','Python','广州','python','old','旧岗位','广州','fixture')")
                con.commit()
            job_db.initialize_database(path)
            job_db.initialize_database(path)
            with closing(sqlite3.connect(path)) as con:
                self.assertTrue(set(LOCATION_COLUMNS) <= {r[1] for r in con.execute("PRAGMA table_info(jobs)")})
                self.assertEqual(con.execute("SELECT id,title,location FROM jobs").fetchone(), (42, "旧岗位", "广州"))

    def test_reads_only_work_address_node_and_map_attributes(self):
        current, root, cdp = job(), {"nodeId": 1, "documentURL": job()["url"]}, Mock()
        def call(method, params=None):
            return {"attributes": ["data-lat", "113.366906,22.931281"]} if method == 'DOM.getAttributes' else {"node": {"backendNodeId": 10}}
        cdp.call.side_effect = call
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "query", side_effect=[2, 3]), \
                patch.object(app, "rendered_text", return_value="广州番禺区淘商城C座"), patch.object(app, "document", return_value=root):
            fields = app.read_location_from_page(cdp, current, Path(directory), root, Mock())
        self.assertEqual(fields['location'], "广州番禺区淘商城C座")
        self.assertEqual(fields['card_location'], "广州")
        self.assertEqual(fields['longitude'], 113.366906)
        self.assertEqual(fields['coordinate_system'], 'unknown')

    def test_location_backfill_updates_existing_record_without_reading_good_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, current = Path(directory), job()
            self.seed(folder, current)
            groups = load_missing_jobs(folder / "jobs.db", "广州", include_locations=True)
            self.assertTrue(groups['Python'][0]['location_refresh_required'])
            data = {"city": "广州", "keyword": "Python", "requested_count": 200, "jobs": []}
            with patch.object(app, "read_detail_location", return_value=dict(location_fields("广州番禺区淘商城C座"), card_location="广州")) as address, \
                    patch.object(app, "read_detail_salary") as salary, patch.object(app, "read_description_with_login") as desc, patch.object(app.time, "sleep"):
                app.repair_missing_history(Mock(), groups, data, folder, folder / "jobs.json", folder / "jobs.db")
            address.assert_called_once()
            salary.assert_not_called()
            desc.assert_not_called()
            self.assertEqual(data['history_repair']['repaired'], 1)
            self.assertEqual(data['jobs'], [])

    def test_failed_location_backfill_keeps_old_address_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, current = Path(directory), job()
            self.seed(folder, current)
            data = {"city": "广州", "keyword": "Python", "requested_count": 200, "jobs": []}
            with patch.object(app, 'read_detail_location', side_effect=TimeoutError('无地址')), patch.object(app.time, 'sleep'):
                app.repair_missing_history(Mock(), load_missing_jobs(folder / 'jobs.db', '广州', include_locations=True),
                                           data, folder, folder / 'jobs.json', folder / 'jobs.db')
            self.assertEqual(data['history_repair']['failed'], 1)
            with closing(sqlite3.connect(folder / 'jobs.db')) as con:
                self.assertEqual(con.execute('SELECT location FROM jobs').fetchone()[0], '广州')

    def test_location_only_cli_does_not_start_new_list_or_change_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.seed(folder, job())
            with patch('sys.argv', ['collect_jobs.py', '--update-locations']), \
                    patch.object(app, 'OUTPUT', folder), patch.object(app, 'check_ocr_runtime'), \
                    patch.object(app, 'ensure_browser'), patch.object(app, 'open_task_tab', return_value={'id':'task','webSocketDebuggerUrl':'ws://test'}), \
                    patch.object(app.websocket, 'create_connection'), patch.object(app, 'RawCDP'), \
                    patch.object(app, 'read_detail_location', return_value=dict(location_fields('广州番禺区淘商城C座'), card_location='广州')), \
                    patch.object(app, 'collect_districts') as districts, patch.object(app, 'collect_list') as listing, patch.object(app.time, 'sleep'):
                self.assertEqual(app.main(), 0)
            districts.assert_not_called()
            listing.assert_not_called()
            self.assertFalse((folder / 'district-progress.json').exists())
            saved = json.loads(next((folder / 'runs').glob('*/jobs.json')).read_text(encoding='utf-8'))
            self.assertEqual(saved['search_mode'], 'location_backfill')
            self.assertEqual(saved['requested_count'], 0)
            self.assertEqual(saved['history_repair']['repaired'], 1)


if __name__ == '__main__':
    unittest.main()
