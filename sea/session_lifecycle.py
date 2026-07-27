from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple

from sea.cancellation import CancellationToken
from sea.eviction_plan import (
    Watermarks,
    compile_groups_from_folds,
    message_chars,
    plan_eviction,
)

if TYPE_CHECKING:
    from sea.runtime import SEARuntime
    from sea.session_window import FoldedRange, SessionWindow

LOGGER = logging.getLogger(__name__)


class SessionLifecycle:
    """Anchor / Metabolism / Chronicle — Session (短期記憶) の節目管理。

    docs/intent/session.md の「Session 統一制御単位」の実装先。
    session_lifecycle_extraction_design.md Step 1 で SEARuntime から抽出した。
    """

    def __init__(self, runtime: "SEARuntime", manager_ref: Any) -> None:
        self.runtime = runtime      # 過渡期の後方参照 (設計書 §4 で削減)
        self.manager = manager_ref

    def get_metabolism_watermarks(
        self, persona, model_key: Optional[str] = None,
    ) -> Optional[Watermarks]:
        """Metabolism の三水位 (文字数) を解決する。

        docs/intent/chronicle_eviction.md §4。水位はモデル依存
        (beat_execution_context.md §3.2 — 各 Session は自分の model の閾値で
        自分の提示コンテキストを管理する)。``model_key`` は実行 model。None なら従来どおり
        ``persona.model`` にフォールバックする。

        manager の override (グローバル設定 UI) が最優先。model が解決できない
        場合だけ None を返す (= Metabolism を回せない)。
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

        def _override(name: str):
            return getattr(self.manager, name, None) if self.manager else None

        low = _override("metabolism_low_chars_override")
        if low is None:
            low = get_metabolism_low_chars(model_name)
        target = _override("metabolism_target_chars_override")
        if target is None:
            target = get_metabolism_target_chars(model_name)
        high = _override("metabolism_high_chars_override")
        if high is None:
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
    ) -> List["FoldedRange"]:
        """(persona, model) の提示コンテキストに空いている圧縮区間を読む (chronicle_eviction.md §6)。"""
        from sea.session_window import deserialize_folds
        entry = self.load_anchor_entry(persona_id, model_key)
        return deserialize_folds(entry.get("folded_ranges") if entry else None)

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
    ) -> None:
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

        docs/intent/cache_lifecycle_control.md §5.2
        """
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return
        if not persona_id or not model_key:
            return

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

            if row is None:
                row = SessionAnchor(PERSONA_ID=persona_id, MODEL_KEY=str(model_key))
                db.add(row)
            new_anchor_id = entry.get("anchor_id")
            if row.ANCHOR_MESSAGE_ID != new_anchor_id and row.FOLDED_RANGES_JSON:
                # 畳んだ範囲は「この anchor 以降の提示コンテキスト」に対する記録なので、anchor が
                # 差し替わった時点で無効になる (chronicle_eviction.md §6)。退場経路
                # 以外でも anchor は動く — TTL 失効後の最小ロードで新しい起点が立ち、
                # LLM 成功後の touch がそれを永続化する。古い圧縮区間を残すと、提示コンテキストには
                # 出ないのに head の Chronicle 枠からは除外され続け、その体験が
                # どこにも現れなくなる。正規の退場経路は anchor 前進の直後に
                # 圧縮区間を書き直すので、ここでクリアしても無傷。
                LOGGER.info(
                    "[metabolism] anchor moved outside the eviction path "
                    "(%s -> %s); clearing folded ranges for %s/%s",
                    row.ANCHOR_MESSAGE_ID, new_anchor_id, persona_id, model_key,
                )
                row.FOLDED_RANGES_JSON = None
            row.ANCHOR_MESSAGE_ID = new_anchor_id
            row.TTL_SECONDS = effective_ttl
            row.UPDATED_AT = int(effective_updated.timestamp())
            db.commit()
        except Exception as exc:
            LOGGER.warning(
                "[metabolism] Failed to upsert anchor entry for %s/%s: %s",
                persona_id, model_key, exc,
            )
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

    def resolve_metabolism_anchor(self, persona, model_key: Optional[str] = None) -> tuple:
        """Resolve the best metabolism anchor using 3-level fallback.

        Args:
            model_key: 「自 model」として扱う model。ExecutionContext が届いている
                呼び出し元は ``execution_context.model_key`` を明示で渡す
                (beat_execution_context.md §3.1)。None なら従来どおり
                ``persona.model`` (読み側の全面 model 化は §6-5 のスコープ)。

        Returns:
            (anchor_id, resolution_type) where resolution_type is
            "self" | "other" | "minimal".
            anchor_id is None for "minimal" (no valid anchor found).
        """
        persona_model = model_key or getattr(persona, "model", None)
        if not persona_model:
            return (None, "minimal")
        persona_model = str(persona_model)

        anchors = self.load_anchor_entries(getattr(persona, "persona_id", None))
        now = datetime.now()

        # Case 1: self model's anchor exists and is valid
        self_entry = anchors.get(persona_model)
        if self_entry:
            try:
                updated_at = datetime.fromisoformat(self_entry["updated_at"])
                validity = self.anchor_entry_ttl_seconds(self_entry, persona_model, getattr(persona, "persona_id", None))
                age = (now - updated_at).total_seconds()
                if age <= validity:
                    LOGGER.debug(
                        "[metabolism] Anchor resolved: self model '%s' (age=%.0fs, validity=%ds)",
                        persona_model, age, validity,
                    )
                    return (self_entry["anchor_id"], "self")
                else:
                    LOGGER.debug(
                        "[metabolism] Self model anchor expired: '%s' (age=%.0fs > validity=%ds)",
                        persona_model, age, validity,
                    )
            except (KeyError, ValueError, TypeError) as exc:
                LOGGER.debug("[metabolism] Invalid self anchor entry: %s", exc)

        # Case 2: most recent valid anchor from any model
        best_entry = None
        best_updated = None
        for model_key, entry in anchors.items():
            if model_key == persona_model:
                continue  # already checked
            try:
                updated_at = datetime.fromisoformat(entry["updated_at"])
                validity = self.anchor_entry_ttl_seconds(entry, model_key, getattr(persona, "persona_id", None))
                age = (now - updated_at).total_seconds()
                if age <= validity:
                    if best_updated is None or updated_at > best_updated:
                        best_entry = entry
                        best_updated = updated_at
            except (KeyError, ValueError, TypeError):
                continue

        if best_entry:
            LOGGER.debug(
                "[metabolism] Anchor resolved: other model (age=%.0fs)",
                (now - best_updated).total_seconds(),
            )
            return (best_entry["anchor_id"], "other")

        # Case 3: no valid anchor
        LOGGER.debug("[metabolism] No valid anchor found — will use minimal load")
        return (None, "minimal")

    def update_anchor_for_model(
        self, persona, model_key: str, anchor_id: str, ttl_seconds: Optional[int] = None,
    ) -> None:
        """Update the anchor for a specific model and persist to DB.

        ``ttl_seconds`` は **この書き込み時点の cache TTL** (= 実際に焼いたキャッシュの
        寿命)。記録しておくことで、後から設定 (5m/1h) を変えても、既に書き込み済みの
        キャッシュの残り寿命は書き込み時 TTL で評価でき、設定変更による遡及的な表示
        ズレを防ぐ (docs/intent/cache_lifecycle_control.md §5.4)。
        """
        if not model_key or not anchor_id:
            return
        # TTL 延命規則 (生存中は max 維持 / 短い書き込みは非スライド) は
        # upsert_anchor_entry が行内の前回値と比較して適用する。
        entry: Dict[str, Any] = {
            "anchor_id": anchor_id,
            "updated_at": datetime.now().isoformat(),
        }
        if ttl_seconds is not None:
            entry["ttl_seconds"] = int(ttl_seconds)
        self.upsert_anchor_entry(getattr(persona, "persona_id", None), model_key, entry)

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

    def touch_anchor_after_llm_call(self, persona, usage, anchor_id: Optional[str] = None) -> None:
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
        self.update_anchor_for_model(persona, model_key, anchor_id, write_ttl_seconds)
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
        """anchor touch 直後に「セッションの見張り」を EventScheduler に予約する。

        歴史的経緯: この予約はもともと ``MetaLayer.on_periodic_tick`` (v1 状況分類の
        メタ判断 Pulse) を発火していた → keep-alive LLM コールに置き換わり
        (life_concept_map.md §14 A2、まはー決定 2026-07-07) → いまは「セッション
        見張り」に一般化した。explicit キャッシュ (Anthropic) では見張りが keep-alive
        を兼ねる (同一 prefix を温め直す)。非 explicit では見張りは温めず、TTL 接近時に
        :meth:`run_cache_keepalive` を再発火させてセッションクローズ (gold_panning
        Phase 3) を採取する足場になる (docs/intent/gold_panning.md §3.6)。

        計算: ``fire_at = now + cache_ttl_seconds * (1 - cache_threshold_ratio)``
        (キャッシュ寿命のうち threshold_ratio 分が残ったタイミング)。
        cache_threshold_ratio はペルソナの ``META_JUDGMENT_CONFIG`` から取得。

        callback は :meth:`run_cache_keepalive` — explicit では意味的に不活性な極小
        LLM コールで同一 prefix を温め直すだけで、**判断 (メタ判断 / 判断点) は行わない**。
        schedule した時刻と発火時刻の間にユーザー対話が入って TTL 起点が更新
        された場合、再 touch で予約が上書きされるため、古い予約は自然に消える。

        ``cache_type == 'explicit'`` (Anthropic) は従来どおり keep-alive を予約する。
        非 explicit (gemini_explicit / implicit 等) は :meth:`_schedule_session_watchdog`
        に委譲して見張りのみ予約する (temp を温めない・keep_cache_alive に従わない)。

        ``META_JUDGMENT_CONFIG.keep_cache_alive == False`` の場合は予約しない
        (低頻度ペルソナ向け: 24 時間間隔等で cache 切れ覚悟の運用)。
        ※ このゲートは explicit (keep-alive) 専用。見張りは keepalive ではないので
        keep_cache_alive 設定には従わせない。
        """
        if cache_type != "explicit":
            # 非 explicit: keep-alive ではなくセッション見張りとして予約する。
            self._schedule_session_watchdog(persona, model_key, cache_type)
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

    def _schedule_session_watchdog(self, persona, model_key: str, cache_type: str) -> None:
        """非 explicit キャッシュ (gemini_explicit / implicit 等) のセッション見張り予約。

        explicit の keep-alive と違い、LLM で prefix を温め直さない。目的はただ一つ:
        TTL 接近時に :meth:`run_cache_keepalive` を再発火させ、その時点でペルソナが
        Active でなければセッションクローズ (gold_panning Phase 3) を採取すること。
        Active のままなら見張りを再予約して待つ (docs/intent/gold_panning.md §3.6)。

        explicit 経路との違い:

        - ``keep_cache_alive`` ゲートには従わない (見張りは keep-alive ではない)。
        - gold_panning が無効なら予約しない (見張りの唯一の目的がクローズ採取のため)。

        ``is_enabled()`` は **発火時ではなく予約時** に読む。含意: env
        (``SAIVERSE_GOLD_PANNING_ENABLED``) を切り替えても既に入っている予約は生き
        続け、次の予約 (再発火 → 再予約の輪) から反映される。

        予約 key は explicit と共通の ``f"ttl:{persona_id}:{model_key}"``
        ((persona, model) ごとに独立予約 — beat_execution_context.md §3.1)。
        """
        from sea.gold_panning import is_enabled

        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return

        if not is_enabled():
            LOGGER.debug(
                "[watchdog] session watchdog skipped (gold_panning disabled): persona=%s model=%s",
                persona_id, model_key,
            )
            return

        manager = self.manager
        scheduler = getattr(manager, "event_scheduler", None) if manager else None
        meta_layer = getattr(manager, "meta_layer", None) if manager else None
        if scheduler is None:
            return

        ttl_seconds = self.get_anchor_validity_seconds(model_key, persona_id)
        if ttl_seconds <= 0:
            return

        # threshold_ratio は explicit 経路と同じ解決 (META_JUDGMENT_CONFIG,
        # 既定 0.3, 同じガード)。meta_layer 不在時は既定にフォールバック
        # (keep_cache_alive は見張りでは参照しない)。
        threshold_ratio = 0.3
        if meta_layer is not None:
            try:
                judgment_config = meta_layer._load_judgment_config(persona)
                threshold_ratio = float(judgment_config.get("cache_threshold_ratio", 0.3))
            except Exception:
                threshold_ratio = 0.3
        if not (0.0 < threshold_ratio < 1.0):
            threshold_ratio = 0.3

        wait_seconds = ttl_seconds * (1.0 - threshold_ratio)
        fire_at = datetime.now() + timedelta(seconds=wait_seconds)
        key = f"ttl:{persona_id}:{model_key}"

        def _fire_callback() -> None:
            try:
                self.runtime.run_cache_keepalive(persona_id, model_key)
            except Exception:
                LOGGER.exception(
                    "[watchdog] session watchdog raised: persona=%s model=%s",
                    persona_id, model_key,
                )

        scheduler.schedule(fire_at=fire_at, callback=_fire_callback, key=key)
        LOGGER.debug(
            "[watchdog] scheduled session watchdog: persona=%s model=%s type=%s in %.0fs (ttl=%ds, threshold=%.2f)",
            persona_id, model_key, cache_type, wait_seconds, ttl_seconds, threshold_ratio,
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
        if not getattr(self.manager, "metabolism_enabled", False):
            return

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
        # OR 参加する)。docs/intent/gold_panning.md §3.7
        pending = getattr(persona, "_metabolism_pending", False)

        watermarks = self.get_metabolism_watermarks(persona, model_key)
        if watermarks is None:
            return

        # 発火判定は**提示される提示コンテキスト**の文字数で行う (chronicle_eviction.md §4)。
        # 既に畳んだ範囲は digest に置き換わって提示されるので、生ログの合計では
        # なく置き換え後の量を数える — でないと「畳んだのに数字が減らない」で
        # 発火し続ける。
        window = self.get_presented_window(persona, model_key, anchor)
        current_messages = window.presented
        current_chars = message_chars(current_messages)

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

        if current_chars <= watermarks.target:
            # 既に目標水位より軽い。削る先が無いので走らせない (token 発火でも同じ)。
            LOGGER.debug(
                "[metabolism] skip: window already at/below target "
                "(persona=%s, %d chars <= target=%d)",
                getattr(persona, "persona_id", "?"), current_chars, watermarks.target,
            )
            persona._metabolism_pending = False
            return

        # defer-to-hot (docs/intent/gold_panning.md §3.7): gold_panning は直前の
        # (main_line, default) コールで温まった prefix に 1 手足すのが安い条件。
        # キャッシュが冷たければ Metabolism ごと繰り延べる。gold_panning 無効時は
        # 熱さ判定をスキップして従来どおり即実行する (defer は gold_panning のためにある)。
        from sea.gold_panning import get_pending_cap, is_enabled
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
                    "[gold_panning] pressure valve: running metabolism cold "
                    "(persona=%s, %d chars > limit=%.0f)",
                    getattr(persona, "persona_id", "?"), current_chars, pressure_limit,
                )
            elif not self._is_cache_hot(persona, model_key):
                persona._metabolism_pending = True
                LOGGER.info(
                    "[gold_panning] deferring metabolism (cache cold) for %s; pending set",
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
    ) -> "SessionWindow":
        """いまペルソナに提示される提示コンテキスト (= anchor 以降 − 畳まれた範囲 + その digest)。

        chronicle_eviction.md §6。**提示とMetabolismの勘定が同じ提示コンテキストを見るための
        一点**。退場が episode 単位になって提示コンテキストの途中に圧縮区間が空くようになったため、
        「anchor 以降を全部」は提示の真実ではなくなった。
        """
        from sea.session_window import SessionWindow, prune_folds

        history_mgr = getattr(persona, "history_manager", None)
        persona_id = getattr(persona, "persona_id", None)
        if anchor_id is None:
            entry = self.load_anchor_entry(persona_id, model_key)
            anchor_id = entry.get("anchor_id") if entry else None
        if history_mgr is None or not anchor_id:
            return SessionWindow(anchor_id=anchor_id, raw=[], presented=[], folds=[])

        raw = history_mgr.get_history_from_anchor(
            anchor_id,
            required_line_roles=["main_line"],
            required_scopes=["committed"],
        )
        folds = prune_folds(
            self.load_folded_ranges(persona_id, model_key),
            [str(m.get("id")) for m in raw],
        )
        presented = self._present_with_folds(persona, raw, folds)
        return SessionWindow(
            anchor_id=anchor_id, raw=raw, presented=presented, folds=folds,
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
            entries = [
                e for e in (
                    get_entry(adapter.conn, eid) for eid in fold.chronicle_entry_ids
                ) if e is not None
            ]
            if not entries:
                entries = get_entries_covering_messages(adapter.conn, fold.message_ids)
        except Exception:
            LOGGER.warning(
                "[window] failed to look up chronicle entries for folded range "
                "(persona=%s)", getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return None, False
        texts = [e.content for e in entries if e.content]
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
        dead = [
            f for f in window.folds
            if self._resolve_fold_digest_status(persona, f)[1]
        ]
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
            if not entry or not entry.get("updated_at"):
                return False
            updated_at = datetime.fromisoformat(entry["updated_at"])
            ttl_seconds = self.anchor_entry_ttl_seconds(
                entry, str(model_key), getattr(persona, "persona_id", None),
            )
            return datetime.now() < updated_at + timedelta(seconds=ttl_seconds)
        except Exception:
            LOGGER.warning(
                "[gold_panning] failed to read anchor state for hot check (persona=%s)",
                getattr(persona, "persona_id", "?"), exc_info=True,
            )
            return False

    def _open_episode_refs(self, persona) -> set:
        """persona の open な episode_ref 集合 (W4 D2 の退役スナップ用)。

        get_open_episode は「最新 1 件」のキャッシュなので使わない — 並行
        episode (会話中に作業 episode が挟まる等) を全部見る必要がある。
        失敗は空集合に degrade (スナップなし = 現行挙動)。
        """
        persona_id = getattr(persona, "persona_id", None)
        manager = self.manager
        if (
            not persona_id
            or manager is None
            or getattr(manager, "SessionLocal", None) is None
        ):
            return set()
        try:
            from database.models import Episode
            from saiverse.episodes import STATUS_OPEN
            db = manager.SessionLocal()
            try:
                rows = (
                    db.query(Episode.SHORT_ID)
                    .filter(
                        Episode.PERSONA_ID == persona_id,
                        Episode.STATUS == STATUS_OPEN,
                    )
                    .all()
                )
            finally:
                db.close()
            return {f"episode:{r[0]}" for r in rows if r[0] is not None}
        except Exception:
            LOGGER.warning(
                "[metabolism] failed to query open episodes (persona=%s)",
                persona_id, exc_info=True,
            )
            return set()

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

        # §6 pulse 関節細分: 実際に退場する open episode の部分だけを子 episode 化。
        # 渡すのは refs を刻んだ側 (FoldedRange) — 子 episode の DIGEST_REF は
        # 再訪の鍵なので、あらすじ id を持っていない計画側の Fold では空になる。
        # あらすじを持たない吸収退場 (disabled / 編纂対象ゼロ) では子 episode を
        # **作らない** — 子の存在は「その部分の記憶はあらすじ経由で持っている」
        # (experience_structure §6) の構造宣言であり、digest 無しの子と digest 層の
        # 継承エッジは再訪先の無い嘘の構造になる (Codex レビュー 2026-07-27)。
        for fold in applied:
            if fold.episode_ref and fold.chronicle_entry_ids:
                self._record_partial_episode(persona, fold)

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

    def _record_partial_episode(self, persona, fold: "FoldedRange") -> None:
        """open episode の部分退場を子 episode として刻む (experience_structure §6)。

        長さを理由に出来事を分割はしない — 親 episode は開いたまま。退場した
        pulse 群だけを「丸ごと退場済みの部分」として子 episode に写し、閉じて
        digest 参照を持たせる。親から子へ digest 層の継承エッジを張り、親の
        「その部分の記憶はあらすじ経由で持っている」を構造として残す (§3.3)。
        """
        persona_id = getattr(persona, "persona_id", None)
        parent_ref = fold.episode_ref
        if not persona_id or not parent_ref or self.manager is None:
            return
        try:
            from saiverse.episodes import (
                close_episode,
                get_by_ref,
                invalidate_open_cache,
                open_episode,
            )
            from saiverse.experience_inheritance import record_edges

            parent = get_by_ref(self.manager, persona_id, parent_ref)
            kind = (parent or {}).get("kind") or "other"
            digest_ref = None
            if getattr(fold, "chronicle_entry_ids", None):
                digest_ref = f"chronicle:{fold.chronicle_entry_ids[0]}"

            # open → close → 継承エッジを**一つのトランザクション**で束ねる。
            # 分けると close の失敗で「開きっぱなしの子」が残り、それが
            # get_open_episode の「最後に開いた open」を奪って以後の会話が
            # 合成 episode に付く (世界状態の破壊)。
            db = self.manager.SessionLocal()
            try:
                child = open_episode(
                    self.manager, persona_id, kind,
                    building_id=(parent or {}).get("building_id"),
                    origin_ref=parent_ref,
                    meta={
                        "partial_of": parent_ref,
                        "partial_reason": "metabolism_eviction",
                        "covered_messages": len(fold.message_ids),
                    },
                    session=db,
                )
                child_ref = child.get("episode_ref")
                close_episode(
                    self.manager, persona_id, child_ref,
                    digest_ref=digest_ref, session=db,
                )
                record_edges(
                    self.manager, persona_id, parent_ref,
                    [{"parent_ref": child_ref, "layer": "digest",
                      "origin": "metabolism_partial_fold"}],
                    session=db,
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            # session 指定時は open/close がキャッシュを触らない契約なので、
            # commit 後に呼び出し側 (ここ) が整合を負う。
            invalidate_open_cache(self.manager, persona_id)
            LOGGER.info(
                "[metabolism] partial fold of open episode %s recorded as child "
                "%s (%d messages, persona=%s)",
                parent_ref, child_ref, len(fold.message_ids), persona_id,
            )
        except Exception:
            LOGGER.warning(
                "[metabolism] failed to record partial episode for %s (persona=%s); "
                "the fold itself stands (chronicle entry is the record of record)",
                parent_ref, persona_id, exc_info=True,
            )

    def run_metabolism(
        self,
        persona,
        building_id: str,
        window: "SessionWindow",
        watermarks: Watermarks,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        model_key: Optional[str] = None,
    ) -> None:
        """Execute history metabolism: Chronicle generation + anchor update.

        ``model_key`` はこの Metabolism を発火させた Pulse の実行 model。退役
        (anchor 前進) は「渡された model の session_anchor 行」だけを進める
        (beat_execution_context.md §3.2 — 編纂は persona に一度、退役は model
        ごと)。None なら従来どおり ``persona.model``。

        Beat ロック (beat_execution_context.md §3.4): Metabolism は persona の
        記憶 (Chronicle / gold_panning のコア記憶採取記録) に書くため、入口で
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
            self._run_metabolism_locked(
                persona, building_id, window, watermarks, event_callback,
                model_key=model_key,
            )

    def _run_metabolism_locked(
        self,
        persona,
        building_id: str,
        window: "SessionWindow",
        watermarks: Watermarks,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        model_key: Optional[str] = None,
    ) -> None:
        """:meth:`run_metabolism` の本体 (Beat ロック保持下で実行される)。

        提示コンテキストは**ロックの内側で撮り直す**。呼び出し元 (発火判定) が撮った提示コンテキストはロックの
        外の値で、その間に別入口 (手動の記憶整理など) が圧縮区間や anchor を書いている
        ことがある。古い提示コンテキストを土台に圧縮区間を上書き保存すると、先行の圧縮区間が消えて生ログが
        復活し、しかもその範囲は編纂済みなので二重提示になる。
        """
        from sai_memory.arasuji.alignment import chronicle_band_budget

        model_key = str(model_key or getattr(persona, "model", "") or "") or None
        persona_id = getattr(persona, "persona_id", "?")
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
            return

        # 退場計画 (chronicle_eviction.md §5): 保護範囲を残し、退場候補範囲の中で
        # 古い方から U に達したまとまりを、open episode は単独・closed 同士は
        # またいで畳む。目標水位に届くまで繰り返す。
        open_refs = self._open_episode_refs(persona)
        plan = plan_eviction(
            current_messages, open_refs, watermarks, target_chars=band_budget,
        )
        if plan.used_last_resort_fold:
            # 最後の手段の経路を通った = 先頭に U 未満の端数が居座って anchor が
            # 詰まっていた。定常運転になっていないかを見るために残す観測ログ。
            LOGGER.info(
                "[metabolism] eviction used the last-resort undersized fold "
                "(persona=%s)", persona_id,
            )
        if plan.is_empty:
            LOGGER.warning(
                "[metabolism] nothing foldable this round (persona=%s, %d chars, "
                "target=%d, protected_from=%d); window stays large until a "
                "foldable range reaches U=%d",
                persona_id, plan.total_chars, watermarks.target,
                plan.protected_from, band_budget,
            )
            return

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
        # 一致するのは**Chronicle 対象の集合に限っての話**。除外タグ
        # (handy_tool / spell / event_message / session_digest) のメッセージは
        # fold に入っていても編纂されずに退場する — これは本設計で入った圧縮区間では
        # なく旧実装から続く既知の欠けで、下限「退場したものは必ず編纂されている」
        # を字義どおりには満たしていない。実際に圧縮区間が空いた範囲は
        # `_apply_eviction_plan` が「あらすじを持たない fold は退場させない」で
        # 拾う (退場そのものを見送るので、消えるのではなく生ログのまま残る)。
        memory_weave_enabled = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "").lower() in ("true", "1")
        chronicle_status = "disabled"
        if memory_weave_enabled and self.is_chronicle_enabled_for_persona(persona):
            try:
                chronicle_status = self.generate_chronicle(
                    persona, event_callback,
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

        # 2.8. gold_panning (砂金採り) — Chronicle 生成後・eviction 前の温まった
        # prefix で、押し出される会話から恒常知識をコア記憶に採取する。
        # 失敗隔離は不変条件 (docs/intent/gold_panning.md §5-2): gold_panning が
        # どう死んでもアンカー更新 (下の step 3) は必ず実行される。
        try:
            from sea.gold_panning import run_gold_panning
            run_gold_panning(self, persona, building_id, current_messages, evict_count, event_callback)
        except Exception:
            LOGGER.exception("[gold_panning] failed; metabolism continues")

        # 3. Update anchor to new window start — S2 ガード: 編纂が済んだ
        # ("ok") か編纂を持たない設計 ("disabled") のときだけ退役する。
        # failed / deferred は据え置き — watermark 超過が残るので、次の
        # maybe_run_metabolism が自然に再試行する (beat_execution_context.md §3.2)。
        if chronicle_status in ("ok", "disabled"):
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
        else:
            LOGGER.warning(
                "[metabolism] anchor held back (chronicle_status=%s, model=%s); "
                "will retry on next maybe_run_metabolism",
                chronicle_status, model_key,
            )
            if event_callback:
                event_callback({
                    "type": "metabolism",
                    "status": "completed",
                    "content": "記憶の整理を見送りました（Chronicle生成が完了しなかったため、次回に再試行します）",
                })

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
        ④session close ⑤①内 gold_panning) が合流する一点のため、編纂の冪等
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
            chronicle_min_digest_chars,
            plan_alignment,
        )
        from saiverse.memory_weave_llm import (
            build_memory_weave_client,
            resolve_memory_weave_config,
        )

        try:
            model_id, model_config, _weave_source = resolve_memory_weave_config(
                persona, purpose="chronicle"
            )
        except LookupError as exc:
            LOGGER.warning("[metabolism] %s (Chronicle generation)", exc)
            return "failed"
        client = build_memory_weave_client(model_id, model_config)

        # Initialize arasuji tables and fetch all messages
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            LOGGER.warning("[metabolism] SAIMemory not available for Chronicle generation")
            return "failed"

        init_arasuji_tables(adapter.conn)

        # 帰化バックフィル (W4 D7): 既存 entry に coverage_chars を刻む
        # (一回きり・冪等・LLM なし)。**dry 予測より前に**実行する — dry が
        # 近似値で動くと実測 backfill 後の列のあふれ判定と食い違い、
        # 「予測 0 → 早期 return → backfill に永久に到達しない」が成立する
        # (Codex W4 三巡 #3)。帰化はメタデータ補完で確認ゲートの対象外。
        from sai_memory.arasuji.bands import backfill_coverage
        try:
            backfill_coverage(adapter.conn)
        except Exception:
            LOGGER.exception("[metabolism] coverage backfill failed; continuing")

        # Fetch ALL messages suitable for Chronicle (shared filter logic).
        from sai_memory.memory.storage import get_messages_for_chronicle
        all_messages = get_messages_for_chronicle(adapter.conn)

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

        # Episode 整列計画 (W4 D3)。processed_ids / digest index / サイズ束ねを
        # 純関数に集約 — コスト見積もり (estimate.py) と同じ計画を共有する。
        _cur = adapter.conn.execute(
            "SELECT DISTINCT json_each.value "
            "FROM arasuji_entries, json_each(source_ids_json) "
            "WHERE level = 1"
        )
        _processed_ids = {row[0] for row in _cur.fetchall()}

        episode_digests = self._collect_episode_digests(persona)
        plan = plan_alignment(
            all_messages,
            _processed_ids,
            episode_digests,
            target_chars=chronicle_band_budget(),
            min_llm_chars=chronicle_min_digest_chars(),
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
        try:
            from sai_memory.arasuji.bands import estimate_leaf_chars
            band_plan_count = 0 if folded_entry_ids is None else plan_band_overflow(
                adapter.conn,
                extra_leaves=[
                    (
                        c.coverage_chars,
                        min((m.created_at for m in c.messages), default=None),
                        max((m.created_at for m in c.messages), default=None),
                        # digest 字数の実測見込み (Codex P1-3 — dry の発火
                        # 過小評価を防ぐ)
                        estimate_leaf_chars(c.kind, c.messages, c.digest_text),
                    )
                    for c in plan.chunks
                ],
                excluded_entry_ids=folded_entry_ids or None,
                # 編纂予定の生メッセージは dry の連続性判定で「未編纂」から
                # 除外する (Codex 再レビュー 必須2 — 実行後の列を再現)
                pending_source_ids={
                    m.id for c in plan.chunks for m in c.messages
                } or None,
            )
        except Exception:
            LOGGER.warning("[metabolism] band overflow dry-plan failed", exc_info=True)
            band_plan_count = 0

        if not plan.chunks and band_plan_count == 0:
            LOGGER.info(
                "[metabolism] Nothing to compile or consolidate "
                "(%d unprocessed messages)", plan.total_unprocessed,
            )
            # 編纂対象なし = claim せず no-op (退役は許す)。
            return "ok"

        unprocessed_count = plan.total_unprocessed
        estimated_llm_calls = plan.llm_calls + band_plan_count

        # 冪等 claim 用の提示コンテキストの同定: 提示コンテキスト末尾 ID = 編纂対象の時系列末尾の
        # メッセージ ID (all_messages は created_at 昇順)。会話が進んで提示コンテキストが
        # 伸びれば ID が変わり新しい claim になる — 失敗した提示コンテキストの再試行は
        # この自然な鍵の更新で成立する (failed 行は終端で再 claim 不可)。
        # plan 空 (列の束ねのみ) の実行は claim しない — 束ねの並走防御は
        # bands の tx 内再検査が担う (並走時の +1 LLM コールは許容)。
        _window_end_id = (
            plan.chunks[-1].messages[-1].id if plan.chunks else None
        )

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
        ledger = self._get_ledger()
        execution_id: Optional[str] = None
        if ledger is not None and _window_end_id is not None:
            try:
                # claim_execution: failed 行 (前回の失敗 / キャンセル) はキーを
                # 退避して新規 prepared を作る — キャンセル直後の同提示コンテキスト再実行が
                # 永久に deferred にならない (Codex W4 二巡 #6)。running /
                # applied / completed / unknown はブロック。
                execution_id, runnable, existing_status = ledger.claim_execution(
                    kind="metabolism.run",
                    idempotency_key=f"{getattr(persona, 'persona_id', None)}:{_window_end_id}",
                    persona_id=getattr(persona, "persona_id", None),
                )
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

        # Notify frontend that generation is starting
        if event_callback:
            event_callback({
                "type": "metabolism",
                "status": "running",
                "content": f"Chronicleを生成しています (0/{unprocessed_count})...",
            })

        # Build progress callback for streaming status to frontend
        def progress_fn(processed, total):
            if event_callback:
                event_callback({
                    "type": "metabolism",
                    "status": "running",
                    "content": f"Chronicleを生成しています ({processed}/{total})...",
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
            )
        except Exception as exc:
            LOGGER.warning("[metabolism] Entity extraction setup failed: %s", exc)

        from sai_memory.arasuji.bands import run_band_overflow
        from sai_memory.arasuji.executor import execute_plan

        try:
            exec_result = execute_plan(
                plan, client, adapter.conn,
                persona_id=persona_id_str,
                progress_callback=progress_fn,
                cancel_check=cancel_fn,
                batch_callback=note_callback,
            )
        except Exception as exc:
            LOGGER.exception("[metabolism] Chronicle generation raised")
            if ledger is not None and execution_id:
                try:
                    # 部分生成 (途中チャンクまで確定済み) はあり得るが、確定済み
                    # チャンクは source_ids で冪等スキップされるため再試行は安全。
                    ledger.mark_failed(execution_id, str(exc) or type(exc).__name__)
                except Exception:
                    LOGGER.warning("[metabolism] ledger mark_failed failed", exc_info=True)
            return "failed"

        if exec_result.cancelled:
            # キャンセル = 部分適用。completed で封印すると同じ提示コンテキストが再実行不能に
            # なる (冪等マーカーは適用の成功だけを封印する — W3 教訓③ /
            # Codex W4 #8)。failed 終端で claim を退け、anchor は据え置く。
            if ledger is not None and execution_id:
                try:
                    ledger.mark_failed(execution_id, "cancelled by user")
                except Exception:
                    LOGGER.warning("[metabolism] ledger mark_failed failed", exc_info=True)
            LOGGER.info(
                "[metabolism] Chronicle generation cancelled (%d chunks committed)",
                exec_result.created_count,
            )
            return "deferred"

        # 束ね (chronicle_consolidation): 未束ねの字数が発火閾値を超えたら、
        # 質量選抜 (比率・連続性・卒業) で群を束ね、束ね不能ノードは治療する。
        # 束ね失敗は編纂の成否に含めない (一次あらすじは確定済み = 情報の欠落は
        # なく、次回の Metabolism の dry 予測が backlog を検出して自然に再試行
        # する)。batch_callback は恒等圧縮の子が初めて要約に変わる瞬間の
        # Fragment 抽出 (intent §7)。
        consolidated_count = 0
        if folded_entry_ids is not None:
            try:
                consolidated_count = run_band_overflow(
                    adapter.conn, client,
                    persona_id=persona_id_str,
                    cancel_check=cancel_fn,
                    excluded_entry_ids=folded_entry_ids or None,
                    batch_callback=note_callback,
                )
            except Exception:
                LOGGER.exception("[bands] consolidation failed; continuing")

        LOGGER.info(
            "[metabolism] Chronicle generation complete: %d chunks created "
            "(%d skipped as duplicates), %d band consolidations",
            exec_result.created_count, exec_result.skipped_duplicates,
            consolidated_count,
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
                ledger.mark_applied(execution_id, result=result_payload)
                # outbox を積まない実行なので completed へ明示遷移して閉じる。
                ledger.mark_completed(execution_id)
            except Exception:
                LOGGER.warning("[metabolism] ledger apply/complete failed", exc_info=True)

        # Notify frontend that generation is complete
        if event_callback:
            event_callback({
                "type": "metabolism",
                "status": "completed",
                "content": f"Chronicle生成完了: {exec_result.created_count}件のエントリを作成しました。",
            })
        return "ok"

    def _collect_episode_digests(self, persona) -> Dict[str, Tuple[str, str]]:
        """digest 確定済み episode の index (W4 D3 — 恒等転写材料)。

        実体は saiverse.episodes.collect_episode_digest_index (API の手動生成
        ジョブと共有する一点管理)。失敗・環境不備は空 dict に degrade。
        """
        persona_id = getattr(persona, "persona_id", None)
        adapter = getattr(persona, "sai_memory", None)
        if not persona_id or adapter is None or not adapter.is_ready():
            return {}
        try:
            from saiverse.episodes import collect_episode_digest_index
            return collect_episode_digest_index(
                self.manager, persona_id, adapter.conn,
            )
        except Exception:
            LOGGER.warning(
                "[metabolism] episode digest index failed (persona=%s)",
                persona_id, exc_info=True,
            )
            return {}

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
            init_arasuji_tables(adapter.conn)
            init_memopedia_tables(adapter.conn)
            n_chr = embed_chronicle_entries(adapter.conn, adapter.embedder, level=1)
            n_page = embed_memopedia_pages(adapter.conn, adapter.embedder)
            n_frag = embed_memopedia_fragments(adapter.conn, adapter.embedder)
            if n_chr or n_page or n_frag:
                LOGGER.info(
                    "[metabolism] Embeddings generated: chronicle=%d, pages=%d, fragments=%d",
                    n_chr, n_page, n_frag,
                )
        except Exception:
            LOGGER.exception("[metabolism] Embedding generation failed")

    def run_session_close_for(self, persona_id: str) -> None:
        """persona_id からペルソナを引いて gold_panning のセッションクローズを走らせる。

        SEARuntime._spawn_session_close の別スレッドから呼ばれる薄いラッパ。
        run_cache_keepalive と同じく manager.personas から引く。
        """
        persona = (getattr(self.manager, "personas", None) or {}).get(persona_id)
        if persona is None:
            LOGGER.debug("[gold_panning] session close: persona not found (%s)", persona_id)
            return
        from sea.gold_panning import run_session_close
        run_session_close(self, persona)


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
                    folds = deserialize_folds(payload)
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
