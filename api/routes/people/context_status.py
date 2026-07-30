"""提示コンテキストの現在量と水位を返す read-only endpoint。

チャットオプションの「データ送信量の管理」セクション用
(docs/issues/chat_options_metabolism_section_redesign.md)。state を持たず、
§15 読み戻しの読み取り専用計画を再利用して「次に話しかけた時に実際に送られる
提示コンテキスト」の文字数を測る — コンテキストプレビューと同じ値を出すため
(プレビューが嘘にならないの原則)。

水位は model 定義一本で解決する (グローバル上書きは 2026-07-30 廃止)。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_manager

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{persona_id}/context-status")
def get_context_status(persona_id: str, manager=Depends(get_manager)) -> dict[str, Any]:
    """指定ペルソナの提示コンテキスト状態 (水位 + 現在文字数) を read-only で返す。

    - ``watermarks``: 実効 model の三水位 (model 定義から解決)。model が水位を
      持たない (null 設定) 場合は None = Metabolism を持たない。
    - ``presented_chars``: 次の user Pulse で実際に送られる提示コンテキストの
      文字数。§15 読み戻しが適用されるならその適用後 (プレビューと一致)。
      anchor 未確立 (まだ会話が始まっていない) や persona 未ロードでは None。
    - 一切の行を書かない (resolve は persist_advance=False)。
    """
    details = manager.get_ai_details(persona_id)
    if not details:
        raise HTTPException(status_code=404, detail="Persona not found")

    persona = manager.personas.get(persona_id)
    model_key = getattr(persona, "model", None) or details.get("DEFAULT_MODEL") or None

    status: dict[str, Any] = {
        "persona_id": persona_id,
        "model": model_key,
        "metabolism": False,          # 実効 model が水位を持つか
        "low_chars": None,            # 初期読み込み量 (anchor 未確立時に読む量)
        "target_chars": None,         # 残す量 (整理後にここへ揃える)
        "high_chars": None,           # 上限 (超えたら整理が走る)
        "presented_chars": None,      # 現在の提示コンテキスト文字数 (読み戻し後)
        "refill_applied": False,      # presented_chars が §15 読み戻し込みの値か
        "measurement_failed": False,  # 計測が失敗した (None を「起点なし」と読ませない)
    }
    if not model_key:
        return status

    lifecycle = _resolve_lifecycle(manager)
    if lifecycle is None:
        return status

    try:
        watermarks = lifecycle.get_metabolism_watermarks(persona, model_key)
    except Exception:
        # 解決失敗を「水位を持たないモデル」(正常な metabolism=false) に偽装
        # しない — 設定破損等の障害はエラーとして UI に届ける (Codex 指摘 2026-07-30)。
        LOGGER.warning(
            "context-status: watermark resolution failed for %s/%s",
            persona_id, model_key, exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="watermark resolution failed",
        )
    if watermarks is None:
        return status
    status.update({
        "metabolism": True,
        "low_chars": watermarks.low,
        "target_chars": watermarks.target,
        "high_chars": watermarks.high,
    })

    # persona 未ロード = 会話中でない → 提示ウィンドウを組めない (水位だけ返す)
    if persona is None:
        return status

    try:
        # raise_on_error: 既定の fail-open (内部失敗 → None) だと「読み戻し適用
        # なし (正常)」と区別できず、素の窓を測った嘘の数字を正常表示してしまう
        # (Codex 指摘 2026-07-30)。失敗はここまで届かせて measurement_failed に落とす。
        refill_plan = lifecycle.preview_refilled_history(
            persona, model_key, raise_on_error=True,
        )
        if refill_plan:
            from sea.eviction_plan import message_chars

            status["presented_chars"] = message_chars(refill_plan["presented"])
            status["refill_applied"] = True
            return status

        anchor_id, _resolution = lifecycle.resolve_metabolism_anchor(
            persona, model_key=model_key, persist_advance=False,
        )
        if not anchor_id:
            return status  # 起点未確立 (ブートストラップ前)
        window = lifecycle.get_presented_window(persona, model_key, anchor_id)
        from sea.eviction_plan import message_chars

        status["presented_chars"] = message_chars(window.presented)
    except Exception:
        # 測れないことは水位表示を妨げない — ただし「まだ起点が無い」(正常) と
        # 区別できるよう、障害であることを明示して返す (Codex 指摘 2026-07-30)。
        LOGGER.warning(
            "context-status: presented-window measurement failed for %s/%s",
            persona_id, model_key, exc_info=True,
        )
        status["measurement_failed"] = True
    return status


def _resolve_lifecycle(manager: Any):
    """SessionLifecycle を manager からたどる (cache_status.py と同じ経路)。"""
    runtime = getattr(manager, "sea_runtime", None) or getattr(manager, "runtime", None)
    if runtime is None:
        return None
    return getattr(runtime, "session_lifecycle", None)
