from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from sea.cancellation import CancellationToken

if TYPE_CHECKING:
    from sea.runtime import SEARuntime

LOGGER = logging.getLogger(__name__)


class SessionLifecycle:
    """Anchor / Metabolism / Chronicle — Session (短期記憶) の節目管理。

    docs/intent/session.md の「Session 統一制御単位」の実装先。
    session_lifecycle_extraction_design.md Step 1 で SEARuntime から抽出した。
    """

    def __init__(self, runtime: "SEARuntime", manager_ref: Any) -> None:
        self.runtime = runtime      # 過渡期の後方参照 (設計書 §4 で削減)
        self.manager = manager_ref

    def get_high_watermark(self, persona) -> Optional[int]:
        """Get the high watermark (max history messages) for metabolism."""
        override = getattr(self.manager, "max_history_messages_override", None) if self.manager else None
        if override is not None:
            return override
        from saiverse.model_configs import get_default_max_history_messages
        persona_model = getattr(persona, "model", None)
        if persona_model:
            return get_default_max_history_messages(persona_model)
        return None

    def get_low_watermark(self, persona) -> Optional[int]:
        """Get the low watermark (keep messages after metabolism) for metabolism."""
        override = getattr(self.manager, "metabolism_keep_messages_override", None) if self.manager else None
        if override is not None:
            return override
        from saiverse.model_configs import get_metabolism_keep_messages
        persona_model = getattr(persona, "model", None)
        if persona_model:
            return get_metabolism_keep_messages(persona_model)
        return None

    def load_anchors(self, persona) -> Dict[str, Any]:
        """Load per-model metabolism anchors from DB (AI.METABOLISM_ANCHORS)."""
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return {}
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return {}
        db = self.manager.SessionLocal()
        try:
            from database.models import AI
            ai_row = db.query(AI).filter_by(AIID=persona_id).first()
            if ai_row and ai_row.METABOLISM_ANCHORS:
                return json.loads(ai_row.METABOLISM_ANCHORS)
        except Exception as exc:
            LOGGER.warning("[metabolism] Failed to load anchors for %s: %s", persona_id, exc)
        finally:
            db.close()
        return {}

    def save_anchors(self, persona, anchors: Dict[str, Any]) -> None:
        """Persist per-model metabolism anchors to DB."""
        if not self.manager or not hasattr(self.manager, "SessionLocal"):
            return
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return
        db = self.manager.SessionLocal()
        try:
            from database.models import AI
            ai_row = db.query(AI).filter_by(AIID=persona_id).first()
            if ai_row:
                ai_row.METABOLISM_ANCHORS = json.dumps(anchors, ensure_ascii=False)
                db.commit()
        except Exception as exc:
            LOGGER.warning("[metabolism] Failed to save anchors for %s: %s", persona_id, exc)
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

    def resolve_metabolism_anchor(self, persona) -> tuple:
        """Resolve the best metabolism anchor using 3-level fallback.

        Returns:
            (anchor_id, resolution_type) where resolution_type is
            "self" | "other" | "minimal".
            anchor_id is None for "minimal" (no valid anchor found).
        """
        persona_model = getattr(persona, "model", None)
        if not persona_model:
            return (None, "minimal")

        anchors = self.load_anchors(persona)
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
        anchors = self.load_anchors(persona)
        now = datetime.now()
        effective_ttl = int(ttl_seconds) if ttl_seconds is not None else None
        effective_updated = now

        # Anthropic 実機観測 (2026-05-25) に整合する更新規則 (モデルB):
        # - 生存中のキャッシュは短い TTL の書き込みで「短縮されない」(max を維持)
        # - 加えて、短い書き込みは expiry ウィンドウを **スライドさせない**。1h を
        #   5m 書き込みで延命できると過大表示になるため (1h ウィンドウは「1h を
        #   確立した時刻」起点で減り続ける)。
        # - 同じか長い TTL の書き込みのときだけ updated_at を now にリフレッシュ
        #   (= 使用でウィンドウが延びる、keep-awake の前提)。
        # - 完全失効後の書き込みは新しい TTL/now でリセット。
        # docs/intent/cache_lifecycle_control.md §5.2
        if effective_ttl is not None:
            prev = anchors.get(model_key)
            prev_ttl = (prev or {}).get("ttl_seconds")
            if prev and prev_ttl:
                try:
                    prev_updated = datetime.fromisoformat(prev["updated_at"])
                    prev_ttl_int = int(prev_ttl)
                    if now < prev_updated + timedelta(seconds=prev_ttl_int):  # 生存中
                        effective_ttl = max(prev_ttl_int, effective_ttl)
                        if int(ttl_seconds) < prev_ttl_int:
                            # 短い書き込み: 短縮も延命もしない (起点を維持)
                            effective_updated = prev_updated
                except (KeyError, ValueError, TypeError):
                    pass

        entry = {
            "anchor_id": anchor_id,
            "updated_at": effective_updated.isoformat(),
        }
        if effective_ttl is not None:
            entry["ttl_seconds"] = effective_ttl
        anchors[model_key] = entry
        self.save_anchors(persona, anchors)

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

    def touch_anchor_after_llm_call(self, persona, usage) -> None:
        """LLM 呼び出し成功後に METABOLISM_ANCHORS の updated_at を touch する (Phase 4-e)。

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
        """
        if persona is None or usage is None:
            return
        history_mgr = getattr(persona, "history_manager", None)
        anchor_id = getattr(history_mgr, "metabolism_anchor_message_id", None) if history_mgr else None
        if not anchor_id:
            return
        persona_model = getattr(persona, "model", None)
        if not persona_model:
            return

        try:
            from saiverse.model_configs import get_cache_config
            cache_config = get_cache_config(persona_model)
            cache_type = (cache_config or {}).get("type", "implicit")
        except Exception:
            LOGGER.warning(
                "[metabolism] Failed to resolve cache type for %s; assuming implicit",
                persona_model, exc_info=True,
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
                    getattr(persona, "persona_id", "?"), persona_model, anchor_id,
                )
                return

        # この書き込みで実際に使った TTL (= 現行設定) を anchor に記録する。
        # 以後この cache の残り寿命はこの値で評価され、設定変更の影響を受けない。
        write_ttl_seconds = self.get_anchor_validity_seconds(
            persona_model, getattr(persona, "persona_id", None),
        )
        self.update_anchor_for_model(persona, persona_model, anchor_id, write_ttl_seconds)
        LOGGER.debug(
            "[metabolism] anchor touched after LLM success: persona=%s model=%s anchor=%s cache_type=%s ttl=%ds",
            getattr(persona, "persona_id", "?"), persona_model, anchor_id, cache_type, write_ttl_seconds,
        )

        # Phase 4-e: touch した時刻を起点に「TTL 接近で前倒し meta_judgment Pulse」
        # を EventScheduler に予約する。同じペルソナ・モデルで再 touch されると
        # 古い予約は cancel される (key 上書き)。失敗時は touch されないので
        # 予約も更新されず、自然と TTL 切れ判定経路に乗る。
        try:
            self.schedule_cache_ttl_pulse(persona, persona_model, cache_type)
        except Exception:
            LOGGER.exception(
                "[metabolism] Failed to schedule cache TTL pulse for persona=%s model=%s",
                getattr(persona, "persona_id", "?"), persona_model,
            )

        # Token-based metabolism trigger: flag persona if input_tokens exceeds threshold
        self.check_token_threshold(persona, persona_model, usage)

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
            scheduler.cancel(f"ttl:{persona_id}")
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
        key = f"ttl:{persona_id}"

        def _fire_callback() -> None:
            try:
                self.runtime.run_cache_keepalive(persona_id)
            except Exception:
                LOGGER.exception(
                    "[keepalive] cache keep-alive raised: persona=%s", persona_id,
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

        予約 key は explicit と共通の ``f"ttl:{persona_id}"`` (1 ペルソナ 1 予約の
        上書き挙動を維持)。
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
        key = f"ttl:{persona_id}"

        def _fire_callback() -> None:
            try:
                self.runtime.run_cache_keepalive(persona_id)
            except Exception:
                LOGGER.exception(
                    "[watchdog] session watchdog raised: persona=%s", persona_id,
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
    ) -> None:
        """Check if metabolism is needed after response and run if so."""
        if not getattr(self.manager, "metabolism_enabled", False):
            return

        history_mgr = getattr(persona, "history_manager", None)
        anchor = getattr(history_mgr, "metabolism_anchor_message_id", None)
        if not history_mgr or not anchor:
            return

        # Token threshold trigger: check if last LLM call exceeded the threshold
        token_triggered = getattr(persona, "_metabolism_token_triggered", False)
        if token_triggered:
            persona._metabolism_token_triggered = False

        # defer-to-hot で繰り延べ中のフラグ (token_triggered と同格で should_run に
        # OR 参加する)。docs/intent/gold_panning.md §3.7
        pending = getattr(persona, "_metabolism_pending", False)

        high_wm = self.get_high_watermark(persona)
        if high_wm is None:
            return

        # Get current message count from anchor (Phase 3 段階 4-A: line ベース)
        current_messages = history_mgr.get_history_from_anchor(
            anchor,
            required_line_roles=["main_line"],
            required_scopes=["committed"],
        )

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
        elif len(current_messages) > high_wm:
            should_run = True
            LOGGER.info(
                "[metabolism] Triggering metabolism for %s: %d messages > high_wm=%d",
                getattr(persona, "persona_id", "?"), len(current_messages), high_wm,
            )

        if not should_run:
            return

        low_wm = self.get_low_watermark(persona)
        if low_wm is None or len(current_messages) - low_wm < 10:
            return

        # defer-to-hot (docs/intent/gold_panning.md §3.7): gold_panning は直前の
        # (main_line, default) コールで温まった prefix に 1 手足すのが安い条件。
        # キャッシュが冷たければ Metabolism ごと繰り延べる。gold_panning 無効時は
        # 熱さ判定をスキップして従来どおり即実行する (defer は gold_panning のためにある)。
        from sea.gold_panning import get_pending_cap, is_enabled
        if is_enabled():
            cap = get_pending_cap()
            if len(current_messages) > high_wm * cap:
                # 圧力弁: 繰り延べ続けて毎ターン肥大ウィンドウを読むより、一回の
                # コールド代のほうが安い。明示ログを残す (不変条件 §5-1 の例外)。
                LOGGER.warning(
                    "[gold_panning] pressure valve: running metabolism cold "
                    "(persona=%s, %d messages > high_wm=%d * cap=%.2f)",
                    getattr(persona, "persona_id", "?"), len(current_messages), high_wm, cap,
                )
            elif not self._is_cache_hot(persona):
                persona._metabolism_pending = True
                LOGGER.info(
                    "[gold_panning] deferring metabolism (cache cold) for %s; pending set",
                    getattr(persona, "persona_id", "?"),
                )
                return

        # 実行に入るので pending をクリアする。
        persona._metabolism_pending = False

        LOGGER.info(
            "[metabolism] Running metabolism: %d messages, will keep %d",
            len(current_messages), low_wm,
        )
        self.run_metabolism(persona, building_id, current_messages, low_wm, event_callback)

    def _is_cache_hot(self, persona) -> bool:
        """persona.model の anchor エントリが生存しているか (= 直前 prefix が温かい)。

        run_cache_keepalive の生存判定と同じロジック (キャッシュ書き込み時 TTL で評価)。
        docs/intent/gold_panning.md §3.7
        """
        model_key = getattr(persona, "model", None)
        if not model_key:
            return False
        try:
            anchors = self.load_anchors(persona) or {}
            entry = anchors.get(str(model_key))
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

    def run_metabolism(
        self,
        persona,
        building_id: str,
        current_messages: List[Dict[str, Any]],
        keep_count: int,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """Execute history metabolism: Chronicle generation + anchor update.

        Beat ロック (beat_execution_context.md §3.4): Metabolism は persona の
        記憶 (Chronicle / gold_panning のコア記憶採取記録) に書くため、入口で
        beat_gate.hold(purpose="metabolism") を通す。Pulse 内 (run_meta_user
        経由) の呼び出しは同一スレッドの RLock 再入で無害 (関所も再実行され
        ない)。API の手動整理 (api/routes/people/config.py → organize-memory)
        からの呼び出しは独立 Beat として直列化され、関所 fail-closed
        (BeatGateClosedError) はそのまま API へ伝播する。
        """
        from sea.beat_gate import hold_beat
        with hold_beat(
            self.manager,
            getattr(persona, "persona_id", None),
            purpose="metabolism",
        ):
            self._run_metabolism_locked(
                persona, building_id, current_messages, keep_count, event_callback,
            )

    def _run_metabolism_locked(
        self,
        persona,
        building_id: str,
        current_messages: List[Dict[str, Any]],
        keep_count: int,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """:meth:`run_metabolism` の本体 (Beat ロック保持下で実行される)。"""
        evict_count = len(current_messages) - keep_count

        # 1. Notify start
        if event_callback:
            event_callback({
                "type": "metabolism",
                "status": "started",
                "content": f"記憶を整理しています（{len(current_messages)}件 → {keep_count}件）...",
            })

        # 2. Chronicle generation (only if Memory Weave is enabled AND per-persona toggle is on)
        memory_weave_enabled = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "").lower() in ("true", "1")
        if memory_weave_enabled and self.is_chronicle_enabled_for_persona(persona):
            try:
                self.generate_chronicle(persona, event_callback)
            except Exception as exc:
                LOGGER.warning("[metabolism] Chronicle generation failed: %s", exc)

        # 2.5. Track Chronicle generation (v0.32, 2026-05-09)。
        # General Chronicle と独立に走る。pulse_type 制限なし、確認 dialog 不要、
        # バッチ未満許容 (incomplete Lv1)、1000 字未満ならスキップ。
        # 詳細は docs/intent/persona_cognition/track_chronicle.md
        if memory_weave_enabled and self.is_chronicle_enabled_for_persona(persona):
            try:
                self.generate_track_chronicle(persona)
            except Exception as exc:
                LOGGER.warning("[metabolism] Track Chronicle generation failed: %s", exc)

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

        # 3. Update anchor to new window start
        new_anchor_id = current_messages[evict_count].get("id")
        if new_anchor_id:
            persona.history_manager.metabolism_anchor_message_id = new_anchor_id
            persona_model = getattr(persona, "model", None)
            if persona_model:
                self.update_anchor_for_model(persona, persona_model, new_anchor_id)
            LOGGER.info("[metabolism] Updated anchor to %s (evicted %d, kept %d)", new_anchor_id, evict_count, keep_count)

        # 4. Dynamic State Sync: AをCで更新し、ビジュアルコンテキストキャッシュを無効化
        try:
            from saiverse.dynamic_state import DynamicStateManager
            DynamicStateManager.on_metabolism(persona, self.manager)
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
    ) -> None:
        """Generate Chronicle entries from all unprocessed messages.

        ``force=True`` は確認ダイアログ・pulse_type 判定を経ずに即生成する。
        UI の「記憶の整理」ボタン (organize-memory API) のように、呼び出しの
        時点でユーザーが既に明示的に同意しているケース専用。Pulse の外から
        呼ばれるため ``persona._current_pulse_type`` は前回 Pulse の残留値で
        あてにならない — force はその不定性を回避する意味もある。
        """
        from llm_clients.factory import get_llm_client
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.generator import DEFAULT_BATCH_SIZE, ArasujiGenerator
        from sai_memory.memory.storage import Message, get_messages_paginated
        from saiverse.model_configs import find_model_config

        from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
        model_name = getattr(persona, "memory_weave_model", None) or os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL)
        model_id, model_config = find_model_config(model_name)
        if not model_config:
            LOGGER.warning("[metabolism] Model '%s' not found for Chronicle generation", model_name)
            return

        provider = model_config.get("provider")
        context_length = model_config.get("context_length", 128000)
        client = get_llm_client(model_id, provider, context_length, config=model_config)

        # Initialize arasuji tables and fetch all messages
        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            LOGGER.warning("[metabolism] SAIMemory not available for Chronicle generation")
            return

        init_arasuji_tables(adapter.conn)

        # Fetch ALL messages suitable for Chronicle (shared filter logic).
        from sai_memory.memory.storage import get_messages_for_chronicle
        all_messages = get_messages_for_chronicle(adapter.conn)

        if not all_messages:
            return

        batch_size_for_estimate = int(os.getenv("MEMORY_WEAVE_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))

        # Pre-check: replicate the same contiguous-run logic used by
        # generate_unprocessed() so we can skip the confirmation dialog
        # when no run is large enough to produce even one batch.
        _cur = adapter.conn.execute(
            "SELECT DISTINCT json_each.value "
            "FROM arasuji_entries, json_each(source_ids_json) "
            "WHERE level = 1"
        )
        _processed_ids = {row[0] for row in _cur.fetchall()}

        _runs: list[list] = []
        _current_run: list = []
        for _msg in all_messages:
            if _msg.id in _processed_ids:
                if _current_run:
                    _runs.append(_current_run)
                    _current_run = []
                continue
            _current_run.append(_msg)
        if _current_run:
            _runs.append(_current_run)

        # Count only full batches (trailing incomplete batches are skipped
        # by generate_from_messages), matching the cost-estimate API logic.
        qualifying_batches = sum(
            len(r) // batch_size_for_estimate
            for r in _runs if len(r) >= batch_size_for_estimate
        )

        if qualifying_batches == 0:
            total_unprocessed = sum(len(r) for r in _runs)
            LOGGER.info(
                "[metabolism] No qualifying runs for Chronicle generation "
                "(%d unprocessed messages in %d runs, all < batch_size %d)",
                total_unprocessed, len(_runs), batch_size_for_estimate,
            )
            return

        unprocessed_count = qualifying_batches * batch_size_for_estimate
        estimated_llm_calls = qualifying_batches

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
        if force:
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
            display_model = model_config.get("display_name", model_name)

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
                return
            LOGGER.info("[metabolism] Chronicle generation approved by user")
        else:
            # No interactive route available and AUTONOMOUS_CHRONICLE_ENABLED is
            # False (or no event_callback/manager) — skip without waiting.
            # (auto / schedule / meta_judgment pulses, or pure CLI runs)
            LOGGER.info(
                "[metabolism] Skipping Chronicle generation confirmation "
                "(pulse_type=%s, event_callback=%s, %d unprocessed)",
                pulse_type, event_callback is not None, unprocessed_count,
            )
            return

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

        batch_size = int(os.getenv("MEMORY_WEAVE_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))
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

        generator = ArasujiGenerator(
            client, adapter.conn,
            batch_size=batch_size,
            consolidation_size=10,
            persona_id=persona_id_str,
        )

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

        level1, consolidated = generator.generate_unprocessed(
            all_messages,
            progress_callback=progress_fn,
            cancel_check=cancel_fn,
            batch_callback=note_callback,
        )
        LOGGER.info(
            "[metabolism] Chronicle generation complete: %d level1, %d consolidated entries",
            len(level1), len(consolidated),
        )

        # Notify frontend that generation is complete
        if event_callback:
            event_callback({
                "type": "metabolism",
                "status": "completed",
                "content": f"Chronicle生成完了: {len(level1)}件のエントリを作成しました。",
            })

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

    def generate_track_chronicle(self, persona) -> None:
        """Track Chronicle 生成 (v0.32, 2026-05-09)。

        General Chronicle (generate_chronicle) と独立に走る。設計上の特徴:

        - **pulse_type 制限なし**: 自律稼働 / メタ判断 / スケジュール pulse でも走る
        - **ユーザー確認 dialog 不要**: ペルソナの自律的な記憶整理として、自動承認
        - **バッチ未満許容**: ArasujiGenerator(allow_incomplete=True) で incomplete Lv1
          として保存。次回 20 件揃った時点で削除して正規 Lv1 に再生成 (Generator 側
          が自動)
        - **Track ごとに 1000 字未満ならスキップ**: 短すぎる範囲は要約しても情報量が
          変わらないため、Chronicle 化せず読み込み側で生メッセージ取得経路に任せる
        - **Track 別に独立処理**: 押し出し対象に複数 Track のメッセージが混在していて
          も、origin_track_id でグループ化して各 Track ごとに ArasujiGenerator を回す

        詳細は docs/intent/persona_cognition/track_chronicle.md
        """
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.generator import DEFAULT_BATCH_SIZE, ArasujiGenerator
        from sai_memory.memory.storage import Message
        from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
        from saiverse.model_configs import find_model_config
        from llm_clients.factory import get_llm_client

        adapter = getattr(persona, "sai_memory", None)
        if not adapter or not adapter.is_ready():
            LOGGER.warning("[metabolism][track] SAIMemory not available for Track Chronicle")
            return

        model_name = getattr(persona, "memory_weave_model", None) or os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL)
        model_id, model_config = find_model_config(model_name)
        if not model_config:
            LOGGER.warning("[metabolism][track] Model '%s' not found for Track Chronicle", model_name)
            return
        provider = model_config.get("provider")
        context_length = model_config.get("context_length", 128000)
        client = get_llm_client(model_id, provider, context_length, config=model_config)

        init_arasuji_tables(adapter.conn)

        # 全メッセージを取得 (handy_tool/spell/event_message タグ除外)
        # origin_track_id IS NULL のメッセージは Track Chronicle 対象外
        import json as _json
        cur = adapter.conn.execute(
            "SELECT id, thread_id, role, content, resource_id, created_at, metadata, origin_track_id "
            "FROM messages "
            "WHERE origin_track_id IS NOT NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM json_each(metadata, '$.tags') WHERE json_each.value IN ('handy_tool', 'spell', 'event_message')"
            ") "
            "ORDER BY created_at ASC"
        )
        all_rows = cur.fetchall()
        if not all_rows:
            LOGGER.debug("[metabolism][track] No track-tagged messages to process")
            return

        # origin_track_id でグループ化
        from collections import defaultdict
        by_track: Dict[str, List[Message]] = defaultdict(list)
        for row in all_rows:
            msg_id, tid, role, content, resource_id, created_at, metadata_raw, otid = row
            metadata = None
            if metadata_raw:
                try:
                    metadata = _json.loads(metadata_raw)
                except Exception:
                    pass
            by_track[otid].append(Message(
                id=msg_id, thread_id=tid, role=role, content=content,
                resource_id=resource_id, created_at=int(created_at),
                metadata=metadata,
            ))

        # 既処理メッセージ ID を Track 別に取得 (incomplete Lv1 は処理済みに含めない、
        # ArasujiGenerator 側で再生成のため削除されるが念のためここでも合わせる)
        cur = adapter.conn.execute(
            "SELECT origin_track_id, json_each.value "
            "FROM arasuji_entries, json_each(source_ids_json) "
            "WHERE level = 1 AND origin_track_id IS NOT NULL AND is_incomplete = 0"
        )
        processed_by_track: Dict[str, set] = defaultdict(set)
        for row in cur.fetchall():
            processed_by_track[row[0]].add(row[1])

        # 既存 incomplete Lv1 の end_time を Track 別に取得 (新規メッセージなし判定用)。
        # 起動直後の anchor TTL 切れ pre-response 経路で本関数が再呼び出しされたとき、
        # Track にメッセージが追加されていなければ delete & regen は同じ内容を作り直す
        # だけで LLM 呼び出しが完全に無駄になる。incomplete Lv1 の end_time 以降に新規
        # メッセージがない Track はここでスキップする。
        cur = adapter.conn.execute(
            "SELECT origin_track_id, MAX(end_time) "
            "FROM arasuji_entries "
            "WHERE level = 1 AND origin_track_id IS NOT NULL AND is_incomplete = 1 "
            "GROUP BY origin_track_id"
        )
        incomplete_end_by_track: Dict[str, int] = {row[0]: row[1] for row in cur.fetchall() if row[1] is not None}

        # 各 Track ごとに処理
        track_manager = getattr(self.manager, "track_manager", None)
        persona_id = getattr(persona, "persona_id", None)
        batch_size = int(os.getenv("MEMORY_WEAVE_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))
        # 1000 字未満スキップ閾値
        min_chars_threshold = 1000

        total_tracks_processed = 0
        total_lv1_created = 0
        for track_id, msgs in by_track.items():
            unprocessed = [m for m in msgs if m.id not in processed_by_track.get(track_id, set())]
            if not unprocessed:
                continue

            # 既存 incomplete Lv1 のカバー範囲を超える新規メッセージがあるか判定。
            # なければ delete & regen を回避 (同じ内容を作り直す LLM 呼び出しの無駄を防ぐ)。
            incomplete_end = incomplete_end_by_track.get(track_id)
            if incomplete_end is not None:
                latest_unprocessed = max(m.created_at for m in unprocessed)
                if latest_unprocessed <= incomplete_end:
                    LOGGER.info(
                        "[metabolism][track] Skipping track=%s: no new messages since incomplete Lv1 "
                        "(incomplete_end=%s, latest_unprocessed=%s, %d msgs)",
                        track_id, incomplete_end, latest_unprocessed, len(unprocessed),
                    )
                    continue

            unprocessed_chars = sum(len(m.content or "") for m in unprocessed)
            if unprocessed_chars < min_chars_threshold:
                LOGGER.info(
                    "[metabolism][track] Skipping track=%s: unprocessed=%d msgs, %d chars < %d threshold",
                    track_id, len(unprocessed), unprocessed_chars, min_chars_threshold,
                )
                continue

            # Track の title / intent 取得
            track_title: Optional[str] = None
            track_intent: Optional[str] = None
            track_type_value: Optional[str] = None
            if track_manager is not None:
                try:
                    track = track_manager.get(track_id)
                    track_title = getattr(track, "title", None)
                    track_intent = getattr(track, "intent", None)
                    track_type_value = getattr(track, "track_type", None)
                except Exception:
                    LOGGER.debug("[metabolism][track] Could not fetch track meta for %s", track_id)

            # ユーザー会話 Track はスキップ (v0.32, 2026-05-09)。
            # 親スレッド保持機構が生メッセージで文脈を担保するため、Track Chronicle 化は不要。
            # 詳細: docs/intent/persona_cognition/track_chronicle.md §11
            if track_type_value == "user_conversation":
                LOGGER.debug(
                    "[metabolism][track] Skipping user_conversation track=%s (preserved by parent-thread mechanism)",
                    track_id,
                )
                continue

            generator = ArasujiGenerator(
                client, adapter.conn,
                batch_size=batch_size,
                consolidation_size=10,
                persona_id=persona_id,
                origin_track_id=track_id,
                track_title=track_title,
                track_intent=track_intent,
                allow_incomplete=True,
            )

            try:
                level1, consolidated = generator.generate_unprocessed(msgs)
                LOGGER.info(
                    "[metabolism][track] track=%s done: %d level1, %d consolidated",
                    track_id, len(level1), len(consolidated),
                )
                total_tracks_processed += 1
                total_lv1_created += len(level1)
            except Exception:
                LOGGER.exception(
                    "[metabolism][track] generate_unprocessed failed for track=%s", track_id,
                )

        LOGGER.info(
            "[metabolism][track] Track Chronicle complete: %d tracks processed, %d lv1 entries",
            total_tracks_processed, total_lv1_created,
        )

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
