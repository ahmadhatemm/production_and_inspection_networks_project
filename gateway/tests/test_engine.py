"""Protocol tests for the gateway engine with a fake clock and fake devices (no sockets, no serial port, no waiting).

Run:  python -m unittest tests.test_engine -v
"""
import unittest

from tests.rig import Rig
from gw import serialproto as sp


def ids(msgs):
    return [m["job_id"] for m in msgs]


class CommandValidation(unittest.TestCase):
    def setUp(self):
        self.r = Rig()

    def test_missing_or_bad_cmd_id(self):
        for body in ({}, {"cmd_id": -1}, {"cmd_id": 2 ** 32}, {"cmd_id": True}, {"cmd_id": 1.5}, {"cmd_id": "7"}, {"cmd_id": None}):
            st, out = self.r.eng.handle_command("batch/pause", body, 0)
            self.assertEqual(st, 400, body)
            self.assertEqual(out["error"]["code"], "MALFORMED")
            self.assertIsNone(out["cmd_id"])

    def test_body_must_be_object(self):
        for body in ([], "x", 5, None):
            st, out = self.r.eng.handle_command("batch/pause", body, 0)
            self.assertEqual((st, out["error"]["code"]), (400, "MALFORMED"))

    def test_cmd_id_limits_accepted(self):
        for cid in (0, 4294967295):
            st, out = self.r.cmd("mode", cid, mode="production")
            self.assertEqual(st, 200, cid)

    def test_start_field_limits(self):
        good = dict(target_parts=20, cycle_time_ms=1000, inspect_time_ms=500)
        bad = [dict(good, target_parts=0), dict(good, target_parts=10001), dict(good, cycle_time_ms=9),
               dict(good, cycle_time_ms=60001), dict(good, inspect_time_ms=9), dict(good, inspect_time_ms=60001),
               dict(good, target_parts=1.5), dict(good, target_parts="20"), dict(good, target_parts=True),
               dict(good, target_parts=None)]
        for k in ("target_parts", "cycle_time_ms", "inspect_time_ms"):
            missing = dict(good)
            del missing[k]
            bad.append(missing)
        for fields in bad:
            st, out = self.r.cmd("batch/start", **fields)
            self.assertEqual((st, out["error"]["code"]), (400, "MALFORMED"), fields)
            self.assertIsNotNone(out["cmd_id"])           # the cmd_id is echoed when it was readable

    def test_start_limits_inclusive(self):
        self.r.configure()
        st, _ = self.r.cmd("batch/start", target_parts=1, cycle_time_ms=10, inspect_time_ms=60000)
        self.assertEqual(st, 200)

    def test_config_limits(self):
        for fields in (dict(N=0, K=1), dict(N=-1, K=1), dict(N=2, K=-1), dict(N=2 ** 32, K=0), dict(N=2, K=2 ** 32),
                       dict(N=2.0, K=1), dict(N="2", K=1), dict(N=2), dict(K=1), dict(N=True, K=1)):
            st, out = self.r.cmd("config/inspection", **fields)
            self.assertEqual((st, out["error"]["code"]), (400, "MALFORMED"), fields)

    def test_mode_values(self):
        for m in ("production", "benchmark"):
            self.assertEqual(self.r.cmd("mode", mode=m)[0], 200)
        for m in ("fast", "", None, 1):
            self.assertEqual(self.r.cmd("mode", mode=m)[0], 400)
        self.assertEqual(self.r.cmd("mode")[0], 400)

    def test_unknown_command(self):
        self.assertEqual(self.r.eng.handle_command("batch/explode", {"cmd_id": 1}, 0)[0], 404)

    def test_malformed_is_not_remembered(self):
        self.assertEqual(self.r.cmd("mode", 55, mode="fast")[0], 400)
        self.assertEqual(self.r.cmd("mode", 55, mode="production")[0], 200)   # same cmd_id, now valid: executes


class StateTable(unittest.TestCase):
    """The contract's table of which command is valid in which state, one cell at a time."""

    def reach(self, state):
        r = Rig()
        r.configure()
        if state == "idle":
            return r
        r.start(target=5)
        r.answer_start()
        if state == "running":
            return r
        if state == "paused":
            self.assertEqual(r.cmd("batch/pause")[0], 200)
            return r
        if state == "fault":
            r.run(3100)                                   # nothing from the devices: heartbeat lost
            self.assertEqual(r.eng.state, "fault")
            return r
        raise AssertionError(state)

    CELLS = {
        # command: {state: expected "ok" (200) or 409}
        "batch/start": {"idle": 200, "running": 409, "paused": 409, "fault": 409},
        "batch/pause": {"idle": 409, "running": 200, "paused": 409, "fault": 409},
        "batch/resume": {"idle": 409, "running": 409, "paused": 200, "fault": 409},
        "batch/stop": {"idle": 409, "running": 200, "paused": 200, "fault": 409},
        "fault/clear": {"idle": 409, "running": 409, "paused": 409, "fault": 200},
        "config/inspection": {"idle": 200, "running": 409, "paused": 200, "fault": 409},
        "mode": {"idle": 200, "running": 409, "paused": 409, "fault": 409},
    }
    ARGS = {"batch/start": dict(target_parts=5, cycle_time_ms=100, inspect_time_ms=500),
            "config/inspection": dict(N=5, K=2), "mode": dict(mode="production")}
    AFTER = {"batch/start": "running", "batch/pause": "paused", "batch/resume": "running", "batch/stop": "idle",
             "fault/clear": "idle"}

    def test_every_cell(self):
        for cmd, row in self.CELLS.items():
            for state, expected in row.items():
                with self.subTest(cmd=cmd, state=state):
                    r = self.reach(state)
                    st, out = r.cmd(cmd, **self.ARGS.get(cmd, {}))
                    self.assertEqual(st, expected, out)
                    if expected == 409:
                        self.assertEqual(out["error"]["code"], "INVALID_STATE")
                        self.assertEqual(r.eng.state, state)          # nothing changes
                        self.assertIn(state, out["error"]["message"])
                    else:
                        self.assertEqual(out["stage"], "accepted")
                        self.assertTrue(out["ok"])
                        self.assertFalse(out["duplicate"])
                        self.assertEqual(r.eng.state, self.AFTER.get(cmd, state))
                        self.assertEqual(out["state"], r.eng.state)

    def test_rejected_command_sends_nothing_to_devices(self):
        r = self.reach("running")
        before = (len(r.a_out), len(r.b_out))
        r.cmd("batch/resume")
        r.cmd("batch/start", target_parts=5, cycle_time_ms=100, inspect_time_ms=500)
        self.assertEqual((len(r.a_out), len(r.b_out)), before)

    def test_error_message_format(self):
        r = self.reach("running")
        st, out = r.cmd("batch/resume", 17)
        self.assertEqual(out, {"ok": False, "cmd_id": 17,
                               "error": {"code": "INVALID_STATE", "message": "resume is not valid in state running"}})


class StartPreconditions(unittest.TestCase):
    def test_needs_both_devices(self):
        r = Rig()
        r.cmd("config/inspection", N=5, K=2) if False else None
        # N and K set while B is connected, then B vanishes
        r.configure()
        r.io.b_up = True
        r.run(3100, keepalive=False)      # nobody sends anything: both devices time out (idle: no fault)
        self.assertEqual(r.eng.state, "idle")
        st, out = r.start()
        self.assertEqual((st, out["error"]["code"]), (503, "DEVICE_NOT_READY"))
        self.assertIn("esp32_a", out["error"]["message"])
        self.assertIn("esp32_b", out["error"]["message"])

    def test_only_one_missing(self):
        r = Rig()
        r.configure()
        r.run(3100, keepalive=False)
        r.a_hb()                                   # A is back, B is not
        st, out = r.start()
        self.assertEqual((st, out["error"]["code"]), (503, "DEVICE_NOT_READY"))
        self.assertEqual(out["error"]["message"], "not connected: esp32_b")

    def test_config_needs_b(self):
        r = Rig()
        r.a_hb()
        st, out = r.cmd("config/inspection", N=5, K=2)
        self.assertEqual((st, out["error"]["code"]), (503, "DEVICE_NOT_READY"))

    def test_needs_inspection_rule(self):
        r = Rig()
        r.connect()
        st, out = r.start()
        self.assertEqual((st, out["error"]["code"]), (409, "INVALID_STATE"))
        self.assertIn("config/inspection", out["error"]["message"])

    def test_benchmark_mode_cannot_start(self):
        r = Rig()
        r.configure()
        r.cmd("mode", mode="benchmark")
        st, out = r.start()
        self.assertEqual((st, out["error"]["code"]), (409, "INVALID_STATE"))
        self.assertIn("benchmark", out["error"]["message"])

    def test_missing_device_in_idle_is_only_a_status(self):
        r = Rig()
        r.connect()
        r.run(5000)
        self.assertEqual(r.eng.state, "idle")
        s = r.eng.status(r.now)
        self.assertFalse(s["devices"]["esp32_a"]["connected"])
        self.assertFalse(s["devices"]["esp32_b"]["connected"])
        self.assertEqual(r.ev("fault"), [])


class Duplicates(unittest.TestCase):
    def test_replay_does_not_execute_twice(self):
        r = Rig()
        r.configure()
        st1, out1 = r.start(cmd_id=17)
        n_a, n_b = len(r.a_out), len(r.b_out)
        st2, out2 = r.start(cmd_id=17)
        self.assertEqual(st1, st2)
        self.assertTrue(out2["duplicate"])
        self.assertFalse(out1["duplicate"])
        self.assertEqual({k: v for k, v in out2.items() if k != "duplicate"}, {k: v for k, v in out1.items() if k != "duplicate"})
        self.assertEqual((len(r.a_out), len(r.b_out)), (n_a, n_b))

    def test_replay_of_a_rejection(self):
        r = Rig()
        r.configure()
        st, out = r.cmd("batch/pause", 40)
        self.assertEqual(st, 409)
        r.start()
        r.answer_start()
        st2, out2 = r.cmd("batch/pause", 40)               # the state would allow it now, but the answer is replayed
        self.assertEqual(st2, 409)
        self.assertTrue(out2["duplicate"])
        self.assertEqual(r.eng.state, "running")

    def test_same_cmd_id_other_command_is_malformed(self):
        r = Rig()
        r.cmd("mode", 9, mode="production")
        st, out = r.cmd("mode", 9, mode="benchmark")
        self.assertEqual((st, out["error"]["code"]), (400, "MALFORMED"))
        self.assertEqual(r.eng.mode, "production")
        st, out = r.cmd("fault/clear", 9)
        self.assertEqual(st, 400)

    def test_history_is_bounded_to_1000(self):
        r = Rig()
        for i in range(1005):
            r.cmd("mode", i, mode="production")
        self.assertEqual(len(r.eng.cmd_history), 1000)
        self.assertTrue(r.cmd("mode", 1004, mode="production")[1]["duplicate"])
        self.assertFalse(r.cmd("mode", 0, mode="production")[1]["duplicate"])    # forgotten long ago

    def test_duplicate_while_state_changed_still_replays_original_state(self):
        r = Rig()
        r.configure()
        _, out = r.start(cmd_id=3)
        self.assertEqual(out["state"], "running")
        r.answer_start()
        r.cmd("batch/pause")
        _, again = r.start(cmd_id=3)
        self.assertEqual(again["state"], "running")        # the original response, word for word
        self.assertEqual(r.eng.state, "paused")


class StartFlow(unittest.TestCase):
    def test_start_sends_config_to_b_and_cmd_to_a(self):
        r = Rig()
        r.configure(n=5, k=2)
        r.start(target=20, cycle=1000, inspect=500, cmd_id=17)
        cfg = r.b_msgs("CONFIG")[-1]
        self.assertEqual((cfg["N"], cfg["K"], cfg["inspect_time_ms"]), (5, 2, 500))
        a = r.a_msgs(sp.PM_CMD)[-1]
        self.assertEqual((a.id, a.arg, a.value_a, a.value_b), (17, sp.CMD_START, 1000, 20))

    def test_start_events_wait_for_both_devices(self):
        r = Rig()
        r.configure()
        r.start(cmd_id=17)
        self.assertEqual(r.stages(17), [("delivered", None)])      # both messages written
        cfg = r.b_msgs("CONFIG")[-1]
        r.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": cfg["cmd_id"], "status": "accepted"})
        self.assertEqual(r.stages(17), [("delivered", None)])      # A has not answered yet
        r.a_send(sp.ProdMsg(sp.PM_CMD_ACK, 17, sp.CMDACK_ACCEPTED))
        self.assertEqual(r.stages(17), [("delivered", None), ("accepted", None)])
        r.a_send(sp.ProdMsg(sp.PM_CMD_DONE, 17, sp.DONE_RUNNING))
        self.assertEqual(r.stages(17), [("delivered", None), ("accepted", None)])      # B has not completed
        r.b_send({"type": "CONFIG_DONE", "cmd_id": cfg["cmd_id"], "N": 5, "K": 2, "inspect_time_ms": 500})
        self.assertEqual([s for s, _ in r.stages(17)], ["delivered", "accepted", "completed"])

    def test_no_job_before_config_done(self):
        r = Rig()
        r.configure()
        r.start()
        r.a_part(1)
        r.run(50)
        self.assertEqual(r.b_msgs("JOB_REQ"), [])                  # B has not confirmed the parameters yet
        cfg = r.b_msgs("CONFIG")[-1]
        r.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": cfg["cmd_id"], "status": "accepted"})
        r.b_send({"type": "CONFIG_DONE", "cmd_id": cfg["cmd_id"], "N": 5, "K": 2, "inspect_time_ms": 500})
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 1)

    def test_config_done_is_acknowledged(self):
        r = Rig()
        r.configure()
        cid = r.b_msgs("CONFIG")[-1]["cmd_id"]
        acks = [a for a in r.b_msgs("ACK") if a["ref"] == "CONFIG_DONE"]
        self.assertEqual(acks, [{"type": "ACK", "ref": "CONFIG_DONE", "cmd_id": cid, "status": "accepted"}])

    def test_config_done_with_other_values_is_protocol_error(self):
        r = Rig()
        r.configure()
        r.start()
        cfg = r.b_msgs("CONFIG")[-1]
        r.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": cfg["cmd_id"], "status": "accepted"})
        r.b_send({"type": "CONFIG_DONE", "cmd_id": cfg["cmd_id"], "N": 7, "K": 2, "inspect_time_ms": 500})
        self.assertEqual(r.eng.state, "fault")
        self.assertEqual(r.ev("fault")[0]["code"], "PROTOCOL_ERROR")

    def test_start_overwrites_params_and_resets_batch(self):
        r = Rig()
        r.configure()
        r.start(target=3, cycle=250, inspect=300)
        s = r.eng.status(r.now)
        self.assertEqual(s["params"], {"cycle_time_ms": 250, "inspect_time_ms": 300, "N": 5, "K": 2})
        self.assertEqual(s["batch"], {"target": 3, "produced": 0, "queued": 0, "inspected": 0, "accepted": 0, "rejected": 0})

    def test_a_rejecting_start_is_a_failure(self):
        r = Rig()
        r.configure()
        r.start(cmd_id=17)
        r.a_send(sp.ProdMsg(sp.PM_CMD_ACK, 17, sp.CMDACK_REJECTED))
        self.assertIn(("rejected", "device_rejected"), r.stages(17))
        self.assertEqual(r.eng.state, "fault")
        self.assertEqual(r.ev("fault")[0]["code"], "PROTOCOL_ERROR")
        self.assertEqual(r.ev("fault")[0]["device"], "esp32_a")

    def test_b_rejecting_config_reason_is_passed_through(self):
        r = Rig()
        r.configure()
        r.start(cmd_id=17)
        cfg = r.b_msgs("CONFIG")[-1]
        r.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": cfg["cmd_id"], "status": "rejected", "reason": "out_of_range"})
        self.assertIn(("rejected", "out_of_range"), r.stages(17))
        self.assertEqual(r.eng.state, "fault")


class ConfigCommand(unittest.TestCase):
    def test_events_and_params(self):
        r = Rig()
        r.connect()
        _, out = r.cmd("config/inspection", 18, N=5, K=2)
        self.assertEqual(out["stage"], "accepted")
        self.assertEqual(r.stages(18), [("delivered", None)])
        cfg = r.b_msgs("CONFIG")[0]
        self.assertEqual(cfg["cmd_id"], 18)
        r.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": 18, "status": "accepted"})
        r.b_send({"type": "CONFIG_DONE", "cmd_id": 18, "N": 5, "K": 2, "inspect_time_ms": 500})
        self.assertEqual([s for s, _ in r.stages(18)], ["delivered", "accepted", "completed"])
        self.assertEqual((r.eng.params["N"], r.eng.params["K"]), (5, 2))

    def test_rejected_by_b(self):
        r = Rig()
        r.connect()
        r.cmd("config/inspection", 18, N=5, K=2)
        r.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": 18, "status": "rejected", "reason": "malformed"})
        self.assertEqual(r.stages(18)[-1], ("rejected", "malformed"))
        self.assertEqual(r.eng.state, "idle")              # in idle a problem is only reported

    def test_retried_with_same_bytes(self):
        r = Rig()
        r.connect()
        r.cmd("config/inspection", 18, N=5, K=2)
        r.run(2100, keepalive=True)
        sent = [raw for (d, raw) in r.b_out if d["type"] == "CONFIG"]
        self.assertEqual(len(sent), 4)
        self.assertEqual(len(set(sent)), 1)                # four identical sends
        self.assertEqual(r.stages(18)[-1], ("failed", "retries_exhausted"))
        self.assertEqual(r.eng.state, "idle")              # idle: no fault state

    def test_config_after_hello_when_known(self):
        r = Rig()
        r.configure(n=7, k=3)
        r.eng.on_b_closed(r.now)
        r.run(100)
        r.b_out.clear()
        r.hello()
        cfgs = r.b_msgs("CONFIG")
        self.assertEqual(len(cfgs), 1)
        self.assertEqual((cfgs[0]["N"], cfgs[0]["K"]), (7, 3))

    def test_no_config_after_hello_when_unknown(self):
        r = Rig()
        r.connect()
        self.assertEqual(r.b_msgs("CONFIG"), [])

    def test_paused_config_does_not_change_running_job(self):
        r = Rig()
        r.configure(n=5, k=2)
        r.start(target=3)
        r.answer_start()
        r.a_part(1)
        job = r.b_msgs("JOB_REQ")[-1]
        r.job_ack(job)
        r.cmd("batch/pause")
        r.cmd("config/inspection", N=4, K=0)
        cfg = r.b_msgs("CONFIG")[-1]
        r.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": cfg["cmd_id"], "status": "accepted"})
        r.b_send({"type": "CONFIG_DONE", "cmd_id": cfg["cmd_id"], "N": 4, "K": 0, "inspect_time_ms": 500})
        r.job_result(job, n=5, k=2)                        # the running job reports the N and K it was accepted with
        self.assertEqual(r.eng.batch.inspected, 1)
        self.assertEqual(r.eng.state, "paused")


class Parts(unittest.TestCase):
    def running(self, target=10, **kw):
        r = Rig(**kw)
        r.configure()
        r.start(target=target)
        r.answer_start()
        return r

    def test_part_is_acked_and_queued(self):
        r = self.running()
        r.a_part(1)
        acks = r.a_msgs(sp.PM_PART_ACK)
        self.assertEqual([(a.id, a.arg) for a in acks], [(1, sp.ACK_ACCEPTED)])
        self.assertEqual(r.part_stages(1)[:2], ["produced", "queued"])
        s = r.eng.status(r.now)
        self.assertEqual((s["batch"]["produced"], s["batch"]["queued"], s["queue"]["depth"]), (1, 1, 1))

    def test_dispatch_delivers_in_order_with_distinct_job_ids(self):
        r = self.running()
        for p in (1, 2, 3):
            r.a_part(p)
        job1 = r.b_msgs("JOB_REQ")[-1]
        self.assertEqual(job1["part_id"], 1)
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 1)           # one job in flight
        r.job_ack(job1)
        r.job_result(job1)
        job2 = r.b_msgs("JOB_REQ")[-1]
        self.assertEqual(job2["part_id"], 2)
        self.assertNotEqual(job1["job_id"], job2["job_id"])
        self.assertEqual(job1["job_id"], 100)                    # rig sets job_id_start=100

    def test_part_stages_in_order(self):
        r = self.running()
        r.a_part(1)
        job = r.b_msgs("JOB_REQ")[-1]
        r.job_ack(job)
        r.job_result(job)
        self.assertEqual(r.part_stages(1), ["produced", "queued", "delivered", "accepted", "completed"])
        done = r.ev("part_event", part_id=1, stage="completed")[0]
        self.assertEqual((done["job_id"], done["verdict"]), (job["job_id"], "PASS"))
        for stage in ("produced", "queued"):
            self.assertIsNone(r.ev("part_event", part_id=1, stage=stage)[0]["job_id"])
        self.assertEqual(r.ev("part_event", part_id=1, stage="delivered")[0]["job_id"], job["job_id"])
        self.assertNotIn("verdict", r.ev("part_event", part_id=1, stage="accepted")[0])

    def test_duplicate_part_reacked_not_requeued(self):
        r = self.running()
        r.a_part(1)
        r.a_part(1)
        r.a_part(1)
        self.assertEqual(len(r.a_msgs(sp.PM_PART_ACK)), 3)
        self.assertEqual(r.eng.batch.produced, 1)
        self.assertEqual(len(r.eng.queue), 1)
        self.assertEqual(r.part_stages(1).count("produced"), 1)

    def test_part_of_a_finished_part_is_reacked(self):
        r = self.running()
        r.a_part(1)
        job = r.b_msgs("JOB_REQ")[-1]
        r.job_ack(job)
        r.job_result(job)
        r.a_part(1)                                            # A never saw our ACK
        self.assertEqual(r.eng.batch.produced, 1)
        self.assertEqual(r.a_msgs(sp.PM_PART_ACK)[-1].arg, sp.ACK_ACCEPTED)
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 1)

    def test_ignored_when_idle_or_out_of_range(self):
        r = Rig()
        r.configure()
        r.a_part(1)                                            # idle
        self.assertEqual(r.a_msgs(sp.PM_PART_ACK), [])
        r2 = self.running(target=3)
        r2.a_part(0)
        r2.a_part(4)
        self.assertEqual(r2.a_msgs(sp.PM_PART_ACK), [])
        self.assertEqual(r2.eng.batch.produced, 0)

    def test_queue_full_blocks_production(self):
        r = self.running()
        for p in range(1, 6):
            r.a_part(p)
        self.assertEqual(len(r.eng.queue), 5)
        self.assertFalse(r.eng.status(r.now)["queue"]["blocked"])
        r.a_part(6)                                            # the sixth does not fit
        self.assertEqual((r.a_msgs(sp.PM_PART_ACK)[-1].id, r.a_msgs(sp.PM_PART_ACK)[-1].arg), (6, sp.ACK_QUEUE_FULL))
        s = r.eng.status(r.now)
        self.assertTrue(s["queue"]["blocked"])
        self.assertEqual((s["queue"]["depth"], s["queue"]["capacity"]), (5, 5))
        self.assertEqual(s["batch"]["produced"], 6)            # seen, but held, not queued
        self.assertEqual(r.part_stages(6), ["produced"])
        r.a_part(6)                                            # A tries again every 200 ms: still full
        self.assertEqual(r.a_msgs(sp.PM_PART_ACK)[-1].arg, sp.ACK_QUEUE_FULL)
        self.assertEqual(r.eng.batch.produced, 6)              # counted once

    def test_queue_frees_up_and_held_part_is_taken(self):
        r = self.running()
        for p in range(1, 7):
            r.a_part(p)
        job = r.b_msgs("JOB_REQ")[-1]
        r.job_ack(job)
        r.job_result(job)
        self.assertEqual(len(r.eng.queue), 4)
        r.a_part(6)
        self.assertEqual(r.a_msgs(sp.PM_PART_ACK)[-1].arg, sp.ACK_ACCEPTED)
        self.assertEqual(len(r.eng.queue), 5)
        self.assertFalse(r.eng.status(r.now)["queue"]["blocked"])
        self.assertEqual(r.part_stages(6), ["produced", "queued"])

    def test_nothing_is_lost_or_duplicated_through_a_full_queue(self):
        r = self.running(target=12)
        seen = []
        part = 1
        for _ in range(400):                                   # A offers its pending part, B works through the queue
            r.a_part(part)
            if r.a_msgs(sp.PM_PART_ACK)[-1].id == part and r.a_msgs(sp.PM_PART_ACK)[-1].arg == sp.ACK_ACCEPTED and part < 12:
                part += 1
            jobs = r.b_msgs("JOB_REQ")
            if jobs and jobs[-1]["job_id"] not in seen:
                seen.append(jobs[-1]["job_id"])
                r.job_ack(jobs[-1])
                r.job_result(jobs[-1])
            r.run(5)
            if r.eng.state == "idle":
                break
        self.assertEqual(r.eng.state, "idle")
        self.assertEqual([j["part_id"] for j in r.b_msgs("JOB_REQ")], list(range(1, 13)))
        self.assertEqual((r.eng.batch.produced, r.eng.batch.inspected), (12, 12))

    def test_part_during_pause_is_queued_not_dispatched(self):
        r = self.running()
        r.cmd("batch/pause")
        r.a_part(1)                                            # a part that was already on the wire
        self.assertEqual(len(r.eng.queue), 1)
        self.assertEqual(r.b_msgs("JOB_REQ"), [])
        r.cmd("batch/resume")
        r.run(10)
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 1)


class BatchLifecycle(unittest.TestCase):
    def run_batch(self, target=5, n=5, k=2, verdict_override=None):
        r = Rig()
        r.configure(n=n, k=k)
        r.start(target=target, cmd_id=50)
        r.answer_start()
        for p in range(1, target + 1):
            r.a_part(p)
            job = r.b_msgs("JOB_REQ")[-1]
            r.job_ack(job)
            r.job_result(job, n=n, k=k, verdict=(verdict_override or {}).get(p))
        return r

    def test_complete_batch(self):
        r = self.run_batch(target=5, n=5, k=2)
        self.assertEqual(r.eng.state, "idle")
        done = r.ev("batch_complete")
        self.assertEqual(len(done), 1)
        d = done[0]
        # (part_id + 2) mod 5 == 0 -> part 3 fails
        self.assertEqual((d["target"], d["produced"], d["inspected"], d["accepted"], d["rejected"]), (5, 5, 5, 4, 1))
        self.assertIn("duration_ms", d)
        self.assertEqual(r.eng.get_report()[0], 200)
        self.assertEqual(r.eng.get_report()[1]["accepted"], 4)

    def test_report_not_available_before_completion_or_after_start(self):
        r = Rig()
        self.assertEqual(r.eng.get_report()[0], 409)
        r = self.run_batch(target=2)
        self.assertEqual(r.eng.get_report()[0], 200)
        r.start(target=2)
        self.assertEqual(r.eng.get_report()[0], 409)

    def test_gateway_never_computes_a_verdict(self):
        # B says FAIL for part 1 although the rule would give PASS: the gateway counts what B says.
        r = self.run_batch(target=2, verdict_override={1: "FAIL", 2: "PASS"})
        d = r.ev("batch_complete")[0]
        self.assertEqual((d["accepted"], d["rejected"]), (1, 1))
        self.assertEqual(r.ev("part_event", part_id=1, stage="completed")[0]["verdict"], "FAIL")

    def test_counters_in_status(self):
        r = Rig()
        r.configure()
        r.start(target=4)
        r.answer_start()
        for p in (1, 2):
            r.a_part(p)
            job = r.b_msgs("JOB_REQ")[-1]
            r.job_ack(job)
            r.job_result(job)
        r.a_part(3)
        r.a_part(4)
        b = r.eng.status(r.now)["batch"]
        self.assertEqual(b, {"target": 4, "produced": 4, "queued": 2, "inspected": 2, "accepted": 2, "rejected": 0})

    def test_second_batch_restarts_counting(self):
        r = self.run_batch(target=3)
        r.start(target=3)
        r.answer_start()
        self.assertEqual(r.eng.batch.produced, 0)
        r.a_part(1)                                            # part_id 1 again: a new part
        self.assertEqual(r.eng.batch.produced, 1)
        job = r.b_msgs("JOB_REQ")[-1]
        self.assertEqual(job["part_id"], 1)
        self.assertEqual(job["job_id"], 103)                   # job ids keep counting: never reused

    def test_batch_completes_after_resume_if_last_result_came_while_paused(self):
        r = Rig()
        r.configure()
        r.start(target=1)
        r.answer_start()
        r.a_part(1)
        job = r.b_msgs("JOB_REQ")[-1]
        r.job_ack(job)
        r.cmd("batch/pause")
        r.job_result(job)
        self.assertEqual(r.eng.state, "paused")
        self.assertEqual(r.ev("batch_complete"), [])
        r.cmd("batch/resume")
        self.assertEqual(r.eng.state, "idle")
        self.assertEqual(len(r.ev("batch_complete")), 1)

    def test_events_are_json_serialisable_and_enveloped(self):
        import json
        r = self.run_batch(target=3)
        for e in r.events:
            json.dumps(e)
            self.assertEqual(e["v"], 1)
            self.assertIsInstance(e["ts"], int)


class PauseResumeStop(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.r.configure()
        self.r.start(target=10)
        self.r.answer_start()

    def test_pause_resume_commands_reach_a_with_their_cmd_ids(self):
        r = self.r
        r.cmd("batch/pause", 61)
        c = r.a_msgs(sp.PM_CMD)[-1]
        self.assertEqual((c.id, c.arg), (61, sp.CMD_PAUSE))
        r.answer_a_cmd(c)
        self.assertEqual([s for s, _ in r.stages(61)], ["delivered", "accepted", "completed"])
        r.cmd("batch/resume", 62)
        c = r.a_msgs(sp.PM_CMD)[-1]
        self.assertEqual((c.id, c.arg), (62, sp.CMD_RESUME))

    def test_no_dispatch_while_paused(self):
        r = self.r
        r.a_part(1)
        job = r.b_msgs("JOB_REQ")[-1]
        r.job_ack(job)
        r.cmd("batch/pause")
        r.job_result(job)                                      # in-flight work finishes
        self.assertEqual(r.eng.batch.inspected, 1)
        r.a_part(2)
        r.run(100)
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 1)          # part 2 waits

    def test_stop_discards_queue_and_late_result(self):
        r = self.r
        for p in (1, 2, 3):
            r.a_part(p)
        job = r.b_msgs("JOB_REQ")[-1]
        r.job_ack(job)
        r.cmd("batch/stop", 70)
        self.assertEqual(r.eng.state, "idle")
        self.assertEqual(len(r.eng.queue), 0)
        c = r.a_msgs(sp.PM_CMD)[-1]
        self.assertEqual((c.id, c.arg), (70, sp.CMD_STOP))
        r.job_result(job)                                      # B finishes the job it was given
        ack = r.b_msgs("ACK")[-1]
        self.assertEqual((ack["ref"], ack["status"], ack.get("reason")), ("JOB_RESULT", "rejected", "stale"))
        self.assertEqual(r.eng.batch.inspected, 0)             # discarded, not counted
        self.assertEqual(r.ev("fault"), [])                    # and not a fault: we stopped it ourselves
        self.assertEqual(r.eng.state, "idle")

    def test_stop_is_not_batch_completion(self):
        r = self.r
        r.cmd("batch/stop")
        self.assertEqual(r.ev("batch_complete"), [])
        self.assertEqual(r.eng.get_report()[0], 409)

    def test_part_after_stop_ignored(self):
        r = self.r
        r.cmd("batch/stop")
        n = len(r.a_msgs(sp.PM_PART_ACK))
        r.a_part(1)
        self.assertEqual(len(r.a_msgs(sp.PM_PART_ACK)), n)

    def test_start_right_after_stop_waits_for_the_old_job(self):
        r = self.r
        r.a_part(1)
        job = r.b_msgs("JOB_REQ")[-1]
        r.job_ack(job)
        r.cmd("batch/stop")
        r.start(target=3)
        r.answer_start()
        r.a_part(1)
        r.run(50)
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 1)          # B may still be busy with the old job: wait
        r.job_result(job)                                      # the stale result arrives and clears the wait
        r.run(10)
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 2)
        self.assertNotEqual(r.b_msgs("JOB_REQ")[-1]["job_id"], job["job_id"])


class Retries(unittest.TestCase):
    def test_a_command_retried_with_identical_bytes_then_failed(self):
        r = Rig()
        r.configure()
        r.start(cmd_id=17)
        r.run(2100, keepalive=True)
        cmds = [(seq, raw) for (seq, m, raw) in r.a_out if m.type == sp.PM_CMD and m.id == 17]
        self.assertEqual(len(cmds), 4)
        self.assertEqual(len({raw for _, raw in cmds}), 1)     # same bytes, same ATR seq
        self.assertEqual(len({seq for seq, _ in cmds}), 1)
        self.assertIn(("failed", "retries_exhausted"), r.stages(17))
        self.assertEqual(r.ev("fault")[0]["code"], "RETRIES_EXHAUSTED")
        self.assertEqual(r.eng.state, "fault")

    def test_retry_timing(self):
        r = Rig()
        r.configure()
        t0 = r.now
        r.start(cmd_id=17)
        times = []
        n = len(r.a_out)
        while r.now < t0 + 2500:
            r.run(5, keepalive=True)
            for (_s, m, _raw) in r.a_out[n:]:
                if m.type == sp.PM_CMD and m.id == 17:
                    times.append(r.now - t0)
            n = len(r.a_out)
        self.assertEqual(len(times), 3)                        # the first send was at t0 itself
        for got, want in zip(times, (500, 1000, 1500)):
            self.assertTrue(want <= got <= want + 10, (got, want))

    def test_late_ack_stops_the_retries(self):
        r = Rig()
        r.configure()
        r.start(cmd_id=17)
        r.run(700, keepalive=True)
        r.answer_start()
        r.run(3000, keepalive=True)
        self.assertEqual(len([1 for (_s, m, _r) in r.a_out if m.type == sp.PM_CMD and m.id == 17]), 2)
        self.assertEqual(r.eng.state, "running")

    def test_job_req_retried_with_identical_bytes_then_fault(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        r.a_part(1)
        r.run(2100, keepalive=True)
        jobs = [raw for (d, raw) in r.b_out if d["type"] == "JOB_REQ"]
        self.assertEqual(len(jobs), 4)
        self.assertEqual(len(set(jobs)), 1)
        self.assertEqual(r.eng.state, "fault")
        self.assertEqual(r.ev("fault")[0]["code"], "RETRIES_EXHAUSTED")
        self.assertEqual(r.eng.batch.inspected, 0)             # never reported as a success

    def test_ack_arrives_for_the_second_copy_only(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        r.a_part(1)
        r.run(600, keepalive=True)
        job = r.b_msgs("JOB_REQ")[-1]
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 2)
        r.job_ack(job)
        r.run(2000, keepalive=True)
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 2)
        self.assertEqual(r.part_stages(1).count("delivered"), 1)
        self.assertEqual(r.part_stages(1).count("accepted"), 1)

    def test_unwritable_link_counts_as_no_answer(self):
        r = Rig()
        r.configure()
        r.io.a_up = False
        r.start(cmd_id=17)
        self.assertEqual(r.stages(17), [])                     # nothing was delivered
        r.io.a_up = True
        r.run(600, keepalive=True)
        self.assertEqual([s for s, _ in r.stages(17)][:1], ["delivered"])


class Heartbeats(unittest.TestCase):
    def test_gateway_heartbeats_every_second(self):
        r = Rig()
        r.connect()
        r.run(5000, keepalive=True)
        a = r.a_msgs(sp.PM_HEARTBEAT)
        b = r.b_msgs("HEARTBEAT")
        self.assertTrue(5 <= len(a) <= 7, len(a))
        self.assertTrue(5 <= len(b) <= 7, len(b))
        self.assertEqual([m.id for m in a], list(range(len(a))))           # counters 0, 1, 2, ...
        self.assertEqual([m["seq"] for m in b], list(range(len(b))))

    def test_a_seq_counts_every_frame_from_zero(self):
        r = Rig()
        r.connect()
        r.run(3000, keepalive=True)
        seqs = [s for (s, _m, _r) in r.a_out]
        self.assertEqual(seqs, list(range(len(seqs))))

    def test_no_heartbeat_to_a_when_port_closed(self):
        r = Rig()
        r.io.a_is_open = False
        r.run(3000)
        self.assertEqual(r.a_out, [])

    def test_b_lost_while_running_is_a_fault(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        for _ in range(30):                                    # A keeps beating, B goes silent
            r.run(100)
            r.a_hb()
        self.assertEqual(r.eng.state, "fault")
        f = r.ev("fault")[0]
        self.assertEqual((f["code"], f["device"]), ("HEARTBEAT_LOST", "esp32_b"))
        self.assertIn("3", f["message"])
        self.assertEqual(r.b_closed, 1)

    def test_a_lost_while_running_is_a_fault(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        for _ in range(30):
            r.run(100)
            r.b_hb()
        f = r.ev("fault")[0]
        self.assertEqual((r.eng.state, f["code"], f["device"]), ("fault", "HEARTBEAT_LOST", "esp32_a"))

    def test_loss_while_paused_is_a_fault_too(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        r.cmd("batch/pause")
        for _ in range(35):
            r.run(100)
            r.a_hb()
        self.assertEqual(r.eng.state, "fault")

    def test_timeout_is_3000_ms(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        r.a_hb()
        r.b_hb()
        t0 = r.now
        while r.eng.state != "fault" and r.now < t0 + 5000:
            r.run(5)
        self.assertTrue(3000 <= r.now - t0 <= 3010, r.now - t0)

    def test_any_valid_message_counts_as_a_sign_of_life(self):
        r = Rig()
        r.configure()
        r.start(inspect=60000)
        r.answer_start()
        r.a_part(1)
        r.job_ack(r.b_msgs("JOB_REQ")[-1])
        for _ in range(60):
            r.run(100)
            r.a_part(1)                                        # only repeated parts, no heartbeat messages from A
            r.b_hb()
        self.assertEqual(r.eng.state, "running")

    def test_b_connection_closed_is_immediate_fault(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        r.eng.on_b_closed(r.now)
        self.assertEqual(r.eng.state, "fault")
        self.assertEqual(r.ev("fault")[0]["code"], "HEARTBEAT_LOST")
        self.assertFalse(r.eng.status(r.now)["devices"]["esp32_b"]["connected"])

    def test_fault_sends_stop_to_a_and_drops_the_queue(self):
        r = Rig()
        r.configure()
        r.start(target=10)
        r.answer_start()
        for p in (1, 2, 3):
            r.a_part(p)
        r.b_hb()
        r.eng.on_b_closed(r.now)
        stop = r.a_msgs(sp.PM_CMD)[-1]
        self.assertEqual(stop.arg, sp.CMD_STOP)
        self.assertGreaterEqual(stop.id, r.cfg.internal_cmd_id_base)
        self.assertEqual(len(r.eng.queue), 0)
        self.assertEqual(r.eng.batch.produced, 3)              # counters stay visible for the operator
        self.assertEqual(r.eng.state, "fault")

    def test_fault_clear_returns_to_idle(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        r.eng.on_b_closed(r.now)
        self.assertEqual(r.cmd("fault/clear", 90)[0], 200)
        self.assertEqual(r.eng.state, "idle")
        self.assertEqual([s for s, _ in r.stages(90)], ["completed"])
        self.assertIsNone(r.eng.fault_message())

    def test_fault_event_content(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        r.eng.on_b_closed(r.now)
        f = r.ev("fault")[0]
        self.assertEqual(set(f), {"v", "type", "ts", "code", "device", "message"})
        self.assertIsNotNone(r.eng.fault_message())

    def test_b_reconnect_gets_config_and_work_resumes_after_clear(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        r.eng.on_b_closed(r.now)
        r.cmd("fault/clear")
        r.b_out.clear()
        r.hello()
        self.assertEqual(len(r.b_msgs("CONFIG")), 1)


class Results(unittest.TestCase):
    def running_with_job(self, target=5):
        r = Rig()
        r.configure(n=5, k=2)
        r.start(target=target)
        r.answer_start()
        r.a_part(1)
        job = r.b_msgs("JOB_REQ")[-1]
        r.job_ack(job)
        return r, job

    def test_duplicate_result_is_acked_and_not_counted(self):
        r, job = self.running_with_job()
        r.job_result(job)
        r.job_result(job)
        r.job_result(job)
        self.assertEqual(r.eng.batch.inspected, 1)
        acks = [a for a in r.b_msgs("ACK") if a["ref"] == "JOB_RESULT"]
        self.assertEqual(len(acks), 3)
        self.assertTrue(all(a["status"] == "accepted" for a in acks))
        self.assertEqual(r.part_stages(1).count("completed"), 1)

    def test_result_for_unknown_job_is_stale(self):
        r, job = self.running_with_job()
        r.b_send({"type": "JOB_RESULT", "job_id": 999, "part_id": 1, "verdict": "PASS", "N": 5, "K": 2})
        ack = r.b_msgs("ACK")[-1]
        self.assertEqual((ack["status"], ack["reason"], ack["job_id"]), ("rejected", "stale", 999))
        self.assertEqual(r.eng.state, "fault")
        self.assertEqual(r.ev("fault")[0]["code"], "STALE_RESULT")
        self.assertEqual(r.eng.batch.inspected, 0)

    def test_result_for_wrong_part_is_protocol_error(self):
        r, job = self.running_with_job()
        r.b_send({"type": "JOB_RESULT", "job_id": job["job_id"], "part_id": 2, "verdict": "PASS", "N": 5, "K": 2})
        self.assertEqual(r.eng.state, "fault")
        self.assertEqual(r.ev("fault")[0]["code"], "PROTOCOL_ERROR")
        self.assertEqual(r.eng.batch.inspected, 0)
        self.assertEqual(r.b_msgs("ACK")[-1]["status"], "rejected")

    def test_result_with_other_n_or_k_is_protocol_error(self):
        for n, k in ((4, 2), (5, 3)):
            r, job = self.running_with_job()
            r.job_result(job, n=n, k=k)
            self.assertEqual(r.eng.state, "fault", (n, k))
            self.assertEqual(r.eng.batch.inspected, 0)

    def test_bad_verdict_or_fields_rejected(self):
        for patch in ({"verdict": "MAYBE"}, {"verdict": None}, {"N": "5"}, {"part_id": -1}):
            r, job = self.running_with_job()
            msg = {"type": "JOB_RESULT", "job_id": job["job_id"], "part_id": 1, "verdict": "PASS", "N": 5, "K": 2}
            msg.update(patch)
            r.b_send(msg)
            self.assertEqual(r.eng.state, "fault", patch)
            self.assertEqual(r.eng.batch.inspected, 0, patch)

    def test_result_after_age_limit_is_stale(self):
        r, job = self.running_with_job()
        r.run(500 + 3000 - 20, keepalive=True)
        self.assertEqual(r.eng.state, "running")
        r.run(40, keepalive=True)                              # the gateway gives up itself at the limit
        self.assertEqual(r.eng.state, "fault")
        self.assertEqual(r.ev("fault")[0]["code"], "STALE_RESULT")
        r.job_result(job)                                      # and when it finally arrives, it is not counted
        self.assertEqual(r.eng.batch.inspected, 0)
        self.assertEqual(r.b_msgs("ACK")[-1]["status"], "rejected")

    def test_result_that_overtakes_the_timer_is_stale(self):
        r, job = self.running_with_job()
        r.now += 500 + 3000 + 1                                # no tick ran in between: the result itself is checked
        r.job_result(job)
        self.assertEqual(r.eng.batch.inspected, 0)
        self.assertEqual(r.ev("fault")[0]["code"], "STALE_RESULT")
        self.assertEqual(r.b_msgs("ACK")[-1]["reason"], "stale")

    def test_result_exactly_in_time_is_accepted(self):
        r, job = self.running_with_job()
        r.run(500 + 3000 - 50, keepalive=True)
        r.job_result(job)
        self.assertEqual(r.eng.batch.inspected, 1)
        self.assertEqual(r.eng.state, "running")

    def test_result_without_ack_proves_acceptance(self):
        r = Rig()
        r.configure()
        r.start()
        r.answer_start()
        r.a_part(1)
        job = r.b_msgs("JOB_REQ")[-1]
        r.job_result(job)                                      # the ACK was lost
        self.assertEqual(r.eng.batch.inspected, 1)
        self.assertEqual(r.part_stages(1), ["produced", "queued", "delivered", "accepted", "completed"])

    def test_result_in_idle_for_unknown_job_gets_stale_ack_no_fault(self):
        r = Rig()
        r.connect()
        r.b_send({"type": "JOB_RESULT", "job_id": 5, "part_id": 1, "verdict": "PASS", "N": 5, "K": 2})
        self.assertEqual(r.b_msgs("ACK")[-1]["reason"], "stale")
        self.assertEqual((r.eng.state, r.ev("fault")), ("idle", []))

    def test_result_for_a_previous_connection_job_is_stale(self):
        r, job = self.running_with_job()
        r.eng.on_b_closed(r.now)                               # fault; the job is cancelled
        self.assertEqual(r.eng.state, "fault")
        r.cmd("fault/clear")
        r.hello()
        r.b_send({"type": "JOB_RESULT", "job_id": job["job_id"], "part_id": 1, "verdict": "PASS", "N": 5, "K": 2})
        self.assertEqual(r.eng.batch.inspected, 0)


class JobRejections(unittest.TestCase):
    def running_with_part(self):
        r = Rig()
        r.configure()
        r.start(target=5)
        r.answer_start()
        r.a_part(1)
        return r, r.b_msgs("JOB_REQ")[-1]

    def test_busy_same_job_id_offered_again_later(self):
        r, job = self.running_with_part()
        r.job_ack(job, accept=False, reason="busy")
        self.assertEqual(r.eng.state, "running")
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 1)          # not immediately
        r.run(510, keepalive=True)
        jobs = r.b_msgs("JOB_REQ")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[1]["job_id"], job["job_id"])
        self.assertEqual(jobs[1]["part_id"], 1)
        rej = r.ev("part_event", part_id=1, stage="rejected")
        self.assertEqual(len(rej), 1)
        self.assertEqual(rej[0]["reason"], "busy")

    def test_abandoned_and_id_conflict_need_a_new_job_id(self):
        for reason in ("abandoned", "id_conflict"):
            r, job = self.running_with_part()
            r.job_ack(job, accept=False, reason=reason)
            r.run(510, keepalive=True)
            jobs = r.b_msgs("JOB_REQ")
            self.assertEqual(len(jobs), 2, reason)
            self.assertNotEqual(jobs[1]["job_id"], job["job_id"], reason)

    def test_repeated_rejection_is_a_fault(self):
        r, job = self.running_with_part()
        for _ in range(4):
            j = r.b_msgs("JOB_REQ")[-1]
            r.job_ack(j, accept=False, reason="busy")
            r.run(510, keepalive=True)
        self.assertEqual(r.eng.state, "fault")
        self.assertEqual(r.ev("fault")[0]["code"], "PROTOCOL_ERROR")
        self.assertEqual(len(r.b_msgs("JOB_REQ")), 4)

    def test_rejected_part_goes_back_to_the_queue_head(self):
        r, job = self.running_with_part()
        r.a_part(2)
        r.job_ack(job, accept=False, reason="busy")
        r.run(510, keepalive=True)
        self.assertEqual(r.b_msgs("JOB_REQ")[-1]["part_id"], 1)

    def test_ack_for_other_job_ignored(self):
        r, job = self.running_with_part()
        r.b_send({"type": "ACK", "ref": "JOB_REQ", "job_id": job["job_id"] + 5, "status": "accepted"})
        self.assertEqual(r.part_stages(1).count("accepted"), 0)


class DeviceInput(unittest.TestCase):
    def test_garbage_from_a_is_discarded(self):
        r = Rig()
        r.eng.on_a_frame(b"\x01\x02\x03", r.now)
        r.eng.on_a_frame(b"x" * 30, r.now)
        self.assertEqual(r.eng.stats["a_bad_frames"], 2)
        self.assertFalse(r.eng.a.connected)                    # bad frames are not a sign of life

    def test_other_test_id_is_not_production(self):
        r = Rig()
        frame = sp.atr_build(5, 0, b"\x00" * 16)
        r.eng.on_a_frame(frame, r.now)
        self.assertFalse(r.eng.a.connected)
        self.assertEqual(r.eng.stats["a_other_test_id"], 1)

    def test_wrong_payload_length_ignored(self):
        r = Rig()
        r.eng.on_a_frame(sp.atr_build(0, 0, b"\x06" * 10), r.now)
        self.assertFalse(r.eng.a.connected)

    def test_garbage_from_b_is_ignored(self):
        r = Rig()
        for body in (b"", b"not json", b"[1]", b'{"x":1}', b'{"type":1}', b"\xff"):
            r.eng.on_b_message(body, r.now)
        self.assertFalse(r.eng.b.connected)
        self.assertEqual(r.eng.stats["b_malformed"], 6)

    def test_b_messages_before_hello_ignored(self):
        r = Rig()
        r.b_hb()
        self.assertFalse(r.eng.b.connected)

    def test_hello_from_wrong_device_closes(self):
        r = Rig()
        r.b_send({"type": "HELLO", "device": "esp32_x", "fw": "1"})
        self.assertEqual(r.b_closed, 1)
        self.assertFalse(r.eng.b.connected)

    def test_unknown_b_type_ignored(self):
        r = Rig()
        r.connect()
        r.b_send({"type": "WHATEVER"})
        self.assertEqual(r.eng.stats["b_unexpected_type"], 1)

    def test_stale_acks_ignored(self):
        r = Rig()
        r.connect()
        r.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": 5, "status": "accepted"})
        r.b_send({"type": "ACK", "ref": "JOB_REQ", "job_id": 5, "status": "accepted"})
        r.a_send(sp.ProdMsg(sp.PM_CMD_ACK, 5, sp.CMDACK_ACCEPTED))
        r.a_send(sp.ProdMsg(sp.PM_CMD_DONE, 5, sp.DONE_RUNNING))
        self.assertEqual(r.eng.stats["stale_acks"], 4)
        self.assertEqual(r.eng.state, "idle")

    def test_cmd_done_with_unexpected_state_is_protocol_error(self):
        r = Rig()
        r.configure()
        r.start(cmd_id=17)
        r.a_send(sp.ProdMsg(sp.PM_CMD_ACK, 17, sp.CMDACK_ACCEPTED))
        r.a_send(sp.ProdMsg(sp.PM_CMD_DONE, 17, sp.DONE_PAUSED))
        self.assertEqual(r.eng.state, "fault")
        self.assertEqual(r.ev("fault")[0]["code"], "PROTOCOL_ERROR")

    def test_a_cmd_done_alone_implies_accepted(self):
        r = Rig()
        r.configure()
        r.cmd("batch/pause") if False else None
        r.start(cmd_id=17)
        cfg = r.b_msgs("CONFIG")[-1]
        r.b_send({"type": "ACK", "ref": "CONFIG", "cmd_id": cfg["cmd_id"], "status": "accepted"})
        r.b_send({"type": "CONFIG_DONE", "cmd_id": cfg["cmd_id"], "N": 5, "K": 2, "inspect_time_ms": 500})
        r.a_send(sp.ProdMsg(sp.PM_CMD_DONE, 17, sp.DONE_RUNNING))   # the CMD_ACK was lost
        self.assertEqual([s for s, _ in r.stages(17)], ["delivered", "accepted", "completed"])

    def test_second_hello_drops_the_connection(self):
        r = Rig()
        r.connect()
        r.hello()
        self.assertEqual(r.b_closed, 1)
        self.assertFalse(r.eng.b.connected)

    def test_a_seq_jump_is_noted_not_fatal(self):
        r = Rig()
        r.a_send(sp.ProdMsg(sp.PM_HEARTBEAT, 0), seq=0)
        r.a_send(sp.ProdMsg(sp.PM_HEARTBEAT, 1), seq=5)
        self.assertEqual(r.eng.stats["a_seq_jumps"], 1)
        self.assertTrue(r.eng.a.connected)


class StatusView(unittest.TestCase):
    def test_shape(self):
        r = Rig()
        s = r.eng.status(r.now)
        self.assertEqual(set(s), {"v", "type", "ts", "state", "mode", "batch", "queue", "params", "devices"})
        self.assertEqual(s["queue"], {"depth": 0, "capacity": 5, "blocked": False})
        self.assertEqual(s["params"], {"cycle_time_ms": 1000, "inspect_time_ms": 500, "N": None, "K": None})
        self.assertEqual(s["devices"], {"esp32_a": {"connected": False, "heartbeat_age_ms": None},
                                        "esp32_b": {"connected": False, "heartbeat_age_ms": None}})
        self.assertEqual((s["state"], s["mode"]), ("idle", "production"))

    def test_heartbeat_age(self):
        r = Rig()
        r.connect()
        r.now += 420
        s = r.eng.status(r.now)
        self.assertEqual(s["devices"]["esp32_a"]["heartbeat_age_ms"], 420)
        self.assertTrue(s["devices"]["esp32_a"]["connected"])

    def test_dirty_flag(self):
        r = Rig()
        r.eng.take_dirty()
        self.assertFalse(r.eng.take_dirty())
        r.cmd("mode", mode="benchmark")
        self.assertTrue(r.eng.take_dirty())
        self.assertFalse(r.eng.take_dirty())


if __name__ == "__main__":
    unittest.main()
