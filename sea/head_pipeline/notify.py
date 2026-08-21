"""head 操作の内容型通知 (beat_execution_context.md §3.3 / 統合工事 §6-4)。

issue: docs/issues/head_mutation_notification_gap.md — head の元データ
(コア記憶・机・Memopedia 目次) をスペルで操作したとき、操作の生ログが読み手の
窓に入らない場合 (別 line / 別 model の Session)、読み手の LLM は変更を知る
手段が無かった。

解 (§3.3 の確定裁定):

- **操作起点 push 型**: 操作したツール (memory_write / memory_open 等) が
  成功時に自分で通知を発行する。snapshot 差分検出
  (flush_diffs) はツールを経由しない変化 (UI 編集・migration 等) を拾う
  backstop に退く。
- **内容忠実性の構造的保証**: 通知本文は ``section.capture(ctx)`` →
  ``section.render(snapshot)`` の text をそのまま同梱する。render は snapshot
  のみに依存する (DB 非参照) ため、head 本体の凍結 (refresh_on_events 空 =
  Metabolism まで再 capture しない) を破らずに「head に入るときと寸分たがわぬ
  断片」が得られる。**別文面を書かない** — 書き分けた瞬間からドリフトが始まる。
- **配送は outbox 経由** (execution_ledger): begin_execution →  mark_running →
  mark_applied(outbox_items=[perception.push], deliver=True)。知覚バッファ →
  SAIMemory は persona 共有の履歴ストリームなので、push は全 Session の窓に届く。
- **push 確定後に B (last_notified) を該当 section だけ全 Session 行で前進**
  (:meth:`HeadPipeline.advance_last_notified`)。前進させないと backstop
  flush_diffs が同じ変化を再通知する。diff 検出自体は消さない — tool 外の変化は
  引き続き backstop が拾う。

通知は世界の副作用であってツール本体の結果ではない — 本モジュールのヘルパーは
失敗しても例外を呼び出し元に伝播させない (WARN に落とす)。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sea.head_pipeline.integration import build_line_head_input
from sea.head_pipeline.pipeline import HeadPipeline, get_default_pipeline

LOGGER = logging.getLogger(__name__)

#: 実行台帳に刻む実行種別。
MUTATION_NOTIFY_KIND = "head.mutation_notify"

#: 知覚バッファ上の kind。
PERCEPTION_KIND = "head_mutation"


def notify_head_mutation(
    persona: Any,
    manager: Any,
    building_id: Optional[str],
    section_name: str,
    *,
    operation_label: str,
    pipeline: HeadPipeline | None = None,
) -> None:
    """head 操作の内容型通知を発行する (操作ツールの成功点から呼ぶ)。

    通知本文 = ``operation_label`` (何をしたか 1 行) + section の render text
    (head と同一の断片)。render が None (例: memopedia_index の opt-in OFF) の
    場合は通知しない — head に無いものの変化は通知対象外。

    配送は ``manager.execution_ledger`` の outbox (target='perception.push')。
    台帳が無い環境 (旧テスト等) は ``persona.sai_memory.push_perception`` へ
    degrade する。push 確定後、B (last_notified) を該当 section だけ全 Session
    行で前進させる。

    本関数は決して raise しない (通知の失敗はツール本体の成功を壊さない)。
    """
    try:
        _notify_head_mutation_inner(
            persona, manager, building_id, section_name,
            operation_label=operation_label, pipeline=pipeline,
        )
    except Exception:
        LOGGER.warning(
            "head_mutation_notify: failed section=%s persona=%s (tool result unaffected)",
            section_name, getattr(persona, "persona_id", "?"), exc_info=True,
        )


def _notify_head_mutation_inner(
    persona: Any,
    manager: Any,
    building_id: Optional[str],
    section_name: str,
    *,
    operation_label: str,
    pipeline: HeadPipeline | None,
) -> None:
    pipeline = pipeline or get_default_pipeline()
    section = pipeline.registry.by_name(section_name)
    if section is None:
        LOGGER.warning(
            "head_mutation_notify: section %r not registered, skipped", section_name,
        )
        return

    # model_key 省略 = resolve_default_model_key フォールバック。capture / render
    # は model 非依存 (ctx の anchor-TTL メタは通知生成に効かない)。
    ctx = build_line_head_input(persona, manager, building_id)
    snapshot = section.capture(ctx)
    rendered = section.render(snapshot)
    if rendered is None or not (rendered.text or "").strip():
        LOGGER.debug(
            "head_mutation_notify: section=%s renders nothing, no notification",
            section_name,
        )
        return

    # 別文面を書かない — render text をそのまま同梱するのが §3.3 不変条件
    # 「寸分たがわず」の構造保証。
    content = f"{operation_label}\n\n{rendered.text}"
    persona_id = ctx.persona_id

    ledger = getattr(manager, "execution_ledger", None)
    if ledger is not None:
        execution_id, _created = ledger.begin_execution(
            MUTATION_NOTIFY_KIND, idempotency_key=None, persona_id=persona_id,
        )
        ledger.mark_running(execution_id)
        ledger.mark_applied(
            execution_id,
            result={"section": section_name, "operation": operation_label},
            outbox_items=[{
                "target": "perception.push",
                "persona_id": persona_id,
                "payload": {
                    "kind": PERCEPTION_KIND,
                    "content": content,
                    "reduce_key": f"head_mutation:{section_name}",
                    "salient": False,
                    "media": [],
                    "metadata": json.dumps(
                        {"section": section_name}, ensure_ascii=False,
                    ),
                },
            }],
            deliver=True,
        )
        LOGGER.info(
            "head_mutation_notify: queued via ledger section=%s persona=%s execution=%s",
            section_name, persona_id, execution_id,
        )
    else:
        # 台帳が無い環境 (旧テスト等): 直接 push に degrade。配達保証は無い。
        sai_mem = getattr(persona, "sai_memory", None)
        if sai_mem is None or not sai_mem.is_ready():
            LOGGER.warning(
                "head_mutation_notify: no ledger and no ready SAIMemory, "
                "notification dropped section=%s persona=%s",
                section_name, persona_id,
            )
            return
        sai_mem.push_perception(
            PERCEPTION_KIND, content,
            reduce_key=f"head_mutation:{section_name}",
            salient=False,
            media=[],
            metadata=json.dumps({"section": section_name}, ensure_ascii=False),
        )
        LOGGER.warning(
            "head_mutation_notify: delivered via direct push_perception "
            "(no execution ledger on manager) section=%s persona=%s",
            section_name, persona_id,
        )

    # push 確定後に B を前進 — 知覚バッファ → SAIMemory は persona 共有の
    # 履歴ストリームで push は全 Session の窓に届くため、全 model 行を進める。
    pipeline.advance_last_notified(persona_id, section_name, snapshot)


def notify_head_mutation_from_tool_context(
    section_name: str,
    *,
    operation_label: str,
) -> None:
    """ツール実行中の contextvars から persona / manager を解決して通知する。

    各 memory 系スペル (builtin_data/tools/memory_*.py) の成功 return 直前から
    呼ぶ薄い便宜口。persona が解決できない環境
    (CLI 直叩き / persona 未ロードの fallback adapter 経路) は黙って skip する
    — その環境には届け先の窓が無い。決して raise しない。
    """
    try:
        from tools.context import get_active_manager, get_active_persona_id

        persona_id = get_active_persona_id()
        manager = get_active_manager()
        if not persona_id or manager is None:
            LOGGER.debug(
                "head_mutation_notify: no active persona/manager context, skipped "
                "(section=%s)", section_name,
            )
            return
        persona = (getattr(manager, "personas", None) or {}).get(persona_id)
        if persona is None:
            LOGGER.debug(
                "head_mutation_notify: persona %s not loaded on manager, skipped",
                persona_id,
            )
            return
        building_id = getattr(persona, "current_building_id", None)
        notify_head_mutation(
            persona, manager, building_id, section_name,
            operation_label=operation_label,
        )
    except Exception:
        LOGGER.warning(
            "head_mutation_notify: tool-context resolution failed section=%s",
            section_name, exc_info=True,
        )
