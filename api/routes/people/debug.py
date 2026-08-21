"""自律稼働デバッグコントローラー API.

設計: docs/intent/persona_cognition/debug_controller.md

UC-2「割り込みと復帰」等の検証で、自律稼働のタイマー
(AutonomyManager watchdog / 会話の沈黙タイマー 30分) を無視して手動で
ステップ実行する。発火系は LLM 呼び出しを伴うため別スレッドで投げ、
API をブロックしない。

NOTE: 廃止機構の no-op エンドポイント (fire-meta-judgment / fire-subline-pulse /
scheduler.subline) は 2026-08-14 に削除した。残していた理由は「UI 互換」だったが、
呼んでいた画面 (DebugPanel の該当ボタン) が無くなったため、押しても必ず失敗する
口だけが残る形になっていた。旧 SubLineScheduler は自律行動 v2 で
(intent autonomous_behavior_v2.md §9.3)、v1 メタ判断は Track 撤廃 順序①で
(track_retirement.md §7.4) それぞれ退役済み。
"""
import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class SchedulerControlRequest(BaseModel):
    autonomy: Optional[bool] = None
    manual_mode: Optional[bool] = None


class DebugActionResponse(BaseModel):
    success: bool
    message: str


def _run_in_background(fn, *args, **kwargs) -> None:
    """発火系を別スレッドで投げて API をブロックしない."""
    def _target():
        try:
            fn(*args, **kwargs)
        except Exception:
            LOGGER.exception(
                "[debug] background fire failed: %s", getattr(fn, "__name__", fn)
            )
    threading.Thread(target=_target, name="DebugFire", daemon=True).start()


@router.post("/{persona_id}/debug/wrap-up-conversation", response_model=DebugActionResponse)
def wrap_up_conversation(persona_id: str, manager=Depends(get_manager)):
    """沈黙タイマー相当を即時発火 (会話状態の解除)。

    本番のタイムアウト経路
    (``saiverse.user_conversation.handle_conversation_timeout``) をそのまま
    即時に呼ぶ。

    **開いている会話があるときだけ撃つ**。切り上げる会話が実在しないまま撃つと、
    存在しない会話の帳簿処理が走る (2026-08-14 Codex 指摘 F5)。
    """
    from saiverse.user_conversation import (
        get_open_conversation,
        handle_conversation_timeout,
    )

    open_conversation = get_open_conversation(manager, persona_id)
    if open_conversation is None:
        raise HTTPException(
            status_code=409,
            detail="開いている会話がありません (会話は既に終了しています)",
        )

    # 検証した会話そのものを背景処理へ渡す。ここは同期検証 → 背景発火なので、
    # 間に自然タイムアウトや別の切り上げが会話を閉じ (さらに新しい会話を開き)
    # うる。渡さないと、発火側は「いま開いている会話」を無条件に終わらせ、
    # 別の会話 / 存在しない会話を閉じる (2026-08-14 Codex 二巡目)。
    _run_in_background(
        handle_conversation_timeout, manager, persona_id,
        expected_conversation_id=open_conversation.get("conversation_id"),
    )
    return DebugActionResponse(
        success=True,
        message=(
            "会話を切り上げました (沈黙タイマーの即時発火 "
            f"conversation={open_conversation.get('conversation_id')})"
        ),
    )


@router.get("/{persona_id}/debug/scheduler")
def get_scheduler_status(persona_id: str, manager=Depends(get_manager)):
    """タイマーの稼働状態を返す."""
    autonomy_state = "stopped"
    ams = getattr(manager, "_autonomy_managers", None)
    if ams and persona_id in ams:
        try:
            autonomy_state = ams[persona_id].get_status().get("state", "unknown")
        except Exception:
            autonomy_state = "error"
    manual_personas = getattr(manager, "_debug_manual_mode_personas", set())
    return {
        "autonomy_state": autonomy_state,
        "manual_mode": persona_id in manual_personas,
    }


@router.post("/{persona_id}/debug/generate-embeddings", response_model=DebugActionResponse)
def generate_embeddings(persona_id: str, manager=Depends(get_manager)):
    """Chronicle / Memopedia page / Fragment の未生成 embedding をバッチ生成."""
    persona = manager.personas.get(persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail=f"persona {persona_id} がロードされていません")

    adapter = getattr(persona, "sai_memory", None)
    if not adapter or not adapter.is_ready():
        raise HTTPException(status_code=503, detail="SAIMemory が利用できません")
    if not adapter.can_embed():
        raise HTTPException(status_code=503, detail="Embedding モデルが利用できません")

    # ⚠ 同じ conn を使う他の書き込み (Memopedia 変換など) と混ざらないよう、
    # SAIMemory のロック配下で回す。ここは内部で何度も commit するため、
    # ロックを取らずに割り込むと相手の開いているトランザクションを確定させる。
    lock = getattr(adapter, "_db_lock", None)
    if lock is None:
        raise HTTPException(
            status_code=503,
            detail="SAIMemory の排他ロックが取得できないため、この操作は実行できません",
        )

    try:
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.memopedia import init_memopedia_tables
        from sai_memory.unified_recall import (
            embed_chronicle_entries,
            embed_memopedia_fragments,
            embed_memopedia_pages,
        )
        with lock:
            init_arasuji_tables(adapter.conn)
            init_memopedia_tables(adapter.conn)
            n_chr = embed_chronicle_entries(adapter.conn, adapter.embedder, level=1)
            n_page = embed_memopedia_pages(adapter.conn, adapter.embedder)
            n_frag = embed_memopedia_fragments(adapter.conn, adapter.embedder)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("[debug] Embedding generation failed")
        raise HTTPException(status_code=500, detail=f"Embedding 生成に失敗: {exc}")

    return DebugActionResponse(
        success=True,
        message=f"Embedding 生成完了: Chronicle={n_chr}, Pages={n_page}, Fragments={n_frag}",
    )


def _get_or_create_autonomy(persona_id: str, manager):
    """このペルソナの AutonomyManager を取り出す (無ければ作る)。

    束 6c (2026-08-22) で ``api/routes/people/autonomy.py`` (自律行動マネージャーの
    操作 API) を削除したので、唯一の残った消費者であるデバッグ操作面へ引き取った。
    本番の起動・停止は ``SAIVerseManager.ensure_autonomy_for`` が
    ``AUTONOMY_ENABLED`` に同期して行う。
    """
    from saiverse.autonomy_manager import AutonomyManager

    if not hasattr(manager, "_autonomy_managers"):
        manager._autonomy_managers = {}
    if persona_id not in manager._autonomy_managers:
        manager._autonomy_managers[persona_id] = AutonomyManager(
            persona_id=persona_id, manager=manager,
        )
    return manager._autonomy_managers[persona_id]


@router.post("/{persona_id}/debug/scheduler", response_model=DebugActionResponse)
def control_scheduler(
    persona_id: str,
    request: SchedulerControlRequest,
    manager=Depends(get_manager),
):
    """タイマー制御. autonomy (per-persona) / manual_mode (per-persona の会話沈黙タイマー停止)."""
    msgs = []

    if request.autonomy is not None:
        am = _get_or_create_autonomy(persona_id, manager)
        if request.autonomy:
            am.start()
            msgs.append("AutonomyManager 開始")
        else:
            am.stop()
            msgs.append("AutonomyManager 停止")

    if request.manual_mode is not None:
        from saiverse.user_conversation import (
            arm_conversation_timeout,
            cancel_conversation_timeout,
            get_open_conversation,
        )

        manual_personas = manager._debug_manual_mode_personas
        if request.manual_mode:
            manual_personas.add(persona_id)
            cancel_conversation_timeout(manager, persona_id)
            msgs.append("完全手動モード ON (会話の沈黙タイマー停止)")
        else:
            manual_personas.discard(persona_id)
            # 会話が開いているペルソナだけ張り直す (閉じた会話へ張ると空撃ちになる)。
            if get_open_conversation(manager, persona_id) is not None:
                arm_conversation_timeout(manager, persona_id)
            msgs.append("完全手動モード OFF")

    if not msgs:
        return DebugActionResponse(success=False, message="制御対象が指定されていません")
    return DebugActionResponse(success=True, message=" / ".join(msgs))


# ---------------------------------------------------------------------------
# v0.2.x Memopedia を v0.3.x 用へ変換する (本文 → Fragment)
#
# 設計: docs/intent/memopedia_body_to_fragment.md
# アップデート時の自動マイグレーションにしない (intent §6)。取り違えが起こりうる
# うえ検出できないので、ユーザーが下見を見てから自分で押す。
# ---------------------------------------------------------------------------

_PREVIEW_PAGE_LIMIT = 300


class RevertConversionRequest(BaseModel):
    run_id: str
    #: 変換より後の編集ごと変換前へ戻してよい、とユーザーが明示したとき
    force: bool = False


class ApplyConversionRequest(BaseModel):
    """保留行への判断。``{page_id: {line_no: "fragment" | "body"}}``。

    渡されなかった保留行は本文に残る (機械が代わりに決めない)。
    ``fingerprint`` は下見が返したもの。下見のあとで本文が変わっていたら、
    選んだ行番号が別の行を指しうるので実行しない。
    """

    decisions: Optional[Dict[str, Dict[str, str]]] = None
    fingerprint: Optional[str] = None


@contextmanager
def _memopedia_session(persona_id: str, manager, *, read_only: bool = False):
    """変換のための **専用の DB 接続** を開く。

    SAIMemory の接続 (``adapter.conn``) は他の経路と共有されている。同じ接続へ
    他所から ``commit()`` が入ると、変換の ``BEGIN IMMEDIATE`` の途中までが確定し、
    その後 rollback しても戻らない —— 部分適用が残る (Codex 指摘 2026-08-05)。

    **接続を分ければ SQLite の書き込みロック自体が排他になる** ので、他の経路が
    何をしようと変換のトランザクションは割られない。

    (2026-08-06 以降、錠前は DB ファイルに紐づく = 取り忘れが起きない
    ``sai_memory.db_locks``。それでも接続を分けるのは、Python の錠前が守るのは
    同一プロセスの中だけで、別プロセスの書き手には効かないため。)
    """
    persona = manager.personas.get(persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail=f"persona {persona_id} がロードされていません")
    adapter = getattr(persona, "sai_memory", None)
    if not adapter or not adapter.is_ready():
        raise HTTPException(status_code=503, detail="SAIMemory が利用できません")
    db_path = getattr(getattr(adapter, "settings", None), "db_path", None)
    if not db_path:
        raise HTTPException(status_code=503, detail="SAIMemory の DB パスが取得できません")

    # 接続を分けるだけだと、変換が書き込みロックを握っている間に SAIMemory 側の
    # 追記が SQLITE_BUSY になり、アダプタの広い例外処理がそれを飲み込んで
    # **ペルソナの記憶が黙って落ちる** (Codex 指摘 2026-08-05)。
    # ロックも一緒に取って、ロックを尊重する書き手は待たせる。
    lock = getattr(adapter, "_db_lock", None)
    if lock is None:
        raise HTTPException(
            status_code=503,
            detail="SAIMemory の排他ロックが取得できないため、この操作は実行できません",
        )

    from sai_memory.memopedia import init_memopedia_tables

    with lock:
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            # 他の接続が書き込み中なら待つ (即 SQLITE_BUSY で落とさない)
            conn.execute("PRAGMA busy_timeout = 30000")
            if read_only:
                # 下見は「ページと Fragment を変えない」と約束している。
                # init_memopedia_tables は root ページの seed と commit を伴うので、
                # 未初期化の DB では走らせずに断る (Codex 指摘 2026-08-05)。
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memopedia_pages'"
                ).fetchone()
                if not exists:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "このペルソナの Memopedia はまだ作られていません"
                            "（変換するものがありません）"
                        ),
                    )
            else:
                # スキーマ初期化は無条件に commit するので、変換の前に済ませておく
                init_memopedia_tables(conn)
            yield conn
        finally:
            conn.close()


@router.get("/{persona_id}/debug/memopedia-conversion/preview")
def preview_memopedia_conversion(persona_id: str, manager=Depends(get_manager)):
    """下見: 変換したら何がどうなるかを、ページと Fragment を変えずに返す。"""
    return _run_preview(persona_id, manager, None)


@router.post("/{persona_id}/debug/memopedia-conversion/preview")
def preview_memopedia_conversion_with_decisions(
    persona_id: str,
    request: Optional[ApplyConversionRequest] = None,
    manager=Depends(get_manager),
):
    """判断を織り込んだ下見。

    保留行を選ぶと変換されるページ数や本文が空になるページ数も変わる。
    最初の下見の数字を出したままだと、自分の選択で何が起きるかが見えない
    (Codex 指摘 2026-08-05)。
    """
    return _run_preview(persona_id, manager, request.decisions if request else None)


def _run_preview(persona_id: str, manager, decisions):
    from sai_memory.memopedia.body_to_fragment import preview_conversion

    try:
        with _memopedia_session(persona_id, manager, read_only=True) as conn:
            preview = preview_conversion(conn, decisions)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("[debug] memopedia conversion preview failed")
        raise HTTPException(status_code=500, detail=f"下見に失敗しました: {exc}")

    conservation = preview["conservation"]
    return {
        "total_page_count": preview["total_page_count"],
        "page_count": preview["page_count"],
        "fragment_count": preview["fragment_count"],
        "dedup_count": preview["dedup_count"],
        "confirmed_count": preview["confirmed_count"],
        "pending_count": preview["pending_count"],
        "decided_count": preview["decided_count"],
        "emptied_count": preview["emptied_count"],
        "kept_body_count": preview["kept_body_count"],
        "is_safe": preview["is_safe"],
        "fingerprint": preview["fingerprint"],
        "verbatim_breaches": preview["verbatim_breaches"],
        "conservation": {
            "before_lines": conservation["before_lines"],
            "after_lines": conservation["after_lines"],
            "lost_count": len(conservation["lost"]),
            "gained_count": len(conservation["gained"]),
            "lost_samples": conservation["lost"][:20],
            "gained_samples": conservation["gained"][:20],
        },
        "marks": preview["marks"],
        "pending_pages": preview["pending_pages"],
        "pages": preview["pages"][:_PREVIEW_PAGE_LIMIT],
        "pages_truncated": max(0, preview["page_count"] - _PREVIEW_PAGE_LIMIT),
    }


@router.post("/{persona_id}/debug/memopedia-conversion/apply")
def apply_memopedia_conversion(
    persona_id: str,
    request: Optional[ApplyConversionRequest] = None,
    manager=Depends(get_manager),
):
    """変換を実行する。逐語の検算に落ちたら何も書かずに 409 を返す。"""
    from sai_memory.memopedia.body_to_fragment import apply_conversion

    try:
        with _memopedia_session(persona_id, manager) as conn:
            if request is None or not request.fingerprint:
                raise HTTPException(
                    status_code=400,
                    detail="下見が返した指紋が要ります（下見からやり直してください）",
                )
            result = apply_conversion(
                conn,
                decisions=request.decisions,
                expected_fingerprint=request.fingerprint,
            )
    except HTTPException:
        raise
    except ValueError as exc:
        # 逐語の検算に落ちた = 記憶の行が消えるか増える。書かずに止める。
        LOGGER.error("[debug] memopedia conversion refused: %s", exc)
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        LOGGER.exception("[debug] memopedia conversion failed")
        raise HTTPException(status_code=500, detail=f"変換に失敗しました: {exc}")

    left = result["pending_left"]
    dedup = result["dedup_count"]
    return {
        "success": True,
        "run_id": result["run_id"],
        "page_count": result["page_count"],
        "fragment_count": result["fragment_count"],
        "dedup_count": dedup,
        "confirmed_count": result["confirmed_count"],
        "decided_count": result["decided_count"],
        "pending_left": left,
        "message": (
            f"変換しました: {result['page_count']} ページ / Fragment {result['fragment_count']} 件"
            f"（来歴の裏づけ {result['confirmed_count']} 行 + 判断済み {result['decided_count']} 行）"
            + (f" / 既に Fragment があった {dedup} 行は本文から抜くだけにしました" if dedup else "")
            + (f" / 判断していない保留 {left} 行は本文に残しました" if left else "")
            + f" (run_id={result['run_id']})"
        ),
    }


@router.get("/{persona_id}/debug/memopedia-conversion/runs")
def list_memopedia_conversion_runs(persona_id: str, manager=Depends(get_manager)):
    """取り消せる変換の一覧 (新しい順)。"""
    from sai_memory.memopedia.body_to_fragment import list_conversion_runs

    with _memopedia_session(persona_id, manager, read_only=True) as conn:
        return {"runs": list_conversion_runs(conn)}


@router.post("/{persona_id}/debug/memopedia-conversion/revert")
def revert_memopedia_conversion(
    persona_id: str,
    request: RevertConversionRequest,
    manager=Depends(get_manager),
):
    """変換を丸ごと取り消す。"""
    from sai_memory.memopedia.body_to_fragment import revert_conversion

    try:
        with _memopedia_session(persona_id, manager) as conn:
            result = revert_conversion(conn, request.run_id, force=request.force)
    except HTTPException:
        raise
    except ValueError as exc:
        # 記録が無い場合と、後続の編集があって拒否した場合を区別する
        status = 404 if "見つかりません" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc))
    except Exception as exc:
        LOGGER.exception("[debug] memopedia conversion revert failed")
        raise HTTPException(status_code=500, detail=f"取り消しに失敗しました: {exc}")

    overwritten = result.get("overwritten_pages") or []
    message = (
        f"取り消しました: {result['restored_pages']} ページを復元 / "
        f"Fragment {result['deleted_fragments']} 件を削除"
    )
    if overwritten:
        # 変換後に別経路が書き換えていたページは、復元でその編集を上書きした。
        # 黙って消さずに知らせる。
        names = "、".join(o["title"] for o in overwritten[:5])
        message += (
            f"　⚠ 変換より後の編集があった {len(overwritten)} ページ（{names}"
            f"{' ほか' if len(overwritten) > 5 else ''}）は、その編集ごと変換前へ戻しました"
        )
    return {"success": True, "message": message, **result}
