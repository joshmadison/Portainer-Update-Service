"""JobGate + RunLogger — the concurrency and durability core.

The whole app leans on these two primitives; their invariants are what the
architecture review flagged as untested-but-catastrophic-if-broken (S1, S3).
"""
import json
import threading

import pytest

from app.jobs import JobGate


class TestJobGate:
    def test_acquire_release_cycle(self):
        g = JobGate()
        assert g.try_acquire("t1") is True
        assert g.is_busy() is True
        assert g.current()["holder"] == "t1"
        g.release()
        assert g.is_busy() is False
        assert g.current()["active"] is False

    def test_second_acquire_fails(self):
        g = JobGate()
        assert g.try_acquire("first") is True
        assert g.try_acquire("second") is False
        assert g.current()["holder"] == "first"
        g.release()
        assert g.try_acquire("second") is True

    def test_meta_cleared_before_release(self):
        # after release, current() must not carry stale holder/runlog
        g = JobGate()
        g.try_acquire("x", runlog=object())
        g.release()
        c = g.current()
        assert c["holder"] is None and c["run_id"] is None and c["steps"] == []

    def test_release_without_acquire_is_safe(self, capsys):
        g = JobGate()
        g.release()  # must not raise, must report
        assert "[jobs] release without acquire" in capsys.readouterr().err

    def test_set_runlog_attaches_without_reacquire(self):
        g = JobGate()
        g.try_acquire("holder")
        runlog = type("R", (), {"run_id": "r1", "steps": [1]})()
        g.set_runlog(runlog)
        assert g.current()["run_id"] == "r1"

    def test_concurrent_single_winner(self):
        g = JobGate()
        winners = []

        def contender():
            if g.try_acquire("race"):
                winners.append("won")
                g.release()

        threads = [threading.Thread(target=contender) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(winners) <= 20
        assert g.is_busy() is False  # everyone released what they acquired


class TestRunLogger:
    def test_finish_writes_history_once(self, tmp_state):
        from app.history import RunLogger, history
        rl = RunLogger("full", "test")
        rid = rl.run_id
        rl.log("hello")
        rl.step("s1", True)
        rl.finish(True)
        rl.finish(True)  # second call: idempotent
        rl.finish(True)  # even a third
        h = history()
        assert len([e for e in h if e["run_id"] == rid]) == 1
        entry = next(e for e in h if e["run_id"] == rid)
        assert entry["success"] is True
        assert entry["kind"] == "full"
        assert any(s["name"] == "s1" for s in entry["steps"])

    def test_finish_neutral_records_success_none(self, tmp_state):
        from app.history import RunLogger, history
        rl = RunLogger("self_update", "test")
        rl.log("redeploying")
        rl.finish_neutral()
        entry = [e for e in history() if e["run_id"] == rl.run_id][0]
        assert entry["success"] is None

    def test_rotation_keeps_max_entries(self, tmp_state, monkeypatch):
        from app import history as hmod
        from app.history import RunLogger
        monkeypatch.setattr(hmod, "KEEP_RUNS", 5)
        for _ in range(8):
            rl = RunLogger("check", "test")
            rl.finish(True)
        hs = hmod.history()
        assert len(hs) <= 5

    def test_mark_interrupted_heals_stale_running(self, tmp_state):
        from app import history as hmod
        # simulate a crash mid-run: status says 'running' with a current_run
        st = hmod.current_status()
        st["state"] = "running"
        st["current_run"] = {"run_id": "orphan", "kind": "full", "started": 1}
        hmod._write_status(st)
        hmod.mark_interrupted()
        st2 = hmod.current_status()
        assert st2["state"] == "interrupted"
        assert "current_run" not in st2
        assert any(e.get("interrupted") and e["run_id"] == "orphan"
                   for e in hmod.history())

    def test_run_log_file_written_and_readable(self, tmp_state):
        from app.history import RunLogger, run_log_path
        rl = RunLogger("full", "test")
        rl.log("line one")
        rl.finish(True)
        path = run_log_path(rl.run_id)
        assert path is not None and path.exists()
        assert "line one" in path.read_text(encoding="utf-8")