"""The worker poll loop must survive transient 'database is locked' errors."""

import sqlite3
import unittest
from unittest import mock

from scripts import jobs


class TestRetryLocked(unittest.TestCase):
    def test_retries_through_locked_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with mock.patch("time.sleep"):
            self.assertEqual(jobs._retry_locked(flaky), "ok")
        self.assertEqual(calls["n"], 3)

    def test_non_lock_errors_raise_immediately(self):
        def broken():
            raise sqlite3.OperationalError("no such table: jobs")

        with self.assertRaises(sqlite3.OperationalError):
            jobs._retry_locked(broken)

    def test_gives_up_after_max_attempts(self):
        def always_locked():
            raise sqlite3.OperationalError("database is locked")

        with mock.patch("time.sleep"):
            with self.assertRaises(sqlite3.OperationalError):
                jobs._retry_locked(always_locked, attempts=3)


class TestWorkerPollSurvivesLock(unittest.TestCase):
    def test_poll_lock_does_not_kill_loop(self):
        """First poll raises locked; second poll claims nothing; loop keeps
        going (we stop it via a sentinel on the third poll)."""
        polls = {"n": 0}

        class StopLoop(Exception):
            pass

        def fake_recover(conn):
            polls["n"] += 1
            if polls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            if polls["n"] >= 3:
                raise StopLoop()
            return 0

        with mock.patch.object(jobs, "_recover_stale_running", side_effect=fake_recover), \
             mock.patch.object(jobs, "_claim_next", return_value=None), \
             mock.patch.object(jobs, "_init_jobs"), \
             mock.patch.object(jobs.kb, "connect", return_value=mock.MagicMock()), \
             mock.patch("time.sleep"):
            with self.assertRaises(StopLoop):
                jobs.worker(run_once=False, sleep_seconds=0)
        self.assertEqual(polls["n"], 3)


class TestClaimLanes(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db = Path(self._tmp.name) / "kb.sqlite"
        real_connect = jobs.kb.connect
        p = mock.patch.object(jobs.kb, "connect", lambda db_path=None: real_connect(db))
        p.start()
        self.addCleanup(p.stop)
        jobs.enqueue_job("ingest_file", {"path": "x.pdf"})
        jobs.enqueue_job("analyst_question", {"question": "q"})
        self.conn = jobs.kb.connect()
        self.addCleanup(self.conn.close)

    def test_kinds_lane_claims_only_matching(self):
        job = jobs._claim_next(self.conn, kinds=["analyst_question"])
        self.assertEqual(job["kind"], "analyst_question")
        self.assertIsNone(jobs._claim_next(self.conn, kinds=["analyst_question"]))

    def test_exclude_lane_skips_excluded(self):
        job = jobs._claim_next(self.conn, exclude_kinds=["analyst_question"])
        self.assertEqual(job["kind"], "ingest_file")
        self.assertIsNone(jobs._claim_next(self.conn, exclude_kinds=["analyst_question"]))

    def test_lanes_partition_the_queue(self):
        a = jobs._claim_next(self.conn, exclude_kinds=["analyst_question"])
        b = jobs._claim_next(self.conn, kinds=["analyst_question"])
        self.assertEqual({a["kind"], b["kind"]}, {"ingest_file", "analyst_question"})

    def test_kinds_and_exclude_together_rejected(self):
        with self.assertRaises(ValueError):
            jobs.worker(run_once=True, kinds=["a"], exclude_kinds=["b"])


class TestStudyJob(unittest.TestCase):
    def test_study_job_builds_config_and_runs(self):
        captured = {}

        def fake_run_study(config):
            captured["config"] = config
            return {"studied": 3, "cost_estimate": 1.5}

        job = {
            "id": 1, "kind": "study",
            "payload_json": '{"since_hours": 30, "max_cost": 15, "notify": false}',
        }
        with mock.patch("scripts.study.run_study", side_effect=fake_run_study):
            result = jobs._process_job(job)
        self.assertIn("studied", result)
        cfg = captured["config"]
        self.assertEqual(cfg.since_hours, 30.0)
        self.assertEqual(cfg.max_cost, 15.0)
        self.assertFalse(cfg.force)


if __name__ == "__main__":
    unittest.main()
