"""OS の証明書ストアが読めない環境で、同梱の証明書束へ退避する。

macOS の Python (python.org 版・pyenv ビルド等) は、OpenSSL からキーチェーンを
読めない。その環境では ``ssl.create_default_context()`` が証明書を **1 枚も持たず**、
``urllib`` 経由の HTTPS がことごとく ``CERTIFICATE_VERIFY_FAILED
(unable to get local issuer certificate)`` で落ちる。Windows は
``ssl.enum_certificates()`` で OS のストアを読めるため、この症状は出ない
— だから「他のユーザーは見えているのに、その人だけ全滅」という形になる
(2026-09-02、実ユーザーの macOS 環境で「読めた証明書 0 個」を実測)。

``requests`` / ``httpx`` は自前で certifi を見るので影響を受けない。壊れるのは
標準ライブラリの ``urllib`` を直接使っている経路だけで、バックエンドでは 3 つ:

- ``saiverse/addon_registry.py``   アドオンカタログの取得
- ``saiverse/addon_installer.py``  アドオン本体のダウンロード
- ``api/routes/system.py``         最新リリースの確認 / お知らせの取得

いずれも同じプロセスで動くので、起動時にここを一度呼べば全部そろって直る。
呼び出し箇所を数え上げて 1 つずつ context を渡す形にしない — 数え落とした
経路が黙って壊れたままになるため。

**OS 側が読めているときは何もしない。** 会社や学校のネットワーク、あるいは
ウイルス対策ソフトが通信を検査している環境では、その中間 CA は OS のストアに
だけ入っている。無条件に certifi へ差し替えると、**いま繋がっている環境を
壊す**方向の変更になる。
"""
from __future__ import annotations

import logging
import os
import ssl
import urllib.request
from typing import Optional

LOGGER = logging.getLogger(__name__)


def _os_trust_store_is_usable() -> bool:
    """OS のストアから信頼できる証明書を読めるか。

    ``get_ca_certs()`` が空なら、検証に使う根拠が 1 つも無いということで、
    その状態の ``urlopen`` はどこへ繋いでも必ず失敗する。
    """
    try:
        return bool(ssl.create_default_context().get_ca_certs())
    except Exception:  # pragma: no cover - ストアの読み取り自体が倒れた場合
        LOGGER.debug("OS の証明書ストアを確認できませんでした", exc_info=True)
        return False


def ensure_default_https_trust() -> Optional[str]:
    """OS のストアが空なら、同梱の certifi を urllib の既定の信頼元にする。

    Returns:
        差し替えた場合は使った証明書束のパス。何もしなかった場合は None。
    """
    if _os_trust_store_is_usable():
        return None

    try:
        import certifi
    except ImportError:
        # requests の依存として必ず入る想定。無い場合は打つ手が無いので、
        # 黙って進まずに理由を残す (この後の HTTPS はすべて失敗する)。
        LOGGER.error(
            "OS の証明書ストアが空で、同梱の証明書束 (certifi) も見つかりません。"
            "アドオンカタログなど HTTPS を使う機能は失敗します"
        )
        return None

    cafile = certifi.where()

    # OpenSSL が既定の検証パスを組み立てるときに読む環境変数。この後に作られる
    # ssl の既定コンテキスト全体に効く。すでに指定があれば利用者の指定を優先する。
    os.environ.setdefault("SSL_CERT_FILE", cafile)

    # urllib が使う既定の opener を、この証明書束で検証するものに差し替える。
    # ``ssl._create_default_https_context`` を差し替えても効かない —
    # ``http.client`` が import 時にその値を自分のモジュール変数へ写しており、
    # 後から ssl 側を変えても参照されないため (2026-09-02 テストで実測)。
    # ``urlopen`` も ``urlretrieve`` もこの opener を通るので、アドオン本体の
    # ダウンロードまで一緒に直る。
    context = ssl.create_default_context(cafile=cafile)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    urllib.request.install_opener(opener)

    LOGGER.info(
        "OS の証明書ストアが空でした。同梱の証明書束を使います: %s", cafile
    )
    return cafile
