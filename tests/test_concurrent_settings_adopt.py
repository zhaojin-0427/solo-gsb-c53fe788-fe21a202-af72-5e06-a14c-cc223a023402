"""PUT settings 与 POST adopt 并发交错时的事务一致性回归。

数据：镜头 [0,1200)、[1200,2400)，cue1=100–500（镜头内），
cue2=900–1300（跨镜头），tol=200 时提案 cue1=100–500、cue2=800–1200，
两条间隔 300ms，互不影响。

两种交错顺序，最终状态必须一致且无失效采纳：

  场景 1（采纳先拿到写锁并先提交）：adopt cue2@800–1200 持锁未提交时
    发起 PUT tol=50；设置事务在 BEGIN IMMEDIATE 处等待，采纳提交后锁内
    重读，按新参数撤销 cue2（|平移|100>50），合法的 cue1 保留。

  场景 2（设置先拿到写锁并先提交）：PUT tol=50 持锁未提交时发起
    adopt cue2@800–1200；采纳事务在 BEGIN IMMEDIATE 处等待，设置提交后
    锁内按新参数重读——tol=50 下 cue2 无提案，旧提案值被拒绝（400），
    不得写入。

  场景 3（合法采纳在交错后保留）：采纳先提交、设置只把容差收到 150，
    cue2@800–1200（|平移|100≤150）仍合法，必须保留并出现在导出中。

旧实现的读/求解先于写事务：场景 1 设置事务按旧快照核验会漏撤销，
场景 2 采纳事务按旧参数校验后撞上写锁、锁释放后直接落库——
均会造成新参数与失效采纳并存。
"""
import io
import json
import os
import sys
import threading
import zipfile

DB = "/tmp/test_concurrent_settings_adopt.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_PATH"] = DB  # 须在导入 app 之前设置

sys.path.insert(0, "/workspace")

import asyncio  # noqa: E402

from fastapi import HTTPException, UploadFile  # noqa: E402

from app import database as db  # noqa: E402
from app import main as m  # noqa: E402

db.init_db()

SRT = ("1\n00:00:00,100 --> 00:00:00,500\n镜头内字幕\n\n"
       "2\n00:00:00,900 --> 00:00:01,300\n跨镜头字幕\n")
SHOTS = json.dumps([{"start_ms": 0, "end_ms": 1200},
                    {"start_ms": 1200, "end_ms": 2400}])


def _upload(name: str, content: str) -> UploadFile:
    return UploadFile(io.BytesIO(content.encode("utf-8")), filename=name)


def _create():
    return asyncio.run(m.create_project(
        name="并发场景", cps_limit=21.0, min_gap_ms=40, tolerance_ms=200,
        subtitle=_upload("a.srt", SRT),
        shots_file=_upload("s.json", SHOTS),
    ))


def _run(fn) -> tuple:
    box = {}

    def worker():
        try:
            box["result"] = fn()
        except BaseException as exc:  # 含 HTTPException
            box["error"] = exc

    th = threading.Thread(target=worker)
    th.start()
    return th, box


def _join(th) -> None:
    th.join(timeout=20)
    assert not th.is_alive(), "并发事务互相等待超时"


class _Gate:
    """被调函数执行后置 entered、放行前阻塞（持锁不提交）。"""

    def __init__(self, orig):
        self.orig = orig
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        out = self.orig(*args, **kwargs)
        self.entered.set()
        assert self.release.wait(timeout=10), "门控未放行"
        return out


def _export_json(pid):
    return json.loads(m.export(pid, fmt="json").body.decode("utf-8"))


def _assert_final_tol50(pid):
    """tol=50 下的唯一合法终态：cue1 采纳保留，cue2 失效、无提案。"""
    detail = m.get_project(pid)
    assert detail["tolerance_ms"] == 50
    cues = detail["analysis"]["cues"]
    assert cues[0]["adopted"] and cues[0]["adopted_start_ms"] == 100
    assert not cues[1]["adopted"]
    assert cues[1]["proposal_start_ms"] is None
    assert cues[1]["cross_shot"] is True

    row = next(r for r in m.list_projects() if r["id"] == pid)
    assert row["adopted_count"] == 1, row["adopted_count"]

    doc = _export_json(pid)
    assert doc["cues"][0]["corrected"] == {"start_ms": 100, "end_ms": 500}
    assert doc["cues"][1]["corrected"] is None
    assert doc["cues"][1]["status"] == "conflict"

    srt = m.export(pid, fmt="srt").body.decode("utf-8")
    assert "00:00:00,100 --> 00:00:00,500" in srt
    assert "00:00:00,900 --> 00:00:01,300" in srt
    assert "00:00:00,800" not in srt, "SRT 不得呈现失效采纳"

    zbody = m.export(pid, fmt="zip").body
    with zipfile.ZipFile(io.BytesIO(zbody)) as zf:
        zdoc = json.loads(zf.read("correction.json").decode("utf-8"))
        zsrt = zf.read("corrected.srt").decode("utf-8")
    assert zdoc["cues"][1]["corrected"] is None
    assert "00:00:00,800" not in zsrt, "ZIP 内 SRT 不得呈现失效采纳"


def test_adopt_commits_first_settings_revokes():
    proj = _create()
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    assert [(c["proposal_start_ms"], c["proposal_end_ms"]) for c in cues] == [
        (100, 500), (800, 1200)]
    # 预置合法采纳 cue1
    m.adopt(pid, m.AdoptIn(cue_uid=cues[0]["uid"],
                           new_start_ms=100, new_end_ms=500))

    gate = _Gate(m.db.upsert_adoption)
    m.db.upsert_adoption = gate
    try:
        # B：旧参数（tol=200）下采纳 cue2，写入后持锁等待
        th_b, box_b = _run(lambda: m.adopt(
            pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                           new_start_ms=800, new_end_ms=1200)))
        assert gate.entered.wait(timeout=10), "采纳事务未进入持锁阶段"

        # A：设置收紧到 50——BEGIN IMMEDIATE 必须等 B 提交
        th_a, box_a = _run(lambda: m.update_settings(
            pid, m.SettingsIn(cps_limit=21.0, min_gap_ms=40,
                              tolerance_ms=50)))
        threading.Event().wait(0.5)  # 确保 A 在写锁上排队
        gate.release.set()
        _join(th_b)
        _join(th_a)
    finally:
        m.db.upsert_adoption = gate.orig

    assert "error" not in box_b, box_b.get("error")
    assert "error" not in box_a, box_a.get("error")
    _assert_final_tol50(pid)
    print("scenario1 adopt-first: stale adoption revoked after wait ok")


def test_settings_commits_first_adopt_rejected():
    proj = _create()
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    m.adopt(pid, m.AdoptIn(cue_uid=cues[0]["uid"],
                           new_start_ms=100, new_end_ms=500))

    gate = _Gate(m.db.update_settings)
    m.db.update_settings = gate
    try:
        # A：设置收紧到 50，落参数后持锁等待（撤销尚未执行）
        th_a, box_a = _run(lambda: m.update_settings(
            pid, m.SettingsIn(cps_limit=21.0, min_gap_ms=40,
                              tolerance_ms=50)))
        assert gate.entered.wait(timeout=10), "设置事务未进入持锁阶段"

        # B：按旧提案采纳 cue2——BEGIN IMMEDIATE 必须等 A 提交
        th_b, box_b = _run(lambda: m.adopt(
            pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                           new_start_ms=800, new_end_ms=1200)))
        threading.Event().wait(0.5)  # 确保 B 在写锁上排队
        gate.release.set()
        _join(th_a)
        _join(th_b)
    finally:
        m.db.update_settings = gate.orig

    assert "error" not in box_a, box_a.get("error")
    # B 在 A 提交后按新参数重读：tol=50 无提案，旧提案被 400 拒绝
    err = box_b.get("error")
    assert isinstance(err, HTTPException) and err.status_code == 400, err
    _assert_final_tol50(pid)
    print("scenario2 settings-first: stale proposal rejected after wait ok")


def test_valid_adoption_survives_interleaving():
    proj = _create()
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    m.adopt(pid, m.AdoptIn(cue_uid=cues[0]["uid"],
                           new_start_ms=100, new_end_ms=500))

    gate = _Gate(m.db.upsert_adoption)
    m.db.upsert_adoption = gate
    try:
        # B：采纳 cue2@800–1200（tol=150 下仍合法）
        th_b, box_b = _run(lambda: m.adopt(
            pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                           new_start_ms=800, new_end_ms=1200)))
        assert gate.entered.wait(timeout=10)
        # A：容差只收到 150，与 B 交错
        th_a, box_a = _run(lambda: m.update_settings(
            pid, m.SettingsIn(cps_limit=21.0, min_gap_ms=40,
                              tolerance_ms=150)))
        threading.Event().wait(0.5)
        gate.release.set()
        _join(th_b)
        _join(th_a)
    finally:
        m.db.upsert_adoption = gate.orig

    assert "error" not in box_b, box_b.get("error")
    assert "error" not in box_a, box_a.get("error")

    detail = m.get_project(pid)
    assert detail["tolerance_ms"] == 150
    for c, start in zip(detail["analysis"]["cues"], (100, 800)):
        assert c["adopted"] and c["adopted_start_ms"] == start, c
    row = next(r for r in m.list_projects() if r["id"] == pid)
    assert row["adopted_count"] == 2

    doc = _export_json(pid)
    assert [c["corrected"] for c in doc["cues"]] == [
        {"start_ms": 100, "end_ms": 500},
        {"start_ms": 800, "end_ms": 1200}]
    srt = m.export(pid, fmt="srt").body.decode("utf-8")
    assert "00:00:00,800 --> 00:00:01,200" in srt
    zbody = m.export(pid, fmt="zip").body
    with zipfile.ZipFile(io.BytesIO(zbody)) as zf:
        assert zf.read("corrected.srt").decode("utf-8") == srt
    print("scenario3 valid adoption survives interleaving ok")


def main():
    test_adopt_commits_first_settings_revokes()
    test_settings_commits_first_adopt_rejected()
    test_valid_adoption_survives_interleaving()
    print("concurrent settings/adopt regression ok")


if __name__ == "__main__":
    main()
