import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from flow_regressions import FlowSuite, expectations, grade_flow, grade_unique
from lifecycle_edges import POOLS, REPO
import source_contracts
from source_contracts import redirect_test_redis, test_result
from verify_lifecycle_state import expected_jobs, grade_snapshot


def fixture(scenario='graph', attempt=1):
    case = {'scenario': scenario, 'pool': POOLS[1], 'second_pool': POOLS[0], 'attempt': attempt, 'run_id': 11}
    expected = expectations(scenario, attempt)
    run = {'status': 'completed', 'conclusion': 'failure' if 'failure' in expected.values() else 'success', 'run_attempt': attempt}
    jobs = []
    for i, (name, conclusion) in enumerate(expected.items()):
        pool = case['second_pool'] if name == 'secondary' else case['pool']
        runner = pool.replace('monkci-', 'monkci--', 1).replace('24.04', '24-04') + f'--vm{i}'
        start = '2026-10-04T10:00:10Z' if name in ('left', 'right', 'after_soft_failure', 'cleanup') else '2026-10-04T10:00:20Z' if name == 'join' else '2026-10-04T10:00:00Z'
        end = '2026-10-04T10:06:10Z' if name == 'long' else '2026-10-04T10:00:15Z' if name in ('left', 'right', 'after_soft_failure', 'cleanup') else '2026-10-04T10:00:25Z' if name == 'join' else '2026-10-04T10:00:05Z'
        jobs.append({'id': 100 + i, 'name': name, 'conclusion': conclusion, 'status': 'completed', 'run_attempt': attempt,
                     'runner_name': '' if conclusion == 'skipped' else runner, 'labels': [pool],
                     'created_at': '2026-10-04T10:00:00Z', 'started_at': start, 'completed_at': end})
    return case, run, jobs


class FlowVerdictTests(unittest.TestCase):
    def test_every_scenario_is_graded(self):
        for scenario in ('graph', 'failures', 'matrix', 'long', 'cross_pool', 'fail_once_matrix'):
            for attempt in (1, 2):
                with self.subTest(scenario=scenario, attempt=attempt):
                    c, r, j = fixture(scenario, attempt)
                    self.assertEqual([], grade_flow(c, r, j, 180))

    def test_green_run_without_required_jobs_is_not_green(self):
        c, r, j = fixture()
        for change in ([], j[:-1], j + [j[0]]):
            with self.subTest(jobs=change):
                self.assertTrue(grade_flow(c, r, change, 180))

    def test_job_identity_outcome_and_timing_are_required(self):
        mutations = ({'id': True}, {'id': 0}, {'conclusion': 'skipped'}, {'status': 'queued'},
                     {'run_attempt': 2}, {'runner_name': ''}, {'runner_name': 'GitHub Actions'},
                     {'labels': [POOLS[0]]}, {'created_at': None}, {'started_at': None},
                     {'completed_at': None}, {'started_at': 'invalid'}, {'started_at': '2026-10-04T10:05:00Z'})
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                c, r, j = fixture()
                j[0].update(mutation)
                self.assertTrue(grade_flow(c, r, j, 180))

    def test_workflow_outcomes_cannot_hide_wrong_jobs(self):
        for scenario in ('failures', 'matrix', 'fail_once_matrix'):
            c, r, j = fixture(scenario)
            j[0]['conclusion'] = 'failure' if j[0]['conclusion'] == 'success' else 'success'
            self.assertTrue(grade_flow(c, r, j, 180))

    def test_skipped_dependency_never_consumes_a_runner(self):
        c, r, j = fixture('failures')
        j[1]['runner_name'] = 'unexpected-runner'
        self.assertTrue(grade_flow(c, r, j, 180))

    def test_other_scenarios_must_remain_skipped(self):
        c, r, j = fixture()
        j.append({'name': 'long', 'conclusion': 'skipped', 'runner_name': ''})
        self.assertEqual([], grade_flow(c, r, j, 180))
        j[-1]['conclusion'] = 'success'
        self.assertTrue(grade_flow(c, r, j, 180))

    def test_long_job_must_cross_five_minutes(self):
        c, r, j = fixture('long')
        j[0]['completed_at'] = '2026-10-04T10:04:59Z'
        self.assertTrue(grade_flow(c, r, j, 180))

    def test_dependency_runtime_is_not_queue_time(self):
        c, r, j = fixture()
        for job in j:
            if job['name'] == 'seed':
                job['completed_at'] = '2026-10-04T10:10:00Z'
            elif job['name'] in ('left', 'right'):
                job.update(started_at='2026-10-04T10:10:05Z', completed_at='2026-10-04T10:10:10Z')
            else:
                job.update(started_at='2026-10-04T10:10:15Z', completed_at='2026-10-04T10:10:20Z')
        self.assertEqual([], grade_flow(c, r, j, 180))
        j[1]['started_at'] = '2026-10-04T10:13:01Z'
        self.assertTrue(grade_flow(c, r, j, 180))

    def test_failed_only_matrix_rerun_retains_successes_and_replaces_failure(self):
        first, _, prior = fixture('fail_once_matrix')
        c, r, jobs = fixture('fail_once_matrix', 2)
        c['previous_jobs'] = {j['name']: j for j in prior}
        redo = jobs[1]
        redo.update(id=500, runner_name=redo['runner_name'] + '-replacement')
        self.assertEqual([], grade_flow(c, r, [redo], 180))
        self.assertTrue(grade_flow(c, r, [], 180))
        for mutation in ({'id': prior[1]['id']}, {'runner_name': prior[1]['runner_name']}, {'run_attempt': 1}):
            with self.subTest(mutation=mutation):
                bad = {**redo, **mutation}
                self.assertTrue(grade_flow(c, r, [bad], 180))
        changed = {**prior[0], 'id': 555}
        self.assertEqual([], grade_flow(c, r, [redo, changed], 180))

    def test_github_cloned_successes_preserve_execution_not_api_record_identity(self):
        first, _, prior = fixture('fail_once_matrix')
        c, r, _ = fixture('fail_once_matrix', 2)
        c['previous_jobs'] = {j['name']: j for j in prior}
        copies = copy.deepcopy(prior)
        for job in copies:
            job.update(id=job['id'] + 1000, run_attempt=2, created_at='2026-10-04T10:30:00Z')
        copies[1].update(conclusion='success', runner_name='monkci--ubuntu-24-04-4--replacement',
                         started_at='2026-10-04T10:30:05Z', completed_at='2026-10-04T10:30:10Z')
        self.assertEqual([], grade_flow(c, r, copies, 180))
        cases = [{**first, 'jobs': prior}, {**c, 'jobs': copies}]
        self.assertEqual([], grade_unique(cases))
        report = {'repository': REPO, 'suite_type': 'workflow_flows', 'passed': True, 'cases': cases}
        self.assertEqual({j['id'] for j in prior} | {copies[1]['id']}, {j['job_id'] for j in expected_jobs(report)})
        for mutation in ({'started_at': '2026-10-04T10:30:05Z'}, {'completed_at': '2026-10-04T10:30:10Z'},
                         {'steps': [{'name': 'executed again'}]}, {'runner_name': copies[0]['runner_name'] + '-new'}):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(copies)
                changed[0].update(mutation)
                self.assertTrue(grade_flow(c, r, changed, 180))
                with self.assertRaises(ValueError):
                    expected_jobs({**report, 'cases': [{**c, 'jobs': changed}]})

    def test_duplicate_runners_and_job_ids_across_flows_fail(self):
        c, _, jobs = fixture()
        a = {**c, 'jobs': jobs}
        b = {**c, 'run_id': 12, 'jobs': copy.deepcopy(jobs)}
        self.assertTrue(grade_unique([a, b]))
        b['run_id'] = c['run_id']
        self.assertEqual([], grade_unique([a, b]))

    def test_state_verifier_includes_all_executed_flow_jobs(self):
        c, _, j = fixture('failures')
        report = {'repository': REPO, 'suite_type': 'workflow_flows', 'passed': True, 'cases': [{**c, 'jobs': j}]}
        result = expected_jobs(report)
        self.assertEqual(4, len(result))
        self.assertNotIn(j[1]['id'], [row['job_id'] for row in result])

    def test_retiring_and_reservation_class_leaks_fail(self):
        expected = [{'job_id': 123, 'conclusion': 'SUCCESS'}]
        for membership in ('pool:ubuntu-24.04-4:busy:reserved', 'pool:ubuntu-24.04-4:warm:onDemand'):
            redis = [{'job_id': 123, 'marker': 'completed:SUCCESS', 'actual_vm_memberships': [membership]}]
            self.assertTrue(grade_snapshot(expected, redis, [{'job_id': 123, 'status': 'completed', 'conclusion': 'success'}]))
        redis = [{'job_id': 123, 'marker': 'completed:SUCCESS', 'memberships': ['jobs:by_schedule:pool:RETIRING']}]
        self.assertTrue(grade_snapshot(expected, redis, [{'job_id': 123, 'status': 'completed', 'conclusion': 'success'}]))


class SourceRunnerTests(unittest.TestCase):
    def test_redis_redirect_changes_test_literals_only(self):
        source = 'Addr: "localhost:6379", Port: 6379, Tunnel: "localhost:6378", Other: 16378'
        self.assertEqual('Addr: "localhost:32100", Port: 32100, Tunnel: "localhost:6378", Other: 16378', redirect_test_redis(source, 32100))

    def test_empty_or_skipped_central_tests_cannot_pass(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'out.jsonl'
            for events in ([], [{'Action': 'pass', 'Package': 'p'}],
                           [{'Action': 'skip', 'Package': 'p', 'Test': 'TestRegressionMissing'}, {'Action': 'pass', 'Package': 'p'}]):
                p.write_text('\n'.join(json.dumps(e) for e in events))
                self.assertFalse(test_result(p, 0)['passed'])

    def test_failed_source_tests_remove_owned_redis_and_save_failure(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            args = SimpleNamespace(output=Path(d) / 'out', component=['controller'], controller=Path(d), race=False)
            inspect = json.dumps([{'NetworkSettings': {'Ports': {'6379/tcp': [{'HostPort': '32100'}]}}}])
            with patch.object(source_contracts, 'command', side_effect=['container-id', inspect, 'PONG']), \
                    patch.object(source_contracts, 'source_snapshot', return_value={'head': 'abc', 'dirty': False}), \
                    patch.object(source_contracts, 'overlay_for', return_value=Path(d) / 'overlay.json'), \
                    patch('source_contracts.subprocess.run', return_value=SimpleNamespace(returncode=1)) as run, patch('builtins.print'):
                self.assertEqual(1, source_contracts.execute(args))
            self.assertEqual(['docker', 'rm', '-f'], run.call_args.args[0][:3])
            self.assertFalse(json.loads((args.output / 'report.json').read_text())['passed'])

    def test_leaf_counts_do_not_inflate_table_tests(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'out.jsonl'
            p.write_text('\n'.join(json.dumps({'Action': 'pass', 'Package': 'p', **({'Test': t} if t else {})})
                                   for t in ('TestRegressionTable/a', 'TestRegressionTable/b', 'TestRegressionTable', 'TestOld', None)))
            result = test_result(p, 0)
            self.assertTrue(result['passed'])
            self.assertEqual(2, result['central_cases'])
            self.assertEqual(1, result['existing_cases'])
            self.assertFalse(test_result(p, 1)['passed'])

class WorkflowOrchestrationTests(unittest.TestCase):
    def suite(self, directory):
        from types import SimpleNamespace
        return FlowSuite(SimpleNamespace(output=Path(directory) / 'evidence', ref='fix-ref', deadline_minutes=35,
                                          pool=POOLS[1], second_pool=None, queue_slo=180))

    def test_rerun_waits_for_new_attempt_instead_of_grading_old_completed_run(self):
        c, r, j = fixture('fail_once_matrix', 2)
        with tempfile.TemporaryDirectory() as d:
            suite = self.suite(d)
            with patch.object(suite.gh, 'api', side_effect=[{**r, 'run_attempt': 1}, {**r, 'status': 'queued'}, r]) as api, \
                    patch.object(suite.gh, 'jobs', return_value=j) as jobs, patch.object(suite, 'pause'):
                suite.wait_flows([c])
            self.assertEqual(3, api.call_count)
            jobs.assert_called_once_with(11, 2)
            self.assertEqual([], c['errors'])

    def test_run_discovery_requires_both_unique_title_and_ref(self):
        with tempfile.TemporaryDirectory() as d:
            suite = self.suite(d)
            runs = [{'id': 1, 'display_title': 'ours', 'head_branch': 'other'},
                    {'id': 2, 'display_title': 'foreign', 'head_branch': 'fix-ref'},
                    {'id': 3, 'display_title': 'ours', 'head_branch': 'fix-ref'}]
            with patch.object(suite.gh, 'api', return_value={'workflow_runs': runs}):
                self.assertEqual(3, suite.find_run('ours'))
            with patch.object(suite.gh, 'api', return_value={'workflow_runs': runs + [runs[-1]]}):
                with self.assertRaises(RuntimeError):
                    suite.find_run('ours')

    def test_dispatch_titles_are_unique_even_for_two_graph_cases(self):
        with tempfile.TemporaryDirectory() as d:
            suite = self.suite(d)
            with patch.object(suite.gh, 'api') as api, patch.object(suite, 'find_run', return_value=11), patch('builtins.print'):
                first, second = suite.dispatch_flow('graph'), suite.dispatch_flow('graph')
            self.assertNotEqual(first['title'], second['title'])
            for call in api.call_args_list:
                self.assertEqual('POST', call.args[1])
                self.assertEqual('graph', call.args[2]['inputs']['scenario'])


if __name__ == '__main__':
    unittest.main()
