"""Cached Head Architecture: Section registry.

Section 実装をプロセス内 1 個の registry に登録し、order 順に列挙する。
登録時に Protocol 充足を検査し、capture/render/diff/serialize/deserialize の
どれかが欠けた実装は弾く (= 型レベルで差分通知忘れ等を防ぐ)。

詳細: docs/intent/cached_head_architecture.md §3.4
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from sea.head_pipeline.types import EventType, HeadSection

LOGGER = logging.getLogger(__name__)


class HeadSectionRegistry:
    """Section の登録 / 列挙を担う。プロセス内 singleton として運用する想定。

    - ``register`` で Protocol 適合をチェック。同名再登録は ValueError。
    - ``all_sections`` は ``order`` 昇順、同点なら登録順を保つ。
    - ``sections_for_event`` は refresh_on_events に該当する Section の一覧を返す
      (= dirty マーク対象の絞り込み用)。
    """

    def __init__(self) -> None:
        self._sections: dict[str, HeadSection] = {}
        self._registration_order: list[str] = []
        self._lock = threading.RLock()

    def register(self, section: HeadSection) -> None:
        """Section を登録する。Protocol 不一致 / 同名重複は拒否。"""
        if not isinstance(section, HeadSection):
            # runtime_checkable Protocol の isinstance チェックで属性の有無を見る。
            # capture/render/diff/serialize/deserialize/name/order/refresh_on_events
            # のいずれかが欠けていればここで弾かれる。
            raise TypeError(
                f"Section {section!r} does not satisfy HeadSection protocol "
                "(must define name, order, refresh_on_events, capture, render, "
                "diff_to_notifications, serialize_snapshot, deserialize_snapshot)"
            )
        name = section.name
        if not name or not isinstance(name, str):
            raise ValueError(f"Section.name must be a non-empty string, got {name!r}")

        with self._lock:
            if name in self._sections:
                raise ValueError(f"Section '{name}' is already registered")
            self._sections[name] = section
            self._registration_order.append(name)
            LOGGER.info(
                "head_pipeline: registered section name=%s order=%d refresh_on=%s",
                name, section.order, sorted(e.value for e in section.refresh_on_events),
            )

    def unregister(self, name: str) -> Optional[HeadSection]:
        """Section の登録を解除する。存在しなければ None を返す。

        テスト / 動的 reload 用。本番運用での頻繁な呼び出しは想定しない。
        """
        with self._lock:
            section = self._sections.pop(name, None)
            if section is not None:
                try:
                    self._registration_order.remove(name)
                except ValueError:
                    pass
                LOGGER.info("head_pipeline: unregistered section name=%s", name)
            return section

    def by_name(self, name: str) -> Optional[HeadSection]:
        with self._lock:
            return self._sections.get(name)

    def all_sections(self) -> list[HeadSection]:
        """``order`` 昇順、同点は登録順を保ったまま返す。"""
        with self._lock:
            items = [(self._sections[n], self._registration_order.index(n))
                     for n in self._sections]
        items.sort(key=lambda pair: (pair[0].order, pair[1]))
        return [section for section, _ in items]

    def required_section_names(self) -> frozenset[str]:
        """``required = True`` を宣言した Section の名前集合 (W6 fail-closed)。

        required は Protocol 外の任意属性 (未宣言 = False)。この集合に入る
        Section の capture/render/persist 失敗は LLM 実行を止める。
        """
        with self._lock:
            return frozenset(
                name for name, s in self._sections.items()
                if getattr(s, "required", False)
            )

    def sections_for_event(self, event: EventType) -> list[HeadSection]:
        """``event`` を ``refresh_on_events`` に含む Section だけを返す。

        Metabolism は全 Section が対象なので、このメソッドの呼び出し側で
        ``event == EventType.METABOLISM`` 時は ``all_sections()`` を使うこと。
        """
        with self._lock:
            return [s for s in self._sections.values() if event in s.refresh_on_events]

    def clear(self) -> None:
        """全 Section を解除する。テスト fixture 用。"""
        with self._lock:
            self._sections.clear()
            self._registration_order.clear()


_default_registry: Optional[HeadSectionRegistry] = None
_default_registry_lock = threading.Lock()


def get_default_registry() -> HeadSectionRegistry:
    """プロセス内 singleton registry を返す。startup 時の Section 登録に使う。"""
    global _default_registry
    if _default_registry is None:
        with _default_registry_lock:
            if _default_registry is None:
                _default_registry = HeadSectionRegistry()
    return _default_registry
