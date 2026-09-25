import tempfile
import unittest
from collections import Counter
from pathlib import Path

from app import BusinessError, RandomizationStore


class RandomizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "多中心降压研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-001"
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_stratified_block_randomization_and_two_person_unblinding(self):
        participants = [
            self.store.enroll("site1", self.trial["id"], f"S001-{i:03d}", {"risk": "low"})
            for i in range(1, 5)
        ]
        self.assertNotIn("arm", participants[0])
        with self.store.connect() as conn:
            arms = [r["arm"] for r in conn.execute(
                "SELECT a.arm FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.trial_id=? ORDER BY p.id",
                (self.trial["id"],),
            ).fetchall()]
        self.assertEqual(Counter(arms), Counter({"A": 2, "B": 2}))
        request = self.store.request_unblinding("site1", participants[0]["id"], "受试者发生严重不良事件需要紧急处理")
        first = self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(ctx.exception.code, "distinct_approver_required")
        second = self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(second["status"], "approved")
        self.assertIn(second["arm"], {"A", "B"})

    def test_idempotent_enrollment_site_isolation_and_protocol_lock(self):
        first = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        again = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["idempotent"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_participant("site2", first["id"])
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_protocol("coord", self.trial["id"], "v2")
        self.assertEqual(ctx.exception.code, "protocol_locked")


    def test_replacement_approve_issues_same_stratum_code_without_unblinding(self):
        p1 = self.store.enroll("site1", self.trial["id"], "S001-101", {"risk": "low"})
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_replacement("site1", p1["id"], "标签坏了")
        self.assertEqual(ctx.exception.code, "reason_required")
        req = self.store.request_replacement("site1", p1["id"], "随机号标签用药前污损无法辨认")
        self.assertEqual(req["status"], "pending")
        self.assertEqual(req["old_code"], p1["allocation_code"])
        self.assertNotIn("arm", req)
        self.assertTrue(self.store.get_participant("site1", p1["id"])["allocation_frozen"])
        # 冻结期间不能重复发起
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_replacement("site1", p1["id"], "重复发起替换申请测试")
        self.assertEqual(ctx.exception.code, "request_exists")
        # 中心角色不能复核；发起人也不能复核自己的申请
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_replacement("site1", req["id"], "approve")
        self.assertEqual(ctx.exception.code, "forbidden")
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_replacement("monitor1", req["id"], "maybe")
        self.assertEqual(ctx.exception.code, "invalid_decision")
        done = self.store.review_replacement("monitor1", req["id"], "approve")
        self.assertEqual(done["status"], "approved")
        self.assertNotIn("arm", done)
        self.assertNotEqual(done["new_code"], done["old_code"])
        self.assertIn("同一分层", done["conclusion"])
        after = self.store.get_participant("site1", p1["id"])
        self.assertEqual(after["allocation_code"], done["new_code"])
        self.assertFalse(after["allocation_frozen"])
        self.assertNotIn("arm", after)
        with self.store.connect() as conn:
            old_alloc = conn.execute(
                "SELECT a.* FROM allocations a JOIN replacement_requests r ON r.old_allocation_id=a.id WHERE r.id=?",
                (req["id"],),
            ).fetchone()
            new_alloc = conn.execute(
                "SELECT a.* FROM allocations a JOIN replacement_requests r ON r.new_allocation_id=a.id WHERE r.id=?",
                (req["id"],),
            ).fetchone()
            part = conn.execute("SELECT * FROM participants WHERE id=?", (p1["id"],)).fetchone()
        # 新编号来自同一分层，受试者已指向新分配
        self.assertEqual(old_alloc["stratum_id"], new_alloc["stratum_id"])
        self.assertEqual(part["stratum_id"], new_alloc["stratum_id"])
        self.assertEqual(part["allocation_id"], new_alloc["id"])
        self.assertEqual(new_alloc["used_by"], p1["id"])
        # 后续入组不会再次拿到原编号
        for i in range(2, 9):
            self.store.enroll("site1", self.trial["id"], f"S001-1{i:02d}", {"risk": "low"})
        with self.store.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT used_by FROM allocations WHERE id=?", (old_alloc["id"],)).fetchone()[0],
                p1["id"],
            )
        # 已完成的申请不能再次复核
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_replacement("monitor2", req["id"], "reject")
        self.assertEqual(ctx.exception.code, "already_decided")
        # 处理记录可查，其他中心看不到
        records = self.store.list_replacements("monitor2", self.trial["id"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["external_id"], "S001-101")
        self.assertEqual(self.store.list_replacements("site2", self.trial["id"]), [])

    def test_replacement_reject_restores_original_and_distinct_reviewer(self):
        p = self.store.enroll("site1", self.trial["id"], "S001-201", {"risk": "high"})
        # 跨中心不能发起
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_replacement("site2", p["id"], "尝试替换其他中心受试者编号")
        self.assertEqual(ctx.exception.code, "site_isolation")
        # 监查员发起后，同一人不能复核，必须换另一名监查员
        req = self.store.request_replacement("monitor1", p["id"], "中心报告随机号标签在用药前破损")
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_replacement("monitor1", req["id"], "approve")
        self.assertEqual(ctx.exception.code, "distinct_reviewer_required")
        done = self.store.review_replacement("monitor2", req["id"], "reject")
        self.assertEqual(done["status"], "rejected")
        self.assertIsNone(done["new_code"])
        self.assertIn("恢复原编号", done["conclusion"])
        after = self.store.get_participant("site1", p["id"])
        self.assertEqual(after["allocation_code"], p["allocation_code"])
        self.assertFalse(after["allocation_frozen"])
        # 驳回后可重新发起；受试者个人记录可查
        self.store.request_replacement("site1", p["id"], "编号再次污损申请重新处理")
        history = self.store.list_participant_replacements("site1", p["id"])
        self.assertEqual([r["status"] for r in history], ["rejected", "pending"])


if __name__ == "__main__":
    unittest.main()
