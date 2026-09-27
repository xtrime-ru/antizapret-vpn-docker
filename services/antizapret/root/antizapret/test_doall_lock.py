#!/usr/bin/env python3
# docker run --rm -v "$PWD/services/antizapret/root/antizapret:/tests:ro" -w /tests ubuntu:24.04 python3 -B -m unittest -v test_doall_lock.py

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

SOURCE = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which('flock'), 'requires util-linux flock')
class DoallLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ('config', 'result', 'bin'):
            (self.root / name).mkdir()
        # Keep the test away from the host's defaults, /dev/shm and results.
        doall = (SOURCE / 'doall.sh').read_text()
        doall = doall.replace('/etc/default/antizapret', str(self.root / 'defaults'))
        doall = doall.replace('/dev/shm/.doall', str(self.root / '.doall'))
        doall = doall.replace('/root/antizapret/result', str(self.root / 'result'))
        (self.root / 'doall.sh').write_text(doall)
        self.script('download.sh', 'exit 0')
        self.script('bin/pkill', 'exit 0')
        self.env = dict(os.environ, PATH=str(self.root / 'bin') + ':' + os.environ['PATH'],
                        DOALL_DISABLED='', IPS_URL='', IPS_WORLD_URL='', ASN_URL='',
                        ASN_WORLD_URL='')

    def script(self, name, body):
        path = self.root / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def start(self):
        return subprocess.Popen(['bash', 'doall.sh'], cwd=self.root, env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)

    def events(self):
        path = self.root / 'events'
        return path.read_text().splitlines() if path.exists() else []

    def wait_for_events(self, count, timeout=3):
        deadline = time.monotonic() + timeout
        while len(self.events()) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(self.events()), count, self.events())

    def finish(self, processes, timeout=5):
        try:
            return [process.wait(timeout=timeout) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()

    def test_parallel_refreshes_are_serialized(self):
        self.script('parse.sh', 'echo start >> events; sleep 0.2; echo end >> events')
        codes = self.finish([self.start() for _ in range(3)])
        self.assertEqual(codes, [0, 0, 0])
        self.assertEqual(self.events(), ['start', 'end'] * 3)

    # The previous existence-based lock waited forever for a leftover file.
    def test_leftover_lock_file_does_not_block(self):
        self.script('parse.sh', 'echo start >> events; echo end >> events')
        (self.root / '.doall_lock').touch()
        self.assertEqual(self.finish([self.start()], timeout=3), [0])
        self.assertEqual(self.events(), ['start', 'end'])

    # timeout --kill-after sends SIGKILL: no trap runs, so a lock file would stay
    # forever. The inherited fd keeps the lock only while the child still runs.
    def test_sigkilled_refresh_releases_lock_after_its_child_exits(self):
        self.script('parse.sh', 'echo start >> events; sleep 0.5; echo end >> events')
        first = self.start()
        try:
            self.wait_for_events(1)
            first.send_signal(signal.SIGKILL)  # doall only; its parse.sh keeps running
            first.wait(timeout=3)
            second = self.start()
            self.assertEqual(self.finish([second]), [0])
        finally:
            if first.poll() is None:
                os.killpg(first.pid, signal.SIGKILL)
                first.wait()
        # The second refresh waited for the orphaned parse.sh, then ran.
        self.assertEqual(self.events(), ['start', 'end', 'start', 'end'])


if __name__ == '__main__':
    unittest.main()
