from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from sea.cancellation import CancellationToken
from sea.eviction_plan import (
    Watermarks,
    compile_groups_from_folds,
    message_chars,
    plan_eviction,
    stored_message_chars,
)

if TYPE_CHECKING:
    from sea.runtime import SEARuntime
    from sea.session_window import FoldedRange, SessionWindow

LOGGER = logging.getLogger(__name__)

# head の weave section を覗く検査 (:meth:`SessionLifecycle._head_weave_snapshot`)
# 自体が失敗したことを表す番人。「weave が正当に無い」(None) と区別するために
# 要る — 両方 None に潰すと、検査が壊れている並びで stale な head が「組み直せ
# ました」の顔で通り、知らせるための機構が黙る (Codex 指摘 2026-09-01)。
_WEAVE_INSPECT_FAILED = object()


class SessionLifecycle:
    """Anchor / Metabolism / Chronicle — Session (短期記憶) の節目管理。

    docs/intent/session.md の「Session 統一制御単位」の実装先。
    session_lifecycle_extraction_design.md Step 1 で SEARuntime から抽出した。
    """

    #: §14-4 (arasuji_levels.md) の時計側見張りの間隔。失効からこの分だけ遅れて
    #: 検知しても実害は無い — 先回り畳みは「次の会話再開より前」に済めばよい。
    COLD_SWEEP_INTERVAL_SECONDS = 600
    _COLD_SWEEP_KEY = "metabolism:cold_window_sweep"

    def __init__(self, runtime: "SEARuntime", manager_ref: Any) -> None:
        self.runtime = runtime      # 過渡期の後方参照 (設計書 §4 で削減)
        self.manager = manager_ref
        # §14-4 先回り畳みの実行中 persona (persona ごとに同時 1 本)
        self._cold_sweep_lock = threading.Lock()
        self._cold_sweep_inflight: Set[str] = set()
        # generate_chronicle が "failed" を返した直近の理由 (error_code /
        # user_message / batch_meta)。戻り値の契約 (status 文字列) を変えずに、
        # 手動生成のジョブ UI が empty_response 等の案内と「該当メッセージを
        # 表示」を出せるようにする口。pop_last_chronicle_failure で取り出す。
        # SessionLifecycle は runtime に 1 つで全ペルソナが共有し、Beat の錠は
        # ペルソナごとなので、走行は重なる — persona_id をキーにして、A の
        # ジョブが B の理由を受け取ったり、B の走行開始が A の理由を消したり
        # しないようにする (Codex 指摘 2026-09-03)。
        self._chronicle_failures_lock = threading.Lock()
        self._chronicle_failures: Dict[str, Dict[str, Any]] = {}
        # 「合計は上限超えだが会話の行は残す量以下 = 畳めるものが無い」を
        # ペルソナごとプロセスごとに 1 度だけ警告するための既出集合
        # (:meth:`_note_perception_over_budget`)。毎ターン同じ警告を出さない。
        self._perception_over_budget_warned: Set[str] = set()
        # 最終防衛ライン (:meth:`ensure_window_floor`) が最後に発火した時刻
        # ((persona_id, model_key) → ISO 文字列)。発火は上流 (読み戻し) の
        # 失敗の印なので context-status に出す。プロセス内の記録で永続化しない。
        self._window_floor_applied_at: Dict[Tuple[str, str], str] = {}
        # 最終防衛ラインを「SAIMemory absent (従来のメモリ上の履歴)」で見送った
        # ことをペルソナごと 1 度だけ INFO に残すための既出集合。
        self._floor_absent_logged: Set[str] = set()

    # ------------------------------------------------------------------
    # 勘定の単位 — 「実際に送る中身」(2026-09-02 まはー裁定)
    # ------------------------------------------------------------------

    def perception_blocks_for(
        self, persona, presented: Sequence[Dict[str, Any]],
        anchor_id: Optional[str] = None,
        *,
        raise_on_error: bool = False,
    ) -> List[Dict[str, Any]]:
        """この窓と一緒に送られる知覚ブロック (組成は組み立て側と同じ一枚)。

        水位判定・整理・表示が数えるのは**実際に送る中身**であって、保存行だけ
        ではない。ブロックを組む規則は
        :func:`sea.runtime_context.list_presented_perception_blocks` にあり、
        測る側もそれを呼ぶ (規則の二枚目を作らない —
        docs/issues/context_accounting_excludes_injected_rows.md)。

        取得できない環境・失敗時は空リスト = 知覚ぶん 0 (従来値へ縮退)。WARN は
        組成側が出す。``raise_on_error=True`` は失敗を例外で伝える — 透明性の
        画面 (context-status) が内部失敗を正常なゼロとして表示しないための口
        (門・発火・送信は fail-open のまま。Codex 指摘 2026-09-02)。
        """
        try:
            from sea.runtime_context import list_presented_perception_blocks
            return list_presented_perception_blocks(
                self.runtime, persona, list(presented), anchor_id=anchor_id,
                raise_on_error=raise_on_error,
            )
        except Exception:
            if raise_on_error:
                raise
            LOGGER.warning(
                "[metabolism] perception block lookup failed; counting the "
                "window without perceptions (persona=%s)",
                getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return []

    def presented_with_perceptions(
        self, persona, presented: Sequence[Dict[str, Any]],
        anchor_id: Optional[str] = None,
        *,
        raise_on_error: bool = False,
    ) -> List[Dict[str, Any]]:
        """保存行 + 知覚ブロックを時刻順にマージした「送る中身」の列。

        水位の勘定 (:func:`~sea.eviction_plan.message_chars`) と退場計画
        (:func:`~sea.eviction_plan.plan_eviction`) の入力はどちらもこれ。
        """
        blocks = self.perception_blocks_for(
            persona, presented, anchor_id, raise_on_error=raise_on_error,
        )
        if not blocks:
            return list(presented)
        from sea.runtime_context import merge_perception_blocks
        return merge_perception_blocks(list(presented), blocks)

    def presented_chars(
        self, persona, presented: Sequence[Dict[str, Any]],
        anchor_id: Optional[str] = None,
        *,
        raise_on_error: bool = False,
    ) -> int:
        """この窓で実際に送られる文字数 (保存行 + 知覚ブロック)。

        適用範囲は**履歴窓**: 時刻アンカー (49 字)・添付注記などの微小な整形と、
        履歴の外のセクション (head / realtime) は含まない — 水位が束ねるのは
        履歴窓で、逸脱は issue (context_accounting_excludes_injected_rows.md)
        に既知として記録済み。
        """
        return message_chars(
            self.presented_with_perceptions(
                persona, presented, anchor_id, raise_on_error=raise_on_error,
            )
        )

    def _note_perception_over_budget(
        self, persona, rows_chars: int, total_chars: int, watermarks: Watermarks,
    ) -> bool:
        """「合計は上限超え・会話の行は残す量以下」なら 1 度だけ警告して True。

        水位の主語は二つある (:class:`~sea.eviction_plan.Watermarks`): 上限は
        実際に送る合計、残す量は会話の行の量。合計が上限を超えているのに行が
        残す量以下なら、保護範囲が行を全部覆っていて退場できるものが無い —
        超過の主は会話ではなく知覚の供給で、Metabolism を走らせても空振り
        (LLM を呼ばない) にしかならない。呼び出し側はこの判定で本体へ進まず
        引き返す。警告はペルソナごとプロセスごとに 1 度 (毎ターン同じ行で
        ログを埋めない)。同じ事実は context-status の ``perception_over_budget``
        にも出る (docs/issues/protection_quota_consumed_by_perception_blocks.md)。
        """
        if watermarks.high is None:
            return False
        if not (total_chars > watermarks.high and rows_chars <= watermarks.target):
            return False
        persona_id = str(getattr(persona, "persona_id", "?"))
        if persona_id not in self._perception_over_budget_warned:
            self._perception_over_budget_warned.add(persona_id)
            LOGGER.warning(
                "[metabolism] perception blocks (%d chars) exceed the room left "
                "by the watermarks (high %d − target %d); nothing evictable — "
                "the perception supply, not the conversation, is over budget "
                "(persona=%s, rows=%d chars, total=%d chars)",
                total_chars - rows_chars, watermarks.high, watermarks.target,
                persona_id, rows_chars, total_chars,
            )
        return True

    def get_metabolism_watermarks(
        self, persona, model_key: Optional[str] = None,
    ) -> Optional[Watermarks]:
        """Metabolism の三水位 (文字数) を解決する。

        docs/intent/chronicle_eviction.md §4。水位はモデル依存
        (beat_execution_context.md §3.2 — 各 Session は自分の model の閾値で
        自分の提示コンテキストを管理する)。``model_key`` は実行 model。None なら従来どおり
        ``persona.model`` にフォールバックする。

        水位の出所は model 定義一本 (2026-07-30、グローバル上書きは廃止 —
        docs/issues/chat_options_metabolism_section_redesign.md)。model が
        解決できない場合と、model 定義が水位を null にしている場合は None を
        返す (= Metabolism を持たない。これが唯一のオプトアウト)。
        """
        persona_model = model_key or getattr(persona, "model", None)
        if not persona_model:
            return None
        from saiverse.model_configs import (
            get_metabolism_high_chars,
            get_metabolism_low_chars,
            get_metabolism_target_chars,
        )
        model_name = str(persona_model)

        low = get_metabolism_low_chars(model_name)
        target = get_metabolism_target_chars(model_name)
        high = get_metabolism_high_chars(model_name)

        if low is None or target is None:
            # 低・目標が無い設定は退場の量を決められない = Metabolism を持たない。
            return None
        return Watermarks(low=int(low), target=int(target), high=high)

    def _get_ledger(self):
        """実行台帳 (manager.execution_ledger)。無い環境 (旧テスト等) は None。"""
        return getattr(self.manager, "execution_ledger", None) if self.manager else None

    # ------------------------------------------------------------------
    # Session anchor 行 API (beat_execution_context.md §3.1、SEA 監査 S8)
    #
    # 旧形式 (AI.METABOLISM_ANCHORS 単一 JSON の全体 read-modify-write) は撤去
    # 済み。永続化先は session_anchor テーブル (1 行 = 1 (persona, model))。
    # entry の dict 形は旧 JSON の値と同じ:
    #   {"anchor_id": str, "updated_at": iso文字列, "ttl_seconds": int (省略可)}
    # (updated_at は DB では epoch 秒 int。API 境界で iso 文字列に往復変換する —
    #  読み手 (api/routes/people/cache_status.py / sea/head_pipeline/integration.py)
    #  の互換のため。秒未満の精度は落ちる。)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "anchor_id": row.ANCHOR_MESSAGE_ID,
            "updated_at": datetime.fromtimestamp(int(row.UPDATED_AT)).isoformat(),
        }
        if row.TTL_SECONDS:
            entry["ttl_seconds"] = int(row.TTL_SECONDS)
        folded = getattr(row, "FOLDED_RANGES_JSON", None)
        if folded:
            entry["folded_ranges"] = folded
        return entry

    def load_folded_ranges(
        self, persona_id: Optional[str], model_key: Optional[str],
        *, strict: bool = False,
    ) -> List["FoldedRange"]:
        """(persona, model) の提示コンテキストに空いている圧縮区間を読む (chronicle_eviction.md §6)。

        ``strict=True`` は行の読み失敗・壊れた記録を例外で伝える (既定は空へ
        縮退)。最終防衛ラインが使う — 読めなかった記録を空と見なして書き戻すと
        既存の区間が消える (Codex 四巡目 #3)。
        """
        from sea.session_window import deserialize_folds
        entry = (
            self.load_anchor_entry_strict(persona_id, model_key) if strict
            else self.load_anchor_entry(persona_id, model_key)
        )
        return deserialize_folds(
            entry.get("folded_ranges") if entry else None, strict=strict,
        )

    def save_folded_ranges(
        self,
        persona_id: Optional[str],
        model_key: Optional[str],
        folds: List["FoldedRange"],
    ) -> None:
        """圧縮区間を session_anchor 行へ書く。anchor / TTL は触らない。"""
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return
        if not persona_id or not model_key:
            return
        from sea.session_window import serialize_folds
        payload = serialize_folds(folds)
        db = self.manager.SessionLocal()
        try:
            from database.models import SessionAnchor
            row = db.query(SessionAnchor).filter_by(
                PERSONA_ID=persona_id, MODEL_KEY=str(model_key),
            ).first()
            if row is None:
                # anchor 行が無い = 提示コンテキストの起点が無い。圧縮区間だけ先に持っても意味がない。
                # ただし捨てる圧縮区間があるなら黙って落とさない (提示から体験が消える)。
                if payload:
                    LOGGER.warning(
                        "[metabolism] no anchor row for %s/%s; %d folded ranges were "
                        "not persisted (the raw log stays presented)",
                        persona_id, model_key, len(folds),
                    )
                return
            row.FOLDED_RANGES_JSON = payload
            db.commit()
        except Exception as exc:
            LOGGER.warning(
                "[metabolism] Failed to save folded ranges for %s/%s: %s",
                persona_id, model_key, exc,
            )
        finally:
            db.close()

    def load_anchor_entry(self, persona_id: Optional[str], model_key: Optional[str]) -> Optional[Dict[str, Any]]:
        """(persona, model) 1 行の anchor entry を読む。無ければ None。"""
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return None
        if not persona_id or not model_key:
            return None
        db = self.manager.SessionLocal()
        try:
            from database.models import SessionAnchor
            row = db.query(SessionAnchor).filter_by(
                PERSONA_ID=persona_id, MODEL_KEY=str(model_key),
            ).first()
            if row is not None:
                return self._row_to_entry(row)
        except Exception as exc:
            LOGGER.warning(
                "[metabolism] Failed to load anchor entry for %s/%s: %s",
                persona_id, model_key, exc,
            )
        finally:
            db.close()
        return None

    def load_anchor_entry_strict(
        self, persona_id: Optional[str], model_key: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """:meth:`load_anchor_entry` の、読み取り失敗を握らない版 (最終防衛ライン用)。

        通常版は DB 失敗を None (= 行なし) へ潰す。床はその行の圧縮区間の記録を
        読んで書き戻すので、「読めなかった」を「無い」と見なすと既存の区間を
        消してしまう — こちらは失敗を例外のまま返す。manager 未接続の部分構築
        環境は行という器ごと無いので None (従来どおり)。
        """
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return None
        if not persona_id or not model_key:
            return None
        db = self.manager.SessionLocal()
        try:
            from database.models import SessionAnchor
            row = db.query(SessionAnchor).filter_by(
                PERSONA_ID=persona_id, MODEL_KEY=str(model_key),
            ).first()
            return self._row_to_entry(row) if row is not None else None
        finally:
            db.close()

    def load_anchor_entries(self, persona_id: Optional[str]) -> Dict[str, Any]:
        """persona の全 model 分の anchor entry を {model_key: entry} で読む (read-only)。

        Case 2 fallback (resolve_metabolism_anchor の他 model 探索) と外部の
        read-only 消費者のための一括読み。書き込みは常に行単位
        (:meth:`upsert_anchor_entry`) で行い、この dict を書き戻す API は無い。
        """
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return {}
        if not persona_id:
            return {}
        db = self.manager.SessionLocal()
        try:
            from database.models import SessionAnchor
            rows = db.query(SessionAnchor).filter_by(PERSONA_ID=persona_id).all()
            return {row.MODEL_KEY: self._row_to_entry(row) for row in rows}
        except Exception as exc:
            LOGGER.warning("[metabolism] Failed to load anchors for %s: %s", persona_id, exc)
        finally:
            db.close()
        return {}

    def load_anchor_entries_strict(self, persona_id: Optional[str]) -> Dict[str, Any]:
        """:meth:`load_anchor_entries` の、読み取り失敗を握らない版 (§16 止め線用)。

        通常版は DB 失敗を空 dict へ潰す (多数の read-only 消費者が縮退で
        足りるため) が、被覆補修の止め線は「行が無い (温かい窓ゼロ = 全域
        編纂可)」と「読めなかった (窓があるかも分からない)」を区別しないと
        fail-open になる — こちらは失敗を例外のまま返す。manager 未接続の
        部分構築環境は行という器ごと無いので空 dict (従来どおり)。
        通常版の他の呼び出し元の挙動は変えない。
        """
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return {}
        if not persona_id:
            return {}
        db = self.manager.SessionLocal()
        try:
            from database.models import SessionAnchor
            rows = db.query(SessionAnchor).filter_by(PERSONA_ID=persona_id).all()
            return {row.MODEL_KEY: self._row_to_entry(row) for row in rows}
        finally:
            db.close()

    def collect_folded_chronicle_entry_ids(
        self, persona_id: Optional[str],
    ) -> Optional[Set[str]]:
        """モジュール関数 :func:`collect_folded_chronicle_entry_ids` への委譲。

        ``None`` = 照会失敗 (fold の有無が不明)。呼び出し側は束ねを見送ること。
        """
        return collect_folded_chronicle_entry_ids(self.manager, persona_id)

    def load_anchors(self, persona) -> Dict[str, Any]:
        """read-only 互換ビュー: persona オブジェクトから全 model 分の entry を読む。

        旧 JSON 経路の読み手 (api/routes/people/cache_status.py /
        sea/head_pipeline/integration.py) が getattr 経由でこの名前を参照する
        ための互換シム。実体は session_anchor テーブル (:meth:`load_anchor_entries`)。
        書き込み側の対 (save_anchors) は存在しない — 更新は行単位 upsert のみ。
        """
        return self.load_anchor_entries(getattr(persona, "persona_id", None))

    def upsert_anchor_entry(
        self, persona_id: Optional[str], model_key: Optional[str], entry: Dict[str, Any],
        require_current_anchor_id: Optional[str] = None,
    ) -> bool:
        """(persona, model) 1 行の anchor entry を upsert する (行単位、S8 根治)。

        TTL 延命規則 (旧 update_anchor_for_model の prev 比較) は行内の前回値との
        比較としてここに移植した。Anthropic 実機観測 (2026-05-25) に整合する
        更新規則 (モデルB):

        - 生存中のキャッシュは短い TTL の書き込みで「短縮されない」(max を維持)
        - 加えて、短い書き込みは expiry ウィンドウを **スライドさせない**。1h を
          5m 書き込みで延命できると過大表示になるため (1h ウィンドウは「1h を
          確立した時刻」起点で減り続ける)。
        - 同じか長い TTL の書き込みのときだけ updated_at を entry の時刻に
          リフレッシュ (= 使用でウィンドウが延びる、keep-awake の前提)。
        - 完全失効後の書き込みは新しい TTL/時刻でリセット。
        - entry に ttl_seconds が無い書き込み (metabolism の anchor 前進等) は
          規則を通さずそのまま書く (旧挙動: 前回の ttl_seconds は引き継がない)。

        ``require_current_anchor_id`` は CAS (書き込み時仲裁、Codex 3巡目
        2026-07-30): 行が存在する場合、その現在の anchor がこの値と一致する
        ときだけ書く (条件付き UPDATE 1 文 = 原子的)。Beat ロックの外を走る
        keepalive の touch 専用 — 呼び出し中に起きた anchor 前進 (§14-2 / 退場)
        の後から古い anchor の touch が届くと、書き戻しで圧縮区間の列クリア
        まで踏み、前進が丸ごと巻き戻るため。一致しない touch は「もう捨て
        られた提示ウィンドウのキャッシュの主張」なので棄却が正しい。行が
        無い場合は従来どおり作成する (ブートストラップ = 「行は LLM 成功後の
        touch が立てる」契約)。

        Returns:
            書き込みが適用されたら True。CAS 棄却・引数不足・DB 失敗は False。
            touch 経路はこれで後続 (見張り予約の上書き) を抑止する。

        docs/intent/cache_lifecycle_control.md §5.2
        """
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return False
        if not persona_id or not model_key:
            return False

        try:
            new_updated = datetime.fromisoformat(entry["updated_at"])
        except (KeyError, ValueError, TypeError):
            new_updated = datetime.now()
        requested_ttl: Optional[int] = None
        raw_ttl = entry.get("ttl_seconds")
        if raw_ttl is not None:
            try:
                requested_ttl = int(raw_ttl)
            except (ValueError, TypeError):
                requested_ttl = None

        db = self.manager.SessionLocal()
        try:
            from database.models import SessionAnchor
            row = db.query(SessionAnchor).filter_by(
                PERSONA_ID=persona_id, MODEL_KEY=str(model_key),
            ).first()

            effective_ttl = requested_ttl
            effective_updated = new_updated
            if requested_ttl is not None and row is not None and row.TTL_SECONDS:
                try:
                    prev_updated = datetime.fromtimestamp(int(row.UPDATED_AT))
                    prev_ttl_int = int(row.TTL_SECONDS)
                    if new_updated < prev_updated + timedelta(seconds=prev_ttl_int):  # 生存中
                        effective_ttl = max(prev_ttl_int, requested_ttl)
                        if requested_ttl < prev_ttl_int:
                            # 短い書き込み: 短縮も延命もしない (起点を維持)
                            effective_updated = prev_updated
                except (ValueError, TypeError):
                    pass

            new_anchor_id = entry.get("anchor_id")
            if row is not None and require_current_anchor_id is not None:
                # CAS: 条件付き UPDATE 1 文で「現在の anchor が一致する行」だけを
                # 書き換える。読み→書きの二段にしない (その隙間に前進が挟まると
                # 巻き戻りが復活する)。一致時は anchor が変わらないので、下の
                # 圧縮区間クリア分岐も不要。
                updated_count = db.query(SessionAnchor).filter_by(
                    PERSONA_ID=persona_id, MODEL_KEY=str(model_key),
                    ANCHOR_MESSAGE_ID=require_current_anchor_id,
                ).update({
                    SessionAnchor.ANCHOR_MESSAGE_ID: new_anchor_id,
                    SessionAnchor.TTL_SECONDS: effective_ttl,
                    SessionAnchor.UPDATED_AT: int(effective_updated.timestamp()),
                }, synchronize_session=False)
                db.commit()
                if not updated_count:
                    LOGGER.info(
                        "[metabolism] stale anchor touch rejected (CAS): row for "
                        "%s/%s no longer points at %s — the warmed prefix belongs "
                        "to an abandoned window",
                        persona_id, model_key, require_current_anchor_id,
                    )
                return bool(updated_count)
            row_created = row is None
            if row_created:
                row = SessionAnchor(PERSONA_ID=persona_id, MODEL_KEY=str(model_key))
                db.add(row)
            if not row_created and row.ANCHOR_MESSAGE_ID != new_anchor_id and row.FOLDED_RANGES_JSON:
                # 畳んだ範囲は「この anchor 以降の提示コンテキスト」に対する記録なので、anchor が
                # 差し替わった時点で無効になる (chronicle_eviction.md §6)。退場経路
                # 以外でも anchor は動く — TTL 失効後の最小ロードで新しい起点が立ち、
                # LLM 成功後の touch がそれを永続化する。古い圧縮区間を残すと、提示コンテキストには
                # 出ないのに head の Chronicle 枠からは除外され続け、その体験が
                # どこにも現れなくなる。正規の退場経路は anchor 前進の直後に
                # 圧縮区間を書き直すので、ここでクリアしても無傷。§14-2 の最前線
                # 前進だけは提示に残る fold を仕分けて保持する必要があるため、
                # ここを通らず :meth:`_advance_anchor_preserving_folds` を使う。
                LOGGER.info(
                    "[metabolism] anchor moved outside the eviction path "
                    "(%s -> %s); clearing folded ranges for %s/%s",
                    row.ANCHOR_MESSAGE_ID, new_anchor_id, persona_id, model_key,
                )
                row.FOLDED_RANGES_JSON = None
            row.ANCHOR_MESSAGE_ID = new_anchor_id
            row.TTL_SECONDS = effective_ttl
            row.UPDATED_AT = int(effective_updated.timestamp())
            if row_created and new_anchor_id:
                # §16-3 窓の誕生時の護り: 新しい (persona, model) の窓が被覆済み
                # 領域の上に開くなら、覆っているエントリを §15 の印として初期
                # 圧縮区間に載せて生まれる — head のあらすじ枠との二重提示の口を
                # 誕生時点で塞ぐ。読めない環境 (adapter 無し等) は印なしの従来形。
                try:
                    initial_folds = self._initial_coverage_folds(
                        persona_id, str(new_anchor_id),
                    )
                except Exception:
                    LOGGER.warning(
                        "[coverage-repair] initial coverage folds failed for "
                        "new anchor row %s/%s; the row is born without marks",
                        persona_id, model_key, exc_info=True,
                    )
                    initial_folds = []
                if initial_folds:
                    from sea.session_window import serialize_folds
                    row.FOLDED_RANGES_JSON = serialize_folds(initial_folds)
                    LOGGER.info(
                        "[coverage-repair] new anchor row %s/%s born with %d "
                        "covered range mark(s) (window opens over compiled "
                        "territory)",
                        persona_id, model_key, len(initial_folds),
                    )
            db.commit()
            return True
        except Exception as exc:
            LOGGER.warning(
                "[metabolism] Failed to upsert anchor entry for %s/%s: %s",
                persona_id, model_key, exc,
            )
            return False
        finally:
            db.close()

    def _initial_coverage_folds(
        self, persona_id: Optional[str], anchor_id: str,
    ) -> List["FoldedRange"]:
        """新規 anchor 行の初期圧縮区間 — 窓を覆う既存エントリの §15 の印 (§16-3)。

        persona の memory.db が manager 経由で引けないとき (テスト環境 /
        未ロード) は空 — 印なしで生まれる従来形に倒す (体験を消す方向の
        失敗ではない: 印が無い場合に起きるのは head との二重提示だけで、
        次の被覆補修の mark_covered_cold_windows が冪等に追記する)。
        """
        if not persona_id or not anchor_id:
            return []
        personas = getattr(self.manager, "personas", None) if self.manager else None
        persona = personas.get(persona_id) if isinstance(personas, dict) else None
        adapter = getattr(persona, "sai_memory", None)
        if adapter is None or not adapter.is_ready():
            return []
        from sea.coverage_repair import coverage_marks_for_window
        return coverage_marks_for_window(adapter.conn, anchor_id, [])

    def _advance_anchor_preserving_folds(
        self,
        persona,
        persona_id: Optional[str],
        model_key: str,
        new_anchor_id: str,
        updated_at_iso: str,
        *,
        strict: bool = False,
    ) -> bool:
        """機構1 (§14-2) 専用の anchor 前進書き込み — 圧縮区間を仕分けて残す。

        汎用 :meth:`upsert_anchor_entry` は anchor 変更時に FOLDED_RANGES_JSON を
        列ごとクリアする。だが最前線は「最初の未編纂メッセージ」なので、その
        後方には未編纂の隙間を跨いで畳まれた fold がまだ提示ウィンドウ内に
        生きていることがある — クリアすると生ログが復活してウィンドウが再膨張し、
        head の Chronicle 枠との二重提示も起こる (Codex 2巡目 2026-07-29)。

        仕分けの基準は読み側 :func:`sea.session_window.prune_folds` と同じ
        「一部でも提示に残る範囲は残す」— fold の末尾メッセージが新 anchor 以降
        なら保持、全体が手前なら破棄 (その Chronicle エントリは head の枠へ戻る)。
        位置が引けない fold は保持に倒す (体験を消す方向へ落とさない)。
        anchor と圧縮区間は同一コミットで書く。TTL は従来の前進と同じく
        引き継がない (前進はキャッシュの主張ではない)。

        ``strict`` (最終防衛ラインの厳格経路): 圧縮区間の記録を厳格に読み
        (壊れた JSON / 形の壊れた記録は例外)、位置照会の失敗も例外にする —
        **何も書かずに**送出する。既定は寛容に読んで書く (従来どおり)。読めない
        記録を寛容に読んで書き戻すと、劣化した区間の列が FOLDED_RANGES_JSON を
        上書きし、厳格な窓の読みが破損に気づく前に消える (Codex 六巡目 #2)。

        Returns:
            前進を永続化できたら True。行消失・DB 失敗は False — 呼び出し側
            (resolve) は前進を主張せず旧 anchor に留まること (Codex 5巡目
            2026-07-30: 失敗を成功として返すと、後続の touch が通常 upsert で
            frontier を書き、anchor 変更の列クリアで fold が全消えする)。
        """
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return False
        if not persona_id or not model_key:
            return False
        from sea.session_window import deserialize_folds, serialize_folds
        db = self.manager.SessionLocal()
        try:
            from database.models import SessionAnchor
            row = db.query(SessionAnchor).filter_by(
                PERSONA_ID=persona_id, MODEL_KEY=str(model_key),
            ).first()
            if row is None:
                # 自行がある Case 1 専用の経路 — 行が消えていたら前進を書く先が
                # 無い。次の resolve が Case 2 (最前線から開始) で立て直す。
                return False
            kept: List["FoldedRange"] = []
            dropped = 0
            # strict は書く前に読みで送出する (ここまで書き込みは無い)
            for fold in deserialize_folds(row.FOLDED_RANGES_JSON, strict=strict):
                if not fold.message_ids:
                    dropped += 1
                    continue
                cmp_result = self._compare_positions(
                    persona, str(fold.message_ids[-1]), new_anchor_id,
                    strict=strict,
                )
                if cmp_result is None:
                    LOGGER.warning(
                        "[metabolism] fold position unresolved during anchor "
                        "advance; keeping the fold (persona=%s model=%s)",
                        persona_id, model_key,
                    )
                    kept.append(fold)
                elif cmp_result >= 0:
                    kept.append(fold)
                else:
                    dropped += 1
            try:
                updated = datetime.fromisoformat(updated_at_iso)
            except (TypeError, ValueError):
                updated = datetime.now() - timedelta(days=3650)
            row.ANCHOR_MESSAGE_ID = new_anchor_id
            row.FOLDED_RANGES_JSON = serialize_folds(kept)
            row.TTL_SECONDS = None
            row.UPDATED_AT = int(updated.timestamp())
            db.commit()
            if dropped:
                LOGGER.info(
                    "[metabolism] anchor advance dropped %d fold(s) fully behind "
                    "the new anchor (persona=%s model=%s, %d kept)",
                    dropped, persona_id, model_key, len(kept),
                )
            return True
        except Exception as exc:
            if strict:
                # 厳格経路は縮退しない — 壊れた記録 / 照会失敗を「前進しなかった」
                # (旧 anchor に留まる正常系) に潰さず、呼び出し側 (床) へ例外の
                # まま返す (Codex 六巡目 #2)。ここまで書き込みは無い (commit 前)。
                raise
            LOGGER.warning(
                "[metabolism] failed to advance anchor preserving folds for "
                "%s/%s: %s", persona_id, model_key, exc,
            )
            return False
        finally:
            db.close()

    def clear_anchor_entries(self, persona_id: Optional[str]) -> None:
        """persona の anchor 行を全 model 分削除する (記憶の整理 = anchor リセット用)。"""
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return
        if not persona_id:
            return
        db = self.manager.SessionLocal()
        try:
            from database.models import SessionAnchor
            db.query(SessionAnchor).filter_by(PERSONA_ID=persona_id).delete()
            db.commit()
        except Exception as exc:
            LOGGER.warning("[metabolism] Failed to clear anchors for %s: %s", persona_id, exc)
        finally:
            db.close()

    def get_anchor_validity_seconds(self, model_key: str, persona_id: Optional[str] = None) -> int:
        """Get anchor validity duration in seconds based on model cache config.

        - Models with metabolism_token_threshold: effectively infinite (token-based trigger only)
        - Anthropic (explicit cache): per-persona TTL override or global manager.state.cache_ttl (300s or 3600s)
        - Others (implicit/no cache): 1200s (20 min)
        """
        try:
            from saiverse.model_configs import get_cache_config, get_metabolism_token_threshold
            if get_metabolism_token_threshold(model_key) is not None:
                return 86400 * 365  # token-based: never expire by time
            cache_config = get_cache_config(model_key)
            cache_type = cache_config.get("type", "implicit")
            if cache_type == "explicit":
                current_ttl = self.runtime._resolve_cache_ttl_str(persona_id)
                return 300 if current_ttl == "5m" else 3600
        except Exception:
            LOGGER.warning("Failed to resolve cache TTL for model %s", model_key, exc_info=True)
        return 1200  # 20 minutes default

    def resolve_metabolism_anchor(
        self, persona, model_key: Optional[str] = None,
        persist_advance: bool = True,
        *,
        strict: bool = False,
    ) -> tuple:
        """Resolve the window-start anchor for (persona, model).

        arasuji_levels.md §13/§14 (2026-07-29 裁定): 起点の TTL 失効は「キャッシュが
        冷えた」という温度情報であり、**生きたキャッシュがある限り**起点は動かない。
        冷え切った後は保守作業が解禁される (§14-1) — 自行が編纂の最前線 (§14-2、
        Chronicle の source_ids から導出) より後ろに取り残されていれば、最前線まで
        前進させる (機構1)。編纂も LLM も伴わない行更新のみで、休眠 model の
        復帰不能 (§12-10 極端形) の主対策。

        不変条件 (2026-08-23): **起点はスルースのパンマーカーを越えない。**
        飛ばす範囲は最前線の定義により必ず Chronicle が覆っているが、それだけ
        では足りない — 押し出される記憶は必ずスルースを通る
        (autonomous_behavior_v3.md §13.3) ので、前進先はさらに「スルースが最後に
        見た位置 (パンマーカー) の次」で頭打ちにする。マーカーが読めない
        (スルース未走行 / 読み取り失敗) なら前進しない (fail-closed)。
        経緯: 手動整理で Chronicle 生成成功 → スルース自身のプロンプト組成で
        機構1 が発火して起点が前進 → スルース失敗、の並びで、スルースを
        通っていない範囲がペルソナの提示範囲から消えた (2026-08-23 実機)。

        Args:
            model_key: 「自 model」として扱う model。ExecutionContext が届いている
                呼び出し元は ``execution_context.model_key`` を明示で渡す
                (beat_execution_context.md §3.1)。None なら従来どおり
                ``persona.model`` (読み側の全面 model 化は §6-5 のスコープ)。
            persist_advance: 機構1 の前進を session_anchor 行へ永続化するか。
                preview (何も変更しない読み) は False を渡す — 返る位置は
                本番と同じで、行だけ触らない。
            strict: 行の読み (``load_anchor_entries_strict``)・最前線の導出・
                位置の照会の失敗を例外で伝える (既定は縮退: 行の読み失敗は
                「行なし」、導出/照会の失敗は「前進しない」)。最終防衛ラインが
                使う — 読めなかったことを「起点なし = skip」に潰さない
                (Codex 三巡目 #2)。本当に行も最前線も無ければ従来どおり
                ``(None, "minimal")``。

        Returns:
            (anchor_id, resolution_type) where resolution_type is
            "self" | "frontier" | "other" | "minimal".

            - "self": 自行の anchor (温かい、または前進の必要なし)
            - "frontier": 編纂の最前線から導出した位置。自行があれば前進を
              永続化済み (persist_advance=True 時)。自行が無ければ候補のみ —
              行は LLM 成功後の touch が立てる (ブートストラップと同じ規約)
            - "other": 自行なし + 最前線より先の他 model 行を借用 (編纂なしで
              前進する設計 (disabled) の persona 等)
            - "minimal": 起点が定義できない — ブートストラップ最小ロード
        """
        persona_model = model_key or getattr(persona, "model", None)
        if not persona_model:
            return (None, "minimal")
        persona_model = str(persona_model)
        persona_id = getattr(persona, "persona_id", None)

        anchors = (
            self.load_anchor_entries_strict(persona_id) if strict
            else self.load_anchor_entries(persona_id)
        )

        # Case 1: 自 model の行がある。温かければそのまま (§13 裁定 1 の芯)。
        self_entry = anchors.get(persona_model)
        if self_entry and self_entry.get("anchor_id"):
            self_anchor = self_entry["anchor_id"]
            if self._anchor_entry_is_hot(self_entry, persona_model, persona_id):
                LOGGER.debug(
                    "[metabolism] Anchor resolved: self model '%s' (hot)", persona_model,
                )
                return (self_anchor, "self")
            # §14-2 機構1: 冷え切った自行は最前線まで前進してよい。ただし
            # 前進先はスルースのパンマーカーの次で頭打ちにする (v3 §13.3 —
            # 押し出される記憶は必ずスルースを通る)。
            frontier = self._resolve_frontier_anchor(persona, strict=strict)
            target = None
            if (
                frontier
                and frontier != self_anchor
                and self._is_ahead_of(persona, frontier, self_anchor, strict=strict)
            ):
                target = self._cap_advance_at_pan_marker(
                    persona, frontier, persona_id, persona_model,
                )
            if (
                target
                and target != self_anchor
                and self._is_ahead_of(persona, target, self_anchor, strict=strict)
            ):
                if persist_advance:
                    # 温度は据え置く — 前進はキャッシュの主張ではないので、
                    # 冷えた updated_at をそのまま書き戻し「温かい行」を偽造しない。
                    # 書き込みは専用経路 — 汎用 upsert は anchor 変更時に圧縮区間を
                    # 列ごとクリアするが、最前線の後方にはまだ提示に生きる fold が
                    # ありうる (Codex 2巡目 2026-07-29)。
                    advanced = self._advance_anchor_preserving_folds(
                        persona, persona_id, persona_model, target,
                        self_entry.get("updated_at")
                        # 元の時刻が無い行は「十分に過去」で冷えを表す
                        # (epoch 0 は TZ 次第で負になり Windows で扱えない)
                        or (datetime.now() - timedelta(days=3650)).isoformat(),
                        strict=strict,
                    )
                    if not advanced:
                        # 永続化できなかったら前進を主張しない (Codex 5巡目
                        # 2026-07-30)。frontier を返すと後続の touch が通常
                        # upsert で frontier を書き、anchor 変更の列クリアで
                        # 行に残った fold が全消えする。旧 anchor に留まれば
                        # 次回の resolve が前進を再試行する。
                        LOGGER.warning(
                            "[metabolism] cold anchor advance not persisted; "
                            "staying at current anchor (persona=%s model=%s %s)",
                            persona_id, persona_model, self_anchor,
                        )
                        return (self_anchor, "self")
                    LOGGER.info(
                        # 「最前線まで」とは書かない — パンマーカーで頭打ちに
                        # なった回は target が最前線より手前になる。
                        "[metabolism] cold anchor advanced "
                        "(persona=%s model=%s %s -> %s, frontier=%s)",
                        persona_id, persona_model, self_anchor, target, frontier,
                    )
                return (target, "frontier")
            LOGGER.debug(
                "[metabolism] Anchor resolved: self model '%s' (cold, no advance)",
                persona_model,
            )
            return (self_anchor, "self")

        # Case 2: 自 model の行が無い = この model での最初の Session。
        # 最前線 (§14-2) があればそこから始める。行はここでは書かず、LLM 成功後の
        # touch が立てる (候補のまま失敗すれば何も残らない)。
        frontier = self._resolve_frontier_anchor(persona, strict=strict)

        # 借用候補: 直近に更新された他 model の起点。Chronicle 実績の無い persona
        # と、編纂なしで前進する設計 (disabled) の persona では、これが最前線より
        # 先を指す正当な値になる — 忘れたはずの生ログを最前線で復活させない。
        best_entry = None
        best_updated = None
        for other_key, entry in anchors.items():
            if other_key == persona_model:
                continue  # already checked
            if not entry.get("anchor_id"):
                continue
            try:
                updated_at = datetime.fromisoformat(entry["updated_at"])
            except (KeyError, ValueError, TypeError):
                continue
            if best_updated is None or updated_at > best_updated:
                best_entry = entry
                best_updated = updated_at

        if frontier and best_entry:
            # 借用側が正典順で先なら借用が正。比較不能 (どちらかの位置が引けない)
            # は最前線側へ倒す — 被覆が保証されているのは最前線だけ。
            cmp_result = self._compare_positions(
                persona, best_entry["anchor_id"], frontier, strict=strict,
            )
            if cmp_result is not None and cmp_result > 0:
                LOGGER.debug(
                    "[metabolism] Anchor resolved: borrowed from other model "
                    "(ahead of frontier)",
                )
                return (best_entry["anchor_id"], "other")
            LOGGER.debug("[metabolism] Anchor resolved: chronicle frontier")
            return (frontier, "frontier")
        if frontier:
            LOGGER.debug("[metabolism] Anchor resolved: chronicle frontier")
            return (frontier, "frontier")
        if best_entry:
            LOGGER.debug("[metabolism] Anchor resolved: borrowed from other model")
            return (best_entry["anchor_id"], "other")

        # Case 3: 起点が定義できない (新規ペルソナ等) — bootstrap
        LOGGER.debug("[metabolism] No anchor row — bootstrap minimal load")
        return (None, "minimal")

    def _resolve_frontier_anchor(
        self, persona, *, strict: bool = False,
    ) -> Optional[str]:
        """編纂の最前線から anchor 候補を導出する (arasuji_levels.md §14-2)。

        真実は Chronicle 自身 (一次エントリの source_ids) が持ち、写しは保存
        しない。導出できない環境 (adapter 無し / 未初期化) は None (= 前進
        しない)。照会失敗は既定では None に倒し、``strict`` なら例外で伝える。
        """
        adapter = getattr(persona, "sai_memory", None)
        if strict:
            # 厳格経路は器の三状態で分ける (Codex 六巡目 #1): absent (adapter
            # なし / 設定で無効) は最前線が存在しない = None、broken (有効なのに
            # 接続が無い) は例外 — None に潰すと「行も最前線も無い = 起点なし」
            # と読まれて床が skip し、壊れた器のまま喋る。
            from persona.history_manager import memory_store_state
            state = memory_store_state(adapter)
            if state == "absent":
                return None
            if state == "broken":
                raise RuntimeError(
                    "memory store is broken (enabled but no connection); the "
                    f"chronicle frontier cannot be resolved "
                    f"(persona={getattr(persona, 'persona_id', '?')})"
                )
        elif not adapter or not adapter.is_ready():
            return None
        try:
            # 器の検査と最前線の照会は adapter の錠前の内側で**一続き**に行う
            # (他の書き手の DDL / close と交錯させない。Codex 五巡目 #3)。錠前は
            # RLock (sai_memory/db_locks.py) なので、上位が持っていても再入できる。
            from contextlib import nullcontext
            lock = getattr(adapter, "_db_lock", None)
            with (lock if lock is not None else nullcontext()):
                # 厳格経路では「最前線が**存在しない**」と「照会の失敗」を分ける:
                # 編纂の器 arasuji_entries が memory.db にまだ無い (一度も編纂して
                # いない新規ペルソナ / Chronicle を使わないペルソナ) なら None —
                # 照会の失敗ではない。器はあるが一次エントリが無ければ
                # get_frontier_anchor_id が None を返す。器の有無は sqlite_master
                # で明示的に見る (例外の文言で判定しない。Codex 四巡目 #2)。既定の
                # 経路は従来どおり照会の例外を None へ縮退するので器の検査は
                # 要らない。錠前の内側でも検査自体が失敗したら本物の I/O 失敗。
                if strict and not self._arasuji_tables_exist(adapter.conn):
                    return None
                from sai_memory.arasuji.storage import get_frontier_anchor_id
                return get_frontier_anchor_id(adapter.conn)
        except Exception:
            if strict:
                raise
            LOGGER.warning(
                "[metabolism] frontier derivation failed (persona=%s)",
                getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return None

    @staticmethod
    def _arasuji_tables_exist(conn) -> bool:
        """memory.db に編纂の器 (arasuji_entries) があるか (sqlite_master を見る)。

        arasuji_entries は Memopedia 統合後は**ビュー** (init_arasuji_tables の
        互換ビュー) なので、table と view の両方を見る。
        """
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name='arasuji_entries'"
        ).fetchone()
        return row is not None

    def _load_sluice_pan_marker(self, persona) -> Optional[str]:
        """スルースのパンマーカー (最後に採取した末尾 message id) を読む。

        真実は sluice が持つので読み方も sluice の関数
        (:func:`sea.sluice._load_pan_marker` — persona 属性 → memory.db の
        embed_metadata の read-through) をそのまま使う。二枚目の読み方を
        書かない。

        Returns:
            マーカー。まだ一度もスルースが走っていない (キーが無い)、または
            読み取りに失敗した場合は None。呼び出し側は None を「前進を
            許可できない」に倒す (fail-closed)。
        """
        try:
            from sea.sluice import _load_pan_marker
            return _load_pan_marker(persona)
        except Exception:
            LOGGER.warning(
                "[metabolism] sluice pan marker unreadable; treating it as "
                "absent (persona=%s)",
                getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return None

    def _cap_advance_at_pan_marker(
        self, persona, frontier: str,
        persona_id: Optional[str], model_key: str,
    ) -> Optional[str]:
        """機構1 の前進先を、スルースのパンマーカーの次で頭打ちにする。

        不変条件 (2026-08-23): 起点はスルースが見た位置より先へは進まない。
        マーカーは「スルースが最後に見た範囲の末尾」なので、マーカーを含む
        範囲までは通過済み — 前進してよいのは「マーカーの次」まで。

        Returns:
            前進先の message id (最前線かパンマーカーの次)。前進を許可でき
            ないときは None: マーカーが無い (スルース未走行 / 読み取り失敗)、
            正典順が引けない、マーカーの次が存在しない。
        """
        marker = self._load_sluice_pan_marker(persona)
        if not marker:
            LOGGER.debug(
                "[metabolism] cold anchor advance skipped: no sluice pan marker "
                "(persona=%s model=%s frontier=%s)",
                persona_id, model_key, frontier,
            )
            return None
        cmp_result = self._compare_positions(persona, frontier, marker)
        if cmp_result is None:
            LOGGER.info(
                "[metabolism] cold anchor advance skipped: cannot order the "
                "frontier against the sluice pan marker "
                "(persona=%s model=%s frontier=%s marker=%s)",
                persona_id, model_key, frontier, marker,
            )
            return None
        if cmp_result <= 0:
            # 最前線はマーカー以前 = 飛ばす範囲は全部スルースを通っている。
            return frontier
        cap = self._next_position_after(persona, marker)
        if not cap:
            LOGGER.info(
                "[metabolism] cold anchor advance skipped: no message after the "
                "sluice pan marker (persona=%s model=%s frontier=%s marker=%s)",
                persona_id, model_key, frontier, marker,
            )
            return None
        if cap != frontier:
            LOGGER.info(
                "[metabolism] cold anchor advance capped at the sluice pan marker "
                "(persona=%s frontier=%s marker=%s)",
                persona_id, frontier, marker,
            )
        return cap

    def _next_position_after(self, persona, message_id: str) -> Optional[str]:
        """正典順で ``message_id`` の直後にあるメッセージの id (引けなければ None)。"""
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            return None
        try:
            from sai_memory.memory.storage import get_next_message_id
            return get_next_message_id(adapter.conn, message_id)
        except Exception:
            LOGGER.warning(
                "[metabolism] next-message lookup failed (persona=%s)",
                getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return None

    def _compare_positions(
        self, persona, id_a: str, id_b: str, *, strict: bool = False,
    ) -> Optional[int]:
        """メッセージ 2 件の正典順比較 (1/-1/0)。引けなければ None (``strict`` なら例外)。"""
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            return None
        try:
            from sai_memory.arasuji.storage import compare_message_positions
            return compare_message_positions(adapter.conn, id_a, id_b)
        except Exception:
            if strict:
                raise
            LOGGER.warning(
                "[metabolism] message position comparison failed (persona=%s)",
                getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return None

    def _is_ahead_of(
        self, persona, candidate_id: str, current_id: str, *, strict: bool = False,
    ) -> bool:
        """candidate が current より正典順で先 (後ろの時刻) か。

        current が messages から消えている (起点が指す先を失った) 場合は True —
        壊れた起点に留まるより、被覆の保証された最前線へ逃がす。candidate 側が
        引けない場合は False (前進しない)。照会の失敗は ``strict`` なら例外。
        """
        cmp_result = self._compare_positions(
            persona, candidate_id, current_id, strict=strict,
        )
        if cmp_result is not None:
            return cmp_result > 0
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            return False
        try:
            cur = adapter.conn.execute(
                "SELECT id FROM messages WHERE id IN (?, ?)",
                (str(candidate_id), str(current_id)),
            )
            found = {str(row[0]) for row in cur.fetchall()}
        except Exception:
            if strict:
                raise
            return False
        return str(candidate_id) in found and str(current_id) not in found

    def update_anchor_for_model(
        self, persona, model_key: str, anchor_id: str, ttl_seconds: Optional[int] = None,
        require_current_anchor_id: Optional[str] = None,
    ) -> bool:
        """Update the anchor for a specific model and persist to DB.

        ``ttl_seconds`` は **この書き込み時点の cache TTL** (= 実際に焼いたキャッシュの
        寿命)。記録しておくことで、後から設定 (5m/1h) を変えても、既に書き込み済みの
        キャッシュの残り寿命は書き込み時 TTL で評価でき、設定変更による遡及的な表示
        ズレを防ぐ (docs/intent/cache_lifecycle_control.md §5.4)。

        ``require_current_anchor_id`` は :meth:`upsert_anchor_entry` の CAS へ
        そのまま渡す (keepalive touch の stale 書き戻し防止)。

        Returns:
            書き込みが適用されたら True (:meth:`upsert_anchor_entry` の伝播)。
        """
        if not model_key or not anchor_id:
            return False
        # TTL 延命規則 (生存中は max 維持 / 短い書き込みは非スライド) は
        # upsert_anchor_entry が行内の前回値と比較して適用する。
        entry: Dict[str, Any] = {
            "anchor_id": anchor_id,
            "updated_at": datetime.now().isoformat(),
        }
        if ttl_seconds is not None:
            entry["ttl_seconds"] = int(ttl_seconds)
        return self.upsert_anchor_entry(
            getattr(persona, "persona_id", None), model_key, entry,
            require_current_anchor_id=require_current_anchor_id,
        )

    def anchor_entry_ttl_seconds(
        self, entry: Dict[str, Any], model_key: str, persona_id: Optional[str] = None,
    ) -> int:
        """既存 anchor entry の実効 TTL 秒。

        書き込み時に記録した ``ttl_seconds`` を優先し、無ければ (旧 anchor) 現行設定
        から算出する (後方互換)。これにより「既存キャッシュの残り寿命」は書き込み時
        TTL で、「次の書き込みに使う TTL」は現行設定で、と分離される。
        """
        stored = entry.get("ttl_seconds")
        if stored:
            return int(stored)
        return self.get_anchor_validity_seconds(model_key, persona_id)

    def touch_anchor_after_llm_call(
        self, persona, usage, anchor_id: Optional[str] = None,
        only_if_anchor_unchanged: bool = False,
    ) -> None:
        """LLM 呼び出し成功後に session_anchor 行の updated_at を touch する (Phase 4-e)。

        旧実装は ``runtime_context.py`` の prepare_context 内で touch していたが、
        その方式だと「context 組成は走ったが LLM 呼び出しが失敗した」ケースで
        updated_at が前進してしまい、次回 ``resolve_metabolism_anchor`` が「TTL 内」
        と誤判定して、実際には切れているキャッシュに対して長大コンテキストを
        送り直す不整合を招いていた。

        この関数は LLM 呼び出しが成功してレスポンス usage が確定した時点で呼ぶ:

        - explicit cache モデル (Anthropic 等): ``cache_read > 0 OR cache_write > 0``
          のときだけ touch。両方 0 なら「実際には cache が触られていない」ので
          touch しない (= 次回 prepare_context で TTL 切れ判定 → Case 3 fallback)。
        - implicit / no cache モデル (Gemini implicit cache, Ollama 等): 呼び出し
          成功 = touch。プロバイダ側で cache 状態を直接観測できないため、
          ``get_anchor_validity_seconds`` が返す既定値 (1200s) を起点として扱う。

        記帳先 model は **usage.model (実際に応答した model)** で解決する
        (beat_execution_context.md §2.1 / SEA 監査 S1 の根治)。lightweight 実行や
        structured-output fallback で実行 model が ``persona.model`` と違っても、
        呼んでいない model の Session 状態を動かさない (不変条件 §4-2)。
        usage.model が空のときだけ ``persona.model`` にフォールバックする。

        ``anchor_id`` は **今回の呼び出しで実際に組成した prefix の anchor**
        (call-local。beat_execution_context.md §3.2)。prepare_context が解決した
        値を ``state["_prefix_anchor_id"]`` 経由で呼び出し元が渡す。None なら
        touch しない — 旧実装の「persona 属性 (単一可変値) からの読み」は
        TTL 失効後に旧 anchor を touch する事故 (記憶監査第 4 片) の源だったため
        廃止した。prefix に anchor を含まない呼び出し (work_session の
        history_depth=0 等) も自然に touch なしになる。

        ``only_if_anchor_unchanged`` は CAS ゲート (Codex 3〜4巡目 2026-07-30):
        True なら「行の現在の anchor が ``anchor_id`` と一致するときだけ書く」。
        **Beat ロックの外を走る keepalive 専用** — LLM 呼び出し中に anchor 前進
        (§14-2 / 退場) が起きると、古い anchor の touch が後から届いて巻き戻しを
        起こすため。Beat 内の touch (会話 Pulse / fallback / sub-line) は前進と
        Beat ロックで直列化済みなので既定 False — こちらに CAS を掛けると、
        実行 model が組成 model と違う正当な touch (usage.model 記帳、S1) が
        「別 anchor の既存行」で誤棄却される。書き込みが棄却されたら見張り
        予約も更新しない (stale touch が正当な予約を後ろへずらさないため)。
        """
        if persona is None or usage is None:
            return
        if not anchor_id:
            return
        persona_model = getattr(persona, "model", None)
        usage_model = getattr(usage, "model", None)
        if usage_model:
            model_key = str(usage_model)
            if persona_model and str(persona_model) != model_key:
                LOGGER.debug(
                    "[metabolism] anchor touch routed to actual model (S1): "
                    "persona=%s persona.model=%s -> usage.model=%s",
                    getattr(persona, "persona_id", "?"), persona_model, model_key,
                )
        else:
            if not persona_model:
                return
            model_key = str(persona_model)
            LOGGER.warning(
                "[metabolism] usage.model is empty; falling back to persona.model=%s "
                "for anchor touch (persona=%s)",
                model_key, getattr(persona, "persona_id", "?"),
            )

        try:
            from saiverse.model_configs import get_cache_config
            cache_config = get_cache_config(model_key)
            cache_type = (cache_config or {}).get("type", "implicit")
        except Exception:
            LOGGER.warning(
                "[metabolism] Failed to resolve cache type for %s; assuming implicit",
                model_key, exc_info=True,
            )
            cache_type = "implicit"

        if cache_type == "explicit":
            cache_read = getattr(usage, "cached_tokens", 0) or 0
            cache_write = getattr(usage, "cache_write_tokens", 0) or 0
            if cache_read == 0 and cache_write == 0:
                LOGGER.warning(
                    "[metabolism] anchor touch skipped (explicit cache miss): "
                    "persona=%s model=%s anchor=%s — cache breakpoint may be misconfigured "
                    "or TTL already expired before this call",
                    getattr(persona, "persona_id", "?"), model_key, anchor_id,
                )
                return

        # この書き込みで実際に使った TTL (= 現行設定) を anchor に記録する。
        # 以後この cache の残り寿命はこの値で評価され、設定変更の影響を受けない。
        write_ttl_seconds = self.get_anchor_validity_seconds(
            model_key, getattr(persona, "persona_id", None),
        )
        applied = self.update_anchor_for_model(
            persona, model_key, anchor_id, write_ttl_seconds,
            require_current_anchor_id=anchor_id if only_if_anchor_unchanged else None,
        )
        if not applied:
            # CAS 棄却 (keepalive の stale touch) または書き込み失敗。見張り予約を
            # 上書きしない — stale 完了時刻を起点に予約し直すと、新 anchor の正当な
            # touch が立てた予約が後ろへずれ、発火時には現行キャッシュが失効して
            # 連鎖ごと止まる (Codex 4巡目 2026-07-30)。
            LOGGER.info(
                "[metabolism] anchor touch not applied; leaving session watch "
                "reservation untouched (persona=%s model=%s anchor=%s)",
                getattr(persona, "persona_id", "?"), model_key, anchor_id,
            )
            return
        LOGGER.debug(
            "[metabolism] anchor touched after LLM success: persona=%s model=%s anchor=%s cache_type=%s ttl=%ds",
            getattr(persona, "persona_id", "?"), model_key, anchor_id, cache_type, write_ttl_seconds,
        )

        # Phase 4-e: touch した時刻を起点に「セッションの見張り (keep-alive /
        # watchdog)」を EventScheduler に予約する。同じペルソナ・モデルで再 touch
        # されると古い予約は cancel される (key 上書き)。失敗時は touch されない
        # ので予約も更新されず、自然と TTL 切れ判定経路に乗る。
        try:
            self.schedule_cache_ttl_pulse(persona, model_key, cache_type)
        except Exception:
            LOGGER.exception(
                "[metabolism] Failed to schedule cache TTL pulse for persona=%s model=%s",
                getattr(persona, "persona_id", "?"), model_key,
            )

        # Token-based metabolism trigger: flag persona if input_tokens exceeds threshold
        self.check_token_threshold(persona, model_key, usage)

    def check_token_threshold(self, persona, model_key: str, usage) -> None:
        """Flag persona for metabolism if input_tokens exceeds configured threshold."""
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        if input_tokens <= 0:
            return
        try:
            from saiverse.model_configs import get_metabolism_token_threshold
            threshold = get_metabolism_token_threshold(model_key)
            if threshold is None:
                return
            if input_tokens > threshold:
                persona._metabolism_token_triggered = True
                LOGGER.info(
                    "[metabolism] Token threshold exceeded: persona=%s model=%s input_tokens=%d > threshold=%d",
                    getattr(persona, "persona_id", "?"), model_key, input_tokens, threshold,
                )
        except Exception:
            LOGGER.debug("[metabolism] Token threshold check failed", exc_info=True)

    def schedule_cache_ttl_pulse(self, persona, model_key: str, cache_type: str) -> None:
        """anchor touch 直後に、キャッシュ TTL 接近時の keep-alive を EventScheduler に予約する。

        歴史的経緯: この予約はもともと ``MetaLayer.on_periodic_tick`` (v1 状況分類の
        メタ判断 Pulse) を発火していた → keep-alive LLM コールに置き換わった
        (life_concept_map.md §14 A2、まはー決定 2026-07-07)。

        計算: ``fire_at = now + cache_ttl_seconds * (1 - cache_threshold_ratio)``
        (キャッシュ寿命のうち threshold_ratio 分が残ったタイミング)。
        cache_threshold_ratio はペルソナの ``META_JUDGMENT_CONFIG`` から取得。

        callback は :meth:`run_cache_keepalive` — 意味的に不活性な極小 LLM コールで
        同一 prefix を温め直すだけで、**判断 (メタ判断 / 判断点) は行わない**。
        schedule した時刻と発火時刻の間にユーザー対話が入って TTL 起点が更新
        された場合、再 touch で予約が上書きされるため、古い予約は自然に消える。

        予約するのは ``cache_type == 'explicit'`` (Anthropic) のときだけ。非 explicit
        (gemini_explicit / implicit 等) は温め直す先が無いので何も予約しない
        (2026-08-24: 非 explicit で見張りだけを回していた経路は、その唯一の目的だった
        セッションクローズ採取の撤去と同時に消した)。

        ``META_JUDGMENT_CONFIG.keep_cache_alive == False`` の場合は予約しない
        (低頻度ペルソナ向け: 24 時間間隔等で cache 切れ覚悟の運用)。
        """
        if cache_type != "explicit":
            return

        manager = self.manager
        scheduler = getattr(manager, "event_scheduler", None) if manager else None
        meta_layer = getattr(manager, "meta_layer", None) if manager else None
        if scheduler is None or meta_layer is None:
            # meta_layer は keep_cache_alive 等の設定 (_load_judgment_config) の
            # 読み口としてのみ使う (判断は発火しない)。
            return

        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return

        # cache_ttl_seconds を取得 (per-persona TTL override 対応)
        ttl_seconds = self.get_anchor_validity_seconds(model_key, getattr(persona, "persona_id", None))
        if ttl_seconds <= 0:
            return

        # ペルソナの judgment config から cache 関連の設定を取得
        try:
            judgment_config = meta_layer._load_judgment_config(persona)
            keep_cache_alive = bool(judgment_config.get("keep_cache_alive", True))
            threshold_ratio = float(judgment_config.get("cache_threshold_ratio", 0.3))
        except Exception:
            keep_cache_alive = True
            threshold_ratio = 0.3

        # keep_cache_alive=False のペルソナは TTL 接近の前倒しを行わない。
        # 念のため既存予約があれば cancel する (設定変更で OFF になったケース対応)。
        if not keep_cache_alive:
            scheduler.cancel(f"ttl:{persona_id}:{model_key}")
            LOGGER.debug(
                "[metabolism] cache TTL pulse skipped (keep_cache_alive=False): persona=%s model=%s",
                persona_id, model_key,
            )
            return

        # threshold_ratio が範囲外なら既定値で防御
        if not (0.0 < threshold_ratio < 1.0):
            threshold_ratio = 0.3

        wait_seconds = ttl_seconds * (1.0 - threshold_ratio)
        fire_at = datetime.now() + timedelta(seconds=wait_seconds)
        # (persona, model) ごとに独立予約 (beat_execution_context.md §3.1 —
        # Session ごとに独立監視)。旧 key f"ttl:{persona_id}" の 1 ペルソナ 1 予約
        # 上書きは廃止した。
        key = f"ttl:{persona_id}:{model_key}"

        def _fire_callback() -> None:
            try:
                self.runtime.run_cache_keepalive(persona_id, model_key)
            except Exception:
                LOGGER.exception(
                    "[keepalive] cache keep-alive raised: persona=%s model=%s",
                    persona_id, model_key,
                )

        scheduler.schedule(fire_at=fire_at, callback=_fire_callback, key=key)
        LOGGER.debug(
            "[metabolism] scheduled cache TTL keep-alive: persona=%s model=%s in %.0fs (ttl=%ds, threshold=%.2f)",
            persona_id, model_key, wait_seconds, ttl_seconds, threshold_ratio,
        )

    def maybe_run_metabolism(
        self,
        persona,
        building_id: str,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        model_key: Optional[str] = None,
    ) -> None:
        """Check if metabolism is needed after response and run if so.

        ``model_key`` はこの Pulse の実行 model (beat_execution_context.md §3.2 —
        閾値・提示コンテキスト・退役は model ごと)。None なら従来どおり ``persona.model``。
        発火判定の anchor は session_anchor 行 (persona, model) から読む —
        旧 ``history_manager.metabolism_anchor_message_id`` (persona 単一可変
        属性) は廃止した。
        """
        model_key = str(model_key or getattr(persona, "model", "") or "") or None
        if not model_key:
            return

        history_mgr = getattr(persona, "history_manager", None)
        anchor_entry = self.load_anchor_entry(getattr(persona, "persona_id", None), model_key)
        anchor = anchor_entry.get("anchor_id") if anchor_entry else None
        if not history_mgr or not anchor:
            return

        # Token threshold trigger: check if last LLM call exceeded the threshold
        token_triggered = getattr(persona, "_metabolism_token_triggered", False)
        if token_triggered:
            persona._metabolism_token_triggered = False

        # defer-to-hot で繰り延べ中のフラグ (token_triggered と同格で should_run に
        # OR 参加する)。docs/intent/gold_panning.md §3.7 (sluice の旧名 intent)
        pending = getattr(persona, "_metabolism_pending", False)

        watermarks = self.get_metabolism_watermarks(persona, model_key)
        if watermarks is None:
            return

        # 発火判定は**提示される提示コンテキスト**の文字数で行う (chronicle_eviction.md §4)。
        # 既に畳んだ範囲は digest に置き換わって提示されるので、生ログの合計では
        # なく置き換え後の量を数える — でないと「畳んだのに数字が減らない」で
        # 発火し続ける。
        # 数えるのは**実際に送る中身** = 保存行 + 送信直前に差し込まれる知覚
        # ブロック (2026-09-02 まはー裁定。issue
        # context_accounting_excludes_injected_rows.md — 本番エリスで勘定 15 万字に
        # 対し実送信 21 万字、差の大半が知覚ブロックだった)。
        window = self.get_presented_window(persona, model_key, anchor)
        current_messages = window.presented
        current_chars = self.presented_chars(persona, current_messages, anchor)

        should_run = False
        if token_triggered:
            should_run = True
            LOGGER.info(
                "[metabolism] Token threshold exceeded for %s, triggering metabolism",
                getattr(persona, "persona_id", "?"),
            )
        elif pending:
            should_run = True
            LOGGER.info(
                "[metabolism] Resuming deferred metabolism for %s (pending flag set)",
                getattr(persona, "persona_id", "?"),
            )
        elif watermarks.high is not None and current_chars > watermarks.high:
            should_run = True
            LOGGER.info(
                "[metabolism] Triggering metabolism for %s: %d chars > high=%d",
                getattr(persona, "persona_id", "?"), current_chars, watermarks.high,
            )

        if not should_run:
            return

        # 「削る先があるか」は**会話の行だけ**を残す量と比べる (2026-09-03 裁定:
        # 残す量の主語は会話の行、上限の主語は合計)。退場計画の保護範囲も行
        # だけで測るので、行が残す量以下なら計画は空 — 走らせても空振り。
        rows_chars = message_chars(current_messages)
        if rows_chars <= watermarks.target:
            # 既に目標水位より軽い。削る先が無いので走らせない (token 発火でも同じ)。
            # 合計が上限を超えている (= 知覚の供給が予算超過) なら 1 度だけ警告。
            if not self._note_perception_over_budget(
                persona, rows_chars, current_chars, watermarks,
            ):
                LOGGER.debug(
                    "[metabolism] skip: window already at/below target "
                    "(persona=%s, %d row chars <= target=%d, %d chars sent)",
                    getattr(persona, "persona_id", "?"), rows_chars,
                    watermarks.target, current_chars,
                )
            persona._metabolism_pending = False
            return

        # defer-to-hot (docs/intent/gold_panning.md §3.7): sluice は直前の
        # (main_line, default) コールで温まった prefix に 1 手足すのが安い条件。
        # キャッシュが冷たければ Metabolism ごと繰り延べる。sluice 無効時は
        # 熱さ判定をスキップして従来どおり即実行する (defer は sluice のためにある)。
        from sea.sluice import get_pending_cap, is_enabled
        if is_enabled():
            cap = get_pending_cap()
            pressure_limit = (
                watermarks.high * cap if watermarks.high is not None
                else watermarks.target * cap
            )
            if current_chars > pressure_limit:
                # 圧力弁: 繰り延べ続けて毎ターン肥大ウィンドウを読むより、一回の
                # コールド代のほうが安い。明示ログを残す (不変条件 §5-1 の例外)。
                LOGGER.warning(
                    "[sluice] pressure valve: running metabolism cold "
                    "(persona=%s, %d chars > limit=%.0f)",
                    getattr(persona, "persona_id", "?"), current_chars, pressure_limit,
                )
            elif not self._is_cache_hot(persona, model_key):
                persona._metabolism_pending = True
                LOGGER.info(
                    "[sluice] deferring metabolism (cache cold) for %s; pending set",
                    getattr(persona, "persona_id", "?"),
                )
                return

        # 実行に入るので pending をクリアする。
        persona._metabolism_pending = False

        LOGGER.info(
            "[metabolism] Running metabolism: %d messages / %d chars, target=%d",
            len(current_messages), current_chars, watermarks.target,
        )
        self.run_metabolism(
            persona, building_id, window, watermarks, event_callback,
            model_key=model_key,
        )

    def get_presented_window(
        self, persona, model_key: Optional[str], anchor_id: Optional[str] = None,
        *, strict: bool = False,
    ) -> "SessionWindow":
        """いまペルソナに提示される提示コンテキスト (= anchor 以降 − 畳まれた範囲 + その digest)。

        chronicle_eviction.md §6。**提示とMetabolismの勘定が同じ提示コンテキストを見るための
        一点**。退場が episode 単位になって提示コンテキストの途中に圧縮区間が空くようになったため、
        「anchor 以降を全部」は提示の真実ではなくなった。

        ``strict=True`` (最終防衛ライン用): 履歴の読みは SAIMemory の厳格モード
        (未準備 / 読み失敗は例外、メモリ上の写しへ縮退しない)、起点が提示対象の
        履歴に見つからなければ例外、圧縮区間の記録は行の読み失敗・壊れた JSON を
        例外にする。読めなかった窓を「薄い窓」「起点なし」「区間なし」に潰すと、
        床が skip したり既存の区間を上書きで消したりする (Codex 四巡目 #1 / #3)。
        """
        from sea.session_window import SessionWindow, prune_folds

        history_mgr = getattr(persona, "history_manager", None)
        persona_id = getattr(persona, "persona_id", None)
        if anchor_id is None:
            entry = (
                self.load_anchor_entry_strict(persona_id, model_key) if strict
                else self.load_anchor_entry(persona_id, model_key)
            )
            anchor_id = entry.get("anchor_id") if entry else None
        if history_mgr is None or not anchor_id:
            if strict and history_mgr is None:
                raise RuntimeError(
                    "presented window cannot be read strictly: persona has no "
                    "history manager"
                )
            return SessionWindow(anchor_id=anchor_id, raw=[], presented=[], folds=[])

        # raise_on_error は厳格なときだけ渡す — 既定の経路の呼び出し形を変えない
        # (history_manager の互換フェイクを持つテストが多い)。
        read_kwargs: Dict[str, Any] = {}
        if strict:
            read_kwargs["raise_on_error"] = True
        raw = history_mgr.get_history_from_anchor(
            anchor_id,
            required_line_roles=["main_line"],
            required_scopes=["committed"],
            **read_kwargs,
        )
        if strict and not raw:
            # 提示対象の行が空 = 「起点の行が無い」とは限らない。起点の行自体が
            # 提示対象外 (scope=discardable 等) で、その後ろにまだ提示対象の行が
            # 無い窓は正当 (行 0 の窓 — 床が古い方から埋める)。起点の**実在**は
            # scope に依らない照会で別に確かめ、無ければ帳簿の破損として例外
            # (Codex 五巡目 #1)。
            self._assert_anchor_message_exists(persona, anchor_id, model_key)
        folds = prune_folds(
            self.load_folded_ranges(persona_id, model_key, strict=strict),
            [str(m.get("id")) for m in raw],
        )
        presented = self._present_with_folds(persona, raw, folds)
        return SessionWindow(
            anchor_id=anchor_id, raw=raw, presented=presented, folds=folds,
        )

    def _assert_anchor_message_exists(
        self, persona, anchor_id: str, model_key: Optional[str],
    ) -> None:
        """起点の行が memory.db に物理的に在るか (scope 不問) を確かめる。無ければ例外。

        厳格な窓の読み (:meth:`get_presented_window` strict) の補助。器が
        「absent」(adapter なし / 設定で無効 = 従来のメモリ上モード) なら照会
        できないので何もしない。「broken」(有効なのに接続が無い) は例外。
        """
        from persona.history_manager import memory_store_state

        adapter = getattr(persona, "sai_memory", None)
        state = memory_store_state(adapter)
        if state == "absent":
            return
        if state == "broken":
            raise RuntimeError(
                f"memory store is broken; the anchor {anchor_id} cannot be "
                f"verified (persona={getattr(persona, 'persona_id', '?')} "
                f"model={model_key})"
            )
        from sai_memory.memory.storage import get_message_position
        with adapter._db_lock:
            pos = get_message_position(adapter.conn, str(anchor_id))
        if pos is None:
            raise RuntimeError(
                f"anchor {anchor_id} is missing from messages "
                f"(persona={getattr(persona, 'persona_id', '?')} model={model_key})"
            )

    def _present_with_folds(
        self, persona, messages: List[Dict[str, Any]], folds: List["FoldedRange"],
    ) -> List[Dict[str, Any]]:
        from sea.session_window import apply_folds

        if not folds:
            return list(messages)
        return apply_folds(
            messages, folds, lambda f: self._resolve_fold_digest(persona, f),
        )

    def apply_window_folds(
        self, persona, model_key: Optional[str], messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """既に取得した提示コンテキストに、畳まれた範囲の digest 置き換えを適用する (§6)。

        圧縮区間が無ければ ``messages`` をそのまま返す (既存経路は無変化)。context 構築
        (sea/runtime_context.py) が anchor 取得後に呼ぶ入口。
        """
        from sea.session_window import prune_folds

        if not messages:
            return messages
        persona_id = getattr(persona, "persona_id", None)
        model_key = str(model_key or getattr(persona, "model", "") or "") or None
        if not persona_id or not model_key:
            return messages
        folds = prune_folds(
            self.load_folded_ranges(persona_id, model_key),
            [str(m.get("id")) for m in messages],
        )
        if not folds:
            return messages
        return self._present_with_folds(persona, messages, folds)

    def _resolve_fold_digest(self, persona, fold: "FoldedRange") -> Optional[str]:
        """畳まれた範囲のあらすじ本文を引く。引けなければ None (= 生ログのまま)。"""
        digest, _permanently_missing = self._resolve_fold_digest_status(persona, fold)
        return digest

    def _resolve_fold_digest_status(
        self, persona, fold: "FoldedRange",
    ) -> Tuple[Optional[str], bool]:
        """畳まれた範囲のあらすじ本文を引く。戻りは ``(digest, 恒久欠落か)``。

        圧縮区間は記録時に必ず一次あらすじ エントリ id を持つ (`_apply_eviction_plan`) ので、
        id 直引きで済む。source_ids の全走査に落ちるのは、エントリが解体・
        再編纂されて id が変わった旧記録だけ。

        恒久欠落 (``True``) = **照会は成功したのにエントリが見つからない**。
        手動削除の道連れ漏れ・DB 破損などで、あらすじが失われた確定状態。
        照会自体の失敗 (一時障害) は ``(None, False)`` — 分からないだけで
        失われたとは言えない。どちらも提示は生ログに倒す (fail-open) が、
        記録を捨ててよいのは恒久欠落だけ (:meth:`_drop_dead_folds`)。
        """
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            return None, False
        try:
            from sai_memory.arasuji.storage import (
                get_entries_covering_messages,
                get_entry,
            )
            wanted = list(dict.fromkeys(fold.chronicle_entry_ids))
            if wanted:
                entries = [
                    e for e in (get_entry(adapter.conn, eid) for eid in wanted)
                    if e is not None
                ]
                if len(entries) < len(wanted):
                    # 記録された id が**全件揃わない** (一部でもゼロ件でも)。
                    # 揃わない digest や、被覆保証の無い救済照会の結果で範囲
                    # 全体を置き換えると、欠けたエントリだけが持つ体験が raw
                    # からも digest からも静かに消える (Codex 指摘 2026-07-30
                    # ×2巡)。恒久欠落扱いに倒す — 提示は生ログ (fail-open)、
                    # 記録は _drop_dead_folds が捨て、次の畳みが
                    # _attach_chronicle_refs で生き残りエントリを引き当て直す
                    # (解体・再編纂で id が変わった旧記録もこの経路で自己修復
                    # し、再編纂は走らない)。
                    LOGGER.warning(
                        "[window] folded range resolved only %d of %d chronicle "
                        "entries; treating as permanently missing (persona=%s)",
                        len(entries), len(wanted),
                        getattr(persona, "persona_id", "?"),
                    )
                    return None, True
            else:
                # id を記録していない旧形式の記録だけ、メッセージからの
                # 引き当てで救済する。被覆の検算つき — この照会は「どれか
                # 1 件でも source に持つエントリ」を返すだけで範囲全体の
                # 被覆を保証しないため、編纂対象メッセージ全件が被覆されて
                # いることを確認する (対象判定は編纂側と同じ
                # filter_chronicle_eligible_ids を共用 — 除外タグのメッセージ
                # は被覆に無いのが健全)。
                entries = get_entries_covering_messages(adapter.conn, fold.message_ids)
                if entries:
                    covered = {str(s) for e in entries for s in e.source_ids}
                    try:
                        from sai_memory.memory.storage import (
                            filter_chronicle_eligible_ids,
                        )
                        required = filter_chronicle_eligible_ids(
                            adapter.conn, fold.message_ids,
                        )
                    except Exception:
                        # 判定できなければ全件要求 (体験を消さない側に倒す)
                        required = list(fold.message_ids)
                    if not {str(m) for m in required} <= covered:
                        LOGGER.warning(
                            "[window] legacy folded range is only partially "
                            "covered by resolvable chronicle entries; treating "
                            "as permanently missing (persona=%s)",
                            getattr(persona, "persona_id", "?"),
                        )
                        return None, True
        except Exception:
            LOGGER.warning(
                "[window] failed to look up chronicle entries for folded range "
                "(persona=%s)", getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return None, False
        texts = [e.content for e in entries]
        if entries and any(not t for t in texts):
            # 本文が空のエントリ混じり。空を落として残りで置き換えると、空
            # エントリだけが被覆する体験が raw からも digest からも静かに
            # 消える — id の部分欠落と同じ恒久欠落扱いに倒す (Codex 指摘
            # 2026-07-30。digest は id と本文が全件揃って初めて成立する)。
            LOGGER.warning(
                "[window] folded range has chronicle entries with empty "
                "content (%d of %d); treating as permanently missing "
                "(persona=%s)",
                sum(1 for t in texts if not t), len(texts),
                getattr(persona, "persona_id", "?"),
            )
            return None, True
        if not texts:
            LOGGER.warning(
                "[window] folded range has no chronicle entry; keeping raw log "
                "(persona=%s, %d messages)",
                getattr(persona, "persona_id", "?"), len(fold.message_ids),
            )
            return None, True
        return "\n\n".join(texts), False

    def _drop_dead_folds(
        self, persona, model_key: Optional[str], window: "SessionWindow",
    ) -> "SessionWindow":
        """あらすじを恒久に失った圧縮区間の記録を捨てる (異常系の安全網)。

        「記録は生きているのに、指す先のあらすじが無い」半端な状態が残ると、
        提示は生ログに倒れる (fail-open) 一方で、適用側の二重記録判定
        (:meth:`_apply_eviction_plan`) は記録を生きているとみなす — 生死の読みが
        食い違い、その範囲を含む束ねが毎ラウンド丸ごと拒否されて anchor が
        恒久に詰まる (docs/issues/chronicle_eviction_applier_veto_deadlock.md
        顔その2)。提示が生ログに倒れると確定した時点で記録も捨て、生死の判定を
        一つに揃える。

        捨てた範囲は普通の材料に戻り、次の計画が再畳みする。生き残っている
        エントリがあれば :meth:`_attach_chronicle_refs` が引き当て直すので、
        再編纂の二重被覆にはならない。

        通常この状態は生まれない — 束ねは追加のみ (chronicle_consolidation
        不変条件5) で、手動削除は API 側が記録を道連れにする
        (:func:`remove_folds_referencing_entry`)。ここは道連れ漏れ・DB 破損
        などに対する Metabolism 時の最後の網。
        """
        from sea.session_window import SessionWindow
        dead = self._dead_folds_of(persona, window)
        if not dead:
            return window
        persona_id = getattr(persona, "persona_id", None)
        for fold in dead:
            LOGGER.warning(
                "[metabolism] dropping folded-range record whose chronicle "
                "entries are permanently gone (persona=%s, %d messages, "
                "entries=%s); the raw log returns to the window for re-folding",
                persona_id, len(fold.message_ids), fold.chronicle_entry_ids,
            )
        kept = [f for f in window.folds if f not in dead]
        self.save_folded_ranges(persona_id, model_key, kept)
        return SessionWindow(
            anchor_id=window.anchor_id,
            raw=window.raw,
            presented=self._present_with_folds(persona, window.raw, kept),
            folds=kept,
        )

    def _dead_folds_of(self, persona, window: "SessionWindow") -> List["FoldedRange"]:
        """あらすじを恒久に失った圧縮区間 (読みだけ — 記録は触らない)。

        本走行の :meth:`_drop_dead_folds` と、下見の
        :meth:`preview_planning_window` が同じ判定を共有するための切り出し。
        """
        return [
            f for f in window.folds
            if self._resolve_fold_digest_status(persona, f)[1]
        ]

    def _is_cache_hot(self, persona, model_key: Optional[str] = None) -> bool:
        """指定 model (既定: persona.model) の anchor 行が生存しているか (= 直前 prefix が温かい)。

        run_cache_keepalive の生存判定と同じロジック (キャッシュ書き込み時 TTL で評価)。
        docs/intent/gold_panning.md §3.7
        """
        model_key = model_key or getattr(persona, "model", None)
        if not model_key:
            return False
        try:
            entry = self.load_anchor_entry(
                getattr(persona, "persona_id", None), str(model_key),
            )
            return self._anchor_entry_is_hot(
                entry, str(model_key), getattr(persona, "persona_id", None),
            )
        except Exception:
            LOGGER.warning(
                "[sluice] failed to read anchor state for hot check (persona=%s)",
                getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return False

    def _anchor_entry_is_hot(
        self, entry: Optional[Dict[str, Any]], model_key: str,
        persona_id: Optional[str],
    ) -> bool:
        """anchor entry 1 件の温度判定 (書き込み時 TTL で評価)。

        「冷え切った」の判定式はこの一枚だけ (arasuji_levels.md §14-6-3 —
        判定式を二枚にしない)。読み手: :meth:`_is_cache_hot` (keep-alive /
        sluice defer)、:meth:`resolve_metabolism_anchor` (機構1)、
        :meth:`cold_precompaction_status` (機構3)。
        """
        if not entry or not entry.get("updated_at"):
            return False
        try:
            updated_at = datetime.fromisoformat(entry["updated_at"])
            ttl_seconds = self.anchor_entry_ttl_seconds(entry, model_key, persona_id)
            return datetime.now() < updated_at + timedelta(seconds=ttl_seconds)
        except (KeyError, ValueError, TypeError):
            return False

    # 強制クローズ (旧 §5-5) は撤去した (2026-07-25)。U 未満の open episode を
    # 畳めるようになったので手詰まりが起きない。そもそも「開きっぱなしの episode を
    # 閉じる」のは提示コンテキストの都合で決める話ではなく、episode 側がタイムアウト
    # を検知して閉じる仕事 (まはー裁定)。場所が足りないという理由でペルソナの
    # 出来事に「終わった」と判定を下してはいけない。

    def _apply_eviction_plan(
        self,
        persona,
        model_key: Optional[str],
        window: "SessionWindow",
        plan,
        chronicle_status: str,
    ) -> None:
        """退場計画を提示コンテキストへ適用する — anchor 前進と「提示コンテキストの中の圧縮区間」の書き分け。

        規則は二つ (chronicle_eviction.md §2/§6):

        1. **あらすじを持たない範囲は圧縮区間にしない**。圧縮区間は「生ログの代わりに digest を
           見せる」ための記録なので、digest が無い圧縮区間はその範囲を黙って消すだけに
           なる。引き当てられなかった fold は退場そのものを見送り、生ログのまま
           残す — 下限「退場したものは必ず編纂されている」をここで手続きとして
           強制する。例外は Chronicle を切っている persona (``disabled``) で、
           これは「編纂なしで忘れる」を選んだ設計上の合意なので anchor 前進だけ
           許し、圧縮区間は作らない (見せる digest が永久に存在しないため)。
        2. **生ログの並びで先頭から連続して畳まれた分は anchor が飲み込み、それ以外
           は圧縮区間として残る**。先頭を飲み込めた分は提示範囲の外に出るので、その
           あらすじは head の Chronicle 枠が担当する (圧縮区間として持ち続けると同じ
           あらすじが提示コンテキストと head に二重で出る)。

        判定に置き換え前の生ログ (``window.raw``) を使うのは、提示側は既に digest
        へ置き換わっていて、先頭が置き換えメッセージだと「連続」を判定できないため。
        """
        from sea.session_window import FoldedRange

        persona_id = getattr(persona, "persona_id", None)
        if not model_key or not window.anchor_id:
            return

        raw_ids = window.raw_ids
        existing: List["FoldedRange"] = list(window.folds)
        already_folded = {mid for f in existing for mid in f.message_ids}

        new_folds: List["FoldedRange"] = []
        for fold in plan.folds:
            if any(mid in already_folded for mid in fold.message_ids):
                # 既に圧縮区間になっている範囲を二重に記録しない (同じ範囲の圧縮区間が
                # 積み上がって JSON と照会コストが単調増加するのを防ぐ)。
                LOGGER.debug(
                    "[metabolism] skipping already-folded range (persona=%s, %d messages)",
                    persona_id, len(fold.message_ids),
                )
                continue
            new_folds.append(
                FoldedRange(
                    message_ids=fold.message_ids,
                    start_at=fold.start_at,
                    end_at=fold.end_at,
                    episode_ref=fold.open_episode_ref,
                )
            )
        self._attach_chronicle_refs(persona, new_folds)

        # あらすじを持たない fold は「圧縮区間」になれない。見送りの条件は
        # カテゴリ (あらすじの有無) ではなく目的 (編纂されるはずの体験を黙って
        # 消さない — §2 下限) から引く:
        #
        # - Chronicle を切っている persona (disabled) は「編纂なしで忘れる」を
        #   選んだ設計上の合意。anchor が飲み込める先頭連続域に限って退場を許す。
        # - **fold に編纂対象のメッセージが 1 件も無い** (全部が Chronicle 除外 —
        #   除外タグ / line_role / Stelis) なら、あらすじは**永久に**生まれない。
        #   「編纂待ち」で見送っても待ちが明けることはなく、anchor がこの fold の
        #   手前で恒久に詰まるだけ (issue chronicle_eviction_applier_veto_deadlock
        #   顔その1)。disabled と同じ扱いに落とす — 吸収の候補には入れ、圧縮区間
        #   (digest の置き換え) としては残さない。
        # - それ以外 (編纂対象を含むのにあらすじが無い = LLM 失敗等の一時状態) は
        #   退場を見送り、生ログのまま提示コンテキストに残して次回再挑戦する。
        candidates: List["FoldedRange"] = []
        for fold in new_folds:
            if fold.chronicle_entry_ids or chronicle_status == "disabled":
                candidates.append(fold)
            elif not self._fold_has_chronicle_material(persona, fold):
                candidates.append(fold)
                LOGGER.info(
                    "[metabolism] fold has no chronicle-eligible message; treating "
                    "it as absorb-only, like a chronicle-disabled persona "
                    "(persona=%s, %d messages, %s..%s)",
                    persona_id, len(fold.message_ids), fold.start_at, fold.end_at,
                )
            else:
                LOGGER.warning(
                    "[metabolism] fold has no chronicle entry; leaving it in the "
                    "window instead of evicting (persona=%s, %d messages, %s..%s)",
                    persona_id, len(fold.message_ids), fold.start_at, fold.end_at,
                )

        folded_ids = {mid for f in existing + candidates for mid in f.message_ids}
        lead = 0
        while lead < len(raw_ids) and raw_ids[lead] in folded_ids:
            lead += 1
        # 提示コンテキストが空にならないよう、最後の 1 件は必ず残す (anchor は実在のメッセージを
        # 指す必要がある)。
        new_anchor_index = min(lead, len(raw_ids) - 1) if lead > 0 else 0
        absorbed = set(raw_ids[:new_anchor_index])

        # 実際に退場するのは「あらすじを持つ fold」か「anchor が丸ごと飲み込む
        # fold」。どちらでもない fold は提示コンテキストに残るので、退場した扱いの記録
        # (子 episode) も作らない — でないと退場していないのに「部分退場した」
        # 世界状態だけが毎ラウンド積み上がる。
        applied = [
            f for f in candidates
            if f.chronicle_entry_ids or set(f.message_ids) <= absorbed
        ]

        # §6 pulse 関節細分 (open episode の部分退場を子 episode 化) は束 6c
        # (2026-08-22) で書き手ごと退役した — エピソードという専用の記録行を
        # 持たなくなったので (v3 §7)、分割すべき親も生まれない。畳んだ範囲の
        # 記録は Chronicle エントリが持つ (`_record_partial_episode` の失敗時
        # フォールバックが元から「あらすじが record of record」だった)。

        # 圧縮区間として持ち続けるのは「提示コンテキストに残っていて、かつあらすじを引ける」範囲だけ。
        # 飲み込まれた分のあらすじは head の Chronicle 枠が担当する。
        folds = [
            f for f in existing + applied
            if f.chronicle_entry_ids and not set(f.message_ids) <= absorbed
        ]

        if new_anchor_index > 0:
            self.update_anchor_for_model(persona, model_key, raw_ids[new_anchor_index])
            LOGGER.info(
                "[metabolism] anchor advanced to %s for model=%s "
                "(absorbed %d leading messages, %d holes remain, persona=%s)",
                raw_ids[new_anchor_index], model_key, len(absorbed), len(folds), persona_id,
            )
        else:
            LOGGER.info(
                "[metabolism] anchor held (episode-unit eviction folded the "
                "middle of the window): %d holes, persona=%s",
                len(folds), persona_id,
            )
        self.save_folded_ranges(persona_id, model_key, folds)

    def _fold_has_chronicle_material(self, persona, fold: "FoldedRange") -> bool:
        """fold に Chronicle 編纂対象のメッセージが 1 件でもあるか。

        判定は編纂側と同じ除外規則
        (sai_memory.memory.storage.filter_chronicle_eligible_ids =
        ``get_messages_for_chronicle`` と同一の WHERE 句) で行う — 規則の
        二枚目を作らない。

        照会できないときは True (= 編纂対象かもしれない) に倒す。誤って
        「対象外」と判定して退場させると下限「退場したものは必ず編纂されている」
        (chronicle_eviction.md §2) が破れるため、迷ったら退場を見送る側に立つ。
        """
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            return True
        try:
            from sai_memory.memory.storage import filter_chronicle_eligible_ids
            eligible = filter_chronicle_eligible_ids(adapter.conn, fold.message_ids)
        except Exception:
            LOGGER.warning(
                "[metabolism] chronicle-eligibility check failed; assuming the "
                "fold is compilable (persona=%s)",
                getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return True
        return bool(eligible)

    def _attach_chronicle_refs(self, persona, folds: List["FoldedRange"]) -> None:
        """畳んだ範囲を覆う一次あらすじ エントリの id / ch:N を刻む (全 fold を 1 照会で)。"""
        if not folds:
            return
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            return
        all_ids = [mid for f in folds for mid in f.message_ids]
        try:
            from sai_memory.arasuji.storage import get_entries_covering_messages
            entries = get_entries_covering_messages(adapter.conn, all_ids)
        except Exception:
            LOGGER.warning(
                "[metabolism] failed to resolve chronicle refs for folded ranges "
                "(persona=%s)", getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return
        # source_ids から「どのメッセージがどのエントリに入ったか」を引き直す
        # (1 つの fold が複数エントリに分かれることがある)。
        by_message: Dict[str, List[Any]] = {}
        for entry in entries:
            for mid in entry.source_ids:
                by_message.setdefault(str(mid), []).append(entry)
        for fold in folds:
            seen: Dict[str, Any] = {}
            for mid in fold.message_ids:
                for entry in by_message.get(mid, ()):
                    seen.setdefault(entry.id, entry)
            fold.chronicle_entry_ids = list(seen.keys())
            fold.chronicle_short_ids = [
                e.short_id for e in seen.values() if e.short_id is not None
            ]

    def run_metabolism(
        self,
        persona,
        building_id: str,
        window: "SessionWindow",
        watermarks: Watermarks,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        model_key: Optional[str] = None,
        chronicle_force: bool = False,
        stop_when_disabled: bool = False,
        cancellation_token: Optional[CancellationToken] = None,
        close_undersized_tail: bool = False,
    ) -> str:
        """Execute history metabolism: Chronicle generation + anchor update.

        Returns (2026-07-29、Codex 指摘「失敗の成功偽装」の根治):

        - "ok": 退場計画を適用した (編纂成功、または編纂を持たない設計)
        - "nothing": 畳むものが無かった (提示コンテキスト空 / 畳める範囲なし)
        - "failed" / "deferred": 編纂が完了せず anchor 据え置き (次回再試行)。
          deferred はユーザーキャンセル・確認拒否・別入口との claim 競合。
        - "deferred_sluice_unseen": 編纂・スルースとも成功したが、スルースが
          読めていない範囲 (末尾の新着とは限らない — 冷えた起点の前進で窓の
          頭側が漏れる並びもある) に退場計画が届いたため退場だけを次回へ
          譲った (anchor 据え置き。再実行すると続きから整理できる)。

        呼び出し元のうち自動発火 (maybe_run_metabolism) は戻り値を使わない
        (次回の水位判定が自然に再試行する)。手動入口 (run_manual_compaction)
        はこれをユーザーへの結果報告に使う — failed を「完了」と報告しない。

        ``model_key`` はこの Metabolism を発火させた Pulse の実行 model。退役
        (anchor 前進) は「渡された model の session_anchor 行」だけを進める
        (beat_execution_context.md §3.2 — 編纂は persona に一度、退役は model
        ごと)。None なら従来どおり ``persona.model``。

        ``chronicle_force`` は編纂の確認ダイアログ・pulse_type 判定を経ずに
        生成する (generate_chronicle force=True)。使うのは (a) 手動入口 —
        ボタン押下でユーザーが既に同意している、(b) §14 の非常畳み / 先回り
        畳み — Pulse の外から呼ぶため ``_current_pulse_type`` が残留値で
        あてにならず、確認ゲートの誤発火 (60 秒ブロック / 誤 disabled 化) を
        防ぐ必要がある。False (応答後の自動発火) では従来どおり
        generate_chronicle 側のゲートに従う。

        ``stop_when_disabled`` は手動入口専用の契約 (§13 裁定 4 補遺):
        ボタンの同意文は「Chronicle に畳む」なので、Chronicle 無効の persona
        では編纂なしの退場 (忘却) をその名目で実行せず "disabled" で止まる。
        False (自動・§14 経路) では従来どおり disabled でも前進する
        (編纂なしで忘れる設計合意)。

        ``close_undersized_tail`` は非常畳み (§14-3) 専用 — U 判定が材料字数に
        なった (2026-08-29 裁定) ため、「生は巨大だが材料が薄い」期間では通常の
        計画が fold を閉じられない。高水位超過の回復措置に限り、材料 U 未満の
        端数でも閉じて前進を保証する (plan_eviction の同名フラグへ渡すだけ)。

        Beat ロック (beat_execution_context.md §3.4): Metabolism は persona の
        記憶 (Chronicle / sluice の採取判断記録) に書くため、入口で
        beat_gate.hold(purpose="metabolism") を通す。Pulse 内 (run_meta_user
        経由) の呼び出しは同一スレッドの RLock 再入で無害 (関所も再実行され
        ない)。

        ``window`` は発火判定側が撮った提示提示コンテキストだが、**本体はロックの内側で撮り
        直す** — ロック外の値で圧縮区間を上書きすると、先行の別入口が書いた圧縮区間が消える。
        """
        from sea.beat_gate import hold_beat
        with hold_beat(
            self.manager,
            getattr(persona, "persona_id", None),
            purpose="metabolism",
        ):
            return self._run_metabolism_locked(
                persona, building_id, window, watermarks, event_callback,
                model_key=model_key, chronicle_force=chronicle_force,
                stop_when_disabled=stop_when_disabled,
                cancellation_token=cancellation_token,
                close_undersized_tail=close_undersized_tail,
            )

    def run_manual_compaction(
        self,
        persona,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        model_key: Optional[str] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> str:
        """手動入口 (記憶の整理 / Chronicle タブの生成) の畳み (arasuji_levels.md §13 裁定4)。

        「発火 (予算超過) を待たずに今すぐ畳む」ボタンの実体。範囲規則は自動
        (応答後 Metabolism) と同一 — 残す量 (watermarks.target) より古い側だけを
        畳む。起点の全消しも全量編纂もしない (それは修復スクリプトの領分)。

        ``cancellation_token`` は UI の中止ボタン用 — 編纂のチャンク間で確認され、
        中止時は "deferred" で戻る (確定済みチャンクは冪等スキップされるため
        再実行は安全)。

        Returns:
            - "ok": 畳んで適用した
            - "noop": 既に残す量以下、または畳める範囲が無い (何もしていない)
            - "failed": 編纂が失敗し anchor 据え置き (再実行で再試行できる)
            - "deferred": キャンセル、または別入口との claim 競合 (同上)
            - "deferred_sluice_unseen": スルースが読めていない範囲があり退場
              だけ見送った (採取と編纂は確定済み。再実行で続きから整理できる)
            - "disabled": Chronicle 生成が無効 (weave env OFF / persona トグル OFF)。
              手動入口の同意文は「Chronicle に畳む」なので、編纂なしの退場 (忘却)
              を黙って実行しない — 何も畳まず設定の案内に倒す (Codex 再レビュー
              2026-07-29)。自動 Metabolism の「disabled でも前進する」設計合意は
              対象外でそのまま
            - "unavailable": model / 水位 / 起点が解決できず、畳みを定義できない
              (ブートストラップ前の新規ペルソナ等)

        head の再構築 (2026-09-01): **手動入口は結果によらず head 再構築を
        保証する** — ボタンを押した以上、畳みが起きなくても設定トグル
        (Memopedia 索引の常時表示など) の変更がコンテキストへ反映されなければ
        ならない。"ok" だけは畳み本体 (:meth:`_run_metabolism_locked`) が
        発火済みなので、ここでは発火しない (二重 capture の回避)。発火責務を
        この関数が持つことで、呼び出し元 (あらすじタブの生成ジョブ) は何も
        しなくてよい。head が組み直されたかまで知りたい呼び出し元は
        :meth:`run_manual_compaction_checked` を使う。
        """
        status, _head_rebuilt = self.run_manual_compaction_checked(
            persona, event_callback, model_key, cancellation_token,
        )
        return status

    def run_manual_compaction_checked(
        self,
        persona,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        model_key: Optional[str] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> Tuple[str, bool]:
        """:meth:`run_manual_compaction` に head 再構築の成否を添えた形。

        Returns:
            ``(status, head_rebuilt)``。status は run_manual_compaction と同じ。
            ``head_rebuilt`` が False なら、畳み自体は status のとおりでも
            **設定変更のコンテキストへの反映は済んでいない** — 画面へ警告として
            出すのは呼び出し元 (ジョブ) の仕事 (2026-09-01 まはー裁定「無理に
            救済しなくていいが、失敗を知らせることは必要」)。

        判定は §15 読み戻し (:meth:`maybe_run_window_refill`) と同じ指紋比較:
        dispatch が成功しても weave が組み直された保証は無い (capture_all は
        section の capture 例外で既存オブジェクトを使い回す stale-but-real)
        ので、weave section の identity が変わったかで見る。weave が元々無い
        環境 (head 未初期化 / weave 無効) は残りようが無いので成功扱い。

        ``before`` は**判定本体より前**に撮る — status=="ok" の再構築は畳み
        本体 (_run_metabolism_locked) の中で起きるため、畳みの後に撮ると
        自分が撮った新品を「変わっていない」と誤判定する。

        **別処理が同じ窓で組み直した場合も成功と数える** (排他はしない、
        2026-09-01 裁定)。この検査が守るのは「設定がコンテキストへ届いたこと」
        であって「組み直したのが自分か」ではない。before と after の間に別の
        手動処理や自動 Metabolism が head を組み直したなら、その head は現在の
        設定で capture されているので、ユーザーの目的は誰の手でも満たされて
        いる。主体を見分けるためのロックは、目的が要求しない複雑化になる。
        """
        resolved_model = str(model_key or getattr(persona, "model", "") or "") or None
        head_model = self._resolve_head_model_key(persona, resolved_model)
        persona_id = getattr(persona, "persona_id", None)
        before_weave = self._head_weave_snapshot(persona_id, head_model)

        status = self._manual_compaction_status(
            persona, event_callback, resolved_model, cancellation_token,
        )
        if status != "ok":
            self._dispatch_manual_head_rebuild(persona, head_model)
        return status, self._head_was_rebuilt(persona_id, head_model, before_weave)

    def _resolve_head_model_key(
        self, persona, model_key: Optional[str],
    ) -> Optional[str]:
        """head の同定キー (persona, model) の model 側を確定する。

        ``on_metabolism(model_key=None)`` は head 側で persona の標準 model に
        フォールバックする。指紋を撮る側も同じ鍵を見ないと別の snapshot を
        比べてしまうので、フォールバックをここで先に済ませて、発火にも同じ値を
        渡す。
        """
        if model_key:
            return str(model_key)
        try:
            from sea.head_pipeline.integration import resolve_default_model_key
            return resolve_default_model_key(persona)
        except Exception:
            return str(getattr(persona, "model", "") or "") or None

    def _head_was_rebuilt(
        self, persona_id: Optional[str], model_key: Optional[str], before_weave: Any,
    ) -> bool:
        """head の weave section が撮り直されたか。

        三値 (:meth:`_head_weave_snapshot` 参照) の扱い:

        - どちらかが :data:`_WEAVE_INSPECT_FAILED` → **False** (検証できて
          いない)。知らせるための機構なので、分からないときは黙らず警告側へ
          倒す
        - before が ``None`` (weave が正当に無い環境) → True。残りようが
          ないので失敗ではない
        - それ以外 → identity が変わったか
        """
        if before_weave is _WEAVE_INSPECT_FAILED:
            return False
        if before_weave is None:
            return True
        after_weave = self._head_weave_snapshot(persona_id, model_key)
        if after_weave is _WEAVE_INSPECT_FAILED:
            return False
        return after_weave is not before_weave

    def _dispatch_manual_head_rebuild(self, persona, model_key: Optional[str]) -> None:
        """手動入口 (ボタン押下) の出口で head を再構築する。

        畳み・補修が実際には何もしなかった場合 ("noop" / "disabled" /
        "failed" 等) でも、ユーザーが押した以上は設定変更がコンテキストへ
        反映されなければならない。畳みが成立した経路は
        :meth:`_run_metabolism_locked` が既に発火しているので、呼び出し側が
        重複しない条件で呼ぶ。

        失敗しても手動操作そのものの成否には畳み込まない (head は次の節目で
        再構築される)。
        """
        try:
            from saiverse.dynamic_state import DynamicStateManager
            DynamicStateManager.on_metabolism(
                persona, self.manager, model_key=model_key,
            )
        except Exception:
            LOGGER.exception("[dynamic_state] manual head rebuild failed")

    def _manual_compaction_status(
        self,
        persona,
        event_callback: Optional[Callable[[Dict[str, Any]], None]],
        resolved_model: Optional[str],
        cancellation_token: Optional[CancellationToken],
    ) -> str:
        """:meth:`run_manual_compaction` の判定本体 — 畳みの結果だけを返す。

        head 再構築は呼び出し元 (run_manual_compaction) が単一の出口で持つ。
        早期 return が多いので、発火をここに書くと必ずどれかで書き漏らす。
        """
        if not resolved_model:
            return "unavailable"
        # 門はペルソナ設定 (AI.CHRONICLE_ENABLED) だけ。かつては env
        # ENABLE_MEMORY_WEAVE_CONTEXT との二段だったが、.env.example が false 出荷で、
        # v0.2 からのアップグレード組の .env には行自体が無く (= false 扱い)、
        # 記憶の整理が全経路で止まる実害を出した (2026-09-01 撤去裁定)。
        if not self.is_chronicle_enabled_for_persona(persona):
            return "disabled"
        watermarks = self.get_metabolism_watermarks(persona, resolved_model)
        if watermarks is None:
            return "unavailable"
        window = self.get_presented_window(persona, resolved_model)
        if not window.anchor_id:
            # 起点行が無い = 提示ウィンドウが未定義 (新規ペルソナ / 修復直後)。
            # 畳む対象を決められないので何もしない。
            return "unavailable"
        # 門の物差しは**会話の行だけ** vs 残す量 (2026-09-03 まはー裁定: 残す量の
        # 主語は会話の行。issue protection_quota_consumed_by_perception_blocks.md)。
        # 2026-09-02 に一度「合計」へ揃えたが、それは残す量が保護範囲でもある
        # ことを見落としていた — 走行側 (plan_eviction) の保護範囲も行だけで
        # 測るので、行が残す量以下の窓は門を通しても計画が空で "nothing" に
        # 終わる。門も同じ主語で測るのが「門と本走行が別の答えを出さない」。
        # 合計が上限を超えている (知覚の供給が予算超過) なら、その事実を 1 度
        # だけ警告して引き返す (LLM は呼ばない)。
        rows_chars = message_chars(window.presented)
        if rows_chars <= watermarks.target:
            total_chars = self.presented_chars(
                persona, window.presented, window.anchor_id,
            )
            if not self._note_perception_over_budget(
                persona, rows_chars, total_chars, watermarks,
            ):
                LOGGER.info(
                    "[metabolism] manual compaction: window already at/below "
                    "target (persona=%s, %d row chars <= target=%d, %d chars "
                    "sent); nothing to fold",
                    getattr(persona, "persona_id", "?"), rows_chars,
                    watermarks.target, total_chars,
                )
            return "noop"
        building_id = getattr(persona, "current_building_id", None) or ""
        status = self.run_metabolism(
            persona, building_id, window, watermarks, event_callback,
            model_key=resolved_model, chronicle_force=True,
            stop_when_disabled=True,
            cancellation_token=cancellation_token,
        )
        return "noop" if status == "nothing" else status

    def run_coverage_repair(
        self,
        persona,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> Tuple[str, int]:
        """被覆補修の手動入口 — 実体は :meth:`_coverage_repair_status`。

        run_manual_compaction と同じく、**結果によらず出口で head を再構築
        する** (設定トグルの反映を保証する。2026-09-01)。補修の本体は
        on_metabolism を一切発火しないので条件は付けない。

        末尾の引き戻し (:func:`sea.coverage_repair.run_tail_rewind`) が窓の
        畳みへ降りた場合だけ、その中の run_manual_compaction が先に 1 回
        発火する。それでも出口の発火は重複ではない — 引き戻しの後に
        ``mark_covered_cold_windows`` が §15 の印を書くので、途中の capture は
        最終状態を写していない。

        head が組み直されたかまで知りたい呼び出し元は
        :meth:`run_coverage_repair_checked` を使う。
        """
        status, mark_failures, _head_rebuilt = self.run_coverage_repair_checked(
            persona, event_callback, cancellation_token,
        )
        return (status, mark_failures)

    def run_coverage_repair_checked(
        self,
        persona,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> Tuple[str, int, bool]:
        """:meth:`run_coverage_repair` に head 再構築の成否を添えた形。

        Returns:
            ``(status, 印を書けなかった行の数, head_rebuilt)``。判定の規約は
            :meth:`run_manual_compaction_checked` と同じ (weave section の
            identity 比較。before は補修本体より前に撮る — 引き戻しが内側で
            走らせる畳みの再構築も拾うため)。
        """
        head_model = self._resolve_head_model_key(persona, None)
        persona_id = getattr(persona, "persona_id", None)
        before_weave = self._head_weave_snapshot(persona_id, head_model)

        status, mark_failures = self._coverage_repair_status(
            persona, event_callback, cancellation_token,
        )
        self._dispatch_manual_head_rebuild(persona, head_model)
        return (
            status, mark_failures,
            self._head_was_rebuilt(persona_id, head_model, before_weave),
        )

    def _coverage_repair_status(
        self,
        persona,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> Tuple[str, int]:
        """被覆補修 (arasuji_levels.md §16-2 機構5) — 手動入口の実体。

        「編纂対象なのに、どの一次あらすじの source でもなく、止め線 (温かい
        anchor の最古位置) より古い」領域を、通常の編纂パイプライン
        (:meth:`generate_chronicle` の全量計画 — W4 の plan_alignment) で
        一次あらすじにする。止め線の絞りは generate_chronicle 側が行う
        (見積もり API と同じ関数)。

        補修は**提示から何も追い出さない** (被覆を足すだけ) — 退場も anchor
        前進も伴わないので、退場の関所 (スルース) の対象外 (§16-2)。編纂済みは
        processed_ids (source_ids 照会) で自動で飛ぶため冪等。

        完了時、冷えた anchor 行の窓を覆うエントリへ §15 の印
        (:func:`sea.coverage_repair.mark_covered_cold_windows`) を書く —
        休眠モデルが目覚めても head のあらすじ枠と二重提示にならない。
        印の失敗は補修の成否 (status) に畳み込まない (エントリは確定済みで、
        印は次回の補修が冪等に追記し直せる) が、**数として返して可視化する**
        (Codex レビュー 2026-08-31 — 黙って落とすと次回補修まで head との
        二重提示が続くことをユーザーが知れない)。

        Returns:
            ``(status, 印を書けなかった行の数)``。status は generate_chronicle
            の status ("ok" / "failed" / "deferred")、または "disabled"
            (weave env OFF / persona トグル OFF — 手動入口の同意文は
            「あらすじにする」なので、無効の persona では何もしない)。
            編纂が "ok" でも**末尾の引き戻しが完了しなかった場合は "failed"**
            (下の白名簿を参照 — 編纂済みエントリは確定しているので、意味は
            「全部は終わらなかった」)。
            印の失敗数は status=="ok" のときだけ意味を持つ。行単位の失敗は
            正確な数、印の工程ごと例外で落ちた場合は -1 (数不明の全滅)。
        """
        # 門はペルソナ設定だけ (env ENABLE_MEMORY_WEAVE_CONTEXT は 2026-09-01 撤去)。
        if not self.is_chronicle_enabled_for_persona(persona):
            return ("disabled", 0)
        persona_id = getattr(persona, "persona_id", None)
        from sea.beat_gate import hold_beat
        # Beat ロックで Metabolism / 手動整理と直列化する。補修は Beat (認知の
        # 一巡) ではなく保守書き込みなので、関所 (pending flush) は通さず
        # ロックだけ取る (remove_folds_referencing_entry と同じ型)。
        mark_failures = 0
        with hold_beat(
            self.manager, persona_id, purpose="coverage_repair",
            check_gate=False,
        ):
            status = self.generate_chronicle(
                persona, event_callback,
                force=True,
                cancellation_token=cancellation_token,
            )
            if status == "ok":
                # 末尾の未被覆 run (後ろに隣人あらすじが居ない極小 run 群) は
                # 編纂せず anchor を引き戻して提示窓へ戻す (arasuji_tiny_run_
                # absorption 裁定 5 改訂 — LLM ゼロ。窓が上限を超えたら同関数が
                # このジョブ内で即座に畳む)。
                #
                # 写像は**成功系の白名簿**で行う (Codex 十巡 N2)。成功と呼べる
                # のは「やるべきことが全部終わった」か「意図した見送り」だけ:
                #   none            — 帯が無かった (解決は成功している)
                #   rewound         — 引き戻し完了 (畳みは不要だった)
                #   rewound_folded  — 引き戻し + 畳みまで完了
                #   skipped         — 行なし / 既に窓の中 / 位置不明 (見送り)
                #   cas_rejected    — anchor が動いた (次回が再計画する見送り)
                # それ以外は失敗側へ倒す — "failed" (帯の解決・計画の読み取り
                # 失敗) と "rewound_fold_failed" (引き戻したが窓が上限超過の
                # まま) に加え、**未知の状態語も**失敗に写像する (状態語が
                # 増えたときに黙って成功の顔で通る穴を作らない)。
                # 編纂済みエントリは確定しているので、失敗表示の意味は「全部は
                # 終わらなかった」の正直な申告 (裁定 6 — 再実行が必要なことが
                # 誰にも分からない状態を作らない)。
                _rewind_ok = ("none", "rewound", "rewound_folded",
                              "skipped", "cas_rejected")
                _rewind_failed = False
                try:
                    from sea.coverage_repair import run_tail_rewind
                    rewind_status = run_tail_rewind(
                        self, persona,
                        event_callback=event_callback,
                        cancellation_token=cancellation_token,
                    )
                    if rewind_status not in _rewind_ok:
                        _rewind_failed = True
                    elif rewind_status in ("skipped", "cas_rejected"):
                        LOGGER.info(
                            "[coverage-repair] tail rewind status=%s "
                            "(persona=%s)", rewind_status, persona_id,
                        )
                except Exception:
                    LOGGER.warning(
                        "[coverage-repair] tail rewind raised (persona=%s)",
                        persona_id, exc_info=True,
                    )
                    _rewind_failed = True
                try:
                    from sea.coverage_repair import mark_covered_cold_windows
                    _, mark_failures = mark_covered_cold_windows(self, persona)
                except Exception:
                    LOGGER.warning(
                        "[coverage-repair] cold-window marking failed "
                        "(persona=%s); entries are committed and the next "
                        "repair run re-marks idempotently",
                        persona_id, exc_info=True,
                    )
                    mark_failures = -1  # 数不明の全滅
                if _rewind_failed:
                    # 引き戻しが完了しなかった — 帯の解決/計画が読めなかった
                    # (M2) か、引き戻し後の畳みが未完 (N2)。印の再適用は上で
                    # 済ませてある — 失敗として返し、ジョブ UI が再実行を促す。
                    # 畳み未完の場合、再実行は畳みを再試行しない (帯はもう窓の
                    # 中で未被覆ではない) — 窓は次の会話の非常畳み (§14-3) が
                    # 回復する。失敗表示は「全部は終わらなかった」の申告。
                    status = "failed"
        return (status, mark_failures)

    # ------------------------------------------------------------------
    # 冷えたウィンドウの保守 (arasuji_levels.md §14)
    # ------------------------------------------------------------------

    def maybe_run_emergency_precompaction(
        self,
        persona,
        building_id: str,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        model_key: Optional[str] = None,
    ) -> str:
        """会話応答より前の非常畳み (arasuji_levels.md §14-3)。

        原因を問わず「話しかけた時点で提示ウィンドウが高水位を既に超過している」
        イレギュラーに対する回復措置。応答より先に通常の範囲規則 (残す量より
        古い側) で畳み、model のコンテキスト上限超過による呼び出し失敗 (§12-10
        極端形) の連鎖を断つ。機構1 (anchor 前進)・機構3 (先回り畳み) が働いて
        いれば通常は発火しない。

        ユーザーへは status イベントで**通知**する — 同意ダイアログではない
        (回復措置に畳まない選択肢は無い。まはー裁定 2026-07-29)。Chronicle
        無効の persona でも前進で救う (stop_when_disabled=False)。

        合計が上限を超えていても会話の行が残す量以下なら、畳めるものが無い
        (超過の主は知覚の供給)。その回は通知も session_anchor 行の立ち上げも
        run_metabolism もせず "skip" を返す (警告はペルソナごと 1 度、
        :meth:`_note_perception_over_budget`)。

        Returns:
            "skip" (条件外・超過なし・知覚の供給だけが予算超過) / run_metabolism の結果
            ("ok"/"nothing"/"failed"/"deferred"/"deferred_sluice_unseen")。
        """
        model_key = str(model_key or getattr(persona, "model", "") or "") or None
        if not model_key:
            return "skip"
        watermarks = self.get_metabolism_watermarks(persona, model_key)
        if watermarks is None or watermarks.high is None:
            return "skip"
        # 機構1 (§14-2) を先に適用した位置で測る — anchor 前進 (無料) で救える
        # ケースに編纂 (有料) を撃たない。
        anchor_id, resolution = self.resolve_metabolism_anchor(
            persona, model_key=model_key,
        )
        if not anchor_id:
            return "skip"  # ブートストラップ前 — 提示ウィンドウが未定義
        window = self.get_presented_window(persona, model_key, anchor_id)
        # 非常畳みの目的は「巨大なコンテキストを送らない」ことそのものなので、
        # 判定は必ず**実際に送る中身**で測る (2026-09-02 まはー裁定)。保存行だけ
        # で測ると、知覚ブロックを足した実送信が model の上限を突き抜けていても
        # 「上限以下」と読んで応答へ進み、この機構の存在意義が壊れる。
        current_chars = self.presented_chars(persona, window.presented, anchor_id)
        if current_chars <= watermarks.high:
            return "skip"
        # 合計は上限超えでも、会話の行が残す量以下なら退場計画は保護範囲で
        # 埋まって空になる (残す量の主語は会話の行、2026-09-03 裁定)。超過の主は
        # 知覚の供給であり、ここで通知を出し・行を立て・本体へ進んでも毎ターン
        # 「整理しています」だけ出て何も畳めない (Codex 指摘 2026-09-03)。1 度
        # だけ警告して、通知・行の立ち上げ・run_metabolism のどれもせず引き返す。
        rows_chars = message_chars(window.presented)
        if self._note_perception_over_budget(
            persona, rows_chars, current_chars, watermarks,
        ):
            return "skip"
        persona_id = getattr(persona, "persona_id", None)
        LOGGER.warning(
            "[metabolism] emergency pre-compaction: window over high watermark "
            "at conversation start (persona=%s model=%s %d chars > high=%d, "
            "resolution=%s)",
            persona_id, model_key, current_chars, watermarks.high, resolution,
        )
        if event_callback:
            try:
                event_callback({
                    "type": "status",
                    "content": "現在のコンテキストが長すぎるため、記憶の整理を行っています…",
                })
            except Exception:
                LOGGER.debug(
                    "[metabolism] emergency notice emit failed", exc_info=True,
                )
        # 自行がまだ無い model (最前線 / 借用から始まる初回) は、畳みの適用先と
        # なる session_anchor 行を先に立てる — 本体はロック内で行の anchor から
        # 提示ウィンドウを撮り直すため、行が無いと空振りする。温度は書かない。
        if persona_id and resolution in ("frontier", "other"):
            # 厳格に読む — 読み失敗を「行なし」と見て upsert すると既存の行を
            # 上書きで消す (七巡目の掃討)。例外は呼び出し側が記録して見送る。
            entry = self.load_anchor_entry_strict(persona_id, model_key)
            if not entry or not entry.get("anchor_id"):
                self.upsert_anchor_entry(persona_id, model_key, {
                    "anchor_id": anchor_id,
                    # 「十分に過去」= 確実に冷えている温度で立てる
                    "updated_at": (datetime.now() - timedelta(days=3650)).isoformat(),
                })
        # close_undersized_tail: U 判定が材料字数になった (2026-08-29 裁定) ため、
        # 「生は巨大だが材料が薄い」期間では通常計画が fold を閉じられず、高水位
        # 超過が続く。回復措置であるここに限り、材料 U 未満の端数でも閉じて前進を
        # 保証する (小粒のあらすじは最後の手段としてのみ許す)。
        return self.run_metabolism(
            persona, building_id, window, watermarks, event_callback,
            model_key=model_key, chronicle_force=True,
            close_undersized_tail=True,
        )

    # ------------------------------------------------------------------
    # 読み戻し (arasuji_levels.md §15)
    # ------------------------------------------------------------------

    def maybe_run_window_refill(
        self,
        persona,
        building_id: str,
        model_key: Optional[str] = None,
    ) -> str:
        """会話応答より前の読み戻し (arasuji_levels.md §15) — 非常畳みの対称。

        話しかけられた時点で提示ウィンドウが残す量 (watermarks.target) を
        下回っていたら、応答より先に畳んだところを開き直して残す量まで充填する。
        水位引き上げ後の既存ペルソナと、旧水位でほぼ全編纂済みのままアップデート
        したペルソナ (生ログほぼ無しで新バージョンの会話が始まる) の救済経路。

        帳簿の付け替えだけで LLM は呼ばない。ユーザーへの通知もしない —
        §14-3 の注意書きの対象は「削る」側で、開き直しに失うものは無い。

        Returns:
            "skip" (条件外・不足なし・開ける区間なし・競合で見送り) /
            "ok" (開き直した)
        """
        model_key = str(model_key or getattr(persona, "model", "") or "") or None
        if not model_key:
            return "skip"
        # 水位ゲートは resolve より**前** — 水位が定義できない model では
        # 読み戻しの適用可否を判定できないので、resolve の副作用 (§14-2 前進の
        # 永続化) も起こさず引き返す (Codex 指摘 2026-07-30: 切り出しで順序が
        # 逆転し、no-op のはずの経路が anchor を書いていた)。
        watermarks = self.get_metabolism_watermarks(persona, model_key)
        if watermarks is None:
            return "skip"
        # 機構1 (§14-2) を先に適用した位置から測る — 冷えた行はまず最前線へ
        # 正規化し、そこから残す量まで引き戻す (前進と読み戻しの主導権を
        # 混ぜない)。
        # 読み戻しは行 (起点 + 圧縮区間) を**書く**経路なので、起点の解決と器の
        # 読みは厳格 (七巡目の掃討): 縮退した読み (行の読み失敗 → 行なし、壊れた
        # 記録 → 空) の上に書くと、既存の区間や起点を消す。例外は呼び出し側
        # (run_meta_user) が記録して床へ進み、床が "unmet" を裁く。器が absent
        # (従来のメモリ上の履歴) の挙動は従来のまま。
        anchor_id, resolution = self.resolve_metabolism_anchor(
            persona, model_key=model_key, strict=True,
        )
        if not anchor_id:
            return "skip"  # ブートストラップ前 — 提示ウィンドウが未定義
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return "skip"

        plan = self._plan_window_refill(
            persona, model_key, anchor_id, watermarks, strict=True,
        )
        if plan is None:
            return "skip"

        # 自行がまだ無い model (最前線 / 借用から始まる初回) は書き込み先の
        # 行を先に立てる (§14-3 と同じ)。温度は「確実に冷えている」で立て、
        # 温度の主張は下の _write_refill に一本化する。読みは厳格 (上と同じ理由)。
        if resolution in ("frontier", "other"):
            entry = self.load_anchor_entry_strict(persona_id, model_key)
            if not entry or not entry.get("anchor_id"):
                self.upsert_anchor_entry(persona_id, model_key, {
                    "anchor_id": anchor_id,
                    "updated_at": (datetime.now() - timedelta(days=3650)).isoformat(),
                })

        if not self._write_refill(
            persona_id, model_key, anchor_id, plan["new_anchor_id"], plan["folds"],
        ):
            return "skip"

        self._recapture_head_after_refill(persona, persona_id, model_key, "refill")

        LOGGER.info(
            "[metabolism] window refill (persona=%s model=%s): rows %d -> %d "
            "chars toward target=%d, total %d chars (verified against high=%s; "
            "straddling %d, reopened %d in-window range(s), rewound %d "
            "message(s) across %d range(s), dropped %d rung(s), resolution=%s)",
            persona_id, model_key, plan["current_chars"], plan["final_chars"],
            plan["target"], plan["final_total_chars"], plan["high"],
            plan["straddled"], plan["reopened"], plan["rewound_messages"],
            plan["rewound_folds"], plan["dropped_steps"], resolution,
        )
        return "ok"

    def _recapture_head_after_refill(
        self, persona, persona_id: str, model_key: str, reason: str,
    ) -> bool:
        """読み戻し / 最終防衛ラインの書き込み後の head 再 capture。

        退場の step 4 と同じ節目扱い。head のあらすじ枠の除外名簿は capture
        済み snapshot に凍っているため、ここで再 capture しないと「生に開いた
        範囲のあらすじが head に残ったまま」の二重提示が本番でも起こる (Codex
        指摘 2026-07-30 — head は節目キャッシュ)。失敗は 1 回だけ即時再試行し、
        それでも駄目なら明示 WARNING で進む — 帳簿 (窓) は正しく、head の
        一時的な重複表示のために成功するはずの応答を潰さない (§14-3 fail-open
        と同じ裁定。残余は intent §15-4)。

        Returns:
            weave が組み直されたと確認できたら True。
        """
        head_refreshed = False
        for _attempt in range(2):
            before_weave = self._head_weave_snapshot(persona_id, model_key)
            dispatched = False
            try:
                from saiverse.dynamic_state import DynamicStateManager
                dispatched = DynamicStateManager.on_metabolism(
                    persona, self.manager, model_key=model_key,
                )
            except Exception:
                LOGGER.exception(
                    "[dynamic_state] on_metabolism failed after %s", reason,
                )
            if not dispatched:
                continue
            # dispatch 成功でも weave が作り直された保証は無い — capture_all は
            # section の capture 例外で**既存オブジェクトを使い回す**
            # (stale-but-real)。使い回しなら identity が変わらないので、旧
            # 除外名簿の weave が残ったこと (= 二重提示の継続) を検出できる
            # (Codex 指摘 2026-07-30)。weave が元々無い環境 (head 未初期化 /
            # weave 無効) は残りようが無いので成功扱い。
            after_weave = self._head_weave_snapshot(persona_id, model_key)
            if before_weave is None or after_weave is not before_weave:
                head_refreshed = True
                break
        if not head_refreshed:
            LOGGER.warning(
                "[metabolism] head re-capture failed after %s (persona=%s "
                "model=%s); the head chronicle frame may keep showing entries "
                "for reopened ranges until the next capture",
                reason, persona_id, model_key,
            )
        return head_refreshed

    # ------------------------------------------------------------------
    # 最終防衛ライン (docs/issues/window_floor_and_refill_redesign.md 設計 0)
    # ------------------------------------------------------------------

    def window_floor_applied_at(
        self, persona_id: Optional[str], model_key: Optional[str],
    ) -> Optional[str]:
        """最終防衛ラインが (persona, model) で最後に発火した時刻 (ISO)。無ければ None。"""
        if not persona_id or not model_key:
            return None
        return self._window_floor_applied_at.get((str(persona_id), str(model_key)))

    def ensure_window_floor(
        self,
        persona,
        building_id: str,
        model_key: Optional[str] = None,
    ) -> str:
        """発話の直前に、窓の会話の行が残す量を下回っていたら生のまま読み足す。

        不変条件 (docs/issues/window_floor_and_refill_redesign.md): **ペルソナは、
        窓の会話が残す量を下回った状態で発話しない — 埋める材料 (起点より古い
        会話) があるかぎり。** 非常畳み → 読み戻し (§15) の直後、全 pulse_type
        で :meth:`~sea.runtime.SEARuntime.run_meta_user` が呼ぶ。読み戻しが
        (段の壊れ・覆うあらすじの欠け・その他どんな理由でも) 埋め切れなかった
        ときに、あらすじの段に関係なく起点より古い会話を不足分だけ生で読み足す。
        ここが発火する = 上流 (読み戻し) の失敗の印なので WARNING を出し、
        context-status の ``window_floor_applied_at`` に時刻を残す。

        手順 (:meth:`_apply_window_floor_once`):

        1. 水位が無ければ skip。起点は :meth:`resolve_metabolism_anchor` で
           通常どおり解決し (§14-2 の前進を済ませた位置)、窓を撮る。
        2. 窓の**保存行**の字数 (:func:`~sea.eviction_plan.stored_message_chars`、
           知覚は数えない) が残す量以上なら skip。
        3. 起点をまたぐ圧縮区間があれば、まずその最古の行まで丸ごと読み足す。
           まだ足りなければ ``get_history_before_anchor`` で不足分だけ古い方へ
           読む。材料が無ければ skip。
        4. 新しい起点 = 読み足した最古の行。読み足した範囲を覆う一次あらすじが
           あれば、その範囲を ``presented_raw=True`` の圧縮区間として記録する
           (head の除外名簿に載せて二重提示を防ぐ)。覆うものが無い範囲は記録
           なし (生のまま)。窓に既にある圧縮区間で読み足した範囲にかかるものは
           ``presented_raw=True`` にする。
        5. 書き込みは読み戻しと同じ CAS (:meth:`_write_refill`)。head の
           再 capture も読み戻しと同じ。

        書けなかった (CAS 不一致 / DB 失敗) ときは、新しい起点から**一度だけ**
        計画し直す — 別入口が起点を動かした直後なら、その窓は既に足りている
        かもしれない。二度目も書けなければ "unmet"。

        Returns:
            "skip" (条件外・不足なし・材料なし・SAIMemory absent = 従来のメモリ上
            の履歴 — 正常) / "ok" (読み足した) /
            "unmet" (例外・書き込み失敗で不変条件を満たせなかった。履歴の読み
            失敗も含む — 読みは厳格モードで、読めなかったことを「材料なし」に
            潰さない)。呼び出し側 (run_meta_user) は "unmet" で
            :class:`~sea.runtime_context.WindowFloorUnmetError` を送出して発話を
            見送る — 不変条件が発話より優先 (Codex 一巡目 #1 / 二巡目 #1)。
        """
        model_key = str(model_key or getattr(persona, "model", "") or "") or None
        if not model_key:
            return "skip"
        persona_id = getattr(persona, "persona_id", None)
        try:
            watermarks = self.get_metabolism_watermarks(persona, model_key)
            if watermarks is None:
                return "skip"
            for _attempt in range(2):
                status = self._apply_window_floor_once(persona, model_key, watermarks)
                if status != "write_failed":
                    return status
            LOGGER.error(
                "[metabolism] window floor could not be written twice (persona=%s "
                "model=%s); the floor invariant is unmet", persona_id, model_key,
            )
            return "unmet"
        except Exception:
            LOGGER.exception(
                "[metabolism] window floor failed (persona=%s model=%s); the "
                "floor invariant is unmet", persona_id, model_key,
            )
            return "unmet"

    def _apply_window_floor_once(
        self, persona, model_key: str, watermarks: Watermarks,
    ) -> str:
        """最終防衛ラインの計画と書き込み 1 回分 ("skip" / "ok" / "write_failed")。"""
        # 床の保証は**永続の器 (SAIMemory)** に対して定義する。器が absent
        # (adapter なし / 設定で無効 = 従来のメモリ上の履歴) なら床は見送る —
        # メモリ上の写しは line_role / scope の絞りを持たず行の勘定が合わない
        # ので、写しに対して保証を作らない (Codex 六巡目 #3)。読み戻し・非常
        # 畳みの absent 時の挙動は従来のまま。
        from persona.history_manager import memory_store_state
        if memory_store_state(getattr(persona, "sai_memory", None)) == "absent":
            pid = str(getattr(persona, "persona_id", "?"))
            if pid not in self._floor_absent_logged:
                self._floor_absent_logged.add(pid)
                LOGGER.info(
                    "[metabolism] window floor disabled: SAIMemory absent "
                    "(legacy in-memory history) (persona=%s)", pid,
                )
            return "skip"
        # 起点の解決も厳格モード — 行の読み失敗を「起点なし = skip」に潰さない
        # (例外は ensure_window_floor が "unmet" に写す。Codex 三巡目 #2)。
        anchor_id, resolution = self.resolve_metabolism_anchor(
            persona, model_key=model_key, strict=True,
        )
        if not anchor_id:
            return "skip"  # ブートストラップ前 — 提示ウィンドウが未定義
        persona_id = getattr(persona, "persona_id", None)
        history_mgr = getattr(persona, "history_manager", None)
        if not persona_id or history_mgr is None:
            # 器は在る (absent なら上で skip 済み) のに履歴の読み手や persona_id
            # が無い = 組み立ての欠陥。skip すると器を検証しないまま喋る
            # (Codex 七巡目 #2)。
            raise RuntimeError(
                "window floor cannot run: the memory store is present but the "
                f"persona has no history manager or id (persona={persona_id!r})"
            )

        # 窓の読みも厳格モード — 履歴の未準備 / 読み失敗・起点の不在・圧縮区間の
        # 記録の読み失敗は例外 (ensure_window_floor が "unmet" に写す)。縮退した
        # 窓で測ると、メモリ上の写しが厚ければ skip し、区間が読めなければ空の
        # 記録で上書きして既存の区間を消す (Codex 四巡目 #1 / #3)。
        window = self.get_presented_window(
            persona, model_key, anchor_id, strict=True,
        )
        rows_chars = stored_message_chars(window.presented)
        if rows_chars >= watermarks.target:
            return "skip"
        folds = list(window.folds)

        # 起点をまたぐ圧縮区間 (読み戻しの前段と同じ規則) は、まずその最古の行
        # まで**丸ごと**読み足す。不足分だけ読むと区間の左端に届かないことが
        # あり、区間が新しい起点をまたいだまま digest 提示に倒れて (apply_folds
        # は部分生存の印を尊重しない) 行が残す量に届かない。最終防衛ラインは
        # 読み戻しがまたぎを処理したことに依存しない。
        raw_ids = set(window.raw_ids)
        straddling = [
            f for f in folds if any(mid not in raw_ids for mid in f.message_ids)
        ]
        # 読みは厳格モード — DB の読み失敗を「古い会話が無い (skip)」と読むと、
        # 材料があるのに残す量を割ったまま喋る。例外は ensure_window_floor が
        # "unmet" に写す (Codex 二巡目 #1)。
        pre_before: List[Dict[str, Any]] = []
        if straddling:
            pre_before = self._history_back_to_folds(
                history_mgr, straddling, anchor_id, raw_ids, persona_id,
                raise_on_error=True,
            )
        pre_ids = {str(m.get("id")) for m in pre_before}
        for fold in folds:
            if any(mid in pre_ids for mid in fold.message_ids):
                fold.presented_raw = True
        base_anchor_id = str(pre_before[0].get("id")) if pre_before else anchor_id
        rows_after_pre = (
            stored_message_chars(
                self._present_with_folds(
                    persona, list(pre_before) + list(window.raw), folds,
                )
            )
            if pre_before else rows_chars
        )

        # まだ足りなければ、またぐ区間の先頭 (無ければ起点) からさらに古い方へ
        # 不足分だけ生で読み足す。
        older: List[Dict[str, Any]] = []
        remaining = watermarks.target - rows_after_pre
        if remaining > 0:
            older = history_mgr.get_history_before_anchor(
                base_anchor_id,
                max_chars=remaining,
                required_line_roles=["main_line"],
                required_scopes=["committed"],
                raise_on_error=True,
            )
        before = list(older) + list(pre_before)
        if not before:
            return "skip"  # 起点より古い会話が本当に無い — 埋める材料が無い
        before_ids = [str(m.get("id")) for m in before]
        new_anchor_id = before_ids[0]

        # 既存の圧縮区間で読み足した範囲にかかるものは生で見せる。範囲全体が
        # 窓に入るので apply_folds が印を尊重する。
        before_set = set(before_ids)
        for fold in folds:
            if any(mid in before_set for mid in fold.message_ids):
                fold.presented_raw = True
        new_folds = self._floor_coverage_folds(persona, before, window, folds)

        # 自行がまだ無い model は書き込み先の行を先に立てる (読み戻しと同じ)。
        if resolution in ("frontier", "other"):
            # 厳格に読む — 読み失敗を「行なし」と見て upsert すると、既存の行の
            # 起点と圧縮区間を上書きで消す (七巡目の掃討)。
            entry = self.load_anchor_entry_strict(persona_id, model_key)
            if not entry or not entry.get("anchor_id"):
                self.upsert_anchor_entry(persona_id, model_key, {
                    "anchor_id": anchor_id,
                    "updated_at": (datetime.now() - timedelta(days=3650)).isoformat(),
                })
        if not self._write_refill(
            persona_id, model_key, anchor_id, new_anchor_id, new_folds + folds,
        ):
            LOGGER.warning(
                "[metabolism] window floor write did not land (CAS mismatch or "
                "DB failure); re-planning from the fresh anchor (persona=%s "
                "model=%s expected=%s)", persona_id, model_key, anchor_id,
            )
            return "write_failed"
        self._recapture_head_after_refill(
            persona, persona_id, model_key, "window floor",
        )
        applied_at = datetime.now().replace(microsecond=0).isoformat()
        self._window_floor_applied_at[(str(persona_id), model_key)] = applied_at
        LOGGER.warning(
            "[metabolism] window floor applied (persona=%s model=%s): rows %d "
            "chars < target=%d; read %d message(s) (%d chars) back to %s raw, "
            "recorded %d covering range(s) as presented_raw (resolution=%s). "
            "The refill upstream failed to keep the floor",
            persona_id, model_key, rows_chars, watermarks.target, len(before),
            stored_message_chars(before), new_anchor_id, len(new_folds),
            resolution,
        )
        return "ok"

    def _floor_coverage_folds(
        self, persona, before: List[Dict[str, Any]], window: "SessionWindow",
        existing_folds: List["FoldedRange"],
    ) -> List["FoldedRange"]:
        """読み足した範囲を覆う一次あらすじを ``presented_raw`` の圧縮区間にする。

        印にするのは **source が全部 (読み足した範囲 ∪ 窓) に収まるエントリ
        だけ** (§16-2 の冷えた窓への印と同じ規律) — 新しい起点をまたぐ
        エントリに印を書くと、apply_folds が部分生存の区間を digest 提示に
        倒し、生で読み足したはずの行が縮む。跨ぐエントリは見送り、head との
        部分的な二重提示を残余として受容する。既存の圧縮区間が持つエントリと、
        既存区間の行に触れるエントリも扱わない (同じ行が二つの区間に属すると
        印戻し後に digest が二重になる)。source を共有する・位置が重なる
        エントリは一枚の区間に束ねる (plan_rewind と同じ理由)。
        """
        from sea.session_window import FoldedRange

        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            return []
        before_ids = [str(m.get("id")) for m in before]
        # 照会の失敗は握らない — 床は厳格経路で、失敗は書く前に "unmet" で止める
        # (空の記録で進むと、覆うあらすじが head に残ったまま生の行と二重になる。
        # Codex 八巡目 #2)。
        from sai_memory.arasuji.storage import get_entries_covering_messages
        entries = get_entries_covering_messages(adapter.conn, before_ids)
        if not entries:
            return []
        ordered = list(before) + list(window.raw)
        pos = {str(m.get("id")): i for i, m in enumerate(ordered)}
        claimed_entries = {
            str(eid) for f in existing_folds for eid in f.chronicle_entry_ids
        }
        claimed_messages = {str(mid) for f in existing_folds for mid in f.message_ids}
        spans: List[Tuple[int, int, List[Any]]] = []
        for entry in entries:
            if str(entry.id) in claimed_entries:
                continue
            sources = {str(s) for s in entry.source_ids}
            if any(s not in pos for s in sources):
                continue  # 新しい起点をまたぐ (または提示対象外の source)
            if sources & claimed_messages:
                continue
            idxs = sorted(pos[s] for s in sources)
            spans.append((idxs[0], idxs[-1], [entry]))
        if not spans:
            return []
        spans.sort(key=lambda s: (s[0], s[1]))
        units: List[List[Any]] = []
        for low, high, group in spans:
            if units and low <= units[-1][1]:
                units[-1][1] = max(units[-1][1], high)
                units[-1][2].extend(group)
            else:
                units.append([low, high, list(group)])
        def _epoch(msg: Dict[str, Any]) -> Optional[int]:
            try:
                return int(msg.get("created_at"))
            except (TypeError, ValueError):
                return None

        folds: List[FoldedRange] = []
        for _low, _high, unit_entries in units:
            mids = sorted(
                {str(s) for e in unit_entries for s in e.source_ids},
                key=lambda x: pos[x],
            )
            short_ids = [
                int(e.short_id) for e in unit_entries
                if getattr(e, "short_id", None) is not None
            ]
            folds.append(FoldedRange(
                message_ids=mids,
                start_at=_epoch(ordered[pos[mids[0]]]),
                end_at=_epoch(ordered[pos[mids[-1]]]),
                chronicle_entry_ids=[str(e.id) for e in unit_entries],
                chronicle_short_ids=short_ids,
                presented_raw=True,
            ))
        return folds

    def preview_refilled_history(
        self, persona, model_key: Optional[str] = None,
        *, raise_on_error: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """§15 読み戻し後の提示を**読みだけ**で組む (context preview 用)。

        実際の読み戻しは次の user Pulse の応答前に走るため、プレビューが素の
        窓を見せると「話しかけた時に実際に見える窓」より薄い嘘になる。§14-2
        の preview (persist_advance=False) と同じ型 — 内容は本番の読み戻しと
        同じ計算 (最終検算まで)、行は一切触らない (§14-6-5)。

        ``raise_on_error=True`` は組み立て失敗を例外で伝える (既定は WARNING +
        None へ縮退 = 会話プレビューの fail-open)。None が「適用なし (正常)」と
        「内部失敗」の両方を意味すると、context-status のような読み手が障害を
        正常値として表示してしまうため、区別が要る呼び出し側はこちらを使う
        (Codex 指摘 2026-07-30。get_memory_weave_context の raise_on_error と同じ型)。

        Returns:
            読み戻しが適用されない状況 (不足なし・開ける区間なし等) は None —
            呼び出し側は従来どおり素の窓を組む。適用されるなら::

                {
                    "presented": 読み戻し後の提示メッセージ列,
                    "new_anchor_id": 引き戻し後の窓の始点,
                    "fold_entry_ids": 読み戻し後の圧縮区間が持つ全あらすじ id
                        (head のあらすじ枠の除外名簿 — プレビュー側が weave を
                         この名簿で組み直すのに使う),
                }
        """
        model_key = str(model_key or getattr(persona, "model", "") or "") or None
        if not model_key:
            return None
        try:
            watermarks = self.get_metabolism_watermarks(persona, model_key)
            if watermarks is None:
                return None
            # raise_on_error は起点の解決と窓の読みにも貫通させる (strict) —
            # 壊れた器を「読み戻しの適用なし (正常)」として context-status に
            # 見せない (Codex 六巡目 #4)。行は書かない (persist_advance=False)。
            anchor_id, _resolution = self.resolve_metabolism_anchor(
                persona, model_key=model_key, persist_advance=False,
                strict=raise_on_error,
            )
            if not anchor_id:
                return None
            # raise_on_error は内層 (知覚一覧の取得) まで貫通させる — 厳格モード
            # の途中の読み出しだけが fail-open だと、知覚ゼロで計算した refill
            # 判定が measurement_failed なしで返る (Codex 指摘 2026-09-02 三巡目)。
            plan = self._plan_window_refill(
                persona, model_key, anchor_id, watermarks,
                raise_on_error=raise_on_error,
            )
        except Exception:
            if raise_on_error:
                raise
            LOGGER.warning(
                "[metabolism] refill preview failed; falling back to the "
                "plain window (persona=%s)",
                getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return None
        if plan is None:
            return None
        return {
            "presented": list(plan["presented"]),
            "new_anchor_id": plan["new_anchor_id"],
            "fold_entry_ids": [
                eid for f in plan["folds"] for eid in f.chronicle_entry_ids
            ],
        }

    def _plan_window_refill(
        self, persona, model_key: str, anchor_id: str, watermarks: Watermarks,
        *,
        raise_on_error: bool = False,
        strict: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """§15 読み戻しの計画 + 最終検算 (読みだけ — 行は触らない)。

        :meth:`maybe_run_window_refill` (実書き込み) と
        :meth:`preview_refilled_history` (プレビュー) の共通部。開き直しの印は
        リクエストローカルな fold オブジェクトに付けるだけで、永続化は
        呼び出し側の :meth:`_write_refill` が行う。

        段取り (docs/issues/window_floor_and_refill_redesign.md ②③④⑤):

        0. 不足判定は**会話の行だけ** vs 残す量 (2026-09-03 裁定)。
        1. **前段 — 起点をまたぐ圧縮区間**: 窓の圧縮区間のうち ``message_ids``
           が起点より左へ及ぶものがあれば、予算に関係なく起点をその区間の
           最古の行まで戻し、区間を ``presented_raw=True`` にする (開こうと
           している時点で不足は確定している)。
        2. 窓内の digest 区間を新しい方から開く (:func:`plan_reopen`)。
        3. 残りの予算であらすじの段の単位で起点を引き戻す (:func:`plan_rewind`)。
        4. **最終検算は上限 (``watermarks.high``、実際に送る合計 = 知覚込み)
           と比べる** — 残す量とは比べない。超えたら引き戻しの段をいちばん
           古いものから一段ずつ外して測り直す。「全部やめる」は無い。前段は
           外さない (それでも超えるなら WARNING を出し、次の Pulse の非常畳みに
           任せる)。

        ``raise_on_error`` は最終検算の知覚一覧の取得まで貫通させる — 厳格
        モード (context-status) の途中の読み出しだけが fail-open だと、知覚
        ゼロで測った検算が measurement_failed なしで返る。``raise_on_error`` は
        器の読み (窓・またぐ区間・古い材料) も厳格にする (Codex 七巡目 #4:
        読み失敗と「材料なし」を区別する)。``strict`` は器の読みだけを厳格に
        する (本走行の読み戻し用 — 知覚は既定のまま fail-open)。

        見送る各経路は INFO で理由を残す (⑤)。

        Returns:
            適用できる読み戻しが無ければ None。あれば::

                {
                    "new_anchor_id": 引き戻し先 (引き戻し無しなら現 anchor),
                    "folds": 書き込むべき圧縮区間の全リスト,
                    "presented": 検算済みの最終提示メッセージ列 (知覚なし),
                    "current_chars" / "final_chars": 会話の行の字数 (前後),
                    "final_total_chars": 検算で測った合計 (知覚込み),
                    "target" / "high": 水位,
                    "straddled" / "reopened" / "rewound_messages" /
                    "rewound_folds" / "dropped_steps": 記録用,
                }
        """
        # 器の読みの厳格さ: context-status の厳格モード (raise_on_error) と
        # 本走行 (strict) のどちらでも、壊れた器を薄い窓と見なして「適用なし」
        # と返したり、縮退した読みの上に書いたりしない (Codex 六巡目 #4 / 七巡目)。
        store_strict = bool(strict or raise_on_error)
        window = self.get_presented_window(
            persona, model_key, anchor_id, strict=store_strict,
        )
        persona_id = getattr(persona, "persona_id", None)
        # 読み戻しの物差しは**会話の行だけ** vs 残す量 (2026-09-03 まはー裁定:
        # 残す量の主語は会話の行。上限の主語 = 合計とは別)。巨大な部屋の様子が
        # 乗った窓でも会話が痩せていれば埋め戻す
        # (docs/issues/protection_quota_consumed_by_perception_blocks.md)。
        current_chars = stored_message_chars(window.presented)
        if current_chars >= watermarks.target:
            LOGGER.info(
                "[metabolism] refill not needed: rows %d >= target=%d "
                "(persona=%s model=%s)",
                current_chars, watermarks.target, persona_id, model_key,
            )
            return None

        from sea.window_refill import plan_reopen, plan_rewind_explained

        history_mgr = getattr(persona, "history_manager", None)
        adapter = getattr(persona, "sai_memory", None)

        # 1. 前段: 起点をまたぐ圧縮区間 (予算に関係なく必ず生へ戻す)。
        raw_ids = set(window.raw_ids)
        straddling = [
            f for f in window.folds
            if any(mid not in raw_ids for mid in f.message_ids)
        ]
        pre_before: List[Dict[str, Any]] = []
        if straddling and history_mgr is not None:
            pre_before = self._history_back_to_folds(
                history_mgr, straddling, anchor_id, raw_ids, persona_id,
                raise_on_error=store_strict,
            )
        if pre_before:
            for fold in straddling:
                fold.presented_raw = True
        base_anchor_id = str(pre_before[0].get("id")) if pre_before else anchor_id
        base_raw = list(pre_before) + list(window.raw)
        base_presented = (
            self._present_with_folds(persona, base_raw, list(window.folds))
            if pre_before else list(window.presented)
        )
        base_chars = stored_message_chars(base_presented)

        # 2. 窓内の digest 圧縮区間を新しい方から開く。
        reopen, projected_chars = plan_reopen(
            window.folds, base_raw, base_presented, base_chars, watermarks.target,
        )

        # 3. まだ足りなければ anchor をあらすじの段の単位で引き戻す。
        rewind = None
        rewind_reason: Optional[str] = None
        before: List[Dict[str, Any]] = []
        budget = watermarks.target - projected_chars
        if budget <= 0:
            rewind_reason = "no budget left after the straddling/reopen stage"
        elif history_mgr is None or not adapter or not adapter.is_ready():
            rewind_reason = "history or memory store unavailable"
        else:
            # raise_on_error は厳格なときだけ渡す (既定の呼び出し形を変えない)
            read_kwargs: Dict[str, Any] = {"raise_on_error": True} if store_strict else {}
            before = history_mgr.get_history_before_anchor(
                base_anchor_id,
                max_chars=budget,
                required_line_roles=["main_line"],
                required_scopes=["committed"],
                **read_kwargs,
            )
            if not before:
                rewind_reason = "no material before the anchor"
            else:
                try:
                    from sai_memory.arasuji.storage import get_entries_covering_messages
                    entries = get_entries_covering_messages(
                        adapter.conn, [str(m.get("id")) for m in before],
                    )
                except Exception:
                    LOGGER.warning(
                        "[metabolism] refill: failed to resolve covering entries "
                        "(persona=%s); skipping anchor rewind", persona_id,
                        exc_info=True,
                    )
                    entries = []
                if not entries:
                    rewind_reason = "no covering entries"
                else:
                    before_ids = [str(m.get("id")) for m in before]
                    try:
                        from sai_memory.memory.storage import (
                            filter_chronicle_eligible_ids,
                        )
                        eligible = {
                            str(m) for m in filter_chronicle_eligible_ids(
                                adapter.conn, before_ids,
                            )
                        }
                    except Exception:
                        # 判定できなければ全件を編纂対象とみなす — 被覆の無い
                        # 領域を跨がない側 (忘却済みの内容を復活させない側)
                        # に倒れる。
                        LOGGER.warning(
                            "[metabolism] refill: chronicle-eligibility check "
                            "failed; treating all messages as eligible "
                            "(persona=%s)", persona_id, exc_info=True,
                        )
                        eligible = set(before_ids)
                    rewind, rewind_reason = plan_rewind_explained(
                        before,
                        entries,
                        [str(m.get("id")) for m in base_raw],
                        {
                            eid
                            for f in window.folds
                            for eid in f.chronicle_entry_ids
                        },
                        {
                            mid
                            for f in window.folds
                            for mid in f.message_ids
                        },
                        eligible,
                        budget,
                        existing_folds=list(window.folds),
                    )

        if not pre_before and not reopen and rewind is None:
            LOGGER.info(
                "[metabolism] refill planned nothing (persona=%s model=%s rows=%d "
                "target=%d): %s",
                persona_id, model_key, current_chars, watermarks.target,
                rewind_reason or "nothing to reopen",
            )
            return None
        if rewind is not None and rewind_reason:
            LOGGER.info(
                "[metabolism] refill ladder stopped after %d rung(s) (persona=%s "
                "model=%s): %s",
                len(rewind.steps), persona_id, model_key, rewind_reason,
            )

        for fold in reopen:
            fold.presented_raw = True

        # 4. 最終検算: 書く前に「書いた後の提示」を実際に組んで、実際に送る合計
        # (知覚込み) を上限と比べる。計画側の勘定がモデル化していない増分が
        # 混ざっても上限をここで守る。超えたら引き戻しの段を古い方から一段
        # ずつ外して測り直す — 「全部やめる」は無い (④)。
        steps = list(rewind.steps) if rewind is not None else []
        n_before = len(before)
        dropped = 0
        final_presented: List[Dict[str, Any]] = []
        final_total = 0
        new_anchor_id = base_anchor_id
        new_folds: List["FoldedRange"] = []
        kept_existing: List["FoldedRange"] = list(window.folds)
        while True:
            if steps:
                step = steps[-1]
                new_anchor_id = step.new_anchor_id
                new_folds = list(step.folds)
                restored = list(before[n_before - step.restored_message_count:])
                # 段に併合された既存区間は、併合済みの区間に置き換わる
                absorbed = {id(f) for f in step.absorbed_existing}
                kept_existing = [f for f in window.folds if id(f) not in absorbed]
            else:
                new_anchor_id = base_anchor_id
                new_folds = []
                restored = []
                kept_existing = list(window.folds)
            final_presented = self._present_with_folds(
                persona, restored + base_raw, new_folds + kept_existing,
            )
            final_total = self.presented_chars(
                persona, final_presented, new_anchor_id,
                raise_on_error=raise_on_error,
            )
            if watermarks.high is None or final_total <= watermarks.high:
                break
            if steps:
                LOGGER.info(
                    "[metabolism] refill verification: %d chars > high=%d; "
                    "dropping the oldest rung (anchor %s) (persona=%s model=%s)",
                    final_total, watermarks.high, new_anchor_id,
                    persona_id, model_key,
                )
                steps.pop()
                dropped += 1
                continue
            # 段を全部外しても上限を超える。前段 (またぐ区間) は外さない —
            # 開こうとしている時点で不足は確定していて、超過の始末は次の
            # Pulse の非常畳みの仕事。前段が無ければ見送る (予算超過)。
            if pre_before:
                LOGGER.warning(
                    "[metabolism] refill: the straddling range alone puts the "
                    "window at %d chars > high=%d; keeping it (the emergency "
                    "pre-compaction of the next pulse trims) (persona=%s model=%s)",
                    final_total, watermarks.high, persona_id, model_key,
                )
                break
            LOGGER.info(
                "[metabolism] refill skipped: reopened window would be %d chars "
                "> high=%d (persona=%s model=%s)",
                final_total, watermarks.high, persona_id, model_key,
            )
            return None

        return {
            "new_anchor_id": new_anchor_id,
            "folds": new_folds + kept_existing,
            "presented": final_presented,
            "current_chars": current_chars,
            "final_chars": stored_message_chars(final_presented),
            "final_total_chars": final_total,
            "target": watermarks.target,
            "high": watermarks.high,
            "straddled": len(straddling) if pre_before else 0,
            "reopened": len(reopen),
            "rewound_messages": (
                len(pre_before)
                + (steps[-1].restored_message_count if steps else 0)
            ),
            "rewound_folds": len(new_folds),
            "dropped_steps": dropped,
        }

    def _history_back_to_folds(
        self, history_mgr, straddling: List["FoldedRange"], anchor_id: str,
        raw_ids: Set[str], persona_id: Optional[str],
        *,
        raise_on_error: bool = False,
    ) -> List[Dict[str, Any]]:
        """起点をまたぐ圧縮区間の最古の行から起点の直前までの提示対象を読む (②の前段)。

        ``raise_on_error`` は履歴の読み失敗を例外にする (最終防衛ラインが使う —
        読めなかったことを「材料なし」に潰さない)。

        Returns:
            時系列昇順。読めなかった (区間の外側の行から起点へ届かない / 区間の
            外側の行が読んだ範囲に揃わない) 区間は飛ばし、一つも読めなければ空。
        """
        best: List[Dict[str, Any]] = []
        for fold in straddling:
            outside = [mid for mid in fold.message_ids if mid not in raw_ids]
            # 区間の記録は正典順 (before 側 → 窓側) なので先頭が最古。
            start_id = outside[0] if outside else None
            if not start_id:
                continue
            read_kwargs: Dict[str, Any] = {"raise_on_error": True} if raise_on_error else {}
            rows = history_mgr.get_history_from_anchor(
                start_id,
                required_line_roles=["main_line"],
                required_scopes=["committed"],
                **read_kwargs,
            )
            # 切る位置は「起点の行」または「窓の生の行」の最初のもの。起点の
            # 行自体が提示対象外 (scope=discardable 等) だと読んだ列に現れず、
            # 起点だけを探すとまたぐ区間を戻せない — 本番の事故 (2026-09-03)
            # がちょうどその形だった。
            cut = next(
                (
                    i for i, m in enumerate(rows)
                    if str(m.get("id")) == anchor_id or str(m.get("id")) in raw_ids
                ),
                None,
            )
            if cut is None:
                LOGGER.warning(
                    "[metabolism] refill: straddling range starting at %s does "
                    "not reach the anchor %s or the window; leaving it (persona=%s)",
                    start_id, anchor_id, persona_id,
                )
                continue
            segment = list(rows[:cut])
            segment_ids = {str(m.get("id")) for m in segment}
            if any(mid not in segment_ids for mid in outside):
                LOGGER.warning(
                    "[metabolism] refill: straddling range starting at %s has "
                    "rows outside the presentable history; leaving it "
                    "(persona=%s)", start_id, persona_id,
                )
                continue
            if len(segment) > len(best):
                best = segment
        return best

    def _head_weave_snapshot(
        self, persona_id: Optional[str], model_key: Optional[str],
    ) -> Any:
        """(persona, model) の head snapshot が持つ weave section。

        §15 読み戻し後・手動入口の再 capture 検証用 — capture は毎回新しい
        section オブジェクトを作るので、identity が変わらなければ stale の
        使い回し。

        返す値は**三値**で、呼び出し側は区別して扱う:

        - section の実体 ... 比較の材料
        - ``None`` ......... weave が正当に無い (head 未初期化 / weave 無効 /
          snapshot 未作成)。「組み直されなかった」ではないので成功扱いでよい
        - :data:`_WEAVE_INSPECT_FAILED` ... 検査自体が失敗した (pipeline の
          import 失敗、get_snapshot の例外など)。**何も分かっていない**ので、
          成功にも失敗にも数えず、判定側が「検証できなかった」に倒す
        """
        try:
            from sea.head_pipeline import get_default_pipeline
            snap = get_default_pipeline().get_snapshot(
                str(persona_id), str(model_key),
            )
            if snap is None:
                return None
            return getattr(snap, "sections", {}).get("memory_weave")
        except Exception:
            LOGGER.warning(
                "[metabolism] head weave inspection failed (persona=%s model=%s)",
                persona_id, model_key, exc_info=True,
            )
            return _WEAVE_INSPECT_FAILED

    def _write_refill(
        self,
        persona_id: str,
        model_key: str,
        expected_anchor_id: str,
        new_anchor_id: str,
        folds: List["FoldedRange"],
        *,
        raise_on_error: bool = False,
    ) -> bool:
        """§15 読み戻しの書き込み — anchor 引き戻しと圧縮区間 (印含む) を同一コミットで。

        汎用 :meth:`upsert_anchor_entry` は anchor 変更時に圧縮区間を列ごと
        クリアするため使えない (§14-6-7 の前進と同じ理由の対側)。

        CAS: 行の現在の anchor が読み戻し計画の前提 (``expected_anchor_id``) と
        一致するときだけ書く。発火判定と書き込みの間に別入口 (手動整理 /
        先回り畳み) が anchor を動かしていたら、この計画は古い窓のものなので
        棄却する — 次の会話開始が再計画する。

        温度は now を書く (§14-6-5 の据え置きと逆にする理由): 読み戻しは応答の
        直前にだけ走り、開いた窓はその応答の LLM 呼び出しでそのままキャッシュ
        される — 据え置きにすると、直後の context 構築の resolve が「冷えた行が
        最前線より後ろ」と見て §14-2 の前進で読み戻しを即座に飲み込み、同じ
        会話の中で開いた窓が閉じる。

        ``raise_on_error=True`` は「書けなかった」の内訳を分ける
        (:meth:`preview_refilled_history` と同じ型): 書き込み自体の失敗
        (DB 例外) と、書き込みを試みられない状態 (manager 未接続) を例外で
        伝え、False を「CAS 不一致 = 意図した見送り」だけの意味にする。
        既定 (False) は §15 読み戻しの fail-open — どちらも False にして
        呼び出し側が "skip" へ落とす従来の挙動をそのまま保つ。補修の
        anchor 引き戻し (sea/coverage_repair.run_tail_rewind) だけが True を
        使い、DB 失敗を "failed" に写像する (Codex 十一巡 P1)。

        Returns:
            書けたら True。CAS 不一致は False。DB 失敗・manager 未接続は
            ``raise_on_error`` が False なら False、True なら例外。
        """
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            if raise_on_error:
                raise RuntimeError(
                    "session store is unavailable; the refill write could not "
                    "be attempted"
                )
            return False
        from sea.session_window import serialize_folds
        folds = self._merge_overlapping_folds(folds, persona_id, model_key)
        db = self.manager.SessionLocal()
        try:
            from database.models import SessionAnchor
            # CAS は条件付き UPDATE 1 文 (keepalive touch の §14-6-10 と同じ型)。
            # SELECT → 比較 → 書き込みに分けると、比較と commit の間に別接続の
            # 前進が挟まったとき stale な巻き戻しで上書きしてしまう (Codex 指摘
            # 2026-07-30)。
            updated = db.query(SessionAnchor).filter_by(
                PERSONA_ID=persona_id,
                MODEL_KEY=str(model_key),
                ANCHOR_MESSAGE_ID=expected_anchor_id,
            ).update({
                SessionAnchor.ANCHOR_MESSAGE_ID: new_anchor_id,
                SessionAnchor.FOLDED_RANGES_JSON: serialize_folds(folds),
                SessionAnchor.UPDATED_AT: int(datetime.now().timestamp()),
            }, synchronize_session=False)
            db.commit()
            if not updated:
                LOGGER.info(
                    "[metabolism] refill write skipped (CAS): anchor moved "
                    "under us (persona=%s model=%s expected=%s)",
                    persona_id, model_key, expected_anchor_id,
                )
                return False
            return True
        except Exception as exc:
            LOGGER.warning(
                "[metabolism] failed to write refill for %s/%s: %s",
                persona_id, model_key, exc,
            )
            if raise_on_error:
                raise
            return False
        finally:
            db.close()

    @staticmethod
    def _merge_overlapping_folds(
        folds: List["FoldedRange"], persona_id: Optional[str], model_key: Optional[str],
    ) -> List["FoldedRange"]:
        """書き込み前の最終検査 — 同じ行が二つの圧縮区間に属していたら併合する。

        同じ行が二つの区間に属すると、印戻し後に digest が二重に立つ。計画側
        (plan_rewind の閉包、_floor_coverage_folds の見送り) が防ぐのが本筋で、
        ここは最後の網 — 見つけたら書き込みを拒まず、該当区間を一つに併合して
        WARNING を残す (Codex 一巡目 #5)。併合は最初に現れた区間へ寄せ、行と
        あらすじ id は出現順に足す。``presented_raw`` はどれか一つでも生なら生
        (併合で行が digest に隠れて残す量を割る側には倒さない)。
        """
        owner: Dict[str, int] = {}
        merged: List[Optional["FoldedRange"]] = []
        violations = 0
        for fold in folds:
            hits = sorted({owner[mid] for mid in fold.message_ids if mid in owner})
            if not hits:
                merged.append(fold)
                index = len(merged) - 1
                for mid in fold.message_ids:
                    owner[mid] = index
                continue
            violations += 1
            target_index = hits[0]
            target = merged[target_index]
            assert target is not None
            parts = [merged[i] for i in hits[1:]] + [fold]
            for part in parts:
                if part is None:
                    continue
                for mid in part.message_ids:
                    if mid not in target.message_ids:
                        target.message_ids.append(mid)
                for eid in part.chronicle_entry_ids:
                    if eid not in target.chronicle_entry_ids:
                        target.chronicle_entry_ids.append(eid)
                for sid in part.chronicle_short_ids:
                    if sid not in target.chronicle_short_ids:
                        target.chronicle_short_ids.append(sid)
                target.presented_raw = target.presented_raw or part.presented_raw
            for i in hits[1:]:
                merged[i] = None
            for mid in target.message_ids:
                owner[mid] = target_index
        if violations:
            LOGGER.warning(
                "[metabolism] %d folded range(s) shared message ids with another "
                "range; merged them before writing (persona=%s model=%s)",
                violations, persona_id, model_key,
            )
        return [f for f in merged if f is not None]

    def write_folds_if_anchor_unchanged(
        self,
        persona_id: str,
        model_key: str,
        expected_anchor_id: str,
        folds: List["FoldedRange"],
    ) -> bool:
        """圧縮区間だけを CAS 付きで書く — anchor・温度は据え置き (§16-2 の印の書き込み)。

        :meth:`_write_refill` と同じ CAS 規律 (行の現在の anchor が期待値と
        一致するときだけ書く条件付き UPDATE 1 文) だが、こちらは anchor を
        動かさず UPDATED_AT も書かない — 被覆補修の印はキャッシュの主張では
        ないので、冷えた行を温かく偽装しない (§14-6-5 と同じ理由)。

        Returns:
            書けたら True。CAS 棄却 (anchor が動いていた)・DB 失敗は False —
            呼び出し側 (mark_covered_cold_windows) は棄却された行を諦め、
            次回の補修が現況から再計算する。
        """
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return False
        if not persona_id or not model_key:
            return False
        from sea.session_window import serialize_folds
        db = self.manager.SessionLocal()
        try:
            from database.models import SessionAnchor
            updated = db.query(SessionAnchor).filter_by(
                PERSONA_ID=persona_id,
                MODEL_KEY=str(model_key),
                ANCHOR_MESSAGE_ID=expected_anchor_id,
            ).update({
                SessionAnchor.FOLDED_RANGES_JSON: serialize_folds(folds),
            }, synchronize_session=False)
            db.commit()
            if not updated:
                LOGGER.info(
                    "[coverage-repair] fold write skipped (CAS): anchor moved "
                    "under us (persona=%s model=%s expected=%s)",
                    persona_id, model_key, expected_anchor_id,
                )
                return False
            return True
        except Exception as exc:
            LOGGER.warning(
                "[coverage-repair] failed to write folds for %s/%s: %s",
                persona_id, model_key, exc,
            )
            return False
        finally:
            db.close()

    def _refold_raw_view_folds(
        self, persona, model_key: Optional[str], window: "SessionWindow",
        watermarks: Watermarks,
    ) -> Optional["SessionWindow"]:
        """§15-3 印戻し — 生に開いた圧縮区間を digest 提示へ戻す (LLM なしの畳み)。

        Metabolism の退場計画より**先**に走る。読み戻しで開いた範囲は既存の
        あらすじを記録に持っているので、印を戻すだけで畳み直せる — 開いたまま
        計画に入れると、計画側がその範囲を未編纂の生ログと見て再編纂し、同じ
        出来事のあらすじが二本立つ (§15-3 の禁止事項)。

        古い方から、提示が残す量に収まるまで戻す。戻したら提示を組み直した
        新しい SessionWindow を返す。戻すものが無ければ None。
        """
        plan = self._refold_raw_view_plan(persona, window, watermarks)
        if plan is None:
            return None
        current_presented, flipped = plan
        persona_id = getattr(persona, "persona_id", None)
        self.save_folded_ranges(persona_id, model_key, window.folds)
        LOGGER.info(
            "[metabolism] refolded %d raw-view range(s) back to digest "
            "(persona=%s model=%s, LLM-free; %d stored chars presented — "
            "injected perceptions add to what is actually sent)",
            flipped, persona_id, model_key, message_chars(current_presented),
        )
        from sea.session_window import SessionWindow
        return SessionWindow(
            anchor_id=window.anchor_id,
            raw=window.raw,
            presented=current_presented,
            folds=window.folds,
        )

    def _refold_raw_view_plan(
        self, persona, window: "SessionWindow", watermarks: Watermarks,
    ) -> Optional[Tuple[List[Dict[str, Any]], int]]:
        """§15-3 印戻しの計画部 — ``window.folds`` の印を古い方から戻す。

        永続化はしない (印の flip は渡された fold オブジェクトに対して行う —
        本走行 :meth:`_refold_raw_view_folds` は行の fold を渡して保存し、
        下見 :meth:`preview_planning_window` は写しを渡して保存しない)。

        Returns:
            戻すものが無ければ None。あれば ``(印戻し後の提示, 戻した区間数)``。
        """
        raw_view = [f for f in window.folds if f.presented_raw]
        if not raw_view:
            return None
        order = {mid: i for i, mid in enumerate(window.raw_ids)}

        def _first_pos(fold: "FoldedRange") -> int:
            positions = [order[mid] for mid in fold.message_ids if mid in order]
            return min(positions) if positions else -1

        # 継続判定は毎回**実際の提示**を組み直して実測する。見積もり
        # (raw − 固定置き換え長) で早く止まると、実提示が残す量を超えたまま
        # 印付き区間が退場計画に入り、既編纂範囲が生ログとして再編纂されて
        # あらすじが二本立ちする (Codex 指摘 2026-07-30)。部分生存の印付き
        # 区間 (提示は既に digest) の削減量も、実測なら自然にゼロと数えられる。
        #
        # 止め時の主語は**会話の行だけ** (2026-09-03 まはー裁定。残す量の主語は
        # 会話の行、上限の主語は合計)。2026-09-02 に一度「知覚ブロックを足した
        # 合計」で止めるようにした — 根拠は「行だけで止めると、実送信が残す量を
        # 超えたまま印付き区間が退場計画へ入り再編纂される」だった。だが退場
        # 計画の保護範囲 (plan_eviction::_protection_boundary) も行だけで測る
        # 以上、行が残す量以下になった時点で残る印付き区間は全て保護範囲の
        # 内側にあり、計画には拾われない — 二本立ちは起きない。逆に合計で
        # 止め続けると、巨大な部屋の様子が乗った回に印付き区間を全部 digest へ
        # 戻して会話が痩せる (保護範囲をブロックが食った事故と同じ向き)。
        # 行だけが一貫した規則で、知覚一覧を引く必要も無い。
        current_presented = window.presented
        flipped = 0
        for fold in sorted(raw_view, key=_first_pos):  # 古い方から
            if message_chars(current_presented) <= watermarks.target:
                break
            fold.presented_raw = False
            flipped += 1
            current_presented = self._present_with_folds(
                persona, window.raw, window.folds,
            )
        if not flipped:
            return None
        return current_presented, flipped

    def preview_planning_window(
        self, persona, model_key: Optional[str], window: "SessionWindow",
        watermarks: Watermarks,
        *,
        raise_on_error: bool = False,
    ) -> Tuple["SessionWindow", int]:
        """本走行が退場計画の前に行う窓の正規化を**書き込みなし**で再現する。

        本走行 (:meth:`_run_metabolism_locked`) は plan_eviction の前に
        ①恒久欠落 fold の記録破棄 (:meth:`_drop_dead_folds`) と
        ②§15-3 印戻し (:meth:`_refold_raw_view_folds`) を通す。下見
        (context-status の fold_ready) が素の窓を planner に渡すと、読み戻しで
        生に開いた区間や死んだ fold のある窓で本走行と別の答えを出す —
        「押せたのに何も起きない」(8/24 に潰した型) の再発口になる
        (Codex 指摘 2026-08-29)。ここは同じ正規化を写しに適用して返す。
        行 (save_folded_ranges / anchor / 温度) は一切書かない。

        Returns:
            ``(正規化後の窓, 印戻しで digest 表示へ戻る区間数)``。区間数が
            1 以上なら、手動の畳みは退場計画が空でも印戻しだけで提示を
            減らせる (= 実行する意味がある)。
        """
        import copy

        from sea.session_window import SessionWindow

        # ① 恒久欠落 fold — 判定は本走行と同じ、記録破棄はしない。恒久欠落の
        # fold は提示でも生ログに倒れている (fail-open) ので、外しても提示の
        # 姿は変わらない — planner へ渡す fold 一覧だけを本走行と揃える。
        dead = self._dead_folds_of(persona, window)
        kept = [f for f in window.folds if f not in dead]
        # ② 印戻しは fold の presented_raw を書き換えるので、写しに対して行う
        # (浅い copy で足りる — flip するのは bool 属性だけ)。
        work_folds = [copy.copy(f) for f in kept]
        work = SessionWindow(
            anchor_id=window.anchor_id,
            raw=window.raw,
            presented=(
                window.presented if not dead
                else self._present_with_folds(persona, window.raw, work_folds)
            ),
            folds=work_folds,
        )
        # (raise_on_error は印戻しには効かない — 止め時が会話の行だけになり、
        # 知覚一覧を引かなくなったため。引数は呼び出し側の契約として残す。)
        refolded = self._refold_raw_view_plan(persona, work, watermarks)
        if refolded is None:
            return work, 0
        presented, flipped = refolded
        return SessionWindow(
            anchor_id=work.anchor_id,
            raw=work.raw,
            presented=presented,
            folds=work.folds,
        ), flipped

    def schedule_cold_window_sweep(self) -> None:
        """§14-4 の時計側見張りを EventScheduler に予約する。

        SAIVerseManager.start() から一度呼ばれ、以降は tick が自分で次回を積む
        (再帰予約)。scheduler の無い環境 (テスト等) では何もしない。
        """
        scheduler = getattr(self.manager, "event_scheduler", None) if self.manager else None
        if scheduler is None:
            return
        scheduler.schedule(
            fire_at=datetime.now() + timedelta(seconds=self.COLD_SWEEP_INTERVAL_SECONDS),
            callback=self._cold_window_sweep_tick,
            key=self._COLD_SWEEP_KEY,
        )

    def _cold_window_sweep_tick(self) -> None:
        """全 persona を巡回して先回り畳み (§14-4) の条件を検査する。

        検査は読みだけ (安い・LLM なし)。畳みが要る persona だけ daemon
        スレッドへ逃がす — EventScheduler の dispatch スレッドで重い LLM 処理を
        同期実行すると後続の予約が滞るため (saiverse/event_scheduler.py の
        docstring: 重い処理は別 thread に投げる)。
        """
        try:
            personas = dict(getattr(self.manager, "personas", None) or {})
            for persona in personas.values():
                try:
                    if self.cold_precompaction_status(persona) != "due":
                        continue
                    self._spawn_cold_precompaction(persona)
                except Exception:
                    LOGGER.exception(
                        "[metabolism] cold sweep check failed (persona=%s)",
                        getattr(persona, "persona_id", "?"),
                    )
        finally:
            self.schedule_cold_window_sweep()

    def cold_precompaction_status(self, persona) -> str:
        """先回り畳み (§14-4) の発火条件を検査する (読みだけ・LLM なし)。

        条件 (まはー裁定 2026-07-29):

        - 全 anchor 行が冷え切っている — 一つでも生きたキャッシュがあるうちは
          畳まない (畳み = anchor 前進はそのキャッシュを壊す)
        - 提示ウィンドウ (現行 model の水位で評価) が残す量と上限の中間
          ((target + high) / 2) を超えている

        加えて Chronicle 生成が有効な persona に限る — 先回りはコスト最適化で
        あって回復措置ではないので、「編纂は都度確認したい」設定
        (AUTONOMOUS_CHRONICLE_ENABLED=False) や「編纂なしで忘れる」設定の
        persona の畳みを裏で前倒ししない (忘却まで早まってしまう)。

        Returns: "skip" (対象外) / "hot" (生きたキャッシュあり) /
        "cool" (中間値以下) / "due" (発火条件成立)
        """
        persona_id = getattr(persona, "persona_id", None)
        model_key = str(getattr(persona, "model", "") or "") or None
        if not persona_id or not model_key:
            return "skip"
        # 門はペルソナ設定だけ (env ENABLE_MEMORY_WEAVE_CONTEXT は 2026-09-01 撤去)。
        if not self.is_chronicle_enabled_for_persona(persona):
            return "skip"
        if not self.is_autonomous_chronicle_enabled_for_persona(persona):
            return "skip"
        watermarks = self.get_metabolism_watermarks(persona, model_key)
        if watermarks is None or watermarks.high is None:
            return "skip"
        anchors = self.load_anchor_entries(persona_id)
        rows = {mk: e for mk, e in anchors.items() if e.get("anchor_id")}
        if not rows:
            return "skip"  # 起点未確立 (ブートストラップ前) — 畳む対象が無い
        for mk, entry in rows.items():
            if self._anchor_entry_is_hot(entry, mk, persona_id):
                return "hot"
        self_entry = rows.get(model_key)
        if not self_entry:
            # 現行 model の行が無い — 窓を定義できない。次の会話の resolve
            # (§14-2) が最前線から立てるのを待つ。
            return "skip"
        window = self.get_presented_window(persona, model_key, self_entry["anchor_id"])
        # 中間値との比較も「実際に送る中身」で測る (2026-09-02 まはー裁定)。
        # 先回りが救おうとしているのは実送信の膨らみなので、保存行だけで測ると
        # 知覚ブロックで既に中間値を越えている窓を "cool" と読み、次の会話の
        # 非常畳み (§14-3) へ丸ごと送ってしまう。
        current_chars = self.presented_chars(
            persona, window.presented, self_entry["anchor_id"],
        )
        midpoint = (watermarks.target + watermarks.high) / 2
        if current_chars <= midpoint:
            return "cool"
        return "due"

    def _spawn_cold_precompaction(self, persona) -> None:
        """先回り畳みを daemon スレッドで実行する (persona ごとに同時 1 本)。"""
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return
        with self._cold_sweep_lock:
            if persona_id in self._cold_sweep_inflight:
                return
            self._cold_sweep_inflight.add(persona_id)

        def _target() -> None:
            try:
                self.run_cold_precompaction(persona)
            except Exception:
                LOGGER.exception(
                    "[metabolism] cold pre-compaction thread crashed (persona=%s)",
                    persona_id,
                )
            finally:
                with self._cold_sweep_lock:
                    self._cold_sweep_inflight.discard(persona_id)

        threading.Thread(
            target=_target, daemon=True,
            name=f"cold-precompaction-{persona_id}",
        ).start()

    def run_cold_precompaction(self, persona) -> str:
        """先回り畳み (§14-4) の本体。条件を再検査してから通常の範囲規則で畳む。

        根拠 (§14-1): 編纂の総作業量は畳む時期によらず不変なので、前倒しに
        追加費用は無い。放置した場合の「もうすぐ畳まれる範囲を非キャッシュ
        単価で読み、直後の畳みで捨てられるキャッシュを作る」無駄だけが消える。

        発火条件の最終判定は **Beat ロックの内側**で行う — tick 側の事前判定から
        ロック取得までの間にユーザー Pulse が完走して anchor を touch していたら、
        温まったばかりのキャッシュを畳まず "hot" で引き返す (§14-4 の中心不変
        条件「生きたキャッシュがあるうちは畳まない」、Codex 2巡目 2026-07-29)。
        内側の :meth:`run_metabolism` は同一スレッドの RLock 再入で無害。

        合計が中間値を超えていても**会話の行が残す量以下**なら畳めるものが
        無い (残す量の主語は会話の行、2026-09-03 裁定 — 退場計画は保護範囲で
        埋まって空になる)。その回は run_metabolism を呼ばず "skip" を返す —
        本体 (:meth:`_run_metabolism_locked`) は計画が空と分かる前に抽出の
        滞留 (:meth:`_retry_extraction_backlog` — LLM 課金と記憶書き込み) を
        流すので、空振りと分かっている回に入れない (Codex 指摘 2026-09-03)。
        合計が上限も超えていれば知覚の供給が予算超過 — 警告はペルソナごと
        1 度 (:meth:`_note_perception_over_budget`)。

        Returns:
            :meth:`cold_precompaction_status` の値 (条件不成立時)、"skip"
            (会話の行が残す量以下で畳めるものが無い)、または run_metabolism の
            結果 ("ok"/"nothing"/"failed"/"deferred"/"deferred_sluice_unseen")。
            関所 (pending flush) が通らないときは "deferred" (次の tick に譲る)。
        """
        from sea.beat_gate import BeatGateClosedError, hold_beat
        persona_id = getattr(persona, "persona_id", None)
        try:
            with hold_beat(self.manager, persona_id, purpose="metabolism"):
                status = self.cold_precompaction_status(persona)
                if status != "due":
                    return status
                model_key = str(getattr(persona, "model", "") or "") or None
                watermarks = self.get_metabolism_watermarks(persona, model_key)
                window = self.get_presented_window(persona, model_key)
                if watermarks is None or not window.anchor_id:
                    return "skip"
                # 判定 (cold_precompaction_status) と同じ物差しで報告する —
                # ログだけ保存行のままだと、「中間値超過」と言いながら中間値
                # 未満の数字が並ぶ。
                current_chars = self.presented_chars(
                    persona, window.presented, window.anchor_id,
                )
                # 「削る先があるか」は会話の行だけを残す量と比べる (残す量の
                # 主語は会話の行、2026-09-03 裁定)。行が残す量以下なら退場計画は
                # 保護範囲で埋まって空 — 本体へ進むと、空と分かる前に抽出の
                # 滞留 (LLM) を流し、10 分ごとに同じ空振りを繰り返す。
                rows_chars = message_chars(window.presented)
                if rows_chars <= watermarks.target:
                    if not self._note_perception_over_budget(
                        persona, rows_chars, current_chars, watermarks,
                    ):
                        LOGGER.debug(
                            "[metabolism] cold pre-compaction skip: rows already "
                            "at/below target (persona=%s model=%s %d row chars <= "
                            "target=%d, %d chars sent)",
                            persona_id, model_key, rows_chars,
                            watermarks.target, current_chars,
                        )
                    return "skip"
                LOGGER.info(
                    "[metabolism] cold pre-compaction: all anchors cold and window "
                    "past midpoint (persona=%s model=%s %d chars, target=%d high=%s)",
                    persona_id, model_key, current_chars,
                    watermarks.target, watermarks.high,
                )
                building_id = getattr(persona, "current_building_id", None) or ""
                return self.run_metabolism(
                    persona, building_id, window, watermarks, None,
                    model_key=model_key, chronicle_force=True,
                )
        except BeatGateClosedError:
            # 先回りは急がない — pending が残る persona は次の tick に譲る。
            LOGGER.info(
                "[metabolism] cold pre-compaction deferred: beat gate closed "
                "(persona=%s)", persona_id,
            )
            return "deferred"

    def _run_metabolism_locked(
        self,
        persona,
        building_id: str,
        window: "SessionWindow",
        watermarks: Watermarks,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        model_key: Optional[str] = None,
        chronicle_force: bool = False,
        stop_when_disabled: bool = False,
        cancellation_token: Optional[CancellationToken] = None,
        close_undersized_tail: bool = False,
    ) -> str:
        """:meth:`run_metabolism` の本体 (Beat ロック保持下で実行される)。

        提示コンテキストは**ロックの内側で撮り直す**。呼び出し元 (発火判定) が撮った提示コンテキストはロックの
        外の値で、その間に別入口 (手動の記憶整理など) が圧縮区間や anchor を書いている
        ことがある。古い提示コンテキストを土台に圧縮区間を上書き保存すると、先行の圧縮区間が消えて生ログが
        復活し、しかもその範囲は編纂済みなので二重提示になる。
        """
        from sai_memory.arasuji.alignment import chronicle_band_budget

        model_key = str(model_key or getattr(persona, "model", "") or "") or None
        persona_id = getattr(persona, "persona_id", "?")

        # Chronicle の有効判定は**ロックの内側で一度だけ**行い、以降はこの値を
        # 使う。手動入口 (stop_when_disabled) の契約は「Chronicle に畳む」なので、
        # 入口の事前判定とこのロックの間に設定が OFF へ反転していたら (TOCTOU,
        # Codex 三巡 2026-07-29)、編纂なしの退場へ進ませずここで止める。
        # 自動発火・§14 経路 (stop_when_disabled=False) は従来どおり disabled
        # でも前進する (編纂なしで忘れる設計合意)。
        # 門はペルソナ設定だけ (env ENABLE_MEMORY_WEAVE_CONTEXT は 2026-09-01 撤去)。
        chronicle_enabled = self.is_chronicle_enabled_for_persona(persona)
        # 前回までに失敗した抽出の拾い直し (付箋 backlog) は**この位置** —
        # 編纂の計画・確認・claim より手前で、手動入口の早期 return よりも手前。
        # 畳むものが無い回でも回収は走り、走らないときは「止まっている」ことを
        # 知らせる (Codex 四巡 #4: 手動入口の return を素通りしていた)。
        self._retry_extraction_backlog(
            persona,
            event_callback=event_callback,
            chronicle_enabled=chronicle_enabled,
        )

        if stop_when_disabled and not chronicle_enabled:
            LOGGER.info(
                "[metabolism] manual compaction stopped: chronicle disabled "
                "under the beat lock (persona=%s)", persona_id,
            )
            return "disabled"

        band_budget = chronicle_band_budget()
        fresh = self.get_presented_window(persona, model_key)
        if fresh.anchor_id:
            # あらすじを恒久に失った圧縮区間の記録はここで捨てる — 残すと
            # 提示 (生ログに fail-open) と適用側の二重記録判定で生死の読みが
            # 食い違い、その範囲の再畳みが永久に拒否される (issue 顔その2)。
            window = self._drop_dead_folds(persona, model_key, fresh)
        current_messages = window.presented
        if not current_messages:
            LOGGER.info(
                "[metabolism] window is empty under the beat lock; nothing to do "
                "(persona=%s)", persona_id,
            )
            return "nothing"
        if not window.anchor_id:
            # 関所 (Codex 2026-08-24 #1): 非空の窓が起点を持たないのは契約違反。
            # 本番の全呼び出し元 (maybe_run_metabolism / run_manual_compaction /
            # emergency / cold precompaction) は get_presented_window 経由で窓を
            # 作り、起点なしは空窓になるのでここへは到達しない — 型の上でだけ
            # 手組みの窓で通れる。起点なしのまま進むとスルースの凍結
            # (window_anchor_id) が None になり、組成側の起点解決 (§14-2 前進
            # つき) が復活して「一つの一貫した窓」が破れる — fail-closed。
            LOGGER.error(
                "[metabolism] non-empty window without anchor_id (persona=%s, "
                "%d messages); refusing to run — the sluice would compose on an "
                "unpinned window", persona_id, len(current_messages),
            )
            return "failed"

        # §15-3 印戻し: 読み戻しで生に開いた圧縮区間は、退場計画より先に digest
        # 提示へ戻す (既存あらすじの再利用 = 編纂ゼロの畳み)。印戻しだけで残す量に
        # 収まればこの Metabolism は編纂なしで完了する。
        refolded = self._refold_raw_view_folds(persona, model_key, window, watermarks)
        if refolded is not None:
            window = refolded
            current_messages = window.presented
            # 「印戻しだけで残す量に収まったか」の主語も**会話の行だけ**
            # (2026-09-03 裁定)。行が残す量以下なら退場計画は保護範囲で埋まって
            # 空になる — 進んでも "nothing" で終わるだけなので、ここで完了を
            # 返す。次の Pulse の発火判定 (合計 vs 上限) がまた発火しても、
            # 行が残す量以下の窓は本体へ進まず引き返す (知覚の供給が予算超過
            # の警告)。
            if message_chars(current_messages) <= watermarks.target:
                self.ensure_recall_embeddings(persona)
                try:
                    from saiverse.dynamic_state import DynamicStateManager
                    DynamicStateManager.on_metabolism(
                        persona, self.manager, model_key=model_key,
                    )
                except Exception:
                    LOGGER.exception("[dynamic_state] on_metabolism failed")
                if event_callback:
                    event_callback({
                        "type": "metabolism",
                        "status": "completed",
                        "content": "記憶を整理しました（開いていた範囲をあらすじ表示に戻しました）",
                    })
                return "ok"

        # 退場計画 (arasuji_levels.md §3/§4): 残す量 (watermarks.target) より
        # 古い側を、古い順に U (材料字数 — 2026-08-29 裁定) ずつの範囲に刻んで
        # 全部畳む。エピソードに畳みを止める権利は無い (開いているエピソードも
        # 畳む)。close_undersized_tail は非常畳み (§14-3) だけが True にする。
        # 計画の入力は「実際に送る中身」= 保存行 + 知覚ブロック (2026-09-02 裁定)。
        # ブロックは重さだけが効き、fold の中身にも境目にもならない — 畳んだ範囲を
        # 覆うあらすじが確定すると、その期間のブロックには付記印が付いて提示から
        # 下りるので、削減見込みには数える。下見 (context-status の fold_ready) も
        # 同じ組成で渡すこと (下見と本走行が別の答えを出す退行の再発防止)。
        plan = plan_eviction(
            self.presented_with_perceptions(
                persona, current_messages, window.anchor_id,
            ),
            set(), watermarks, target_chars=band_budget,
            close_undersized_tail=close_undersized_tail,
        )
        if plan.is_empty:
            # 会話の行が残す量以下で合計だけが上限超え = 知覚の供給が予算超過。
            # 入口の門 (行 vs 残す量) で弾かれるのが普通だが、印戻し・恒久欠落
            # fold の破棄でロック内の窓が痩せた回はここまで来る。LLM は呼ばずに
            # 引き返す (警告はペルソナごと 1 度)。
            if not self._note_perception_over_budget(
                persona, plan.stored_chars, plan.total_chars, watermarks,
            ):
                LOGGER.warning(
                    "[metabolism] nothing foldable this round (persona=%s, %d chars "
                    "sent / %d row chars, target=%d, protected_from=%d); window "
                    "stays large until the foldable range reaches U=%d in "
                    "material chars (currently %d)",
                    persona_id, plan.total_chars, plan.stored_chars,
                    watermarks.target, plan.protected_from, band_budget,
                    plan.pending_material_chars,
                )
            return "nothing"

        evict_count = plan.evicted_count
        keep_count = len(current_messages) - evict_count

        # 1. Notify start
        if event_callback:
            event_callback({
                "type": "metabolism",
                "status": "started",
                "content": f"記憶を整理しています（{len(current_messages)}件 → {keep_count}件）...",
            })

        # 2. Chronicle generation (only if Memory Weave is enabled AND per-persona toggle is on)。
        # 二層分離 (beat_execution_context.md §3.2): 編纂の成否 (status) を持ち、
        # 退役 (step 3 の anchor 前進) を「編纂が済んだ」ときだけ許す (SEA 監査 S2)。
        # "disabled" (トグル OFF / weave 無効) で前進するのは設計判断 — Chronicle を
        # 切った persona は「編纂なしで忘れる」を選んでおり、前進を止めると
        # metabolism が永久デッドロックする。
        # 退場時圧縮 (§4-1): 編纂対象は**今回退場させる範囲そのもの**。範囲は連続
        # とは限らない (提示コンテキストの途中を畳むため) ので、fold ごとに区切って渡し、離れた
        # 範囲が一つのあらすじに束ねられること (§4-5 連続束ねのみ) を防ぐ。
        #
        # 編纂対象 (chronicle_eligibility_filter) と fold の集合はほぼ一致する
        # (2026-08-29 裁定で機構タグ handy_tool / spell / event_message の除外を
        # 解除。session_digest も 2026-07-28 から編纂対象)。残る除外は Stelis
        # スレッド・除外 line_role (sub_line / meta_judgment / nested)・非
        # committed scope (discardable / volatile) で、いずれも main_line の
        # 提示に立たない行なので fold にもまず現れない (例外は committed へ
        # 昇格したメタ判断 — 提示には立つが line_role で編纂対象外のまま)。
        # あらすじが生まれなかった fold は `_apply_eviction_plan` が「あらすじを
        # 持たない fold は退場させない」で拾う (退場そのものを見送るので、
        # 消えるのではなく生ログのまま残る)。
        chronicle_status = "disabled"
        if chronicle_enabled:
            try:
                chronicle_status = self.generate_chronicle(
                    persona, event_callback,
                    force=chronicle_force,
                    cancellation_token=cancellation_token,
                    compile_groups=compile_groups_from_folds(
                        plan.folds, current_messages,
                    ),
                )
            except Exception as exc:
                LOGGER.warning("[metabolism] Chronicle generation failed: %s", exc)
                chronicle_status = "failed"

        # (旧 2.5. Track Chronicle generation は W4 で廃止 —
        # experience_structure.md §11-10 の裁定。既存 Track Chronicle データと
        # 読み込み側は残る。再訪問題は docs/issues/track_episode_continuity.md)

        # 2.7. Recall embedding maintenance — Chronicle 生成の成否・トグルとは独立に、
        # 未埋め込みの Chronicle/ページ/Fragment を毎回埋める (ローカル・無料)。
        # Chronicle 生成に相乗りさせると早期 return の巻き添えでバックログが溜まる
        # (2026-07-04 の実測で確認済み)。自動想起 (ゾーン C) の再現率を支える。
        self.ensure_recall_embeddings(persona)

        # 2.8. スルース (sluice) — Chronicle 生成後・eviction 前の温まった
        # prefix で、押し出される会話からコア記憶・手帳メモ・約束を採取する。
        # 確実に通るゲート (autonomous_behavior_v3.md §13.3): スルースが失敗したら
        # 退場を止める — あらすじ生成の失敗と同格で、anchor が据え置かれるので
        # watermark 超過が残り、次の maybe_run_metabolism が自然に再試行する。
        # 旧 gold_panning の「失敗しても退場が進む」柔らかい格は廃止 (2026-08-19)。
        # 編纂が failed / deferred の回は走らせない — 退場は既に止まっており、
        # 走らせるとパンマーカーだけ前進して再試行時の担当範囲が痩せる。
        sluice_status = "ok"
        sluice_seen_ids: Optional[List[str]] = None
        sluice_seen_end: Optional[str] = None
        sluice_finalize = None
        if chronicle_status in ("ok", "disabled"):
            try:
                from sea.sluice import run_sluice
                # window_anchor_id: 実行頭に撮った窓の起点をスルースへ渡し、
                # プロンプト組成をこの起点に凍結する — 実行中に Chronicle が
                # 確定して機構1 (§14-2) が動いても、退場計画の土台とスルース
                # 入力は同じ窓のまま (2026-08-24 まはー裁定「一回の整理は
                # 一つの一貫した窓で最後まで走る」)。
                sluice_summary = run_sluice(
                    self, persona, building_id, current_messages, evict_count,
                    event_callback, finalize=False,
                    window_anchor_id=window.anchor_id,
                    model_key=model_key,
                )
                sluice_seen_ids = (sluice_summary or {}).get("seen_ids")
                sluice_seen_end = (sluice_summary or {}).get("seen_span_end")
                sluice_finalize = (sluice_summary or {}).get("finalize")
            except Exception:
                LOGGER.exception(
                    "[sluice] failed; eviction blocked, will retry on next metabolism",
                )
                sluice_status = "failed"

        # 2.9. 二段の検算と確定 (Codex 第五巡 修正 2 — v3 §13.3 の不変条件:
        # 全経験は退場前に一度本人の目を通る)。seen_ids が None なのは disabled
        # スキップだけで、そのとき退場は「採取なしで忘れる」設計どおり進む。
        #
        # ① 確定 (マーカー前進 + 台帳 completed) のゲート: マーカーの新位置
        #    (seen_span_end) 以前の窓のメッセージが**全件 seen に含まれる**とき
        #    だけ確定する。コールド実行の anchor 前進で窓の頭が LLM 入力から
        #    漏れた回にマーカーを進めると、その未提示メッセージは次回の担当
        #    範囲からも漏れて永遠に採取されない — その回は確定を保留し、記録は
        #    applied のまま次回の再適用に委ねる。
        # ② 退場のゲート: 退場計画の対象 ID **全件**が seen に含まれるときだけ
        #    退場する。新着 (seen の後ろ) が計画に入った回はここで止まる —
        #    ①は通る (マーカーは未提示を跨がない) ので確定は済み、次回の
        #    Metabolism が新しいスルースで新着を見てから退場する (現行の
        #    「新着は退場見送り → 次回の新スルース」経路)。
        if sluice_status == "ok" and sluice_seen_ids is not None:
            finalize_ok = _marker_advance_is_safe(
                current_messages, sluice_seen_ids, sluice_seen_end,
            )
            evict_ok = _eviction_within_seen(plan, sluice_seen_ids)
            if finalize_ok and sluice_finalize is not None:
                try:
                    sluice_finalize()
                except Exception:
                    LOGGER.exception(
                        "[sluice] finalize failed; eviction blocked "
                        "(record stays applied, will retry on next metabolism)",
                    )
                    sluice_status = "failed"
            elif not finalize_ok:
                LOGGER.info(
                    "[sluice] finalize withheld: marker advance to %s would skip "
                    "messages the sluice never saw (persona=%s); record stays "
                    "applied for re-application on the next metabolism",
                    sluice_seen_end, persona_id,
                )
            if sluice_status == "ok" and not (finalize_ok and evict_ok):
                LOGGER.info(
                    "[sluice] eviction deferred (finalize_ok=%s evict_ok=%s, "
                    "persona=%s, seen=%d ids); the next metabolism will run a "
                    "fresh sluice over the unseen range first",
                    finalize_ok, evict_ok, persona_id, len(sluice_seen_ids),
                )
                sluice_status = "unseen_tail"

        # 3. Update anchor to new window start — S2 ガード: 編纂が済んだ
        # ("ok") か編纂を持たない設計 ("disabled")、かつスルースが通った
        # ときだけ退役する。failed / deferred は据え置き — watermark 超過が
        # 残るので、次の maybe_run_metabolism が自然に再試行する
        # (beat_execution_context.md §3.2 / autonomous_behavior_v3.md §13.3)。
        if chronicle_status in ("ok", "disabled") and sluice_status == "ok":
            self._apply_eviction_plan(
                persona, model_key, window, plan, chronicle_status,
            )

            # 4. Dynamic State Sync: 可視化は model の節目 — anchor を進めた model の
            # (persona, model) snapshot だけを再 capture する (§3.2。他 model の提示コンテキストは
            # 自分の節目まで prefix を変えない = prefix cache 保護)。
            try:
                from saiverse.dynamic_state import DynamicStateManager
                DynamicStateManager.on_metabolism(persona, self.manager, model_key=model_key)
            except Exception:
                LOGGER.exception("[dynamic_state] on_metabolism failed")

            # 5. Notify completion
            if event_callback:
                event_callback({
                    "type": "metabolism",
                    "status": "completed",
                    "content": f"記憶の整理が完了しました（{evict_count}件の会話をChronicleに圧縮）",
                    "evicted": evict_count,
                    "kept": keep_count,
                })
            return "ok"
        else:
            LOGGER.warning(
                "[metabolism] anchor held back (chronicle_status=%s, "
                "sluice_status=%s, model=%s); will retry on next "
                "maybe_run_metabolism",
                chronicle_status, sluice_status, model_key,
            )
            if chronicle_status not in ("ok", "disabled"):
                message = "記憶の整理を見送りました（Chronicle生成が完了しなかったため、次回に再試行します）"
                ret = chronicle_status  # "failed" / "deferred" (手動入口の結果報告用)
            elif sluice_status == "unseen_tail":
                # スルース自体は成功している (採取とマーカー前進は確定済み)。
                # 退場だけを次回へ譲る。理由を戻り値で運ぶ — 手動入口
                # (run_manual_compaction → arasuji.py / organize-memory) が
                # 「別の整理が処理中」(claim 競合の文面) と混同して報告しない
                # ため (docs/issues/archive/metabolism_deferral_mislabeled_as_window_claim.md 従)。
                # 読めていない範囲は末尾の新着とは限らない (冷えた起点の前進で
                # 窓の頭側が漏れる並びが実機の初出) — 文面で新着と断定しない。
                message = "記憶の整理を見送りました（今回の採取で読めていない範囲があるため、次回に改めて整理します）"
                ret = "deferred_sluice_unseen"
            else:
                message = "記憶の整理を見送りました（記憶の採取（スルース）が完了しなかったため、次回に再試行します）"
                ret = "failed"
            if event_callback:
                event_callback({
                    "type": "metabolism",
                    "status": "completed",
                    "content": message,
                })
            return ret

    def _retry_extraction_backlog(
        self,
        persona,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        chronicle_enabled: bool = True,
    ) -> None:
        """失敗した抽出の拾い直し（付箋 backlog）。Metabolism の頭で呼ぶ。

        抽出が発火するのは「チャンクが Chronicle として確定する瞬間」の一度きり
        で、確定済みチャンクは再実行で冪等スキップされる —— 失敗した抽出には
        本編の再実行では二度と番が回らない。だから回収は**編纂の計画・確認・
        claim から独立**させ、Metabolism の頭に置く。編纂対象が無い夜も、確認を
        断った回も、claim が競合した回も回収は走る。

        （Sol レビュー 2026-08-06 F4: 以前は generate_chronicle の中ほどにあり、
        「編纂対象なし → return」の向こう側だった —— 静かな夜が続くと付箋は
        永久に残り、「次回の記憶の整理でやり直します」という画面の約束が嘘に
        なっていた。）

        **課金の同意について**: ここは編纂の確認ダイアログより手前なので、
        拾い直しの LLM コールはそのダイアログの承認を通らない。通せない ——
        通すと元の穴 (確認を断つと永久に回収されない) が戻る。代わりに
        次の三つで縛る:

        1. 走ってよいのは「確認なしで編纂してよい」と設定されている persona
           だけ (``AUTONOMOUS_CHRONICLE_ENABLED``)。拾い直しは常に確認ダイアログ
           の外側で起きるので、判断材料はこの設定しかない。**Pulse の種別は
           見ない** —— ``_current_pulse_type`` は Pulse の外 (§14 の先回り畳み
           など) で残留値になり、認可の根拠にできない (Codex 二巡 #1)。
        2. 対象は「一度は確定と共に承認された抽出のうち失敗した分」だけ。
           付箋 1 枚につき LLM 1 回、1 枚あたり 3 回まで (それ以上は止まる)。
        3. 走ったことは画面通知に出す (黙って課金しない)。

        止めるときは**止まっていることを見せる**。Chronicle 自体を切っている
        persona では抽出も拾い直しも走らないが、付箋が残っているなら WARN で
        毎回知らせる —— 黙って永久に溜まるのが元の欠陥だった (Codex 三巡 #3)。

        LLM クライアントは、拾い直せる付箋が実際にあるときだけ用意する。
        """
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            return
        persona_id = getattr(persona, "persona_id", None)

        try:
            from sai_memory.memory.entity_extractor import (
                count_extraction_backlog,
                make_batch_callback,
                retry_extraction_backlog,
            )

            counts = count_extraction_backlog(
                adapter.conn, db_lock=adapter._db_lock,
            )
            pending, total = counts["claimable"], counts["total"]
            if not total:
                # 付箋が無い回も一行だけ残す。何も出ないと「頭で呼ばれている」
                # こと自体が実機で確かめられない (まはーは常に DEBUG で見ている)
                LOGGER.debug(
                    "[extraction-backlog] 付箋なし — 拾い直しは不要 (persona=%s)",
                    persona_id,
                )
                return

            if pending < total:
                # 上限まで試して止まっている付箋。拾い直しは走らないので、
                # 残っていること自体を毎回知らせる (黙って諦めない)
                LOGGER.warning(
                    "[extraction-backlog] 上限まで試して止まっている付箋が "
                    "%d 件あります (persona=%s) — この範囲の知識は自動では"
                    "拾い直されません", total - pending, persona_id,
                )
            if not chronicle_enabled:
                LOGGER.warning(
                    "[extraction-backlog] 失敗した抽出が %d 件残っていますが、"
                    "Chronicle を切っているので拾い直しは止まっています "
                    "(persona=%s)。Chronicle を戻すと再開します",
                    total, persona_id,
                )
                return
            if not self.is_autonomous_chronicle_enabled_for_persona(persona):
                LOGGER.warning(
                    "[extraction-backlog] 失敗した抽出が %d 件残っていますが、"
                    "確認なしの編纂を切っているので拾い直しは止まっています "
                    "(persona=%s)",
                    total, persona_id,
                )
                return
            if not pending:
                return

            from saiverse.memory_weave_llm import (
                build_memory_weave_client,
                resolve_memory_weave_config,
            )
            try:
                model_id, model_config, _source = resolve_memory_weave_config(
                    persona, purpose="chronicle",
                )
            except LookupError as exc:
                LOGGER.warning(
                    "[extraction-backlog] %s — 拾い直しは次回へ持ち越し "
                    "(付箋 %d 枚, persona=%s)", exc, pending, persona_id,
                )
                return
            client = build_memory_weave_client(model_id, model_config)
            callback = make_batch_callback(
                client, adapter.conn,
                persona_id=persona_id,
                db_lock=adapter._db_lock,
            )
            LOGGER.info(
                "[extraction-backlog] 拾い直しを開始 (persona=%s, 付箋 %d 枚)",
                persona_id, pending,
            )
            if event_callback:
                event_callback({
                    "type": "metabolism",
                    "status": "running",
                    "content": (
                        f"前回失敗した知識の書き出しを {pending} 件やり直しています..."
                    ),
                })
            stats = retry_extraction_backlog(
                adapter.conn, callback, db_lock=adapter._db_lock,
            )
            if event_callback and stats.get("recovered"):
                event_callback({
                    "type": "metabolism",
                    "status": "running",
                    "content": (
                        f"失敗していた知識の書き出しを {stats['recovered']} 件"
                        "やり直しました。"
                    ),
                })
        except Exception:
            # 拾い直しそのものが落ちた回。付箋は残るので次の Metabolism で
            # もう一度番が回る —— Metabolism 本体は続ける (編纂を巻き添えに
            # しない)。ただし WARN に畳まない: 記憶の追記が今回落ちている
            LOGGER.error(
                "[extraction-backlog] 拾い直しが実行できませんでした "
                "(persona=%s) — 付箋は残るので次回もう一度試みます",
                persona_id, exc_info=True,
            )

    def is_chronicle_enabled_for_persona(self, persona) -> bool:
        """Check per-persona Chronicle auto-generation toggle from DB."""
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id or not self.manager:
            return True  # fallback: enabled
        db = self.manager.SessionLocal()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return ai.CHRONICLE_ENABLED if ai else True
        finally:
            db.close()

    def is_autonomous_chronicle_enabled_for_persona(self, persona) -> bool:
        """Check per-persona toggle for Chronicle generation during non-user Pulses.

        docs/intent/memory_architecture_v2.md §6.3 (Phase 0): 自律/schedule Pulse
        では確認ダイアログが出せないため、この設定が True なら確認なしで生成を実行する。
        デフォルト True。
        """
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id or not self.manager:
            return True  # fallback: enabled
        db = self.manager.SessionLocal()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return ai.AUTONOMOUS_CHRONICLE_ENABLED if ai else True
        finally:
            db.close()

    def is_memory_weave_context_enabled(self, persona) -> bool:
        """Check per-persona Memory Weave context injection toggle from DB."""
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id or not self.manager:
            return True  # fallback: enabled
        db = self.manager.SessionLocal()
        try:
            from database.models import AI as AIModel
            ai = db.query(AIModel).filter_by(AIID=persona_id).first()
            return ai.MEMORY_WEAVE_CONTEXT if ai else True
        finally:
            db.close()

    #: persona_id が取れない persona オブジェクトの失敗理由を入れるキー。本番の
    #: 呼び出し元は全て PersonaCore (persona/core.py で persona_id を必ず持つ)
    #: なので実運用では使われない — テストの SimpleNamespace などの保険。
    _CHRONICLE_FAILURE_UNKNOWN_KEY = "__unknown__"

    @classmethod
    def _chronicle_failure_key(cls, persona_id: Optional[str]) -> str:
        return str(persona_id) if persona_id else cls._CHRONICLE_FAILURE_UNKNOWN_KEY

    def _note_chronicle_failure(
        self, persona_id: Optional[str], exc: BaseException,
    ) -> None:
        """generate_chronicle が "failed" を返す直前に、その理由を persona ごとに保持する。

        LLMError (llm_clients/exceptions.py) は ``error_code`` / ``user_message``
        を持ち、executor はチャンクの LLMError に ``batch_meta`` (落ちたチャンクの
        message_ids / 時間範囲) を付けて propagate する。それ以外の例外は
        error_code "unknown"、本文は str(exc)。
        """
        failure = {
            "error_code": getattr(exc, "error_code", None) or "unknown",
            "error": getattr(exc, "user_message", None) or str(exc),
            "error_detail": str(exc),
            "error_meta": getattr(exc, "batch_meta", None),
        }
        with self._chronicle_failures_lock:
            self._chronicle_failures[self._chronicle_failure_key(persona_id)] = failure

    def _reset_chronicle_failure(self, persona_id: Optional[str]) -> None:
        """走行の頭で、その persona の前回の理由だけを捨てる (他 persona には触れない)。"""
        with self._chronicle_failures_lock:
            self._chronicle_failures.pop(self._chronicle_failure_key(persona_id), None)

    def pop_last_chronicle_failure(
        self, persona_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """その persona の直近の generate_chronicle の失敗理由を返して消す (無ければ None)。

        手動生成のジョブ (api/routes/people/arasuji.py) が "failed" を受けた
        ときに呼び、error_code / error_meta をジョブ台帳へ写す。
        """
        with self._chronicle_failures_lock:
            return self._chronicle_failures.pop(
                self._chronicle_failure_key(persona_id), None,
            )

    def generate_chronicle(
        self,
        persona,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        force: bool = False,
        compile_groups: Optional[List[List[str]]] = None,
    ) -> str:
        """Generate Chronicle entries via episode-aligned chunk planning.

        体験の構造 工程(2) (W4): 編纂は episode 整列チャンク
        (sai_memory/arasuji/alignment.py — 恒等転写 / 恒等圧縮 / サイズ束ね)
        で行う。旧 20 件固定バッチ (ArasujiGenerator.generate_unprocessed)
        は本入口からは廃止された。

        ``force=True`` は確認ダイアログ・pulse_type 判定を経ずに即生成する。
        UI の「記憶の整理」ボタン (organize-memory API) のように、呼び出しの
        時点でユーザーが既に明示的に同意しているケース専用。Pulse の外から
        呼ばれるため ``persona._current_pulse_type`` は前回 Pulse の残留値で
        あてにならない — force はその不定性を回避する意味もある。

        ``compile_groups`` は退場時圧縮の対象 (chronicle_eviction.md §2/§5):
        指定時、**今回退場させる範囲そのもの**だけを編纂する。退場する集合と
        編纂する集合を一致させることで、下限「退場したものは必ず編纂されている」
        が手続きとして保証される。範囲は連続とは限らない (提示コンテキストの途中を畳むため)
        ので、fold ごとの message id 列を並べて渡し、離れた範囲が一つのあらすじに
        束ねられること (§4-5 連続束ねのみ) を防ぐ。自動経路
        (_run_metabolism_locked) が退場計画から渡す。force / session close 等の
        全量整理は None (現行どおり全未編纂を時系列で整列)。

        全入口 (①応答後 Metabolism ②会話前 anchor 失効 ③手動 organize-memory
        ④session close ⑤①内 sluice) が合流する一点のため、編纂の冪等
        claim (実行台帳 kind="metabolism.run"、M1 の解) はここで行う
        (beat_execution_context.md §3.2 — 編纂は persona に一度)。

        Returns:
            status 文字列。呼び出し元 (_run_metabolism_locked) が anchor 退役の
            ゲートに使う (SEA 監査 S2):

            - "ok": 編纂成功、または編纂対象なし (退役してよい)
            - "disabled": ここでは返さない (Chronicle トグルの評価は呼び出し元
              _run_metabolism_locked が行い、OFF なら本関数を呼ばない)。ただし
              非 user Pulse で AUTONOMOUS_CHRONICLE_ENABLED=False の確認スキップは
              「編纂しないことを選んでいる」ポリシー OFF なのでこれに該当する
            - "failed": 例外・LLM 失敗・環境不備 (anchor 据え置き → 次回再試行)
            - "deferred": 確認 timeout/拒否、または claim 競合 (別入口が同じ提示コンテキストを
              編纂中/編纂済み。anchor 据え置き → 次回再試行)
        """
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.alignment import (
            chronicle_band_budget,
            plan_alignment,
        )
        from saiverse.memory_weave_llm import (
            build_memory_weave_client,
            resolve_memory_weave_config,
        )

        # 走行ごとに失敗理由を捨てる — 前回の理由が今回の "failed" に化けない。
        # 捨てるのはこの persona のぶんだけ (別 persona の走行が重なっていても
        # その理由には触れない)。
        _failure_persona_id = getattr(persona, "persona_id", None)
        self._reset_chronicle_failure(_failure_persona_id)

        try:
            model_id, model_config, _weave_source = resolve_memory_weave_config(
                persona, purpose="chronicle"
            )
        except LookupError as exc:
            LOGGER.warning("[metabolism] %s (Chronicle generation)", exc)
            self._note_chronicle_failure(_failure_persona_id, exc)
            return "failed"
        client = build_memory_weave_client(model_id, model_config)

        # Initialize arasuji tables and fetch all messages
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            LOGGER.warning("[metabolism] SAIMemory not available for Chronicle generation")
            return "failed"

        # 帰化バックフィル (W4 D7): 既存 entry に coverage_chars を刻む
        # (一回きり・冪等・LLM なし)。**dry 予測より前に**実行する — dry が
        # 近似値で動くと実測 backfill 後の列のあふれ判定と食い違い、
        # 「予測 0 → 早期 return → backfill に永久に到達しない」が成立する
        # (Codex W4 三巡 #3)。帰化はメタデータ補完で確認ゲートの対象外。
        #
        # テーブルの用意も backfill も DDL / commit を伴う書き込み。共有接続
        # なので adapter の錠前の内側で行う —— ロック外の commit は他所の開いた
        # トランザクションを途中で確定させる (Codex 六巡 #2)。LLM は呼ばないので
        # 錠を持ったままでも待たせない。
        from sai_memory.arasuji.bands import backfill_coverage
        with adapter._db_lock:
            init_arasuji_tables(adapter.conn)
            try:
                backfill_coverage(adapter.conn)
            except Exception:
                LOGGER.exception("[metabolism] coverage backfill failed; continuing")

        # Fetch ALL messages suitable for Chronicle (shared filter logic).
        from sai_memory.memory.storage import get_messages_for_chronicle
        all_messages = get_messages_for_chronicle(adapter.conn)

        # 要約してよい上限 (arasuji_levels.md §16-2): 全量計画 (compile_groups
        # なし = 被覆補修 / 一括生成) は、温かい提示窓と**現在モデルの窓**の下を
        # 掘らない — 掘ると生きている会話を丸ごとあらすじにする (2026-09-03
        # 実害) か、head のあらすじ枠と生の提示の二重提示が起きる。上端の解決と
        # 絞りは見積もり (estimate_chronicle_generation_cost) と同じ関数の対
        # (resolve_compile_ceiling + clip_messages_before_position) を通す —
        # 表示と実走が違う数を言ってはならない。実走なので現在モデルの冷えた
        # 起点の前進は永続化する (persist_advance=True)。退場時圧縮
        # (compile_groups あり) は自分の温かい窓を意図して畳む経路なので対象外。
        if compile_groups is None:
            try:
                from sea.coverage_repair import resolve_compile_ceiling
                ceiling = resolve_compile_ceiling(
                    self, getattr(persona, "persona_id", None), adapter.conn,
                    persona=persona, persist_advance=True,
                )
            except Exception as exc:
                # 上端が分からないまま全量を編纂すると、温かい窓の下を掘る
                # リスクを黙って踏む — fail-closed で止める (次回再試行)。
                LOGGER.warning(
                    "[metabolism] compile ceiling resolution failed; refusing "
                    "the full-plan compile (persona=%s)",
                    getattr(persona, "persona_id", "?"), exc_info=True,
                )
                self._note_chronicle_failure(_failure_persona_id, exc)
                return "failed"
            if ceiling is not None:
                from sai_memory.memory.storage import clip_messages_before_position
                total_before = len(all_messages)
                all_messages = clip_messages_before_position(
                    adapter.conn, all_messages,
                    ceiling.created_at, ceiling.rowid,
                )
                LOGGER.info(
                    "[metabolism] compile ceiling at anchor %s (model=%s): "
                    "%d -> %d candidate messages",
                    ceiling.message_id, ceiling.model_key,
                    total_before, len(all_messages),
                )

        # 退場時圧縮 (§4-1): 編纂対象を「今回退場させる範囲そのもの」に絞る。
        # 退場する集合と編纂する集合が一致することが、下限「退場したものは必ず
        # 編纂されている」の手続き上の保証になる (chronicle_eviction.md §2)。
        run_groups: Optional[List[List[str]]] = None
        if compile_groups is not None:
            wanted = {mid for group in compile_groups for mid in group}
            all_messages = [m for m in all_messages if m.id in wanted]
            # 群の切れ目 = run の切れ目。提示コンテキストの途中を畳むと範囲は不連続になるので、
            # 離れた範囲が一つのあらすじに束ねられないようにする (§4-5)。
            # 渡すのは群の**全 id** — 「群の先頭 id の手前で切る」形は、
            # 先頭が Chronicle 除外対象 (除外タグ / line_role / Stelis) だと
            # その id が編纂対象に居らず、境界が一度も立たなかった
            # (docs/issues/archive/chronicle_run_boundary_lost_by_excluded_tag.md)。
            # 群が連続範囲であることの検算は渡す側の仕事 —
            # eviction_plan.compile_groups_from_folds (提示コンテキストの完全な
            # 並びを持つのはあちら側だけ)。
            run_groups = compile_groups

        # チャンク計画 (arasuji_levels.md §4)。processed_ids / サイズ束ねを
        # 純関数に集約 — コスト見積もり (estimate.py) と同じ計画を共有する。
        _cur = adapter.conn.execute(
            "SELECT DISTINCT json_each.value "
            "FROM arasuji_entries, json_each(source_ids_json) "
            "WHERE level = 1"
        )
        _processed_ids = {row[0] for row in _cur.fetchall()}

        plan = plan_alignment(
            all_messages,
            _processed_ids,
            target_chars=chronicle_band_budget(),
            run_groups=run_groups,
        )

        # 列のあふれ束ねの dry 予測 (Codex W4 #3/#4): 新チャンク確定後に発生する
        # 統合 LLM 回数を実行と同じ選定ロジックで数え、確認ゲートの LLM 数に
        # 含める。plan が空でも列のあふれ backlog (前回の束ね失敗の残り) があれば
        # 実行に進む — 「新チャンクが無いと列の束ねが永久に再試行されない」抜けの
        # 閉塞。
        from sai_memory.arasuji.bands import plan_band_overflow
        # 圧縮区間として提示中の digest は列の勘定・束ね対象から外す
        # (intent chronicle_consolidation §3 — dry と実行で同じ集合を渡す)。
        # None = 照会失敗 = fold の有無が不明。「fold なし」と読み替えると
        # 提示中の digest を上へ束ねて §4-1 が黙って破れるので、その回の
        # 束ねは dry / 実行とも丸ごと見送る (Codex P1-5 — 待つのは常に安全)。
        folded_entry_ids: Optional[Set[str]] = None
        try:
            folded_entry_ids = self.collect_folded_chronicle_entry_ids(
                getattr(persona, "persona_id", None)
            )
        except Exception:
            LOGGER.warning(
                "[metabolism] folded-range collection failed", exc_info=True,
            )
        if folded_entry_ids is None:
            LOGGER.warning(
                "[metabolism] folds unknown; skipping band consolidation this round",
            )

        # 極小 run の隣人吸収 (arasuji_tiny_run_absorption、2026-08-31 裁定 5):
        # 適用は**全量計画 (compile_groups=None = 被覆補修 / 一括生成) のみ**。
        # 通常の Metabolism 畳み (退場範囲の編纂) には入れない。材料 0.5U 未満の
        # run は単独で編纂せず、後ろの隣人 Lv1 を開き直して合体させる。
        # 提示中の digest (folded_entry_ids) は開けないので、fold が不明
        # (None) の回は吸収ごと見送る (束ねの見送りと同形 — 待つのは常に安全)。
        absorption_plan = None
        pending_stale_count = 0
        _stale_marker = False
        _full_plan_end_id = (
            plan.chunks[-1].messages[-1].id if plan.chunks else None
        )
        if compile_groups is None:
            from sai_memory.arasuji.absorption import (
                list_stale_upper_ids,
                plan_absorption,
                split_plan_for_absorption,
            )
            # 死んだ参照の掃除 (sweep) は tiny/stale の有無に関係なく回す
            # (Codex 五巡 H1 — 仕事ゼロだと run_absorption 自体が呼ばれず
            # 保険が眠る。検出は単一クエリで常時実行のコストは無視できる)。
            # 印が付けば下の pending_stale_count が仕事に数え、run_absorption
            # の flush が本文を語り直す。
            #
            # **計画より先** に置く (2026-09-01): 吸収の計画は隣人の
            # source_ids が全生存かを検査して開き直しを決めるので、掃除が
            # 後だと孤児参照を持つ隣人がその回は開けず、取り残しの解消が
            # 毎回 1 巡遅れる。
            #
            # sweep / stale 件数 / 未完了マーカーの照会例外は 0 / False へ
            # 潰さない (Codex 六巡 J4 — 「常時実行して仕事量に数える」保証が
            # 例外時に黙って消える)。テーブル不在の縮退は各関数の内側
            # (is_missing_table_error) が持つので、ここまで届く例外は実失敗
            # = failed で止めて再実行を促す。
            #
            # 掃除と判定は adapter の錠前の内側で一息に行う (Codex 九巡):
            # sweep は内部で commit する書き込みで、conn は adapter と共有な
            # ので、ロック外の commit は他所の開いた トランザクションを途中で
            # 確定させる (Codex 四巡 #2 — executor.execute_plan の db_lock と
            # 同じ教義)。件数と印の照会も同じ錠の中に入れる — 掃除と判定の
            # 間に他 writer が割り込むと、数えた世界と印を上げ下げする世界が
            # ずれる (run_absorption 冒頭の保守ブロックと同形)。LLM は呼ば
            # ないので、錠を持ったままでも他を待たせない。
            try:
                from sai_memory.arasuji.absorption import (
                    _sweep_broken_parents,
                    _sweep_dead_message_sources,
                    is_repair_incomplete,
                )
                with adapter._db_lock:
                    # 「上位あらすじ → 消えた下位あらすじ」の死んだ子 id。
                    _sweep_broken_parents(adapter.conn)
                    # 「Lv1 → 消えたメッセージ」も同じ理由 (UI の素の削除が
                    # 参照を直さない) で掃く。孤児参照が残っていると吸収の
                    # 再開検査が落ち、その隣の未被覆断片が永久に取り残される。
                    _sweep_dead_message_sources(adapter.conn)
                    # 前回の未完了 (content_stale の残り) は items ゼロでも
                    # flush する。未完了の印だけが残った状態 (差し替え確定後の
                    # 例外など) も仕事に数える — run_absorption が仕事ゼロを
                    # 確認して印を外す (ローカルレビュー 2026-08-31 L1: 印が
                    # 残ると帯の「前回の処理が完了していません」が永久表示に
                    # なる)。
                    pending_stale_count = len(list_stale_upper_ids(adapter.conn))
                    _stale_marker = is_repair_incomplete(adapter.conn)
            except Exception as exc:
                LOGGER.exception(
                    "[metabolism] absorption maintenance checks failed; "
                    "refusing the full-plan compile (persona=%s)",
                    getattr(persona, "persona_id", "?"),
                )
                self._note_chronicle_failure(_failure_persona_id, exc)
                return "failed"
            normal_plan, _tiny_chunks = split_plan_for_absorption(
                plan, target_chars=chronicle_band_budget(),
            )
            if _tiny_chunks and folded_entry_ids is None:
                LOGGER.warning(
                    "[metabolism] %d tiny run(s) deferred: folds unknown, "
                    "neighbors cannot be safely reopened this round",
                    len(_tiny_chunks),
                )
                plan = normal_plan
            elif _tiny_chunks:
                # 計画の例外は「見送り = ok」に潰さない (Codex 六巡 J3):
                # 潰すと極小 run が黙って捨てられ、ジョブは成功の顔で閉じる。
                # failed で返せばジョブ UI に失敗が出て再実行を促す。
                # fold 不明 (上の分岐) は設計上の見送りであって失敗ではない —
                # 従来どおり ok 側。
                try:
                    absorption_plan = plan_absorption(
                        adapter.conn, _tiny_chunks, all_messages,
                        _processed_ids,
                        target_chars=chronicle_band_budget(),
                        excluded_entry_ids=frozenset(folded_entry_ids),
                    )
                except Exception as exc:
                    LOGGER.exception(
                        "[metabolism] absorption planning failed; refusing "
                        "the full-plan compile (persona=%s)",
                        getattr(persona, "persona_id", "?"),
                    )
                    self._note_chronicle_failure(_failure_persona_id, exc)
                    return "failed"
                plan = normal_plan
        _absorption_calls = (
            absorption_plan.llm_calls if absorption_plan is not None
            else pending_stale_count
        )
        _absorption_work = (
            _absorption_calls > 0 or pending_stale_count > 0 or _stale_marker
        )

        try:
            from sai_memory.arasuji.bands import EST_PARENT_CHARS
            band_plan_count = 0 if folded_entry_ids is None else plan_band_overflow(
                adapter.conn,
                extra_leaves=[
                    (
                        c.coverage_chars,
                        min((m.created_at for m in c.messages), default=None),
                        max((m.created_at for m in c.messages), default=None),
                        EST_PARENT_CHARS,
                    )
                    for c in plan.chunks
                ],
                excluded_entry_ids=folded_entry_ids or None,
            )
        except Exception:
            LOGGER.warning("[metabolism] band overflow dry-plan failed", exc_info=True)
            band_plan_count = 0

        if not plan.chunks and band_plan_count == 0 and not _absorption_work:
            LOGGER.info(
                "[metabolism] Nothing to compile or consolidate "
                "(%d unprocessed messages)", plan.total_unprocessed,
            )
            # 編纂対象なし = claim せず no-op (退役は許す)。
            return "ok"

        unprocessed_count = plan.total_unprocessed
        estimated_llm_calls = plan.llm_calls + band_plan_count + _absorption_calls

        # 冪等 claim 用の提示コンテキストの同定: 提示コンテキスト末尾 ID = 編纂対象の時系列末尾の
        # メッセージ ID (all_messages は created_at 昇順)。失敗した提示コンテキストは
        # 同じ鍵のままでも再試行できる (claim_execution は failed 行のキーを退避して
        # 新規 prepared を作る — Codex W4 二巡 #6)。会話が進んで提示コンテキストが
        # 伸びれば ID が変わり、それはそれで新しい claim になる。
        # plan 空 (列の束ねのみ) の実行は claim しない — 束ねの並走防御は
        # bands の tx 内再検査が担う (並走時の +1 LLM コールは許容)。
        # 鍵は分割前の全量計画の末尾 (_full_plan_end_id) — 吸収へ回した極小
        # チャンクも同じ提示コンテキストの一部なので、同定から外さない。
        _window_end_id = _full_plan_end_id

        # Request user confirmation before generating.
        # Only valid when the current Pulse is a user-driven request — only
        # then is there a frontend listening for chronicle_confirm and a user
        # actually positioned to answer the dialog. Pulses spawned by
        # MetaLayer / autonomy / schedule pass an event_callback (for internal
        # event capture / progress notifications) but no UI is attached, so
        # waiting on confirm_event would just stall for the full timeout.
        #
        # docs/intent/memory_architecture_v2.md §6.3 (Phase 0, 2026-07-04):
        # 非 user pulse (auto/schedule/meta_judgment) では、ペルソナ単位の
        # AUTONOMOUS_CHRONICLE_ENABLED が True なら確認なしで生成を実行する。
        # False なら従来どおりスキップする。
        pulse_type = getattr(persona, "_current_pulse_type", None)
        if estimated_llm_calls == 0:
            # 全チャンクが恒等転写 / 恒等圧縮 (LLM コストゼロ)。確認ダイアログは
            # LLM コストへの同意なので、コストが無ければ確認なしで直行する
            # (W4 D3 — 恒等圧縮は「要約という儀式」ではなく置き直し §4-3)。
            LOGGER.info(
                "[metabolism] Generating Chronicle without confirmation "
                "(no LLM calls needed: %d chunks, %d unprocessed messages)",
                len(plan.chunks), unprocessed_count,
            )
        elif force:
            LOGGER.info(
                "[metabolism] Generating Chronicle (forced by explicit request, "
                "%d unprocessed messages)",
                unprocessed_count,
            )
        elif pulse_type != "user" and self.is_autonomous_chronicle_enabled_for_persona(persona):
            LOGGER.info(
                "[metabolism] Generating Chronicle without confirmation "
                "(pulse_type=%s, AUTONOMOUS_CHRONICLE_ENABLED=True, %d unprocessed messages)",
                pulse_type, unprocessed_count,
            )
        elif event_callback and pulse_type == "user":
            import threading as _threading

            request_id = str(uuid.uuid4())
            confirm_event = _threading.Event()
            self.manager._pending_permission_requests[request_id] = confirm_event

            persona_name = getattr(persona, "persona_name", None)
            display_model = model_config.get("display_name", model_id)

            event_callback({
                "type": "chronicle_confirm",
                "request_id": request_id,
                "unprocessed_messages": unprocessed_count,
                "total_messages": len(all_messages),
                "estimated_llm_calls": estimated_llm_calls,
                "model_name": display_model,
                "persona_name": persona_name,
            })
            LOGGER.info(
                "[metabolism] Sent chronicle_confirm: %d unprocessed messages, model=%s (id=%s)",
                unprocessed_count, display_model, request_id,
            )

            # Block until user responds or timeout (60s)
            responded = confirm_event.wait(timeout=60)
            self.manager._pending_permission_requests.pop(request_id, None)
            response = self.manager._permission_responses.pop(request_id, None)

            if not responded or response != "allow":
                reason = "timeout" if not responded else response
                LOGGER.info("[metabolism] Chronicle generation skipped (user %s)", reason)
                if event_callback:
                    event_callback({
                        "type": "warning",
                        "content": "Chronicle生成をスキップしました。",
                        "warning_code": "chronicle_skipped",
                        "display": "toast",
                    })
                return "deferred"
            LOGGER.info("[metabolism] Chronicle generation approved by user")
        else:
            # No interactive route available and AUTONOMOUS_CHRONICLE_ENABLED is
            # False (or no event_callback/manager) — skip without waiting.
            # (auto / schedule / meta_judgment pulses, or pure CLI runs)
            # 「編纂しない」ポリシー選択 = disabled 相当。deferred にすると
            # 自律 Pulse しか走らない persona の anchor が永久に据え置かれる
            # (押し出された生ログは SAIMemory に残り、後続の user Pulse /
            # session close (force=True) で編纂される)。
            LOGGER.info(
                "[metabolism] Skipping Chronicle generation confirmation "
                "(pulse_type=%s, event_callback=%s, %d unprocessed)",
                pulse_type, event_callback is not None, unprocessed_count,
            )
            return "disabled"

        # ---- 冪等 claim (実行台帳、M1 の解) ----
        # 確認ゲート通過後・LLM 実行前に claim する。(kind, idempotency_key) の
        # UNIQUE で全入口が同じ排他を通り、同じ提示コンテキストの二重編纂 (二重 LLM コスト) を
        # 収束させる。台帳が無い環境 (旧テスト / 単体実行) は claim なしで従来
        # どおり実行する (degrade)。claim 自体の失敗も degrade (編纂を止めない —
        # arasuji の source_ids スキップが事後冪等の安全網として残る)。
        #
        # metabolism.run の期限と unknown の規則 (docs/intent/execution_ledger.md
        # 「metabolism.run の期限と unknown の規則 (2026-09-03)」):
        # - 心拍: 走行中は UI へ進捗を流すたびに ledger.touch_running を呼ぶ
        #   (下の _heartbeat)。回復 tick の 1 時間期限は UPDATED_AT で測るので、
        #   心拍が無いと 1 時間を超える一括編纂 (数万通のインポート) が生きた
        #   まま unknown に落とされ、終了時の mark_failed が不正遷移で弾かれて
        #   unknown のまま残る。
        # - unknown の照合: この kind の成果物は arasuji テーブルで観測でき、
        #   確定済みチャンクは source_ids で冪等スキップされる (再実行しても
        #   LLM は再発火しない)。だから claim が unknown に当たったら、その行を
        #   照合済み (completed、キーは #unknown- 付きで退避) として閉じ、新しい
        #   claim で走る。閉じないと同じ鍵 (チャンクは古い順なので最新の未処理
        #   ID は変わらない) が永久に window_claimed になる。
        ledger = self._get_ledger()
        execution_id: Optional[str] = None
        if ledger is not None and _window_end_id is not None:
            try:
                from saiverse.execution_ledger import STATUS_UNKNOWN

                claim_key = f"{getattr(persona, 'persona_id', None)}:{_window_end_id}"
                # claim_execution: failed 行 (前回の失敗 / キャンセル) はキーを
                # 退避して新規 prepared を作る — キャンセル直後の同提示コンテキスト再実行が
                # 永久に deferred にならない (Codex W4 二巡 #6)。running /
                # applied / completed はブロック。unknown は上の規則で一度だけ
                # 照合して閉じ、再 claim する。
                execution_id, runnable, existing_status = ledger.claim_execution(
                    kind="metabolism.run",
                    idempotency_key=claim_key,
                    persona_id=getattr(persona, "persona_id", None),
                )
                if not runnable and existing_status == STATUS_UNKNOWN:
                    LOGGER.warning(
                        "[metabolism] reconciling unknown execution %s for key %s: "
                        "metabolism.run is idempotent (committed chunks are skipped "
                        "by source_ids), treating the lost run as applied and "
                        "starting a new one",
                        execution_id, claim_key,
                    )
                    # 照合の失敗は degrade しない。ここで外側の except に落ちると
                    # claim なし (追跡外) で走り、台帳上の unknown 行と追跡外の
                    # 実走行が同じ鍵で並ぶ (追跡された別の走行と競合しうる)。
                    # 照合できないなら見送る — unknown は次の claim でも同じ
                    # 規則で再び照合を試みる (照合時刻は台帳が刻む、F3)。
                    try:
                        ledger.reconcile_unknown(
                            execution_id,
                            result={
                                "reconciled": (
                                    "metabolism.run superseded; chunks idempotent "
                                    "by source_ids"
                                ),
                            },
                        )
                        execution_id, runnable, existing_status = (
                            ledger.claim_execution(
                                kind="metabolism.run",
                                idempotency_key=claim_key,
                                persona_id=getattr(persona, "persona_id", None),
                            )
                        )
                    except Exception:
                        LOGGER.warning(
                            "[metabolism] unknown reconciliation failed for key %s; "
                            "deferring (no untracked run)",
                            claim_key, exc_info=True,
                        )
                        return "deferred"
                if not runnable:
                    LOGGER.info(
                        "[metabolism] Chronicle generation skipped: window already "
                        "claimed by another entrance (persona=%s window_end=%s "
                        "execution=%s status=%s)",
                        getattr(persona, "persona_id", "?"), _window_end_id,
                        execution_id, existing_status,
                    )
                    return "deferred"
                # prepared 再利用の同時二重 claim は同じ execution_id を返しうる
                # — 席取り (prepared→running の条件付き遷移) で勝者を一意化。
                if not ledger.try_mark_running(execution_id):
                    LOGGER.info(
                        "[metabolism] Chronicle generation skipped: lost the "
                        "running seat (persona=%s execution=%s)",
                        getattr(persona, "persona_id", "?"), execution_id,
                    )
                    return "deferred"
            except Exception:
                LOGGER.warning(
                    "[metabolism] ledger claim failed; proceeding without claim",
                    exc_info=True,
                )
                execution_id = None

        # 台帳の心拍。UI へ進捗を流す場所すべてから呼ぶ (チャンク / 吸収 /
        # 束ね / 本編開始)。台帳が壊れていても本体は止めず、警告は一度だけ。
        _heartbeat_failed = [False]

        def _heartbeat() -> None:
            if ledger is None or not execution_id:
                return
            try:
                ledger.touch_running(execution_id)
            except Exception:
                if not _heartbeat_failed[0]:
                    _heartbeat_failed[0] = True
                    LOGGER.warning(
                        "[metabolism] ledger heartbeat failed (execution=%s); "
                        "further failures are not logged",
                        execution_id, exc_info=True,
                    )

        # 「Chronicleを生成しています (0/N)」はここでは出さない。前段の吸収
        # (run_absorption) が先に走る回があり、そちらは run 一件ごとに LLM を
        # 呼ぶので実機では数分〜数十分かかる。開始メッセージを先に出すと、その
        # 間ずっと「(0/410)」のまま凍って見える (2026-09-01 まはー実機報告 —
        # 実際は止まっておらず、前段だけで未被覆 410→258 まで進んでいた)。
        # 本編 (execute_plan) の直前で出す。
        def _emit_compile_start() -> None:
            _heartbeat()
            if event_callback:
                event_callback({
                    "type": "metabolism",
                    "status": "running",
                    "content": f"Chronicleを生成しています (0/{unprocessed_count})...",
                })

        # Build progress callback for streaming status to frontend
        def progress_fn(processed, total):
            _heartbeat()
            if event_callback:
                event_callback({
                    "type": "metabolism",
                    "status": "running",
                    "content": f"Chronicleを生成しています ({processed}/{total})...",
                })

        # 束ね (run_band_overflow) の畳み 1 件ごと — LLM 1 コールずつ進むので、
        # ここも画面と台帳に同じ信号を出す。束ねはチャンク確定のたびに挟まる
        # (下の _consolidate) ので、run_band_overflow が渡す「この呼び出し内の
        # (done, limit)」ではなく、走行全体の累計 / 承認済みの総予算で数える —
        # 呼び出しごとに (1/3) からやり直す表示にしない。
        _consolidated = [0]  # 走行全体で確定した束ねの累計
        _band_disabled = [False]  # LLM に届く前の失敗で、この走行の束ねを止めた印

        def band_progress_fn(done, _total):
            _heartbeat()
            if event_callback:
                event_callback({
                    "type": "metabolism",
                    "status": "running",
                    "content": (
                        "上位のあらすじを束ねています "
                        f"({_consolidated[0] + done}/{band_plan_count})..."
                    ),
                })

        # Build cancellation check from cancellation token
        cancel_fn = None
        if cancellation_token:
            cancel_fn = lambda: cancellation_token.is_cancelled()

        persona_id_str = getattr(persona, "persona_id", None)

        # Ensure persona pages for conversation partners
        current_building_id = getattr(persona, "current_building_id", None)
        if current_building_id:
            try:
                occupants = getattr(persona, "occupants", {})
                building_occupants = occupants.get(current_building_id, [])
                if building_occupants:
                    id_to_name_map = getattr(persona, "id_to_name_map", {})
                    history_manager = getattr(persona, "history_manager", None)

                    for occupant_id in building_occupants:
                        # 自分自身のみスキップ。ユーザーは意図的に含める (まはー裁定
                        # 2026-07-11: 対ペルソナと同様にユーザーのページも持つ)。
                        # 旧ガード `startswith("user_")` はユーザーの occupant id が
                        # 素の数値 (例: "1") のため一度もマッチしていなかった —
                        # 事故的に正しく動いていた挙動を正式仕様化した。
                        if occupant_id == persona_id_str:
                            continue

                        # Get persona name from id_to_name_map
                        occupant_name = id_to_name_map.get(occupant_id, occupant_id)

                        # Ensure Memopedia page exists
                        if history_manager:
                            success = history_manager.ensure_persona_page(occupant_id, occupant_name)
                            if success:
                                LOGGER.debug(
                                    "[metabolism] Ensured Memopedia page for persona=%s (name=%s)",
                                    occupant_id,
                                    occupant_name,
                                )
                            else:
                                LOGGER.warning(
                                    "[metabolism] Failed to ensure Memopedia page for persona=%s",
                                    occupant_id,
                                )
            except Exception:
                LOGGER.exception("[metabolism] Failed to ensure persona pages for conversation partners")

        # Entity extraction callback (extracts entities → reflects to Memopedia)
        note_callback = None
        try:
            from sai_memory.memory.entity_extractor import make_batch_callback as make_entity_callback
            note_callback = make_entity_callback(
                client, adapter.conn,
                persona_id=persona_id_str,
                # adapter と同じ錠前で書く (渡さなくても DB ファイルの錠前に
                # なるが、付箋の書き込みなど Memopedia を通らない経路にも要る)
                db_lock=adapter._db_lock,
            )
        except Exception as exc:
            LOGGER.warning("[metabolism] Entity extraction setup failed: %s", exc)

        # (付箋 backlog の拾い直しは Metabolism の頭 —— `_retry_extraction_backlog`。
        #  ここに置くと「編纂対象なし」の早期 return の向こう側になり、静かな夜が
        #  続くと永久に回収されない: Sol レビュー 2026-08-06 F4)

        from sai_memory.arasuji.bands import run_band_overflow
        from sai_memory.arasuji.executor import execute_plan

        # 吸収の実行 (全量計画のみ)。通常チャンクの編纂 (execute_plan) より先
        # — 歯抜けの古い区間を先に治し、上位あらすじを新しくしてから後続の
        # 生成に「これまでの流れ」を引かせる (裁定 4 の時系列たたみ込み)。
        if _absorption_work:
            from sai_memory.arasuji.absorption import run_absorption

            # 前段は本編より長くなることがあるので、フェーズと進行を画面へ流す
            # (2026-09-01)。ここが無言だと「Chronicleを生成しています (0/N)」で
            # 凍って見え、ユーザーが止まったと判断して中止してしまう。
            _heartbeat()
            if event_callback:
                event_callback({
                    "type": "metabolism",
                    "status": "running",
                    "content": (
                        "編纂の下ごしらえをしています"
                        "（取り残された断片の吸収と、上位あらすじの語り直し）..."
                    ),
                })

            def absorption_progress_fn(phase, done, total):
                _heartbeat()
                if not event_callback:
                    return
                if phase == "absorb":
                    content = f"取り残された断片を吸収しています ({done}/{total})..."
                else:
                    content = f"上位のあらすじを語り直しています ({total} 件)..."
                event_callback({
                    "type": "metabolism",
                    "status": "running",
                    "content": content,
                })

            try:
                absorption_result = run_absorption(
                    adapter.conn, client, absorption_plan,
                    persona_id=persona_id_str,
                    db_lock=adapter._db_lock,
                    cancel_check=cancel_fn,
                    batch_callback=note_callback,
                    progress_callback=absorption_progress_fn,
                )
            except Exception as exc:
                LOGGER.exception("[metabolism] tiny-run absorption failed")
                self._note_chronicle_failure(_failure_persona_id, exc)
                if ledger is not None and execution_id:
                    try:
                        ledger.mark_failed(
                            execution_id, str(exc) or type(exc).__name__,
                        )
                    except Exception:
                        LOGGER.warning(
                            "[metabolism] ledger mark_failed failed",
                            exc_info=True,
                        )
                return "failed"
            if absorption_result.cancelled:
                if ledger is not None and execution_id:
                    try:
                        ledger.mark_failed(execution_id, "cancelled by user")
                    except Exception:
                        LOGGER.warning(
                            "[metabolism] ledger mark_failed failed",
                            exc_info=True,
                        )
                LOGGER.info(
                    "[metabolism] absorption cancelled (%d merges committed)",
                    len(absorption_result.merged_entries),
                )
                return "deferred"
            if (
                absorption_result.merged_entries
                or absorption_result.regenerated_upper_ids
            ):
                LOGGER.info(
                    "[metabolism] absorption done: %d merged, %d reopened, "
                    "%d upper regenerated, %d unresolved run(s)",
                    len(absorption_result.merged_entries),
                    len(absorption_result.reopened_entry_ids),
                    len(absorption_result.regenerated_upper_ids),
                    absorption_result.unresolved_runs,
                )

        # 本編の開始をここで報告する (前段の吸収を跨いだ後)。
        _emit_compile_start()

        # 束ね (chronicle_consolidation): 未束ねの字数が発火閾値を超えたら、
        # 古い側を 1 個の親に畳んで上のレベルへ送る (bands.run_band_overflow)。
        #
        # 順序 (2026-09-03 まはー裁定): 束ねは**チャンクが確定するたび**に挟み、
        # 走行の最後にもう一度呼ぶ。各チャンクのプロンプトに載る「これまでの
        # 流れ」(context.get_episode_context_for_timerange) は確定済みあらすじを
        # 新しい側から 20 件辿り、近い過去はレベル1、遠い過去はレベル2 以上で
        # 読む — 階層があって初めて 20 件で全史を覆える設計。束ねを最後に一度
        # だけにすると、大量編纂 (数万通のインポート修復) の後半チャンクは
        # 直前 20 件のレベル1 しか見えず、それより前の流れを失っていた。W4
        # 移行前の generator はバッチごとに maybe_consolidate を呼んでいて、
        # 移行 (2026-07-21) がこの挟み込みを落としていた (回帰)。
        #
        # 予算: 走行全体の畳み回数は確認ゲートに提示した dry 件数
        # (band_plan_count) を超えない — 実出力長のブレで連鎖が増えても承認
        # 回数を超えない。各呼び出しには残り予算だけを渡す。超過の無い回は
        # run_band_overflow が 1 クエリの前検査で抜ける (LLM も並びの読み直しも
        # 走らない)。
        #
        # 束ね失敗は編纂の成否に含めない (一次あらすじは確定済み = 情報の欠落は
        # なく、次回の Metabolism の dry 予測が backlog を検出して自然に再試行
        # する)。batch_callback は恒等圧縮の子が初めて要約に変わる瞬間の
        # Fragment 抽出 (intent §7)。束ね側の抽出失敗は下の器に積み、
        # execute_plan の結果へ合流させて executor 側とまとめて報告する
        # (ERROR ログ / 台帳 / 画面通知)。
        band_extraction_failures: List[str] = []
        band_extraction_failures_unrecorded: List[str] = []

        def _consolidate(reason: str) -> None:
            # 台帳の心拍は成否に依らず 1 回打つ (finally)。失敗した畳みは進捗
            # イベントを出さないので、progress 経由の心拍だけだとプロバイダ障害
            # の間の走行が「観測途絶」に見える。
            try:
                if folded_entry_ids is None or _consolidated[0] >= band_plan_count:
                    return
                if _band_disabled[0]:
                    return
                # 予算は**試行回数**で消費する (Codex 指摘 2026-09-03 high):
                # run_band_overflow の戻り値は確定数で、LLM 失敗・空応答・
                # tx 内再検査の放棄は 0 で返る。成功数だけを累計すると超過が
                # 残ったまま次の after_chunk が同じ残り予算で同じ畳みを再試行し、
                # 障害の間の課金回数が承認件数で縛られない。stats は失敗の
                # 途中でも書かれているので、例外で抜けた回も同じ規則で数える。
                stats: Dict[str, int] = {"attempts": 0, "created": 0}
                folded = 0
                try:
                    folded = run_band_overflow(
                        adapter.conn, client,
                        persona_id=persona_id_str,
                        cancel_check=cancel_fn,
                        excluded_entry_ids=folded_entry_ids or None,
                        batch_callback=note_callback,
                        max_folds=band_plan_count - _consolidated[0],
                        extraction_failures=band_extraction_failures,
                        db_lock=adapter._db_lock,
                        extraction_failures_unrecorded=(
                            band_extraction_failures_unrecorded
                        ),
                        progress_callback=band_progress_fn,
                        stats=stats,
                    )
                except Exception:
                    LOGGER.exception("[bands] consolidation failed; continuing")
                    if not int(stats.get("attempts") or 0):
                        # LLM に届く前 (前検査・並びの読み直し・計画) で転んだ =
                        # 予算は減らないが、原因は次のチャンクでも同じなので、
                        # この走行では束ねを止める。毎チャンク同じ例外を吐き
                        # 続けない (ローカルレビュー 2026-09-03)。残りは次回の
                        # 走行の dry 予測が数え直す。
                        _band_disabled[0] = True
                        LOGGER.warning(
                            "[bands] consolidation disabled for the rest of this "
                            "run after a failure before any LLM attempt (%s)",
                            reason,
                        )
                created = max(int(folded or 0), int(stats.get("created") or 0))
                attempts = int(stats.get("attempts") or 0)
                consumed = max(attempts, created)
                _consolidated[0] += consumed
                if attempts > created:
                    LOGGER.warning(
                        "[bands] %d fold attempt(s) failed in this call (%s); "
                        "budget consumed anyway (%d/%d)",
                        attempts - created, reason,
                        _consolidated[0], band_plan_count,
                    )
                if created:
                    LOGGER.debug(
                        "[bands] consolidated %d (%s, %d/%d in this run)",
                        created, reason, _consolidated[0], band_plan_count,
                    )
            finally:
                _heartbeat()

        def _merge_band_failures(exec_result) -> None:
            """束ね側の抽出失敗を execute_plan の結果へ移す (二重計上しない)。"""
            if band_extraction_failures:
                exec_result.extraction_failures.extend(band_extraction_failures)
                band_extraction_failures.clear()
            if band_extraction_failures_unrecorded:
                exec_result.extraction_failures_unrecorded.extend(
                    band_extraction_failures_unrecorded
                )
                band_extraction_failures_unrecorded.clear()

        def _report_extraction_failures_early(
            failures: List[str], unrecorded: List[str], what: str,
        ) -> str:
            """走行が正常終了しない経路 (execute_plan の raise / キャンセル) での
            抽出失敗の報告。

            正常終了なら下の報告ブロックが executor 側とまとめて拾うが、
            それらの経路はそこへ届かない。確定済みの分の失敗は事実として
            残っているので、ここで同じ言い回しの ERROR を出し、台帳の失敗理由
            にも添える (戻り値はその接尾辞。無ければ空)。
            """
            recorded = [e for e in failures if e not in unrecorded]
            unrecorded_ = list(unrecorded)
            if recorded:
                LOGGER.error(
                    "[metabolism] entity extraction failed for %d %s "
                    "(entries=%s) — 付箋 (backlog) に記録済み。"
                    "次回の Metabolism の頭で拾い直す",
                    len(recorded), what, ",".join(e[:8] for e in recorded),
                )
            if unrecorded_:
                LOGGER.error(
                    "[metabolism] entity extraction failed for %d %s "
                    "(entries=%s) — **付箋にも残せなかった**。"
                    "この範囲の知識は自動では拾い直されない (手動の再構築が要る)",
                    len(unrecorded_), what,
                    ",".join(e[:8] for e in sorted(unrecorded_)),
                )
            if not recorded and not unrecorded_:
                return ""
            label = what.split()[0]  # "band consolidations" -> "band", "entries" -> "entries"
            return (
                f" ({label} extraction failures: {len(recorded)} recorded, "
                f"{len(unrecorded_)} unrecorded)"
            )

        def _report_band_failures_on_abort() -> str:
            return _report_extraction_failures_early(
                band_extraction_failures, band_extraction_failures_unrecorded,
                "band consolidations",
            )

        def _finish_cancelled(exec_result) -> str:
            """キャンセル終端 — 部分適用を completed で封印しない。

            completed で封印すると同じ提示コンテキストが再実行不能になる
            (冪等マーカーは適用の成功だけを封印する — W3 教訓③ / Codex W4
            #8)。failed 終端で claim を退け、anchor は据え置く。
            executor がチャンク境界で拾ったキャンセルも、最後のチャンクの後
            (after_chunk の束ね・最後の束ねの最中) に押されたキャンセルも、
            同じ終端に落とす — 後者を "ok" で閉じると、束ねの残りが無言で
            切り捨てられたまま completed に封印される。
            """
            # キャンセルは正常終了の報告ブロックへ届かないので、確定済みの
            # チャンクと挟み込み済みの束ねの抽出失敗 (_merge_band_failures 済み)
            # をここで表へ出す (ローカルレビュー 2026-09-03: 付箋に残せなかった分
            # の唯一の通知が消えていた)。
            suffix = _report_extraction_failures_early(
                list(exec_result.extraction_failures),
                list(exec_result.extraction_failures_unrecorded),
                "entries",
            )
            if ledger is not None and execution_id:
                try:
                    ledger.mark_failed(execution_id, "cancelled by user" + suffix)
                except Exception:
                    LOGGER.warning("[metabolism] ledger mark_failed failed", exc_info=True)
            LOGGER.info(
                "[metabolism] Chronicle generation cancelled (%d chunks committed)",
                exec_result.created_count,
            )
            return "deferred"

        try:
            exec_result = execute_plan(
                plan, client, adapter.conn,
                persona_id=persona_id_str,
                progress_callback=progress_fn,
                cancel_check=cancel_fn,
                batch_callback=note_callback,
                # 抽出失敗の付箋も adapter と同じ錠前で書く (Codex 四巡 #2)
                db_lock=adapter._db_lock,
                after_chunk=lambda done, total: _consolidate("after_chunk"),
            )
        except Exception as exc:
            LOGGER.exception("[metabolism] Chronicle generation raised")
            # LLMError の error_code / user_message / batch_meta (落ちたチャンク
            # のメッセージ id) を手動生成のジョブ UI へ届ける。
            self._note_chronicle_failure(_failure_persona_id, exc)
            # 途中のチャンクまでに挟んだ束ねの抽出失敗は、正常終了の報告
            # ブロックへ届かないのでここで表へ出す。
            band_suffix = _report_band_failures_on_abort()
            if ledger is not None and execution_id:
                try:
                    # 部分生成 (途中チャンクまで確定済み) はあり得るが、確定済み
                    # チャンクは source_ids で冪等スキップされるため再試行は安全。
                    ledger.mark_failed(
                        execution_id,
                        (str(exc) or type(exc).__name__) + band_suffix,
                    )
                except Exception:
                    LOGGER.warning("[metabolism] ledger mark_failed failed", exc_info=True)
            return "failed"

        # チャンクの合間に挟んだ束ねの抽出失敗を、この時点で結果へ合流させる
        # (キャンセルで早期 return する経路でも、確定した束ねの失敗は結果に残す)。
        _merge_band_failures(exec_result)

        # executor はチャンクの頭でしかキャンセルを見ない。最後のチャンクが確定
        # した後 (after_chunk の束ねの最中) に押されたキャンセルは
        # exec_result.cancelled に乗らないので、ここでもう一度トークンを見る。
        if exec_result.cancelled or (cancel_fn is not None and cancel_fn()):
            return _finish_cancelled(exec_result)

        # 最後の束ね — 末尾のチャンクぶんの超過を畳む。チャンクが無く束ねだけの
        # 走行 (plan 空 + band backlog) もここで従来どおり実行される。
        _consolidate("final")
        _merge_band_failures(exec_result)
        consolidated_count = _consolidated[0]

        # 最後の束ねの最中に押されたキャンセル (run_band_overflow は畳みの合間で
        # 抜けるだけで、キャンセルを戻り値に乗せない)。
        if cancel_fn is not None and cancel_fn():
            return _finish_cancelled(exec_result)

        LOGGER.info(
            "[metabolism] Chronicle generation complete: %d chunks created "
            "(%d skipped as duplicates), %d band consolidations",
            exec_result.created_count, exec_result.skipped_duplicates,
            consolidated_count,
        )
        # 抽出の失敗は Chronicle の成否に畳み込まない (チャンクは確定済みで、
        # failed 再実行しても冪等スキップにより抽出は再発火しない = 嘘の失敗)。
        # 代わりに握り潰さず表へ出す: ERROR ログ + 台帳 + 画面通知
        # (docs/issues/memopedia_writers_bypass_adapter_lock.md)。
        unrecorded = set(exec_result.extraction_failures_unrecorded)
        if exec_result.extraction_failures:
            recorded = [
                e for e in exec_result.extraction_failures if e not in unrecorded
            ]
            if recorded:
                LOGGER.error(
                    "[metabolism] entity extraction failed for %d chunks (entries=%s) — "
                    "付箋 (backlog) に記録済み。次回の Metabolism の頭で拾い直す",
                    len(recorded), ",".join(e[:8] for e in recorded),
                )
            if unrecorded:
                # 付箋に残せなかった分は拾い直しの対象にならない。
                # 「次回やり直します」と一緒くたに報告してはいけない
                LOGGER.error(
                    "[metabolism] entity extraction failed for %d chunks "
                    "(entries=%s) — **付箋にも残せなかった**。この範囲の知識は"
                    "自動では拾い直されない (手動の再構築が要る)",
                    len(unrecorded), ",".join(e[:8] for e in sorted(unrecorded)),
                )
        if ledger is not None and execution_id:
            try:
                result_payload = dict(plan.summary)
                result_payload.update({
                    "created": exec_result.created_count,
                    "skipped_duplicates": exec_result.skipped_duplicates,
                    "cancelled": exec_result.cancelled,
                    "bands_consolidated": consolidated_count,
                })
                if exec_result.extraction_failures:
                    result_payload["extraction_failures"] = list(
                        exec_result.extraction_failures
                    )
                if unrecorded:
                    result_payload["extraction_failures_unrecorded"] = sorted(
                        unrecorded
                    )
                ledger.mark_applied(execution_id, result=result_payload)
                # outbox を積まない実行なので completed へ明示遷移して閉じる。
                ledger.mark_completed(execution_id)
            except Exception:
                LOGGER.warning("[metabolism] ledger apply/complete failed", exc_info=True)

        # Notify frontend that generation is complete
        if event_callback:
            content = f"Chronicle生成完了: {exec_result.created_count}件のエントリを作成しました。"
            retriable = len(exec_result.extraction_failures) - len(unrecorded)
            if retriable > 0:
                content += (
                    f"⚠ うち {retriable} 件で知識の"
                    "書き出しに失敗しました。次回の記憶の整理で自動的にやり直します。"
                )
            if unrecorded:
                # ここで「やり直します」と言えない相手。嘘をつかない
                content += (
                    f"⚠ うち {len(unrecorded)} 件は書き出しに失敗したうえ、"
                    "やり直しの記録も残せませんでした。この範囲は自動では"
                    "やり直せません（ログを確認してください）。"
                )
            event_callback({
                "type": "metabolism",
                "status": "completed",
                "content": content,
            })
        return "ok"

    def ensure_recall_embeddings(self, persona) -> None:
        """Chronicle / Memopedia ページ / Fragment の未埋め込み分を全件埋める。

        自動想起 (ゾーン C) の想起インフラ整備。ローカル埋め込みのみで API コスト
        ゼロのため、Metabolism のたびに**無条件で**実行する (Chronicle 生成の
        成否・トグルに相乗りさせない)。

        歴史的経緯: かつてこの処理は generate_chronicle の末尾にあり、生成の
        早期 return (未処理なし / 20件揃った run なし / 自律 Pulse の確認スキップ /
        ユーザーの確認拒否) のたびに巻き添えでスキップされ、埋め込みバックログが
        恒常的に溜まっていた (2026-07-04 に air_city_a で Fragment 778/1638 を確認)。
        記憶アーキv2 §4.1 運用ノート参照。
        """
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready() or not adapter.can_embed():
            return
        try:
            from sai_memory.unified_recall import (
                embed_chronicle_entries,
                embed_memopedia_fragments,
                embed_memopedia_pages,
            )
            from sai_memory.arasuji import init_arasuji_tables
            from sai_memory.memopedia import init_memopedia_tables
            # テーブルの用意は commit を伴う。共有接続なので adapter のロックの
            # 内側で行う (ロック外の commit は他所の開いた tx を途中で確定させる)
            with adapter._db_lock:
                init_arasuji_tables(adapter.conn)
                init_memopedia_tables(adapter.conn)
            # 埋め込みの計算は錠の外、保存だけ錠の内側 (db_lock を渡す)。
            # 共有接続なので、錠外の commit は他所の開いたトランザクションを
            # 途中で確定させる (Codex 八巡 #1)
            n_chr = embed_chronicle_entries(
                adapter.conn, adapter.embedder, level=1, db_lock=adapter._db_lock,
            )
            n_page = embed_memopedia_pages(
                adapter.conn, adapter.embedder, db_lock=adapter._db_lock,
            )
            n_frag = embed_memopedia_fragments(
                adapter.conn, adapter.embedder, db_lock=adapter._db_lock,
            )
            if n_chr or n_page or n_frag:
                LOGGER.info(
                    "[metabolism] Embeddings generated: chronicle=%d, pages=%d, fragments=%d",
                    n_chr, n_page, n_frag,
                )
        except Exception:
            LOGGER.exception("[metabolism] Embedding generation failed")


def _marker_advance_is_safe(
    current_messages: List[Dict[str, Any]],
    seen_ids: List[str],
    seen_end_id: Optional[str],
) -> bool:
    """パンマーカーを seen_end_id へ進めてよいか (未提示メッセージを跨がないか)。

    窓 (current_messages) の並びで seen_end_id 以前にある全メッセージが seen に
    含まれるときだけ True。跨ぐ形でマーカーが進むと、跨がれた未提示メッセージは
    次回の担当範囲 (マーカー以降) からも漏れて永遠に採取されない (Codex 第五巡
    修正 2 の芯)。seen_end_id が無い (マーカーは進まない) なら常に安全。
    seen_end_id が窓に見つからない場合は安全側の False (確定保留)。
    """
    if not seen_end_id:
        return True
    seen = {str(message_id) for message_id in seen_ids}
    target = str(seen_end_id)
    ids = [
        str(m.get("id")) for m in current_messages
        if isinstance(m, dict) and m.get("id")
    ]
    end_pos: Optional[int] = None
    for index in range(len(ids) - 1, -1, -1):  # 複数一致は後勝ち
        if ids[index] == target:
            end_pos = index
            break
    if end_pos is None:
        return False
    return all(message_id in seen for message_id in ids[: end_pos + 1])


def _eviction_within_seen(plan, seen_ids: List[str]) -> bool:
    """退場計画の対象 ID 全件がスルースの見た集合に含まれるかを検算する。

    v3 §13.3 の不変条件 (全経験は退場前に一度本人の目を通る) の退場側の検算。
    末尾位置の勘定 (代理指標) ではなく **ID 集合の包含**で見る — コールド実行で
    _prepare_context が anchor を前進させ、退場計画の土台とスルース入力がズレる
    並び (Codex 第三巡 修正 1) は末尾勘定では拾えない。1 件でも欠ければ False
    (退場見送り)。次回の Metabolism でスルースが今の窓を見れば自然に通る。
    """
    seen = {str(message_id) for message_id in seen_ids}
    for fold in plan.folds:
        for message_id in fold.message_ids:
            if str(message_id) not in seen:
                return False
    return True


def collect_folded_chronicle_entry_ids(
    manager, persona_id: Optional[str],
) -> Optional[Set[str]]:
    """persona の全 model 行の圧縮区間が提示中の Chronicle entry id 集合。

    束ね (sai_memory/arasuji/bands.py) は提示コンテキストに置き換え表示中の
    digest を列の勘定・束ね対象から外す (intent chronicle_consolidation §3 —
    提示中のものを上へ畳まない §4-1 の帰結)。head の除外 (memory_weave
    section) が単一 model 行で読むのに対し、束ねは persona 単位の処理なので
    全 model 行を集約する。SessionLifecycle を持たない API 生成ジョブと共有
    するためモジュール関数 (manager 経由の read-only)。

    戻り値の意味 (Codex レビュー P1-5 — 失敗を空集合に潰さない):

    - ``set(...)`` = 集約に成功した (空集合 = 提示中の fold は無い)
    - ``None`` = **照会に失敗した = fold の有無が分からない**。呼び出し側は
      「fold なし」と読み替えず、その回の束ねを見送ること — 提示中の digest
      を知らずに上へ束ねると §4-1 (提示中のものを畳まない) が黙って破れる。
      待つのは常に安全。
    - manager / world DB が無い環境 (テスト等) は「fold という概念ごと無い」
      ので正当な空集合。
    """
    if manager is None or not hasattr(manager, "SessionLocal") or not persona_id:
        return set()
    from sea.session_window import deserialize_folds
    ids: Set[str] = set()
    db = manager.SessionLocal()
    try:
        from database.models import SessionAnchor
        rows = db.query(SessionAnchor).filter_by(PERSONA_ID=persona_id).all()
        for row in rows:
            folded = getattr(row, "FOLDED_RANGES_JSON", None)
            if not folded:
                continue
            # deserialize_folds は壊れた JSON を空リストへ縮退させる
            # (test_session_window_folds で固定済み) — ここでの例外は想定外の
            # 破損なので「分からない」に倒す。
            for fold in deserialize_folds(folded):
                ids.update(fold.chronicle_entry_ids)
    except Exception as exc:
        LOGGER.warning(
            "[bands] folded-range collection failed for %s: %s "
            "(folds unknown — caller must skip consolidation this round)",
            persona_id, exc,
        )
        return None
    finally:
        db.close()
    return ids


def remove_folds_referencing_entry(
    manager, persona_id: Optional[str], entry_id: str,
) -> int:
    """persona の全 model 行から、指定 Chronicle エントリを指す圧縮区間の記録を外す。

    あらすじエントリの手動削除 (api/routes/people/arasuji.py) の道連れ処理。
    エントリだけ消して記録が残ると「記録は生きているのに指す先のあらすじが無い」
    半端な状態になり、提示は生ログに倒れる (fail-open) のに適用側の二重記録判定
    がその範囲の再畳みを拒否して anchor が恒久に詰まる
    (docs/issues/chronicle_eviction_applier_veto_deadlock.md 顔その2)。
    派生状態 (圧縮区間の記録) の無効化は、元 (エントリ) を消す側が同じ操作の
    中で行う。

    記録は**丸ごと**外す (エントリ id を 1 本だけ抜いて残さない) — 複数エントリを
    指す記録から 1 本抜くと、残りの digest が範囲全体の顔をして、消したエントリ
    ぶんの体験が黙って隠れるため。外された範囲は生ログに戻り、次の Metabolism が
    再畳みする (生き残ったエントリは再編纂されず引き当て直される)。

    Returns:
        外した記録の数。照会・書き込みに失敗したら 0 (Metabolism 時の安全網
        ``SessionLifecycle._drop_dead_folds`` が後から拾う)。
    """
    if manager is None or not hasattr(manager, "SessionLocal") or not persona_id:
        return 0
    from sea.beat_gate import hold_beat
    from sea.session_window import deserialize_folds, serialize_folds
    removed = 0
    try:
        # Beat ロックで Metabolism (save_folded_ranges) と直列化する。行の
        # FOLDED_RANGES_JSON は列まるごとの read-modify-write なので、並走
        # すると後勝ちで一方の書き込みが黙って消える (Codex レビュー 2026-07-27)。
        # check_gate=False: これは Beat (認知の一巡) ではなく保守書き込みなので、
        # 関所 (pending flush) は通さずロックだけ取る。
        with hold_beat(
            manager, persona_id, purpose="chronicle_entry_delete",
            check_gate=False,
        ):
            db = manager.SessionLocal()
            try:
                from database.models import SessionAnchor
                rows = db.query(SessionAnchor).filter_by(PERSONA_ID=persona_id).all()
                for row in rows:
                    payload = getattr(row, "FOLDED_RANGES_JSON", None)
                    if not payload:
                        continue
                    try:
                        # 厳格に読む — 形の壊れた記録を寛容に読んで書き戻すと、
                        # 読めなかった属性が黙って落ちる (Codex 八巡目 #5)。読めない
                        # 行は触らない (Metabolism 時の安全網 _drop_dead_folds が拾う)。
                        folds = deserialize_folds(payload, strict=True)
                    except ValueError:
                        LOGGER.warning(
                            "[metabolism] folded ranges of persona=%s model=%s are "
                            "unreadable; leaving the row untouched while removing "
                            "entry %s", persona_id, getattr(row, "MODEL_KEY", "?"),
                            entry_id, exc_info=True,
                        )
                        continue
                    kept = [
                        f for f in folds
                        if str(entry_id) not in f.chronicle_entry_ids
                    ]
                    if len(kept) == len(folds):
                        continue
                    row.FOLDED_RANGES_JSON = serialize_folds(kept)
                    removed += len(folds) - len(kept)
                if removed:
                    db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
    except Exception:
        LOGGER.warning(
            "[metabolism] failed to remove folded ranges referencing chronicle "
            "entry %s (persona=%s); the metabolism-time sweep will catch them",
            entry_id, persona_id, exc_info=True,
        )
        return 0
    if removed:
        LOGGER.info(
            "[metabolism] removed %d folded-range record(s) referencing deleted "
            "chronicle entry %s (persona=%s)", removed, entry_id, persona_id,
        )
    return removed
