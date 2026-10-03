"""参数变更后采纳状态重核验的端到端回归测试。

场景：镜头 [0,1200) 与 [1200,2400) 相接于 1200ms 切点，字幕 900–1300ms。
- 容差 200ms：采纳提案 800–1200ms；
- 容差改为 150ms：采纳仍满足约束，保持有效，导出继续包含 corrected；
- 重算被强制失败：参数与采纳状态整体回滚，不留半更新；
- 容差改为 50ms：同一事务内重核验 → 采纳撤销，详情显示冲突，
  JSON / SRT / ZIP 三种导出均不含旧 corrected；
- 最小间隔收紧时，间隔不足的采纳同样被撤销。
"""
import asyncio
import hashlib
import io
import json
import os
import sys
import zipfile

DB = "/tmp/test_settings_revalidate.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_PATH"] = DB  # 须在导入 app 之前设置

sys.path.insert(0, "/workspace")

from fastapi import UploadFile  # noqa: E402

from app import database as db  # noqa: E402
from app import main as m  # noqa: E402

db.init_db()

SRT = "1\n00:00:00,900 --> 00:00:01,300\n跨镜头字幕\n"
SHOTS = json.dumps([{"start_ms": 0, "end_ms": 1200},
                    {"start_ms": 1200, "end_ms": 2400}])


def _upload(name: str, content: str) -> UploadFile:
    return UploadFile(io.BytesIO(content.encode("utf-8")), filename=name)


def _create(name, srt, shots=None, cps=21.0, gap=40, tol=200):
    return asyncio.run(m.create_project(
        name=name, cps_limit=cps, min_gap_ms=gap, tolerance_ms=tol,
        subtitle=_upload("a.srt", srt),
        shots_file=_upload("s.json", shots) if shots else None,
    ))


def _settings(pid, cps=21.0, gap=40, tol=200):
    return m.update_settings(pid, m.SettingsIn(
        cps_limit=cps, min_gap_ms=gap, tolerance_ms=tol))


def _export_json(pid):
    resp = m.export(pid, fmt="json")
    return json.loads(resp.body.decode("utf-8"))


def _check_doc_hash(doc):
    """document_hash 必须等于去掉该字段后规范化 JSON 的 SHA-256。"""
    got = doc["document_hash"]
    doc2 = dict(doc, document_hash="")
    raw = json.dumps(doc2, ensure_ascii=False, indent=2, sort_keys=True)
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == got


def test_valid_adoption_survives_and_failure_rolls_back():
    proj = _create("主场景", SRT, SHOTS)
    pid = proj["id"]
    cue = proj["analysis"]["cues"][0]
    assert (cue["proposal_start_ms"], cue["proposal_end_ms"]) == (800, 1200)

    # 容差 200ms 下采纳 800–1200
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cue["uid"],
                                  new_start_ms=800, new_end_ms=1200))
    assert proj["analysis"]["cues"][0]["adopted"] is True

    # 容差收紧到 150ms：|平移量| = 100 ≤ 150，采纳保持有效
    proj = _settings(pid, tol=150)
    cue = proj["analysis"]["cues"][0]
    assert cue["adopted"] is True and cue["adopted_start_ms"] == 800
    doc = _export_json(pid)
    assert doc["cues"][0]["corrected"] == {"start_ms": 800, "end_ms": 1200}
    assert doc["cues"][0]["status"] == "corrected"
    _check_doc_hash(doc)

    # 强制重算失败：参数与采纳必须整体回滚，不留半更新
    orig = m.build_analysis
    def _boom(*a, **k):
        raise RuntimeError("forced recompute failure")
    m.build_analysis = _boom
    try:
        try:
            _settings(pid, tol=50)
            raise AssertionError("重算失败应抛错")
        except RuntimeError:
            pass
    finally:
        m.build_analysis = orig
    proj = m.get_project(pid)
    assert proj["tolerance_ms"] == 150, proj["tolerance_ms"]
    cue = proj["analysis"]["cues"][0]
    assert cue["adopted"] is True and cue["adopted_start_ms"] == 800
    print("valid adoption kept + atomic rollback ok")
    return pid


def test_tolerance_50_revokes_everywhere(pid):
    # 容差改为 50ms：|平移量| = 100 > 50，采纳必须在同一事务内被撤销
    proj = _settings(pid, tol=50)
    assert proj["tolerance_ms"] == 50
    cue = proj["analysis"]["cues"][0]
    assert cue["adopted"] is False and cue["adopted_start_ms"] is None
    assert cue["proposal_start_ms"] is None, "容差 50ms 下无可采纳提案"
    assert cue["cross_shot"] is True
    assert len(proj["analysis"]["chains"]) == 1, "应显示冲突链"
    assert len(proj["analysis"]["intrinsic"]) == 1

    # 详情接口一致
    detail = m.get_project(pid)
    dcue = detail["analysis"]["cues"][0]
    assert dcue["adopted"] is False and dcue["proposal_start_ms"] is None
    assert len(detail["analysis"]["chains"]) == 1

    # 项目列表的采纳计数一致
    row = next(r for r in m.list_projects() if r["id"] == pid)
    assert row["adopted_count"] == 0, row["adopted_count"]

    # JSON 导出：不含旧 corrected，状态为冲突
    doc = _export_json(pid)
    assert doc["cues"][0]["corrected"] is None
    assert doc["cues"][0]["status"] == "conflict"
    assert doc["cues"][0]["proposal"] is None
    _check_doc_hash(doc)

    # SRT 导出：回退原值 900–1300，不含旧采纳时间
    srt = m.export(pid, fmt="srt").body.decode("utf-8")
    assert "00:00:00,900 --> 00:00:01,300" in srt
    assert "00:00:00,800" not in srt

    # ZIP 导出：内含 JSON 与 SRT 同样不含旧 corrected
    zbody = m.export(pid, fmt="zip").body
    with zipfile.ZipFile(io.BytesIO(zbody)) as zf:
        zdoc = json.loads(zf.read("correction.json").decode("utf-8"))
        zsrt = zf.read("corrected.srt").decode("utf-8")
    assert zdoc["cues"][0]["corrected"] is None
    assert zdoc["cues"][0]["status"] == "conflict"
    _check_doc_hash(zdoc)
    assert "00:00:00,900 --> 00:00:01,300" in zsrt
    assert "00:00:00,800" not in zsrt
    print("tolerance 50ms: revoked in detail/list/exports ok")


def test_min_gap_tightening_revokes():
    """最小间隔收紧后，间隔不足的采纳被撤销。"""
    srt = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\n乙\n")
    proj = _create("间隔场景", srt, shots=None, gap=40, tol=200)
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    # 原间隔 0 < 40：求解器将乙平移到 1040–2040
    assert cues[0]["proposal_start_ms"] == 0
    assert cues[1]["proposal_start_ms"] == 1040
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                                  new_start_ms=1040, new_end_ms=2040))
    assert proj["analysis"]["cues"][1]["adopted"] is True

    # 最小间隔收紧到 100ms：采纳位置与原甲的间隔 40 < 100 → 撤销
    proj = _settings(pid, gap=100, tol=200)
    cue_b = proj["analysis"]["cues"][1]
    assert cue_b["adopted"] is False and cue_b["adopted_start_ms"] is None
    assert cue_b["proposal_start_ms"] == 1100, cue_b["proposal_start_ms"]
    doc = _export_json(pid)
    assert doc["cues"][1]["corrected"] is None
    assert doc["cues"][1]["status"] == "proposed"
    print("min_gap tightening revocation ok")


def test_first_adoption_survives_same_params_and_srt_exports_align():
    """0–1000 / 1000–2000 字幕（原间隔 0），min_gap=40、tol=200：

    提案为 0–1000 与 1040–2040。只采纳第一条后，用完全相同的参数再次
    PUT settings：第一条采纳必须保持有效（相邻未采纳者按新提案 1040
    计间隔，0–1000 与 1040 的间隔恰为 40ms；旧实现误按第二条原始起点
    1000 计间隔而错撤销）。独立 SRT 与 ZIP 内 SRT 共用同一有效时间轴：
    第一条采纳值、第二条提案值。
    """
    srt = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\n乙\n")
    proj = _create("同参重提", srt, shots=None, gap=40, tol=200)
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    assert [(c["proposal_start_ms"], c["proposal_end_ms"]) for c in cues] == [
        (0, 1000), (1040, 2040)]

    # 只采纳第一条（提案等于原值 0–1000）
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[0]["uid"],
                                  new_start_ms=0, new_end_ms=1000))
    assert proj["analysis"]["cues"][0]["adopted"] is True

    # 再次提交完全相同的参数：采纳不得被误撤销
    proj = _settings(pid, gap=40, tol=200)
    cues = proj["analysis"]["cues"]
    assert cues[0]["adopted"] is True, cues[0]
    assert cues[0]["adopted_start_ms"] == 0
    assert cues[1]["adopted"] is False
    assert cues[1]["proposal_start_ms"] == 1040

    # 项目列表采纳计数
    row = next(r for r in m.list_projects() if r["id"] == pid)
    assert row["adopted_count"] == 1

    # JSON：第一条 corrected，第二条 proposed（corrected=None）
    doc = _export_json(pid)
    assert doc["cues"][0]["corrected"] == {"start_ms": 0, "end_ms": 1000}
    assert doc["cues"][0]["status"] == "corrected"
    assert doc["cues"][1]["corrected"] is None
    assert doc["cues"][1]["status"] == "proposed"
    _check_doc_hash(doc)

    # 独立 SRT 与 ZIP 内 SRT 必须完全一致：甲=0–1000（采纳值），
    # 乙=1040–2040（未采纳回退提案，不是原值 1000–2000）
    srt_text = m.export(pid, fmt="srt").body.decode("utf-8")
    assert "00:00:00,000 --> 00:00:01,000" in srt_text
    assert "00:00:01,040 --> 00:00:02,040" in srt_text
    assert "00:00:01,000 --> 00:00:02,000" not in srt_text
    zbody = m.export(pid, fmt="zip").body
    with zipfile.ZipFile(io.BytesIO(zbody)) as zf:
        zsrt = zf.read("corrected.srt").decode("utf-8")
        zdoc = json.loads(zf.read("correction.json").decode("utf-8"))
    assert zsrt == srt_text, "独立 SRT 与 ZIP 内 SRT 必须共用有效时间轴"
    assert zdoc["cues"][0]["corrected"] == {"start_ms": 0, "end_ms": 1000}
    _check_doc_hash(zdoc)

    # 重算强制失败：参数与采纳整体回滚（这里参数本就相同，重点是采纳不丢）
    orig = m.build_analysis
    def _boom(*a, **k):
        raise RuntimeError("forced recompute failure")
    m.build_analysis = _boom
    try:
        try:
            _settings(pid, gap=40, tol=200)
            raise AssertionError("重算失败应抛错")
        except RuntimeError:
            pass
    finally:
        m.build_analysis = orig
    proj = m.get_project(pid)
    assert proj["analysis"]["cues"][0]["adopted"] is True
    assert proj["analysis"]["cues"][0]["adopted_start_ms"] == 0
    print("same-params PUT keeps first adoption + SRT alignment ok")


def main():
    pid = test_valid_adoption_survives_and_failure_rolls_back()
    test_tolerance_50_revokes_everywhere(pid)
    test_min_gap_tightening_revokes()
    test_first_adoption_survives_same_params_and_srt_exports_align()
    print("settings revalidate regression ok")


if __name__ == "__main__":
    main()
