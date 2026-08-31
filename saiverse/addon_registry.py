"""アドオンレジストリ (registry.json) のスキーマと fetch ロジック。

設計は ``docs/intent/addon_catalog_management.md`` の「設計方針」節を参照。

registry.json はまはー管理下の public GitHub リポジトリで配信される。
SAIVerse 本体は起動時 / ユーザー要求時にこれを fetch して、UI カタログの
ソースとして使う。

デフォルト URL は ``SAIVERSE_ADDON_REGISTRY_URL`` 環境変数で上書き可能
(セルフホスト registry / 開発用ローカルファイルを使いたい場合に)。

キャッシュ戦略:
    - メモリキャッシュのみ、TTL は短め (デフォルト 5 分)
    - 失敗時は古いキャッシュを返すフォールバック (offline でも以前 fetch
      した内容を見られるように)
    - 明示的な無効化は ``invalidate_cache()`` で
"""
from __future__ import annotations

import json
import base64
import logging
import os
import time
import urllib.error
import urllib.request
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

LOGGER = logging.getLogger(__name__)

DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/maha0525/saiverse-addon-registry/main/registry.json"
)
ENV_REGISTRY_URL = "SAIVERSE_ADDON_REGISTRY_URL"
ENV_REGISTRY_PUBLIC_KEY = "SAIVERSE_ADDON_REGISTRY_PUBLIC_KEY"
ENV_ALLOW_UNSIGNED_REGISTRY = "SAIVERSE_ALLOW_UNSIGNED_ADDON_REGISTRY"
# 公式レジストリ (maha0525/saiverse-addon-registry) の署名検証用 Ed25519 公開鍵
# (raw 32byte の base64)。秘密鍵はメンテナがリポジトリ外で保管する。
# ローテーション時はこの値を差し替えてリリースする。
# セルフホスト registry を使う場合は ENV_REGISTRY_PUBLIC_KEY で上書きできる。
DEFAULT_REGISTRY_PUBLIC_KEY = "+ZKgzmLTqd3PzF0PUPYOy6vTQM9gk5FXCVaxbiFDER4="
_DEFAULT_TTL_SECONDS = 300  # 5 分
_FETCH_TIMEOUT_SECONDS = 15
SUPPORTED_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Schema (Pydantic)
# ---------------------------------------------------------------------------

class RegistryRequires(BaseModel):
    """アドオンインストール時のシステム要件。

    UI カタログでバッジ表示 + 導入前チェックに使う。
    """
    model_config = ConfigDict(extra="ignore")
    gpu: Optional[str] = Field(
        None, description="'required' / 'optional' / 'none' のいずれか"
    )
    disk_gb: Optional[float] = Field(None, description="必要ディスク容量 (GB)")
    os: List[str] = Field(
        default_factory=list,
        description="サポート OS の小文字リスト (例: ['windows', 'linux', 'macos'])",
    )


class RegistryVersionEntry(BaseModel):
    """1 つのバージョン (commit SHA pin) のエントリ。"""
    model_config = ConfigDict(extra="ignore")

    version: str = Field(..., description="セマンティックバージョン (例: '0.5.0')")
    commit: str = Field(..., description="checkout 対象 full commit SHA (40 hex)")
    setup_version: int = Field(
        1,
        description=(
            "addon.json の setup_version と一致する値。"
            "アップデート時に値が増えていれば installer が setup.steps を再実行。"
        ),
    )
    min_saiverse_version: Optional[str] = Field(
        None, description="必要な SAIVerse 本体バージョン (任意)"
    )
    released_at: Optional[str] = Field(
        None, description="リリース日時 (ISO 8601)"
    )
    changelog_url: Optional[str] = Field(
        None, description="このバージョンの changelog URL (任意)"
    )

    @field_validator("commit")
    @classmethod
    def _check_commit(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[0-9a-f]{40}", v):
            raise ValueError(
                f"commit must be a full 40-character lowercase SHA, got {v!r}"
            )
        return v


class RegistryAddonEntry(BaseModel):
    """カタログに載る 1 つのアドオン (複数バージョンを持つ)。"""
    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="アドオン ID (= expansion_data/<id>/ のディレクトリ名)")
    display_name: str
    description: str = ""
    category: Optional[str] = Field(
        None,
        description="UI フィルタ用カテゴリ (voice / vessel / social / persona など)",
    )
    repo_url: str = Field(..., description="clone する Git リポジトリ URL")
    versions: List[RegistryVersionEntry] = Field(..., min_length=1)
    latest: str = Field(..., description="latest version の version 文字列")
    icon_url: Optional[str] = None
    requires: RegistryRequires = Field(default_factory=RegistryRequires)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z][a-z0-9\-_]*$", v):
            raise ValueError(
                f"addon id must be lowercase [a-z0-9_-], starting with letter; got {v!r}"
            )
        return v

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError(f"repo_url must use HTTPS ({v!r})")
        return v

    def model_post_init(self, _ctx) -> None:  # type: ignore[override]
        version_strs = [v.version for v in self.versions]
        if self.latest not in version_strs:
            raise ValueError(
                f"latest={self.latest!r} not found in versions list {version_strs}"
            )

    def get_version(self, version: str) -> Optional[RegistryVersionEntry]:
        for v in self.versions:
            if v.version == version:
                return v
        return None

    def get_latest_version(self) -> RegistryVersionEntry:
        latest = self.get_version(self.latest)
        # validator が一致を担保しているのでここで None はない
        assert latest is not None
        return latest


class Registry(BaseModel):
    """registry.json 全体のルートモデル。"""
    model_config = ConfigDict(extra="ignore")

    schema_version: int = Field(..., description="registry.json スキーマバージョン")
    updated_at: Optional[str] = Field(None, description="最終更新日時 (ISO 8601)")
    addons: List[RegistryAddonEntry] = Field(default_factory=list)
    trust_level: str = Field(
        "unsigned",
        description="Runtime verification result: official, verified, or unsigned.",
    )
    publisher_key_id: Optional[str] = None

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: int) -> int:
        if v != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {v} "
                f"(this SAIVerse supports {SUPPORTED_SCHEMA_VERSION})"
            )
        return v

    def get_addon(self, addon_id: str) -> Optional[RegistryAddonEntry]:
        for a in self.addons:
            if a.id == addon_id:
                return a
        return None


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

class _Cache:
    def __init__(self) -> None:
        self.registry: Optional[Registry] = None
        self.fetched_at: float = 0.0


_cache = _Cache()


def get_registry_url() -> str:
    return os.environ.get(ENV_REGISTRY_URL, DEFAULT_REGISTRY_URL).strip()


def invalidate_cache() -> None:
    """次回 ``fetch_registry`` 呼び出しで強制的に再 fetch させる。"""
    _cache.registry = None
    _cache.fetched_at = 0.0
    LOGGER.debug("addon_registry: cache invalidated")


def _is_local_file_url(url: str) -> bool:
    return url.startswith("file://") or os.path.isabs(url)


def _read_local_registry(url: str) -> dict:
    """file:// or 絶対パスで指定された registry.json を読む (開発用)。"""
    if url.startswith("file://"):
        path = url[len("file://") :]
        # Windows の file:///C:/... 形式に対応
        if path.startswith("/") and len(path) > 3 and path[2] == ":":
            path = path[1:]
    else:
        path = url
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _http_fetch_registry(url: str) -> dict:
    if not url.startswith("https://"):
        raise ValueError("Remote addon registry URL must use HTTPS")
    req = urllib.request.Request(
        url, headers={"User-Agent": "SAIVerse-addon-registry-client/1.0"}
    )
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310 - URL は env で固定
        if not resp.geturl().startswith("https://"):
            raise ValueError("Addon registry redirected to a non-HTTPS URL")
        body = resp.read().decode("utf-8")
    return json.loads(body)


def _canonical_registry_payload(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _verify_registry_document(data: dict, source_url: str) -> tuple[dict, str, str | None]:
    """Return payload and trust provenance after Ed25519 verification."""
    signed_payload = data.get("signed")
    signature = data.get("signature")
    if isinstance(signed_payload, dict) and isinstance(signature, dict):
        if signature.get("algorithm") != "ed25519":
            raise ValueError("Addon registry signature algorithm must be ed25519")
        public_key_raw = (
            os.getenv(ENV_REGISTRY_PUBLIC_KEY, "").strip() or DEFAULT_REGISTRY_PUBLIC_KEY
        )
        if not public_key_raw:
            raise ValueError(
                f"Signed addon registry requires pinned {ENV_REGISTRY_PUBLIC_KEY}"
            )
        try:
            public_key_bytes = base64.b64decode(public_key_raw, validate=True)
            signature_bytes = base64.b64decode(signature["value"], validate=True)
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            public_key.verify(signature_bytes, _canonical_registry_payload(signed_payload))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid addon registry signature encoding: {exc}") from exc
        except Exception as exc:
            raise ValueError("Addon registry signature verification failed") from exc
        key_id = signature.get("key_id")
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("Addon registry signature is missing key_id")
        trust = "official" if source_url == DEFAULT_REGISTRY_URL else "verified"
        return signed_payload, trust, key_id

    if source_url == DEFAULT_REGISTRY_URL:
        raise ValueError("Official addon registry must be signed")
    if not _is_local_file_url(source_url):
        allow_unsigned = os.getenv(ENV_ALLOW_UNSIGNED_REGISTRY, "").strip().lower()
        if allow_unsigned not in {"1", "true", "yes", "on"}:
            raise ValueError(
                "Unsigned third-party addon registry requires explicit "
                f"{ENV_ALLOW_UNSIGNED_REGISTRY}=true"
            )
    return data, "unsigned", None


def fetch_registry(
    url: Optional[str] = None,
    force: bool = False,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> Registry:
    """registry.json を fetch し、Pydantic 検証済みの Registry を返す。

    - url 省略時は ``SAIVERSE_ADDON_REGISTRY_URL`` 環境変数 → デフォルト URL
    - force=False で TTL 内なら memory cache を返す
    - HTTP 失敗時は古い cache があればそれを返す (offline フォールバック)
    """
    effective_url = url or get_registry_url()
    now = time.time()

    if (
        not force
        and _cache.registry is not None
        and (now - _cache.fetched_at) < ttl_seconds
    ):
        LOGGER.debug("addon_registry: returning cached (age=%.1fs)", now - _cache.fetched_at)
        return _cache.registry

    LOGGER.info("addon_registry: fetching from %s", effective_url)
    try:
        if _is_local_file_url(effective_url):
            data = _read_local_registry(effective_url)
        else:
            data = _http_fetch_registry(effective_url)
        payload, trust_level, publisher_key_id = _verify_registry_document(
            data,
            effective_url,
        )
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
        if _cache.registry is not None:
            LOGGER.warning(
                "addon_registry: fetch failed (%s), returning stale cache", e
            )
            return _cache.registry
        raise RuntimeError(f"failed to fetch registry from {effective_url}: {e}") from e

    try:
        registry = Registry.model_validate(payload).model_copy(
            update={
                "trust_level": trust_level,
                "publisher_key_id": publisher_key_id,
            }
        )
    except ValidationError as e:
        if _cache.registry is not None:
            LOGGER.warning(
                "addon_registry: validation failed, returning stale cache:\n%s", e
            )
            return _cache.registry
        raise RuntimeError(f"registry.json validation failed:\n{e}") from e

    _cache.registry = registry
    _cache.fetched_at = now
    LOGGER.info(
        "addon_registry: fetched and cached, %d addon(s)", len(registry.addons)
    )
    return registry


__all__ = [
    "Registry",
    "RegistryAddonEntry",
    "RegistryVersionEntry",
    "RegistryRequires",
    "fetch_registry",
    "invalidate_cache",
    "get_registry_url",
    "DEFAULT_REGISTRY_URL",
    "ENV_REGISTRY_URL",
    "ENV_REGISTRY_PUBLIC_KEY",
    "ENV_ALLOW_UNSIGNED_REGISTRY",
    "SUPPORTED_SCHEMA_VERSION",
]
