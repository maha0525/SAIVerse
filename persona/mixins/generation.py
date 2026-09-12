"""Messaging and LLM generation helpers shared by persona core.

旧 pre-SEA LLM 生成パス (_generate / _build_messages / handle_user_input 等) は
SEA runtime への移行完了に伴い 2026-06 に撤去済。
モデル切替 (set_model / apply_parameter_overrides) は SEA runtime 経由でも必要。

接続 (LLM クライアント) の持ち方 (docs/intent/persona_model_selection.md):

- モデルの設定を書き換える操作 (``set_model`` / ``set_lightweight_model`` /
  ``drop_llm_clients``) は、値を書き換えて作ってあった接続を捨てるだけにする。
  新しい接続は、次の返事を始めたあとで初めて要ったときに作られる。
- 書き換えるたびに「設定の世代札」(``_model_settings_token``) を取り替える。
  接続を作ってペルソナに持たせるのは、作り始めたときの札がまだ有効なときだけ
  (``_install_client_if_current``)。作っている途中で設定が変わったら、古い
  モデルの接続をペルソナに残さない。
- 書いている途中の返事は、ペルソナが持つ接続ではなく、返事の始まりに決めた
  モデルと接続を使う (saiverse/persona_model_selection.py の ReplyModelBinding)。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from saiverse.model_configs import model_supports_images, get_model_parameters


class PersonaGenerationMixin:
    """Model switching and parameter override helpers for PersonaCore."""

    def _init_model_client_state(self) -> None:
        """接続の持ち方の初期状態。PersonaCore.__init__ が一度だけ呼ぶ。"""
        self._model_client_lock = threading.RLock()
        self._model_settings_token = object()
        self._llm_client = None
        self._lightweight_llm_client = None
        self._lightweight_llm_client_initialized = False
        # 軽量モデルの接続を作れなかった理由。作れなかった回は覚えずに次に要ったとき
        # 作り直すので、ここは「最後に失敗した理由」を返事の側へ伝えるためだけにある。
        self._lightweight_llm_client_error: Optional[BaseException] = None
        self._pending_parameter_overrides: Optional[Dict[str, Any]] = None

    def set_model(
        self,
        model: str,
        context_length: int,
        provider: str,
        parameter_overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        """話す標準モデルの値を書き換え、作ってあった標準モデルの接続を捨てる。

        ``parameter_overrides`` はチャット画面の一時上書きのパラメータ。渡さなければ
        前の上書きのパラメータも消える (上書きを解除したあとに古い値が新しい接続へ
        載らないように)。
        """
        with self._model_client_lock:
            self.model = model
            self.provider = provider
            self.context_length = context_length
            self.model_supports_images = model_supports_images(model)
            self._pending_parameter_overrides = (
                dict(parameter_overrides) if parameter_overrides else None
            )
            self._llm_client = None
            self._model_settings_token = object()

    def set_lightweight_model(self, lightweight_model: Optional[str]) -> None:
        """個別の軽量モデルの値を書き換え、変わったときだけ軽量モデルの接続を捨てる。"""
        value = lightweight_model or None
        with self._model_client_lock:
            if getattr(self, "lightweight_model", None) == value:
                return
            self.lightweight_model = value
            self._lightweight_llm_client = None
            self._lightweight_llm_client_initialized = False
            self._lightweight_llm_client_error = None
            self._model_settings_token = object()

    def drop_llm_clients(self) -> None:
        """作ってあった標準・軽量モデルの接続を捨てる (モデルやプロバイダの設定の読み直し後)。"""
        with self._model_client_lock:
            self._llm_client = None
            self._lightweight_llm_client = None
            self._lightweight_llm_client_initialized = False
            self._lightweight_llm_client_error = None
            self._model_settings_token = object()

    def _install_client_if_current(self, tier: str, token: object, client: Any) -> Any:
        """作り始めたときの世代札がまだ有効なら、接続をペルソナに持たせる。

        返り値はこの呼び出しの後で使う接続。同じ世代の接続を別の呼び出しが先に
        持たせていたらそちらを返す (同じ設定の接続を二つ持たない)。札が変わって
        いたら持たせずに、作った接続をそのまま返す (呼び出し元の一回には使ってよい)。
        """
        with self._model_client_lock:
            if self._model_settings_token is not token:
                return client
            if tier == "lightweight":
                if self._lightweight_llm_client is None:
                    self._lightweight_llm_client = client
                self._lightweight_llm_client_initialized = True
                self._lightweight_llm_client_error = None
                return self._lightweight_llm_client
            if self._llm_client is None:
                self._llm_client = client
            return self._llm_client

    def _configure_client_parameters(
        self, client: Any, model: str, overrides: Optional[Dict[str, Any]],
    ) -> None:
        if not overrides or client is None:
            return
        allowed = get_model_parameters(model)
        filtered = {key: value for key, value in overrides.items() if key in allowed}
        if not filtered:
            return
        try:
            client.configure_parameters(filtered)
        except Exception:
            logging.debug(
                "Failed to apply parameter overrides for %s",
                getattr(self, "persona_name", None), exc_info=True,
            )

    def apply_parameter_overrides(self, overrides: Optional[Dict[str, Any]] = None) -> None:
        """チャット画面の一時上書きのパラメータを書き換え、作ってあった標準モデルの接続を捨てる。

        作ってあった接続を設定し直さない。書いている途中の返事はその接続を使っているので、
        設定し直すと返事の途中でパラメータが変わってしまう (docs/intent/persona_model_selection.md
        決まったこと 10)。次の返事は、新しいパラメータで作った接続を使う。世代札も取り替える
        ので、書き換えの前に古いパラメータを読んで作り始めていた接続は、ペルソナに持たせない。
        空で呼ばれたら、パラメータの一時上書きを外す。
        """
        with self._model_client_lock:
            self._pending_parameter_overrides = dict(overrides) if overrides else None
            self._llm_client = None
            self._model_settings_token = object()


__all__ = ["PersonaGenerationMixin"]
