"""プロバイダ定義を変えたとき、ペルソナが使う接続先が変わるかを隔離環境で観測する (FLOW-28)。

何を確かめるものか
------------------
``docs/issues/provider_change_does_not_reach_live_personas.md`` (2026-08-05 起票) は、
UI からプロバイダの接続先を変えても **すでに LLM クライアントを作ったペルソナは
再起動まで変更前の接続へ送り続ける** と書いている。本スクリプトはその主張の各項を
現行コードで一つずつ観測する。

観測するのは 6 点。

  A. 生成済みのクライアントが、プロバイダ保存後も古い接続先を保持するか
  B. 保存によってモデル側の解決結果 (``MODEL_CONFIGS``) は新しくなっているか
  C. まだクライアントを作っていない側は、新しい接続先で作られるか
     (= 同一プロセス内で新旧が混在するか)
  D. 通常用 (``_llm_client``) と軽量用 (``_lightweight_llm_client``) が
     別々に切り替わり、同じペルソナの中で新旧が混ざりうるか
  E. ``api/routes/admin.py:136-138`` が API キー変更時に行っている無効化
     (3 つの属性を落とすだけ) を同じ形で当てると、次のアクセスで新しい接続先になるか
  F. プロバイダを **削除** したあと、生成済みのクライアントが削除前の接続先を
     保持し続けるか
  G. 同じことが **モデル JSON の編集** (``PUT /api/config/models/{key}`` が呼ぶ
     ``model_configs.reload_configs()``) でも起きるか

LLM は呼ばない。観測するのは ``OpenAI`` SDK クライアントが握っている ``base_url``
だけで、リクエストは 1 本も送らない。

``PersonaCore`` そのものは SAIMemory / HistoryManager / DB を要求するため、
本スクリプトは **``persona/core.py`` の遅延生成プロパティの実体オブジェクトを
そのまま借りた** 器 (``_LazyClientHolder``) で観測する。実行されるのは
``persona/core.py:241-283`` の製品コードそのもので、器は属性を持つだけである。

どう実行するか
--------------
リポジトリルートから::

    .venv/Scripts/python.exe \
      docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b6_provider_change_reaches_live_persona.py

隔離: ``SAIVERSE_HOME`` / ``SAIVERSE_USER_DATA_DIR`` を一時ディレクトリへ向け、
合成プロバイダ 1 件と合成モデル 1 件だけを置く。本番の ``~/.saiverse/`` には
一切触れない。``OPENAI_API_KEY`` は親プロセスから引き継がないよう明示的に外す
(観測結果を実行環境に依存させないため)。

何が観測されたか (2026-09-10 実行、HEAD=7d7214be の作業ツリー)
--------------------------------------------------------------
A. 生成済みクライアント: 変更前 ``http://127.0.0.1:19001/v1`` のまま。
   → **プロバイダ保存は、生成済みのクライアントに届かない。**
B. ``MODEL_CONFIGS`` の解決結果: ``http://127.0.0.1:19002/v1`` (新しい方)。
   → 設定の辞書とモデル側の解決は、その場で更新されている。
C. 保存後に初めてクライアントを作った側: ``http://127.0.0.1:19002/v1`` (新しい方)。
   → **同一プロセス内で、新しい接続先のペルソナと古い接続先のペルソナが混在する。**
D. 先に軽量用だけを作っておいたペルソナ: 保存後、
   軽量用 ``http://127.0.0.1:19001/v1`` (古い) / 通常用 ``http://127.0.0.1:19002/v1`` (新しい)。
   → **同じペルソナの中で新旧が混ざる。**
E. ``_llm_client=None`` / ``_lightweight_llm_client=None`` /
   ``_lightweight_llm_client_initialized=False`` を当てた直後:
   通常用・軽量用ともに ``http://127.0.0.1:19002/v1``。
   → 走行中の競合を別にすれば、無効化の一手で届くようになる。
F. プロバイダ削除後: 生成済みクライアントは ``http://127.0.0.1:19002/v1`` を保持し、
   削除前の宛先へ送れる状態のままだった。同じモデルで新しくクライアントを作ると
   ``Unknown provider_ref: audit_b6_local`` (ValueError) で失敗した。
   → **削除は「これから作る分」にしか効かない。**
G. モデル JSON の ``base_url`` を書き換えて ``reload_configs()`` した直後:
   ``MODEL_CONFIGS`` は ``http://127.0.0.1:19012/v1``、生成済みクライアントは
   ``http://127.0.0.1:19011/v1`` のまま。編集後に作った側は新しい方。
   → **同じ食い違いが、プロバイダを経由しないモデル編集でも起きる。**
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT))

# 本番データに触れないよう、import より前に隔離する。
_TMP_HOME = tempfile.mkdtemp(prefix="b6_saiverse_home_")
_TMP_USER_DATA = Path(_TMP_HOME) / "user_data"
os.environ["SAIVERSE_HOME"] = _TMP_HOME
os.environ["SAIVERSE_USER_DATA_DIR"] = str(_TMP_USER_DATA)
# 観測結果を実行環境の資格情報に依存させない。
os.environ.pop("OPENAI_API_KEY", None)

PROVIDER_ID = "audit_b6_local"
MODEL_KEY = "audit_b6_model"
DIRECT_MODEL_KEY = "audit_b6_direct"
OLD_URL = "http://127.0.0.1:19001/v1"
NEW_URL = "http://127.0.0.1:19002/v1"
DIRECT_OLD_URL = "http://127.0.0.1:19011/v1"
DIRECT_NEW_URL = "http://127.0.0.1:19012/v1"


def _write_direct_model(base_url: str) -> None:
    """接続先を自分で直書きする合成モデル (provider を経由しない経路、G 用)。"""
    models_dir = _TMP_USER_DATA / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / f"{DIRECT_MODEL_KEY}.json").write_text(
        json.dumps(
            {
                "model": "audit-b6-direct-dummy",
                "display_name": "audit b6 direct model",
                "provider": "openai",
                "base_url": base_url,
                "api_key_required": False,
                "context_length": 8192,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_synthetic_layer(base_url: str) -> None:
    """合成プロバイダ 1 件と、それを ``provider_ref`` で指す合成モデル 1 件を置く。"""
    providers_dir = _TMP_USER_DATA / "providers"
    models_dir = _TMP_USER_DATA / "models"
    providers_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    (providers_dir / f"{PROVIDER_ID}.json").write_text(
        json.dumps(
            {
                "id": PROVIDER_ID,
                "display_name": "audit b6 local server",
                "protocol": "openai_compat",
                "base_url": base_url,
                # ローカルサーバー扱い。実鍵を要求させないための宣言で、
                # factory がプレースホルダを入れる (llm_clients/factory.py:207-213)。
                "api_key_required": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (models_dir / f"{MODEL_KEY}.json").write_text(
        json.dumps(
            {
                "model": "audit-b6-dummy",
                "display_name": "audit b6 dummy model",
                "provider_ref": PROVIDER_ID,
                "context_length": 8192,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


_write_synthetic_layer(OLD_URL)
_write_direct_model(DIRECT_OLD_URL)

from persona.core import PersonaCore  # noqa: E402
from saiverse import model_configs, provider_configs  # noqa: E402


class _LazyClientHolder:
    """``persona/core.py`` の遅延生成プロパティ**そのもの**を借りた観測用の器。

    ``PersonaCore`` を丸ごと組むには SAIMemory / HistoryManager / DB が要る。
    ここで確かめたいのは「クライアントがいつ作られ、いつ作り直されるか」だけなので、
    プロパティオブジェクトを借りて、それが読む属性だけを持たせる。
    実行されるコードは製品と同一 (``persona/core.py:241-283``)。
    """

    llm_client = PersonaCore.llm_client
    lightweight_llm_client = PersonaCore.lightweight_llm_client

    def __init__(self, persona_id: str, model_key: str = MODEL_KEY) -> None:
        self.persona_id = persona_id
        self.model = model_key
        self.lightweight_model = model_key
        self.provider = model_configs.get_model_provider(model_key)
        self.context_length = model_configs.get_context_length(model_key)
        self._llm_client = None
        self._lightweight_llm_client = None
        self._lightweight_llm_client_initialized = False
        self._pending_parameter_overrides = None

    def apply_parameter_overrides(self, overrides=None) -> None:  # pragma: no cover
        return None


def _client_base_url(client: object) -> str:
    """SDK クライアントが実際に握っている宛先を読む。"""
    if client is None:
        return "<no client>"
    inner = getattr(client, "client", None)
    return str(getattr(inner, "base_url", "<no base_url>"))


def _resolved_model_base_url(model_key: str = MODEL_KEY) -> str:
    return str(model_configs.get_model_config(model_key).get("base_url"))


def _invalidate_like_admin_route(holder: _LazyClientHolder) -> None:
    """``api/routes/admin.py:136-138`` が API キー変更時に行う無効化と同じ 3 行。"""
    holder._llm_client = None
    holder._lightweight_llm_client = None
    holder._lightweight_llm_client_initialized = False


def main() -> int:
    print(f"[setup] SAIVERSE_HOME={_TMP_HOME}")
    print(f"[setup] provider {PROVIDER_ID} base_url={OLD_URL}")
    print(f"[setup] model config resolved base_url={_resolved_model_base_url()}")

    # already_spoken: 変更前にクライアントを作ってしまったペルソナ
    already_spoken = _LazyClientHolder("already_spoken")
    print(f"[A/before] already_spoken.llm_client -> {_client_base_url(already_spoken.llm_client)}")

    # lightweight_only: 変更前に軽量用だけを作ったペルソナ (D 用)
    lightweight_only = _LazyClientHolder("lightweight_only")
    print(
        "[D/before] lightweight_only.lightweight_llm_client -> "
        f"{_client_base_url(lightweight_only.lightweight_llm_client)}"
    )

    # --- プロバイダの接続先を変更する (UI の保存と同じ入口) ---------------------
    existing = dict(provider_configs.get_provider(PROVIDER_ID) or {})
    existing["base_url"] = NEW_URL
    provider_configs.save_provider(PROVIDER_ID, existing)
    print(f"\n[change] save_provider({PROVIDER_ID}) base_url -> {NEW_URL}")

    # B: 設定の辞書とモデル側の解決
    print(f"[B] PROVIDER_CONFIGS base_url = {provider_configs.get_provider(PROVIDER_ID)['base_url']}")
    print(f"[B] MODEL_CONFIGS resolved base_url = {_resolved_model_base_url()}")

    # A: 生成済みクライアント
    print(f"[A/after ] already_spoken.llm_client -> {_client_base_url(already_spoken.llm_client)}")

    # C: 保存後に初めて作る側
    not_yet_spoken = _LazyClientHolder("not_yet_spoken")
    print(f"[C] not_yet_spoken.llm_client -> {_client_base_url(not_yet_spoken.llm_client)}")

    # D: 同一ペルソナ内の新旧混在
    print(
        "[D/after ] lightweight_only.lightweight_llm_client -> "
        f"{_client_base_url(lightweight_only.lightweight_llm_client)}"
    )
    print(
        "[D/after ] lightweight_only.llm_client (初回生成) -> "
        f"{_client_base_url(lightweight_only.llm_client)}"
    )

    # E: admin ルートと同じ無効化を当てる
    _invalidate_like_admin_route(already_spoken)
    print(f"\n[E] 無効化後 already_spoken.llm_client -> {_client_base_url(already_spoken.llm_client)}")
    print(
        "[E] 無効化後 already_spoken.lightweight_llm_client -> "
        f"{_client_base_url(already_spoken.lightweight_llm_client)}"
    )

    # F: プロバイダの削除
    survivor = _LazyClientHolder("survivor")
    survivor_url_before = _client_base_url(survivor.llm_client)
    provider_configs.delete_provider(PROVIDER_ID)
    print(f"\n[F] delete_provider({PROVIDER_ID}) 実行")
    print(f"[F] 削除前に作った survivor.llm_client -> {_client_base_url(survivor.llm_client)}")
    print(f"[F]   (削除前の観測値 = {survivor_url_before})")
    after_delete = _LazyClientHolder("after_delete")
    try:
        print(f"[F] 削除後に作る after_delete.llm_client -> {_client_base_url(after_delete.llm_client)}")
    except Exception as exc:
        print(f"[F] 削除後に作る after_delete.llm_client -> 失敗: {type(exc).__name__}: {exc}")

    # G: モデル JSON 側の編集 (プロバイダを経由しない直書き経路)
    direct = _LazyClientHolder("direct_model_user", DIRECT_MODEL_KEY)
    print(f"\n[G/before] direct.llm_client -> {_client_base_url(direct.llm_client)}")
    _write_direct_model(DIRECT_NEW_URL)
    model_configs.reload_configs()
    print(f"[G] MODEL_CONFIGS resolved base_url = {_resolved_model_base_url(DIRECT_MODEL_KEY)}")
    print(f"[G/after ] direct.llm_client -> {_client_base_url(direct.llm_client)}")
    fresh_direct = _LazyClientHolder("direct_model_fresh", DIRECT_MODEL_KEY)
    print(f"[G] 編集後に作る fresh_direct.llm_client -> {_client_base_url(fresh_direct.llm_client)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
