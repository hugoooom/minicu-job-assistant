import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import collect_jobs as app
from district_progress import DistrictProgress


def card(cdp, n):
    return (f'<a class="job-name" href="/job_detail/id{n}.html">岗位{n}</a>'
            '<span class="boss-name">公司</span><span class="job-salary">150元/天</span>')


def payload(target=1):
    return {"city": "广州", "keyword": "Python", "requested_count": target,
            "jobs": [], "finish_district": True, "empty_rounds": 3}


class DistrictResumeTests(unittest.TestCase):
    def test_soft_target_finishes_district_and_imports_before_stopping(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, data = Path(directory), payload()
            progress = DistrictProgress(folder / 'progress.json', '广州', 'Python', ['荔湾区', '白云区'])
            def finish():
                for job in data['jobs']:
                    job.update(description='完整正文', description_status='ok')
                app.update_report(data)
                app.import_collected_jobs(folder / 'jobs.json', data, folder / 'jobs.db')
            # 达到目标 1 条后，仍继续收完第三条，再等三轮没有不同岗位。
            with patch.object(app, 'navigate') as navigate, patch.object(app, 'require_login'), \
                    patch.object(app, 'wait_search_scope'), patch.object(app, 'outer_html', side_effect=card), \
                    patch.object(app, 'wait_cards', side_effect=[[1, 2], [1, 2, 3], [1, 2, 3], [1, 2, 3], [1, 2, 3]]), \
                    patch.object(app, 'scroll_list'), patch.object(app.time, 'sleep'):
                reason = app.collect_districts(Mock(), data, folder, folder / 'jobs.json', ['荔湾区', '白云区'], True, 60,
                                              set(), progress=progress, after_district=finish)
            self.assertEqual(reason, 'requested_limit_reached')
            self.assertEqual(len(data['jobs']), 3)
            self.assertEqual(navigate.call_count, 1)
            self.assertEqual(data['district_runs'][0]['stop_reason'], 'no_new_results_after_scroll')
            self.assertEqual(data['database']['status'], 'ok')
            with closing(sqlite3.connect(folder / 'jobs.db')) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 3)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE description_status='ok'").fetchone()[0], 3)
            self.assertTrue(app.collection_complete(dict(data, list_stop_reason=reason)))

    def test_checkpoint_revisits_last_district_and_isolates_search_scope(self):
        names = ['荔湾区', '白云区', '天河区']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.json'
            DistrictProgress(path, '广州', 'Python', names).save('白云区')
            checkpoint = DistrictProgress(path, '广州', 'Python', names)
            self.assertEqual(checkpoint.order(names), ['白云区', '天河区', '荔湾区'])
            self.assertEqual(checkpoint.order(names, restart=True), names)
            self.assertEqual(DistrictProgress(path, '广州', 'Java', names).order(names), names)
            self.assertEqual(DistrictProgress(path, '广州', 'Python', ['荔湾区', '白云区']).order(['荔湾区', '白云区']), ['荔湾区', '白云区'])

    def test_first_district_is_in_database_before_next_district_navigation(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, data, active = Path(directory), payload(1000), [None]
            data['empty_rounds'] = 1
            def navigate(cdp, url):
                active[0] = parse_qs(urlsplit(url).query)['multiBusinessDistrict'][0]
                if active[0] == '440111':
                    with closing(sqlite3.connect(folder / 'jobs.db')) as con:
                        self.assertEqual(con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
            def finish():
                for job in data['jobs']:
                    job.update(description='正文', description_status='ok')
                app.update_report(data)
                app.import_collected_jobs(folder / 'jobs.json', data, folder / 'jobs.db')
            with patch.object(app, 'navigate', side_effect=navigate), patch.object(app, 'require_login'), \
                    patch.object(app, 'wait_search_scope'), patch.object(app.time, 'sleep'), \
                    patch.object(app, 'wait_cards', side_effect=lambda *a: [1] if active[0] == '440103' else [1, 2]), \
                    patch.object(app, 'outer_html', side_effect=card), patch.object(app, 'scroll_list'):
                reason = app.collect_districts(Mock(), data, folder, folder / 'jobs.json', ['荔湾区', '白云区'],
                                              True, 60, set(), after_district=finish)
            self.assertEqual(reason, 'districts_exhausted')
            self.assertEqual(data['database']['total'], 2)

    def test_no_new_jobs_switches_to_next_district_and_preserves_checkpoint_on_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, data = Path(directory), payload(1000)
            progress = DistrictProgress(folder / 'progress.json', '广州', 'Python', ['荔湾区', '白云区', '天河区'])
            with patch.object(app, 'navigate') as navigate, patch.object(app, 'require_login'), \
                    patch.object(app, 'wait_search_scope'), patch.object(app.time, 'sleep'), \
                    patch.object(app, 'collect_list', side_effect=['no_new_results_after_scroll', 'max_scrolls_reached']), \
                    patch.object(app, 'collect_details'):
                reason = app.collect_districts(Mock(), data, folder, folder / 'jobs.json', ['荔湾区', '白云区', '天河区'],
                                              True, 60, set(), progress=progress)
            self.assertEqual(reason, 'district_scroll_budget_reached')
            self.assertEqual(navigate.call_count, 2)
            self.assertEqual(progress.order(['荔湾区', '白云区', '天河区'])[0], '白云区')

    def test_database_failure_keeps_json_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, data = Path(directory), payload()
            data['jobs'] = [{'id':'a','title':'岗位','company':'公司','description':'正文','description_status':'ok'}]
            with patch.object(app, 'turn_db', side_effect=sqlite3.OperationalError('database is locked')):
                app.import_collected_jobs(folder / 'jobs.json', data, folder / 'jobs.db')
            saved = json.loads((folder / 'jobs.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['database']['status'], 'error')
            self.assertEqual(saved['jobs'][0]['description'], '正文')
            app.import_collected_jobs(folder / 'jobs.json', data, folder / 'jobs.db')
            self.assertEqual(data['database']['status'], 'ok')

    def test_completed_details_are_not_visited_on_next_district(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, data = Path(directory), payload()
            data['jobs'] = [{'id':'done','title':'已完成','description_status':'ok'},
                            {'id':'new','title':'待处理','description_status':'pending'}]
            with patch.object(app, 'read_description_with_login', return_value='正文') as read:
                app.collect_details(Mock(), data, folder, folder / 'jobs.json')
            self.assertEqual(read.call_count, 1)
            self.assertEqual(read.call_args.args[1]['id'], 'new')

    def test_custom_empty_round_count(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, data = Path(directory), payload(1000)
            data['empty_rounds'] = 5
            with patch.object(app, 'wait_cards', return_value=[1]) as wait, \
                    patch.object(app, 'outer_html', side_effect=card), patch.object(app, 'scroll_list'), patch.object(app.time, 'sleep'):
                reason = app.collect_list(Mock(), data, folder, folder / 'jobs.json', 'url', True, 60,
                                          district='荔湾区')
            self.assertEqual(reason, 'no_new_results_after_scroll')
            self.assertEqual(wait.call_count, 6)


if __name__ == '__main__':
    unittest.main()
