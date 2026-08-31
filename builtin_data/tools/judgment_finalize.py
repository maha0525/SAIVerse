"""judgment_finalize: 判断点 (judgment_points.md) の後処理ツール。

判断点 Playbook (judgment_day_open / judgment_post_session / judgment_on_event /
judgment_day_close) の最終ノードで呼ばれる。kind ディスパッチ:

1. judge LLM ノードが返した dict (構造化出力結果) を受け取る
2. kind に応じて検証・適用する。不正な項目は **該当項目だけ棄却 + WARN**
   (判断全体を落とさない。握り潰さない)
   - day_open: timetable 検証 → save_day_plan + schedule_day_plan
   - post_session: task_verdict 適用 (done は artifact_ref の接地検証つき) /
     desk_memo → 独白記録のみ / remaining_timetable → 残りコマの全置換
   - on_event: reaction (engage_now は結果への反映のみ — 応対の起動は
     呼び出し側の責務 / insert_slot は時刻整合検証つき挿入 / note_only は
     plan meta への覚え書き / ignore は記録のみ)。alert では engage_now 以外を
     棄却 (スキーマ縮退の二重ガード)
   - day_close: tomorrow_memo + day_theme + user_report_seeds → plan meta

会話終了判断 (post_conversation) の適用は 2026-08-16 の裁定で退役した
(autonomous_behavior_v3.md §8 / §13.3)。欲求まわりの適用 (promotions /
new_desires / desire_reviews) と track_op も、Track 操作スペルと欲求プールの
退役で機構ごと消えた。
3. 整形済みテキスト ``monologue + 適用結果の要約行`` を SAIMemory に
   ``role='assistant', line_role='meta_judgment'`` で保存する。
   メインキャッシュに LLM の生 JSON は残らない (不変条件 v2-A 継承)。

W1 Chunk B (A8/A9/A11): ``manager.execution_ledger`` と
``judgment_context.execution_id`` が両方あるとき (tracked) は実行台帳フロー —
入口で台帳 status=running を検査して再 finalize の二重適用を封じ、SAIMemory
判断行は直書きでなく ``mark_applied`` の outbox (``saimemory.append``) に凍結
する (配送失敗は「適用済み・記録待ち」として pending に残り関所/回復 tick が
引き継ぐ)。台帳が無い環境 (旧テスト・mock シム) は従来の直書きに degrade する。

⚠ **判断が直接スペルを撃つ経路は束 6c (2026-08-22) で消えた。** 唯一の実行口
だった ``_fire_spell`` の呼び手が Track 操作と欲求プールの退役で全滅したため、
関数ごと撤去した。「失敗した spell を本人の /spell 行にせず、システム名義の適用
失敗通知 (``perception.push``) で届ける」という A11/不変条件 7 の規律は、供給源が
消えたので発火する場面が無くなった — 規律そのものは正しいので、判断がまたスペルを
撃つ形になったときはここへ書き戻す。

W1 Chunk C (D9): post_session は digest 統合 (judgment_points.md §6 改定) —
judge の構造化出力 ``digest`` 欄 (required) から SAIMemory ダイジェスト行
(``sea.work_session.DIGEST_TAG`` / main_line / committed) を組み、tracked では
outbox の**第 1 項目** (``saimemory.append_digest``、判断行より先) として配送する。
untracked は直書きに degrade する。出来事へ再訪の鍵を刻む後段は束 6c
(2026-08-22) で退役した (v3 §7 — 専用の記録行を持たない)。

詳細: ``docs/intent/persona_cognition/judgment_points.md`` /
``docs/handoff/2026-07-19_w1_judgment_ledger_handoff.md`` D6〜D10
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from saiverse import clock
from saiverse import day_plan as day_plan_mod
from saiverse.judgment_points import (
    KIND_DAY_CLOSE,
    KIND_DAY_OPEN,
    KIND_ON_EVENT,
    KIND_POST_SESSION,
    REACTION_ENGAGE_NOW,
    REACTION_IGNORE,
    REACTION_INSERT_SLOT,
    REACTION_NOTE_ONLY,
    insert_timetable_slot,
    normalize_task_ref,
    sanitize_timetable,
)
from saiverse.persona_task_manager import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    PersonaTaskManager,
    TaskNotFoundError,
)
from tools.context import (
    get_active_manager,
    get_active_persona_id,
    get_active_pulse_context,
)
from tools.core import ToolResult, ToolSchema

LOGGER = logging.getLogger("saiverse.tools.judgment_finalize")

# 適用失敗のシステム通知 (perception.push) で使う、ユーザー向けの判断名。
_KIND_LABELS = {
    KIND_DAY_OPEN: "起床判断",
    KIND_POST_SESSION: "セッション終了判断",
    KIND_ON_EVENT: "イベント到着判断",
    KIND_DAY_CLOSE: "就寝判断",
}


# ---------------------------------------------------------------------------
# 表示用ヘルパ (番号だけの参照/コマに人が読める表題を添える)
# ---------------------------------------------------------------------------

# episode は title 列を持たない (kind + 出来事)。棚入れ記録では kind を添える。
_EPISODE_KIND_LABELS = {
    "conversation": "会話",
    "work_session": "作業セッション",
    "slot": "コマ",
    "presence": "在室",
    "stroll": "散策",
    "other": "その他",
}


def _normalize_artifact_ref(manager: Any, persona_id: str, ref: str) -> str:
    """成果物参照 (``item:N`` / 生 Item ID) を Item ID に正規化する。

    セッションの成果物リスト (``_collect_new_item_ids``) は生 Item ID を持つ
    一方、ペルソナが目にする成果物参照は世界の表示語彙 ``item:N`` である
    (document_create の戻り値・知覚通知・head の所持品欄すべて)。書式のまま
    突き合わせると、実際に作った成果物が「やったフリ」として棄却され、
    タスクが完了しないまま WARNING だけが残る。

    解決できないときは元の文字列を返す — ここは接地検証の前処理であって
    検証そのものではない。存在しない ref は後段の照合で落ちるべき。
    """
    ref = (ref or "").strip()
    if not ref:
        return ref
    resolver = getattr(manager, "resolve_item_ref_for_persona", None)
    if resolver is None:
        return ref
    try:
        return resolver(persona_id, ref)
    except Exception:
        LOGGER.debug(
            "[judgment_finalize] artifact_ref を解決できず素通し: persona=%s ref=%s",
            persona_id, ref, exc_info=True,
        )
        return ref


def _ref_label(manager: Any, persona_id: str, ref: str) -> str:
    """統一参照 (task:N / episode:N) に人が読める表題を添える。

    「番号だけ」の参照 (task:4) を独白/適用サマリに残すと、あとで読むまはーにも
    ペルソナ自身にも中身が分からない (ユーザー向け表示の原則)。解決できたら
    ``task:4「タスク名」`` の形に、失敗したら素の ref を返す (表題は装飾であって
    記録の骨は ref — 解決失敗で記録を落とさない)。

    ⚠ ``track:N`` の解決は束 6c (2026-08-22) で削除した (Track の退役)。旧データの
    ``track:N`` は表題なしの素の文字列へ縮退する。
    """
    ref = (ref or "").strip()
    if not ref:
        return ref
    try:
        if ref.startswith("task:"):
            ptm = PersonaTaskManager(manager.SessionLocal)
            task = ptm.get_task(
                ptm.resolve_task_ref(persona_id, normalize_task_ref(ref)),
                persona_id=persona_id,
            )
            title = (task.get("title") or "").strip()
            if title:
                return f"{ref}「{title}」"
        elif ref.startswith("episode:"):
            from saiverse import episodes as _episodes
            ep = _episodes.get_by_ref(manager, persona_id, ref)
            label = _EPISODE_KIND_LABELS.get(ep.get("kind"))
            if label:
                return f"{ref}（{label}）"
    except Exception:
        LOGGER.debug(
            "[judgment_finalize] _ref_label failed for %r", ref, exc_info=True,
        )
    return ref


def _format_slot_line(s: Dict[str, Any]) -> str:
    """時間割コマ 1 行の表示 (day_open / remaining_timetable で共通)。"""
    return (
        f"  {s['start']} {s['kind']}"
        + (f"「{s['title']}」" if s.get("title") else "")
        + (f" {s['ref']}" if s["ref"] != "none" else "")
        + f" @{s['facility']}"
        + (f"（{s['note']}）" if s["note"] else "")
    )


# ---------------------------------------------------------------------------
# day_open
# ---------------------------------------------------------------------------


def _finalize_day_open(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """起床判断の適用。timetable を保存できたら True (committed)。"""
    applied = False
    plan_date = ctx.get("plan_date") or clock.now().date().isoformat()

    # --- timetable: 検証 → save_day_plan + schedule_day_plan -------------
    # plan_date を渡すのは並び順をライフ基準にするため (深夜跨ぎ対応)。
    slots, tt_warnings = sanitize_timetable(
        manager, persona_id, output.get("timetable"), plan_date
    )
    warnings.extend(tt_warnings)

    # --- 習慣テンプレート (時間割改修 T2、timetable_redesign.md §5.2) ------
    # テンプレートのあるペルソナは「組む」でなく「埋める」: LLM 出力は穴の値の
    # 供給源にすぎず、確定フィールドが食い違えばテンプレート値で矯正する。
    # 「枠は LLM 出力で直接変わらない」(intent §9-1) をプロンプトへの信頼で
    # なくこの構造で保証する。過ぎたテンプレートコマは「流れた」帳簿
    # (ledger_prefix 区間) として先頭に置く (§11-12)。テンプレ未設定 (または
    # 無効) のペルソナは template=None → 従来どおりの全生成 (移行の安全弁)。
    template = None
    template_corrections: List[str] = []
    ledger_prefix = 0
    try:
        from saiverse.timetable_template import (
            compose_timetable_from_template,
            get_active_template,
        )
        template = get_active_template(manager, persona_id, plan_date)
    except Exception:
        LOGGER.exception(
            "[judgment_finalize] failed to load timetable template; "
            "falling back to free composition"
        )
        template = None
    if template is not None:
        ledger, pending, template_corrections = compose_timetable_from_template(
            manager, persona_id, plan_date, template["slots"], slots,
        )
        slots = ledger + pending
        ledger_prefix = len(ledger)
        # 矯正は monologue (本人の独白) と別のログ = 適用サマリ (lines) と
        # サーバーログの両方へ残す。
        for note in template_corrections:
            LOGGER.info(
                "[judgment_finalize] template enforcement: persona=%s %s",
                persona_id, note,
            )

    # 予算合計の検証はソフト制約 (judgment_points.md §3.2)。超過は WARN のみで
    # 保存は通す — 予算ゲート (v2 §4.5) が発火時に日次残高でラウンドを切り詰める。
    # テンプレ経路では「流れた」帳簿区間 (発火しない) を合計に含めない。
    # 合計は保存値の素朴な和ではなく**実効値** (ゲート対象 kind のみ・0/未指定は
    # 実行時と同じ既定 8) — 表示とゲートの単位を揃える (Codex 三巡目)。
    daily_budget = ctx.get("daily_budget_rounds")
    if not isinstance(daily_budget, int) or isinstance(daily_budget, bool) or daily_budget <= 0:
        from saiverse.judgment_points import DEFAULT_DAILY_BUDGET_ROUNDS
        daily_budget = DEFAULT_DAILY_BUDGET_ROUNDS
    total = day_plan_mod.effective_budget_total(slots[ledger_prefix:])
    if total > daily_budget:
        warnings.append(
            f"作業コマの実効予算合計 {total} ラウンドが日次予算 {daily_budget} を"
            "超過 (保存は続行。発火時に残高で切り詰められます)"
        )

    if not slots:
        # 空配列は不可 — 最低 1 コマを要求する (judgment_points.md §4)。
        # 検証で全滅した場合も plan は保存しない (前日の plan / 既存 plan を壊さない)。
        # 却下はペルソナの文脈 (lines) にも載せる — 黙って捨てない
        # (_apply_remaining_timetable と同じ接地原則)。
        warnings.append(
            "timetable が検証後に空になったため、時間割は保存しませんでした"
        )
        lines.append(
            "（時間割は保存されませんでした: 提出されたコマがすべて無効でした。"
            "今日の時間割は編成されていません）"
        )
    else:
        # 全置換を原子的に (A1、docs/handoff/..._audit.md): 検証・ライフ範囲正規化を
        # 先に済ませ、通ってから旧予約 cancel → 保存 → 再 push する。検証が失敗
        # (ValueError) する場合は plan も EventScheduler 予約も一切変更されない
        # (旧実装の「先に cancel してから save が raise」で孤児化する順序を断つ)。
        try:
            pushed, range_notes = day_plan_mod.replace_day_plan(
                manager, persona_id, plan_date, slots,
                ledger_prefix=ledger_prefix,
            )
        except ValueError as exc:
            # 何も変更されていない — 旧 plan / 旧予約はそのまま残る。applied は
            # timetable 由来で True にしない。エコーを実状態 (既存を維持) に合わせる
            # (監査 A1「報告と実状態が一致しない」の是正)。
            warnings.append(
                f"提出された時間割が編成範囲に収まらなかったため既存の時間割を"
                f"維持しました（{exc}）"
            )
            lines.append(
                "（既存の時間割を維持しました（提出されたコマが編成範囲に"
                "収まりませんでした））"
            )
        else:
            # ライフの組織化範囲による丸め・部分救済 (life.md §3) で実際に
            # 保存されたコマ数・内容が sanitize 直後の slots と異なりうる —
            # 一覧は必ず保存済みの実データから組む (捏造を防ぐ)。
            saved_slots = day_plan_mod.load_day_plan(manager, persona_id, plan_date) or slots
            applied = True
            lines.append(
                f"（今日の時間割を編成: {len(saved_slots)} コマ、{pushed} コマを予約）"
            )
            for s in saved_slots:
                lines.append(_format_slot_line(s))
            for note in range_notes:
                lines.append(note)
            # テンプレート整合の矯正・流れたコマの記録 (T2)。monologue とは
            # 別の適用サマリとして、実際に保存されたときだけ載せる。
            for note in template_corrections:
                lines.append(note)
            # 日次予算台帳の初期化 (v2 §4.5)。total を書き、消費済み (used) は
            # 保持する — 発火時の予算ゲートがこの残高でラウンドを切り詰める。
            try:
                budget_state = day_plan_mod.init_budget_ledger(
                    manager, persona_id, plan_date, daily_budget,
                )
            except Exception as exc:
                LOGGER.exception("[judgment_finalize] init_budget_ledger raised")
                warnings.append(f"日次予算台帳の初期化に失敗: {exc}")
            else:
                budget_line = f"（今日の作業ラウンド予算: {budget_state['total']}"
                if budget_state["used"]:
                    budget_line += (
                        f"、消費済み {budget_state['used']}"
                        f"、残り {budget_state['remaining']}"
                    )
                lines.append(budget_line + "）")

    # v0.5 (life.md §3/§11.2): ライフはもう LLM が宣言しない — ユーザー設定
    # (PersonaSchedule の起床・就寝) からシステムが day_open 発火時に確定して
    # 焼く (呼び出し元は saiverse.autonomy_wiring.fire_judgment_point、
    # このツールが実行される前に済んでいる)。LLM 出力に紛れ込んだ "lives" は
    # 単に無視する (書ける口そのものが無くなった)。

    return applied


# ---------------------------------------------------------------------------
# 層2 棚入れ: episode_purposes → purpose_tags (life_concept_map.md §9.1)
# ---------------------------------------------------------------------------


def _write_shelving_tags(
    manager: Any,
    persona_id: str,
    pairs: List[Tuple[str, str]],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """検証済み (episode_ref, purpose_ref) ペア群を層2タグとして永続化する。

    書き込み口はペルソナの SAIMemory adapter (``add_purpose_tag``、memory.db
    相乗り)。adapter が未対応 (テスト・シムの軽量スタブ等) なら WARN して
    棄却する — 黙って捨てない。
    """
    if not pairs:
        return False
    from sai_memory.purpose_tags import LAYER_SHELVE

    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    add = getattr(adapter, "add_purpose_tag", None)
    if not callable(add):
        warnings.append(
            "episode_purposes: 記憶アダプタが目的タグ未対応のため保存できません"
        )
        return False
    applied = False
    by_episode: Dict[str, List[str]] = {}
    for episode_ref, purpose_ref in pairs:
        if add(target_ref=episode_ref, purpose_ref=purpose_ref, layer=LAYER_SHELVE):
            applied = True
            by_episode.setdefault(episode_ref, []).append(purpose_ref)
        else:
            warnings.append(
                f"episode_purposes: タグの保存に失敗 ({episode_ref} → {purpose_ref})"
            )
    for episode_ref, purposes in by_episode.items():
        ep_label = _ref_label(manager, persona_id, episode_ref)
        purpose_labels = ", ".join(
            _ref_label(manager, persona_id, p) for p in purposes
        )
        lines.append(
            f"（この出来事 {ep_label} を {purpose_labels} の棚に入れた）"
        )
    return applied


def _apply_episode_purposes_single(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """episode_purposes (purpose 参照の配列) の適用 — 対象の出来事は判断文脈で単一。

    post_session (セッション) が使う。enum 外 ref は該当項目だけ棄却 + WARN。
    """
    raw = output.get("episode_purposes")
    if not raw:
        return False
    if not isinstance(raw, list):
        warnings.append(
            f"episode_purposes rejected: 配列が必要 (got {type(raw).__name__})"
        )
        return False
    episode_ref = str(ctx.get("episode_ref") or "")
    if not episode_ref:
        warnings.append("episode_purposes rejected: 対象の出来事が不明です")
        return False
    valid = set(ctx.get("purpose_refs") or [])
    pairs: List[Tuple[str, str]] = []
    for i, ref in enumerate(raw):
        ref_str = str(ref or "").strip()
        if ref_str not in valid:
            warnings.append(
                f"episode_purposes[{i}] rejected: {ref_str!r} は選択可能な目的にありません"
            )
            continue
        pairs.append((episode_ref, ref_str))
    return _write_shelving_tags(manager, persona_id, pairs, lines, warnings)


def _apply_episode_purposes_pairs(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """episode_purposes ({episode, purpose} ペア配列) の適用 — day_close 用。"""
    raw = output.get("episode_purposes")
    if not raw:
        return False
    if not isinstance(raw, list):
        warnings.append(
            f"episode_purposes rejected: 配列が必要 (got {type(raw).__name__})"
        )
        return False
    valid_episodes = set(ctx.get("episode_refs") or [])
    valid_purposes = set(ctx.get("purpose_refs") or [])
    pairs: List[Tuple[str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"episode_purposes[{i}] rejected: not a dict")
            continue
        episode_ref = str(item.get("episode") or "").strip()
        purpose_ref = str(item.get("purpose") or "").strip()
        if episode_ref not in valid_episodes:
            warnings.append(
                f"episode_purposes[{i}] rejected: {episode_ref!r} は今日の出来事にありません"
            )
            continue
        if purpose_ref not in valid_purposes:
            warnings.append(
                f"episode_purposes[{i}] rejected: {purpose_ref!r} は選択可能な目的にありません"
            )
            continue
        pairs.append((episode_ref, purpose_ref))
    return _write_shelving_tags(manager, persona_id, pairs, lines, warnings)


# ---------------------------------------------------------------------------
# 共通適用部品 (post_session / on_event が共有)
# ---------------------------------------------------------------------------


def _apply_remaining_timetable(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """remaining_timetable: null=変更なし / 配列=残りコマの全置換 (§3.3)。

    却下 (全滅・置換失敗) は warnings (ログ) だけでなく **lines (ペルソナの
    文脈に乗る適用サマリ) にも必ず載せる** — 黙って捨てるとペルソナは
    「組み替えた」つもりのまま一日を続け、就寝判断が実態とズレた総括をする
    (接地原則違反。2026-07-05 実 LLM シム 3回目で実証)。
    """
    rt = output.get("remaining_timetable")
    if isinstance(rt, list):
        plan_date = ctx.get("plan_date") or clock.now().date().isoformat()
        if not rt:
            # 空配列は null と同じ「変更なし」として黙って扱う。空の時間割は
            # 不変条件 (最低 1 コマ) で保存できず、[] が有効な変更要求で
            # ありうる余地がゼロのため、この読み替えは無損失。実データでは
            # [] は「残りコマが現実に無い時点の判断」で事実の記述として
            # 出てくる (2026-07-18 観測: 却下 6 件全件がこのケース) — それを
            # 却下エコーで咎めるとペルソナの記憶に無意味な失敗文が積もる。
            LOGGER.debug(
                "[judgment_finalize] remaining_timetable=[] treated as no-change "
                "(persona=%s)", persona_id,
            )
        else:
            slots, rt_warnings = sanitize_timetable(manager, persona_id, rt, plan_date)
            warnings.extend(rt_warnings)
            if not slots:
                warnings.append(
                    "remaining_timetable が検証後に空になったため、時間割は変更しません"
                )
                lines.append(
                    "（時間割の変更は適用されませんでした: 提出されたコマが"
                    "すべて無効でした。今日の残りのコマは元のままです）"
                )
            else:
                try:
                    pushed, range_notes = day_plan_mod.replace_remaining_slots(
                        manager, persona_id, plan_date, slots
                    )
                except ValueError as exc:
                    warnings.append(
                        f"remaining_timetable の置換に失敗 (時間割は不変): {exc}"
                    )
                    lines.append(
                        f"（時間割の変更は適用されませんでした（{exc}）。"
                        "今日の残りのコマは元のままです）"
                    )
                else:
                    # ライフの組織化範囲による丸め・部分救済 (life.md §3) で
                    # 実際に保存された内容が sanitize 直後の slots と異なりうる —
                    # 一覧・件数は保存済みの実データ (status=pending 分) から組む。
                    saved_plan = day_plan_mod.load_day_plan(manager, persona_id, plan_date) or []
                    saved_new = [
                        s for s in saved_plan
                        if s.get("status") == day_plan_mod.STATUS_PENDING
                    ] or slots
                    lines.append(
                        f"（残りの時間割を組み替えた: {len(saved_new)} コマ、"
                        f"{pushed} コマを予約）"
                    )
                    for s in saved_new:
                        lines.append(_format_slot_line(s))
                    for note in range_notes:
                        lines.append(note)
                    dropped = len(rt) - len(saved_new)
                    if dropped > 0:
                        lines.append(
                            f"（うち {dropped} コマは無効または活動時間の外のため"
                            "除外されました）"
                        )
                    return True
    elif rt is not None:
        warnings.append(
            f"remaining_timetable rejected: 配列または null が必要 (got {type(rt).__name__})"
        )
    return False


# ---------------------------------------------------------------------------
# post_session
# ---------------------------------------------------------------------------


def _apply_task_verdict(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """task_verdict の適用。タスク完了 or 作業メモ保存があれば True。"""
    verdict = output.get("task_verdict")
    if not isinstance(verdict, dict):
        return False
    task_ref = ctx.get("task_ref")
    if not task_ref:
        warnings.append("task_verdict がありますが対象タスクが不明のため棄却")
        return False

    status = verdict.get("status")
    desk_memo = str(verdict.get("desk_memo") or "").strip()
    session_artifacts = [str(a) for a in (ctx.get("artifacts") or [])]

    ptm = PersonaTaskManager(manager.SessionLocal)
    try:
        task_id = ptm.resolve_task_ref(persona_id, normalize_task_ref(str(task_ref)))
        task = ptm.get_task(task_id, persona_id=persona_id)
    except TaskNotFoundError:
        warnings.append(f"task_verdict rejected: タスク {task_ref!r} が見つかりません")
        return False

    # 終了済みタスクへの再裁定は棄却する (二重ガード — スキーマ側でも終了済み
    # タスクには裁定欄を出さない: judgment_points.build_post_session_schema)。
    # 再 done を通すと artifact_refs への多重追記・completed_at の上書きが起きる
    # (2026-07-05 実 LLM シム 3回目 異常③)。desk_memo の保存もしない —
    # 終了済みタスクを「中断中の作業」に見せてしまうため。
    current_status = task.get("status")
    if current_status in (STATUS_COMPLETED, STATUS_CANCELLED):
        warnings.append(
            f"task_verdict rejected: タスク {task_ref} は既に {current_status} です"
            " (再裁定はできません)"
        )
        return False

    applied = False
    if status == "done":
        artifact_ref = str(verdict.get("artifact_ref") or "")
        # 接地検証は「同一性」で行う。session_artifacts は生 Item ID だが、
        # ペルソナが目にする成果物参照は世界の表示語彙 (``item:N``) なので、
        # 書式のまま突き合わせると本物の成果物が「やったフリ」に誤判定される。
        artifact_ref = _normalize_artifact_ref(manager, persona_id, artifact_ref)
        if artifact_ref and artifact_ref in session_artifacts:
            # 接地検証 OK: 完了 + 成果物参照を**単一トランザクション**でタスクに
            # 刻む (A9/D7: 旧 update_task_status → append_artifact_ref の 2 連
            # commit は後段失敗で「証拠のない completed」を確定させていた)。
            execution_id = str(ctx.get("execution_id") or "") or None
            try:
                ptm.complete_with_artifact(
                    task_id, artifact_ref,
                    persona_id=persona_id, actor="judgment_post_session",
                    execution_id=execution_id,
                    reason=f"session verdict: done (artifact={artifact_ref})",
                )
            except Exception as exc:
                # 全か無か: 失敗時はタスク不変 (completed になっていない)。
                # 該当項目だけ棄却 + WARN の原則で判断全体は落とさない。
                LOGGER.exception(
                    "[judgment_finalize] complete_with_artifact raised"
                )
                warnings.append(
                    f"タスク {task_ref} の完了適用に失敗 (タスクは不変): {exc}"
                )
            else:
                lines.append(
                    f"（タスク {task_ref} を完了にした。成果物: {artifact_ref}）"
                )
                applied = True
        else:
            # やったフリの棄却: 成果物リストに無い ref は完了させない。
            # continue 相当に降格 (タスクは動かさず、作業メモだけ残す)。
            warnings.append(
                f"task_verdict 'done' rejected: artifact_ref={artifact_ref!r} は"
                "このセッションの成果物リストにありません (タスクは完了させません)"
            )
            status = "continue"

    if status in ("continue", "blocked"):
        # 作業メモ (desk_memo) は独白記録にだけ残す。旧実装は Track の metadata へ
        # も保存していたが、その格納先を読む経路は既に到達不能で、2026-08-21 の
        # 会話経路の Track なし化に合わせて書き手ごと退役した
        # (track_retirement.md §2 住人 4 — 引っ越し先は中断中エピソードのしおり)。
        # continue / blocked の裁定の意味論 (タスクを動かさない) は変えていない。
        memo_label = "詰まり" if status == "blocked" else "続き"
        if desk_memo:
            lines.append(f"（作業メモ [{memo_label}]: {desk_memo}）")
    elif status not in ("done",):
        warnings.append(f"task_verdict rejected: 未知の status={status!r}")

    return applied


def _finalize_post_session(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """セッション終了判断の適用。何か 1 つでも適用したら True (committed)。"""
    applied = _apply_task_verdict(manager, persona_id, output, ctx, lines, warnings)

    # --- episode_purposes → 層2 棚入れタグ (§9.1) -------------------------
    applied |= _apply_episode_purposes_single(
        manager, persona_id, output, ctx, lines, warnings,
    )

    # --- remaining_timetable: null=変更なし / 配列=残りコマの全置換 ------
    applied |= _apply_remaining_timetable(
        manager, persona_id, output, ctx, lines, warnings,
    )

    return applied


# ---------------------------------------------------------------------------
# on_event
# ---------------------------------------------------------------------------


def _append_event_memo(
    manager: Any,
    persona_id: str,
    plan_date: str,
    memo_text: str,
    event_text: Optional[str],
) -> None:
    """note_only の覚え書きを plan meta (``event_memos`` 配列) に積む。

    作業メモ (Track metadata) 様式の記録先だが、イベントは Track に属さないため
    「その日」の付帯情報 (persona_day_plan.meta_json) を置き場にする。
    """
    entry = {
        "text": memo_text,
        "event": str(event_text or "")[:120],
        "at": clock.now().isoformat(timespec="seconds"),
    }

    # 追記は最新 meta の上で CAS の内側で行う (外で読んだ古い一覧に append した
    # 完成値を書くと、並走した別のメモ追記が失われる — day_plan 第七陣 P1 と同型)
    def _append(meta: dict) -> list:
        memos = meta.get("event_memos")
        memos = list(memos) if isinstance(memos, list) else []
        memos.append(entry)
        meta["event_memos"] = memos
        return memos

    day_plan_mod.mutate_plan_meta(
        manager, persona_id, plan_date, _append, context="event_memo",
    )


def _finalize_on_event(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
    summary_extras: List[str],
) -> bool:
    """イベント到着判断の適用。

    engage_now は状態を変えない — 「今すぐ応対する」の実行 (Pulse 起動 /
    Track alert 処理) は呼び出し側の責務で、ここでは判断結果を要約
    (summary_extras) と記録テキストに反映するのみ (本番の応対起動は
    saiverse/autonomy_wiring.py の handle_external_event が judgment_applied
    イベント経由で reaction を読んで行う)。
    """
    applied = False
    plan_date = ctx.get("plan_date") or clock.now().date().isoformat()
    is_alert = bool(ctx.get("is_alert"))

    reaction = output.get("reaction")
    rtype = reaction.get("type") if isinstance(reaction, dict) else None
    if not isinstance(reaction, dict) or not rtype:
        warnings.append(
            f"reaction rejected: type を持つ object が必要 (got {reaction!r})"
        )
    elif is_alert and rtype != REACTION_ENGAGE_NOW:
        # スキーマ縮退 (engage_now のみ) の二重ガード
        warnings.append(
            f"reaction rejected: alert イベントでは engage_now のみ選べます "
            f"(got {rtype!r})"
        )
    elif rtype == REACTION_ENGAGE_NOW:
        summary_extras.append("reaction=engage_now")
        lines.append("（このイベントに今すぐ応対する）")
        applied = True
    elif rtype == REACTION_INSERT_SLOT:
        summary_extras.append("reaction=insert_slot")
        slot = reaction.get("slot")
        if not isinstance(slot, dict):
            warnings.append("insert_slot rejected: slot (object) がありません")
        else:
            pushed, insert_warnings = insert_timetable_slot(
                manager, persona_id, plan_date, slot,
                not_before=clock.now().strftime("%H:%M"),
            )
            warnings.extend(insert_warnings)
            if pushed is not None:
                applied = True
                lines.append(
                    f"（このイベントのためのコマを時間割へ挿入: "
                    f"{slot.get('start')} {slot.get('kind')}）"
                )
    elif rtype == REACTION_NOTE_ONLY:
        summary_extras.append("reaction=note_only")
        memo_text = str(reaction.get("memo") or "").strip()
        if not memo_text:
            warnings.append("note_only rejected: memo が空です")
        else:
            try:
                _append_event_memo(
                    manager, persona_id, plan_date, memo_text,
                    ctx.get("event_text"),
                )
                applied = True
                lines.append(f"（覚え書きに留める: {memo_text}）")
            except Exception as exc:
                LOGGER.exception("[judgment_finalize] event memo append raised")
                warnings.append(f"覚え書きの保存に失敗: {exc}")
    elif rtype == REACTION_IGNORE:
        summary_extras.append("reaction=ignore")
        lines.append("（このイベントには反応しない）")
    else:
        warnings.append(f"reaction rejected: 未知の type {rtype!r}")

    return applied


# ---------------------------------------------------------------------------
# curation_reviews (P4-a 編纂裁定)
# ---------------------------------------------------------------------------


def _apply_curation_reviews(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """curation_reviews → approve 分を編纂プランとして永続化する (P4-a)。

    - approve: ペルソナの memory.db の curation_plans テーブルに pending 行を積む。
      同じ op_id の pending が既にあれば重複挿入しない（冪等）。
    - skip: 何もしない（条件が続けば翌日以降に再提示）。

    **実際の分割・統合（書き換え）は P4-a2 の curation_ops.py で実装する。
    このモジュールは「プランを積む」だけで実行しない。**
    """
    raw = output.get("curation_reviews")
    if not raw:
        return False
    if not isinstance(raw, list):
        warnings.append(
            f"curation_reviews rejected: 配列が必要 (got {type(raw).__name__})"
        )
        return False

    # ctx に格納された候補リストから有効な op_id を収集
    candidates = ctx.get("curation_candidates") or []
    valid_ops: Dict[str, Any] = {}  # op_id → candidate dict
    for c in candidates:
        oid = c.get("op_id")
        if oid:
            valid_ops[oid] = c

    if not valid_ops:
        warnings.append(
            "curation_reviews: 候補が不明のため適用できません (judgment_context に "
            "curation_candidates がありません)"
        )
        return False

    # adapter / conn の取得
    persona_obj = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona_obj, "sai_memory", None) if persona_obj else None
    mem_conn = getattr(adapter, "conn", None) if adapter else None
    if mem_conn is None:
        warnings.append(
            "curation_reviews: 記憶アダプタが利用できないため編纂プランを保存できません"
        )
        return False

    from sai_memory.curation_ops import enqueue_plan

    applied = False
    for i, review in enumerate(raw):
        if not isinstance(review, dict):
            warnings.append(f"curation_reviews[{i}] rejected: not a dict")
            continue
        op_id = str(review.get("op_id") or "").strip()
        verdict = str(review.get("verdict") or "").strip()
        if op_id not in valid_ops:
            warnings.append(
                f"curation_reviews[{i}] rejected: op_id={op_id!r} は"
                "今日の棚の乱れ候補にありません"
            )
            continue
        if verdict not in ("approve", "skip"):
            warnings.append(
                f"curation_reviews[{i}] rejected: verdict={verdict!r} は"
                " approve または skip が必要です"
            )
            continue
        if verdict == "skip":
            # skip は何もしない（翌日再提示）
            LOGGER.debug(
                "[judgment_finalize] curation_reviews: op_id=%r skipped (persona=%s)",
                op_id, persona_id,
            )
            continue

        # approve → プランをキューに積む
        cand = valid_ops[op_id]
        try:
            plan_id = enqueue_plan(
                conn=mem_conn,
                kind=cand.get("kind", "split"),
                op_id=op_id,
                refs=list(cand.get("refs") or []),
            )
            applied = True
            lines.append(
                f"（棚の整理を予定に入れた: {op_id}）"
            )
            LOGGER.info(
                "[judgment_finalize] curation_reviews: op_id=%r enqueued "
                "as plan_id=%s (persona=%s)",
                op_id, plan_id, persona_id,
            )
        except Exception as exc:
            LOGGER.exception(
                "[judgment_finalize] curation_reviews: enqueue_plan raised "
                "(op_id=%r, persona=%s)", op_id, persona_id,
            )
            warnings.append(
                f"curation_reviews[{i}] の編纂プラン保存に失敗: {exc}"
            )

    return applied


# ---------------------------------------------------------------------------
# naming_reviews (P4-b 命名裁定)
# ---------------------------------------------------------------------------


def _apply_naming_reviews(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """naming_reviews → verdict=name の候補をテーマページとして即時作成する (P4-b)。

    - name: create_theme_page を即時呼び出しテーマページを作成する（ゼロコール）。
      name フィールドが欠落 / 空なら warning + skip。
    - skip: 何もしない（条件が続けば翌日以降に再提示）。
    """
    raw = output.get("naming_reviews")
    if not raw:
        return False
    if not isinstance(raw, list):
        warnings.append(
            f"naming_reviews rejected: 配列が必要 (got {type(raw).__name__})"
        )
        return False

    # ctx に格納された候補リストから有効な cluster_id を収集
    candidates = ctx.get("naming_candidates") or []
    valid_clusters: Dict[str, Any] = {}  # cluster_id → candidate dict
    for c in candidates:
        cid = c.get("cluster_id")
        if cid:
            valid_clusters[cid] = c

    if not valid_clusters:
        warnings.append(
            "naming_reviews: 候補が不明のため適用できません (judgment_context に "
            "naming_candidates がありません)"
        )
        return False

    # adapter / conn の取得
    persona_obj = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona_obj, "sai_memory", None) if persona_obj else None
    mem_conn = getattr(adapter, "conn", None) if adapter else None
    if mem_conn is None:
        warnings.append(
            "naming_reviews: 記憶アダプタが利用できないためテーマページを作成できません"
        )
        return False

    from sai_memory.theme_pages import create_theme_page

    applied = False
    for i, review in enumerate(raw):
        if not isinstance(review, dict):
            warnings.append(f"naming_reviews[{i}] rejected: not a dict")
            continue
        cluster_id = str(review.get("cluster_id") or "").strip()
        verdict = str(review.get("verdict") or "").strip()
        if cluster_id not in valid_clusters:
            warnings.append(
                f"naming_reviews[{i}] rejected: cluster_id={cluster_id!r} は"
                "今日のテーマ候補にありません"
            )
            continue
        if verdict not in ("name", "skip"):
            warnings.append(
                f"naming_reviews[{i}] rejected: verdict={verdict!r} は"
                " name または skip が必要です"
            )
            continue
        if verdict == "skip":
            LOGGER.debug(
                "[judgment_finalize] naming_reviews: cluster_id=%r skipped (persona=%s)",
                cluster_id, persona_id,
            )
            continue

        # verdict=name → テーマページを即時作成
        name = str(review.get("name") or "").strip()
        if not name:
            warnings.append(
                f"naming_reviews[{i}] rejected: verdict=name のとき name フィールドが必要です"
                f" (cluster_id={cluster_id!r})"
            )
            continue

        cand = valid_clusters[cluster_id]
        member_refs = list(cand.get("member_refs") or [])
        try:
            # create_theme_page の get-or-create (check-then-insert) の原子性は
            # プロセス内ロックが担う — slot_close 側と同じ規律で _db_lock を
            # 保持する (Codex 一巡目 #6。無ロック呼び出しは同名ページの重複
            # 作成の窓になる)。
            with adapter._db_lock:
                page_id = create_theme_page(
                    mem_conn,
                    title=name,
                    member_refs=member_refs,
                    origin="naming",
                )
            applied = True
            lines.append(
                f"テーマ「{name}」が棚に立ちました（{len(member_refs)} 件まとめて, page_id={page_id[:8]}）"
            )
            LOGGER.info(
                "[judgment_finalize] naming_reviews: theme page created "
                "cluster_id=%r name=%r page_id=%s (persona=%s)",
                cluster_id, name, page_id, persona_id,
            )
        except Exception as exc:
            LOGGER.exception(
                "[judgment_finalize] naming_reviews: create_theme_page raised "
                "(cluster_id=%r, persona=%s)", cluster_id, persona_id,
            )
            warnings.append(
                f"naming_reviews[{i}] テーマページの作成に失敗: {exc}"
            )

    return applied


# ---------------------------------------------------------------------------
# day_close
# ---------------------------------------------------------------------------


def _finalize_day_close(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """就寝判断の適用。

    - tomorrow_memo / day_theme / user_report_seeds → 当日 plan 行の meta_json
      (翌朝の day_open は「昨日 = この plan_date」の meta を読む —
      ``build_day_open_situation_text`` と対になる読み書き)

    ``desire_reviews`` (欲求のたな卸し) は欄ごと退役した — 欲求プールが機構ごと
    消えたため (autonomous_behavior_v3.md §8)。

    旧 day_digest (実績の決定論要約の保存コピー) は 2026-07-29 撤去 —
    唯一の読者だった day_open の [昨日のふりかえり] を廃止したため。昨日の
    消化は就寝判断が済ませ、朝へは tomorrow_memo だけが渡る。実績表が要る
    場面では :func:`saiverse.judgment_points.build_day_results_text` で
    いつでも再構築できる (slots_json が正)。
    """
    applied = False
    plan_date = ctx.get("plan_date") or clock.now().date().isoformat()

    updates: Dict[str, Any] = {}
    tomorrow_memo = str(output.get("tomorrow_memo") or "").strip()
    if tomorrow_memo:
        updates["tomorrow_memo"] = tomorrow_memo
    else:
        warnings.append(
            "tomorrow_memo が空です (明日の起床判断はメモなしで始まります)"
        )
    day_theme = str(output.get("day_theme") or "").strip()
    if day_theme:
        updates["day_theme"] = day_theme

    seeds_raw = output.get("user_report_seeds")
    seeds: List[str] = []
    if isinstance(seeds_raw, list):
        for i, seed in enumerate(seeds_raw):
            if isinstance(seed, str) and seed.strip():
                seeds.append(seed.strip())
            else:
                warnings.append(
                    f"user_report_seeds[{i}] rejected: 空でない文字列が必要"
                )
        if len(seeds) > 3:
            warnings.append(
                f"user_report_seeds は最大 3 件 (got {len(seeds)}); 先頭 3 件のみ保存"
            )
            seeds = seeds[:3]
    elif seeds_raw is not None:
        warnings.append(
            f"user_report_seeds rejected: 配列が必要 (got {type(seeds_raw).__name__})"
        )
    if seeds:
        updates["user_report_seeds"] = seeds

    if updates:
        try:
            day_plan_mod.update_plan_meta(manager, persona_id, plan_date, updates)
            applied = True
            lines.append("（今日のふりかえりを記録した）")
            if tomorrow_memo:
                lines.append(f"（明日の自分へのメモ: {tomorrow_memo}）")
            if day_theme:
                lines.append(f"（今日のテーマ: {day_theme}）")
            if seeds:
                lines.append("（ユーザーに話したいこと: " + " / ".join(seeds) + "）")
        except Exception as exc:
            LOGGER.exception("[judgment_finalize] update_plan_meta raised")
            warnings.append(f"ふりかえりの保存に失敗: {exc}")
    else:
        # メモ・テーマ・報告種が全て空 = 明日へ引き継ぐものが何も無い。
        # 空の update_plan_meta を呼んで「記録した」とエコーすると、引き継ぎ
        # 消失が成功の顔で残る (2026-07-29 Codex 指摘 high2)。保存もエコーも
        # せず、事実を独白側の要約行に残す。
        lines.append("（明日へのメモは残さなかった）")

    # --- episode_purposes → 層2 棚入れタグ ({episode, purpose} ペア; §9.1) --
    applied |= _apply_episode_purposes_pairs(
        manager, persona_id, output, ctx, lines, warnings,
    )

    # --- curation_reviews → 承認分を編纂プランとして永続化 (P4-a) ----------
    applied |= _apply_curation_reviews(
        manager, persona_id, output, ctx, lines, warnings,
    )

    # --- naming_reviews → verdict=name の候補をテーマページとして即時作成 (P4-b) --
    applied |= _apply_naming_reviews(
        manager, persona_id, output, ctx, lines, warnings,
    )

    # --- curation バッチ起動 (裁定 (c): 就寝判断適用直後の背景ジョブ) --------
    # pending プランがあれば背景スレッドで即実行する。
    # スレッド起動前に必要な依存（manager, persona_id）は全て構築済み。
    _maybe_launch_curation_batch(manager, persona_id)

    return applied


def _maybe_launch_curation_batch(manager: Any, persona_id: str) -> None:
    """pending の編纂プランがあれば背景スレッドで run_pending_plans を起動する。

    - daemon スレッドとして起動するので本体の終了を妨げない。
    - 依存（manager, persona_id）はスレッド起動前にここで確認し、問題があれば
      起動しない——スレッド内での None アクセスを防ぐ。
    - 既存の背景スレッド起動流儀（event_scheduler / integration_manager）に倣い
      threading.Thread(daemon=True) を使う。
    """
    import threading

    try:
        persona = (getattr(manager, "personas", None) or {}).get(persona_id)
        if persona is None:
            return
        adapter = getattr(persona, "sai_memory", None)
        mem_conn = getattr(adapter, "conn", None) if adapter is not None else None
        if mem_conn is None:
            return

        from sai_memory.curation_ops import list_pending
        pending = list_pending(mem_conn)
        if not pending:
            LOGGER.debug(
                "[judgment_finalize] curation_batch: no pending plans, skip (persona=%s)",
                persona_id,
            )
            return

        LOGGER.info(
            "[judgment_finalize] curation_batch: launching background thread "
            "(persona=%s pending=%d)",
            persona_id, len(pending),
        )

        from sai_memory.curation_ops import run_pending_plans

        def _run() -> None:
            try:
                run_pending_plans(manager, persona_id)
            except Exception:
                LOGGER.warning(
                    "[judgment_finalize] curation_batch: background run raised",
                    exc_info=True,
                )

        t = threading.Thread(
            target=_run,
            name=f"CurationBatch-{persona_id[:8]}",
            daemon=True,
        )
        t.start()

    except Exception:
        LOGGER.warning(
            "[judgment_finalize] _maybe_launch_curation_batch raised",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def _resolve_tracked_ledger(
    manager: Any, ctx: Dict[str, Any], kind: str,
) -> Tuple[Any, Optional[str], Optional[str]]:
    """台帳フローで走るか (tracked) を判定する (D6 の degrade 分岐)。

    tracked = ``manager.execution_ledger`` と ``judgment_context.execution_id``
    (Chunk A の run_judgment_point が同乗させる) が**両方**あるとき。どちらかが
    無い環境 (台帳なし・旧テスト) は従来挙動 (SAIMemory 直書き) に degrade する。
    台帳 status の読み取り失敗も WARN + degrade (可用性優先、従来挙動)。

    Returns:
        ``(ledger, execution_id, status)``。untracked なら ``(None, None, None)``。
    """
    execution_id = str(ctx.get("execution_id") or "").strip() or None
    ledger = getattr(manager, "execution_ledger", None)
    if ledger is None or execution_id is None:
        return None, None, None
    try:
        status = ledger.get_execution(execution_id).get("status")
    except Exception:
        LOGGER.warning(
            "[judgment_finalize] ledger status read failed; degrading to "
            "untracked finalize (kind=%s execution=%s)", kind, execution_id,
            exc_info=True,
        )
        return None, None, None
    return ledger, execution_id, status


def _build_session_digest_message(
    ctx: Dict[str, Any], digest_text: str
) -> Dict[str, Any]:
    """post_session の digest 欄から SAIMemory ダイジェスト行を組む (D9-5)。

    旧 work_session 直書き (削除済み ``sea.work_session`` の digest 保存) と
    同形: tags=[DIGEST_TAG] / line_role='main_line' / scope='committed' /
    metadata.work_session (ws_meta を judgment_context から復元) / tz-aware
    timestamp。day_close の ``_collect_today_session_digests``
    (DIGEST_TAG + committed) と一日新聞 (metadata.work_session) が読む形を必ず保つ。

    NOTE: ``origin_track_id`` の刻印は 2026-08-21 に、``origin_episode`` の刻印は
    2026-08-22 (束 6c、v3 §7) に退役した。
    """
    from sea.work_session import DIGEST_TAG

    metadata: Dict[str, Any] = {"tags": [DIGEST_TAG]}
    ws_meta = ctx.get("ws_meta")
    if isinstance(ws_meta, dict) and ws_meta:
        metadata["work_session"] = dict(ws_meta)
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": digest_text,
        # tz-aware UTC ISO (naive は adapter 側で ±9h ずれる — 判断行と同じ規律)
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
        "line_role": "main_line",
        "scope": "committed",
    }
    return message


def _write_session_digest_direct(
    manager: Any,
    persona_id: str,
    digest_message: Dict[str, Any],
) -> None:
    """untracked degrade 経路の digest 直書き (D9-5)。

    台帳の無い環境 (旧テスト・mock シム) でも digest は残す。

    ⚠ 束 6c (2026-08-22) で ``episodes.set_digest_ref`` による「出来事へ再訪の鍵を
    刻む」後段が消えた — エピソードという専用の記録行を持たなくなったため
    (v3 §7)。digest そのものは SAIMemory の行として残る。
    """
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    if adapter is None:
        LOGGER.warning(
            "[judgment_finalize] no adapter for %s; session digest not stored",
            persona_id,
        )
        return
    try:
        adapter.append_persona_message(digest_message)
    except Exception:
        LOGGER.exception(
            "[judgment_finalize] failed to append session digest (persona=%s)",
            persona_id,
        )


def _build_judgment_message(
    manager: Any,
    kind: str,
    ctx: Dict[str, Any],
    final_text: str,
    monologue: str,
    scope: str,
    situation_text: str,
) -> Dict[str, Any]:
    """SAIMemory 判断行の message dict (tracked/untracked 共通の凍結形)。"""
    pulse_ctx = get_active_pulse_context()
    pulse_id = getattr(pulse_ctx, "pulse_id", None) if pulse_ctx else None
    # Chunk C (digest コールローカル注入) の受け口: 保存用の状況テキストが
    # 別携帯されていればそれを使う (今は常に situation_text)。
    paired = str(ctx.get("paired_situation_text") or "").strip() or situation_text.strip()
    return {
        "role": "assistant",
        "content": final_text,
        # tz-aware UTC ISO で渡す (naive ISO は adapter 側で ±9h ずれる。
        # docs/issues/history_manager_timestamp_tz_drift.md と同根)。
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # metadata.judgment: 独白本文を適用エコー行と構造的に分離して
        # 持つ。一日新聞 (saiverse/day_report.py) が「就寝のふりかえり」
        # に独白だけを載せるための読み口 (content は従来どおり
        # 独白+要約行の全文 — ペルソナの文脈に乗る内容は変えない)。
        "metadata": {
            "tags": ["meta_judgment", f"judgment:{kind}"],
            "judgment": {"kind": kind, "monologue": monologue},
        },
        "line_role": "meta_judgment",
        "scope": scope,
        "pulse_id": str(pulse_id) if pulse_id is not None else None,
        # 判断時に渡された状況テキストを Pulse タイムラインで見えるようにする。
        "paired_action_text": paired or None,
    }


def judgment_finalize(
    judgment_output: Optional[Dict[str, Any]] = None,
    kind: str = "",
    judgment_context: str = "",
    situation_text: str = "",
) -> Tuple[str, ToolResult, None]:
    """Finalize a judgment-point turn (see module docstring)."""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("judgment_finalize requires an active persona context")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("judgment_finalize requires an active manager context")

    output = judgment_output if isinstance(judgment_output, dict) else {}
    monologue = (output.get("monologue") or "").strip()
    try:
        ctx = json.loads(judgment_context) if judgment_context else {}
    except (TypeError, ValueError):
        LOGGER.warning(
            "[judgment_finalize] judgment_context is not valid JSON: %r",
            judgment_context,
        )
        ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}

    # --- 実行単位の冪等 (A8/D6): tracked で running でなければ再適用しない ----
    from saiverse.execution_ledger import STATUS_RUNNING
    from saiverse.execution_ledger_wiring import (
        TARGET_SAIMEMORY_APPEND,
        TARGET_SAIMEMORY_APPEND_DIGEST,
    )

    ledger, execution_id, ledger_status = _resolve_tracked_ledger(manager, ctx, kind)
    tracked = ledger is not None
    if tracked and ledger_status != STATUS_RUNNING:
        # 再 finalize (適用済み/終端) — 世界更新の二重適用の口を閉じる。
        msg = (
            f"Judgment already finalized (execution={execution_id}, "
            f"status={ledger_status}); nothing applied"
        )
        LOGGER.info("[judgment_finalize] %s (persona=%s kind=%s)",
                    msg, persona_id, kind)
        return msg, ToolResult(history_snippet=msg), None

    lines: List[str] = []
    warnings: List[str] = []
    summary_extras: List[str] = []

    if kind == KIND_DAY_OPEN:
        committed = _finalize_day_open(
            manager, persona_id, output, ctx, lines, warnings,
        )
    elif kind == KIND_POST_SESSION:
        committed = _finalize_post_session(
            manager, persona_id, output, ctx, lines, warnings,
        )
    elif kind == KIND_ON_EVENT:
        committed = _finalize_on_event(
            manager, persona_id, output, ctx, lines, warnings, summary_extras,
        )
    elif kind == KIND_DAY_CLOSE:
        committed = _finalize_day_close(
            manager, persona_id, output, ctx, lines, warnings,
        )
    else:
        LOGGER.warning("[judgment_finalize] unknown kind=%r; nothing applied", kind)
        warnings.append(f"unknown judgment kind: {kind!r}")
        committed = False

    # --- digest 統合 (D9-5): post_session の digest 欄 → SAIMemory ダイジェスト行 ---
    # tracked では outbox の第 1 項目 (判断行より前 = FIFO で digest が先) に
    # 積み、配送 handler (saimemory.append_digest) が append する。untracked は
    # 直書き (degrade でも digest は残す)。
    digest_message: Optional[Dict[str, Any]] = None
    if kind == KIND_POST_SESSION:
        digest_text = str(output.get("digest") or "").strip()
        if digest_text:
            digest_message = _build_session_digest_message(ctx, digest_text)
        else:
            # スキーマ required なので通常は入る — 空は観測に残す (黙らせない)
            warnings.append(
                "digest が空です (このセッションのダイジェストは記録されません)"
            )

    for w in warnings:
        LOGGER.warning("[judgment_finalize] (%s/%s) %s", persona_id, kind, w)

    # --- 整形済みテキスト (JSON 非混入; 不変条件 v2-A) --------------------
    # 判断が直接スペルを撃つ経路は束 6c で消えたので、本文は独白と適用結果の
    # 要約行だけになった (module docstring の ⚠ 参照)。
    body_parts = [p for p in (monologue, "\n".join(lines)) if p]
    final_text = "\n\n".join(body_parts) or "(empty judgment)"
    scope = "committed" if committed else "discardable"

    message = _build_judgment_message(
        manager, kind, ctx, final_text, monologue, scope, situation_text,
    )

    if tracked:
        # --- 台帳化 (A8/D6): 直書きを廃し mark_applied + outbox に載せ替え ---
        # RESULT_JSON 標準 (D6/§11-1): 照合 (#5) と呼び出し側読み出しに足る最小。
        result_json: Dict[str, Any] = {
            "kind": kind,
            "committed": committed,
            "scope": scope,
            "warnings": len(warnings),
        }
        if kind == KIND_ON_EVENT:
            reaction_type = next(
                (e.split("=", 1)[1] for e in summary_extras
                 if e.startswith("reaction=")),
                None,
            )
            if reaction_type:
                result_json["reaction"] = reaction_type
        elif kind == KIND_POST_SESSION and ctx.get("episode_ref"):
            result_json["episode_ref"] = str(ctx["episode_ref"])

        # wiring の saimemory.append handler の payload 契約
        # (execution_ledger_wiring._make_saimemory_append_handler):
        # {"message": {...}, "building_id": ..., "thread_suffix": ...}。
        # 現行 append_persona_message(message) は building_id=None /
        # thread_suffix=None 相当 (persona スレッド直書き) — それを正確に写す。
        outbox_items: List[Dict[str, Any]] = []
        if digest_message is not None:
            # D9-5: digest は第 1 項目 (FIFO で判断行より先に記憶へ届く)。
            outbox_items.append({
                "target": TARGET_SAIMEMORY_APPEND_DIGEST,
                "persona_id": persona_id,
                "payload": {"message": digest_message},
            })
        outbox_items.append({
            "target": TARGET_SAIMEMORY_APPEND,
            "persona_id": persona_id,
            "payload": {
                "message": message,
                "building_id": None,
                "thread_suffix": None,
            },
        })
        # mark_applied の失敗 (台帳遷移例外) は素直に raise する: 世界更新は
        # 済み・台帳は running のまま → run_judgment_point が unknown 化 →
        # 照合対象として観測面に残る (intent の「分裂を見えるようにする」)。
        # 配送失敗は mark_applied 内で処理される (pending 残存 → 関所/回復 tick)。
        ledger.mark_applied(
            execution_id, result=result_json,
            outbox_items=outbox_items, deliver=True,
        )
    else:
        # --- 従来経路 (台帳なし環境・旧テスト): SAIMemory 直書き -----------
        # digest は判断行より先に書く (tracked の FIFO と同順)。
        if digest_message is not None:
            _write_session_digest_direct(manager, persona_id, digest_message)
        persona = (getattr(manager, "personas", None) or {}).get(persona_id)
        if persona is not None:
            adapter = getattr(persona, "sai_memory", None)
            if adapter is not None:
                try:
                    adapter.append_persona_message(message)
                except Exception:
                    LOGGER.exception(
                        "[judgment_finalize] Failed to append persona message"
                    )

    summary = (
        f"Judgment finalized (kind={kind}, applied={committed}, "
        f"warnings={len(warnings)}, scope={scope})"
    )
    if summary_extras:
        # on_event の reaction 等、呼び出し側が読む判断結果。
        summary += " [" + ", ".join(summary_extras) + "]"

    # 適用結果を Pulse の event_callback へ通知する (best-effort)。
    # run_judgment_point (saiverse/judgment_points.py) がこれを捕捉して
    # applied_events として呼び出し側へ返す — on_event の engage_now で
    # 応対 Pulse を起動するか等を、本番配線 (saiverse/autonomy_wiring.py) が
    # 判断結果に基づいて選ぶための唯一の戻り経路。
    try:
        from tools.context import get_event_callback

        _cb = get_event_callback()
        if callable(_cb):
            _cb({
                "type": "judgment_applied",
                "kind": kind,
                "applied": committed,
                "extras": list(summary_extras),
            })
    except Exception:
        LOGGER.debug(
            "[judgment_finalize] failed to emit judgment_applied event",
            exc_info=True,
        )
    return summary, ToolResult(history_snippet=summary), None


def schema() -> ToolSchema:
    return ToolSchema(
        name="judgment_finalize",
        description=(
            "Internal tool for judgment-point Playbooks only (judgment_day_open / "
            "judgment_post_session / judgment_on_event / judgment_day_close). "
            "Receives the judge node's structured output (dict), validates and "
            "applies it per judgment kind (day plan save, task verdict with "
            "artifact grounding, desk memo, event reactions, day-close review), "
            "and persists the resulting monologue + summary text to SAIMemory. "
            "Invalid items are rejected individually with warnings."
        ),
        parameters={
            "type": "object",
            "properties": {
                "judgment_output": {"type": "object"},
                "kind": {"type": "string"},
                "judgment_context": {"type": "string"},
                "situation_text": {"type": "string"},
            },
            "required": ["judgment_output", "kind"],
        },
        result_type="string",
        spell=False,
    )
