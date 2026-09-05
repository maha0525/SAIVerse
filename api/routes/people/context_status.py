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

    - ``watermarks``: 実効 model の二水位 (model 定義から解決)。model が水位を
      持たない (null 設定) 場合は None = Metabolism を持たない。
    - ``presented_chars``: 次の user Pulse で実際に送られる提示コンテキストの
      文字数。§15 読み戻しが適用されるならその適用後 (プレビューと一致)。
      anchor 未確立 (まだ会話が始まっていない) や persona 未ロードでは None。
      **保存済みの行に加えて、送信直前に差し込まれる知覚 (部屋の様子) を
      含む合計**で、内訳は ``stored_chars`` (会話) / ``mechanism_chars``
      (スペル結果などの機構名義の行) / ``injected_perception_chars``
      の三分割 (2026-09-02 / 2026-09-04 まはー裁定 —
      docs/issues/context_accounting_excludes_injected_rows.md /
      watermarks_unsatisfiable_when_perception_is_large.md 裁定 10)。上限
      (``high_chars``) と比べる量はこの合計。
    - ``window_rows_chars``: 残す量 (``target_chars``) の契約が見ている量 =
      整理が計画を立てる窓 (§15-3 印戻しなどの正規化後。読み戻しが適用される
      なら読み戻し後の窓) の**会話の行だけ**の文字数 (``EvictionPlan.stored_chars``)。
      残す量の主語は会話の行で、知覚 (2026-09-03 裁定) も機構名義の行
      (2026-09-04 裁定) も消費しない
      (docs/issues/protection_quota_consumed_by_perception_blocks.md)。
      ``EvictionPlan.protected_rows_chars`` (保護範囲の**内側**の行の字数) とは
      別の量なので、名前も分けてある。測れないときは None。
    - ``perception_over_budget``: 合計は上限を超えているのに会話の行が残す量
      以下 = 整理しても畳めるものが無い (超過の主は知覚の供給)。判定の合計と
      行は**同じ窓** (``window_rows_chars`` と同じ計画窓に知覚を足した列) から
      取る — 別の窓の合計と行を突き合わせると、窓の正規化の前後で旗が裏返る
      (f288f003 の Codex レビュー残 #2)。``presented_chars`` は「いま実際に送る
      合計」で、正規化前の窓の値なので、計画窓の合計とは違いうる。測れない
      ときは None。
    - ``fold_unit_chars``: 一次あらすじの標準被覆 U。整理は残す量より古い側を
      U (材料字数 — 2026-08-29 裁定) ずつの範囲に刻んで畳む。
    - ``fold_ready`` / ``fold_shortfall_chars``: いま手動の畳みで実際に fold が
      閉じるか、閉じないなら畳める範囲の材料があと何字たまれば閉じるか。
      U 判定が材料字数になったため、生の超過と U の比較では「実行できるか」を
      言えない — 実際の計画 (``sea/eviction_plan.py::plan_eviction``、純関数)
      を dry に呼んで判定し、画面側に算数を再実装させない。presented を
      測れないときは None。
    - 一切の行を書かない (resolve は persist_advance=False)。
    """
    from sai_memory.arasuji.alignment import chronicle_band_budget

    details = manager.get_ai_details(persona_id)
    if not details:
        raise HTTPException(status_code=404, detail="Persona not found")

    persona = manager.personas.get(persona_id)
    model_key = getattr(persona, "model", None) or details.get("DEFAULT_MODEL") or None

    status: dict[str, Any] = {
        "persona_id": persona_id,
        "model": model_key,
        "metabolism": False,          # 実効 model が水位を持つか
        "target_chars": None,         # 残す量 (整理後にここへ揃える。anchor 未確立時の初期読み込み量も兼ねる)
        "high_chars": None,           # 上限 (超えたら整理が走る)
        "presented_chars": None,      # 実際に送られる合計文字数 (読み戻し後)
        "stored_chars": None,         # うち会話の行 (機構名義の行は含まない)
        "mechanism_chars": None,      # うち機構名義の行 (スペル結果・通知、生)
        "injected_perception_chars": None,  # うち送信直前に差し込まれる知覚
        "window_rows_chars": None,  # 残す量の契約が見る量 (計画窓の会話の行だけ)
        "perception_over_budget": None,  # 合計は上限超えだが行は残す量以下 (畳めない)
        "fold_unit_chars": chronicle_band_budget(),  # 一度に畳む単位 U (env 由来の定数)
        "fold_ready": None,           # いま畳みで fold が実際に閉じるか (測れなければ None)
        "fold_shortfall_chars": None,  # 閉じないとき、あと材料何字で閉じるか (閉じるなら 0)
        "refill_applied": False,      # presented_chars が §15 読み戻し込みの値か
        "measurement_failed": False,  # 計測が失敗した (None を「起点なし」と読ませない)
        # 最終防衛ライン (ensure_window_floor) がこのプロセスで最後に発火した
        # 時刻 (ISO)。無ければ None。発火 = 上流 (読み戻し) の失敗の印
        # (docs/issues/window_floor_and_refill_redesign.md 設計 0)。
        "window_floor_applied_at": None,
    }
    if not model_key:
        return status

    lifecycle = _resolve_lifecycle(manager)
    if lifecycle is None:
        return status
    status["window_floor_applied_at"] = lifecycle.window_floor_applied_at(
        persona_id, model_key,
    )

    try:
        watermarks = lifecycle.get_metabolism_watermarks(
            persona, model_key, persona_id=persona_id,
        )
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
            _record_presented_chars(
                status, lifecycle, persona,
                refill_plan["presented"], refill_plan.get("new_anchor_id"),
            )
            status["refill_applied"] = True
            _measure_fold_readiness(
                status,
                lifecycle.presented_with_perceptions(
                    persona, refill_plan["presented"],
                    refill_plan.get("new_anchor_id"), raise_on_error=True,
                ),
                watermarks,
            )
            return status

        anchor_id, _resolution = lifecycle.resolve_metabolism_anchor(
            persona, model_key=model_key, persist_advance=False,
        )
        if not anchor_id:
            return status  # 起点未確立 (ブートストラップ前)
        window = lifecycle.get_presented_window(persona, model_key, anchor_id)

        # 表示する送信量は「いまの提示そのまま」(読み戻しで生に開いた区間は
        # 生のまま送られる) — 正規化前の値。
        _record_presented_chars(
            status, lifecycle, persona, window.presented, anchor_id,
        )
        # 「畳めるか」の下見は、本走行 (run_metabolism) が退場計画の前に通す
        # 窓の正規化 (恒久欠落 fold の除外 → §15-3 印戻し) を書き込みなしで
        # 再現した窓で測る。素の窓で測ると、読み戻しで生に開いた区間のある
        # 窓で下見と本走行が別の答えを出す — 8/24 に潰した「押せたのに何も
        # 起きない」の再発口 (Codex 指摘 2026-08-29)。
        planning_window, refold_ranges = lifecycle.preview_planning_window(
            persona, model_key, window, watermarks, raise_on_error=True,
        )
        _measure_fold_readiness(
            status,
            lifecycle.presented_with_perceptions(
                persona, planning_window.presented, anchor_id,
                raise_on_error=True,
            ),
            watermarks,
            refold_ranges=refold_ranges,
        )
    except Exception:
        # 測れないことは水位表示を妨げない — ただし「まだ起点が無い」(正常) と
        # 区別できるよう、障害であることを明示して返す (Codex 指摘 2026-07-30)。
        LOGGER.warning(
            "context-status: presented-window measurement failed for %s/%s",
            persona_id, model_key, exc_info=True,
        )
        status["measurement_failed"] = True
    return status


def _record_presented_chars(
    status: dict[str, Any], lifecycle: Any, persona: Any,
    presented: list, anchor_id: Any,
) -> None:
    """送信量の内訳 (会話 / 機構名義の行 / 差し込みの知覚 / 合計) を status に載せる。

    ``presented_chars`` は**合計** = 実際に送られる中身の字数。保存行だけを
    数えていた頃は、本番エリスで勘定 149,856 字に対し実送信 209,031 字という
    乖離が出ていた (docs/issues/context_accounting_excludes_injected_rows.md) —
    「データ送信量の管理」は透明性の看板なので、合計を出す。内訳は三分割
    (2026-09-04 まはー裁定): ``stored_chars`` (会話の行、生) /
    ``mechanism_chars`` (スペル結果・通知など機構名義の保存行、生 — 残す量の
    勘定には入らないが合計には生で入る) / ``injected_perception_chars``
    (送信直前に差し込まれる部屋の様子)。
    """
    from sea.eviction_plan import message_chars, stored_message_chars

    rows = stored_message_chars(presented)      # 会話の行だけ (残す量の主語)
    stored_rows_total = message_chars(presented)  # 保存行の生の合計
    # raise_on_error: 知覚一覧の内部失敗を「知覚 0 (正常)」として表示しない —
    # ここは透明性の画面なので、失敗は上の except → measurement_failed に落とす
    # (preview_refilled_history の raise_on_error と同じ型。Codex 指摘 2026-09-02)。
    total = message_chars(
        lifecycle.presented_with_perceptions(
            persona, presented, anchor_id, raise_on_error=True,
        )
    )
    status["stored_chars"] = rows
    status["mechanism_chars"] = stored_rows_total - rows
    status["injected_perception_chars"] = max(0, total - stored_rows_total)
    status["presented_chars"] = total


def _measure_fold_readiness(
    status: dict[str, Any], presented: list, watermarks: Any,
    refold_ranges: int = 0,
) -> None:
    """「いま畳めるか」「あと材料何字で畳めるか」を実際の退場計画で測る。

    U 判定は材料字数 (2026-08-29 まはー裁定) — 長い機構名義の行 (スペル結果等)
    は材料を組む時だけ決定論の一行に縮むため、生の超過と U の比較は判定として
    嘘になる。実行時と同じ純関数 ``plan_eviction`` を dry に呼び、fold の有無と
    「閉じられなかった端数の材料字数」から出す (画面側に算数を再実装させない)。

    - ``fold_ready``: 手動の畳みが実際に何かをする — 計画に fold が 1 つ以上
      あるか、または §15-3 印戻し (``refold_ranges`` > 0) だけで提示が減る
      (本走行は退場計画の前に印戻しを通すので、これも「畳みが起きる」)。
    - ``fold_shortfall_chars``: fold が無いとき、端数の材料が U に届くまでの
      残り字数。会話は末尾 (保護範囲) に積もり、その分だけ古い側が畳み候補へ
      押し出されるので、「あと約この字数の会話がたまれば畳める」の目安になる。
      提示が残す量以下 (畳み候補ゼロ) のときも U がそのまま出るが、その場合の
      文言は overflow 側の分岐が受け持つ (frontend)。

    ``presented`` は本走行と同じ正規化 (preview_planning_window) を通した窓に、
    本走行と同じく知覚ブロックを時刻順マージした列を渡すこと — 下見と本走行が
    同じ形の窓を planner に見せるための契約 (組成が片方だけズレると「押せたのに
    何も起きない」が戻る)。
    """
    from sea.eviction_plan import plan_eviction

    band = _band_budget_from_status(status)
    plan = plan_eviction(
        list(presented), set(), watermarks, target_chars=band,
    )
    ready = (not plan.is_empty) or refold_ranges > 0
    status["fold_ready"] = ready
    status["fold_shortfall_chars"] = (
        0 if ready
        else max(0, band - plan.pending_material_chars)
    )
    # 残す量の契約が見ている量 = 計画窓の会話の行だけ (知覚も機構名義の行も
    # 残す量を消費しない — 2026-09-03 / 2026-09-04 裁定)。
    # 合計が上限を超えているのに行が残す量以下なら、
    # 整理は畳めるものを見つけられない — 超過の主は知覚の供給で、画面はその
    # 事実を出す (本走行は同じ判定で LLM を呼ばず引き返す)。判定の合計と行は
    # **この同じ列** (計画窓 + 知覚) から取る — 正規化前の窓の合計 (presented_
    # chars) と計画窓の行を突き合わせると、印戻しで窓が縮む回に旗が裏返る。
    from sea.eviction_plan import message_chars

    status["window_rows_chars"] = plan.stored_chars
    total = message_chars(list(presented))
    high = getattr(watermarks, "high", None)
    status["perception_over_budget"] = bool(
        high is not None and total > high and plan.stored_chars <= watermarks.target
    )


def _band_budget_from_status(status: dict[str, Any]) -> int:
    """status に既に載せた U を読む (env 再読込との食い違いを作らない)。"""
    value = status.get("fold_unit_chars")
    if isinstance(value, int) and value > 0:
        return value
    from sai_memory.arasuji.alignment import chronicle_band_budget
    return chronicle_band_budget()


def _resolve_lifecycle(manager: Any):
    """SessionLifecycle を manager からたどる (cache_status.py と同じ経路)。"""
    runtime = getattr(manager, "sea_runtime", None) or getattr(manager, "runtime", None)
    if runtime is None:
        return None
    return getattr(runtime, "session_lifecycle", None)
