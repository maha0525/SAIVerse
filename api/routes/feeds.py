"""Feeds API — RSS フィード施設の管理・閲覧エンドポイント。

docs/intent/rss_feed_intake.md の配置層 (購読編集 UI) と透明性 UI (§10-4:
ユーザーがペルソナと同じフィードを読める窓) の HTTP 層。

- 購読の起点はプリセットかユーザーの明示操作のみ (intent 不変条件 5)。
  本 API は UI (= ユーザーの明示操作) からのみ叩かれる前提で、フィード URL の
  自動発見も「ユーザーが貼ったサイト URL」起点に限る (発見は feed_fetch 側)
- GET /items は取得済み記事の転載のみ。要約・言い換え・生成はしない (不変条件 1)
- 健康状態 (last_ok_at / last_error / consecutive_failures) は隠さず返す
  (不変条件 2: 取得できなかったものを取得できたことにしない)
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps import get_manager
from database.models import FeedItem, FeedSubscription, Fixture
from saiverse import feed_fetch
from saiverse.feed_fetch import FeedFetchError
from saiverse.feed_manager import (
    FEED_URL_MAX_CHARS,
    FeedSubscriptionLimitError,
    city_feed_fixture_ids,
    feed_url_too_long,
)

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# FeedFetchError.kind → HTTP status。入力起因 (URL がおかしい / フィードが無い /
# 内部ネットワーク宛で拒否) は 422、相手サーバー起因 (落ちている・遅い・
# 応答が大きすぎる) は 502 で区別する。
_FETCH_ERROR_STATUS = {
    "invalid_url": 422,
    "not_a_feed": 422,
    "forbidden_url": 422,
    "timeout": 502,
    "network": 502,
    "http_error": 502,
    "too_large": 502,
}

_ITEMS_DEFAULT_LIMIT = 50
_ITEMS_MAX_LIMIT = 200

# 購読追加リクエスト全体の壁時計予算 (秒)。fetch → discover → fetch と外部
# 取得を最大 3 回直列に呼ぶため、呼び出しごとの既定予算 (60 秒 × 3 = 最大
# 約 180 秒) をそのまま許すと同期 worker を長時間占有する。入口で 1 つの
# deadline を作り全取得へ渡して共有させる (feed_fetch の deadline 引数)。
_SUBSCRIBE_TOTAL_BUDGET_SEC = 90.0

# 購読追加のネットワーク区間 (fetch → discover → 再 fetch) の同時実行上限。
# 検証は外部サイトへの同期取得を最大 3 回直列に行う遅い処理で、無制限に
# 並ぶと同期 worker プールを長時間占有し、他の API まで詰まる。枠が取れない
# ときは待たせず 429 で即返す (やり直しはユーザー操作に委ねる)。
_SUBSCRIBE_CONCURRENCY = threading.BoundedSemaphore(2)


# ------------------------------------------------------------------
# Request models
# ------------------------------------------------------------------

class CreateFixtureRequest(BaseModel):
    """フィード施設の作成。preset_id (プリセットから) か name (空の施設) のどちらか。"""
    building_id: str
    preset_id: Optional[str] = None
    name: Optional[str] = None
    description: str = ""


class AddSubscriptionRequest(BaseModel):
    """購読追加。url はフィード URL でもサイトの URL でもよい (自動発見)。"""
    fixture_id: str
    url: str
    title: Optional[str] = None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_feed_manager(manager):
    feed_manager = getattr(manager, "feed_manager", None)
    if feed_manager is None:
        raise HTTPException(status_code=503, detail="Feed manager not available")
    return feed_manager


def _building_name(manager, building_id: str) -> Optional[str]:
    """現 City の Building 名を返す (無ければ None)。**表示名の解決専用**。

    manager.buildings は起動時に読み込まれるメモリキャッシュで、認可根拠に
    しない — Building の実在・City 所有の検証は、実操作 (INSERT/DELETE) と
    同一 transaction 内の DB 条件が担う (feed_manager 側の
    _require_city_building / city_feed_fixture_ids)。キャッシュを境界に使うと
    事前確認と実操作の間の変化 (Building 削除・所属変更) に耐えられない。
    """
    for building in getattr(manager, "buildings", []) or []:
        if getattr(building, "building_id", None) == building_id:
            return getattr(building, "name", None)
    return None


def _iso_utc(value: Optional[datetime]) -> Optional[str]:
    """DB の naive UTC datetime を UTC 明示の ISO 文字列に。

    tz を付けずに返すとブラウザ側がローカル時刻として誤解釈するため、
    ここで UTC を明示してからフロントに渡す。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _subscription_payload(sub: FeedSubscription) -> dict:
    return {
        "subscription_id": sub.SUBSCRIPTION_ID,
        "title": sub.TITLE or "",
        "feed_url": sub.FEED_URL,
        "site_url": sub.SITE_URL,
        "enabled": bool(sub.ENABLED),
        "last_ok_at": _iso_utc(sub.LAST_OK_AT),
        "last_error": sub.LAST_ERROR,
        "consecutive_failures": sub.CONSECUTIVE_FAILURES or 0,
    }


def _fixture_or_404(manager, fixture_id: str) -> Fixture:
    """現 City のフィード施設 (TYPE="feed_stand") の Fixture を返す。

    存在しない場合だけでなく、**フィード施設以外の Fixture も、別 City の
    フィード施設も 404** — フィード API が任意の Fixture を購読の持ち主に
    できてしまう口と、別 City の購読を列挙・操作できてしまう口 (City 所有権
    境界 — city_feed_fixture_ids の docstring 参照) を閉じる。同じ 404 で
    返して存在自体を漏らさない。
    """
    db = manager.SessionLocal()
    try:
        fixture = (
            db.query(Fixture)
            .filter(
                Fixture.FIXTURE_ID == fixture_id,
                Fixture.FIXTURE_ID.in_(
                    city_feed_fixture_ids(db, manager.city_id)
                ),
            )
            .first()
        )
    finally:
        db.close()
    if fixture is None:
        raise HTTPException(
            status_code=404, detail=f"フィード施設が見つかりません: {fixture_id}"
        )
    return fixture


def _fetch_error_http(exc: FeedFetchError) -> HTTPException:
    status = _FETCH_ERROR_STATUS.get(exc.kind, 502)
    return HTTPException(status_code=status, detail=str(exc))


# ------------------------------------------------------------------
# Presets
# ------------------------------------------------------------------

@router.get("/presets")
def list_presets():
    """フィードプリセット (購読束 + 施設の見た目) の一覧。"""
    from saiverse import feed_presets

    result = []
    for preset_id, preset in feed_presets.FEED_PRESETS.items():
        feeds = preset.get("feeds") or []
        result.append(
            {
                "id": preset_id,
                "name": preset.get("name") or preset_id,
                "description": preset.get("description") or "",
                "feed_count": len(feeds),
                "feed_titles": [
                    (f.get("title") or f.get("url") or "").strip() for f in feeds
                ],
            }
        )
    return result


# ------------------------------------------------------------------
# Fixtures (フィード施設)
# ------------------------------------------------------------------

@router.post("/fixtures")
def create_feed_fixture(body: CreateFixtureRequest, manager=Depends(get_manager)):
    """フィード施設を作成する。プリセットから、または空の施設として。

    Building の実在・City 所有の検証は feed_manager 側の INSERT と同一
    transaction の DB 条件が担う (LookupError → 404)。キャッシュ
    (manager.buildings) による事前関所は置かない (_building_name の
    docstring 参照)。
    """
    feed_manager = _get_feed_manager(manager)

    if body.preset_id:
        from saiverse import feed_presets

        if feed_presets.get_preset(body.preset_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"フィードプリセットが見つかりません: {body.preset_id}",
            )
        try:
            fixture = feed_manager.create_fixture_from_preset(
                body.building_id, body.preset_id
            )
        except LookupError as exc:
            # Building が現 City に実在しない (存在しない / 別 City)
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            # プリセット内の不正 URL 等。feed_manager 側は全か無か —
            # 施設も購読も作られていない (部分状態は残らない)
            raise HTTPException(status_code=422, detail=str(exc))
    else:
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(
                status_code=422,
                detail="preset_id か name のどちらかを指定してください。",
            )
        try:
            fixture = feed_manager.create_feed_fixture(
                body.building_id, name, body.description or ""
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    return {
        "status": "ok",
        "fixture_id": fixture.FIXTURE_ID,
        "building_id": fixture.BUILDING_ID,
        "name": fixture.NAME,
    }


@router.get("/fixtures")
def list_feed_fixtures(manager=Depends(get_manager)):
    """フィード施設の一覧 (購読と健康状態つき)。"""
    feed_manager = _get_feed_manager(manager)

    result = []
    for fixture in feed_manager.list_feed_fixtures():
        subs = feed_manager.list_subscriptions(fixture.FIXTURE_ID)
        result.append(
            {
                "fixture_id": fixture.FIXTURE_ID,
                "building_id": fixture.BUILDING_ID,
                "building_name": _building_name(manager, fixture.BUILDING_ID),
                "name": fixture.NAME,
                "description": fixture.DESCRIPTION or "",
                "subscriptions": [_subscription_payload(s) for s in subs],
            }
        )
    return result


# ------------------------------------------------------------------
# Subscriptions (購読)
# ------------------------------------------------------------------

@router.post("/subscriptions")
def add_subscription(body: AddSubscriptionRequest, manager=Depends(get_manager)):
    """購読を追加する。

    自動発見の流れ (intent 不変条件 5 の「サイトの URL を貼るだけ」UX):

    1. まず url をフィードとして取得してみる → 成功ならそのまま購読
    2. フィードでなければ discover_feed でサイトの自己宣言フィードを探す
       - 候補 1 件 → そのまま購読
       - 候補複数 → ``{"status": "candidates", ...}`` を返してユーザーに選ばせる
         (フロントは選んだ URL で本エンドポイントを再 POST する)
       - 候補 0 件 → 422
    """
    feed_manager = _get_feed_manager(manager)
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="URL を入力してください。")
    # DB 宣言長 (String(512)) を超える URL は保存できない — 外部取得に出る前に
    # 入口で弾く (自動発見の候補 URL は _create_subscription 側の同じ検査が守る)
    if feed_url_too_long(url):
        raise HTTPException(
            status_code=422,
            detail=f"URLが長すぎます (最大{FEED_URL_MAX_CHARS}文字)。",
        )

    _fixture_or_404(manager, body.fixture_id)

    # リクエスト全体で 1 つの時間予算を共有する (_SUBSCRIBE_TOTAL_BUDGET_SEC)
    deadline = time.monotonic() + _SUBSCRIBE_TOTAL_BUDGET_SEC

    # ネットワーク区間の同時実行上限 (_SUBSCRIBE_CONCURRENCY のコメント参照)。
    # 上限に達していたら待たずに 429 で即返す。
    if not _SUBSCRIBE_CONCURRENCY.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail=(
                "フィードの検証が混み合っています。"
                "しばらくしてからやり直してください"
            ),
        )
    try:
        try:
            result = feed_fetch.fetch_feed(url, deadline=deadline)
        except FeedFetchError as exc:
            if exc.kind != "not_a_feed":
                raise _fetch_error_http(exc)
            return _subscribe_via_discovery(
                feed_manager, manager, body, site_url=url, deadline=deadline,
            )

        return _create_subscription(
            feed_manager,
            fixture_id=body.fixture_id,
            feed_url=url,
            site_url=result.site_url or None,
            title=(body.title or "").strip() or result.title,
        )
    finally:
        _SUBSCRIBE_CONCURRENCY.release()


def _subscribe_via_discovery(
    feed_manager, manager, body: AddSubscriptionRequest, *,
    site_url: str, deadline: float,
):
    """フィードでない URL からの自動発見 → 購読 or 候補提示。

    ``deadline`` は購読追加リクエスト入口で作った共有予算 (add_subscription)。
    """
    try:
        candidates = feed_fetch.discover_feed(site_url, deadline=deadline)
    except FeedFetchError as exc:
        raise _fetch_error_http(exc)

    if not candidates:
        raise HTTPException(
            status_code=422,
            detail=(
                "このサイトはフィード (RSS/Atom) を提供していないようです。"
                "フィード URL が分かる場合は直接入力してください。"
            ),
        )

    if len(candidates) > 1:
        return {
            "status": "candidates",
            "candidates": [{"url": c.url, "title": c.title} for c in candidates],
        }

    candidate = candidates[0]
    try:
        result = feed_fetch.fetch_feed(candidate.url, deadline=deadline)
    except FeedFetchError as exc:
        raise _fetch_error_http(exc)

    return _create_subscription(
        feed_manager,
        fixture_id=body.fixture_id,
        feed_url=candidate.url,
        site_url=site_url,
        title=(body.title or "").strip() or candidate.title or result.title,
    )


def _create_subscription(
    feed_manager, *, fixture_id: str, feed_url: str,
    site_url: Optional[str], title: Optional[str],
):
    # 直接入力は route 入口で弾いてあるが、自動発見の候補 URL (外部 HTML 由来)
    # もここを通るため、保存直前の共通経路でも DB 宣言長を検査する。
    # feed_manager 側にも同じ検査 (ValueError) があるが、そちらは下の
    # except で 404 に写像されてしまう — 入力起因のエラーは 422 で返す
    if feed_url_too_long(feed_url):
        raise HTTPException(
            status_code=422,
            detail=f"URLが長すぎます (最大{FEED_URL_MAX_CHARS}文字)。",
        )
    try:
        sub = feed_manager.add_subscription(
            fixture_id, feed_url, site_url=site_url, title=title,
        )
    except FeedSubscriptionLimitError as exc:
        # 購読数上限は入力起因 (ユーザーが減らせば通る) — 404 系の
        # 「見つかりません」ValueError と区別して 422 で返す
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # 表示更新は best-effort — 購読の commit は成功済みで、表示の失敗を
    # 500 に見せない (次の取得サイクルで自己修復する派生物)
    feed_manager.update_fixture_display_best_effort(fixture_id)
    return {"status": "subscribed", "subscription": _subscription_payload(sub)}


@router.delete("/subscriptions/{subscription_id}")
def remove_subscription(subscription_id: str, manager=Depends(get_manager)):
    """購読を削除する (記事・既読カーソルも道連れ — feed_manager 側の仕様)。"""
    feed_manager = _get_feed_manager(manager)

    db = manager.SessionLocal()
    try:
        row = (
            db.query(FeedSubscription.FIXTURE_ID)
            .filter(
                FeedSubscription.SUBSCRIPTION_ID == subscription_id,
                # 別 City の購読は「見つかりません」— 存在を漏らさず、削除も
                # させない (City 所有権境界 — city_feed_fixture_ids 参照)
                FeedSubscription.FIXTURE_ID.in_(
                    city_feed_fixture_ids(db, manager.city_id)
                ),
            )
            .first()
        )
    finally:
        db.close()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"購読が見つかりません: {subscription_id}"
        )

    # 認可は削除 transaction 自身の City 条件が担う (feed_manager 側)。上の
    # 事前確認は 404 の応答と表示更新用 fixture_id の解決のためで、確認と
    # 削除の間に購読が消えた / 所有が変わったときは False = 404 に倒す。
    if not feed_manager.remove_subscription(subscription_id):
        raise HTTPException(
            status_code=404, detail=f"購読が見つかりません: {subscription_id}"
        )
    # 表示更新は best-effort — 削除の commit は成功済みで、表示の失敗を
    # 500 に見せない (次の取得サイクルで自己修復する派生物)
    feed_manager.update_fixture_display_best_effort(row[0])
    return {"status": "ok"}


# ------------------------------------------------------------------
# Manual fetch / Items (透明性 UI)
# ------------------------------------------------------------------

@router.post("/fetch", status_code=202)
def fetch_all_feeds(manager=Depends(get_manager)):
    """全フィードの手動取得を起動する (完了は待たず 202 を返す)。

    既に取得サイクルが実行中のときは起動せず 409 を返す (busy を成功と
    偽装しない)。
    """
    feed_manager = _get_feed_manager(manager)
    worker = feed_manager.fetch_now()
    if worker is None:
        raise HTTPException(status_code=409, detail="取得処理が既に実行中です")
    return {"status": "accepted", "detail": "フィードの取得を開始しました。"}


@router.get("/items")
def list_feed_items(
    fixture_id: str = Query(..., description="フィード施設の Fixture ID"),
    limit: int = Query(_ITEMS_DEFAULT_LIMIT, ge=1, le=_ITEMS_MAX_LIMIT),
    manager=Depends(get_manager),
):
    """フィード施設の取得済み記事一覧 (新しい順)。

    ペルソナが読んでいるものをユーザーも読める透明性 UI (intent §10-4)。
    転載のみで、生成・要約はしない (不変条件 1)。
    """
    _fixture_or_404(manager, fixture_id)

    db = manager.SessionLocal()
    try:
        rows = (
            db.query(FeedItem, FeedSubscription.TITLE)
            .join(
                FeedSubscription,
                FeedItem.SUBSCRIPTION_ID == FeedSubscription.SUBSCRIPTION_ID,
            )
            .filter(
                FeedSubscription.FIXTURE_ID == fixture_id,
                # City 境界は実クエリ自身が運ぶ — _fixture_or_404 は別
                # transaction の事前確認で、確認とこのクエリの間の変化
                # (施設の所属変更等) に耐えるのはこの条件だけ
                FeedSubscription.FIXTURE_ID.in_(
                    city_feed_fixture_ids(db, manager.city_id)
                ),
            )
            .order_by(FeedItem.PUBLISHED_AT.desc(), FeedItem.id.desc())
            .limit(limit)
            .all()
        )
        return {
            "items": [
                {
                    "title": item.TITLE,
                    "summary": item.SUMMARY,
                    "link": item.LINK,
                    "published_at": _iso_utc(item.PUBLISHED_AT),
                    "subscription_title": sub_title or "",
                }
                for item, sub_title in rows
            ]
        }
    finally:
        db.close()
