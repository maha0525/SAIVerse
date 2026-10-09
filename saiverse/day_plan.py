"""ライフ (起床〜就寝の活動区間) の帳簿と、起床・就寝の節目 (機械の帳簿処理)。

モジュール名は歴史的経緯 (v2 の「時間割 (day plan)」の置き場だった) のまま。
v2 の時間割一式 — 日次編成・コマの予約/発火/繰り下げ/精算・予算ゲート・作業
セッション・暮らしコマ — は autonomous_behavior_v04_plan.md 段 1-4 で撤去した
(autonomous_behavior_v3.md §8)。いま本モジュールが持つのは:

- **ライフの置き場** (persona_life テーブル) の読み書き — :func:`get_lives` /
  :func:`save_lives` / :func:`confirm_life_for_today`。行の無い日付に限り、旧置き場
  persona_day_plan.meta_json.lives を読み取り専用で参照する (互換読み)
- **起床・就寝の節目** — :func:`handle_scheduled_life_boundary` (ScheduleManager
  と watchdog が呼ぶ。LLM を呼ばない)
- **営業日の解決** — :func:`resolve_business_day` (keep-alive・スルース・
  watchdog が同じ解決器を見る)
- **keep-alive のライフ従属ゲート** — :func:`is_keepalive_allowed`
- **ライフの帳簿への記帳** — :func:`record_judgment_pulse` (判断点の別枠) /
  :func:`consume_life_pulse` (自発活動の回数)
- **「ユーザー会話中か」の唯一の判定** — :func:`is_in_user_conversation` /
  :func:`get_user_conversation_state`

旧 persona_day_plan テーブルは定義ごと残置する (既存ユーザーのデータが載った
まま上がってくるため)。読むのはライフの互換読み (:func:`_legacy_lives_in_session`)
だけで、書き手はもう無い。

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from sqlalchemy.orm import Session

from saiverse import clock

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ライフ (life.md v0.5 §3/§4: ユーザーが設定する起床・就寝の区間)
#
# v0.4 までは起床判断 (day_open) で LLM がライフを「宣言」していたが、実機初日
# (2026-07-13) にペルソナが過去起点・予算不整合のライフを宣言できてしまう
# 破綻が起き、まはー裁定で責任分界を全面改訂した — ライフ = ユーザーが設定する
# 起床・就寝の区間 (PersonaSchedule が器)。ペルソナは宣言しない。以下の宣言口
# 検証 (重なり・谷コマ・均等モード間隔) は廃止し、システムが起床時刻に
# :func:`confirm_life_for_today` で確定して焼く。
#
# 2026-10 (autonomous_behavior_v04_plan.md 段 1-2、v3 §6「起床・就寝は機械の
# 帳簿処理のみ」): 確定と起床・就寝の節目処理は判断点 (fire_judgment_point) から
# 切り出され、:func:`handle_scheduled_life_boundary` が LLM なしで行う。置き場も
# persona_day_plan.meta_json から persona_life テーブルへ独立した。段 1-4 で
# 時間割 (コマ・予算ゲート・ラウンド台帳と κ 減衰) が撤去され、ライフの帳簿に
# 残るのは予算 (budget_pulses)・自発活動の回数 (used_pulses)・判断点の別枠
# (judgment_pulses)・節目のマーカー (started / ended)。
# ---------------------------------------------------------------------------

#: ライフのモード (life.md §5.1): 均等 = 標準パルスの間隔を TTL 内に保つ
#: (Anthropic/OpenAI 系のキャッシュ延命に最適)。自由 = 間隔制約なし (Gemini 等)。
LIFE_MODE_EVEN = "even"
LIFE_MODE_FREE = "free"
LIFE_MODES = (LIFE_MODE_EVEN, LIFE_MODE_FREE)

#: 均等モードで許容するパルス間隔の上限 (分)。TTL (Anthropic 1h) ちょうどは
#: 遅延で割れるため安全マージンを引いた初期値 (life.md §12-2)。均等モードの
#: 最低予算 (:func:`_min_life_budget`) の基準値として使う (life.md §4.2)。
LIFE_EVEN_MAX_GAP_MINUTES = 50

#: 旧置き場 persona_day_plan.meta_json の lives 配列のキー (life.md §11.2)。
#: ライフの正の置き場は persona_life テーブル (autonomous_behavior_v04_plan.md
#: 段 1-2)。このキーは互換読み (:func:`_legacy_lives_in_session`) だけが読む —
#: persona_day_plan 撤去時に互換読みごと消す。
META_LIVES = "lives"

#: DEFAULT_MODEL の provider がこの集合に属せば既定モードは均等 (life.md §5.1)。
_EVEN_MODE_PROVIDERS = frozenset({"anthropic", "openai"})

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def is_valid_hhmm(value: Any) -> bool:
    """"HH:MM" 形式の妥当性チェック (ライフ設定 API 等、保存前バリデーションの共用口)。"""
    return isinstance(value, str) and bool(_TIME_RE.match(value))


def _normalize_plan_date(plan_date: Any) -> str:
    """plan_date を "YYYY-MM-DD" 文字列へ正規化する。"""
    if isinstance(plan_date, datetime):
        return plan_date.date().isoformat()
    if isinstance(plan_date, date):
        return plan_date.isoformat()
    if isinstance(plan_date, str):
        try:
            return date.fromisoformat(plan_date.strip()).isoformat()
        except ValueError as exc:
            raise ValueError(f"invalid plan_date: {plan_date!r} (expected YYYY-MM-DD)") from exc
    raise ValueError(f"invalid plan_date type: {type(plan_date).__name__}")


#: CAS 再試行の上限 (:func:`_mutate_lives`)。
_CAS_MAX_RETRIES = 5


def _load_plan_row(db: Session, persona_id: str, plan_date_str: str) -> Any:
    """PersonaDayPlan 行を与えられた Session で読む (無ければ None)。"""
    from database.models import PersonaDayPlan

    return (
        db.query(PersonaDayPlan)
        .filter_by(persona_id=persona_id, plan_date=plan_date_str)
        .first()
    )


def _life_minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


# ---------------------------------------------------------------------------
# ライフの置き場 (persona_life テーブル、autonomous_behavior_v04_plan.md 段 1-2)
#
# 正の置き場は persona_life (1 ペルソナ 1 営業日 1 行、LIVES_JSON = ライフ配列)。
# 以前は persona_day_plan.meta_json.lives に同居していた。
#
# 互換読み: persona_life に行が**無い**日付に限り、旧 meta_json.lives を読み取り
# 専用で参照する (切り替えた当日の「前日の深夜跨ぎライフ」の営業日解決を守る
# ため)。旧置き場へは書き戻さない。書き手 (:func:`_mutate_lives`) が行の無い
# 日付に書くときは、互換読みで得たライフを種にして persona_life に新しい行を
# 作る (旧ライフの帳簿 — started / used_* — を新しい置き場で引き継ぐ)。
# persona_day_plan 撤去時にこの互換読みも消す。
# ---------------------------------------------------------------------------


def _load_life_row(db: Session, persona_id: str, plan_date_str: str) -> Any:
    """PersonaLife 行を与えられた Session で読む (無ければ None)。"""
    from database.models import PersonaLife

    return (
        db.query(PersonaLife)
        .filter_by(PERSONA_ID=persona_id, PLAN_DATE=plan_date_str)
        .first()
    )


def _parse_lives_payload(
    raw: Any,
    *,
    strict: bool,
    persona_id: str,
    plan_date_str: str,
    source: str,
) -> List[Dict[str, Any]]:
    """ライフ配列の JSON 文字列 / 値を list[dict] に読む。

    ``strict`` は :func:`get_lives` と同じ意味: 壊れていたら例外。既定は空リスト /
    不正要素の除去へ縮退する。``raw`` が JSON 文字列でなく読み済みの値
    (旧 meta_json.lives) でもよい。
    """
    lives = raw
    if isinstance(raw, str):
        try:
            lives = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            if strict:
                raise ValueError(
                    f"{source} is not valid JSON (persona={persona_id} "
                    f"date={plan_date_str})"
                )
            LOGGER.warning(
                "[day_plan] %s is not valid JSON (persona=%s date=%s); "
                "treating as no lives", source, persona_id, plan_date_str,
            )
            return []
    if not isinstance(lives, list):
        if strict and lives is not None:
            raise ValueError(
                f"{source} is not a list (persona={persona_id} "
                f"got={type(lives).__name__})"
            )
        return []
    if strict and any(not isinstance(life, dict) for life in lives):
        raise ValueError(
            f"{source} contains non-object entries (persona={persona_id})"
        )
    return [life for life in lives if isinstance(life, dict)]


def _legacy_lives_in_session(
    db: Session, persona_id: str, plan_date_str: str, *, strict: bool = False
) -> List[Dict[str, Any]]:
    """互換読み: 旧置き場 persona_day_plan.meta_json.lives を**読み取り専用**で読む。

    persona_life に行が無い日付だけが呼ぶ。旧置き場へは決して書かない。
    persona_day_plan 撤去時にこの互換読みも消す。
    """
    row = _load_plan_row(db, persona_id, plan_date_str)
    if row is None or not row.meta_json:
        return []
    try:
        meta = json.loads(row.meta_json)
    except (TypeError, ValueError):
        if strict:
            raise ValueError(
                f"meta_json is not valid JSON (persona={persona_id} "
                f"date={plan_date_str})"
            )
        LOGGER.warning(
            "[day_plan] legacy meta_json is not valid JSON (persona=%s date=%s); "
            "treating as no lives", persona_id, plan_date_str,
        )
        return []
    if not isinstance(meta, dict):
        if strict:
            raise ValueError(
                f"meta_json is not a JSON object (persona={persona_id} "
                f"date={plan_date_str} got={type(meta).__name__})"
            )
        return []
    return _parse_lives_payload(
        meta.get(META_LIVES), strict=strict, persona_id=persona_id,
        plan_date_str=plan_date_str, source=f"meta_json.{META_LIVES}",
    )


def _read_lives_in_session(
    db: Session, persona_id: str, plan_date_str: str, *, strict: bool = False
) -> Tuple[Any, List[Dict[str, Any]]]:
    """``(persona_life 行 or None, ライフ配列)`` を同じ Session で読む。

    行があればその LIVES_JSON が正 (空配列でも — 旧置き場は見ない)。行が無ければ
    互換読み (:func:`_legacy_lives_in_session`) へ落ちる。
    """
    row = _load_life_row(db, persona_id, plan_date_str)
    if row is not None:
        return row, _parse_lives_payload(
            row.LIVES_JSON, strict=strict, persona_id=persona_id,
            plan_date_str=plan_date_str, source="persona_life.LIVES_JSON",
        )
    return None, _legacy_lives_in_session(
        db, persona_id, plan_date_str, strict=strict,
    )


def _mutate_lives(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    mutate: Callable[[List[Dict[str, Any]]], Optional[Any]],
    *,
    context: str = "",
    in_session_extra: Optional[Callable[[Any], None]] = None,
) -> Optional[Any]:
    """ライフ配列を CAS 再試行つきで変異させる — ライフの書き手の唯一の口。

    契約 (旧 ``mutate_plan_meta`` — 時間割の meta の書き手、段 1-4 で撤去 — から
    引き継いだもの):

    - 読み → 計算 → 保存を**同じ CAS 試行の内側**で行う。競合のたびに
      ``mutate`` が最新のライフ配列で呼び直され、並走した積算を失わない
      (第七陣 P1 の契約)
    - ``mutate`` は受け取ったリストをその場で書き換え、非 None (呼び出し元へ
      返す結果) を返す。None = 書かずに中止 (行も作らない)
    - ``in_session_extra`` は書き込みが確定する試行の **commit 直前**に同じ
      Session で一度だけ呼ばれる (W5: ライフのマーカーと実行台帳の applied +
      outbox を単一 commit に同梱する口)。例外は試行ごと rollback して伝播

    行が無い日付は互換読み (旧 meta_json.lives) を種にして新しい行を INSERT
    する (旧置き場へは書き戻さない)。

    読みは strict (2026-10-10 Codex 敵対レビュー 5 巡目): 壊れた記録 (persona_life
    の LIVES_JSON / 種にする旧 meta_json) を空と読むと、その上に新しい内容を
    書いて破損を隠す (save_lives) か、「対象なし」の no-op を成功と取り違える
    (境界マーカー)。壊れていたら書かずに送出する。

    Raises:
        RuntimeError: 再試行が枯渇した場合 (書けていない — silent 消失にしない)。
        ValueError: 読んだライフの記録が壊れている場合 (何も書いていない)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    from sqlalchemy.exc import IntegrityError

    from database.models import PersonaLife

    for _attempt in range(_CAS_MAX_RETRIES):
        now = clock.now()
        db = manager.SessionLocal()
        try:
            row = _load_life_row(db, persona_id, plan_date_str)
            if row is None:
                lives = _legacy_lives_in_session(
                    db, persona_id, plan_date_str, strict=True,
                )
                result = mutate(lives)
                if result is None:
                    return None
                db.add(PersonaLife(
                    PERSONA_ID=persona_id,
                    PLAN_DATE=plan_date_str,
                    LIVES_JSON=json.dumps(lives, ensure_ascii=False),
                    CREATED_AT=now,
                    UPDATED_AT=now,
                ))
                if in_session_extra is not None:
                    try:
                        in_session_extra(db)
                    except Exception:
                        db.rollback()
                        raise
                try:
                    db.commit()
                    return result
                except IntegrityError:
                    # 並走の書き手が先に行を作った — 更新経路で再試行
                    db.rollback()
                    continue
            original = row.LIVES_JSON
            lives = _parse_lives_payload(
                original, strict=True, persona_id=persona_id,
                plan_date_str=plan_date_str, source="persona_life.LIVES_JSON",
            )
            result = mutate(lives)
            if result is None:
                return None
            changed = (
                db.query(PersonaLife)
                .filter(
                    PersonaLife.PERSONA_ID == persona_id,
                    PersonaLife.PLAN_DATE == plan_date_str,
                    PersonaLife.LIVES_JSON == original,
                )
                .update(
                    {
                        PersonaLife.LIVES_JSON: json.dumps(
                            lives, ensure_ascii=False,
                        ),
                        PersonaLife.UPDATED_AT: now,
                    },
                    synchronize_session=False,
                )
            )
            if changed and in_session_extra is not None:
                try:
                    in_session_extra(db)
                except Exception:
                    db.rollback()
                    raise
            db.commit()
            if changed:
                return result
        finally:
            db.close()
        LOGGER.info(
            "[day_plan] lives CAS conflict (%s): lives changed since read; "
            "retrying with fresh lives (persona=%s date=%s attempt=%d/%d)",
            context, persona_id, plan_date_str, _attempt + 1, _CAS_MAX_RETRIES,
        )
    raise RuntimeError(
        f"lives mutation kept conflicting with concurrent writes "
        f"(persona={persona_id} date={plan_date_str} context={context})"
    )


def get_lives(
    manager: Any, persona_id: str, plan_date: Any, *, strict: bool = False
) -> List[Dict[str, Any]]:
    """保存済みライフ (persona_life) を返す。無ければ空リスト。

    persona_life に行が無い日付に限り、旧置き場 (persona_day_plan.meta_json.lives)
    を読み取り専用で参照する (互換読み — 上の節の注記参照)。

    「lives が無い日 (旧データ・宣言なし) は検証もゲートも従来挙動」の判定は
    すべてこの関数の戻り値が空かどうかで行う (life.md §4.1)。

    Args:
        strict: True で「壊れていて読めない」を例外にする (LIVES_JSON / 旧
            meta_json が不正 JSON、lives が list でない / 要素が dict でない)。
            営業日の選択と、起床・就寝の節目 (ライフの確定・世代の検査・
            終了対象の読み) が True で呼ぶ — 壊れた台帳を「ライフ未宣言の日」と
            読むと、現行スケジュール基準で別の営業日を駆動する・壊れた記録を
            新しいライフで覆う・「ライフ無しの日」で成功と封印する。既定
            (False) は空リスト / 不正要素の除去へ縮退する (表示などの読み手)。

    Raises:
        ValueError: ``strict`` かつ台帳が壊れている場合。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    db = manager.SessionLocal()
    try:
        _row, lives = _read_lives_in_session(
            db, persona_id, plan_date_str, strict=strict,
        )
        return lives
    finally:
        db.close()


def _life_extended_minutes(life: Dict[str, Any], hhmm: str) -> int:
    """ライフの開始を 0 とした経過分。深夜跨ぎでは 1440 を超えうる。

    跨ぎライフ (例: 07:00〜01:00) で "23:30" と "03:00" を同じ数直線上に
    正しく並べるための変換 (life.md v0.5 §11.2「区間内判定の書き直し」)。
    hhmm がライフの開始より前の時刻なら「翌暦日の続き」とみなして +1440 する。
    """
    start_min = _life_minutes(life["start"])
    target_min = _life_minutes(hhmm)
    if target_min < start_min:
        target_min += 24 * 60
    return target_min - start_min


def _life_span_minutes(life: Dict[str, Any]) -> int:
    """ライフの長さ (分)。深夜跨ぎ込み (例: 07:00〜01:00 = 1080 分)。"""
    start_min = _life_minutes(life["start"])
    end_min = _life_minutes(life["end"])
    if end_min <= start_min:
        end_min += 24 * 60
    return end_min - start_min


def get_life_for_time(lives: List[Dict[str, Any]], hhmm: str) -> Optional[int]:
    """hhmm ("HH:MM") が属するライフの index を返す (無ければ None)。

    深夜跨ぎ (end <= start) を正常形として扱う (life.md v0.5 §4.1)。
    """
    for i, life in enumerate(lives):
        if not life.get("start") or not life.get("end"):
            continue
        ext = _life_extended_minutes(life, hhmm)
        if 0 <= ext < _life_span_minutes(life):
            return i
    return None


def is_keepalive_allowed(manager: Any, persona_id: str) -> bool:
    """keep-alive 連鎖のライフ従属ゲート (life.md §5.2)。

    その日 lives が宣言されていれば、現在時刻がいずれかのライフ区間内のときだけ
    True (谷では False = keep-alive を止める)。lives 未宣言の日 / ペルソナは
    常に True (旧挙動のまま — 完全後方互換)。判定失敗時は True にフォールバック
    する (安全側は「温め続ける」— keep-alive を止める方向に倒さない)。

    ``sea.runtime.SEARuntime.run_cache_keepalive`` が唯一の呼び出し元
    (keep-alive 連鎖の判定を 1 箇所に集約する設計、life.md Phase3)。

    見る営業日は :func:`resolve_business_day` — 予約・watchdog と同じ解決器
    (現在時刻を含む確定ライフ優先)。ここだけ現行 PersonaSchedule で日を決めると、
    起床設定を日中に変えた日はライフの真っ最中に「ライフ未宣言の日」を読む。
    """
    try:
        basis = resolve_business_day(manager, persona_id)
        if basis is None or not basis.lives:
            return True
        hhmm = clock.now().strftime("%H:%M")
        return get_life_for_time(basis.lives, hhmm) is not None
    except Exception:
        LOGGER.warning(
            "[day_plan] is_keepalive_allowed failed (persona=%s); defaulting to allow",
            persona_id, exc_info=True,
        )
        return True


def get_life_status_now(manager: Any, persona_id: str) -> Dict[str, Any]:
    """現在時刻のライフ状態 — life.md §9.1「話しかけやすさ」表示の唯一の判定源。

    試金石「エアは今話しかけて大丈夫か」への機械回答。API 層 (occupants の
    常在インジケータ / day-plan のライフ状態) はどちらもこの関数を呼び、
    判定ロジックを二重化しない。

    Returns:
        {
          "lives_declared": bool,   # その営業日にライフが宣言されているか
          "in_life": bool,          # lives_declared かつ現在時刻がいずれかの区間内
          "life_index": int | None, # in_life なら対象ライフの index (get_lives の並び)
          "life": dict | None,      # in_life なら対象ライフの宣言 dict そのもの
          "plan_date": str | None,  # 判定に使った営業日 ("YYYY-MM-DD")
        }

        判定失敗時は lives_declared=False 側にフォールバックする——「未宣言」
        表示 (何も出さない) の方が「熱くないのに熱いと見せる」より安全
        (不変条件5)。is_keepalive_allowed の「失敗時は許可側 (True)」とは
        安全方向が逆であることに注意 (あちらは延命を止めない方が安全、
        こちらは嘘の「話しかけやすい」を出さない方が安全)。

    見る営業日は :func:`resolve_business_day` — 予約・watchdog と同じ解決器。
    起床設定を日中に変えた日の深夜、ペルソナが確定ライフの真っ最中でも
    「未宣言」と表示していたのはここが現行 PersonaSchedule で日を決めていたため。
    """
    try:
        basis = resolve_business_day(manager, persona_id)
        if basis is None:
            # ライフを読めない = どの営業日の状態も判定できない。「未宣言」
            # (何も出さない) 側へ倒す — 上の Returns の方針そのもの。
            LOGGER.warning(
                "[day_plan] get_life_status_now: lives unreadable (persona=%s); "
                "reporting lives_declared=False", persona_id,
            )
            return {
                "lives_declared": False, "in_life": False,
                "life_index": None, "life": None, "plan_date": None,
            }
        if not basis.lives:
            return {
                "lives_declared": False, "in_life": False,
                "life_index": None, "life": None, "plan_date": basis.plan_date,
            }
        hhmm = clock.now().strftime("%H:%M")
        idx = get_life_for_time(basis.lives, hhmm)
        return {
            "lives_declared": True,
            "in_life": idx is not None,
            "life_index": idx,
            "life": basis.lives[idx] if idx is not None else None,
            "plan_date": basis.plan_date,
        }
    except Exception:
        LOGGER.warning(
            "[day_plan] get_life_status_now failed (persona=%s); "
            "defaulting to lives_declared=False", persona_id, exc_info=True,
        )
        return {
            "lives_declared": False, "in_life": False,
            "life_index": None, "life": None, "plan_date": None,
        }


def _validate_and_normalize_lives(lives: Any) -> List[Dict[str, Any]]:
    """ライフ配列の型検証 (v0.5): システムが構築した値の型だけを守る。

    v0.4 までの LLM 宣言口前提の検証 (ライフ同士の重なり・谷コマ・均等モード
    間隔) は**廃止した**——ライフはユーザー設定 (PersonaSchedule の起床・
    就寝) からシステム (:func:`confirm_life_for_today`) が確定するため、
    その手の不整合は書ける口ごと構造的に無くなった (life.md v0.5 §3
    「不正な値は検証で弾くのでなく、書ける口をなくす」)。

    検証項目 (フォーマットのみ):
    - start/end は "HH:MM"
    - start と end が同一でない (長さ 0 のライフは無効)
    - budget_pulses は正の int
    - mode は "even" / "free" のみ

    深夜跨ぎ (end <= start、例 07:00〜01:00) はここでは**正常形として許容**
    する (:func:`autonomy_wiring.in_waking_window` と同じ意味論)。

    Raises:
        ValueError: 上記いずれかの違反。
    """
    if not isinstance(lives, list):
        raise ValueError(f"lives must be a list (got {type(lives).__name__})")
    if not lives:
        return []

    normalized: List[Dict[str, Any]] = []
    for i, life in enumerate(lives):
        if not isinstance(life, dict):
            raise ValueError(f"lives[{i}] must be a dict (got {type(life).__name__})")
        start = life.get("start")
        if not isinstance(start, str) or not _TIME_RE.match(start):
            raise ValueError(f"lives[{i}].start must be 'HH:MM' (got {start!r})")
        end = life.get("end")
        if not isinstance(end, str) or not _TIME_RE.match(end):
            raise ValueError(f"lives[{i}].end must be 'HH:MM' (got {end!r})")
        if end == start:
            raise ValueError(
                f"lives[{i}]: start と end が同一です ({start!r}) — 長さ 0 のライフは無効です"
            )
        budget = life.get("budget_pulses")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ValueError(
                f"lives[{i}].budget_pulses must be a positive int (got {budget!r})"
            )
        mode = life.get("mode")
        if mode not in LIFE_MODES:
            raise ValueError(f"lives[{i}].mode must be one of {LIFE_MODES} (got {mode!r})")
        normalized.append({"start": start, "end": end, "budget_pulses": budget, "mode": mode})
    return normalized


def save_lives(
    manager: Any, persona_id: str, plan_date: Any, lives: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """ライフを検証して保存する (システムが起床時刻の確定で呼ぶ書き手)。

    既存ライフと (start, end) が一致する行は積算済みの帳簿 (used_pulses /
    judgment_pulses) を引き継ぐ — 再確定 (起床の再発火等) で消費や判断点回数の
    帳簿をリセットしない。一致しない (新規・時刻変更の) ライフは 0 から
    始まる。旧ラウンド台帳の ``used_rounds`` は時間割の撤去 (段 1-4) で書き手
    ごと消えたので、新しく保存する行には載せない。

    Raises:
        ValueError: :func:`_validate_and_normalize_lives` の検証失敗。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    normalized = _validate_and_normalize_lives(lives)

    # 消費の引き継ぎは最新のライフから CAS の内側で行う (外で読んだ古い消費を
    # 完成値として書くと、読みと書きの間に積まれた消費が巻き戻る — 第七陣 P1)。
    def _apply(lives: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        existing = {
            (life.get("start"), life.get("end")): life for life in lives
        }
        for life in normalized:
            prev = existing.get((life["start"], life["end"]))
            if prev is not None:
                life["used_pulses"] = int(prev.get("used_pulses") or 0)
                life["judgment_pulses"] = int(prev.get("judgment_pulses") or 0)
            else:
                life["used_pulses"] = 0
                life["judgment_pulses"] = 0
        lives[:] = normalized
        return normalized

    _mutate_lives(
        manager, persona_id, plan_date_str, _apply, context="save_lives",
    )
    LOGGER.info(
        "[day_plan] lives saved: persona=%s date=%s lives=%d",
        persona_id, plan_date_str, len(normalized),
    )
    return normalized


def derive_default_life_mode(manager: Any, persona_id: str) -> str:
    """ペルソナの標準モデル (DEFAULT_MODEL) の provider からライフモードの既定を導出する。

    Anthropic/OpenAI 系は均等 (explicit/implicit cache の TTL 再送延命が効く)、
    それ以外 (Gemini/Ollama 等) は自由 (life.md §5.1)。モデル未解決・provider
    不明時は安全側 (自由 = 間隔制約なし) に倒す。
    """
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    model = getattr(persona, "model", None)
    if not model:
        return LIFE_MODE_FREE
    try:
        from saiverse.model_configs import get_model_config
        provider = str(get_model_config(model).get("provider") or "")
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to resolve provider for model=%r (persona=%s); "
            "defaulting life mode to free", model, persona_id, exc_info=True,
        )
        return LIFE_MODE_FREE
    return LIFE_MODE_EVEN if provider in _EVEN_MODE_PROVIDERS else LIFE_MODE_FREE


def _life_window_minutes(wake: str, close: str) -> int:
    """wake〜close の長さ (分)。深夜跨ぎ (close < wake) を正常形として扱う。"""
    start_min = _life_minutes(wake)
    end_min = _life_minutes(close)
    if end_min <= start_min:
        end_min += 24 * 60
    return end_min - start_min


def _min_life_budget(mode: str, window_minutes: int) -> int:
    """ライフの最低予算 (life.md v0.5 §4.2)。

    均等モード: キャッシュを繋ぐには :data:`LIFE_EVEN_MAX_GAP_MINUTES`
    (既定 50 分) に 1 回のパルスが物理的に必要 → ``ceil(窓の長さ ÷ 50分)``。
    自由モード: キャッシュ制約は無いが、1 回も動けない予算は無意味
    なので最低 1。
    """
    if mode == LIFE_MODE_EVEN:
        return max(1, math.ceil(window_minutes / LIFE_EVEN_MAX_GAP_MINUTES))
    return 1


def life_mode_and_min_budget(
    manager: Any,
    persona_id: str,
    wake: Optional[str],
    close: Optional[str],
    mode_override: Optional[str] = None,
) -> Dict[str, Any]:
    """ライフ設定 UI (life.md v0.5 §9.2-1) 向け: 実効モードと最低予算をまとめて
    計算する副作用なしの読み取り専用ヘルパ。

    :func:`confirm_life_for_today` と同じ「モード決定」「最低予算」ロジックを
    共有するが、DB へは何も書かない (プレビュー・バリデーション専用)。

    Args:
        wake/close: "HH:MM"。どちらか欠けていれば窓長・最低予算は計算できない
            ( ``window_minutes`` / ``min_budget_pulses`` は None)。
        mode_override: ユーザーによる明示上書き ("even"/"free")。
            :data:`LIFE_MODES` に無い値は無視して自動判定にフォールバックする
            (life.md §5.1: 上書きは設定 UI からの脱出口のみ)。

    Returns:
        ``{"derived_mode", "effective_mode", "window_minutes", "min_budget_pulses"}``
    """
    derived = derive_default_life_mode(manager, persona_id)
    effective = mode_override if mode_override in LIFE_MODES else derived
    window_minutes: Optional[int] = None
    min_budget: Optional[int] = None
    if wake and close and is_valid_hhmm(wake) and is_valid_hhmm(close):
        window_minutes = _life_window_minutes(wake, close)
        min_budget = _min_life_budget(effective, window_minutes)
    return {
        "derived_mode": derived,
        "effective_mode": effective,
        "window_minutes": window_minutes,
        "min_budget_pulses": min_budget,
    }


def confirm_life_for_today(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    wake: Optional[str],
    close: Optional[str],
    requested_budget_pulses: Optional[int] = None,
    mode_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """起床時刻に、ユーザー設定 (PersonaSchedule の起床・就寝 + 予算) から今日の
    ライフを確定して persona_life に焼く (life.md v0.5 §3/§4/§8.1)。呼び出し元は
    :func:`handle_scheduled_life_boundary` (機械の帳簿処理 — LLM なし)。

    - **区間**: wake〜close をそのまま使う (深夜跨ぎも正常形)
    - **モード**: ``mode_override`` (ユーザー設定、"even"/"free") が
      :data:`LIFE_MODES` に入っていればそれを、無ければ
      :func:`derive_default_life_mode` (provider 導出) を使う。上書きは
      ライフ設定 UI からの明示的な脱出口のみ (life.md §5.1) —
      ペルソナ自身は選ばない
    - **予算**: ``requested_budget_pulses`` (ユーザー設定、PersonaSchedule の
      PLAYBOOK_PARAMS.daily_budget_pulses 由来) が最低値
      (:func:`_min_life_budget`) 以上ならそれを、未設定/最低値未満なら
      最低値へ切り上げる (INFO ログ)

    **冪等**: 当日すでにライフが焼かれていれば (:func:`get_lives` が非空)
    何もせず既存の 1 件目をそのまま返す — 再起動での watchdog 再発火等で
    二重に焼き直して used_pulses / judgment_pulses の帳簿をリセットしない。

    就寝スケジュール未設定 (``close`` が None) は「ライフ無し日」(従来動作) —
    起床時刻だけでは活動区間が定義できない。``wake`` が無い場合も同様
    (v2 の一日リズム自体が未設定)。

    Returns:
        確定した (または既存の) ライフ dict。ライフ無し日は None。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    # strict: 壊れた記録を「未確定」と読むと、新しいライフで上書きして破損を
    # 隠すか、起床・就寝未設定なら「ライフ無しの日」で決着させてしまう
    # (2026-10-10 Codex 敵対レビュー 5 巡目)。壊れていたら送出する。
    existing = get_lives(manager, persona_id, plan_date_str, strict=True)
    if existing:
        LOGGER.debug(
            "[day_plan] life already confirmed for today; skipping re-confirmation "
            "(persona=%s date=%s)", persona_id, plan_date_str,
        )
        return existing[0]

    if not wake or not close:
        LOGGER.info(
            "[day_plan] no wake/close schedule; no life declared today "
            "(persona=%s date=%s wake=%r close=%r)",
            persona_id, plan_date_str, wake, close,
        )
        return None

    if mode_override in LIFE_MODES:
        mode = mode_override
        LOGGER.info(
            "[day_plan] life mode overridden by user setting: persona=%s mode=%s",
            persona_id, mode,
        )
    else:
        mode = derive_default_life_mode(manager, persona_id)
    window_minutes = _life_window_minutes(wake, close)
    min_budget = _min_life_budget(mode, window_minutes)

    budget = requested_budget_pulses
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        budget = min_budget
        LOGGER.info(
            "[day_plan] no valid budget configured; using minimum %dパルス "
            "(persona=%s mode=%s window=%d分)",
            min_budget, persona_id, mode, window_minutes,
        )
    elif budget < min_budget:
        LOGGER.info(
            "[day_plan] configured budget %dパルス below minimum %d; "
            "clamped up (persona=%s mode=%s window=%d分)",
            budget, min_budget, persona_id, mode, window_minutes,
        )
        budget = min_budget

    saved = save_lives(manager, persona_id, plan_date_str, [
        {"start": wake, "end": close, "budget_pulses": budget, "mode": mode},
    ])
    life = saved[0]
    LOGGER.info(
        "[day_plan] life confirmed: persona=%s date=%s %s-%s budget=%dパルス mode=%s",
        persona_id, plan_date_str, wake, close, budget, mode,
    )
    return life


def _ledger_plan_date(
    manager: Any, persona_id: str, plan_date: Any, *, what: str
) -> Optional[str]:
    """ライフ台帳へ記帳する対象の営業日。省略時は自己解決する。

    解決器は予約・watchdog・表示と同じ :func:`resolve_business_day` (現在時刻を
    含む確定ライフ優先)。**解決できないときは None** — 呼び出し元は記帳しない
    こと。どの日か分からないまま積むと、別のライフの帳簿に数字が乗る (欠落は
    後から追えるが、他人の帳簿に乗った数字は追えない)。
    """
    if plan_date is not None:
        return _normalize_plan_date(plan_date)
    basis = resolve_business_day(manager, persona_id)
    if basis is None:
        LOGGER.warning(
            "[day_plan] cannot resolve the business day (lives unreadable); "
            "not recording %s (persona=%s)", what, persona_id,
        )
        return None
    return basis.plan_date


def consume_life_pulse(
    manager: Any,
    persona_id: str,
    plan_date: Any = None,
    *,
    at_time: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """自発活動 1 回をその時刻が属するライフの予算へ積算する
    (life.md §5.3/§8.2、2026-08-08 追補で単位を改訂)。

    予算が数えるのは **ペルソナが自分から動いた 1 回** — 利用者向けの意味は
    「その日の自発活動の回数 (used / budget)」。

    ⚠ **現在この関数を呼ぶ実体は無い**。唯一の呼び手だった暮らしコマ
    (出かける / 自室で過ごす) が時間割ごと撤去された (autonomous_behavior_v04_plan.md
    段 1-4)。記帳の器は残す — 次の呼び手は v0.4 のティック (v3 §5) で、自発の
    一手を数える単位はそちらで決める。

    数えないもの: keep-alive と、判断点 (イベント到着) の発火 — 判断点の回数は
    別枠 (:func:`record_judgment_pulse`) で観測する。

    lives が無い日 / パルス時刻がどのライフにも属さない場合は no-op
    (None、後方互換)。

    Args:
        plan_date: 省略時は現在時刻が属する営業日を自己解決する
            (:func:`resolve_business_day`)。解決できない (ライフを読めない) ときは
            記帳せず no-op + WARN — 別のライフの帳簿へ積むより欠落の方が軽い。
        at_time: パルス時刻 "HH:MM"。省略時は ``clock.now()``。
    """
    plan_date_str = _ledger_plan_date(manager, persona_id, plan_date, what="pulse")
    if plan_date_str is None:
        return None
    hhmm = at_time or clock.now().strftime("%H:%M")
    return _increment_life_field(
        manager, persona_id, plan_date_str, hhmm, "used_pulses", 1, what="pulse",
    )


def _increment_life_field(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    hhmm: str,
    field: str,
    inc: int,
    *,
    what: str,
) -> Optional[Dict[str, Any]]:
    """``hhmm`` が属するライフの ``field`` へ ``inc`` を積算する共通実装。

    ライフの解決と増分計算を :func:`_mutate_lives` の CAS 試行の**内側**で行う
    — 外で読んだ lives に増分を足した完成値を書くと、並走した積算が失われる
    (第七陣 P1: record_judgment_pulse 2 本並走で judgment_pulses が 2 でなく 1 に
    なる再現)。lives が無い日 / どのライフにも属さない時刻は no-op (None、行も
    作らない)。
    """

    def _add(lives: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not lives:
            return None
        idx = get_life_for_time(lives, hhmm)
        if idx is None:
            LOGGER.info(
                "[day_plan] %s at %s does not belong to any declared life "
                "(persona=%s date=%s); not counted",
                what, hhmm, persona_id, plan_date_str,
            )
            return None
        lives[idx][field] = int(lives[idx].get(field) or 0) + inc
        return lives[idx]

    return _mutate_lives(
        manager, persona_id, plan_date_str, _add, context=f"increment:{field}",
    )


def _life_mark_mutator(
    index: int, field: str
) -> Callable[[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """lives[index] に境界マーカー (``started`` / ``ended``) を立てる mutate 閉包。

    :func:`_mutate_lives` の CAS 試行の内側で評価される (第七陣 P1 の契約 —
    外で読んだライフから完成値を作らない)。lives が無い日 / index 外 / 既マークは
    None (no-op — 書かない)。

    マーカーの意味: ライフ境界の節目処理 (:func:`apply_life_boundary`) は
    非冪等 (通知を含む) なので、schedule 側 backoff 再試行や watchdog の
    再発火で :func:`handle_scheduled_life_boundary` が再突入しても
    節目が (persona, 営業日) につき一度で済むよう、済んだことをここに永続する
    (Codex W3 第二陣 P1 / 第八陣)。「確認 → 適用 → マーク」の順で、マーク
    先行だと適用されないまま封印される。
    """
    def _mark(lives: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if index >= len(lives):
            return None
        if lives[index].get(field):
            return None  # 既にマーク済み — 書かない
        lives[index][field] = True
        return lives[index]

    return _mark


#: ライフ境界の実行台帳 KIND (W5)。冪等キーは "{persona}:{plan_date}" —
#: (persona, 営業日, 境界種) につき一つの実行。
LIFE_BOUNDARY_KIND_START = "life.boundary_start"
LIFE_BOUNDARY_KIND_END = "life.boundary_end"


def _life_boundary_outbox_items(
    manager: Any, persona_id: str, text: str
) -> list:
    """境界通知の outbox item 列を組み立てる (W5)。

    配送先 (ペルソナの adapter) が無ければ空 — 通知なしで決着する (ペルソナ
    未ロード等は再試行しても届く見込みが無く、失敗扱いにすると節目が永久に
    閉じない)。様式は Track 切替通知と同じ (``<system>`` ラップの user
    メッセージ、event_message タグ、キャッシュ無破壊 — life.md §9.3)。
    本文・時刻は enqueue 時点で凍結する (台帳 不変条件 6) — 配送が遅延しても
    節目の時刻がずれない。時刻は仮想クロック (clock.now) を尊重しつつ
    tz-aware UTC ISO にする (naive だと adapter が UTC と解釈して ±9h ずれる)。
    """
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    if adapter is None or not hasattr(adapter, "append_persona_message"):
        return []
    from datetime import timezone
    timestamp = clock.now().astimezone(timezone.utc).isoformat()
    message = {
        "role": "user",
        "content": f"<system>[システム通知] {text}</system>",
        "timestamp": timestamp,
        "metadata": {"tags": ["internal", "event_message", "day_plan"]},
    }
    return [{
        "target": "saimemory.append",
        "payload": {"message": message, "building_id": None, "thread_suffix": None},
        "persona_id": persona_id,
    }]


def _newer_confirmed_life_date(
    manager: Any, persona_id: str, plan_date_str: str
) -> Optional[str]:
    """``plan_date_str`` より新しい営業日で、ライフが確定している最古の日 (無ければ None)。

    ライフの確定は起床の帳簿処理 (:func:`confirm_life_for_today`) だけが行う
    ので、新しい日のライフがある = 新しい一日が既に始まっている。

    読む優先順位は :func:`get_lives` と同じ: persona_life に行のある日付はその
    LIVES_JSON が正 (空配列でも旧置き場は見ない)、行の無い日付だけ旧置き場
    persona_day_plan.meta_json.lives を互換読みする。persona_life だけを見ると、
    当日のライフが旧置き場にだけある日に前日の終了を回収したとき「新しい日は
    無い」と誤判定し、現在のキャッシュ維持・TTL・本人への通知を触ってしまう
    (2026-10-10 Codex 敵対レビュー 4 巡目 修正 2)。旧置き場の日付は
    persona_day_plan から列挙する。

    読み出しの例外は送出する (呼び出し側は「分からない」を現在の世代と
    取り違えず、失敗として再試行に回す)。両置き場とも strict で読む — 壊れた
    ライフを空 (= 新しい日は無い) に丸めると、前日の終了の回収が現在の世代の
    キャッシュ維持・TTL・通知を触る (2026-10-10 Codex 敵対レビュー 5 巡目)。
    """
    from database.models import PersonaDayPlan, PersonaLife

    db = manager.SessionLocal()
    try:
        life_rows = (
            db.query(PersonaLife.PLAN_DATE, PersonaLife.LIVES_JSON)
            .filter(
                PersonaLife.PERSONA_ID == persona_id,
                PersonaLife.PLAN_DATE > plan_date_str,
            )
            .all()
        )
        life_dates = {str(newer_date) for newer_date, _raw in life_rows}
        legacy_dates = [
            str(d) for (d,) in (
                db.query(PersonaDayPlan.plan_date)
                .filter(
                    PersonaDayPlan.persona_id == persona_id,
                    PersonaDayPlan.plan_date > plan_date_str,
                )
                .all()
            )
            if str(d) not in life_dates
        ]
        candidates: List[Tuple[str, List[Dict[str, Any]]]] = [
            (
                str(newer_date),
                _parse_lives_payload(
                    raw, strict=True, persona_id=persona_id,
                    plan_date_str=str(newer_date),
                    source="persona_life.LIVES_JSON",
                ),
            )
            for newer_date, raw in life_rows
        ]
        candidates.extend(
            (
                legacy_date,
                _legacy_lives_in_session(
                    db, persona_id, legacy_date, strict=True,
                ),
            )
            for legacy_date in legacy_dates
        )
    finally:
        db.close()
    for newer_date, lives in sorted(candidates, key=lambda item: item[0]):
        if lives:
            return newer_date
    return None


def _life_boundary_staleness(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    index: int,
    boundary: str,
) -> Optional[str]:
    """節目のライフが「現在の世代」でなければ、その理由を返す (現在の世代なら None)。

    不変条件 (2026-10-10 Codex 敵対レビュー 3 巡目 修正 3):

    1. 過去の節目の**帳簿の決着** (マーカー・実行台帳) は遅れてもやってよい —
       再試行を止めるため
    2. **ペルソナの現在状態** (TTL 同期・keep-alive の停止 / 予約・本人への通知)
       に触ってよいのは、その節目のライフが現在の世代のときだけ。現在の世代 =
       そのライフがまだ ended でなく、より新しい営業日のライフが確定していない
    3. 開始の節目は、そのライフが ended 済み・またはライフの窓 (終了時刻) が
       既に過ぎている場合、「開始」として扱わない

    読むもの: より新しい営業日の persona_life (:func:`_newer_confirmed_life_date`)
    と、開始では節目のライフ自身 (``ended`` マーカーと、営業日に錨を下ろした
    区間の終端 :func:`_life_span_at` を ``clock.now()`` と比べる)。終了の節目の
    ``ended`` はここでは見ない — 既に ended なら終了のマーカーが書けずに
    no-op で閉じる (:func:`_life_mark_mutator`)。

    Returns:
        None = 現在の世代。文字列 = 古い理由 (``"superseded_by:<日付>"`` /
        ``"ended"`` / ``"window_passed"``)。台帳の result の ``stale`` に残る。
    """
    newer = _newer_confirmed_life_date(manager, persona_id, plan_date_str)
    if newer is not None:
        return f"superseded_by:{newer}"
    if boundary != LIFE_BOUNDARY_START:
        return None
    # strict: 壊れた記録を空と読むと「節目のライフが無い = 現在の世代」に
    # 落ちる (2026-10-10 Codex 敵対レビュー 5 巡目)。壊れていたら送出する。
    lives = get_lives(manager, persona_id, plan_date_str, strict=True)
    if index >= len(lives):
        return None
    life = lives[index]
    if life.get("ended"):
        return "ended"
    span = _life_span_at(date.fromisoformat(plan_date_str), life)
    if span is not None and span[1] <= clock.now():
        return "window_passed"
    return None


def _settle_stale_life_start(
    ledger: Any,
    execution_id: str,
    persona_id: str,
    plan_date_str: str,
    stale: str,
) -> bool:
    """古い開始の節目を「見送った」として台帳だけで決着させる (再試行を止める)。

    started マーカー・開始通知・TTL 同期のどれも書かない。台帳は applied (result
    に ``stale`` の印) → completed — 以後の同じ営業日の開始は claim の冪等で
    「決着済み」になり、ScheduleManager の再試行も watchdog の撃ち直しも止まる。
    """
    LOGGER.info(
        "[day_plan] life start boundary is stale (%s); not treating it as a "
        "start — no marker, no notice, no TTL sync; settling the ledger only "
        "(execution=%s persona=%s date=%s)",
        stale, execution_id, persona_id, plan_date_str,
    )
    try:
        ledger.mark_applied(
            execution_id,
            result={"boundary": LIFE_BOUNDARY_START, "stale": stale,
                    "notified": False},
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to record stale life-start boundary "
            "(execution=%s)", execution_id, exc_info=True,
        )
        try:
            ledger.mark_failed(execution_id, "life-start stale settle failed")
        except Exception:
            LOGGER.error(
                "[day_plan] failed to record life-start stale settle failure "
                "(execution=%s)", execution_id, exc_info=True,
            )
        return False
    try:
        ledger.mark_completed(execution_id)
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to close stale life-start boundary execution "
            "(execution=%s)", execution_id, exc_info=True,
        )
    return True


def apply_life_boundary(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    life: Dict[str, Any],
    *,
    boundary: str,
    index: int = 0,
) -> bool:
    """ライフ境界 (開始 / 終了) の節目を実行台帳の下で決着させる (W5)。

    W3 第六陣 P2 の恒久解 — 旧構造の二つの窓を閉じる:

    (a) 「通知の追記成功 → 成功報告前の crash」で再試行が通知を再適用する
        at-least-once 窓 → **マーカーと通知 outbox を world DB の単一 commit**
        (:func:`_mutate_lives` の ``in_session_extra`` で
        :meth:`~saiverse.execution_ledger.ExecutionLedger.mark_applied` を同梱)
        にし、配送は outbox_id 冪等 (append_ledger_message) で一度きり。
    (b) 「マーカー書き込み失敗の無条件 True + 即時リトライ 1 回」の暫定 →
        マーカーが書けなければ台帳 failed + False で正直に失敗し、schedule 側
        backoff が再試行する (claim は failed キーを退避して新 prepared を取る)。

    順序: claim → try_mark_running → 冪等段 (keep-alive cancel / TTL 同期 —
    すべて再試行安全) → 「マーカー + applied + 通知 outbox」単一 commit →
    即時配送試行 (失敗しても durable、関所 / 回復 tick が引き継ぐ)。

    実行台帳 (``manager.execution_ledger``) は SAIVerseManager が無条件に構築
    するので、台帳なしの縮退経路は持たない (旧 ``_handle_life_start`` /
    ``_handle_life_end`` の直接通知は 2026-09-28 監査で撤去)。

    冪等段の中身: 開始は TTL override (均等モードの 1h 運転)。終了は keep-alive
    予約の cancel と TTL override の遅延解除予約だけで、anchor は**触らない**
    (life.md §6.2 v0.4 — 失効は TTL に任せ、惜しい谷の生きたキャッシュを
    捨てない、§8.3)。

    Returns:
        境界が決着したか。True = 適用済み (今回適用 / 既に決着済み / 通知先
        なし)。False = 今回の適用が失敗 — 呼び出し元
        (:func:`handle_scheduled_life_boundary`) は False を返して schedule 側
        backoff に再試行させる。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    if boundary == "start":
        kind = LIFE_BOUNDARY_KIND_START
        marker_field = "started"
        notice = f"（活動開始）今日は {life['start']}〜{life['end']}。"
    elif boundary == "end":
        kind = LIFE_BOUNDARY_KIND_END
        marker_field = "ended"
        notice = "（活動終了）今日の活動時間はここまで。"
    else:
        raise ValueError(f"unknown life boundary: {boundary!r}")

    ledger = manager.execution_ledger
    execution_id, runnable, existing = ledger.claim_execution(
        kind, idempotency_key=f"{persona_id}:{plan_date_str}",
        persona_id=persona_id,
    )
    if not runnable:
        if existing in ("applied", "completed"):
            LOGGER.info(
                "[day_plan] life %s boundary already settled (execution=%s "
                "persona=%s date=%s)", boundary, execution_id, persona_id,
                plan_date_str,
            )
            return True
        LOGGER.warning(
            "[day_plan] life %s boundary claim not runnable (status=%s "
            "persona=%s date=%s) — leaving retry to the caller",
            boundary, existing, persona_id, plan_date_str,
        )
        return False
    if not ledger.try_mark_running(execution_id):
        # ほぼ同時の並走者が席を取った — 台帳へ書かず離脱 (敗者契約)
        return False

    # 世代の検査 (2026-10-10 Codex 敵対レビュー 3 巡目 修正 3): 遅れて届いた
    # 節目 (再試行・再起動後の回収) が、終わった日・次の世代のライフの「現在
    # 状態」(TTL・keep-alive・本人への通知) を書き換えないように。
    try:
        stale = _life_boundary_staleness(
            manager, persona_id, plan_date_str, index, boundary,
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] life %s boundary generation check failed (persona=%s "
            "date=%s)", boundary, persona_id, plan_date_str, exc_info=True,
        )
        try:
            ledger.mark_failed(
                execution_id, f"life-{boundary} generation check failed"
            )
        except Exception:
            LOGGER.error(
                "[day_plan] failed to record life-%s generation check failure "
                "(execution=%s)", boundary, execution_id, exc_info=True,
            )
        return False

    if stale is not None and boundary == "start":
        # 古い開始は「開始」として扱わない — started マーカーも通知も TTL 同期も
        # 書かず、台帳だけ見送りとして決着させて再試行を止める。
        return _settle_stale_life_start(
            ledger, execution_id, persona_id, plan_date_str, stale,
        )

    if stale is None:
        if boundary == "start":
            steps_ok = _sync_cache_ttl_for_life_start(manager, persona_id, life)
        else:
            steps_ok = (
                _cancel_keepalive_reservation(manager, persona_id)
                and _sync_cache_ttl_for_life_end(manager, persona_id, life)
            )
        if not steps_ok:
            try:
                ledger.mark_failed(
                    execution_id, f"life-{boundary} idempotent steps failed"
                )
            except Exception:
                LOGGER.error(
                    "[day_plan] failed to record life-%s boundary failure "
                    "(execution=%s)", boundary, execution_id, exc_info=True,
                )
            return False
        outbox_items = _life_boundary_outbox_items(manager, persona_id, notice)
    else:
        # 古い終了: 帳簿 (ended マーカー + 台帳) だけ決着させる。ペルソナの現在
        # 状態 (keep-alive の cancel・TTL 解除の予約) には触らず、終了通知も
        # 同梱しない — いま走っているのは新しい世代のライフなので。
        LOGGER.info(
            "[day_plan] life end boundary is stale (%s); settling the marker "
            "and ledger only — current cache/keep-alive state and notices "
            "untouched (persona=%s date=%s)", stale, persona_id, plan_date_str,
        )
        outbox_items = []

    def _extra(db: Any) -> None:
        result: Dict[str, Any] = {
            "boundary": boundary, "notified": bool(outbox_items),
        }
        if stale is not None:
            result["stale"] = stale
        ledger.mark_applied(
            execution_id,
            result=result,
            outbox_items=outbox_items,
            session=db,
        )

    try:
        marked = _mutate_lives(
            manager, persona_id, plan_date_str,
            _life_mark_mutator(index, marker_field),
            context=f"life_boundary_{boundary}",
            in_session_extra=_extra,
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] life %s boundary marker tx failed (persona=%s "
            "date=%s)", boundary, persona_id, plan_date_str, exc_info=True,
        )
        try:
            ledger.mark_failed(
                execution_id, f"life-{boundary} marker tx failed"
            )
        except Exception:
            LOGGER.error(
                "[day_plan] failed to record life-%s marker tx failure "
                "(execution=%s)", boundary, execution_id, exc_info=True,
            )
        return False
    if marked is None:
        # 並走が先にマークした / lives が消えた — 通知なしでこの実行を閉じる
        # (境界そのものは決着済み)。
        try:
            ledger.mark_applied(
                execution_id,
                result={"boundary": boundary, "note": "marker already set"},
            )
            ledger.mark_completed(execution_id)
        except Exception:
            LOGGER.warning(
                "[day_plan] failed to close no-op life-%s boundary execution "
                "(execution=%s)", boundary, execution_id, exc_info=True,
            )
        return True

    if stale is not None:
        # 通知を同梱していないので、配送を待たずに閉じる。閉じ損ねても applied
        # (決着済み) なので再試行は止まる — 回復 tick の sweep が拾う。
        try:
            ledger.mark_completed(execution_id)
        except Exception:
            LOGGER.warning(
                "[day_plan] failed to close stale life-%s boundary execution "
                "(execution=%s)", boundary, execution_id, exc_info=True,
            )
        return True

    LOGGER.info(
        "[day_plan] life %s boundary applied: marker + %d notice outbox in "
        "one commit (execution=%s persona=%s date=%s)",
        boundary, len(outbox_items), execution_id, persona_id, plan_date_str,
    )
    try:
        ledger.flush_pending_for_persona(persona_id)
    except Exception:
        LOGGER.warning(
            "[day_plan] life boundary notice delivery deferred "
            "(persona=%s) — outbox remains pending for the gate / recovery "
            "tick", persona_id, exc_info=True,
        )
    return True


# ---------------------------------------------------------------------------
# 起床・就寝の節目 — 機械の帳簿処理 (autonomous_behavior_v3.md §6、
# autonomous_behavior_v04_plan.md 段 1-2)
#
# 起床時刻: ライフの確定 + 開始の節目 (TTL override・「（活動開始）」通知)。
# 就寝時刻: 終了の節目 (keep-alive 予約の cancel・TTL 遅延解除・「（活動終了）」
# 通知)。どちらも LLM を呼ばない。以前は判断点 (fire_judgment_point の
# day_open / day_close) の前段で行っていたが、判断点の LLM 部分の退役に先立って
# 切り出した — Playbook が取り込まれていない世界でもライフは確定する
# (旧経路は playbook_available の検査で確定ごと黙って飛んでいた)。
# ---------------------------------------------------------------------------

#: 節目の種類 (:func:`handle_scheduled_life_boundary` の ``boundary``)。
LIFE_BOUNDARY_START = "start"
LIFE_BOUNDARY_END = "end"


def life_settings_from_params(params: Any) -> Dict[str, Any]:
    """起床スケジュール行の PLAYBOOK_PARAMS から、ライフ確定に効く設定を拾う。

    - ``daily_budget_pulses`` (正の int): ライフの標準パルス予算 (未設定 /
      最低値未満は :func:`confirm_life_for_today` が最低値へ切り上げる)
    - ``life_mode_override`` ("even"/"free"): モードの明示上書き (それ以外の
      値は無視して自動判定へ — life.md §5.1)
    """
    out: Dict[str, Any] = {}
    if not isinstance(params, dict):
        return out
    budget_pulses = params.get("daily_budget_pulses")
    if isinstance(budget_pulses, int) and not isinstance(budget_pulses, bool) \
            and budget_pulses >= 1:
        out["daily_budget_pulses"] = budget_pulses
    mode_override = params.get("life_mode_override")
    if isinstance(mode_override, str) and mode_override in LIFE_MODES:
        out["life_mode_override"] = mode_override
    return out


def handle_scheduled_life_boundary(
    manager: Any,
    persona_id: str,
    boundary: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    occurrence_at: Optional[datetime] = None,
    plan_date: Optional[str] = None,
) -> bool:
    """起床 / 就寝の時刻に、ライフの確定と節目処理を機械の帳簿処理として行う。

    呼び出し元は ScheduleManager (起床・就寝スケジュール行の発火) と watchdog
    (当日のライフが無いときの起床の再発火)。

    - ``"start"``: 起床・就寝時刻 (PersonaSchedule) と ``params`` のユーザー設定
      から今日 (暦日) のライフを確定し (:func:`confirm_life_for_today`、冪等)、
      開始の節目 (:func:`apply_life_boundary`) を決着させる
    - ``"end"``: その営業日 (深夜跨ぎリズムでは前日) の確定済みライフに終了の
      節目を決着させる

    節目の一度きり保証は二重 — lives[0] の永続マーカー (``started`` /
    ``ended``) と、実行台帳 ``life.boundary_start`` / ``life.boundary_end`` の
    冪等キー ``{persona}:{plan_date}``。後者は旧経路 (判断点の前段) と同じ kind・
    キーなので、旧経路で既に節目を済ませた日に新経路が二重に通知しない。

    ``occurrence_at`` = この節目の occurrence が属する時刻 (ScheduleManager は
    予約の発火予定時刻 = occurrence トークンを、watchdog は判定した時刻を渡す)。
    営業日はこの時刻から決める — 実行時の暦日を取り直すと、23:59 の起床が
    失敗して 00:01 に backoff 再試行されたとき、翌日のライフを確定・開始して
    しまい、翌日の正規の起床が「開始済み」として省略される (2026-10-10 Codex
    敵対レビュー 2 巡目 修正 2)。None (旧予約・直接呼び出しの互換) は現在時刻。

    ``plan_date`` = この節目が属する営業日を**呼び出し側が凍結した値**
    ("YYYY-MM-DD")。渡されればそれを使い、``occurrence_at`` からの計算はしない。
    ScheduleManager は occurrence ごとに一度だけ :func:`life_boundary_plan_date`
    で決め、発火の実行台帳の payload に凍結して再試行・再起動後の回収まで
    持ち回す — 就寝の営業日は**現在の**起床設定に依存するので、再試行のたびに
    引き直すと、間で起床設定が変わった就寝が別の日を選び、元の日のライフが
    終了の節目を永遠に失う (2026-10-10 Codex 敵対レビュー 3 巡目 修正 2)。
    台帳のある ScheduleManager は凍結値が得られない回をここへ渡さず再試行に
    回す (旧予約の行にも実行前に凍結値を埋める — 2026-10-10 Codex 敵対レビュー
    4 巡目 修正 1)。None (watchdog・直接呼び出し・台帳の無い構成) は従来どおり
    ``occurrence_at`` から計算する。

    自律 OFF のペルソナでは何もしない (確定も節目も行わない — 自律 OFF の世界に
    ライフは無い。アラームはライフと無関係に鳴る)。

    Returns:
        決着したか。True = 済んだ (今回適用 / 既に決着済み / ライフ無しの設定 /
        自律 OFF で対象外)。False = 失敗 — 呼び出し元 (ScheduleManager) は
        backoff 再試行に乗せる。
    """
    if boundary not in (LIFE_BOUNDARY_START, LIFE_BOUNDARY_END):
        raise ValueError(f"unknown life boundary: {boundary!r}")
    from saiverse.autonomy_wiring import is_autonomy_on

    if not is_autonomy_on(manager, persona_id):
        LOGGER.debug(
            "[day_plan] life %s boundary skipped (persona=%s autonomy disabled)",
            boundary, persona_id,
        )
        return True
    if boundary == LIFE_BOUNDARY_START:
        return _settle_life_start(
            manager, persona_id, params, occurrence_at=occurrence_at,
            plan_date=plan_date,
        )
    return _settle_life_end(
        manager, persona_id, occurrence_at=occurrence_at, plan_date=plan_date,
    )


def life_boundary_plan_date(
    manager: Any,
    persona_id: str,
    boundary: str,
    occurrence_at: Optional[datetime] = None,
) -> str:
    """節目が属する営業日 ("YYYY-MM-DD") を ``occurrence_at`` から決める。

    - 起床 (start): occurrence の暦日 (設定に依存しない)
    - 就寝 (end): occurrence の時刻を**現在の**起床・就寝設定に当てた覚醒日
      (深夜跨ぎリズムでは 01:00 の就寝は前日)。設定を読めなければ送出する

    答えが現在の設定に依存するので、ScheduleManager は occurrence ごとに一度
    だけ呼び、発火の実行台帳に凍結する (:func:`handle_scheduled_life_boundary`
    の ``plan_date``)。
    """
    from saiverse.autonomy_wiring import _day_close_plan_date, _day_open_plan_date

    if boundary == LIFE_BOUNDARY_START:
        return _day_open_plan_date(occurrence_at)
    if boundary == LIFE_BOUNDARY_END:
        return _day_close_plan_date(manager, persona_id, occurrence_at)
    raise ValueError(f"unknown life boundary: {boundary!r}")


def _settle_life_start(
    manager: Any,
    persona_id: str,
    params: Optional[Dict[str, Any]],
    *,
    occurrence_at: Optional[datetime] = None,
    plan_date: Optional[str] = None,
) -> bool:
    """起床時刻の帳簿処理: 今日のライフを確定し、開始の節目を決着させる。

    確定は冪等 (:func:`confirm_life_for_today` が既存確定を保持する)。開始の
    節目の一度きり保証は lives[0] の ``started`` マーカー — 確定は済んだが節目が
    失敗した日は、再試行で節目だけをやり直せる (確認 → 適用 → マークの順)。
    営業日は凍結済みの ``plan_date``、無ければ ``occurrence_at`` (節目の
    occurrence の時刻) の暦日。
    """
    from saiverse.autonomy_wiring import _find_day_schedules

    plan_date = plan_date or life_boundary_plan_date(
        manager, persona_id, LIFE_BOUNDARY_START, occurrence_at,
    )
    settings = life_settings_from_params(params)
    try:
        # strict: 壊れた記録は「未開始」でも「ライフ無し」でもない — 下の
        # except で False → backoff 再試行 (2026-10-10 Codex 敵対レビュー 5 巡目)。
        existing = get_lives(manager, persona_id, plan_date, strict=True)
        already_started = bool(existing) and bool(existing[0].get("started"))
        # strict: 設定の**読み取り失敗**を「未設定」(ライフ無しの日として決着)
        # と混ぜない — 混ぜると DB の一時失敗でその日の起床が成功扱いのまま
        # 静かに失われる (2026-10-10 Codex 敵対レビュー 4 巡目 修正 3)。失敗は
        # 下の except で False → backoff 再試行。正常に読めて空のときだけ
        # confirm_life_for_today が None を返し「ライフ無し」で決着する。
        sched = _find_day_schedules(manager, persona_id, strict=True)
        life = confirm_life_for_today(
            manager, persona_id, plan_date,
            sched.get("wake"), sched.get("close"),
            requested_budget_pulses=settings.get("daily_budget_pulses"),
            mode_override=settings.get("life_mode_override"),
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to confirm today's life at wake "
            "(persona=%s date=%s)", persona_id, plan_date, exc_info=True,
        )
        return False
    if life is None:
        return True  # 起床・就寝未設定 = ライフ無しの日 (決着)
    if already_started:
        LOGGER.info(
            "[day_plan] life start already applied for this business day; "
            "skipping boundary side effects (persona=%s date=%s)",
            persona_id, plan_date,
        )
        return True
    try:
        return apply_life_boundary(
            manager, persona_id, plan_date, life, boundary=LIFE_BOUNDARY_START,
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] life-start processing failed (persona=%s date=%s)",
            persona_id, plan_date, exc_info=True,
        )
        return False


def _settle_life_end(
    manager: Any,
    persona_id: str,
    *,
    occurrence_at: Optional[datetime] = None,
    plan_date: Optional[str] = None,
) -> bool:
    """就寝時刻の帳簿処理: その営業日のライフに終了の節目を決着させる。

    営業日 (覚醒日) は凍結済みの ``plan_date``。無ければ (旧予約・直接呼び出し)
    :func:`life_boundary_plan_date` を ``occurrence_at`` (節目の occurrence の
    時刻) で引く — 深夜跨ぎリズムでは 01:00 の就寝は前日が営業日。ライフの無い
    日は何もしない (決着)。一度きり保証は lives[0] の ``ended`` マーカーと
    実行台帳の冪等キー。
    """
    try:
        plan_date = plan_date or life_boundary_plan_date(
            manager, persona_id, LIFE_BOUNDARY_END, occurrence_at,
        )
        # strict: 壊れた記録を「ライフの無い日」(成功で決着) と読まない —
        # 失敗として backoff 再試行へ (2026-10-10 Codex 敵対レビュー 5 巡目)。
        # 行が無い・正常に空の日は従来どおり下の ``not lives`` で決着する。
        lives = get_lives(manager, persona_id, plan_date, strict=True)
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to read lives at close (persona=%s)",
            persona_id, exc_info=True,
        )
        return False
    if not lives:
        return True
    if lives[0].get("ended"):
        LOGGER.info(
            "[day_plan] life end already applied for this business day; "
            "skipping boundary side effects (persona=%s date=%s)",
            persona_id, plan_date,
        )
        return True
    try:
        return apply_life_boundary(
            manager, persona_id, plan_date, lives[0], boundary=LIFE_BOUNDARY_END,
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] life-end processing failed (persona=%s date=%s)",
            persona_id, plan_date, exc_info=True,
        )
        return False


def record_judgment_pulse(
    manager: Any,
    persona_id: str,
    plan_date: Any = None,
    *,
    at_time: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """判断点の発火 1 回をその時刻が属するライフへ「別枠」で記帳する
    (life.md v0.5 §5.3/§8.2)。

    予算 (budget_pulses / used_pulses) には一切触れない。判断点 (いまは
    イベント到着の on_event だけ) はペルソナがコントロールできない発火 (いつ
    イベントが届くかはペルソナ次第ではない) であり、同じ財布に入れると構造
    矛盾が生じる (v2 実機初日の教訓)。判断点の回数はライフビューに別枠で
    表示するための観測値であり、:func:`saiverse.autonomy_wiring.fire_judgment_point` が
    判断点発火の都度呼ぶ。

    lives が無い日 / 発火時刻がどのライフにも属さない場合は no-op
    (None、:func:`consume_life_pulse` と同じ判定)。

    Args:
        plan_date: 省略時は現在時刻が属する営業日を自己解決する
            (:func:`resolve_business_day`)。解決できない (ライフを読めない) ときは
            記帳せず no-op + WARN — 別のライフの帳簿へ積むより欠落の方が軽い。
        at_time: 発火時刻 "HH:MM"。省略時は ``clock.now()``。
    """
    plan_date_str = _ledger_plan_date(
        manager, persona_id, plan_date, what="judgment pulse",
    )
    if plan_date_str is None:
        return None
    hhmm = at_time or clock.now().strftime("%H:%M")
    return _increment_life_field(
        manager, persona_id, plan_date_str, hhmm, "judgment_pulses", 1,
        what="judgment pulse",
    )


#: 均等モード中に運転する explicit cache TTL (life.md §5.1)。均等モードの
#: 最大パルス間隔 (:data:`LIFE_EVEN_MAX_GAP_MINUTES` 既定 50 分) は TTL=1h を
#: 前提に設計されている — global 既定の "5m" のままだと keep-alive が
#: 3〜4 分おきに artificial touch を打ち続けることになり、意図した「実パルス
#: 自身がキャッシュを繋ぐ」設計にならない。
_EVEN_MODE_CACHE_TTL = "1h"


def _life_ttl_clear_key(persona_id: str) -> str:
    """均等モードの TTL override 遅延解除の EventScheduler 予約 key。"""
    return f"life_ttl_clear:{persona_id}"


def _resolve_ttl_clear_delay_seconds(manager: Any, persona_id: str) -> int:
    """TTL override 遅延解除の待ち秒数 (= anchor validity 秒) を解決する。

    ``SessionLifecycle.get_anchor_validity_seconds`` は anchor の生存を
    「**現在の** TTL 設定」で評価する — override (1h) がまだ生きている
    ライフ終端のこの瞬間に読めば、実キャッシュの寿命評価と同じ 3600 秒が
    返る。runtime / model が引けない異常系は 3600 秒 (Anthropic explicit 1h
    相当) にフォールバックする (長すぎる側に倒す — 早すぎる clear が
    「実キャッシュは生きているのに anchor は失効扱い」の欠陥の源)。
    """
    default = 3600
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    model_key = getattr(persona, "model", None) if persona is not None else None
    runtime = manager.sea_runtime
    lifecycle = getattr(runtime, "session_lifecycle", None)
    if not model_key or lifecycle is None:
        return default
    try:
        seconds = int(lifecycle.get_anchor_validity_seconds(model_key, persona_id))
        return seconds if seconds > 0 else default
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to resolve TTL clear delay (persona=%s); using %ds",
            persona_id, default, exc_info=True,
        )
        return default


def _clear_life_ttl_override(manager: Any, persona_id: str) -> None:
    """遅延解除の発火体: ライフが設定した TTL override を厳密一致チェック付きで外す。

    現在の override が「ライフが設定した値」({"enabled": True, "ttl": "1h"}) と
    厳密一致するときだけ clear する — 予約〜発火の間にユーザーが人設定タブで
    別の値へ明示的に変更していた場合はそれを尊重して触らない。
    """
    get_override = getattr(manager, "get_persona_cache_override", None)
    clear_override = getattr(manager, "clear_persona_cache_override", None)
    if get_override is None or clear_override is None:
        return
    try:
        current = get_override(persona_id)
        if current == {"enabled": True, "ttl": _EVEN_MODE_CACHE_TTL}:
            clear_override(persona_id)
            LOGGER.info(
                "[day_plan] life TTL override cleared (delayed, persona=%s)",
                persona_id,
            )
        else:
            LOGGER.debug(
                "[day_plan] life TTL clear fired but override does not match "
                "life-set value; leaving as-is (persona=%s current=%r)",
                persona_id, current,
            )
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to clear life TTL override (persona=%s)",
            persona_id, exc_info=True,
        )


def _sync_cache_ttl_for_life_start(manager: Any, persona_id: str, life: Dict[str, Any]) -> bool:
    """life.md §5.1: 均等モードのライフ中は persona の explicit cache TTL を
    1h override する。

    前のライフの遅延解除予約 (:func:`_sync_cache_ttl_for_life_end`) が残って
    いれば先に cancel する — TTL 経過前に次のライフが始まったケースで、
    ライフの最中に解除が発火して override が外れる事故を防ぐ。

    persona に既存の cache override (人設定タブでユーザーが明示設定したもの、
    または前のライフが設定してまだ解除されていない 1h) があればそのまま触ら
    ない — ライフの宣言は既定値の補完であって、明示指定を上書きしない (前の
    ライフの 1h が残っている場合は望む値が既に入っているので set 不要)。
    自由モードのライフでは何もしない (間隔制約が無いので TTL を強制する理由
    がない)。
    """
    if life.get("mode") != LIFE_MODE_EVEN:
        return True
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is not None:
        try:
            if scheduler.cancel(_life_ttl_clear_key(persona_id)):
                LOGGER.info(
                    "[day_plan] pending life TTL clear cancelled by next life "
                    "start (persona=%s)", persona_id,
                )
        except Exception:
            # cancel 失敗を成功扱いにしてはならない (Codex W3 第九陣): 残った旧
            # 解除予約がライフ中に発火して override を外すのに、直後の「既存
            # override あり」分岐が True を返すと started マーカーで封印され、
            # 再試行が二度と TTL 同期をやり直せない — 部分失敗の成功封印そのもの。
            LOGGER.warning(
                "[day_plan] failed to cancel pending life TTL clear (persona=%s)",
                persona_id, exc_info=True,
            )
            return False
    get_override = getattr(manager, "get_persona_cache_override", None)
    set_override = getattr(manager, "set_persona_cache_override", None)
    if get_override is None or set_override is None:
        return True
    try:
        if get_override(persona_id) is not None:
            LOGGER.debug(
                "[day_plan] life start (mode=even): existing cache override "
                "present; not forcing TTL=%s (persona=%s)",
                _EVEN_MODE_CACHE_TTL, persona_id,
            )
            return True
        set_override(persona_id, enabled=True, ttl=_EVEN_MODE_CACHE_TTL)
        LOGGER.info(
            "[day_plan] life start (mode=even): cache TTL set to %s (persona=%s)",
            _EVEN_MODE_CACHE_TTL, persona_id,
        )
        return True
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to apply even-mode cache TTL (persona=%s)",
            persona_id, exc_info=True,
        )
        return False


def _sync_cache_ttl_for_life_end(manager: Any, persona_id: str, life: Dict[str, Any]) -> bool:
    """ライフ終端で TTL override の**遅延**解除を予約する (life.md §6.2 v0.4)。

    即時に clear してはいけない: anchor の生存判定
    (``SessionLifecycle.get_anchor_validity_seconds``) は「**現在の** TTL 設定」
    で評価されるため、終端で即時に global 既定 (5m) へ戻すと、実キャッシュは
    1h 生きているのに anchor は 5m で失効扱いになり、惜しい谷 (終了直後〜TTL
    内) の再訪が Case 3 に落ちて生きたキャッシュを捨てる。終端 + anchor
    validity 秒 (= 実キャッシュが確実に切れた後) に発火する予約を入れ、発火体
    (:func:`_clear_life_ttl_override`) が厳密一致チェック付きで clear する。

    次のライフが TTL 経過前に始まる場合は :func:`_sync_cache_ttl_for_life_start`
    が同 key の予約を cancel する。

    Returns:
        予約に成功したか (Codex W3 第四陣 P2)。予約が不要なケース (均等モード
        以外 / scheduler の無い構成) は True。
    """
    if life.get("mode") != LIFE_MODE_EVEN:
        return True
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return True
    try:
        delay = _resolve_ttl_clear_delay_seconds(manager, persona_id)
        fire_at = clock.now() + timedelta(seconds=delay)
        scheduler.schedule(
            fire_at=fire_at,
            callback=lambda: _clear_life_ttl_override(manager, persona_id),
            key=_life_ttl_clear_key(persona_id),
        )
        LOGGER.info(
            "[day_plan] life end (mode=even): TTL override clear scheduled in "
            "%ds (persona=%s)", delay, persona_id,
        )
        return True
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to schedule life TTL clear (persona=%s)",
            persona_id, exc_info=True,
        )
        return False


def _cancel_keepalive_reservation(manager: Any, persona_id: str) -> bool:
    """ライフ終端で keep-alive 予約を全 model 分 cancel する (谷では温めない)。

    ``sea.session_lifecycle.SessionLifecycle.schedule_cache_ttl_pulse`` の予約
    key は (persona, model) 単位の ``ttl:{persona_id}:{model_key}``
    (beat_execution_context.md §3.1)。終端側はどの model の Session に予約が
    入っているか列挙できないため prefix で一括 cancel する。
    ライフ中に未発火の予約が残っていても、この cancel で確実に止まる —
    :func:`is_keepalive_allowed` による発火時ゲートは二重の安全網。

    Returns:
        cancel に成功したか (Codex W3 第四陣 P2)。scheduler の無い構成は True。
    """
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return True
    try:
        cancelled = scheduler.cancel_prefix(f"ttl:{persona_id}:")
        if cancelled:
            LOGGER.info(
                "[day_plan] keep-alive reservations cancelled at life end (persona=%s, keys=%s)",
                persona_id, ", ".join(sorted(cancelled)),
            )
        return True
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to cancel keep-alive reservation at life end (persona=%s)",
            persona_id, exc_info=True,
        )
        return False



# ---------------------------------------------------------------------------
# 営業日の解決 (keep-alive・スルース・watchdog の共通の解決器)
# ---------------------------------------------------------------------------


def _resolve_wake(manager: Any, persona_id: str) -> Optional[str]:
    """ペルソナの起床時刻 "HH:MM" を PersonaSchedule から解決する (無ければ None)。

    :func:`_business_day_from_lives` が、確定ライフの開始が使えないときの
    退き先として使う。
    """
    try:
        from saiverse.autonomy_wiring import _find_day_schedules
        return _find_day_schedules(manager, persona_id).get("wake")
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to resolve wake time (persona=%s)", persona_id,
            exc_info=True,
        )
        return None


#: ライフを**読めなかった**ことの印。「その日にライフが宣言されていない」
#: (空リスト) と厳密に区別する — 混同すると、DB ロック等で読み出しが一時的に
#: 失敗しただけの日に現行 PersonaSchedule 基準へ黙って落ち、起床設定を変えた
#: 日は営業日の解決が丸一日ずれる (Codex 八巡目 #2)。
_LIVES_UNREADABLE = object()


def _load_lives_or_unreadable(
    manager: Any, persona_id: str, plan_date_str: str
) -> Any:
    """基準解決のためのライフ読み出し。読めなければ :data:`_LIVES_UNREADABLE`。

    読取の失敗は例外 (DB ロック等) だけではない — 壊れた LIVES_JSON / 互換読みの
    旧 meta_json は :func:`get_lives` の既定経路では空へ縮退し、「ライフ未宣言の日」と
    区別がつかなくなる。ここは ``strict=True`` で読み、**壊れている**も
    **読めなかった**側に数える (縮退したまま現行スケジュール基準で別の営業日を
    駆動する方が害が大きい。Codex 九巡目 #2)。

    Returns:
        ライフ配列 (宣言なしは空リスト) / :data:`_LIVES_UNREADABLE` (読取失敗)。
    """
    try:
        return get_lives(manager, persona_id, plan_date_str, strict=True)
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to read lives (persona=%s date=%s); callers must "
            "not fall back to the current schedule — the basis would split",
            persona_id, plan_date_str, exc_info=True,
        )
        return _LIVES_UNREADABLE


def _wake_from_lives(lives: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """確定ライフ基準の起点 (最初のライフの開始時刻)。使えなければ None。

    確定ライフのある日は、現行 PersonaSchedule の起床よりこちらが基準
    (日中に起床設定を変えた日に物差しを割らないため。Codex 六〜七巡目)。
    """
    if not lives:
        return None
    start = str(lives[0].get("start") or "")
    return start if is_valid_hhmm(start) else None


class BusinessDay(NamedTuple):
    """:func:`resolve_business_day` の答え — 営業日と、その日の暦日補正の起点。"""

    #: 営業日 "YYYY-MM-DD"
    plan_date: str
    #: 起点となる起床時刻 "HH:MM" (解決できなければ None)
    wake: Optional[str]
    #: 由来: ``"life"`` = 現在時刻を含む確定ライフ (いま起きている) /
    #: ``"life_ended"`` = その日のライフは始まっていたが今は区間の外 (谷・就寝後) /
    #: ``"schedule"`` = ライフの記録が無く現行 PersonaSchedule から決めた日。
    #: **ゲートを外してよいのは ``"life"`` だけ** (watchdog の窓・曜日判定)。
    source: str
    #: その営業日の確定ライフ (宣言なしは空リスト)。解決器が既に読んだものを
    #: そのまま持たせる — 消費側が引き直すと、営業日を決めた読みと表示・記帳の
    #: 読みが別世代になりうるうえ、10 秒ポーリングの表示経路で無駄な問い合わせが
    #: 増える。
    lives: List[Dict[str, Any]]


def _life_span_at(
    plan_date: date, life: Dict[str, Any]
) -> Optional[Tuple[datetime, datetime]]:
    """営業日 ``plan_date`` の暦日に錨を下ろしたライフの区間 ``[開始, 終了)``。

    開始は plan_date の start、終端は跨ぎ (end <= start) なら翌暦日。"HH:MM" だけ
    で判定する :func:`get_life_for_time` は暦日を持たないため、「その時刻はどの
    営業日のライフに属するか」を問う経路では使えない (07-04 の 23:00〜06:00 と
    07-05 の 23:00〜06:00 を区別できない)。

    **区間として成立しないライフは None** (start == end / 不正な "HH:MM")。
    書き手 (:func:`_validate_and_normalize_lives`) は start == end を「長さ 0 の
    ライフ」として拒否しており、読み手がそれを ``_life_span_minutes`` の跨ぎ規約
    (end <= start は +24h) で 24 時間ライフと読み替えるのは書き手の契約に反する。
    壊れた行 (手編集・旧データ) がその日の営業日を名乗り、watchdog の窓・曜日
    ゲートまで外してしまう (Codex 十巡目 #2)。
    """
    start, end = life.get("start"), life.get("end")
    if not is_valid_hhmm(start) or not is_valid_hhmm(end) or start == end:
        return None
    start_dt = datetime.combine(plan_date, dt_time(int(start[:2]), int(start[3:])))
    return start_dt, start_dt + timedelta(minutes=_life_span_minutes(life))


def _life_start_covering(
    plan_date: date, lives: List[Dict[str, Any]], now_dt: datetime
) -> Optional[datetime]:
    """``now_dt`` を**区間に含む**ライフの開始 datetime (無ければ None)。

    複数が該当する場合は開始が最も新しいもの (直近に始まったライフ)。
    """
    latest: Optional[datetime] = None
    for life in lives:
        span = _life_span_at(plan_date, life)
        if span is None:
            continue
        start_dt, end_dt = span
        if start_dt <= now_dt < end_dt and (latest is None or start_dt > latest):
            latest = start_dt
    return latest


def _latest_life_start(
    plan_date: date, lives: List[Dict[str, Any]], now_dt: datetime
) -> Optional[datetime]:
    """**もう始まっている**ライフのうち、開始が最も新しいもの (無ければ None)。

    区間に含まれていなくてよい — 「今日はもう終わったライフ」も数える。まだ
    始まっていないライフ (今日の起床前に確定だけ済んでいる等) は数えない。
    """
    latest: Optional[datetime] = None
    for life in lives:
        span = _life_span_at(plan_date, life)
        if span is None:
            continue
        start_dt = span[0]
        if start_dt <= now_dt and (latest is None or start_dt > latest):
            latest = start_dt
    return latest


def resolve_business_day(
    manager: Any, persona_id: str, *, now: Optional[datetime] = None
) -> Optional[BusinessDay]:
    """いま駆動中の営業日と、その日の暦日補正の起点を**一度に**決める。

    営業日と起点 (wake) を別々に解決すると基準が割れる: 現行 PersonaSchedule で
    営業日を選んでから確定ライフで wake を引くと、**日中に起床設定を変えた日**は
    営業日そのものを取り違える。例 — 営業日 D の確定ライフが 23:00〜06:00、
    設定変更後の現行スケジュールが 07:00〜22:00 のとき、D+1 の 00:30 に再起動
    すると (現行スケジュールは跨ぎでないので) 営業日を D+1 と読み、D の pending
    コマは検査されないまま回復 0 件で静かに終わる (Codex 八巡目 #1)。

    そこで基準は**確定ライフ**。暦日とその前日のライフを読み、次の順で決める:

    1. ``"life"`` — 現在時刻を**区間に含む**ライフのある日 (いま起きている日)
    2. ライフが走っていないときは、**最後に始まったライフの日**と、現行
       PersonaSchedule の営業日 (:func:`~autonomy_wiring.effective_plan_date`) の
       **遅い方** — 一日は前へしか進まないから。選んだ日にライフの記録があれば
       ``"life_ended"``、無ければ ``"schedule"``

    「最後に始まったライフの日」が要るのは、起床設定を変えた日の谷で設定だけを
    見ると**過去の日**を指してしまうから (例: 確定ライフ D 07:00〜10:00、変更後の
    設定が跨ぎリズム、現在 D 12:00 → effective_plan_date は D-1)。そこにライフが
    無いので「ライフ未宣言の日」と読まれ、keep-alive が終わったライフを温め続ける
    (Codex 十二巡目 #1)。逆に「遅い方」を採らないと、朝の day_open が失敗した日
    (D+1 08:00、D のライフは終了済み、D+1 にライフ無し) に前日 D を指し、watchdog
    が「前の営業日がまだ続いている」と読んで day_open を撃ち直さなくなる — 一日が
    始まらないまま止まる。通常運用 (設定を触らない日) では両者は一致する。

    **「その日に plan がある」ことは候補の選択に使わない** — 複数日に plan が
    あるのは普通で (昨日と今日)、存在の有無は「いまどちらの日を生きているか」を
    区別しない。日ごとに記録された時間の基準は確定ライフだけであり、ライフの
    無い日には基準そのものが存在しない (現行スケジュールが唯一の手掛かり)。

    Returns:
        :class:`BusinessDay` / **None = 決められなかった** (ライフ、または
        退き先の起床・就寝設定の読取失敗)。None のとき呼び出し元は予約も
        再分類も進めず、次の watchdog へ委ねること (Codex 八巡目 #2)。設定が
        **正常に読めて空** (一日リズム未設定) のときは None ではなく暦日の
        ``"schedule"`` を返す (2026-10-10 Codex 敵対レビュー 4 巡目 修正 3)。
    """
    now_dt = now or clock.now()
    today = now_dt.date()
    lives_by_date: Dict[date, List[Dict[str, Any]]] = {}
    started: Optional[Tuple[datetime, date]] = None
    # 候補は暦日と前日の 2 日 — 前日が要るのは深夜跨ぎライフの尻尾
    # (effective_plan_date が返しうるのもこの 2 日)。暦日を先に見て、そこに
    # 走っているライフがあれば前日は読まない: 前日のライフの開始は必ず暦日の
    # それより前なので、暦日が勝つと決まっている (表示は 10 秒ポーリングで
    # 呼ばれるため、無駄な問い合わせを残さない。Codex 十二巡目 #2)。
    for candidate in (today, today - timedelta(days=1)):
        lives = _load_lives_or_unreadable(
            manager, persona_id, candidate.isoformat(),
        )
        if lives is _LIVES_UNREADABLE:
            return None
        lives_by_date[candidate] = lives
        if _life_start_covering(candidate, lives, now_dt) is not None:
            return _business_day_from_lives(
                manager, persona_id, candidate, lives, "life",
            )
        start_dt = _latest_life_start(candidate, lives, now_dt)
        if start_dt is not None and (started is None or start_dt > started[0]):
            started = (start_dt, candidate)

    from saiverse.autonomy_wiring import _find_day_schedules, effective_plan_date

    # strict: 設定の**読み取り失敗**を「未設定」(暦日へ倒す) と混ぜない —
    # 混ぜると深夜跨ぎリズムの深夜帯に翌日 (暦日) を返し、「業務日不明なら
    # 見送る」呼び出し側 (スルースの提示等) をすり抜ける (2026-10-10 Codex
    # 敵対レビュー 4 巡目 修正 3)。読めなければ「決められない」= None。
    try:
        sched = _find_day_schedules(manager, persona_id, strict=True)
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to read the day schedules while resolving the "
            "business day (persona=%s); returning None (cannot decide)",
            persona_id, exc_info=True,
        )
        return None
    plan_date = effective_plan_date(now_dt, sched.get("wake"), sched.get("close"))
    if started is not None and started[1] > plan_date:
        plan_date = started[1]
    # 起点はその日のライフを優先する — 現行スケジュールの起床を返すと、日中に
    # 起床設定を変えた日に確定ライフと物差しが割れる (七巡目の欠陥)。まだ
    # 始まっていないライフ (起床前に確定だけ済んだ日) の起点もこれで拾う。
    lives = lives_by_date.get(plan_date) or []
    wake = _wake_from_lives(lives)
    # 由来は「その日に**始まったライフ**があるか」で決める (区間として成立しない
    # 行や、まだ始まっていない行は数えない — 名乗りが実態とずれないように)。
    source = "life_ended" if started is not None and started[1] == plan_date \
        else "schedule"
    return BusinessDay(
        plan_date.isoformat(), wake or sched.get("wake"), source, lives,
    )


def _business_day_from_lives(
    manager: Any,
    persona_id: str,
    plan_date: date,
    lives: List[Dict[str, Any]],
    source: str,
) -> BusinessDay:
    """ライフ基準で決まった営業日の :class:`BusinessDay` を組む。

    起点 (wake) は最初のライフの開始 (:func:`_wake_from_lives`)。ライフの開始が
    使えないときだけ現行 PersonaSchedule へ退く。
    """
    wake = _wake_from_lives(lives)
    return BusinessDay(
        plan_date.isoformat(), wake or _resolve_wake(manager, persona_id),
        source, lives,
    )


# ---------------------------------------------------------------------------
# 「ユーザー会話中か」の唯一の判定
# ---------------------------------------------------------------------------


def get_user_conversation_state(manager: Any, persona_id: str) -> Optional[bool]:
    """ユーザー会話中か。**読めなかったときは None** (不明) を返す三値版。

    :func:`is_in_user_conversation` の実装本体。判定そのものはここ 1 つに保つ
    (下の docstring 参照)。「不明」を「会話していない」へ丸めるかは呼び出し側の
    判断なので、丸めない生の答えをここが返す:

    - 既定の呼び出し側 (判断点) は fail-open で構わない — 会話でない
      前提で自律を進めても、取り返しのつかない出来事は起きない
    - ユーザーへ声を出す側 (tell スペル) は fail-closed にする — 会話中に
      重ねて話しかける失敗は届いた後では取り消せない

    ⚠ 器がメモリ内の会話状態になった 2026-08-22 (束 6c、v3 §7) 以降、読み取りは
    失敗しようがないので実際には None を返さない。三値のまま残すのは、v0.4 で
    別プロセス / 別ノードから状態を引く形になったときに「不明」が復活しうるため
    (呼び出し側の fail 方向の設計を今のうちに壊さない)。
    """
    try:
        from saiverse.user_conversation import get_open_conversation

        return get_open_conversation(manager, persona_id) is not None
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to read the conversation state (persona=%s); "
            "conversation state unknown", persona_id, exc_info=True,
        )
        return None


def is_in_user_conversation(manager: Any, persona_id: str) -> bool:
    """ユーザー会話中か。開いている会話状態があれば True。

    読み取りに失敗したときは False (fail-open)。不明と「会話していない」を
    区別したい呼び出し側は :func:`get_user_conversation_state` を使う。

    **「いま会話中か」を判定したい全ての箇所はこの関数を使うこと** (2026-07-29 公開化)。
    running Track の種別を見る旧判定が `judgment_points.build_on_event_situation_text`
    に残っており、終了済みの会話を「ユーザーと会話中です」と LLM へ渡していた
    (案 Y の追従漏れ)。同型の再発を防ぐため、判定の実装はここ 1 つに保つ。

    正典の器は三代目 — Track の status (v1) → 開いている会話の出来事
    (life.md §7 案 Y、2026-07-13) → **メモリ内の会話状態** (v3 §7、2026-08-22)。
    「無応答タイムアウトが会話を終わらせる」という意味論は三代とも不変
    (``autonomy_wiring.handle_conversation_end``)。
    """
    return get_user_conversation_state(manager, persona_id) is True
