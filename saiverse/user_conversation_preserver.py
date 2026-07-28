"""User Conversation Track preserver (v0.32, 2026-05-09).

Stelis 親子スレッドモデルを流用したユーザー会話 Track の生メッセージ保持機構。
ペルソナのオーナーユーザーとの会話を、Metabolism やコンテキスト圧縮があっても
**生メッセージとして** 一定数 (デフォルト 20、SAIVERSE_USER_CONV_PRESERVE_COUNT) 確保する。

設計思想:
- ユーザー会話 Track = 親 Stelis スレッド (常時保持)
- 他 Track = 子 Stelis スレッド (メインラインで入れ替わる)
- ペルソナの人格・対話温度の安定性を、自律稼働中も生メッセージで担保する

挙動:
1. Metabolism 後の history 内に居る オーナー Track メッセージの数 = existing_count
2. 不足分 (target - existing_count) だけ history より過去のメッセージを取得して上部補完
3. existing_count が target 以上なら何もしない (= 重複回避)

詳細: docs/intent/persona_cognition/track_chronicle.md, revisions.md v0.32
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)


def _get_target_count() -> int:
    """環境変数 SAIVERSE_USER_CONV_PRESERVE_COUNT で調整可能、デフォルト 20。"""
    raw = os.getenv("SAIVERSE_USER_CONV_PRESERVE_COUNT", "20")
    try:
        n = int(raw)
        return n if n > 0 else 20
    except (TypeError, ValueError):
        return 20


def get_owner_user_conversation_track_id(
    persona_id: str, manager: Any
) -> Optional[str]:
    """ペルソナのオーナーユーザーとの user_conversation Track ID を返す。

    優先順位:
    1. UserAiLink で persona に紐付くユーザーがいれば、その user_id の user_conversation Track
    2. 紐付きユーザーが居なければ、最古の user_conversation Track をオーナー扱い
    3. user_conversation Track が一つも無ければ None
    """
    if not manager:
        return None
    track_manager = getattr(manager, "track_manager", None)
    if track_manager is None:
        return None

    # ペルソナの全 user_conversation Track を列挙
    try:
        all_tracks = track_manager.list_for_persona(persona_id)
    except Exception:
        LOGGER.debug(
            "[user-conv-preserver] list_for_persona failed for %s",
            persona_id, exc_info=True,
        )
        return None

    user_conv_tracks = [
        t for t in all_tracks if getattr(t, "track_type", None) == "user_conversation"
    ]
    if not user_conv_tracks:
        return None

    # リンクユーザー取得
    linked_user_id_str: Optional[str] = None
    SessionLocal = getattr(manager, "SessionLocal", None)
    if SessionLocal is not None:
        try:
            from database.models import UserAiLink
            db = SessionLocal()
            try:
                link = (
                    db.query(UserAiLink)
                    .filter(UserAiLink.AIID == persona_id)
                    .first()
                )
                if link is not None:
                    linked_user_id_str = str(link.USERID)
            finally:
                db.close()
        except Exception:
            LOGGER.debug(
                "[user-conv-preserver] UserAiLink query failed for %s",
                persona_id, exc_info=True,
            )

    # リンクユーザーの user_conversation Track を探す
    if linked_user_id_str:
        for t in user_conv_tracks:
            try:
                md = json.loads(t.track_metadata) if t.track_metadata else {}
            except (TypeError, ValueError):
                md = {}
            if str(md.get("user_id", "")) == linked_user_id_str:
                return t.track_id

    # フォールバック: 最古の user_conversation Track
    user_conv_tracks.sort(
        key=lambda t: getattr(t, "created_at", None) or datetime.min
    )
    return user_conv_tracks[0].track_id


def get_supplementary_user_conversation_messages(
    sai_memory_adapter: Any,
    owner_track_id: str,
    history_messages: List[Dict[str, Any]],
    *,
    target_count: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """history 内のオーナー Track メッセージ数を数えて、不足分を補完取得する。

    Args:
        sai_memory_adapter: persona.sai_memory (= SAIMemoryAdapter)
        owner_track_id: オーナーユーザー会話 Track の id
        history_messages: 既に取得済みの history メッセージリスト (line_role/scope フィルタ済み)
        target_count: 確保したい件数 (None なら環境変数から)

    Returns:
        (supplementary_messages, oldest_supplementary_created_at)
        - supplementary_messages: 上部に挿入すべき不足分メッセージ (古→新の順)
        - oldest_supplementary_created_at: 補完メッセージ最古の created_at (時刻アンカー用)
        - 補完不要なら ([], None)
    """
    if not owner_track_id or sai_memory_adapter is None:
        return [], None
    if not getattr(sai_memory_adapter, "is_ready", lambda: False)():
        return [], None
    target = target_count if target_count is not None else _get_target_count()

    # history 内のオーナー Track メッセージ数を数える
    existing_count = sum(
        1 for m in history_messages
        if m.get("origin_track_id") == owner_track_id
    )
    needed = target - existing_count
    if needed <= 0:
        LOGGER.debug(
            "[user-conv-preserver] No supplement needed: existing=%d >= target=%d",
            existing_count, target,
        )
        return [], None

    # history 最古の created_at を取得 (補完範囲の上限)
    if history_messages:
        oldest_history_ts = history_messages[0].get("created_at") or 0
    else:
        oldest_history_ts = None

    # SAIMemory adapter の DB 接続を直接利用して SQL で取得
    conn = getattr(sai_memory_adapter, "conn", None)
    if conn is None:
        LOGGER.debug("[user-conv-preserver] No conn on adapter")
        return [], None

    try:
        # line_role / scope の空欄は legacy 行 (Phase 1 以前 + 転記経路) で、
        # 文脈構築の絞り込み (_payload_passes_context_filter) は空欄を
        # main_line / committed 扱いに救済する。ここだけ完全一致で書くと、
        # 転記されたユーザー発言 (line_role 空欄) が構造的に全滅し、補完が
        # ペルソナ側の発言だけになる (2026-07-29 実害: anchor リセット後の
        # プレビューが「片側だけの会話」になった)。
        legacy_main_committed = (
            "AND (line_role = 'main_line' OR line_role IS NULL) "
            "AND (scope = 'committed' OR scope IS NULL) "
        )
        if oldest_history_ts is not None and oldest_history_ts > 0:
            cur = conn.execute(
                "SELECT id, role, content, created_at, metadata "
                "FROM messages "
                "WHERE origin_track_id = ? "
                + legacy_main_committed +
                "AND created_at < ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (owner_track_id, int(oldest_history_ts), needed),
            )
        else:
            cur = conn.execute(
                "SELECT id, role, content, created_at, metadata "
                "FROM messages "
                "WHERE origin_track_id = ? "
                + legacy_main_committed +
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (owner_track_id, needed),
            )
        rows = cur.fetchall()
    except Exception:
        LOGGER.warning(
            "[user-conv-preserver] SQL fetch failed", exc_info=True,
        )
        return [], None

    if not rows:
        return [], None

    # 古→新 の順に並べ替え (DESC で取った後 reverse)
    rows = list(reversed(rows))
    supplementary: List[Dict[str, Any]] = []
    oldest_ts: Optional[int] = None
    for msg_id, role, content, created_at, metadata_raw in rows:
        try:
            ts = int(created_at)
        except (TypeError, ValueError):
            ts = 0
        if oldest_ts is None and ts:
            oldest_ts = ts
        # role 正規化 (model → assistant)
        norm_role = "assistant" if role == "model" else role
        if isinstance(norm_role, str) and norm_role.lower() == "system":
            # system role は user + <system> ラップに変換 (Gemini 互換)
            norm_role = "user"
            content_wrapped = (
                f"<system>\n{content}\n</system>" if content else "<system></system>"
            )
        else:
            content_wrapped = content or ""
        supplementary.append({
            "id": msg_id,
            "role": norm_role,
            "content": content_wrapped,
            "created_at": ts,
            "origin_track_id": owner_track_id,
        })
    LOGGER.info(
        "[user-conv-preserver] Supplementing %d user-conversation messages "
        "(history existing=%d, target=%d, needed=%d, owner_track=%s)",
        len(supplementary), existing_count, target, needed, owner_track_id,
    )
    return supplementary, oldest_ts
