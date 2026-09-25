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

    def _enroll_pair(self):
        """同一分层入组两名受试者，返回 (participant1, participant2)。"""
        p1 = self.store.enroll("site1", self.trial["id"], "S001-101", {"risk": "low"})
        p2 = self.store.enroll("site1", self.trial["id"], "S001-102", {"risk": "low"})
        return p1, p2

    def test_replacement_approved_freezes_voids_old_and_issues_same_stratum_code(self):
        p1, p2 = self._enroll_pair()
        old_code = p1["allocation_code"]
        req = self.store.request_replacement("site1", p1["id"], "药物包装随机编号标签在拆箱时撕毁无法辨认")
        self.assertEqual(req["status"], "pending")
        self.assertEqual(req["old_allocation_code"], old_code)
        self.assertNotIn("arm", req)
        with self.store.connect() as conn:
            old_id = conn.execute("SELECT allocation_id FROM participants WHERE id=?", (p1["id"],)).fetchone()[0]
            self.assertEqual(
                conn.execute("SELECT status FROM allocations WHERE id=?", (old_id,)).fetchone()[0], "frozen"
            )
        # 冻结期间重复发起被拦截
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_replacement("site1", p1["id"], "重复发起补发申请的原因说明")
        self.assertEqual(ctx.exception.code, "replacement_pending")
        result = self.store.review_replacement("monitor1", req["id"], "approved", "情况属实，同意补发")
        self.assertEqual(result["status"], "approved")
        self.assertIsNotNone(result["new_allocation_code"])
        self.assertNotEqual(result["new_allocation_code"], old_code)
        self.assertNotIn("arm", result)
        with self.store.connect() as conn:
            # 受试者已指向新编号
            row = conn.execute("SELECT * FROM participants WHERE id=?", (p1["id"],)).fetchone()
            self.assertEqual(row["allocation_code"], result["new_allocation_code"])
            new_id = row["allocation_id"]
            old_row = conn.execute("SELECT * FROM allocations WHERE id=?", (old_id,)).fetchone()
            new_row = conn.execute("SELECT * FROM allocations WHERE id=?", (new_id,)).fetchone()
            self.assertEqual(old_row["status"], "void")          # 原编号作废、不再使用
            self.assertEqual(new_row["status"], "used")
            self.assertEqual(new_row["stratum_id"], old_row["stratum_id"])  # 必须同一分层
            # 原编号不应再出现于可用池
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM allocations WHERE id=? AND status='available'", (old_id,)).fetchone())
        # 受试者视图仍不暴露分组，并显示新编号
        view = self.store.get_participant("site1", p1["id"])
        self.assertEqual(view["allocation_code"], result["new_allocation_code"])
        self.assertNotIn("arm", view)
        # 记录可查：受试者、原编号、新编号、结论齐全
        got = self.store.get_replacement("monitor2", req["id"])
        self.assertEqual(got["external_id"], "S001-101")
        self.assertEqual(got["old_allocation_code"], old_code)
        self.assertEqual(got["new_allocation_code"], result["new_allocation_code"])
        self.assertEqual(got["conclusion"], "复核通过，已补发")

    def test_replacement_rejected_restores_original_code(self):
        p1, _ = self._enroll_pair()
        old_code = p1["allocation_code"]
        req = self.store.request_replacement("site1", p1["id"], "声称标签污损但现场可拍照辨认")
        result = self.store.review_replacement("monitor2", req["id"], "rejected", "编号仍可辨认，驳回")
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["new_allocation_code"])
        with self.store.connect() as conn:
            alloc_id = conn.execute("SELECT allocation_id FROM participants WHERE id=?", (p1["id"],)).fetchone()[0]
            self.assertEqual(
                conn.execute("SELECT status FROM allocations WHERE id=?", (alloc_id,)).fetchone()[0], "used")
            self.assertEqual(
                conn.execute("SELECT allocation_code FROM participants WHERE id=?", (p1["id"],)).fetchone()[0],
                old_code)
        self.assertEqual(self.store.get_participant("site1", p1["id"])["allocation_code"], old_code)
        # 恢复后可以重新发起
        again = self.store.request_replacement("site1", p1["id"], "标签随后彻底损毁无法再辨认")
        self.assertEqual(again["status"], "pending")

    def test_replacement_review_requires_monitor_and_site_isolation(self):
        p1, _ = self._enroll_pair()
        req = self.store.request_replacement("monitor1", p1["id"], "现场监查时确认随机编号标签浸水损坏")
        # 发起人本人不能复核自己发起的申请，必须另一名监查员
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_replacement("monitor1", req["id"], "approved")
        self.assertEqual(ctx.exception.code, "distinct_reviewer_required")
        # 中心人员不能复核
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_replacement("site2", req["id"], "approved")
        self.assertEqual(ctx.exception.code, "forbidden")
        self.store.review_replacement("monitor2", req["id"], "approved")
        # 其他中心看不到处理记录
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_replacement("site2", req["id"])
        self.assertEqual(ctx.exception.code, "site_isolation")
        # 其他中心列表也为空
        self.assertEqual(self.store.list_replacements("site2", self.trial["id"]), [])
        # 本中心及监查员可见
        self.assertEqual(len(self.store.list_replacements("site1", self.trial["id"])), 1)
        self.assertEqual(len(self.store.list_replacements("monitor1", self.trial["id"])), 1)

    def test_replacement_does_not_reuse_voided_code_for_new_enrollment(self):
        p1, _ = self._enroll_pair()
        req = self.store.request_replacement("site1", p1["id"], "随机编号标签损坏需要换号处理")
        done = self.store.review_replacement("monitor1", req["id"], "approved")
        p3 = self.store.enroll("site1", self.trial["id"], "S001-103", {"risk": "low"})
        self.assertNotIn(p3["allocation_code"], (done["old_allocation_code"], done["new_allocation_code"]))
        with self.store.connect() as conn:
            voided = conn.execute("SELECT id FROM allocations WHERE status='void'").fetchall()
            self.assertEqual(len(voided), 1)
            # 作废编号未被新受试者占用
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM participants WHERE allocation_id=?", (voided[0]["id"],)).fetchone())


if __name__ == "__main__":
    unittest.main()
