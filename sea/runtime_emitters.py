from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from saiverse.marker_parser import strip_marks

LOGGER = logging.getLogger(__name__)

#: 「ペルソナの発言が世界に保存された」ことを運ぶイベントの型。
#:
#: 画面の完了 (``streaming_complete`` / ``say``) とは別の信号 — あちらは
#: 「画面へ流れた」の合図で、保存の証拠ではない。このイベントは建物履歴の
#: 行に本文が入った後にだけ流れ、保存された行の id を運ぶ。受け手
#: (manager/runtime.py の続きの生成の印降ろし) は、この信号が来なかった回は
#: 印を残す側へ倒れる。
#: 設計: docs/issues/archive/stream_completion_is_not_proof_of_persistence.md
SPEAK_PERSISTED_EVENT_TYPE = "speak_persisted"


def notify_speak_persisted(
    event_callback: Optional[Callable[[Dict[str, Any]], None]],
    building_msg: Optional[Dict[str, Any]],
    persona: Any,
    pulse_id: Optional[str],
) -> None:
    """保存が成功した assistant 発言の、保存完了イベントの唯一の発火口。

    assistant の発言を建物履歴に保存する経路は複数ある (下書き行の確定 /
    ``emit_say`` / ``emit_speak`` / tell ツール)。信号の条件を経路ごとに
    書き分けると必ず漏れるので、判定はここに一本化する:

    - ``building_msg`` に DB 採番の ``message_id`` が付いている
      (= insert / update が実際に通った。``HistoryManager`` は失敗時に
      id 無しの dict を返すことがあるため、dict の有無では判定しない)。
    - **保存された本文** (正規化後に行へ入った content) が空でない。
      正規化前のテキストで判定すると、除去後に空で確定した行 (発言では
      ない) にまで「発言が保存された」の信号が流れる (Codex #6)。
    """
    if event_callback is None or not isinstance(building_msg, dict):
        return
    message_id = building_msg.get("message_id")
    content = str(building_msg.get("content") or "")
    if not message_id or not content.strip():
        return
    try:
        event_callback({
            "type": SPEAK_PERSISTED_EVENT_TYPE,
            "message_id": str(message_id),
            "persona_id": getattr(persona, "persona_id", None),
            "pulse_id": pulse_id,
        })
    except Exception:
        LOGGER.warning(
            "notify_speak_persisted: could not deliver the persistence signal "
            "(msg=%s)", message_id, exc_info=True,
        )


@dataclass
class SpeakFinalizeResult:
    """``emit_speak_finalize`` の三値の結果。

    「呼んだ = 保存できた」をやめるための器 (2026-08-29 設計裁定)。呼び出し元は
    ``status`` で三つを区別する:

    - ``"saved"``: 建物履歴の行に本文が入った。``building_msg`` に確定後の行。
    - ``"missing"``: 更新対象の placeholder 行が見つからなかった。
    - ``"failed"``: 保存に失敗した。``error`` に例外を写す。

    ``status`` は**建物履歴の行の永続化の成否だけ**で決まる (2026-08-29 裁定)。
    ペルソナ履歴・gateway 配信の失敗は ERROR ログに残るが saved を覆さない。

    例外は上へ投げない — 結果型で返し、処理は止めない。
    設計: docs/issues/archive/stream_completion_is_not_proof_of_persistence.md
    """

    status: str
    building_msg: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    @property
    def saved_message_id(self) -> Optional[str]:
        """保存できた回の建物メッセージ id。保存できていなければ None。

        保存された本文 (正規化後に行へ入った content) は ``building_msg``
        が運ぶ — 保存完了イベントの発火判定 (:func:`notify_speak_persisted`)
        はその非空を見る (Codex #6)。
        """
        if self.status == "saved" and isinstance(self.building_msg, dict):
            mid = self.building_msg.get("message_id")
            return str(mid) if mid else None
        return None


class RuntimeEmitters:
    """Emit/output helpers delegated from SEARuntime."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def emit_speak(
        self,
        persona: Any,
        building_id: str,
        text: str,
        pulse_id: Optional[str] = None,
        record_history: bool = True,
        extra_metadata: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        # pulse_id は messages.pulse_id 専用カラムに直接書く (Phase 2.5, 2026-05-01)。
        # `pulse:{uuid}` タグへの併行記録は Phase 3 段階 4-D で廃止 (2026-05-09)。
        if pulse_id:
            msg["pulse_id"] = pulse_id
        # Build metadata with tags and conversation partners
        metadata: Dict[str, Any] = {"tags": ["conversation"]}
        # Merge extra metadata (reasoning, reasoning_details, etc.)
        if isinstance(extra_metadata, dict):
            for key, value in extra_metadata.items():
                if key == "tags":
                    extra_tags = [str(t) for t in value if t] if isinstance(value, list) else []
                    metadata["tags"].extend(extra_tags)
                else:
                    metadata[key] = value

        # Add conversation partners to "with" field
        partners = []
        occupants = self.runtime.manager.occupants.get(building_id, [])
        for oid in occupants:
            if oid != persona.persona_id:
                partners.append(oid)
        presence = getattr(self.runtime.manager, "user_presence_status", "offline")
        if presence in ("online", "away"):
            partners.append("user")
        if partners:
            metadata["with"] = partners

        msg["metadata"] = metadata
        building_msg: Optional[Dict[str, Any]] = None
        building_content_for_hook: Optional[str] = None
        msg_id_for_hook: Optional[str] = None
        if record_history:
            try:
                from saiverse.content_tags import strip_in_heart, strip_user_only
                heard_by_list = list(occupants)
                if persona.persona_id not in heard_by_list:
                    heard_by_list.append(persona.persona_id)
                # SAIMemory: 生のテキスト（<in_heart>タグ含む）を保存。ペルソナが
                # 書いた item:N は安定 short_id なのでそのまま記憶に残せる。
                persona.history_manager.add_to_persona_only(msg)
                # building_histories / gateway: <in_heart>除去。item 参照は item:N /
                # saiverse://item/N が既に安定・world共通なので解決 pin は不要。
                building_content = strip_in_heart(text)
                # text_for_voice: <user_only> ブロックを完全除去 (TTS / 音声系
                # アドオンに UI 専用の HTML 等を読み上げさせないため)。emit_speak
                # は通常 wrap_spell_blocks を経由しないが、ペルソナが手書きで
                # <user_only> を出力するケースに備えて防御的に適用する。
                building_content_for_hook = strip_user_only(building_content)
                building_msg_dict = {**msg, "content": building_content}
                building_msg = persona.history_manager.add_to_building_only(
                    building_id, building_msg_dict, heard_by=heard_by_list,
                )
                # BuildingHistory保存完了直後にmessage_idを確定させる。
                # これにより後続のアドオンツール（TTSなど）が get_active_message_id() で
                # 正しいIDを取得してメタデータを紐付けられる。
                msg_id = building_msg.get("message_id") if building_msg else None
                if msg_id:
                    from tools.context import set_active_message_id
                    set_active_message_id(str(msg_id))
                    msg_id_for_hook = str(msg_id)
                self.runtime.manager.gateway_handle_ai_replies(building_id, persona, [building_content])
            except Exception:
                LOGGER.exception("Failed to emit speak message")
        # 保存完了イベント: 建物の行に本文が入った回だけ流れる (判定は
        # notify_speak_persisted に一本化。message_id 無し = insert 失敗)。
        notify_speak_persisted(event_callback, building_msg, persona, pulse_id)
        self.notify_unity_speak(persona, text)
        # アドオン向けサーバー側 hook (persona_speak イベント) を発火する。
        # ThreadPoolExecutor で隔離実行されるため本関数は即座に return する。
        # See docs/intent/addon_speak_hooks.md.
        if record_history and msg_id_for_hook:
            try:
                from saiverse.addon_hooks import dispatch_hook
                from saiverse.content_tags import strip_in_heart, strip_user_only
                voice_text = (
                    building_content_for_hook
                    if building_content_for_hook is not None
                    else strip_user_only(strip_in_heart(text))
                )
                dispatch_hook(
                    "persona_speak",
                    order_key=msg_id_for_hook,
                    persona_id=persona.persona_id,
                    building_id=building_id,
                    text_raw=text,
                    text_for_voice=voice_text,
                    message_id=msg_id_for_hook,
                    pulse_id=pulse_id,
                    source="speak",
                    metadata=dict(metadata),
                )
            except Exception:
                LOGGER.warning("persona_speak hook dispatch failed", exc_info=True)
        return building_msg

    def emit_say(
        self,
        persona: Any,
        building_id: str,
        text: str,
        pulse_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        # 層1マーカー (==語句==) は表示系シンク (建物履歴 / gateway / Unity /
        # TTS hook) には流さない (life_concept_map.md §9.1 / P3)。mark の保存は
        # SAIMemory 側の _store_memory が担うので、ここでは剥離のみ。
        text = strip_marks(text)
        msg = {"role": "assistant", "content": text, "persona_id": persona.persona_id}
        # Phase 3 段階 4-D (2026-05-09): pulse_id は専用カラムへ。タグ併行記録廃止。
        if pulse_id:
            msg["pulse_id"] = pulse_id
        msg_metadata: Dict[str, Any] = {}
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                if key == "tags":
                    extra_tags = [str(t) for t in value if t] if isinstance(value, list) else []
                    msg_metadata.setdefault("tags", []).extend(extra_tags)
                else:
                    msg_metadata[key] = value

        partners = []
        occupants = self.runtime.manager.occupants.get(building_id, [])
        for oid in occupants:
            if oid != persona.persona_id:
                partners.append(oid)
        presence = getattr(self.runtime.manager, "user_presence_status", "offline")
        if presence in ("online", "away"):
            partners.append("user")
        if partners:
            msg_metadata["with"] = partners

        if msg_metadata:
            msg["metadata"] = msg_metadata
        building_msg: Optional[Dict[str, Any]] = None
        building_content_for_hook: Optional[str] = None
        msg_id_for_hook: Optional[str] = None
        try:
            from saiverse.content_tags import strip_in_heart, strip_user_only, wrap_spell_blocks
            heard_by_list = list(occupants)
            if persona.persona_id not in heard_by_list:
                heard_by_list.append(persona.persona_id)
            # スペルブロックを <user_only alt="Name"> でラッピング、<in_heart> を除去。
            # item 参照 (item:N / saiverse://item/N) は安定・world共通なので解決 pin 不要。
            building_content = wrap_spell_blocks(strip_in_heart(text))
            # text_for_voice: <user_only> ブロック (= スペル HTML 詳細含む) を
            # 完全除去する。voice/外部出力系アドオン (TTS, gateway audio 等) が
            # スペル名・引数・結果といった UI 専用要素を読み上げないようにするため。
            # building_content 自体は UI / 履歴用に <user_only> ラップを保持する。
            building_content_for_hook = strip_user_only(building_content)
            building_msg_for_hist = {**msg, "content": building_content}
            building_msg = persona.history_manager.add_to_building_only(
                building_id, building_msg_for_hist, heard_by=heard_by_list,
            )
            # BuildingHistory 保存完了直後に message_id を ContextVar に確定させる。
            # 後続のアドオンツール (TTS 等) が get_active_message_id() で正しい ID を
            # 取得できるよう、emit_speak と同様の配線を行う。
            msg_id = building_msg.get("message_id") if building_msg else None
            if msg_id:
                from tools.context import set_active_message_id
                set_active_message_id(str(msg_id))
                msg_id_for_hook = str(msg_id)
            self.runtime.manager.gateway_handle_ai_replies(building_id, persona, [building_content])
        except Exception:
            LOGGER.exception("Failed to emit say message")
        # 保存完了イベント: 建物の行に本文が入った回だけ流れる (判定は
        # notify_speak_persisted に一本化。message_id 無し = insert 失敗)。
        notify_speak_persisted(event_callback, building_msg, persona, pulse_id)
        self.notify_unity_speak(persona, text)
        # アドオン向けサーバー側 hook (persona_speak イベント) を発火する。
        # emit_speak と同一イベントに統合し、source="say" で区別する。
        # See docs/intent/addon_speak_hooks.md.
        if msg_id_for_hook:
            try:
                from saiverse.addon_hooks import dispatch_hook
                from saiverse.content_tags import strip_in_heart, strip_user_only
                voice_text = (
                    building_content_for_hook
                    if building_content_for_hook is not None
                    else strip_user_only(strip_in_heart(text))
                )
                dispatch_hook(
                    "persona_speak",
                    order_key=msg_id_for_hook,
                    persona_id=persona.persona_id,
                    building_id=building_id,
                    text_raw=text,
                    text_for_voice=voice_text,
                    message_id=msg_id_for_hook,
                    pulse_id=pulse_id,
                    source="say",
                    metadata=dict(msg_metadata) if msg_metadata else {},
                )
            except Exception:
                LOGGER.warning("persona_speak hook dispatch failed", exc_info=True)
        return building_msg

    # ---------- Pipeline Streaming (Phase 2-β) -------------------------
    # emit_speak の 2 段階 API。 LLM streaming 開始時に placeholder を登録 →
    # 文区切りごとに sub-speak hook 発火 (= TTS 側で並走合成) → streaming
    # 完了で finalize して全文記録 + 最終 hook 発火、 という 3-step フロー
    # を組むためのビルディングブロック。 詳細:
    # docs/intent/voice_tts_pipeline_streaming.md

    def emit_speak_start(
        self,
        persona: Any,
        building_id: str,
        pulse_id: Optional[str] = None,
    ) -> Optional[str]:
        """Streaming 開始時に空 content の placeholder を building history に
        登録し、 発番された message_id を返す。

        sub-speak hook で voice-tts に渡す ``message_id`` を事前に確定させる
        ためだけのコール。 persona-only history と SAIMemory には触らない
        (= placeholder 期間中は text が未確定なので、 ここで record すると
        「空メッセージを発した」 履歴になる)。 確定処理は ``emit_speak_finalize``
        側に集約する。

        ``set_active_message_id`` で contextvar も更新するので、 後続のツール
        呼び出しが get_active_message_id で同じ ID を取れる。
        """
        placeholder_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "persona_id": persona.persona_id,
        }
        if pulse_id:
            placeholder_msg["pulse_id"] = pulse_id
        placeholder_msg["metadata"] = {
            "tags": ["conversation"],
            "_streaming_placeholder": True,
        }

        occupants = self.runtime.manager.occupants.get(building_id, [])
        heard_by = list(occupants)
        if persona.persona_id not in heard_by:
            heard_by.append(persona.persona_id)

        try:
            building_msg = persona.history_manager.add_to_building_only(
                building_id, placeholder_msg, heard_by=heard_by,
            )
        except Exception:
            LOGGER.exception("emit_speak_start: failed to register placeholder")
            return None

        msg_id = building_msg.get("message_id") if building_msg else None
        if msg_id:
            from tools.context import set_active_message_id
            set_active_message_id(str(msg_id))
            LOGGER.debug(
                "emit_speak_start: placeholder msg_id=%s persona=%s building=%s pulse=%s",
                msg_id, persona.persona_id, building_id, pulse_id,
            )
        return str(msg_id) if msg_id else None

    def emit_sub_speak(
        self,
        persona: Any,
        building_id: str,
        message_id: str,
        sub_text: str,
        sub_seq: int,
        pulse_id: Optional[str] = None,
    ) -> None:
        """文区切り sub-text を voice-tts 等の subscriber に届ける hook 発火専用。

        history への記録は finalize でまとめて行うので、 ここでは
        ``persona_speak`` hook を ``sub_seq=N`` + ``is_final=False`` で発火
        するだけ。 voice-tts addon は sub_seq の小さい順で同 message_id 内
        の audio_stream に連結 push する (= Phase 2-α)。
        """
        if not sub_text:
            return
        try:
            from saiverse.addon_hooks import dispatch_hook
            from saiverse.content_tags import strip_in_heart, strip_user_only
            # 層1マーカーを音声に読み上げさせない。マーカーが文区切りを跨いだ
            # 場合は閉じない "==" として残る (剥がせない) が、ペルソナが記法を
            # 知らない現状では実害なし。
            text_for_voice = strip_marks(strip_user_only(strip_in_heart(sub_text)))
            if not text_for_voice:
                return
            dispatch_hook(
                "persona_speak",
                order_key=message_id,
                persona_id=persona.persona_id,
                building_id=building_id,
                text_raw=sub_text,
                text_for_voice=text_for_voice,
                message_id=message_id,
                pulse_id=pulse_id,
                source="speak",
                metadata={},
                sub_seq=sub_seq,
                is_final=False,
            )
            LOGGER.debug(
                "emit_sub_speak: msg=%s sub_seq=%d len=%d",
                message_id, sub_seq, len(sub_text),
            )
        except Exception:
            LOGGER.warning("persona_speak (sub_speak) hook dispatch failed", exc_info=True)

    def emit_speak_finalize(
        self,
        persona: Any,
        building_id: str,
        message_id: str,
        text: str,
        pulse_id: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        final_sub_seq: Optional[int] = None,
        final_voice_text: Optional[str] = None,
    ) -> SpeakFinalizeResult:
        """emit_speak_start で発番した placeholder の content を確定する。

        戻り値は :class:`SpeakFinalizeResult` の三値 — 保存成功 (行 id つき) /
        対象行なし / 保存失敗 (例外を写す)。例外は上へ投げない。

        - building history: update_building_message で content + metadata を
          確定。 ``_streaming_placeholder`` フラグも False に倒す
        - persona-only history: 全文を add (= placeholder 期間中は不在だった)
        - SAIMemory: persona-only 経路の add で _sync_to_memory が走る
        - gateway: ai_replies broadcast
        - persona_speak hook: ``sub_seq=final_sub_seq`` + ``is_final=True``
          で 1 回発火。 voice-tts はここで is_final=True を受け取って stream
          を close + wav 保存する

        ``final_sub_seq``: sub-speak 経路を使った場合は最終 sub-text の連番。
        使ってない場合 (= 文区切りせず全文 1 回で finalize) は ``None`` で OK
        で、 voice-tts は sub_seq=None で従来通りの 「1 message=1 job」 動作
        になる。

        ``final_voice_text``: voice-tts hook の ``text_for_voice`` に渡す
        テキスト。 None なら ``text`` (= 全文) から
        ``strip_user_only(strip_in_heart(...))`` で derive する従来挙動。
        Pipeline Streaming で sub-speak 経路を使った場合は、 既に sub-speak
        済の文を再合成しないため、 caller が 「last sub-speak 以降の残テキスト」
        を ``strip_user_only`` 済の形で渡す (= voice_tts_pipeline_streaming
        intent doc Phase 2-C 案 A)。 空文字 ``""`` を渡せば finalize hook で
        voice-tts は 「合成すべきテキスト無し → stream close + wav 保存のみ」
        で完結する。
        """
        # 層1マーカー (==語句==) の剥離 (emit_say と同じ理由)。確定 content /
        # ペルソナ log / gateway / hook の text_raw 全てが綺麗な本文になる。
        # mark の保存は SAIMemory 側 (_store_memory) が担う。
        text = strip_marks(text)
        if final_voice_text:
            final_voice_text = strip_marks(final_voice_text)
        metadata: Dict[str, Any] = {"tags": ["conversation"]}
        if isinstance(extra_metadata, dict):
            for key, value in extra_metadata.items():
                if key == "tags":
                    extra_tags = (
                        [str(t) for t in value if t] if isinstance(value, list) else []
                    )
                    metadata["tags"].extend(extra_tags)
                else:
                    metadata[key] = value

        occupants = self.runtime.manager.occupants.get(building_id, [])
        partners = [oid for oid in occupants if oid != persona.persona_id]
        presence = getattr(self.runtime.manager, "user_presence_status", "offline")
        if presence in ("online", "away"):
            partners.append("user")
        if partners:
            metadata["with"] = partners

        building_msg: Optional[Dict[str, Any]] = None
        building_content: Optional[str] = None
        text_for_voice: Optional[str] = None
        result_status = "failed"
        result_error: Optional[str] = None

        # ── 保存の本体: 建物履歴の行の確定。status はここだけで決まる ──
        # 信号の契約は「建物履歴の行に本文が入った」(2026-08-29 まはー裁定)。
        # 続きの生成の印降ろしも retry の門番も見るのは建物の行なので、
        # 後続の副作用 (ペルソナ履歴・gateway) の失敗で saved を覆すと、
        # 受け手の見ている場所と status の意味がずれる。
        # Codex の outbox / 状態機械案は採らない — 建物の行を唯一の判定点に
        # 据える最小の分離で、信号の受け手全員の契約が揃うため。
        try:
            from saiverse.content_tags import strip_in_heart, strip_user_only
            building_content = strip_in_heart(text)
            text_for_voice = strip_user_only(building_content)

            update_metadata = dict(metadata)
            update_metadata["_streaming_placeholder"] = False
            building_msg = persona.history_manager.update_building_message(
                building_id, message_id,
                content=building_content,
                metadata=update_metadata,
            )
            if building_msg is None:
                result_status = "missing"
                LOGGER.error(
                    "emit_speak_finalize: placeholder msg_id=%s not found "
                    "for building=%s — finalizing without history update",
                    message_id, building_id,
                )
            elif (building_msg.get("metadata") or {}).get(
                "_streaming_placeholder"
            ):
                # update_building_message は DB エラーを内側で握って戻る。
                # 更新後に読み直した行がまだ下書きの印を付けたままなら、
                # 書き込みは載っていない — 「保存できた」と言ってはいけない。
                result_status = "failed"
                result_error = "placeholder row was not updated"
                LOGGER.error(
                    "emit_speak_finalize: placeholder msg_id=%s still marked "
                    "as a draft after the update (building=%s) — the write "
                    "did not land",
                    message_id, building_id,
                )
            else:
                result_status = "saved"
        except Exception as exc:
            result_status = "failed"
            result_error = repr(exc)
            LOGGER.exception("emit_speak_finalize: failed to finalize placeholder")

        # ── 副作用: ペルソナ履歴 + gateway 配信 ──
        # ここの失敗は ERROR ログに残すが status には触らない — 建物の行は
        # もう入っており、「保存されていない」と報告すると受け手 (続きの印 /
        # 門番) の見る事実と食い違う。欠けた側の記録は個別に追える。
        try:
            persona_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": text,
                "persona_id": persona.persona_id,
                "metadata": dict(metadata),
            }
            if pulse_id:
                persona_msg["pulse_id"] = pulse_id
            # ``sync_to_memory=False``: SAIMemory への転写はスキップする。
            # Pipeline Streaming の確定経路ではスペルループ内で 「各ラウンド
            # の発言部分 + スペル起動行」 と 「スペル結果」 を既に SAIMemory
            # に書いており、 最終発言の単独レコードは後段の memorize ノードが
            # ``state["last"]`` を保存する経路で残る。 ここでさらに全文を
            # SAIMemory に書き込むと、 同 pulse 内に内容完全一致の重複レコード
            # が生まれる (= 旧コードでも起きなかった現象)。 これを防ぐため
            # ペルソナ履歴 (log.json) と建物履歴 (= update_building_message
            # 経由) には全文 1 件を残しつつ、 SAIMemory には書かない。
            persona.history_manager.add_to_persona_only(
                persona_msg, sync_to_memory=False,
            )
        except Exception:
            LOGGER.exception(
                "emit_speak_finalize: persona-history append failed "
                "(building row status=%s msg=%s) — the building row stands",
                result_status, message_id,
            )
        try:
            if building_content is not None:
                self.runtime.manager.gateway_handle_ai_replies(
                    building_id, persona, [building_content],
                )
        except Exception:
            LOGGER.exception(
                "emit_speak_finalize: gateway delivery failed "
                "(building row status=%s msg=%s) — the building row stands",
                result_status, message_id,
            )

        self.notify_unity_speak(persona, text)

        try:
            from saiverse.addon_hooks import dispatch_hook
            # Pipeline Streaming (案 A): caller が指定した ``final_voice_text``
            # が最優先 (= sub-speak 経路で既に発話済みの prefix を再合成しない
            # ため、 caller が remainder を渡す)。 None なら従来通り全文から
            # derive する。 ``text_for_voice`` 計算結果 (building_content 経由)
            # も None の時は full-text fallback として使う。
            if final_voice_text is not None:
                hook_text_for_voice = final_voice_text
            elif text_for_voice is not None:
                hook_text_for_voice = text_for_voice
            else:
                from saiverse.content_tags import strip_in_heart, strip_user_only
                hook_text_for_voice = strip_user_only(strip_in_heart(text))
            dispatch_hook(
                "persona_speak",
                order_key=message_id,
                persona_id=persona.persona_id,
                building_id=building_id,
                text_raw=text,
                text_for_voice=hook_text_for_voice,
                message_id=message_id,
                pulse_id=pulse_id,
                source="speak",
                metadata=dict(metadata),
                sub_seq=final_sub_seq,
                is_final=True,
            )
        except Exception:
            LOGGER.warning(
                "persona_speak (finalize) hook dispatch failed", exc_info=True,
            )

        return SpeakFinalizeResult(
            status=result_status,
            building_msg=building_msg,
            error=result_error,
        )

    def emit_think(
        self,
        persona: Any,
        pulse_id: str,
        text: str,
        record_history: bool = True,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """THINK ノードの独白を SAIMemory (memory.db) へ 1 行残す。

        ``extra_metadata``: 書き込み時の機械刻印など、tags 以外に載せる欄。
        ``tags`` キーは既定の ``internal`` に追記される。
        """
        if not record_history:
            return
        adapter = getattr(persona, "sai_memory", None)
        try:
            if adapter and adapter.is_ready():
                think_metadata: Dict[str, Any] = {"tags": ["internal"]}
                if isinstance(extra_metadata, dict):
                    for key, value in extra_metadata.items():
                        if key == "tags":
                            extra_tags = (
                                [str(t) for t in value if t] if isinstance(value, list) else []
                            )
                            think_metadata["tags"].extend(extra_tags)
                        else:
                            think_metadata[key] = value
                msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": text,
                    "metadata": think_metadata,
                    # Phase 3 段階 4-D (2026-05-09): pulse_id 専用カラムへ。
                    # 旧 `pulse:{uuid}` タグの併行記録は廃止。
                    "pulse_id": pulse_id,
                    "persona_id": persona.persona_id,
                }
                adapter.append_persona_message(msg)
        except Exception:
            LOGGER.warning("think message not stored", exc_info=True)

    def notify_unity_speak(self, persona: Any, text: str) -> None:
        """Send persona speak event to Unity Gateway if connected."""
        if not text:
            return
        unity_gateway = getattr(self.runtime.manager, "unity_gateway", None)
        if not unity_gateway:
            return
        try:
            persona_id = getattr(persona, "persona_id", "unknown")
            try:
                asyncio.get_running_loop()
                asyncio.create_task(unity_gateway.send_speak(persona_id, text))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(unity_gateway.send_speak(persona_id, text))
                loop.close()
        except Exception as exc:
            LOGGER.debug("Failed to notify Unity Gateway: %s", exc)
