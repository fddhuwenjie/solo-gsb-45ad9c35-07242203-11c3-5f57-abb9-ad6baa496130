# -*- coding: utf-8 -*-
"""航线自动保存（PUT /api/state/route）与双页面交错回归测试。

对应页面行为：已有发布时，航线编辑自动保存只落库航线（不带禁飞区），
并携带读取时的工作区修订号做乐观并发；提交禁飞区核准不再预先保存
工作区，直接携带所见修订号/发布号。

覆盖：
- 仅保存航线：禁飞区（含已核准内容）不被航线保存改写，修订号 +1；
- baseRev 乐观并发：他页先改工作区 → 409 stale 附明细，工作区不变
  （不静默掩盖跨页面修订变化）；
- 字段校验：缺 route / baseRev 或类型错误 → 400 missing_fields；
- 提交核准无需预先保存工作区：仅改禁飞区后直接提交，失败（冲突/号已变）
  时修订号不变、工作区与发布快照不变；草稿修改后用同一修订号重新提交可通过；
- 双页面交错：A 页存航线、B 页核准禁飞区，交错的保存与核准按号协调——
  旧号一律 409，重新取号后放行，已核准禁飞区与最新航线都保留。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

# 干净航线：4km 直线（y=100），200m 等高，08:00 起飞
ROUTE = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [25],
    "waypoints": [
        {"id": "w1", "name": "A", "x": 100, "y": 100, "alt": 200},
        {"id": "w2", "name": "B", "x": 500, "y": 100, "alt": 200},
    ],
}
# 覆盖发布航线中段、时窗覆盖全程的禁飞区（与 ROUTE 必冲突）
BLOCK_NFZ = {
    "id": "nz1", "name": "阻断区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T07:00", "endTime": "2026-09-24T09:00",
}
# 远离发布航线的合法禁飞区（与 ROUTE 不冲突）
FAR_NFZ = {
    "id": "nzFar", "name": "远侧限制区", "enabled": True,
    "vertices": [[100, 500], [200, 500], [200, 600], [100, 600]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "", "endTime": "",
}
FAR_NFZ2 = {
    "id": "nzFar2", "name": "远侧限制区-2", "enabled": True,
    "vertices": [[300, 500], [400, 500], [400, 600], [300, 600]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "", "endTime": "",
}


def clone(obj):
    return json.loads(json.dumps(obj))


def moved_route(y):
    """把 ROUTE 平移到指定 y 的变体航线。"""
    r = clone(ROUTE)
    for wp in r["waypoints"]:
        wp["y"] = y
    return r


class RouteSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        self.client = app.app.test_client()

    def tearDown(self):
        app.DB_PATH = self.old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # —— 辅助 ——
    def put_state(self, state):
        r = self.client.put("/api/state", json=state)
        self.assertEqual(r.status_code, 200)
        return r.get_json()["rev"]

    def save_route(self, route, base_rev):
        return self.client.put("/api/state/route",
                               json={"route": route, "baseRev": base_rev})

    def save_version(self, state, name="v1"):
        r = self.client.post("/api/versions", json={"name": name, "state": state})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def publish(self, vid, rev, seq):
        return self.client.post("/api/release", json={
            "versionId": vid, "workspaceRev": rev, "releaseSeq": seq})

    def approve(self, nfzs, rev, seq):
        return self.client.post("/api/nfz-approval", json={
            "nfzs": nfzs, "workspaceRev": rev, "releaseSeq": seq})

    def ws_state(self):
        """当前工作区状态与修订号（响应头）。"""
        r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        return r.get_json(), int(r.headers["X-Workspace-Rev"])

    def current_release(self):
        return self.client.get("/api/release").get_json()

    def publish_route(self):
        """发布 ROUTE（工作区禁飞区为空），返回 (versionId, 当前修订号)。"""
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": []})
        vid = self.save_version({"route": ROUTE, "nfzs": []})
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 200, r.get_json())
        return vid, rev

    # —— 仅保存航线 ——
    def test_route_save_keeps_nfzs_and_bumps_rev(self):
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": [clone(FAR_NFZ)]})
        r = self.save_route(clone(ROUTE), rev)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["rev"], rev + 1)
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev + 1)
        self.assertEqual(state["route"]["waypoints"], ROUTE["waypoints"])
        self.assertEqual(state["nfzs"], [FAR_NFZ])  # 禁飞区原样保留

    def test_route_save_missing_fields(self):
        rev = self.put_state({"route": {}, "nfzs": []})
        for body in ({}, {"route": {}}, {"baseRev": rev},
                     {"route": "not-a-dict", "baseRev": rev},
                     {"route": {}, "baseRev": "not-an-int"}):
            r = self.client.put("/api/state/route", json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertEqual(r.get_json()["reason"], "missing_fields")
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)  # 修订号不变

    def test_route_save_stale_rejected_and_not_overwritten(self):
        # 页面 A、B 同读修订号 rev；B 先改工作区，A 的保存须被拒绝而非静默覆盖
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": [clone(FAR_NFZ)]})
        rev_b = self.put_state({"route": moved_route(300), "nfzs": [clone(FAR_NFZ2)]})
        self.assertEqual(rev_b, rev + 1)

        r = self.save_route(moved_route(550), rev)  # A 持旧号保存
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["workspaceRev"],
                         {"expected": rev, "actual": rev_b})
        # 工作区保持 B 的内容，未被 A 的缓存覆盖
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev_b)
        self.assertEqual(state["route"]["waypoints"][0]["y"], 300)
        self.assertEqual(state["nfzs"], [FAR_NFZ2])

    # —— 与核准的交错（双页面） ——
    def test_route_save_after_approval_keeps_approved_nfzs(self):
        _, rev = self.publish_route()
        # B 页核准新禁飞区（工作区修订号随之 +1）
        r = self.approve([clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev2 = r.get_json()["rev"]

        # A 页用核准后的新号保存航线：只替换航线，已核准禁飞区不动
        r = self.save_route(moved_route(550), rev2)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev2 + 1)
        self.assertEqual(state["route"]["waypoints"][0]["y"], 550)
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        # 发布快照不受航线保存影响
        self.assertEqual(self.current_release()["release"]["state"]["nfzs"], [])

    def test_route_save_with_pre_approval_rev_rejected(self):
        _, rev = self.publish_route()
        # B 页核准禁飞区使修订号 +1；A 页仍持核准前的号保存航线 → 拒绝
        r = self.approve([clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.save_route(moved_route(550), rev)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "stale")
        # 已核准禁飞区与工作区航线都未被 A 的缓存覆盖
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"], {"waypoints": []})

    def test_approval_needs_no_prior_workspace_save(self):
        """页面仅改禁飞区后直接提交核准：无需先 PUT 工作区，
        失败不增修订号，草稿修改后用同一修订号重新提交可通过。"""
        _, rev = self.publish_route()
        rel_before = self.current_release()["release"]

        # 直接提交冲突候选（中间没有任何 PUT /api/state）
        r = self.approve([clone(BLOCK_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)          # 修订号未因失败提交增加
        self.assertEqual(state["nfzs"], [])     # 工作区不变
        self.assertEqual(self.current_release()["release"], rel_before)  # 发布不变

        # 草稿修改为不冲突候选，用同一修订号重新提交 → 通过
        r = self.approve([clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["rev"], rev + 1)
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [FAR_NFZ])

    def test_dual_page_interleaved_save_and_approval(self):
        """A 页编辑航线、B 页核准禁飞区，交错操作按修订号协调。"""
        _, rev = self.publish_route()
        # 两页同读：工作区修订 rev、发布号 1

        # ① B 页核准禁飞区 → 工作区修订 +1
        r = self.approve([clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev_b = r.get_json()["rev"]

        # ② A 页持旧号保存航线 → 409，不静默掩盖 B 的核准
        r = self.save_route(moved_route(550), rev)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["details"]["workspaceRev"]["actual"], rev_b)

        # ③ A 页重新取号（页面重新载入）后保存 → 通过，已核准禁飞区保留
        _, rev_a = self.ws_state()
        r = self.save_route(moved_route(550), rev_a)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev_a2 = r.get_json()["rev"]
        state, _ = self.ws_state()
        self.assertEqual(state["route"]["waypoints"][0]["y"], 550)
        self.assertEqual(state["nfzs"], [FAR_NFZ])

        # ④ B 页持旧号再次核准 → 409；重新取号后提交 → 通过
        r = self.approve([clone(FAR_NFZ2)], rev_b, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["details"]["workspaceRev"],
                         {"expected": rev_b, "actual": rev_a2})
        r = self.approve([clone(FAR_NFZ2)], rev_a2, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [FAR_NFZ2])          # 最新核准生效
        self.assertEqual(state["route"]["waypoints"][0]["y"], 550)  # A 的航线保留
        # 发布快照自始至终不变
        rel = self.current_release()["release"]
        self.assertEqual(rel["seq"], 1)
        self.assertEqual(rel["state"]["nfzs"], [])
        self.assertEqual(rel["state"]["route"]["waypoints"], ROUTE["waypoints"])

    def test_adjacent_save_and_approval_ordering(self):
        """保存与核准相邻时按完成顺序衔接修订号：先存后核、先核后存都可行。"""
        _, rev = self.publish_route()
        # 先存航线（rev → rev+1），再用保存产生的号核准（rev+1 → rev+2）
        r = self.save_route(moved_route(550), rev)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev2 = r.get_json()["rev"]
        r = self.approve([clone(FAR_NFZ)], rev2, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev3 = r.get_json()["rev"]
        self.assertEqual(rev3, rev2 + 1)
        # 再存航线（rev+2 → rev+3）：已核准禁飞区不动
        r = self.save_route(moved_route(600), rev3)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev3 + 1)
        self.assertEqual(state["route"]["waypoints"][0]["y"], 600)
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(self.current_release()["releaseSeq"], 1)

    # —— 未发布时的整体自动保存不回归 ——
    def test_whole_state_put_unchanged(self):
        rev = self.put_state({"route": clone(ROUTE), "nfzs": [clone(FAR_NFZ)]})
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["route"]["waypoints"], ROUTE["waypoints"])
        self.assertEqual(state["nfzs"], [FAR_NFZ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
