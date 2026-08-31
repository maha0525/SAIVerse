"""v0.3「形の層」への機械写し — 旧データを新しい器へ、解釈なしで移す。

autonomous_behavior_v3.md §9-8 の裁定の実装。芯は一行:

    **意味の解釈が要る移行はやらない。機械的に写せるものだけ写す。**

写すのは三つ (どれも本人のテキストを一切加工しない — 置き場だけ変える):

1. **LIFE_PURPOSE 列** (`ai.LIFE_PURPOSE`、JSON
   ``{"purpose": str, "interests": [str], "vocations": [str]}``)
   → ``purpose`` の一文はコア記憶へ / ``interests``・``vocations`` の各項目は
   手帳のアクティビティへ (§9-5)。
2. **Track の関心** (``action_track`` の完了・中止でない行) の題
   → 手帳のアクティビティへ (§9-8 ②)。
3. **desire 候補** (``persona_task`` の desire 系列の行)
   → 手帳のやりたいメモへ (§9-8 ②)。

すべて ``origin='migration'``。

**写し元は無傷で残す** (読み取り専用の残置)。列も行も落とさない — v3 §9-8 ① の
哲学「削除はいつでもできる」で、ペルソナ所有のデータを v0.3 で壊さない。

置き場: ペルソナごとの ``memory.db`` (コア記憶・手帳の両方がそこに住む)。
呼び出し元は ``SAIVerseManager._on_persona_registered`` — 起動時ロード・動的作成・
Blueprint spawn の全経路が通る統一フック (``note_theme_migration`` と同じ扇形移行)。

**冪等の設計は二段**:

- **一回きりのマーカー** (``memory.db`` の ``embed_metadata`` KV、キーは
  :data:`MARKER_KEY`)。スルースの旧パンマーカー移行 (``sea/sluice.py``) と同じ
  置き場・同じ「新キーが無いときだけ走る」判定。書き込み先と同じ DB に置くので、
  memory.db を差し替え・復元しても「写したか」と「写した中身」がずれない。
- **個々の書き込み自身の冪等**。アクティビティは名前で get-or-create
  (open な名前の部分ユニーク索引)、メモは ``idem_key``、コア記憶は
  ``metadata.idem_key`` (:data:`CORE_IDEM_KEY_PREFIX`) の照会。マーカーが
  何らかの理由で失われても、二周目が同じものを二重に作らない。

**なぜコア記憶にも自前の冪等キーが要るか** (2026-08-22 Codex 指摘 2):
``add_core_memory`` は内部で ``conn.commit()`` するので、**呼び出し側の
トランザクションに載せられない**。「全部を 1 つのトランザクションにして、
転んだら丸ごと巻き戻す」は成立せず、コア記憶だけが確定してマーカーが立たない
並びが必ず残る。そこでコア記憶は本文ではなく ``metadata.idem_key`` で照合する
get-or-create にした — 本人が中身を書き換えた後に再実行されても二重にならない
(本文照合ではここが破れる)。

書き込みの順序も「巻き戻せるものを先に、巻き戻せないものを最後に」に揃えてある:
アクティビティとメモ (``commit=False`` で 1 トランザクション) → ``commit`` →
コア記憶 (自分で commit する) → マーカー。

**締め切りつきタスク → タスク帳**は中央 DB 内で完結するので、ここではなく
``database/migrate.py`` の ``migrate_deadline_tasks_to_task_book`` が担う。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text

LOGGER = logging.getLogger(__name__)

#: 一回きりマーカーのキー (memory.db の embed_metadata KV)。値は完了時刻の ISO 文字列。
MARKER_KEY = "v3_shape_migration_done"

#: 手帳の ``origin`` 語彙のうち、本移行が使うもの (pocketbook.ACTIVITY_ORIGINS)。
ORIGIN_MIGRATION = "migration"

#: コア記憶の冪等キーの前置き (後ろに persona_id が付く)。
#: ``add_core_memory`` の ``metadata`` へ刻み、二周目は照会して書かない。
CORE_IDEM_KEY_PREFIX = "migration:life_purpose:"

#: 完了・中止した Track は「生きた関心」ではないので写さない
#: (saiverse/track_manager.py の STATUS_COMPLETED / STATUS_ABORTED)。
_DEAD_TRACK_STATUSES = ("completed", "aborted")


# ---------------------------------------------------------------------------
# LIFE_PURPOSE の解析 (退役した saiverse/life_purpose.py の意味論をここへ引き取る)
# ---------------------------------------------------------------------------


def parse_life_purpose(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """``ai.LIFE_PURPOSE`` の JSON を ``{purpose, interests, vocations}`` へ。

    退役した ``saiverse/life_purpose.py`` の ``parse_life_purpose`` と同じ意味論
    (壊れた JSON・非 dict・全欄が空はすべて「未設定」= None)。パーサをここへ
    持ってきたのは、読み手が本移行だけになったため — 移行が終われば列ごと
    休眠するので、汎用モジュールとして残す理由が無い。
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        LOGGER.warning("[v3-migration] LIFE_PURPOSE is not valid JSON: %r", raw)
        return None
    if not isinstance(data, dict):
        LOGGER.warning("[v3-migration] LIFE_PURPOSE is not a JSON object: %r", raw)
        return None

    purpose = str(data.get("purpose") or "").strip()
    interests = [
        str(x).strip() for x in (data.get("interests") or []) if str(x).strip()
    ]
    vocations = [
        str(x).strip() for x in (data.get("vocations") or []) if str(x).strip()
    ]
    if not purpose and not interests and not vocations:
        return None
    return {"purpose": purpose, "interests": interests, "vocations": vocations}


# ---------------------------------------------------------------------------
# 写し元の読み出し (中央 DB。raw SQL — 退役済み / 退役予定の列に触れるため)
# ---------------------------------------------------------------------------


class MigrationSourcesUnavailable(RuntimeError):
    """写し元を**読めなかった** (「写すものが無い」とは別の結末)。

    区別が要る理由 (2026-08-22 Codex 指摘 3): 読み取り失敗を「データなし」と
    同一視すると、DB ロックやスキーマ不整合のような**直せる一過性の失敗**でも
    完了マーカーが立ち、以後その判定で短絡して旧データが永久に写されなくなる。
    このリポジトリで三度塞いだ族 (束 3 第八巡・束 4) と同じ形なので、
    「正常に空」と「読めなかった」は型で分ける。
    """


def _table_exists(db: Any, name: str) -> bool:
    row = db.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": name},
    ).fetchone()
    return row is not None


def _existing_columns(db: Any, table: str) -> set:
    """``table`` に実在する列名の集合 (小文字化)。

    途中バージョンからの移行では、参照したい列がまだ / もう無いことがある。
    これは「正常にデータなし」であって読み取り失敗ではない — だが判別は
    **例外の丸呑みではなく明示の列検査**で行う。例外で判別すると、ロックや
    破損のような直すべき失敗まで「列が無いだけ」として飲み込んでしまう。
    """
    rows = db.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {str(r[1]).lower() for r in rows}


def _read_sources(manager: Any, persona_id: str) -> Optional[Dict[str, Any]]:
    """中央 DB から写し元を読む。**正常に**何も無ければ None。

    Raises:
        MigrationSourcesUnavailable: 読み取り自体に失敗した場合。呼び出し元は
            マーカーを立てずに降りる (次回起動が再試行する)。
    """
    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is None:
        # 中央 DB への口が無い = 「写すものが無い」ではなく「読めない」。
        # ここでマーカーを立てると、口が戻った後も二度と写されない。
        raise MigrationSourcesUnavailable(
            f"manager has no SessionLocal (persona={persona_id})"
        )

    try:
        db = session_factory()
    except Exception as exc:
        raise MigrationSourcesUnavailable(
            f"failed to open a session on the central DB (persona={persona_id})"
        ) from exc

    try:
        life_purpose: Optional[Dict[str, Any]] = None
        if _table_exists(db, "ai") and "life_purpose" in _existing_columns(db, "ai"):
            row = db.execute(
                text("SELECT LIFE_PURPOSE FROM ai WHERE AIID = :pid"),
                {"pid": persona_id},
            ).fetchone()
            if row is not None:
                life_purpose = parse_life_purpose(row[0])

        track_titles: List[str] = []
        if _table_exists(db, "action_track"):
            columns = _existing_columns(db, "action_track")
            if {"title", "persona_id", "status"} <= columns:
                rows = db.execute(
                    text(
                        # status IS NULL を明示的に拾う: SQL の NOT IN は
                        # NULL に対して NULL (= 偽) を返すため、条件を書かないと
                        # 「完了でも中止でもない生きた関心」が黙って落ちる。
                        # 列は NOT NULL だが、この移行が読むのは**既に配布済みの
                        # 世界の DB** で、古いスキーマの残骸が入りうる
                        # (2026-08-22 掃討フェーズ 束 6c 指摘 3a)。
                        "SELECT title FROM action_track "
                        "WHERE persona_id = :pid AND ("
                        "  status NOT IN ('completed', 'aborted') "
                        "  OR status IS NULL)"
                    ),
                    {"pid": persona_id},
                ).fetchall()
                track_titles = [
                    str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()
                ]

        desires: List[Dict[str, Any]] = []
        if _table_exists(db, "persona_task"):
            columns = _existing_columns(db, "persona_task")
            needed = {
                "id", "title", "goal", "persona_id", "status",
                "desire_type", "desire_state", "desire_source",
            }
            if needed <= columns:
                # desire 系列 = desire_* 欄のどれかが埋まっている行 (旧 desire
                # ノート由来の候補)。status が終端 (completed / cancelled) の
                # ものは「もうやりたくない / やり終えた」なので写さない。
                rows = db.execute(
                    text(
                        "SELECT id, title, goal FROM persona_task "
                        "WHERE persona_id = :pid "
                        "AND (desire_type IS NOT NULL OR desire_state IS NOT NULL "
                        "     OR desire_source IS NOT NULL) "
                        # NOT IN の NULL 落ち対策 (指摘 3a と同型)。
                        "AND (status NOT IN ('completed', 'cancelled') "
                        "     OR status IS NULL)"
                    ),
                    {"pid": persona_id},
                ).fetchall()
                for r in rows:
                    title = str(r[1] or "").strip()
                    if not title:
                        continue
                    goal = str(r[2] or "").strip()
                    desires.append({"id": str(r[0]), "title": title, "goal": goal})
    except Exception as exc:
        raise MigrationSourcesUnavailable(
            f"failed to read the migration sources (persona={persona_id}): {exc}"
        ) from exc
    finally:
        try:
            db.close()
        except Exception:
            LOGGER.debug(
                "[v3-migration] closing the source session raised (persona=%s)",
                persona_id, exc_info=True,
            )

    if not life_purpose and not track_titles and not desires:
        return None
    return {
        "life_purpose": life_purpose,
        "track_titles": track_titles,
        "desires": desires,
    }


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------


def migrate_persona_to_v3_shape(manager: Any, persona_id: str) -> Dict[str, int]:
    """persona_id の旧データを v0.3 の器へ機械写しする (一回きり・冪等)。

    完了マーカーが立つのは「写し切った」ときと「**正常に**写すものが無かった」
    ときだけ。写し元を読めなかった場合はマーカーを立てずに降りるので、次回の
    起動が同じ地点からやり直す (2026-08-22 Codex 指摘 3)。

    Returns:
        ``{"core_memories", "activities", "memos"}`` の書き込み件数。
        既に済んでいる / 写すものが無い / 器が使えない / 写し元を読めなかった
        場合は全部 0。
    """
    empty = {"core_memories": 0, "activities": 0, "memos": 0}

    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    conn = getattr(adapter, "conn", None) if adapter is not None else None
    if conn is None or not getattr(adapter, "_ready", True):
        LOGGER.debug(
            "[v3-migration] no memory.db for persona=%s; skipping", persona_id,
        )
        return empty

    from sai_memory.memory.storage import get_embed_metadata, set_embed_metadata

    try:
        with adapter._db_lock:
            already = get_embed_metadata(conn, MARKER_KEY)
    except Exception:
        LOGGER.warning(
            "[v3-migration] failed to read the marker (persona=%s); skipping "
            "rather than risking a duplicate copy", persona_id, exc_info=True,
        )
        return empty
    if already:
        return empty

    try:
        sources = _read_sources(manager, persona_id)
    except MigrationSourcesUnavailable:
        # 「読めなかった」は「写すものが無い」ではない。マーカーを立てずに降りる
        # ので、次回の起動が同じ地点からやり直す (2026-08-22 Codex 指摘 3)。
        LOGGER.warning(
            "[v3-migration] could not read the migration sources for persona=%s; "
            "leaving the marker unset so the next start retries (nothing was "
            "written)", persona_id, exc_info=True,
        )
        return empty

    if sources is None:
        # 写すものが無い世界 (新規ペルソナ) でもマーカーは立てる — 次回以降
        # 中央 DB を読み直す必要がない。**読み取り失敗はここへ来ない**
        # (例外で分岐済み) ので、この分岐は「正常に空」だけを意味する。
        try:
            with adapter._db_lock:
                set_embed_metadata(conn, MARKER_KEY, _marker_value())
        except Exception:
            LOGGER.warning(
                "[v3-migration] failed to set the marker for an empty migration "
                "(persona=%s)", persona_id, exc_info=True,
            )
        return empty

    from sai_memory.core_memory import add_core_memory, find_core_memory_by_idem_key
    from sai_memory.memory.pocketbook import add_memo, get_or_create_activity

    counts = dict(empty)
    life_purpose = sources["life_purpose"]
    today = _today(manager)
    core_idem_key = f"{CORE_IDEM_KEY_PREFIX}{persona_id}"

    try:
        with adapter._db_lock:
            # --- 巻き戻せる書き込み (commit=False で 1 トランザクション) ---
            try:
                # 1) interests / vocations / Track の関心 → アクティビティ。
                #    どれも「できる・好きな活動」の名前で、器は同じ (§13.1 の
                #    レパートリー = アクティビティ名の一覧の眺め)。
                activity_names: List[str] = []
                if life_purpose:
                    activity_names.extend(life_purpose["interests"])
                    activity_names.extend(life_purpose["vocations"])
                activity_names.extend(sources["track_titles"])
                for name in activity_names:
                    activity = get_or_create_activity(
                        conn, name, ORIGIN_MIGRATION, commit=False,
                    )
                    if activity is not None:
                        counts["activities"] += 1

                # 2) desire 候補 → やりたいメモ。
                #    メモは activity_id 必須 (§13.6 に「どこにも属さない」メモの
                #    席は無い) なので、一件ごとに同名のアクティビティを立てて
                #    そこへ吊る。粒度が細かすぎるものが混ざる実害は一覧の一行で、
                #    掃除は本人かユーザー (§9-8 ② の明示裁定)。
                #    受け皿を 1 本にまとめる案は採らない — 「どのアクティビティに
                #    属するか」を機械が決めるのは意味の解釈で、§9-8 の芯に反する。
                for desire in sources["desires"]:
                    activity = get_or_create_activity(
                        conn, desire["title"], ORIGIN_MIGRATION, commit=False,
                    )
                    if activity is None:
                        continue
                    counts["activities"] += 1
                    add_memo(
                        conn,
                        activity.id,
                        today,
                        "want",
                        desire["goal"] or desire["title"],
                        idem_key=f"migration:desire:{desire['id']}",
                        commit=False,
                    )
                    counts["memos"] += 1

                conn.commit()
            except BaseException:
                conn.rollback()
                raise

            # --- 巻き戻せない書き込み (add_core_memory は自分で commit する) ---
            # 3) purpose の一文 → コア記憶 (§9-5: 在り方であって活動ではないので
            #    手帳には入れない。常駐注入で全 Pulse から見える)。
            #    ここより前の書き込みは確定済みで、どれも自前の冪等を持つ。
            #    コア記憶自身も idem_key で get-or-create にしてあるので、
            #    この後 (マーカーを含む) で転んでも再実行で二重にならない。
            if life_purpose and life_purpose["purpose"]:
                if find_core_memory_by_idem_key(conn, core_idem_key) is None:
                    add_core_memory(
                        conn,
                        life_purpose["purpose"],
                        metadata=json.dumps(
                            {
                                "source": "migration",
                                "from": "LIFE_PURPOSE.purpose",
                                "idem_key": core_idem_key,
                            },
                            ensure_ascii=False,
                        ),
                        confirmed=1,
                    )
                    counts["core_memories"] += 1
                else:
                    LOGGER.info(
                        "[v3-migration] the LIFE_PURPOSE core memory already "
                        "exists for persona=%s (idem_key=%s); not writing it "
                        "again", persona_id, core_idem_key,
                    )

            # マーカーは移行の成功**後**に立てる。途中で転んだら立てないので、
            # 次回の起動が丸ごとやり直す — アクティビティは名前、メモは idem_key、
            # コア記憶は metadata.idem_key で、どれも二周目に二重にならない。
            set_embed_metadata(conn, MARKER_KEY, _marker_value())
    except Exception:
        LOGGER.warning(
            "[v3-migration] migration failed for persona=%s; the marker stays "
            "unset (the next start retries; every write is idempotent so the "
            "retry does not duplicate anything)", persona_id, exc_info=True,
        )
        return empty

    LOGGER.info(
        "[v3-migration] copied legacy data into the v0.3 shape for persona=%s: "
        "core_memories=%d activities=%d memos=%d (the sources are left "
        "untouched)",
        persona_id, counts["core_memories"], counts["activities"], counts["memos"],
    )
    return counts


def _marker_value() -> str:
    from saiverse import clock

    return clock.now().isoformat(timespec="seconds")


def _today(manager: Any) -> str:
    """メモの日付 (YYYY-MM-DD)。仮想クロックを尊重する。"""
    from saiverse import clock

    return clock.now().date().isoformat()
