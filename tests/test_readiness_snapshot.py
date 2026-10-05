"""Validate the expanded read-only snapshot on an explicitly disposable Redis."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from lifecycle_edges import POOLS
from verify_lifecycle_state import SNAPSHOT_LUA


@unittest.skipUnless(os.environ.get('LIFECYCLE_TEST_REDIS_PORT'), 'requires throwaway local Redis')
class ReadinessSnapshotTests(unittest.TestCase):
    def redis(self, *args):
        return subprocess.check_output(['redis-cli', '-h', '127.0.0.1', '-p', os.environ['LIFECYCLE_TEST_REDIS_PORT'],
                                        '-n', '15', '--raw', *args], text=True).strip()

    def test_retiring_and_class_indexes_are_observed_without_mutation(self):
        uid = 'regression-' + uuid.uuid4().hex
        vm = uid + '-vm'
        jid = uuid.uuid4().int % 1000000000000
        mapping, detail, vmkey = f'jobs:github_job_id:{jid}', f'jobs:details:{uid}', f'vm:{vm}'
        keys = [f'jobs:by_schedule:{POOLS[0]}:RETIRING', 'pool:ubuntu-24.04-4:busy:reserved',
                'pool:ubuntu-24.04-2:warm:onDemand']
        try:
            self.redis('SET', mapping, uid, 'EX', '60')
            self.redis('SET', detail, json.dumps({'job_id': jid, 'status': 'COMPLETED', 'conclusion': 'SUCCESS', 'retry_count': 3}), 'EX', '60')
            self.redis('SET', vmkey, json.dumps({'vmId': vm, 'warmSinceMs': 10, 'lastHeartbeatSourceMs': 20,
                                                'lastStateSourceMs': 30, 'retirementRequested': True, 'secret_token': 'never-output'}), 'EX', '60')
            self.redis('SADD', keys[0], uid)
            for key in keys[1:]:
                self.redis('SADD', key, vm)
            before = self.redis('GET', vmkey)
            output = self.redis('EVAL_RO', SNAPSHOT_LUA, '0', json.dumps([{'job_id': jid, 'runner_name': vm}]), json.dumps(POOLS))
            row = json.loads(output)[0]
            self.assertEqual([keys[0]], row['memberships'])
            self.assertEqual(set(keys[1:]), set(row['actual_vm_memberships']))
            self.assertEqual(3, row['job']['retry_count'])
            self.assertTrue(row['actual_vm']['retirementRequested'])
            self.assertEqual(30, row['actual_vm']['lastStateSourceMs'])
            self.assertNotIn('secret_token', output)
            self.assertNotIn('never-output', output)
            self.assertEqual(before, self.redis('GET', vmkey))
            self.assertGreater(int(self.redis('TTL', vmkey)), 0)
        finally:
            self.redis('DEL', mapping, detail, vmkey)
            self.redis('SREM', keys[0], uid)
            for key in keys[1:]:
                self.redis('SREM', key, vm)


if __name__ == '__main__':
    unittest.main()
