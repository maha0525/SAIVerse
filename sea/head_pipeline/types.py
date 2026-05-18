"""Cached Head Architecture: 型定義。

このモジュールは Section interface (Protocol) と、capture/render/diff で
やりとりされるデータ型を定義する。Section 実装本体は別モジュールで、
各 Section が自前の SectionSnapshot dataclass を持つ。

詳細: docs/intent/cached_head_architecture.md
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


class EventType(enum.Enum):
    """snapshot 再構築 / dirty マークを引き起こすイベント種別。

    各 Section は ``refresh_on_events`` でこれらのうちどれを許容するかを宣言する。
    既定 (空 frozenset) は「Metabolism 以外では cache を切らない」を意味する。
    """
    METABOLISM = "metabolism"
    BUILDING_ENTERED = "building_entered"
    SYSTEM_PROMPT_EDITED = "system_prompt_edited"
    ADDON_LOADED = "addon_loaded"
    ADDON_UNLOADED = "addon_unloaded"
    MODEL_CHANGED = "model_changed"
    APPEARANCE_CHANGED = "appearance_changed"


@dataclass(frozen=True)
class MediaRef:
    """head / 通知に乗る非テキストコンテンツの参照。

    Section snapshot や RenderedSection が image / audio 等を保持するときに使う。
    cache 安定性のため snapshot 側は ``path`` (= 同定可能な識別子) を保持し、
    内容が変わらない限り同じ MediaRef が出続けるようにする。
    """
    path: str
    mime_type: str
    role: str  # "image" | "audio" | "document" | ...


@dataclass
class RenderedSection:
    """Section.render の出力。text と media のセット。

    どちらか / 両方を含む。``text=None`` かつ ``media=[]`` の場合は ``render`` が
    ``None`` を返したのと等価扱い (head に何も出さない)。
    """
    text: Optional[str] = None
    media: list[MediaRef] = field(default_factory=list)


@dataclass
class NotificationLabel:
    """末尾通知 1 件分のラベル。

    Section.diff_to_notifications が返す。pipeline は同 Pulse 内で得た全ラベルを
    まとめて [システム通知] 形式の 1 メッセージとして末尾に注入する。
    """
    kind: str   # 機械可読の識別子 ("spell_added", "building_image_changed" 等)
    label: str  # ペルソナに見せる文 ("移動先 Building で新たに使えるようになったスペル: …")
    media: list[MediaRef] = field(default_factory=list)  # 必要なら新コンテンツを tail に attach


@dataclass
class LineHeadInput:
    """Section.capture に渡される world アクセサ。

    Phase 1 段階では生の ``manager`` 参照を許容する (各 Section が必要とする
    accessor の最小集合は Phase 2 移植時に Section ごとに確定する)。
    将来的に method ベースの限定 surface にして「live state 直参照」を物理的に
    封じる方針。詳細: docs/intent/cached_head_architecture.md §8.1
    """
    persona_id: str
    line_id: str
    line_role: str        # "main_line" | "sub_line" | "meta_judgment" | "nested"
    model_key: str        # per-model anchor 紐付け用 (例: "claude-opus-4-7", "gemini-2.5-flash")
    current_building_id: Optional[str] = None
    persona: Any = None   # 一時、Phase 2 で accessor 化
    manager: Any = None   # 一時、Phase 2 で accessor 化


@dataclass
class LineHeadSnapshot:
    """ライン単位の凍結 head 状態。

    Section.name → SectionSnapshot の dict を保持する。snapshot 更新は
    Metabolism / refresh_on_events のみで起きる (= 平時は frozen)。

    永続化: 1 行 = 1 ペルソナの 1 ライン。``sections`` 全体を JSON 化して保存。
    """
    persona_id: str
    line_id: str
    line_role: str
    model_key: str
    captured_at: float       # epoch seconds
    snapshot_version: int    # 監査用、capture 毎に bump
    sections: dict[str, Any] = field(default_factory=dict)  # name -> SectionSnapshot


@runtime_checkable
class HeadSection(Protocol):
    """Head の 1 section を表す Protocol。

    capture / render / diff_to_notifications / serialize / deserialize の
    5 つすべてを実装した型のみ ``HeadSectionRegistry.register`` で登録できる。
    1 つでも欠けると Protocol 不一致で登録拒否される (型レベルの強制)。

    各 Section は自前の SectionSnapshot dataclass を定義し、capture/render/diff の
    引数・戻り値の型を一貫させる責任を持つ (Protocol 側では ``Any`` で受ける)。

    詳細: docs/intent/cached_head_architecture.md §3.1
    """

    @property
    def name(self) -> str:
        """Section 識別子。LineHeadSnapshot.sections の key、registry 内の一意名。"""
        ...

    @property
    def order(self) -> int:
        """Head 内の出現順。小さい順に並ぶ。"""
        ...

    @property
    def refresh_on_events(self) -> frozenset[EventType]:
        """snapshot 再構築を許可するイベント。空 frozenset() = Metabolism のみ。

        Metabolism は常に全 Section に対して capture を走らせるため、明示列挙不要。
        """
        ...

    def capture(self, ctx: LineHeadInput) -> Any:
        """live state から SectionSnapshot を作る。

        snapshot 構築タイミング (= Metabolism / refresh_on_events 発火) でのみ呼ばれる。
        平時は呼ばれない (= 同じ snapshot が render される)。
        """
        ...

    def render(self, snapshot: Any) -> Optional[RenderedSection]:
        """SectionSnapshot から head のコンテンツを作る。

        snapshot 以外の引数を取らない (= live state 参照不可)。``None`` 返却で
        この section は head に何も出さない (= 空 section)。
        """
        ...

    def diff_to_notifications(
        self, old: Any, new: Any,
    ) -> list[NotificationLabel]:
        """snapshot 間の差分を末尾通知ラベル列に変換する。

        ``old`` は最後に通知済みの snapshot (B 相当)、``new`` は live state から
        いま capture した snapshot (C 相当)。差分なしなら空 list を返す。
        """
        ...

    def serialize_snapshot(self, snapshot: Any) -> str:
        """SectionSnapshot を JSON 文字列に直列化する (DB 永続化用)。"""
        ...

    def deserialize_snapshot(self, data: str) -> Any:
        """JSON 文字列から SectionSnapshot を復元する。"""
        ...
