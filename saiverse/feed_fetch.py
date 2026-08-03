"""RSS/Atom フィードの取得・解析層 (純粋関数中心)。

docs/intent/rss_feed_intake.md の取得層。方言差 (RSS 2.0 / Atom 等) は
feedparser で吸収し、エントリを guid / title / summary / link / published の
正規形に揃えて返す。**転載のみで、要約・言い換えは一切しない** (intent 不変条件 1)。

- fetch_feed: 条件付き GET (ETag / Last-Modified) 対応のフィード取得 + 解析
- discover_feed: サイト URL からのフィード自動発見 (HTML autodiscovery + 定番パス)。
  ユーザーが与えた site_url 起点のみ — 記事の中身等が購読先を注入する経路は
  作らない (intent 不変条件 5)

DB やマネージャには依存しない。永続化・配送は saiverse/feed_manager.py が担う。
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import feedparser
import requests
# HTML autodiscovery (discover_feed) 用。モジュール先頭で import する理由:
# 関数内 import だと依存欠落 (clean install で beautifulsoup4 が無い) が
# discovery の広い except に吸われて黙って死ぬ — import 時に騒がしく失敗させる。
from bs4 import BeautifulSoup

LOGGER = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SEC = 15


def _fetch_timeout() -> int:
    """SAIVERSE_FEED_FETCH_TIMEOUT の安全 parse (既定 15 秒)。

    module-level の ``int(os.getenv(...))`` だと不正値で import 自体が落ちる
    ため関数化する (_max_feed_bytes と同じ流儀)。非数値・空・0・負数は
    WARNING を出して既定値へ fallback — timeout 0 以下は requests の意味論
    でも取得の意図でも成立しない。
    """
    env_val = os.getenv("SAIVERSE_FEED_FETCH_TIMEOUT")
    if env_val is not None:
        try:
            value = int(env_val)
        except ValueError:
            value = None
        if value is not None and value > 0:
            return value
        LOGGER.warning(
            "Invalid SAIVERSE_FEED_FETCH_TIMEOUT=%r; using default (%d)",
            env_val, _DEFAULT_TIMEOUT_SEC,
        )
    return _DEFAULT_TIMEOUT_SEC

# 応答サイズの上限。フィード本体は env で調整可、discovery の HTML は固定。
_DEFAULT_MAX_FEED_BYTES = 10 * 1024 * 1024
_DISCOVERY_MAX_BYTES = 2 * 1024 * 1024
# フェッチ 1 回 (fetch_feed / discover_feed の呼び出し 1 回) の壁時計予算。
# 接続+読み込み+リダイレクト追跡の合計を単調時間 (time.monotonic) で測る。
_TOTAL_BUDGET_SEC = 60.0
_MAX_REDIRECTS = 5
_READ_CHUNK = 65536

# 1 フィードから受け付ける entry 数と各フィールドの長さ上限 (超過は切り詰め)
_MAX_ENTRIES = 200
_TITLE_MAX_CHARS = 500
_SUMMARY_MAX_CHARS = 5000
_LINK_MAX_CHARS = 512  # DB 列 FeedItem.LINK の宣言長 String(512) と一致させる
# guid の上限 (DB 列 feed_item.GUID の宣言長 String(512) と揃える)。guid は
# 重複判定キーなので切り詰めではなくハッシュ置換 (_entry_guid 参照) — 末尾を
# 落とすと別記事同士が同一 guid に潰れる。
_GUID_MAX_CHARS = 512

# discover_feed が返す候補タイトルの上限。HTML の <link title=...> は外部入力で
# 無制限に長くできる — 購読タイトルの入口上限 (feed_manager 側 200 文字) と揃える。
_DISCOVERED_TITLE_MAX_CHARS = 200

# ブラウザ偽装 UA。tools/web_fetch_helpers.py の USER_AGENT と同じ値。
# import で共有しない理由: tools パッケージは import 時に全ツールの自動発見を
# 走らせるため、取得層のような低レベルモジュールから import できない (層の逆転)。
# 値の同期は tests/test_feed_intake.py が機械的に検査する。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# フィード autodiscovery で拾う <link rel="alternate"> の MIME タイプ
_FEED_LINK_TYPES = ("application/rss+xml", "application/atom+xml")

# autodiscovery が空振りしたときに試す定番パス (site の同一オリジンのみ)
_COMMON_FEED_PATHS = ("/feed", "/rss", "/atom.xml", "/rss.xml", "/index.xml", "/feed.xml")

# 先頭バイト判定: これらのマーカーが冒頭に見えたらフィードとみなす
_FEED_BODY_MARKERS = (b"<rss", b"<feed", b"<rdf:RDF")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


class FeedFetchError(Exception):
    """フィード取得・解析中のエラー。

    tools/web_fetch_helpers.py の FetchError と同じ思想: message はそのまま
    UI / ログ / LLM に提示してよい日本語。``kind`` で種類を機械判別できる
    ("timeout" / "network" / "http_error" / "not_a_feed" / "invalid_url" /
    "forbidden_url" / "too_large")。
    """

    def __init__(self, message: str, *, kind: str = "network") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class FeedEntry:
    """正規化済みのフィード記事 1 件。

    ``guid``: フィード内で記事を一意に指す ID。元データに guid/id が無ければ
        link、それも無ければ title+published のハッシュで代替する。
    ``summary``: HTML タグを落とした素テキスト (機械的な整形のみ。要約しない)。
    ``published``: tz-aware UTC。フィードに日時が無ければ None。
    """
    guid: str
    title: str
    summary: str
    link: str
    published: Optional[datetime]


@dataclass
class FeedFetchResult:
    """fetch_feed の結果。``not_modified=True`` のとき entries は空。"""
    url: str
    not_modified: bool = False
    title: str = ""
    site_url: str = ""
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    entries: List[FeedEntry] = field(default_factory=list)


@dataclass(frozen=True)
class DiscoveredFeed:
    """discover_feed が見つけたフィード候補 1 件。

    ``source``: "autodiscovery" (HTML の自己宣言リンク) | "common_path" (定番パス)。
    """
    url: str
    title: str = ""
    source: str = "autodiscovery"


# 明示 scheme (「xxx://」形式) の検出。urlparse の scheme で判定しないのは、
# 無 scheme の「example.com:8080/feed」が scheme="example.com" と誤検出される
# ため (scheme 文字集合にドットが含まれる)。
_EXPLICIT_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _normalize_url(url: str) -> str:
    """取得用 URL の最小整形と入口検査。

    - 明示 scheme (「xxx://」形式) が http/https なら scheme だけ小文字化して
      他成分は保持 (大文字 scheme を startswith で判定すると「scheme なし」と
      誤認して壊れ URL になるため)。http/https 以外 (ftp:, file: 等) は
      forbidden_url で即拒否 — https:// を前置すると
      ``https://ftp://example.com/feed`` という壊れ URL に化ける。
    - 無 scheme (example.com/feed) は https:// を補う。
    - urlparse が解釈できない URL (不正な IPv6 ブラケット・不正な port 等) は
      invalid_url — 生 ValueError を漏らすと API 層が 500 になる。

    アドレス検査 (SSRF ガード) は _ensure_url_allowed、購読の同一性正規化は
    feed_manager 側の仕事。
    """
    if not url or not url.strip():
        raise FeedFetchError("URLが指定されていません。", kind="invalid_url")
    url = url.strip()
    match = _EXPLICIT_SCHEME_RE.match(url)
    if match:
        scheme = match.group(0)[:-3].lower()
        if scheme not in ("http", "https"):
            raise FeedFetchError(
                f"このURLのスキームは許可されていません: {scheme}",
                kind="forbidden_url",
            )
        # scheme だけ小文字化して他成分は保持 (元 URL の scheme 部を置換)
        url = scheme + url[len(scheme):]
    else:
        url = "https://" + url
    try:
        parsed = urlparse(url)  # 不正な IPv6 ブラケットはここで ValueError
        _ = parsed.port  # port が数値でない / 範囲外ならここで ValueError
    except ValueError as exc:
        raise FeedFetchError(
            "URLを解釈できませんでした (形式が不正です)。", kind="invalid_url",
        ) from exc
    return url


def _max_feed_bytes() -> int:
    env_val = os.getenv("SAIVERSE_FEED_MAX_BYTES")
    if env_val:
        try:
            return max(1024, int(env_val))
        except ValueError:
            LOGGER.warning("Invalid SAIVERSE_FEED_MAX_BYTES=%r; using default", env_val)
    return _DEFAULT_MAX_FEED_BYTES


def _allow_private_urls() -> bool:
    """SAIVERSE_FEED_ALLOW_PRIVATE=1 で SSRF ガードを無効化できる。

    ローカルネットワーク内のフィード (自宅サーバー・イントラネット等) を
    購読したい上級者向けの脱出口。既定で閉じるのは、フィード URL や
    リダイレクト先が loopback / private アドレス (localhost の管理 API 等) を
    突く SSRF の入口になるため。
    """
    return os.getenv("SAIVERSE_FEED_ALLOW_PRIVATE", "") == "1"


def _resolve_host_addresses(hostname: str) -> List[object]:
    """hostname の DNS 解決結果 (全アドレス) を ipaddress オブジェクトで返す。"""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise FeedFetchError(
            f"ホスト名を解決できませんでした: {hostname}", kind="network",
        ) from exc
    addresses: List[object] = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return addresses


def _ensure_url_allowed(url: str) -> None:
    """外部フィード取得の SSRF ガード。全 outbound GET がここを通る。

    弾くもの: http/https 以外の scheme、userinfo 付き URL、そして hostname の
    解決結果 (全アドレス) に 1 つでもグローバルでないアドレスが混ざるもの。
    アドレス判定の軸は「全アドレスが is_global であること」— 個別フラグ
    (is_private 等) の否定の列挙だと、どのフラグにも該当しない非グローバル帯
    (例: CGN 共有帯 100.64.0.0/10 は is_private=False かつ is_global=False)
    がすり抜ける。個別フラグの否定は読み手への明示として補助的に残す。
    違反は FeedFetchError(kind="forbidden_url")。
    SAIVERSE_FEED_ALLOW_PRIVATE=1 でアドレス検査だけ無効化できる
    (_allow_private_urls の docstring 参照)。

    既知の残余リスク: 検査時と接続時の DNS 再解決差 (rebinding TOCTOU) は
    塞いでいない。対策には接続先 IP の pin (TLS/SNI 絡みのカスタムアダプタ)
    が要り、ローカル個人アプリの脅威モデルでは過剰と裁定 (2026-08-02)。
    攻撃成立には悪意フィード + 攻撃者制御 DNS が必要で、成立しても内部への
    GET 応答はフィード形式でない限り表面化しない。
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise FeedFetchError(
            f"このURLのスキームは許可されていません: {parsed.scheme}",
            kind="forbidden_url",
        )
    if parsed.username is not None or parsed.password is not None:
        raise FeedFetchError(
            "認証情報 (user:pass@) 付きのURLは許可されていません。",
            kind="forbidden_url",
        )
    hostname = parsed.hostname
    if not hostname:
        raise FeedFetchError("URLにホスト名がありません。", kind="invalid_url")
    if _allow_private_urls():
        return
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        addresses = _resolve_host_addresses(hostname)
    if not addresses:
        raise FeedFetchError(
            f"ホスト名を解決できませんでした: {hostname}", kind="network",
        )
    for addr in addresses:
        if (
            not addr.is_global
            or addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise FeedFetchError(
                "このURLへのアクセスは許可されていません "
                "(内部ネットワーク宛のため)。",
                kind="forbidden_url",
            )


def _remaining_budget(deadline: float, timeout_s: float) -> float:
    """壁時計予算の残りと単発 timeout の小さい方。予算切れは timeout エラー。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        # 予算の秒数は書かない — 既定 (_TOTAL_BUDGET_SEC) とは限らず、
        # 呼び出し側が deadline で予算を共有している場合がある
        raise FeedFetchError(
            "タイムアウト: 取得全体の時間予算を使い切りました。",
            kind="timeout",
        )
    return min(timeout_s, remaining)


def _checked_get(
    url: str,
    *,
    headers: dict,
    timeout_s: float,
    deadline: float,
) -> Tuple[requests.Response, str]:
    """SSRF ガードを通した GET。リダイレクトは手動追跡 (最大 5 hop) し、
    各 hop で ``_ensure_url_allowed`` を再検査する (公開 URL → 内部宛への
    リダイレクトで関門を迂回させない)。

    各 hop の残予算検査は SSRF ガード (DNS 解決 = getaddrinfo を含む) より
    **前**に行う — 期限切れなら DNS すら呼ばずに timeout を上げる。残余:
    期限内に始まった単発の遅い getaddrinfo は予算を超過しうるが、OS リゾルバ
    のタイムアウトで有界 — deadline-aware DNS のスレッドラッパは過剰と裁定し
    受容 (2026-08-03)。

    リダイレクトで得た URL (Location) は外部入力で、urlparse 不能な形
    (不正な IPv6 ブラケット等) がありうる — hop 0 の URL は呼び出し側が
    _normalize_url で検査済みだが、hop 1 以降の urljoin / SSRF ガード内の
    urlparse が上げる生 ValueError はここで invalid_url に写像する
    (漏らすと API 層が 500 になる)。

    Returns: (response, 最終 URL)。response は stream=True (本文未読)。
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        # 期限切れなら DNS (下の _ensure_url_allowed) の前に timeout を上げる
        _remaining_budget(deadline, timeout_s)
        try:
            _ensure_url_allowed(current)
        except ValueError as exc:
            raise FeedFetchError(
                "リダイレクト先のURLが不正です。", kind="invalid_url",
            ) from exc
        response = requests.get(
            current,
            headers=headers,
            timeout=_remaining_budget(deadline, timeout_s),
            allow_redirects=False,
            stream=True,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = (response.headers.get("Location") or "").strip()
            response.close()
            if not location:
                raise FeedFetchError(
                    "リダイレクト先が指定されていません。", kind="http_error",
                )
            try:
                current = urljoin(current, location)
            except ValueError as exc:
                raise FeedFetchError(
                    "リダイレクト先のURLが不正です。", kind="invalid_url",
                ) from exc
            continue
        return response, current
    raise FeedFetchError(
        f"リダイレクトが多すぎます (最大{_MAX_REDIRECTS}回)。", kind="network",
    )


def _read_limited(response, max_bytes: int, deadline: float) -> bytes:
    """本文を上限バイト未満まで読む。上限到達は too_large、予算切れは timeout。

    どちらの打ち切りも **EOF を待たずその場で response.close() して即座に**
    行う — EOF を返さないストリーム相手に次の chunk を待ち続けない。累計が
    上限に「達した」時点 (>=) で too_large にするのは、ちょうど上限で止まった
    ストリームの続きが有るか無いかを、追加 read なしには判別できないため。
    read 間の待ち時間には _checked_get が requests.get に渡した
    min(単発 timeout, 残り予算) の socket timeout が効く。

    deadline と socket timeout の関係: chunk 待ちのブロックは requests へ
    渡した read timeout (残り予算との min) で有界であり、deadline 超過の
    最悪値は read timeout 一回分。絶対 deadline をソケット層へ貫通させる
    改修は、有界性が既に成立しているため見送り (2026-08-02 裁定)。
    """
    chunks: List[bytes] = []
    total = 0
    for chunk in response.iter_content(_READ_CHUNK):
        if not chunk:
            continue
        total += len(chunk)
        if total >= max_bytes:
            response.close()
            raise FeedFetchError(
                f"応答が大きすぎます (上限 {max_bytes} バイト)。", kind="too_large",
            )
        chunks.append(chunk)
        if time.monotonic() > deadline:
            response.close()
            raise FeedFetchError(
                "タイムアウト: 取得全体の時間予算を使い切りました。",
                kind="timeout",
            )
    return b"".join(chunks)


def strip_html(text: str) -> str:
    """HTML タグを機械的に除去し、空白を整えた素テキストを返す。

    転載のための整形であり、内容の要約・言い換えはしない。実体参照
    (&amp; 等) は復元する。
    """
    if not text:
        return ""
    import html as _html
    no_tags = _TAG_RE.sub(" ", text)
    unescaped = _html.unescape(no_tags)
    # 連続空白を 1 つに、行単位の体裁だけ整える
    lines = [_WS_RE.sub(" ", ln).strip() for ln in unescaped.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _entry_published(entry) -> Optional[datetime]:
    """feedparser エントリから公開日時 (tz-aware UTC) を取り出す。"""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                import calendar
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            except (ValueError, OverflowError, TypeError):
                continue
    return None


def _entry_guid(entry, published: Optional[datetime]) -> str:
    """guid を決める: id → link → title+published のハッシュ、の順で代替。

    guid は外部入力で無制限に長くできるため、_GUID_MAX_CHARS (= DB 列の宣言長)
    を超えるものは sha256 ハッシュに置換する。形式は guid 欠落時の代替と同じ
    ``hash:{hex}``。置換は決定論 (同じ guid は常に同じハッシュ) なので、
    再取得でも同一 guid になり (SUBSCRIPTION_ID, GUID) の重複判定は機能する。
    """
    guid = (entry.get("id") or "").strip() or (entry.get("link") or "").strip()
    if guid:
        if len(guid) > _GUID_MAX_CHARS:
            digest = hashlib.sha256(guid.encode("utf-8")).hexdigest()
            return f"hash:{digest}"
        return guid
    title = (entry.get("title") or "").strip()
    stamp = published.isoformat() if published else ""
    digest = hashlib.sha256(f"{title}\n{stamp}".encode("utf-8")).hexdigest()
    return f"hash:{digest}"


def _safe_link(raw: str) -> str:
    """記事リンクとして安全な http(s) URL のみ通す。

    フィードは外部入力であり、link は最終的に UI の href とペルソナへの
    提示テキストに流れる。javascript: / data: 等のスキームをここ (供給側の
    正規化) で落とす — 下流の表示層それぞれに検証を散らさない。

    urlparse が解釈できない悪性リンク (不正な IPv6 ブラケット等で ValueError)
    も空文字に無害化する — 記事 1 件の壊れた link でフィード全体の取得を
    失敗させない (記事本体は保存される)。
    """
    link = (raw or "").strip()
    if not link:
        return ""
    try:
        scheme = urlparse(link).scheme.lower()
    except ValueError:
        return ""
    return link if scheme in ("http", "https") else ""


def _normalize_entries(parsed) -> List[FeedEntry]:
    entries: List[FeedEntry] = []
    # 外部入力の暴走対策: entry 数は _MAX_ENTRIES 件まで、各フィールドは
    # 長さ上限で切り詰める (title/summary は表示・knowledge 用の転載として
    # 十分な長さを残す)。
    for entry in parsed.entries[:_MAX_ENTRIES]:
        published = _entry_published(entry)
        summary = entry.get("summary") or ""
        if not summary:
            # Atom で content だけ持つフィードへの保険 (値はそのまま使う)
            contents = entry.get("content") or []
            if contents:
                summary = contents[0].get("value") or ""
        entries.append(
            FeedEntry(
                guid=_entry_guid(entry, published),
                title=strip_html(entry.get("title") or "")[:_TITLE_MAX_CHARS],
                summary=strip_html(summary)[:_SUMMARY_MAX_CHARS],
                link=_safe_link(entry.get("link"))[:_LINK_MAX_CHARS],
                published=published,
            )
        )
    return entries


def fetch_feed(
    url: str,
    *,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
    timeout: Optional[int] = None,
    deadline: Optional[float] = None,
) -> FeedFetchResult:
    """フィードを 1 本取得して解析する。

    ETag / Last-Modified を渡すと条件付き GET になり、変化が無ければ
    ``not_modified=True`` の結果を返す (entries は空)。失敗は FeedFetchError。
    取得できなかったものを取得できたことにはしない (intent 不変条件 2) —
    エラーは握らず必ず例外で表明する。

    ``deadline`` (time.monotonic の時刻) を渡すと、内部の壁時計予算をそれに
    従わせる — 複数回の取得を直列に呼ぶ側 (購読追加 API の
    fetch → discover → fetch) が、リクエスト全体で 1 つの予算を共有するため。
    未指定なら従来どおり呼び出しごとに _TOTAL_BUDGET_SEC (60 秒)。
    """
    url = _normalize_url(url)
    timeout_s = timeout if timeout is not None else _fetch_timeout()
    if deadline is None:
        deadline = time.monotonic() + _TOTAL_BUDGET_SEC

    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        response, _final_url = _checked_get(
            url, headers=headers, timeout_s=timeout_s, deadline=deadline,
        )
    except requests.exceptions.Timeout as exc:
        raise FeedFetchError(
            f"タイムアウト: フィードの取得に{timeout_s}秒以上かかりました。",
            kind="timeout",
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise FeedFetchError(
            f"フィードの取得に失敗しました: {exc}", kind="network",
        ) from exc

    try:
        if response.status_code == 304:
            LOGGER.debug("fetch_feed not_modified url=%s", url)
            return FeedFetchResult(
                url=url, not_modified=True, etag=etag, last_modified=last_modified,
            )

        if response.status_code >= 400:
            raise FeedFetchError(
                f"フィードの取得に失敗しました (HTTP {response.status_code})。",
                kind="http_error",
            )

        try:
            content = _read_limited(response, _max_feed_bytes(), deadline)
        except requests.exceptions.Timeout as exc:
            raise FeedFetchError(
                f"タイムアウト: フィードの取得に{timeout_s}秒以上かかりました。",
                kind="timeout",
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise FeedFetchError(
                f"フィードの取得に失敗しました: {exc}", kind="network",
            ) from exc
        response_headers = response.headers
    finally:
        try:
            response.close()
        except Exception:
            pass

    try:
        parsed = feedparser.parse(content)
        feed_info = getattr(parsed, "feed", {}) or {}
        entries = _normalize_entries(parsed)
    except Exception as exc:
        # 解析・正規化の予期しない例外は FeedFetchError に写像する — 悪性
        # フィード 1 本の解析クラッシュを、API 層の 500 (生例外) にしない
        # (呼び出し側は kind で機械判別できる。取得サイクル側の隔離は
        # feed_manager._fetch_all が階層防御として持つ)。
        raise FeedFetchError(
            "フィードの解析に失敗しました。", kind="not_a_feed",
        ) from exc

    # bozo (解析上の問題) でもエントリが取れていれば方言差として許容する
    # (軽微な仕様違反は feedparser が回収してエントリを返す)。bozo かつ
    # エントリ空は破損した応答 (途中で切れた XML 等) — 成功にすると
    # ETag / Last-Modified / LAST_OK_AT が破損応答の値で更新され、以後の
    # 条件付き GET が 304 を返し続けて記事を取りこぼしうる。失敗として
    # 表明する (取得できなかったものを取得できたことにしない — 不変条件 2)。
    # 正当な空フィード (bozo なし・エントリ空) は従来どおり成功。
    if bool(getattr(parsed, "bozo", False)) and not entries:
        raise FeedFetchError(
            "フィードの解析に失敗しました (破損した応答)。", kind="not_a_feed",
        )

    # エントリもフィードタイトルも無いものはフィードではないと判断する。
    if not entries and not feed_info.get("title"):
        raise FeedFetchError(
            "フィードとして解釈できませんでした (RSS/Atom ではない可能性があります)。",
            kind="not_a_feed",
        )

    result = FeedFetchResult(
        url=url,
        title=strip_html(feed_info.get("title") or ""),
        site_url=(feed_info.get("link") or "").strip(),
        etag=response_headers.get("ETag"),
        last_modified=response_headers.get("Last-Modified"),
        entries=entries,
    )
    LOGGER.info(
        "fetch_feed done url=%s title=%r entries=%d", url, result.title, len(entries),
    )
    return result


def _looks_like_feed(content_type: str, head: bytes) -> bool:
    ct = (content_type or "").lower()
    if any(t in ct for t in ("rss", "atom", "xml")):
        # XML 系 content-type でも HTML の可能性は低いが、先頭バイトも見る
        stripped = head.lstrip()
        if stripped.startswith(b"<?xml") or any(
            m in head for m in _FEED_BODY_MARKERS
        ):
            return True
    return any(m in head for m in _FEED_BODY_MARKERS)


def _probe_feed_url(url: str, timeout_s: int, deadline: float) -> bool:
    """URL がフィードらしいか、先頭バイトだけ読んで確認する。

    候補固有の失敗 (network / http_error / not_a_feed / forbidden_url) は
    「フィードではない」= False に落とす。ただし予算切れ (kind="timeout") は
    この候補の性質でなく探索全体の中断なので再送出する — False に畳むと
    discover_feed が「候補なし」を返し、API 層で 422 (サイトにフィードなし)
    に化けてしまう。requests 層の Timeout (iter_content 中の ReadTimeout 等)
    も同じ理由で timeout として再送出する — 汎用 RequestException に吸わせて
    False に畳まない。SSRF ガード・リダイレクト再検査は _checked_get 側。
    """
    try:
        response, _final_url = _checked_get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout_s=timeout_s,
            deadline=deadline,
        )
        try:
            if response.status_code != 200:
                return False
            head = next(response.iter_content(2048), b"") or b""
            return _looks_like_feed(response.headers.get("Content-Type", ""), head)
        finally:
            response.close()
    except FeedFetchError as exc:
        if exc.kind == "timeout":
            raise
        return False
    except requests.exceptions.Timeout as exc:
        raise FeedFetchError(
            "タイムアウト: フィード候補の確認に時間がかかりすぎました。",
            kind="timeout",
        ) from exc
    except requests.exceptions.RequestException:
        return False


def discover_feed(
    site_url: str,
    *,
    timeout: Optional[int] = None,
    deadline: Optional[float] = None,
) -> List[DiscoveredFeed]:
    """サイト URL からフィード URL を自動発見する。

    ユーザーの登録 UX は「サイトの URL を貼るだけ」(intent 不変条件 5)。
    探索は次の 2 段で、いずれも **ユーザーが指定した site_url 起点のみ**:

    1. サイトの HTML が自己宣言する ``<link rel="alternate" type=rss/atom>``
    2. 見つからなければ定番パス (/feed, /rss, /atom.xml, ...) を同一オリジンで試す

    見つからなければ空リスト。サイト自体の取得失敗は FeedFetchError。
    ``deadline`` の意味論は fetch_feed と同じ (呼び出し側が予算を共有する口)。
    """
    site_url = _normalize_url(site_url)
    timeout_s = timeout if timeout is not None else _fetch_timeout()
    if deadline is None:
        deadline = time.monotonic() + _TOTAL_BUDGET_SEC

    try:
        response, final_url = _checked_get(
            site_url,
            headers={"User-Agent": USER_AGENT},
            timeout_s=timeout_s,
            deadline=deadline,
        )
    except requests.exceptions.Timeout as exc:
        raise FeedFetchError(
            f"タイムアウト: サイトの取得に{timeout_s}秒以上かかりました。",
            kind="timeout",
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise FeedFetchError(
            f"サイトの取得に失敗しました: {exc}", kind="network",
        ) from exc

    try:
        if response.status_code >= 400:
            raise FeedFetchError(
                f"サイトの取得に失敗しました (HTTP {response.status_code})。",
                kind="http_error",
            )
        try:
            content = _read_limited(response, _DISCOVERY_MAX_BYTES, deadline)
        except requests.exceptions.RequestException as exc:
            raise FeedFetchError(
                f"サイトの取得に失敗しました: {exc}", kind="network",
            ) from exc
        content_type = response.headers.get("Content-Type", "")
        base_url = getattr(response, "url", "") or final_url
    finally:
        try:
            response.close()
        except Exception:
            pass

    # 与えられた URL 自体がフィードだった場合はそれをそのまま返す
    head = content[:2048]
    if _looks_like_feed(content_type, head):
        return [DiscoveredFeed(url=site_url, source="autodiscovery")]

    discovered: List[DiscoveredFeed] = []
    seen: set = set()

    # --- 1. HTML autodiscovery (<link rel="alternate">) ---
    try:
        soup = BeautifulSoup(content, "html.parser")
        for link in soup.find_all("link"):
            rels = [r.lower() for r in (link.get("rel") or [])]
            link_type = (link.get("type") or "").lower()
            href = (link.get("href") or "").strip()
            if "alternate" not in rels or not href:
                continue
            if not any(t in link_type for t in _FEED_LINK_TYPES):
                continue
            # サイト自身が宣言するリンクなので、別ホスト (例: feedburner) も許容
            feed_url = urljoin(base_url or site_url, href)
            if feed_url in seen:
                continue
            seen.add(feed_url)
            discovered.append(
                DiscoveredFeed(
                    url=feed_url,
                    title=(link.get("title") or "").strip()[
                        :_DISCOVERED_TITLE_MAX_CHARS
                    ],
                    source="autodiscovery",
                )
            )
    except Exception:
        LOGGER.warning("discover_feed HTML parse failed url=%s", site_url, exc_info=True)

    if discovered:
        LOGGER.info(
            "discover_feed autodiscovery url=%s found=%d", site_url, len(discovered),
        )
        return discovered

    # --- 2. 定番パス fallback (同一オリジンのみ) ---
    parts = urlparse(site_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    for path in _COMMON_FEED_PATHS:
        candidate = origin + path
        if candidate in seen:
            continue
        seen.add(candidate)
        if _probe_feed_url(candidate, timeout_s, deadline):
            discovered.append(DiscoveredFeed(url=candidate, source="common_path"))

    LOGGER.info(
        "discover_feed common_path url=%s found=%d", site_url, len(discovered),
    )
    return discovered
