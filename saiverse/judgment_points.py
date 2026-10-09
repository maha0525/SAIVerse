"""判断点コーディネータ — イベント到着判断 (on_event) の組み立てと起動。

判断点 = ペルソナが「何を見て、どういうスキーマで意思決定を出力するか」が
定義された LLM 呼び出し。meta_judgment v2 で確立した様式
(docs/intent/persona_cognition/meta_judgment_structured.md) をそのまま継承する:

1. 状況テキストは tail 注入 (Playbook judge ノードの action テンプレートに展開。
   head は不変、キャッシュ保護)
2. LLM は動的 ``response_schema`` に従う JSON を返す (function calling は使わない)
3. finalize ツール (``builtin_data/tools/judgment_finalize.py``) が JSON を
   検証・適用し、メインキャッシュには整形済み独白＋要約行のみを残す
   (JSON 非混入、不変条件 v2-A 継承)
4. 選択肢は動的 enum 注入で物理的に絞る (実在しないものは構造的に選べない)
5. ``additionalProperties`` はスキーマにハードコードしない (プロバイダ正規化層に
   任せる。meta_judgment_structured.md §Phase4 の Gemini 事故の教訓)

**残っている判断点は on_event (イベント到着判断) だけ** — 反応の選択
(engage_now / add_task / note_only / ignore。alert は engage_now のみに縮退)。

v2 の他の判断点は退役した (autonomous_behavior_v3.md §8 /
autonomous_behavior_v04_plan.md 段 1-4):

- 起床・就寝判断 (``day_open`` / ``day_close``) — ライフの確定と節目は機械の
  帳簿処理 (``saiverse.day_plan.handle_scheduled_life_boundary``) になった
- セッション終了判断 (``post_session``) — 作業セッションごと撤去
- 会話終了判断 (``post_conversation``) — 2026-08-16 の裁定で退役。本人の声の
  捕獲はスルースの一手へ一本化された

モデルは standard (META 相当): 起動は ``PulseController.submit_meta_judgment``
(= ``pulse_type="meta_judgment"``) を使い、``sea.pulse_context.aspect_from_pulse_type``
が META アスペクト (line_role='meta_judgment' / scope='discardable' / standard tier)
を導出する — meta_judgment Playbook の起動経路と同一。

**自動起動の配線は本モジュールではしない** (中間起動の空打ち防止)。本番の恒久
配線は ``saiverse.autonomy_wiring`` (Active ゲート / Playbook 欠如スキップ /
実行台帳込み) が担い、テストは ``run_judgment_point`` を直接呼ぶ。

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from saiverse import clock
from saiverse.day_plan import is_in_user_conversation

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

KIND_ON_EVENT = "on_event"

#: 判断点 kind → Playbook 名 (builtin_data/playbooks/public/)。
JUDGMENT_PLAYBOOK_MAP: Dict[str, str] = {
    KIND_ON_EVENT: "judgment_on_event",
}

# イベント到着判断 reaction の種別 (judgment_points.md §7)
REACTION_ENGAGE_NOW = "engage_now"
#: あとで取り組むためにタスク帳へシステムタスクとして積む。時間割にコマを
#: 挿す旧 ``insert_slot`` の置き換え (autonomous_behavior_v04_plan.md 段 1-3 —
#: 時間割の撤去より先に、唯一残る判断点の選択肢から時間割依存を外す)。
REACTION_ADD_TASK = "add_task"
REACTION_NOTE_ONLY = "note_only"
REACTION_IGNORE = "ignore"


# ---------------------------------------------------------------------------
# スキーマと状況テキスト
# ---------------------------------------------------------------------------


def build_on_event_schema(
    manager: Any, persona_id: str, is_alert: bool
) -> Dict[str, Any]:
    """イベント到着判断の response_schema (judgment_points.md §7)。

    reaction は anyOf 4 分岐 (engage_now / add_task / note_only / ignore)。
    **alert イベントでは anyOf を engage_now のみに動的縮退**させる
    (v1 状況 B の「強制」の継承)。

    add_task はタスク帳へのシステムタスクの追加で、時間割に依存しない
    (2026-10 段 1-3 で insert_slot から置き換え)。note_only の memo は判断の
    記録 (ペルソナの記憶に残る判断行) に載るだけで、別の置き場には書かない。
    """
    engage_now = {
        "type": "object",
        "properties": {"type": {"type": "string", "const": REACTION_ENGAGE_NOW}},
        "required": ["type"],
    }
    variants: List[Dict[str, Any]] = [engage_now]
    if not is_alert:
        variants.append({
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": REACTION_ADD_TASK},
                "task": {
                    "type": "string",
                    "description": (
                        "あとで取り組むためにタスク帳に積む一件。あとで読み返した"
                        "自分が迷わず取りかかれる具体さで、何をするかを書く"
                    ),
                },
            },
            "required": ["type", "task"],
        })
        variants.append({
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": REACTION_NOTE_ONLY},
                "memo": {"type": "string"},
            },
            "required": ["type", "memo"],
        })
        variants.append({
            "type": "object",
            "properties": {"type": {"type": "string", "const": REACTION_IGNORE}},
            "required": ["type"],
        })
    return {
        "type": "object",
        "properties": {
            "monologue": {"type": "string"},
            "reaction": {"anyOf": variants},
        },
        "required": ["monologue", "reaction"],
    }


def build_on_event_situation_text(
    manager: Any, persona_id: str, context: Dict[str, Any]
) -> str:
    """イベント到着判断の tail 注入テキスト (judgment_points.md §7「見るもの」)。"""
    now = clock.now()
    event_text = str(context.get("event_text") or "").strip()
    is_alert = bool(context.get("is_alert"))

    # 現在の活動状態。
    #
    # 「ユーザーと会話中か」の正典は開いている会話の状態であって、running
    # Track の種別ではない (life.md §7 案 Y, 2026-07-13)。判定は
    # day_plan.is_in_user_conversation に一本化する — 二つ目の実装を作らない。
    #
    # 会話以外の活動は、束 6c (2026-08-22) で供給源ごと消えた —— 出来事の書き手が
    # 全滅したので (v3 §7)、「いま何に取り組んでいるか」を答えられる器が無い。
    # 器の作り直しは v0.4 のティック設計 (v3 §9-3)。
    activity = "手すきです。"
    if is_in_user_conversation(manager, persona_id):
        activity = "ユーザーと会話中です。"

    parts = [
        "[イベント到着判断]",
        "イベントが届きました。どう反応するかを決めてください。",
    ]
    if is_alert:
        parts.append("このイベントは即応が必要です（今すぐ応対してください）。")
    parts += [
        "",
        "[イベント内容]",
        event_text or "(内容なし)",
        "",
        "[現在の状態]",
        f"現在時刻: {now.strftime('%H:%M')}",
        f"いまの活動: {activity}",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 判断点の起動
# ---------------------------------------------------------------------------


def validate_judgment_context(kind: str, context: Optional[Dict[str, Any]]) -> None:
    """呼び出し側が渡すべき context が揃っているかの検査 (**配線の誤り**)。

    :func:`run_judgment_point` は引数の組み立てで起きた**環境の障害** (DB が
    読めない等) を「起動できなかった」という結果へ畳むが、契約違反はそこに
    混ぜない — 畳むと発火経路の配線ミスが submitted=False として静かに流れ、
    誰も気づかないまま判断が起きなくなる。だから検査はここに分けて、
    **環境の状態を見るより前**に必ず raise させる (ペルソナ未ロード等で先に
    return してしまうと、配線ミスが環境の問題に化けて隠れる)。

    - ``on_event``: ``event_text`` が要る。無ければ「何のイベントか」の無い
      判断になる

    Raises:
        ValueError: 必須の context が欠けている。
    """
    ctx = context or {}
    if kind == KIND_ON_EVENT:
        if not str(ctx.get("event_text") or "").strip():
            raise ValueError(
                "on_event judgment requires context['event_text'] (non-empty)"
            )


def build_judgment_args(
    manager: Any, persona_id: str, kind: str, context: Dict[str, Any]
) -> Dict[str, Any]:
    """判断点 Playbook に渡す args (situation_text + response_schema + judgment_context)。"""
    if kind != KIND_ON_EVENT:
        raise ValueError(f"unknown judgment kind: {kind!r}")

    validate_judgment_context(kind, context)
    today = clock.now().date().isoformat()
    event_text = str(context.get("event_text") or "").strip()
    is_alert = bool(context.get("is_alert"))
    situation_text = build_on_event_situation_text(manager, persona_id, context)
    response_schema = build_on_event_schema(manager, persona_id, is_alert)
    judgment_context = {
        "plan_date": today,
        "is_alert": is_alert,
        # add_task で積むタスクに「何のイベントだったか」を添えるための抜粋
        "event_text": event_text[:200],
        # 刺激の永続 ID — add_task の出どころ参照と冪等キーに使う
        "stimulus_id": context.get("stimulus_id"),
    }
    return {
        "situation_text": situation_text,
        "response_schema": response_schema,
        "judgment_context": json.dumps(judgment_context, ensure_ascii=False),
    }


#: 判断が結末に至らなかったときの結末 (``submitted=False`` の結果 dict の
#: ``outcome``)。呼び出し側が答えたい問いは 1 つ — **判断の代わりに自分で応対
#: してよいか**。それは「判断が世界へ作用しえた地点まで進んだか」と「席が
#: 残っていて後からもう一度走りうるか」で決まる。
#:
#: - ``aborted``: LLM へ渡る前に止まり、席は残っていない (そもそも取っていない /
#:   放棄済み)。→ 代替経路 **OK**
#: - ``no_effect``: メタレーンへ渡ったが、副作用ゼロが確定した失敗 (Beat 関所の
#:   閉鎖 / LLM エラー) で戻り、台帳は failed 終端。→ 代替経路 **OK**
#: - ``ran``: メタレーンが走った後、成功の証跡なしに戻った。finalize が判断を
#:   適用済みかもしれない。→ 代替経路 **NG** (判断の決定を上書きしてしまう)
#: - ``indeterminate``: 席が残っている / 別の claimant が走らせている / 台帳が
#:   読めない。回復 tick に再発火されうる。→ 代替経路 **NG** (二重処理になる)
OUTCOME_ABORTED = "aborted"
OUTCOME_NO_EFFECT = "no_effect"
OUTCOME_RAN = "ran"
OUTCOME_INDETERMINATE = "indeterminate"

#: 代替経路 (呼び出し側が判断を経ずに自分で応対する) を許す結末。
_DIRECT_FALLBACK_OUTCOMES = frozenset({OUTCOME_ABORTED, OUTCOME_NO_EFFECT})


def direct_fallback_allowed(result: Dict[str, Any]) -> bool:
    """``submitted=False`` の判断結果に対し、代替経路を走らせてよいか。

    **既定は「走らせない」**。``outcome`` の無い結果は「LLM が動いたかもしれない」
    として扱う (2026-08-14 Codex 指摘 F3)。判断が走った後の失敗を「起動できな
    かった」と読んで応対すると、finalize が note_only 等を適用した**後**に応答を
    重ねてしまう —— 判断の決定を機構が上書きする形になる。

    結末を書き忘れた経路は WARNING で表に出す。既定が拒否なので、書き忘れは
    「ユーザーの呼びかけへの沈黙」として現れる —— 黙って通すより、ログに残して
    気づける形にしておく。
    """
    outcome = result.get("outcome")
    if outcome is None:
        LOGGER.warning(
            "[judgment] result has no outcome; refusing the direct fallback "
            "(kind=%s reason=%s execution=%s)",
            result.get("kind"), result.get("reason"), result.get("execution_id"),
        )
        return False
    return outcome in _DIRECT_FALLBACK_OUTCOMES


def _abandon_seat(ledger: Any, execution_id: Optional[str], reason: str) -> bool:
    """LLM 開始前の離脱で、claim 済みの席を放棄する (prepared 限定 CAS)。

    ``mark_failed`` は running からの遷移も許すため、同じ execution_id を共有
    した別の claimant が既に走らせている台帳まで failed に壊しうる。放棄には
    :meth:`ExecutionLedger.abandon_prepared` (status=prepared のときだけ failed)
    を使う — これがこの用途のために用意されている条件付き遷移。

    Returns:
        True = 席は残っていない (放棄した / そもそも席が無い)。
        False = 放棄できなかった (他の claimant の所有、または台帳が応答しない)。
    """
    if ledger is None or execution_id is None:
        return True
    abandon = getattr(ledger, "abandon_prepared", None)
    try:
        if callable(abandon):
            return bool(abandon(execution_id, reason))
        # prepared 限定 CAS を持たない台帳 (旧テストスタブ) は mark_failed へ
        # degrade する。本番台帳は abandon_prepared を持つ。
        ledger.mark_failed(execution_id, reason)
        return True
    except Exception:
        LOGGER.warning(
            "[judgment] failed to abandon prepared seat (execution=%s reason=%s)",
            execution_id, reason, exc_info=True,
        )
        return False


def _try_mark_running(ledger: Any, execution_id: str) -> bool:
    """prepared → running の席取り (早い者勝ち CAS)。

    claim_execution は既存 prepared 行を再利用するため、ほぼ同時の二重 claim は
    同じ execution_id を両方へ runnable として返しうる — 実行を一人に絞るのは
    :meth:`ExecutionLedger.try_mark_running` (status=prepared のときだけ running)。
    持たない台帳 (旧テストスタブ) は無条件 ``mark_running`` へ degrade する
    (勝者一意化なしの従来挙動。本番台帳は try_mark_running を持つ)。

    台帳の例外はそのまま上げる (呼び出し側が「台帳が応答しない」として処理)。
    """
    try_mark = getattr(ledger, "try_mark_running", None)
    if callable(try_mark):
        return bool(try_mark(execution_id))
    ledger.mark_running(execution_id)
    return True


def run_judgment_point(
    manager: Any,
    persona_id: str,
    kind: str,
    context: Optional[Dict[str, Any]] = None,
    execution_id: Optional[str] = None,
) -> Dict[str, Any]:
    """判断点を 1 回起動する (状況テキスト組み立て → 動的スキーマ → Playbook 起動)。

    起動経路は meta_judgment v2 と同一: ``PulseController.submit_meta_judgment``
    (pulse_type="meta_judgment" → META アスペクト → standard モデル)。
    Playbook 内の judge ノードが構造化出力を生成し、``judgment_finalize`` ツールが
    検証・適用・SAIMemory 書き込みを行う。

    W1 Chunk A (A7): ``execution_id`` と ``manager.execution_ledger`` が両方
    あるときは台帳フロー — submit 直前に ``try_mark_running`` (prepared 限定
    CAS。二重 claim の敗者は台帳に書かず indeterminate で離脱)、例外は
    BeatGateClosedError / LLMError → failed (適用前・副作用ゼロ)、
    Cancelled / その他 → unknown (LLM が動いたか不明) に分類し、正常 return
    後は台帳 status の証跡で成功を判定する。どちらかが無ければ従来挙動に
    degrade する (WARN は発火側 ``autonomy_wiring.fire_judgment_point`` が出す)。

    Args:
        context: 判断点の入力。on_event: ``event_text`` (必須) / ``is_alert``
            (省略時 False。True なら reaction スキーマが engage_now のみに縮退) /
            ``stimulus_id``。**ユーザー会話中は原則発火させないこと** — 会話の
            至上性 (judgment_points.md §7)。その抑止は呼び出し側の責務であり、
            本モジュールは判定しない (会話中の収穫はスルースが担う)

    Returns:
        ``{"kind", "playbook", "args", "submitted": bool, "errors": [...],
        "execution_id": str | None}``。
        起動できなかった場合は ``submitted=False`` + ``reason``。
    """
    context = context or {}
    playbook_name = JUDGMENT_PLAYBOOK_MAP.get(kind)
    if playbook_name is None:
        raise ValueError(
            f"unknown judgment kind: {kind!r} (expected one of {sorted(JUDGMENT_PLAYBOOK_MAP)})"
        )

    # 呼び出し側の契約違反 (必須 context の欠落) は畳まずに上げる。**環境の状態を
    # 見るより前**に検査する — ペルソナ未ロード等で先に return すると、配線ミスが
    # 環境の問題に化けて隠れる (2026-07-30 Codex 四巡目)。
    validate_judgment_context(kind, context)

    ledger = getattr(manager, "execution_ledger", None)
    tracked = ledger is not None and execution_id is not None

    def _abort(reason: str) -> Dict[str, Any]:
        """LLM 開始前の離脱: 席を放棄してから「起動できなかった」を返す。

        席を放棄しないまま submitted=False を返すと、呼び出し側の代替経路
        (on_event の direct dispatch) と、回復 tick による prepared の再発火が
        **両方**走る。放棄できなかった場合は結末を indeterminate にして、
        呼び出し側に代替経路を走らせない (二重処理の方が害が大きい)。
        """
        LOGGER.warning(
            "[judgment] %s aborted before dispatch: %s (persona=%s execution=%s)",
            kind, reason, persona_id, execution_id,
        )
        released = _abandon_seat(ledger if tracked else None, execution_id, reason)
        return {
            "kind": kind, "playbook": playbook_name, "submitted": False,
            "reason": reason, "execution_id": execution_id,
            "outcome": OUTCOME_ABORTED if released else OUTCOME_INDETERMINATE,
        }

    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    if persona is None:
        return _abort("persona not loaded")

    building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        return _abort("no current building")

    pulse_controller = getattr(manager, "pulse_controller", None)
    if pulse_controller is None:
        return _abort("no pulse_controller")

    # 引数の組み立て (= 状況テキストと動的スキーマの DB 収集) は LLM 開始前の
    # 工程。ここで落ちたら副作用はゼロなので、例外でなく「起動できなかった」
    # として返す — 呼び出し側 (on_event の direct fallback) はその戻り値で
    # 分岐する (2026-07-30 Codex 三巡目)。
    try:
        args = build_judgment_args(manager, persona_id, kind, context)
    except Exception as exc:
        LOGGER.warning(
            "[judgment] failed to build args for %s (persona=%s execution=%s)",
            kind, persona_id, execution_id, exc_info=True,
        )
        return _abort(f"args build failed: {exc!r}")

    if execution_id is not None:
        # execution_id を judgment_context に同乗させ finalize へ届ける
        # (playbook JSON は不変 — judgment_context は既に args で渡っている)。
        try:
            jctx = json.loads(args.get("judgment_context") or "{}")
        except (TypeError, ValueError):
            jctx = {}
        jctx["execution_id"] = execution_id
        args["judgment_context"] = json.dumps(jctx, ensure_ascii=False)

    errors: List[Dict[str, Any]] = []
    applied_events: List[Dict[str, Any]] = []

    def _capture_event(ev: Dict[str, Any]) -> None:
        if not isinstance(ev, dict):
            return
        if ev.get("type") == "error":
            errors.append(ev)
        elif ev.get("type") == "judgment_applied":
            # judgment_finalize が emit する適用結果 (kind / applied / extras)。
            # on_event の reaction 等、呼び出し側 (saiverse.autonomy_wiring) が
            # 判断結果に応じて後続処理 (engage_now の応対起動) を選ぶために使う。
            applied_events.append(ev)

    LOGGER.info(
        "[judgment] dispatching %s: persona=%s playbook=%s execution=%s",
        kind, persona_id, playbook_name, execution_id,
    )
    if tracked:
        # 不変条件 1: 不可逆処理 (LLM) の開始「前」に running を宣言する。
        # claim_execution は既存 prepared 行を再利用するため、ほぼ同時の二重
        # claim は同じ execution_id を両方へ runnable として返しうる — 勝者を
        # 一人に絞るのは、この prepared 限定 CAS (try_mark_running)。
        try:
            seat_won = _try_mark_running(ledger, execution_id)
        except Exception:
            LOGGER.warning(
                "[judgment] ledger mark_running failed; not dispatching %s "
                "(persona=%s execution=%s)", kind, persona_id, execution_id,
                exc_info=True,
            )
            # ここも LLM 開始前。席が残ったままなら結末は indeterminate になり、
            # 呼び出し側の代替経路は走らない (別の claimant が走らせている、
            # あるいは台帳が応答しない状態なので)。
            aborted = _abort("ledger transition failed")
            aborted.update({"args": args, "errors": errors,
                            "applied_events": applied_events})
            return aborted
        if not seat_won:
            # 敗者: 同じ execution_id の席を別の claimant が先に running へ
            # 進めた。判断は勝者側で走る (あるいはもう終端している) ので、
            # 台帳には一切書かずに離脱する — mark_failed 等を呼ぶと勝者の
            # 走行中台帳を壊す (try_mark_running の契約)。呼び出し側は代替
            # 経路を走らせてはいけない (勝者が同じイベントを処理するため
            # indeterminate)。
            LOGGER.info(
                "[judgment] %s seat already taken by another claimant; "
                "leaving without ledger writes (persona=%s execution=%s)",
                kind, persona_id, execution_id,
            )
            return {"kind": kind, "playbook": playbook_name, "args": args,
                    "submitted": False,
                    "reason": "seat taken by another claimant",
                    "outcome": OUTCOME_INDETERMINATE,
                    "errors": errors, "applied_events": applied_events,
                    "execution_id": execution_id}
    try:
        pulse_controller.submit_meta_judgment(
            persona_id=persona_id,
            building_id=building_id,
            meta_playbook=playbook_name,
            args=args,
            event_callback=_capture_event,
        )
    except Exception as exc:
        LOGGER.warning(
            "[judgment] %s Playbook raised: persona=%s error=%r",
            kind, persona_id, exc,
        )
        # 結末は「台帳へ何を書けたか」から導く (書けなかった = 席の状態が不明)。
        # 台帳の無い manager (旧テストスタブ) では例外の性質だけで決める。
        if tracked:
            terminal = _classify_runtime_failure(ledger, execution_id, exc)
            if terminal is None:
                outcome = OUTCOME_INDETERMINATE
            else:
                outcome = OUTCOME_NO_EFFECT if terminal == "failed" else OUTCOME_RAN
        else:
            outcome = (
                OUTCOME_NO_EFFECT if _runtime_failure_is_side_effect_free(exc)
                else OUTCOME_RAN
            )
        return {"kind": kind, "playbook": playbook_name, "args": args,
                "submitted": False, "reason": f"runtime exception: {exc!r}",
                "outcome": outcome,
                "errors": errors, "applied_events": applied_events,
                "execution_id": execution_id}

    if errors:
        for err in errors:
            LOGGER.warning(
                "[judgment] %s Playbook emitted error: persona=%s error=%s",
                kind, persona_id, err,
            )

    submitted = True
    #: 台帳 status を読めなかったときの結末 (読めた場合は None のまま)。
    unread_outcome: Optional[str] = None
    if tracked:
        # A7: 成功 = finalize 完了の永続証跡 (台帳 status) から導出する。
        status: Optional[str] = None
        try:
            status = ledger.get_execution(execution_id)["status"]
        except Exception:
            # 台帳が読めない = 証跡が確認できない。**成功へ倒さない**
            # (2026-08-14 Codex 指摘: 旧実装は初期値 submitted=True のまま通し、
            # 結末も付かないので代替経路の可否すら判定できなかった)。
            # ただし callback が finalize の judgment_applied を捕まえていれば、
            # それは台帳とは独立した一次証跡なので成功として扱ってよい —
            # 台帳の読み取り失敗だけを理由に、実際に下された判断を捨てない。
            # ただし**イベントの中身を確かめる**: 同じ判断点 (kind) のもので、
            # かつ applied が真のものだけ (finalize は適用できなかった場合も
            # applied=False で emit する。type だけ見ると「適用に失敗した」を
            # 「適用した」と読む。2026-08-14 Codex 三巡目)。
            if any(
                isinstance(ev, dict)
                and ev.get("kind") == kind
                and bool(ev.get("applied"))
                for ev in applied_events
            ):
                LOGGER.warning(
                    "[judgment] failed to read ledger status after %s; "
                    "accepting the finalize event captured by the callback "
                    "(persona=%s execution=%s)",
                    kind, persona_id, execution_id, exc_info=True,
                )
            else:
                LOGGER.warning(
                    "[judgment] failed to read ledger status after %s and no "
                    "finalize event was captured; treating the outcome as "
                    "indeterminate (persona=%s execution=%s)",
                    kind, persona_id, execution_id, exc_info=True,
                )
                submitted = False
                unread_outcome = OUTCOME_INDETERMINATE
                errors.append({
                    "type": "error",
                    "message": "ledger status unreadable after meta lane return",
                })
        from saiverse.execution_ledger import (
            STATUS_APPLIED,
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_RUNNING,
        )
        if status in (STATUS_APPLIED, STATUS_COMPLETED):
            submitted = True
        elif status == STATUS_RUNNING:
            # finalize が mark_applied を呼ばずにメタレーンが戻った = 証跡なし。
            # 「成功 = finalize 完了の永続証跡」(A7 修正方針) に従い unknown 化
            # (自動再実行はされず、照合対象として観測面に残る)。
            submitted = False
            LOGGER.warning(
                "[judgment] %s returned without finalize evidence "
                "(ledger still running); marking unknown "
                "(persona=%s execution=%s)", kind, persona_id, execution_id,
            )
            try:
                ledger.mark_unknown(
                    execution_id, "meta lane returned without finalize evidence",
                )
            except Exception:
                LOGGER.warning(
                    "[judgment] mark_unknown failed after missing finalize "
                    "evidence (persona=%s execution=%s)",
                    persona_id, execution_id, exc_info=True,
                )
            errors.append({
                "type": "error",
                "message": "no finalize evidence: ledger still running "
                           "after meta lane return",
            })
        elif status == STATUS_FAILED:
            submitted = False
        elif status is not None:
            # unknown 等 (回復 tick との競合)。finalize 証跡なし = 成功と言えない。
            submitted = False
            errors.append({
                "type": "error",
                "message": f"ledger status {status} after meta lane return",
            })

    result = {"kind": kind, "playbook": playbook_name, "args": args,
              "submitted": submitted, "errors": errors,
              "applied_events": applied_events, "execution_id": execution_id}
    if not submitted:
        # メタレーンは例外なく戻ったが成功の証跡が無い (finalize 証跡なし /
        # failed / unknown)。LLM も finalize も走った後かもしれないので、
        # 呼び出し側の代替経路は許さない。台帳自体が読めなかった場合は
        # 「走ったかどうかも分からない」= indeterminate。
        result["outcome"] = unread_outcome or OUTCOME_RAN
    return result


def _runtime_failure_is_side_effect_free(exc: Exception) -> bool:
    """submit_meta_judgment の例外が「副作用ゼロ確定」か (A7、D4)。

    - BeatGateClosedError: 実行は始まっていない → 副作用ゼロ
    - LLMError: 出力なし = 世界適用前 → 副作用ゼロ
    - ExecutionCancelledException / その他: LLM が動いたか不明 → 副作用不明

    台帳の終端 (failed / unknown) と呼び出し側の結末 (no_effect / ran) は
    **この 1 つの判定から導く** — 二箇所で例外型を読み分けると、片方だけ直した
    ときに「台帳は unknown なのに呼び出し側は再実行してよいと読む」形の食い違いが
    生まれる。

    ⚠ **型だけでは「判断が適用済みか」は分からない** (2026-08-14 Codex 指摘)。
    ``sea/runtime_graph.py`` と ``sea/runtime_llm.py`` は Playbook 実行中の
    **任意の例外**を ``LLMError`` に包み直すため、ここへ届く LLMError は
    「プロバイダが出力前に落ちた」とは限らず、finalize が判断を適用した**後**の
    クラッシュでもありうる。それでも代替経路が判断を上書きしないのは、**台帳の
    合法遷移が歯止めになっている**から:

    - finalize が適用済み → 行は ``applied`` → ``applied → failed`` は不正遷移で
      :func:`_classify_runtime_failure` が書けず None を返す → 結末は
      ``indeterminate`` (代替経路は走らない)
    - finalize 前 → 行は ``running`` → ``running → failed`` は合法 → ``no_effect``
      (適用された判断が無いので、代替経路が上書きする決定も無い)

    つまり**この関数の型判定は台帳の裏付けとセットでしか正しくない**。台帳の無い
    経路 (execution_ledger を持たない manager = 旧テストスタブ) では裏付けが無く、
    型の推定がそのまま結末になる。本番の manager は必ず台帳を持つ。
    回帰は ``test_runtime_exception_after_finalize_is_indeterminate``。
    """
    from llm_clients.exceptions import LLMError
    from sea.beat_gate import BeatGateClosedError

    return isinstance(exc, (BeatGateClosedError, LLMError))


def _classify_runtime_failure(
    ledger: Any, execution_id: str, exc: Exception
) -> Optional[str]:
    """submit_meta_judgment の例外を台帳の終端状態へ分類する (A7、D4)。

    副作用ゼロ確定 (:func:`_runtime_failure_is_side_effect_free`) なら failed、
    そうでなければ unknown (自動再実行禁止の対象、intent §2.5)。

    台帳遷移自体の例外は握らず WARN に留める (二重障害でクラッシュさせない)。

    Returns:
        台帳へ実際に書いた終端 (``"failed"`` / ``"unknown"``)。遷移が失敗して
        **何も書けなかった場合は None** — 呼び出し側はそれを「席の状態が不明」
        として扱う (書けなかったことを成功と読まない)。
    """
    from llm_clients.exceptions import LLMError
    from sea.beat_gate import BeatGateClosedError
    from sea.cancellation import ExecutionCancelledException

    if isinstance(exc, BeatGateClosedError):
        detail = f"beat gate closed: {exc}"
    elif isinstance(exc, LLMError):
        detail = f"llm error: {exc}"
    elif isinstance(exc, ExecutionCancelledException):
        detail = f"cancelled: {exc}"
    else:
        detail = str(exc) or type(exc).__name__
    terminal = "failed" if _runtime_failure_is_side_effect_free(exc) else "unknown"

    try:
        if terminal == "failed":
            ledger.mark_failed(execution_id, detail)
        else:
            ledger.mark_unknown(execution_id, detail)
    except Exception:
        LOGGER.warning(
            "[judgment] ledger transition failed after runtime error "
            "(execution=%s original=%r)", execution_id, exc, exc_info=True,
        )
        return None
    return terminal
