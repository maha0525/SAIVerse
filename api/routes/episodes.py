"""できごとタイムライン API — City 単位の「今日のできごと」一覧 (画面 A 用)。

life_concept_map.md §8.1「用途起点: 仕事から帰ったユーザーが『今日は何が
あったかな』と眺める」の読み出し面。一覧は saiverse/episodes.py の
``list_today`` (SELECT のみ) を使い、LLM は呼ばない (新聞の決定論構築原則)。

City への帰属は **AI.HOME_CITYID** で判定する。理由:

1. episodes 行はペルソナ単位の主観 (§8.1 複数主観) なので、City への帰属も
   「その City に住むペルソナのできごと」とペルソナ起点で引くのが自然。
2. Episode.BUILDING_ID は nullable (場所を持たないできごとが合法) のため、
   Building→City 経路では全行を分類できない。
3. AI テーブルに現在地カラムは無い (現在地は in-memory の
   ``persona.current_building_id`` と BuildingOccupancyLog)。DB だけで
   決定論的に引ける City リンクは HOME_CITYID のみで、出張中 (IS_DISPATCHED)
   のペルソナのできごともホーム City のタイムラインに載る方が
   「うちの街の子たちの今日」という画面 A の意味に合う。

日付境界は City.TIMEZONE の暦日 (00:00〜翌 00:00)。時刻は epoch 秒と
City タイムゾーンの ISO 文字列の両方を返す。
"""
import logging
from datetime import date as date_cls, datetime, time as dt_time, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import avatar_path_to_url, get_manager
from saiverse import clock
from saiverse.episodes import list_today

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class EpisodeTimelineItem(BaseModel):
    """タイムライン 1 行 = Episode 全属性 + 表示用の名前解決。

    ``group_key`` が同じ行は同一の世界的できごと (§8.1 複数主観) なので、
    フロントは group_key で束ねて 1 カードに複数ペルソナ主観を並べられる。
    レスポンスの episodes 配列は group_key が同じ行が必ず隣接するように
    ソート済み (グループ先頭時刻 → group_key → 行の開始時刻の順)。
    """
    # --- Episode 全属性 (saiverse/episodes.py:_to_dict と同形) ---
    episode_id: str
    short_id: Optional[int]
    episode_ref: Optional[str]           # "episode:N"
    persona_id: str
    kind: str                            # conversation / work_session / slot / presence / stroll / other
    occurrence_id: Optional[str]
    started_at: int                      # epoch 秒
    ended_at: Optional[int]              # epoch 秒。open (「いま」行) は None
    building_id: Optional[str]
    participants: List[str]
    origin_ref: Optional[str]            # None = 自発 (無計画のできごと)
    status: str                          # 'open' | 'closed'
    digest_ref: Optional[str]
    meta: Optional[Dict[str, Any]]
    # --- 表示用の付加情報 ---
    persona_name: str
    persona_avatar_url: Optional[str]
    building_name: Optional[str]
    group_key: str                       # occurrence_id、無ければ episode_id (単独グループ)
    started_at_iso: str                  # City タイムゾーンの ISO 8601
    ended_at_iso: Optional[str]


class EpisodesTimelineResponse(BaseModel):
    city_id: int
    date: str                            # "YYYY-MM-DD" (City タイムゾーンの暦日)
    timezone: str
    day_start: int                       # epoch 秒 (その暦日の 00:00)
    day_end: int                         # epoch 秒 (翌日の 00:00)
    episodes: List[EpisodeTimelineItem]


def _city_zoneinfo(tz_name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        LOGGER.warning("[episodes] invalid City TIMEZONE %r; falling back to UTC", tz_name)
        return ZoneInfo("UTC")


@router.get("", response_model=EpisodesTimelineResponse)
def list_city_episodes(
    city_id: int,
    date: Optional[str] = None,
    manager=Depends(get_manager),
) -> EpisodesTimelineResponse:
    """City に属するペルソナの当日 episodes を返す (画面 A: 今日のできごと)。

    - ``date``: "YYYY-MM-DD"。省略時は City.TIMEZONE での今日
      (現在時刻は仮想クロック尊重のため ``saiverse.clock.now()`` 経由)。
    - status=open の行 (まだ開いているできごと = 「いま」行) も含む。
    - ソートと束ね: ``group_key`` (= occurrence_id、無ければ episode_id) が
      同じ行が隣接するよう、(グループ内最小 started_at, group_key,
      started_at, episode_id) の昇順で返す。
    """
    from database.models import AI as AIModel, Building as BuildingModel, City as CityModel

    db = manager.SessionLocal()
    try:
        city = db.query(CityModel).filter(CityModel.CITYID == city_id).first()
        if city is None:
            raise HTTPException(status_code=404, detail=f"City {city_id} not found")
        tz = _city_zoneinfo(city.TIMEZONE)

        persona_rows = (
            db.query(AIModel.AIID, AIModel.AINAME, AIModel.AVATAR_IMAGE)
            .filter(AIModel.HOME_CITYID == city_id)
            .all()
        )
    finally:
        db.close()

    # 日付の決定 (省略時は City タイムゾーンでの今日。仮想クロック尊重)
    if date is None:
        now_epoch = int(clock.now().timestamp())
        target_date = datetime.fromtimestamp(now_epoch, tz).date()
    else:
        try:
            target_date = date_cls.fromisoformat(date.strip())
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"invalid date: {date!r} (expected YYYY-MM-DD)"
            )

    day_start_dt = datetime.combine(target_date, dt_time(0), tzinfo=tz)
    day_end_dt = datetime.combine(target_date + timedelta(days=1), dt_time(0), tzinfo=tz)
    day_start = int(day_start_dt.timestamp())
    day_end = int(day_end_dt.timestamp())

    # ペルソナごとに当日 episodes を集める (saiverse/episodes.py の一覧関数を使う)
    persona_info = {
        row.AIID: (row.AINAME or row.AIID, avatar_path_to_url(row.AVATAR_IMAGE))
        for row in persona_rows
    }
    raw_episodes: List[Dict[str, Any]] = []
    for persona_id in persona_info:
        raw_episodes.extend(list_today(manager, persona_id, day_start, day_end))

    # Building 表示名の解決 (1 クエリ)
    building_ids = {ep["building_id"] for ep in raw_episodes if ep.get("building_id")}
    building_names: Dict[str, str] = {}
    if building_ids:
        db = manager.SessionLocal()
        try:
            rows = (
                db.query(BuildingModel.BUILDINGID, BuildingModel.BUILDINGNAME)
                .filter(BuildingModel.BUILDINGID.in_(building_ids))
                .all()
            )
            building_names = {r.BUILDINGID: r.BUILDINGNAME for r in rows}
        finally:
            db.close()

    def _iso(epoch: Optional[int]) -> Optional[str]:
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, tz).isoformat()

    items: List[Dict[str, Any]] = []
    for ep in raw_episodes:
        name, avatar_url = persona_info[ep["persona_id"]]
        b_id = ep.get("building_id")
        items.append({
            **ep,
            "persona_name": name,
            "persona_avatar_url": avatar_url,
            "building_name": building_names.get(b_id) if b_id else None,
            "group_key": ep.get("occurrence_id") or ep["episode_id"],
            "started_at_iso": _iso(ep["started_at"]),
            "ended_at_iso": _iso(ep.get("ended_at")),
        })

    # 同一 group_key の行が隣接するようにソートする (docstring 参照)。
    group_start: Dict[str, int] = {}
    for item in items:
        key = item["group_key"]
        if key not in group_start or item["started_at"] < group_start[key]:
            group_start[key] = item["started_at"]
    items.sort(
        key=lambda i: (
            group_start[i["group_key"]], i["group_key"], i["started_at"], i["episode_id"],
        )
    )

    return EpisodesTimelineResponse(
        city_id=city_id,
        date=target_date.isoformat(),
        timezone=str(tz.key),
        day_start=day_start,
        day_end=day_end,
        episodes=[EpisodeTimelineItem(**item) for item in items],
    )
