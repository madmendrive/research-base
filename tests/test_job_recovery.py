"""Stale-job reaper, terminal-failure notification, singleton locks."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import jobs


def _old_ts(minutes=120):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )


class JobQueueCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db = Path(self._tmp.name) / "kb.sqlite"
        real_connect = jobs.kb.connect
        p = mock.patch.object(jobs.kb, "connect", lambda db_path=None: real_connect(db))
        p.start()
        self.addCleanup(p.stop)
        self.conn = jobs.kb.connect()
        self.addCleanup(self.conn.close)
        jobs._init_jobs(self.conn)

    def _insert_running(self, kind="ingest_file", attempts=1, max_attempts=3,
                        claimed_by=None, started_minutes_ago=120):
        cur = self.conn.execute(
            """INSERT INTO jobs (kind, payload_json, status, attempts, max_attempts,
               available_at, created_at, updated_at, started_at, claimed_by)
               VALUES (?, '{}', 'running', ?, ?, ?, ?, ?, ?, ?)""",
            (kind, attempts, max_attempts, _old_ts(), _old_ts(), _old_ts(),
             _old_ts(started_minutes_ago), claimed_by),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _status(self, job_id):
        return self.conn.execute(
            "SELECT status, last_error FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


class TestStaleReaper(JobQueueCase):
    def test_dead_owner_job_requeued_immediately(self):
        job_id = self._insert_running(claimed_by="999999999", started_minutes_ago=1)
        with mock.patch.object(jobs, "_pid_alive", return_value=False):
            n = jobs._recover_stale_running(self.conn)
        self.assertEqual(n, 1)
        self.assertEqual(self._status(job_id)["status"], "queued")

    def test_live_owner_job_never_reaped_even_when_old(self):
        # Regression: a >45-min reindex used to be requeued while still running.
        job_id = self._insert_running(claimed_by=str(os.getpid()),
                                      started_minutes_ago=600)
        n = jobs._recover_stale_running(self.conn)
        self.assertEqual(n, 0)
        self.assertEqual(self._status(job_id)["status"], "running")

    def test_terminal_attempt_crash_marked_failed_and_notified(self):
        # Regression: attempts >= max_attempts rows sat 'running' forever and
        # analyst questions (max_attempts=1) silently vanished on a crash.
        job_id = self._insert_running(kind="analyst_question", attempts=1,
                                      max_attempts=1, claimed_by="999999999")
        sent = []
        with mock.patch.object(jobs, "_pid_alive", return_value=False), \
             mock.patch.object(jobs, "telegram_send", side_effect=lambda t: sent.append(t) or True):
            n = jobs._recover_stale_running(self.conn)
        self.assertEqual(n, 1)
        row = self._status(job_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn("worker died", row["last_error"])
        self.assertEqual(len(sent), 1)
        self.assertIn("analyst_question", sent[0])

    def test_legacy_row_without_pid_uses_age_cutoff(self):
        fresh = self._insert_running(claimed_by=None, started_minutes_ago=5)
        old = self._insert_running(claimed_by=None, started_minutes_ago=120)
        n = jobs._recover_stale_running(self.conn)
        self.assertEqual(n, 1)
        self.assertEqual(self._status(fresh)["status"], "running")
        self.assertEqual(self._status(old)["status"], "queued")

    def test_lane_filter_keeps_reaper_off_other_lane(self):
        heavy = self._insert_running(kind="ingest_file", claimed_by="999999999")
        interactive = self._insert_running(kind="analyst_question",
                                           claimed_by="999999999")
        with mock.patch.object(jobs, "_pid_alive", return_value=False), \
             mock.patch.object(jobs, "telegram_send", return_value=True):
            n = jobs._recover_stale_running(self.conn, kinds=["analyst_question"])
        self.assertEqual(n, 1)
        self.assertEqual(self._status(heavy)["status"], "running")
        self.assertNotEqual(self._status(interactive)["status"], "running")

    def test_claim_records_pid(self):
        self.conn.execute(
            """INSERT INTO jobs (kind, payload_json, status, attempts, max_attempts,
               available_at, created_at, updated_at)
               VALUES ('ingest_file', '{}', 'queued', 0, 3, ?, ?, ?)""",
            (_old_ts(), _old_ts(), _old_ts()),
        )
        self.conn.commit()
        job = jobs._claim_next(self.conn)
        claimed = self.conn.execute(
            "SELECT claimed_by FROM jobs WHERE id = ?", (job["id"],)
        ).fetchone()["claimed_by"]
        self.assertEqual(claimed, str(os.getpid()))


class TestTerminalFailureNotify(JobQueueCase):
    def test_fail_terminal_sends_notice(self):
        job = {"id": self._insert_running(attempts=2, max_attempts=3),
               "kind": "ingest_file", "attempts": 2, "max_attempts": 3,
               "payload_json": '{"path": "note.pdf"}'}
        sent = []
        with mock.patch.object(jobs, "telegram_send", side_effect=lambda t: sent.append(t) or True):
            jobs._fail(self.conn, job, "RuntimeError: boom")
        self.assertEqual(len(sent), 1)
        self.assertIn("permanently failed", sent[0])
        self.assertIn("note.pdf", sent[0])

    def test_fail_nonterminal_stays_quiet(self):
        job = {"id": self._insert_running(attempts=0, max_attempts=3),
               "kind": "ingest_file", "attempts": 0, "max_attempts": 3,
               "payload_json": "{}"}
        with mock.patch.object(jobs, "telegram_send") as send:
            jobs._fail(self.conn, job, "transient")
        send.assert_not_called()


class TestSingletonLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(jobs.kb, "KB_DIR", Path(self._tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_second_holder_refused_while_first_alive(self):
        path = jobs.acquire_singleton_lock("testlock")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        # Simulate a different, live process holding it.
        path.write_text("1", encoding="utf-8")  # pid 1: not ours
        with mock.patch.object(jobs, "_pid_alive", return_value=True):
            with self.assertRaises(RuntimeError):
                jobs.acquire_singleton_lock("testlock")

    def test_stale_lock_taken_over(self):
        lock = Path(self._tmp.name) / "testlock.lock"
        lock.write_text("999999999", encoding="utf-8")
        with mock.patch.object(jobs, "_pid_alive", return_value=False):
            path = jobs.acquire_singleton_lock("testlock")
        self.assertEqual(path.read_text(encoding="utf-8"), str(os.getpid()))
        path.unlink()

    def test_own_pid_lock_taken_over(self):
        lock = Path(self._tmp.name) / "testlock.lock"
        lock.write_text(str(os.getpid()), encoding="utf-8")
        path = jobs.acquire_singleton_lock("testlock")
        self.assertTrue(path.exists())
        path.unlink()


if __name__ == "__main__":
    unittest.main()
