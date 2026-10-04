"""FastAPI 入口：项目导入、分析、采纳、导出。"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import database as db
from .analysis import build_analysis, revalidate_adoptions
from .shots import Shot, parse_shots
from .subtitles import ParsedCue, parse_subtitle, visible_text

app = FastAPI(title="字幕节奏与镜头边界校正台", version="1.0")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------- 数据模型 ----------

class SettingsIn(BaseModel):
    cps_limit: float = 21.0
    min_gap_ms: int = 40
    tolerance_ms: int = 200


class AdoptIn(BaseModel):
    cue_uid: int
    new_start_ms: int
    new_end_ms: int


# ---------- 序列化 ----------

def _cue_row(row) -> dict:
    return {
        "uid": row["id"],
        "cue_id": row["cue_id"],
        "ord": row["ord"],
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "text": row["text"],
        "graphemes": row["graphemes"],
        "raw_hash": row["raw_hash"],
        "adopted_start_ms": row["new_start_ms"],
        "adopted_end_ms": row["new_end_ms"],
        "adopted": row["new_start_ms"] is not None,
    }


def _load_inputs(conn, pid: int) -> tuple:
    """读取库中项目、字幕与镜头，构造 build_analysis 的输入。"""
    prow = db.get_project(conn, pid)
    if prow is None:
        raise HTTPException(404, "项目不存在")
    cue_db_rows = list(db.get_cues(conn, pid))
    shot_rows = list(db.get_shots(conn, pid))
    parsed = [
        ParsedCue(
            cue_id=r["cue_id"], ord=r["ord"],
            start_ms=r["start_ms"], end_ms=r["end_ms"],
            raw_text=r["text"], graphemes=r["graphemes"],
            raw_hash=r["raw_hash"],
        ) for r in cue_db_rows
    ]
    shots = [Shot(ord=s["ord"], start_ms=s["start_ms"], end_ms=s["end_ms"])
             for s in shot_rows]
    return prow, cue_db_rows, shots, parsed


def _recompute_from(conn, pid: int, prow, cue_db_rows, shots, parsed) -> dict:
    analysis = build_analysis(
        parsed, shots, prow["cps_limit"],
        prow["min_gap_ms"], prow["tolerance_ms"],
    )
    raw_by_cid = {r["cue_id"]: r for r in cue_db_rows}
    for rep in analysis.cues:
        dbr = raw_by_cid[rep.cue_id]
        rep.uid = dbr["id"]
        rep.adopted = dbr["new_start_ms"] is not None
        rep.adopted_start_ms = dbr["new_start_ms"]
        rep.adopted_end_ms = dbr["new_end_ms"]
    return {
        "id": prow["id"],
        "name": prow["name"],
        "cps_limit": prow["cps_limit"],
        "min_gap_ms": prow["min_gap_ms"],
        "tolerance_ms": prow["tolerance_ms"],
        "created_at": prow["created_at"],
        "shots": [{"ord": s.ord, "start_ms": s.start_ms,
                   "end_ms": s.end_ms} for s in shots],
        "cues": [_cue_row(r) for r in cue_db_rows],
        "analysis": {
            "cues": [vars(c) for c in analysis.cues],
            "chains": analysis.chains,
            "intrinsic": analysis.intrinsic,
            "stats": analysis.stats,
        },
    }


def _recompute(conn, pid: int) -> dict:
    prow, cue_db_rows, shots, parsed = _load_inputs(conn, pid)
    return _recompute_from(conn, pid, prow, cue_db_rows, shots, parsed)


# ---------- 路由 ----------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/projects")
def list_projects():
    with db.get_conn() as conn:
        rows = db.list_projects(conn)
        return [{"id": r["id"], "name": r["name"],
                 "created_at": r["created_at"],
                 "cps_limit": r["cps_limit"],
                 "min_gap_ms": r["min_gap_ms"],
                 "tolerance_ms": r["tolerance_ms"],
                 "cue_count": r["cue_count"],
                 "shot_count": r["shot_count"],
                 "adopted_count": r["adopted_count"]} for r in rows]


@app.post("/api/projects")
async def create_project(
    name: str = Form("未命名项目"),
    cps_limit: float = Form(21.0),
    min_gap_ms: int = Form(40),
    tolerance_ms: int = Form(200),
    subtitle: UploadFile | None = File(None),
    shots_file: UploadFile | None = File(None),
):
    if subtitle is None:
        raise HTTPException(400, "必须提供字幕文件（.srt/.vtt）")
    if cps_limit <= 0 or min_gap_ms < 0 or tolerance_ms < 0:
        raise HTTPException(400, "参数必须为正数/非负")
    sub_bytes = await subtitle.read()
    try:
        parsed = parse_subtitle(sub_bytes, subtitle.filename or "")
    except Exception as exc:
        raise HTTPException(400, f"字幕解析失败：{exc}")
    if not parsed:
        raise HTTPException(400, "字幕文件中未解析出任何条目")

    shots = []
    if shots_file is not None and shots_file.filename:
        raw = await shots_file.read()
        try:
            shots = parse_shots(raw)
        except Exception as exc:
            raise HTTPException(400, f"镜头 JSON 解析失败：{exc}")

    cues_payload = [{
        "cue_id": c.cue_id, "ord": c.ord,
        "start_ms": c.start_ms, "end_ms": c.end_ms,
        "text": visible_text(c.raw_text),
        "raw_text": c.raw_text,
        "graphemes": c.graphemes, "raw_hash": c.raw_hash,
    } for c in parsed]
    shots_payload = [{"ord": s.ord, "start_ms": s.start_ms,
                      "end_ms": s.end_ms} for s in shots]
    with db.get_conn(write=True) as conn:
        pid = db.create_project(conn, name, cps_limit, min_gap_ms,
                                tolerance_ms, cues_payload, shots_payload)
        return _recompute(conn, pid)


@app.get("/api/projects/{pid}")
def get_project(pid: int):
    with db.get_conn() as conn:
        return _recompute(conn, pid)


@app.put("/api/projects/{pid}/settings")
def update_settings(pid: int, body: SettingsIn):
    if body.cps_limit <= 0 or body.min_gap_ms < 0 or body.tolerance_ms < 0:
        raise HTTPException(400, "参数非法")
    # 先取 RESERVED 写锁再读状态：与并发的 POST /adopt 互斥。若采纳
    # 事务先提交，这里锁内读到的就是含新采纳的最新状态，revalidate
    # 会撤销新参数下失效者；若本事务先拿到锁，采纳请求会阻塞到本事务
    # 提交，随后按新参数重读并拒绝失效旧提案（见 adopt）。
    with db.get_conn(write=True) as conn:
        prow = db.get_project(conn, pid)
        if prow is None:
            raise HTTPException(404, "项目不存在")
        cue_rows = list(db.get_cues(conn, pid))
        shot_rows = list(db.get_shots(conn, pid))
        shots = [Shot(ord=s["ord"], start_ms=s["start_ms"], end_ms=s["end_ms"])
                 for s in shot_rows]
        parsed = [
            ParsedCue(
                cue_id=r["cue_id"], ord=r["ord"],
                start_ms=r["start_ms"], end_ms=r["end_ms"],
                raw_text=r["text"], graphemes=r["graphemes"],
                raw_hash=r["raw_hash"],
            ) for r in cue_rows
        ]
        # 同一写事务内：用新参数求提案（与详情/采纳校验完全同一套
        # build_analysis 规则）。失败则由 get_conn 整体回滚，参数与
        # 采纳状态都不会被改动。
        analysis = build_analysis(
            parsed, shots, body.cps_limit,
            body.min_gap_ms, body.tolerance_ms,
        )
        proposals = {
            rep.cue_id: (
                (rep.proposal_start_ms, rep.proposal_end_ms)
                if rep.proposal_start_ms is not None else None
            ) for rep in analysis.cues
        }
        # 落新参数；随后按「已采纳取采纳值，未采纳先取新提案、无提案取
        # 原值」的生效位置重核验每条采纳，撤销失效者。重算若失败，
        # 下面任一步异常都会触发整事务回滚，不留半更新。
        db.update_settings(conn, pid, body.cps_limit,
                           body.min_gap_ms, body.tolerance_ms)
        shot_ranges = [(s["start_ms"], s["end_ms"]) for s in shot_rows]
        for uid in revalidate_adoptions(
            cue_rows, shot_ranges, body.tolerance_ms, body.min_gap_ms,
            proposals,
        ):
            db.delete_adoption(conn, pid, uid)
        return _recompute(conn, pid)


@app.post("/api/projects/{pid}/adopt")
def adopt(pid: int, body: AdoptIn):
    # 先取写锁再读取：若并发的 PUT settings 先提交，本请求会在 BEGIN
    # IMMEDIATE 处等待，锁内重读时拿到的已是新参数，随后 _recompute
    # 用新参数求提案——旧提案位置在此被判失效而拒绝（400），不会把按
    # 旧参数算出的采纳值与新参数并存落库。
    with db.get_conn(write=True) as conn:
        if db.get_project(conn, pid) is None:
            raise HTTPException(404, "项目不存在")
        cue_rows = list(db.get_cues(conn, pid))
        target = next((r for r in cue_rows if r["id"] == body.cue_uid), None)
        if target is None:
            raise HTTPException(400, "cue 不存在")
        orig_dur = target["end_ms"] - target["start_ms"]
        if body.new_end_ms - body.new_start_ms != orig_dur:
            raise HTTPException(400, "校正仅允许平移：时长必须与原时长完全一致")
        # 必须落在当前（锁内最新）参数下的提案位置（重新计算并比对）
        payload = _recompute(conn, pid)
        cand = next((c for c in payload["analysis"]["cues"]
                     if c["uid"] == body.cue_uid), None)
        if cand is None or cand["proposal_start_ms"] is None:
            raise HTTPException(400, "该 cue 在当前参数下无可采纳提案")
        if (body.new_start_ms != cand["proposal_start_ms"]
                or body.new_end_ms != cand["proposal_end_ms"]):
            raise HTTPException(400, "采纳值必须等于系统给出的提案时间")
        db.upsert_adoption(conn, pid, body.cue_uid,
                           body.new_start_ms, body.new_end_ms)
        return _recompute(conn, pid)


@app.delete("/api/projects/{pid}/adopt/{cue_uid}")
def revert_adoption(pid: int, cue_uid: int):
    with db.get_conn(write=True) as conn:
        db.delete_adoption(conn, pid, cue_uid)
        return _recompute(conn, pid)


@app.delete("/api/projects/{pid}")
def remove_project(pid: int):
    with db.get_conn(write=True) as conn:
        db.delete_project(conn, pid)
    return {"ok": True}


# ---------- 导出 ----------

def _fmt_ms(ms: int) -> str:
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, mss = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{mss:03d}"


def _build_srt(cues: list[dict], key_start="proposal_start_ms",
               key_end="proposal_end_ms") -> str:
    out = []
    for i, c in enumerate(cues, start=1):
        s, e = c[key_start], c[key_end]
        if s is None:
            s, e = c["start_ms"], c["end_ms"]
        out.append(f"{i}\n{_fmt_ms(s)} --> {_fmt_ms(e)}\n{c['text']}\n")
    return "\n".join(out)


def _effective_times(c: dict) -> tuple[int, int]:
    """导出时间轴上一条 cue 的生效时间：已采纳取采纳值；未采纳先取提案，
    无提案取原值。独立 SRT 与 ZIP 内 SRT 共用同一有效时间轴。"""
    s, e = c["adopted_start_ms"], c["adopted_end_ms"]
    if s is None:
        s, e = c["proposal_start_ms"], c["proposal_end_ms"]
    if s is None:
        s, e = c["start_ms"], c["end_ms"]
    return s, e


@app.get("/api/projects/{pid}/export")
def export(pid: int, fmt: str = "json"):
    with db.get_conn() as conn:
        data = _recompute(conn, pid)
    if fmt == "srt":
        srt_cues = [{
            "proposal_start_ms": s, "proposal_end_ms": e, "text": c["text"],
        } for c in data["analysis"]["cues"]
            for s, e in [_effective_times(c)]]
        body = _build_srt(srt_cues)
        return Response(content=body, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition":
                                 "attachment; filename=corrected.srt"})
    if fmt not in ("json", "zip"):
        raise HTTPException(400, "fmt 仅支持 json / srt / zip")

    export_doc = {
        "schema": "subtitle-correction-export/1.0",
        "project": {
            "id": data["id"],
            "name": data["name"],
            "cps_limit": data["cps_limit"],
            "min_gap_ms": data["min_gap_ms"],
            "tolerance_ms": data["tolerance_ms"],
        },
        "document_hash": "",
        "cues": [{
            "cue_id": c["cue_id"],
            "raw_text_hash": c["raw_hash"],
            "graphemes": c["graphemes"],
            "original": {"start_ms": c["start_ms"], "end_ms": c["end_ms"]},
            "proposal": (
                {"start_ms": c["proposal_start_ms"],
                 "end_ms": c["proposal_end_ms"]}
                if c["proposal_start_ms"] is not None else None
            ),
            "corrected": (
                {"start_ms": c["adopted_start_ms"],
                 "end_ms": c["adopted_end_ms"]}
                if c["adopted"] else None
            ),
            "status": ("corrected" if c["adopted"]
                       else ("proposed" if c["proposal_start_ms"] is not None
                             else "conflict")),
            "text": c["text"],
        } for c in data["analysis"]["cues"]],
        "conflicts": {
            "chains": data["analysis"]["chains"],
            "intrinsic": data["analysis"]["intrinsic"],
        },
        "stats": data["analysis"]["stats"],
    }
    raw = json.dumps(export_doc, ensure_ascii=False, indent=2, sort_keys=True)
    doc_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    export_doc["document_hash"] = doc_hash
    raw = json.dumps(export_doc, ensure_ascii=False, indent=2, sort_keys=True)

    if fmt == "json":
        return Response(
            content=raw, media_type="application/json; charset=utf-8",
            headers={"Content-Disposition":
                     "attachment; filename=correction.json"})

    # zip：JSON + SRT，SRT 与独立 SRT 导出共用同一有效时间轴
    # （采纳值，未采纳者回退提案再回退原值）
    srt_cues = []
    for c in data["analysis"]["cues"]:
        s, e = _effective_times(c)
        srt_cues.append({"proposal_start_ms": s, "proposal_end_ms": e,
                         "text": c["text"]})
    srt_text = _build_srt(srt_cues)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("correction.json", raw)
        zf.writestr("corrected.srt", srt_text)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 "attachment; filename=correction.zip"})


# ---------- 静态前端 ----------

_STATIC = Path(__file__).parent / "static"
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/")
    def index():
        return Response(content=(_STATIC / "index.html").read_text(encoding="utf-8"),
                        media_type="text/html; charset=utf-8")
