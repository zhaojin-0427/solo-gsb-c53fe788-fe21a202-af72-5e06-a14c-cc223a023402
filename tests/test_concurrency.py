"""参数更新与字幕采纳交错并发的一致性回归测试。

场景：镜头 [0,1200) 与 [1200,2400) 相接于 1200ms 切点，字幕 900–1300ms，
容差 200ms 下提案为 800–1200ms。两条写路径（PUT settings 与 POST adopt）
共用 BEGIN IMMEDIATE 写锁串行化，用「带闸门的 build_analysis」确定性地
制造交错：

- 设置先提交：adopt 在写锁外等待，等到锁后读到新参数重算提案，
  旧参数下的提案 800–1200 在容差 50ms 下失效 → 采纳被拒绝（400），
  库中不留无效采纳；
- 采纳先提交：settings 在写锁外等待，等到锁后重读能看到该采纳，
  同一事务内按新参数重核验并撤销，详情与 JSON/SRT/ZIP 导出均不呈现
  失效采纳；
- 设置先提交但提案在新参数下仍有效（容差 150ms）：adopt 等待后按
  新参数校验通过，合法采纳正常落库。
"""
import asyncio
import io
import json
import os
import sys
import threading
import time
import zipfile

DB = "/tmp/test_concurrency.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_PATH"] = DB  # 须在导入 app 之前设置

sys.path.insert(0, "/workspace")

from fastapi import HTTPException, UploadFile  # noqa: E402

from app import database as db  # noqa: E402
from app import main as m  # noqa: E402

db.init_db()

SRT = "1\n00:00:00,900 --> 00:00:01,300\n跨镜头字幕\n"
SHOTS = json.dumps([{"start_ms": 0, "end_ms": 1200},
                    {"start_ms": 1200, "end_ms": 2400}])


def _upload(name: str, content: str) -> UploadFile:
    return UploadFile(io.BytesIO(content.encode("utf-8")), filename=name)


def _create(name):
    return asyncio.run(m.create_project(
        name=name, cps_limit=21.0, min_gap_ms=40, tolerance_ms=200,
        subtitle=_upload("a.srt", SRT),
        shots_file=_upload("s.json", SHOTS),
    ))


class _Gate:
    """把首次 build_analysis 调用挡在持锁事务内，让另一线程排到锁外。

    entered：首个调用已进入（此时调用方持有写锁）；release：放行。
    只拦截第一次调用，其余调用直达真实求解器。
    """

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.armed = True

    def install(self):
        real = m.build_analysis
        gate = self

        def gated(*a, **k):
            if gate.armed:
                gate.armed = False
                gate.entered.set()
                assert gate.release.wait(timeout=15), "闸门等待超时"
            return real(*a, **k)

        m.build_analysis = gated
        return real

    @staticmethod
    def uninstall(real):
        m.build_analysis = real


def _run_thread(fn):
    """在线程中运行 fn，返回 (结果, 异常) 容器与线程句柄。"""
    box = {"result": None, "error": None}

    def runner():
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - 测试需捕获 HTTPException
            box["error"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return box, t


def _adopt_800_1200(pid, uid):
    return m.adopt(pid, m.AdoptIn(cue_uid=uid, new_start_ms=800,
                                  new_end_ms=1200))


def _settings_tol(pid, tol):
    return m.update_settings(pid, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=40, tolerance_ms=tol))


def _export_json(pid):
    return json.loads(m.export(pid, fmt="json").body.decode("utf-8"))


def _revoked_and_no_proposal(cue) -> bool:
    return (cue["adopted"] is False and cue["adopted_start_ms"] is None
            and cue["proposal_start_ms"] is None)


def test_settings_commits_first_stale_proposal_rejected():
    """设置先提交（容差 200→50）：并发的旧提案采纳等待写锁后被拒绝。"""
    proj = _create("并发-设置先提交")
    pid = proj["id"]
    uid = proj["analysis"]["cues"][0]["uid"]
    assert proj["analysis"]["cues"][0]["proposal_start_ms"] == 800

    gate = _Gate()
    real = gate.install()
    try:
        # 设置线程先持写锁，在首次 build_analysis（新参数求解）处被挡住
        sbox, st = _run_thread(lambda: _settings_tol(pid, 50))
        assert gate.entered.wait(timeout=10), "设置线程未进入求解"
        # 采纳线程此时只能排在写锁外等待
        abox, at = _run_thread(lambda: _adopt_800_1200(pid, uid))
        time.sleep(0.5)  # 确认采纳线程已阻塞在 BEGIN IMMEDIATE
        assert at.is_alive(), "采纳线程应阻塞在写锁等待上"
        gate.release.set()
        st.join(timeout=15)
        at.join(timeout=15)
    finally:
        _Gate.uninstall(real)

    assert sbox["error"] is None, sbox["error"]
    assert sbox["result"]["tolerance_ms"] == 50
    # 采纳必须被拒绝：容差 50ms 下 800–1200 不是有效提案
    assert isinstance(abox["error"], HTTPException), abox["error"]
    assert abox["error"].status_code == 400

    # 库中不得留下无效采纳；详情与导出一致
    detail = m.get_project(pid)
    cue = detail["analysis"]["cues"][0]
    assert detail["tolerance_ms"] == 50
    assert cue["adopted"] is False and cue["adopted_start_ms"] is None
    doc = _export_json(pid)
    assert doc["cues"][0]["corrected"] is None
    assert doc["cues"][0]["status"] == "conflict"
    srt = m.export(pid, fmt="srt").body.decode("utf-8")
    assert "00:00:00,900 --> 00:00:01,300" in srt
    assert "00:00:00,800" not in srt
    print("settings-first: stale proposal rejected, no invalid adoption ok")


def test_adopt_commits_first_settings_revokes():
    """采纳先提交：设置请求等待写锁后重读，撤销新参数下失效的采纳。"""
    proj = _create("并发-采纳先提交")
    pid = proj["id"]
    uid = proj["analysis"]["cues"][0]["uid"]

    gate = _Gate()
    real = gate.install()
    try:
        # 采纳线程先持写锁，在提案校验的 build_analysis 处被挡住
        abox, at = _run_thread(lambda: _adopt_800_1200(pid, uid))
        assert gate.entered.wait(timeout=10), "采纳线程未进入求解"
        # 设置线程（容差收紧到 50ms）排在写锁外等待
        sbox, st = _run_thread(lambda: _settings_tol(pid, 50))
        time.sleep(0.5)
        assert st.is_alive(), "设置线程应阻塞在写锁等待上"
        gate.release.set()
        at.join(timeout=15)
        st.join(timeout=15)
    finally:
        _Gate.uninstall(real)

    # 采纳在旧参数（容差 200ms）下合法，先行提交成功
    assert abox["error"] is None, abox["error"]
    assert abox["result"]["analysis"]["cues"][0]["adopted"] is True

    # 设置请求等到锁后重读，必须看到并在同一事务内撤销该采纳
    assert sbox["error"] is None, sbox["error"]
    sres = sbox["result"]
    assert sres["tolerance_ms"] == 50
    cue = sres["analysis"]["cues"][0]
    assert _revoked_and_no_proposal(cue), cue

    detail = m.get_project(pid)
    dcue = detail["analysis"]["cues"][0]
    assert dcue["adopted"] is False and dcue["adopted_start_ms"] is None
    row = next(r for r in m.list_projects() if r["id"] == pid)
    assert row["adopted_count"] == 0

    # 三种导出均不呈现失效采纳
    doc = _export_json(pid)
    assert doc["cues"][0]["corrected"] is None
    assert doc["cues"][0]["status"] == "conflict"
    srt = m.export(pid, fmt="srt").body.decode("utf-8")
    assert "00:00:00,900 --> 00:00:01,300" in srt
    assert "00:00:00,800" not in srt
    zbody = m.export(pid, fmt="zip").body
    with zipfile.ZipFile(io.BytesIO(zbody)) as zf:
        zdoc = json.loads(zf.read("correction.json").decode("utf-8"))
        zsrt = zf.read("corrected.srt").decode("utf-8")
    assert zdoc["cues"][0]["corrected"] is None
    assert "00:00:00,800" not in zsrt
    print("adopt-first: settings re-read and revoked invalid adoption ok")


def test_settings_first_valid_proposal_still_adoptable():
    """设置先提交（容差 200→150）：提案 800–1200 仍有效，等待后的
    采纳按新参数校验通过并落库——合法采纳必须保留。"""
    proj = _create("并发-新参数下仍有效")
    pid = proj["id"]
    uid = proj["analysis"]["cues"][0]["uid"]

    gate = _Gate()
    real = gate.install()
    try:
        sbox, st = _run_thread(lambda: _settings_tol(pid, 150))
        assert gate.entered.wait(timeout=10)
        abox, at = _run_thread(lambda: _adopt_800_1200(pid, uid))
        time.sleep(0.5)
        assert at.is_alive()
        gate.release.set()
        st.join(timeout=15)
        at.join(timeout=15)
    finally:
        _Gate.uninstall(real)

    assert sbox["error"] is None, sbox["error"]
    assert sbox["result"]["tolerance_ms"] == 150
    # 新参数下提案仍是 800–1200（|平移量| 100 ≤ 150），采纳成功
    assert abox["error"] is None, abox["error"]
    cue = abox["result"]["analysis"]["cues"][0]
    assert cue["adopted"] is True and cue["adopted_start_ms"] == 800

    detail = m.get_project(pid)
    dcue = detail["analysis"]["cues"][0]
    assert dcue["adopted"] is True and dcue["adopted_start_ms"] == 800
    doc = _export_json(pid)
    assert doc["cues"][0]["corrected"] == {"start_ms": 800, "end_ms": 1200}
    assert doc["cues"][0]["status"] == "corrected"
    print("settings-first: still-valid adoption kept ok")


def main():
    test_settings_commits_first_stale_proposal_rejected()
    test_adopt_commits_first_settings_revokes()
    test_settings_first_valid_proposal_still_adoptable()
    print("concurrency settings/adopt regression ok")


if __name__ == "__main__":
    main()
