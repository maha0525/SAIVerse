"""UserConversationTrackHandler unit tests.

Handler の責務:
- 対ユーザー Track の取得 / 自動作成
- Track が running なら invoke_main_line を直接呼ぶ
- Track が running 以外でも、別の活動中 (開いている会話以外の出来事) でなければ
  直接 activate → on_track_activated hook 経由で main_line 起動 (2026-07-07 改訂)
- 別の活動中の場合のみ on_event 判断点へ直結 (handle_user_utterance_conflict、
  track_retirement.md §7.4。旧 set_alert → MetaLayer 経路は撤去)
- Track が running に**遷移したタイミング**で Track コンテキストを SAIMemory に注入
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse import episodes
from saiverse.event_scheduler import EventScheduler
from saiverse.track_handlers import UserConversationTrackHandler
from saiverse.track_manager import (
    STATUS_PENDING,
    STATUS_RUNNING,
    TrackManager,
)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture
def persona(session_factory):
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="alice", HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.commit()
    finally:
        db.close()
    return "alice"


@pytest.fixture
def tm(session_factory):
    return TrackManager(session_factory=session_factory)


@pytest.fixture
def manager_stub(persona, session_factory):
    """history_manager.add_to_persona_only と manager.run_sea_user を mock した最小限の manager スタブ。

    pulse_dispatch.md §9.3 段階 3 で on_track_activated hook 経由の main_line
    Pulse 起動を導入したため、テストでは ``mgr.run_sea_user`` の呼び出し回数を
    検証することで Pulse 起動の有無を確認する。

    ``SessionLocal`` は実 DB (USERNAME 解決用) を指すよう実体を割り当てる
    — MagicMock のままだとユーザー名解決のクエリチェーンが MagicMock を返す。
    """
    history_manager = MagicMock()
    persona_obj = MagicMock()
    persona_obj.history_manager = history_manager
    persona_obj.current_building_id = "test_building"
    mgr = MagicMock()
    mgr.personas = {persona: persona_obj}
    mgr.SessionLocal = session_factory
    return mgr, history_manager


@pytest.fixture
def handler(tm, manager_stub):
    mgr, _hm = manager_stub
    h = UserConversationTrackHandler(track_manager=tm, manager=mgr)
    # pulse_dispatch.md §5: SAIVerseManager と同じパターンで
    # track_activated observer に登録する。これがないと
    # _inject_track_context (= Track 切替通知の SAIMemory 注入) が
    # 走らないので、新仕様のテストが期待通り動かない。
    tm.add_track_activated_observer(h.on_track_activated)
    return h


# ---------------------------------------------------------------------------
# get_or_create_track: tuple (track, was_newly_created) を返す
# ---------------------------------------------------------------------------

def test_first_call_creates_returns_was_newly_created_true(handler, tm, persona):
    track, was_new = handler.get_or_create_track(persona, "1")
    assert was_new is True
    assert track.status == STATUS_RUNNING
    assert track.is_persistent is True
    assert track.track_type == "user_conversation"
    assert track.output_target == "building:current"


def test_second_call_returns_was_newly_created_false(handler, persona):
    track1, was_new_1 = handler.get_or_create_track(persona, "1")
    track2, was_new_2 = handler.get_or_create_track(persona, "1")
    assert was_new_1 is True
    assert was_new_2 is False
    assert track1.track_id == track2.track_id


def test_different_user_ids_get_separate_tracks(handler, persona):
    t1, was_new_1 = handler.get_or_create_track(persona, "1")
    t2, was_new_2 = handler.get_or_create_track(persona, "2")
    assert was_new_1 is True
    assert was_new_2 is True
    assert t1.track_id != t2.track_id


def test_title_uses_username(handler, persona):
    """タイトルは USERNAME 入りの「対 <名前>（id:N）会話」形式 (旧 user1 形式ではない)。"""
    track, _ = handler.get_or_create_track(persona, "1")
    # fixture の USERNAME="tester"
    assert track.title == "対 tester（id:1）会話"


def test_unknown_user_id_falls_back_to_generic_name(handler, persona):
    """USERNAME を引けない user_id ではフォールバック名「ユーザー」を使う。"""
    track, _ = handler.get_or_create_track(persona, "999")
    assert track.title == "対 ユーザー（id:999）会話"


def test_legacy_title_is_healed_on_fetch(handler, tm, persona):
    """旧形式タイトルの既存 Track は再取得時に新形式へ貼り替えられる。"""
    track, _ = handler.get_or_create_track(persona, "1")
    # 旧形式に巻き戻してから再取得
    tm.set_title(track.track_id, "対 user1 会話")
    healed, was_new = handler.get_or_create_track(persona, "1")
    assert was_new is False
    assert healed.title == "対 tester（id:1）会話"
    # DB にも反映されている
    assert tm.get(track.track_id).title == "対 tester（id:1）会話"


# ---------------------------------------------------------------------------
# build_track_context: Track コンテキスト本文の組み立て
# ---------------------------------------------------------------------------

def test_build_track_context_includes_required_sections(handler, tm, persona):
    track, _ = handler.get_or_create_track(persona, "1")
    text = handler.build_track_context(track)
    # 切替通知
    assert "Track 切替通知" in text
    # Track の identity
    assert "user_conversation" in text
    # 完了後挙動 (pulse_completion_notice 由来)
    assert "ユーザーの返答を待つ" in text
    # スペル一覧はシステムプロンプト側 (SpellListSection) に集約したので、
    # Track 切替通知には載せない。
    assert "track_pause" not in text
    assert "利用可能なスペル名" not in text


# ---------------------------------------------------------------------------
# on_user_utterance: running 経路
# ---------------------------------------------------------------------------

def test_first_utterance_creates_track_and_starts_pulse_via_hook(handler, tm, persona, manager_stub):
    """初回会話: Track 作成 → activate → on_track_activated hook 経由で
    Track コンテキスト注入 + main_line Pulse 起動 (invoke_main_line は呼ばれない)。"""
    mgr, history_manager = manager_stub

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "おはよう"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    # 新規作成時は hook 経由なので invoke_main_line ハードコードは呼ばれない
    assert invoked == []
    # hook 内で Track コンテキスト注入が行われる
    history_manager.add_to_persona_only.assert_called_once()
    args, _kwargs = history_manager.add_to_persona_only.call_args
    assert args[0]["role"] == "user"
    assert "Track 切替通知" in args[0]["content"]
    assert "<system>" in args[0]["content"]
    # hook 内で main_line Pulse が起動される
    mgr.run_sea_user.assert_called_once()


def test_subsequent_utterance_on_running_track_uses_invoke_main_line(handler, tm, persona, manager_stub):
    """既存 running Track への発話: 直接経路 (1-A) で invoke_main_line を呼ぶ。
    hook は走らない (activate が起きないので)、注入なし。"""
    mgr, history_manager = manager_stub
    handler.get_or_create_track(persona, "1")  # 1 回目で running 化 + hook 経由 Pulse 起動
    history_manager.reset_mock()  # 1 回目の注入呼び出しをクリア
    mgr.run_sea_user.reset_mock()  # 1 回目の hook 経由 Pulse 起動をクリア

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "二回目"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    # 既存 running の場合は直接経路で invoke_main_line が呼ばれる
    assert invoked == [True]
    # 既存 running セッション継続なので注入なし、hook 経由 Pulse 起動もなし
    history_manager.add_to_persona_only.assert_not_called()
    mgr.run_sea_user.assert_not_called()


# ---------------------------------------------------------------------------
# life.md §7 案 Y (2026-07-13): wait_response タイムアウトは Track を pause
# しなくなった。「同一 Track への再 activate」という事象自体が消え、
# redundant_track_switch_notification_on_reactivation.md が構造的に根治する。
# ---------------------------------------------------------------------------

def test_reactivation_after_wait_response_timeout_no_longer_injects_notice(
    handler, tm, persona, manager_stub
):
    """wait_response タイムアウト後の再発話は running のまま直接応答 → 通知は注入されない。

    根治の証明: 旧実装はタイムアウトで running→pending に落ち、再発話のたびに
    activate → Track 切替通知が積もっていた (issue の実測 7 件)。
    ``TrackManager._handle_wait_response_timeout`` はもう Track を pause しない
    ため、この経路そのものが無くなる。
    """
    mgr, history_manager = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    history_manager.reset_mock()  # 初回注入をクリア
    mgr.run_sea_user.reset_mock()

    # wait_response タイムアウト相当を直接叩く (base_time=None → 即時判定)。
    timeout_calls = []
    tm.wait_response_timeout_provider = lambda t: (30, None)
    tm.wait_response_timeout_callback = (
        lambda pid, tid: timeout_calls.append((pid, tid))
    )
    tm._handle_wait_response_timeout(track.track_id, persona)
    assert timeout_calls == [(persona, track.track_id)]
    # Track は running のまま (時間経過は目的を動かさない)
    assert tm.get(track.track_id).status == STATUS_RUNNING

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "戻ってきた"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    # running のまま直接経路 (1-A) → invoke_main_line 直呼び、hook は走らない
    assert invoked == [True]
    assert tm.get(track.track_id).status == STATUS_RUNNING
    history_manager.add_to_persona_only.assert_not_called()
    mgr.run_sea_user.assert_not_called()


def test_second_conversation_rearms_timeout_and_closes_new_episode(
    session_factory, persona, manager_stub
):
    """一度 timeout を消費した running Track でも、次の会話を再び閉じられる。

    life.md §7 案 Y では timeout 後も Track は running のまま残る。そのため
    二度目の発話は activate を通らず、直接応答経路自身が一回限りの timeout を
    再装填しなければ、新しい conversation Episode が永久に open のままになる。
    """
    scheduler = EventScheduler()
    mgr, _history_manager = manager_stub
    # MagicMock の動的属性では Episode キャッシュとして振る舞わないため、
    # 本番 manager と同じ実 dict を明示する。
    mgr._open_episode_cache = {}

    tm_with_scheduler = TrackManager(
        session_factory=session_factory,
        event_scheduler=scheduler,
        wait_response_timeout_provider=lambda _track: (30, None),
        wait_response_timeout_callback=(
            lambda pid, _tid: episodes.close_conversation_episode(mgr, pid)
        ),
    )
    handler_with_scheduler = UserConversationTrackHandler(
        track_manager=tm_with_scheduler,
        manager=mgr,
    )
    tm_with_scheduler.add_track_activated_observer(
        handler_with_scheduler.on_track_activated
    )

    track, was_new = handler_with_scheduler.get_or_create_track(persona, "1")
    assert was_new is True
    first_episode = episodes.get_open_episode(
        mgr, persona, kind=episodes.KIND_CONVERSATION
    )
    assert first_episode is not None

    timeout_key = f"wait_response_timeout:{track.track_id}"
    assert scheduler.has_key(timeout_key)
    first_fire_at = scheduler.next_fire_time()
    assert first_fire_at is not None
    assert scheduler.run_due(first_fire_at) == 1
    assert episodes.get_open_episode(
        mgr, persona, kind=episodes.KIND_CONVERSATION
    ) is None
    assert not scheduler.has_key(timeout_key)

    invoked = []
    handler_with_scheduler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "もう一度話そう"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    assert invoked == [True]
    second_episode = episodes.get_open_episode(
        mgr, persona, kind=episodes.KIND_CONVERSATION
    )
    assert second_episode is not None
    assert second_episode["episode_id"] != first_episode["episode_id"]
    assert scheduler.has_key(timeout_key)

    second_fire_at = scheduler.next_fire_time()
    assert second_fire_at is not None
    assert scheduler.run_due(second_fire_at) == 1
    assert episodes.get_open_episode(
        mgr, persona, kind=episodes.KIND_CONVERSATION
    ) is None


def test_reactivation_after_real_track_switch_still_activates_and_notifies(
    handler, tm, persona, manager_stub
):
    """本物の Track 切替 (別 Track の activate による displacement) の後の
    会話再開は、従来どおり pending→activate→Track 切替通知が出る。

    根治するのは「同一 Track への再 activate」のみ — ペルソナが実際に別の
    目的へ移った（そして戻ってきた）場合の通知は正しい情報であり、消さない。
    """
    mgr, history_manager = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    history_manager.reset_mock()
    mgr.run_sea_user.reset_mock()

    # 自律 Track の activate が本物の displacement を起こす (時間経過由来ではない)。
    other_id = tm.create(
        persona_id=persona, track_type="autonomous",
        title="別の目的", initial_status=STATUS_RUNNING,
    )
    assert tm.get(track.track_id).status == STATUS_PENDING
    tm.complete(other_id)  # 自律 Track が完了して running 衝突が消える

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "戻ってきた"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    # 衝突なし → 直接 activate → hook 経由で通知 + main_line Pulse 起動
    assert tm.get(track.track_id).status == STATUS_RUNNING
    history_manager.add_to_persona_only.assert_called_once()
    args, _kwargs = history_manager.add_to_persona_only.call_args
    assert "Track 切替通知" in args[0]["content"]
    mgr.run_sea_user.assert_called_once()
    assert invoked == []  # hook 経由 (invoke_main_line ハードコードは呼ばれない)


# ---------------------------------------------------------------------------
# on_user_utterance: pending + 別の活動なし → 直接 activate (2026-07-07 改訂)
# ---------------------------------------------------------------------------

def test_pending_track_without_open_activity_directly_activates(
    handler, tm, persona, manager_stub, monkeypatch
):
    """pending Track + 開いている会話以外の出来事なし → 判断を経由せず直接
    activate。on_track_activated hook 経由で Track コンテキスト注入 + main_line
    Pulse 起動 (Idle への呼びかけは常に即応答、pulse_dispatch.md §4.2 Q2 改訂)。"""
    mgr, history_manager = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)  # running -> pending
    history_manager.reset_mock()  # 初回注入をクリア
    mgr.run_sea_user.reset_mock()  # 初回 hook 経由 Pulse 起動をクリア

    from saiverse import autonomy_wiring

    conflict_calls = []
    monkeypatch.setattr(
        autonomy_wiring, "handle_user_utterance_conflict",
        lambda *a, **kw: conflict_calls.append((a, kw)) or "judged:stub",
    )

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "話しかけた"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    # 直結経路 (on_event 判断) は通らない
    assert conflict_calls == []
    # 直接 activate されて running になっている
    assert tm.get(track.track_id).status == STATUS_RUNNING
    # hook 経由で Track コンテキスト注入 + main_line Pulse 起動
    history_manager.add_to_persona_only.assert_called_once()
    mgr.run_sea_user.assert_called_once()
    # invoke_main_line のハードコード起動は無し (hook 経由に統一)
    assert invoked == []


# ---------------------------------------------------------------------------
# on_user_utterance: 直結経路 (別の活動中 = 開いている出来事 ≠ 会話。
# track_retirement.md §7.4 — 旧 set_alert → MetaLayer 経路の後継)
# ---------------------------------------------------------------------------

def _make_busy(mgr, persona):
    """「別の活動中」を作る: 会話の出来事を閉じ、作業セッションの出来事を開く。"""
    episodes.close_conversation_episode(mgr, persona)
    episodes.open_episode(
        mgr, persona, episodes.KIND_WORK_SESSION,
        building_id="test_building",
        participants=[persona],
        meta={"title": "作業セッション"},
    )


def test_busy_utterance_fires_direct_connection_and_passes_utterance(
    handler, tm, persona, manager_stub, monkeypatch
):
    """pending Track + 別の活動中 → handle_user_utterance_conflict が呼ばれ、
    発話テキストと activate callback が渡る。判断が engage しなければ
    Track は pending のまま・応答なし (旧 alert 経路と同じ挙動)。"""
    mgr, history_manager = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)
    _make_busy(mgr, persona)
    history_manager.reset_mock()
    mgr.run_sea_user.reset_mock()

    from saiverse import autonomy_wiring

    conflict_calls = []

    def fake_conflict(manager, persona_id, utterance_text, *, activate,
                      track_id, user_id):
        # 応対先の凍結 (F4) — 回復 tick が後から engage_now を出したときに
        # activate する Track。handler が渡し損ねると回収が応対先を失う。
        conflict_calls.append((persona_id, utterance_text, track_id, user_id))
        return "judged:note_only"  # engage しない

    monkeypatch.setattr(
        autonomy_wiring, "handle_user_utterance_conflict", fake_conflict,
    )

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "話しかけた"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    assert conflict_calls == [(persona, "話しかけた", track.track_id, "1")]
    # engage しない → 応答なし・Track は pending のまま
    assert invoked == []
    assert mgr.run_sea_user.call_count == 0
    assert tm.get(track.track_id).status == STATUS_PENDING
    history_manager.add_to_persona_only.assert_not_called()


def test_busy_utterance_engage_now_activates_and_starts_pulse_via_hook(
    handler, tm, persona, manager_stub, monkeypatch
):
    """直結経路で判断が engage (activate callback を呼ぶ) → running になり、
    on_track_activated hook 経由で Track コンテキスト注入 + main_line Pulse 起動。"""
    mgr, history_manager = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)
    _make_busy(mgr, persona)
    history_manager.reset_mock()
    mgr.run_sea_user.reset_mock()

    from saiverse import autonomy_wiring

    def fake_conflict(manager, persona_id, utterance_text, *, activate,
                      track_id, user_id):
        activate()
        return "judged:engage_now"

    monkeypatch.setattr(
        autonomy_wiring, "handle_user_utterance_conflict", fake_conflict,
    )

    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "話しかけた"},
        invoke_main_line=lambda *_a, **_kw: None,
    )
    assert tm.get(track.track_id).status == STATUS_RUNNING
    history_manager.add_to_persona_only.assert_called_once()
    mgr.run_sea_user.assert_called_once()


def test_busy_detection_survives_conversation_episode_opened_later(
    handler, tm, persona, manager_stub, monkeypatch
):
    """回帰 (2026-08-14 Codex 指摘 F2): 会話の出来事が作業より**後**に開いていても
    「別の活動中」を見落とさない。

    孤児化した会話の出来事 (Track が running を離れたのに閉じ損ねた行) が
    作業セッションより後に開いている並びでは、「最後に開いた 1 件」を見る旧実装が
    会話を見て打ち切り、仲裁を経ずに即応答していた。
    """
    mgr, _hm = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)
    _make_busy(mgr, persona)
    # 作業セッションの後に会話の出来事を開く (= 最後に開いた open は会話)
    episodes.open_conversation_episode(
        mgr, persona, building_id="test_building", participants=[persona, "1"],
    )
    mgr.run_sea_user.reset_mock()

    from saiverse import autonomy_wiring

    conflict_calls = []

    def fake_conflict(manager, persona_id, utterance_text, *, activate,
                      track_id, user_id):
        conflict_calls.append(persona_id)
        return "judged:note_only"

    monkeypatch.setattr(
        autonomy_wiring, "handle_user_utterance_conflict", fake_conflict,
    )

    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "話しかけた"},
        invoke_main_line=lambda *_a, **_kw: None,
    )
    # 仲裁を経由し、engage しないので Track は pending のまま
    assert conflict_calls == [persona]
    assert tm.get(track.track_id).status == STATUS_PENDING
    assert mgr.run_sea_user.call_count == 0


def test_busy_utterance_conflict_raise_does_not_propagate(
    handler, tm, persona, manager_stub, monkeypatch
):
    """直結経路の例外は on_user_utterance の外に伝播しない (dispatcher 側の
    フォールバックに任せる前に handler 内では落とさない)。"""
    mgr, _hm = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)
    _make_busy(mgr, persona)
    mgr.run_sea_user.reset_mock()

    from saiverse import autonomy_wiring

    def bad_conflict(manager, persona_id, utterance_text, *, activate,
                     track_id, user_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        autonomy_wiring, "handle_user_utterance_conflict", bad_conflict,
    )

    with pytest.raises(RuntimeError):
        # Handler 自体は透過 — 例外時のフォールバック (invoke_main_line 直呼び)
        # は PulseDispatcher.dispatch_user_utterance が担う (本体 §7)。
        handler.on_user_utterance(
            persona_id=persona,
            user_id="1",
            event={"role": "user", "content": "x"},
            invoke_main_line=lambda *_a, **_kw: None,
        )


# ---------------------------------------------------------------------------
# 回復経路: 仲裁が席を残したまま落ちた後、回復 tick が engage_now を出した場合
# (autonomy_wiring._dispatch_recovered_response。track_retirement F4)
# ---------------------------------------------------------------------------


def _recover(mgr, persona, **overrides):
    """回収経路の応対を 1 回走らせる (台帳 payload 相当の context を渡す)。"""
    from saiverse import autonomy_wiring

    context = {
        "event_text": "ユーザーがあなたに話しかけました:\nちょっといい？",
        "is_alert": False,
        "utterance_conflict": True,
    }
    context.update(overrides)
    return autonomy_wiring._dispatch_recovered_response(mgr, persona, context)


def test_recovered_utterance_conflict_activates_the_frozen_track_once(
    handler, tm, persona, manager_stub
):
    """凍結された会話 Track を activate し、hook 経由で応対が **1 回だけ** 走る。

    回収側が外部イベント形 (``<system>[外部イベント通知]`` + track_user_conversation)
    へ縮退していた頃は、応答は届くのに Track は running にならず会話の出来事も
    開かなかった (帳簿の乖離。2026-08-14 Codex 指摘 F4)。初回発火と同じ入口
    (activate) を通すことで、切替通知・会話の出来事・main_line が揃う。
    """
    from saiverse import autonomy_wiring

    mgr, history_manager = manager_stub
    mgr.track_manager = tm
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)
    _make_busy(mgr, persona)
    history_manager.reset_mock()
    mgr.run_sea_user.reset_mock()

    outcome = _recover(mgr, persona, conversation_track_id=track.track_id)

    assert outcome == autonomy_wiring.RECOVERED_DISPATCH_OK
    assert tm.get(track.track_id).status == STATUS_RUNNING
    history_manager.add_to_persona_only.assert_called_once()  # 切替通知は 1 回
    mgr.run_sea_user.assert_called_once()                     # main_line も 1 回
    # 会話の出来事が開いている (「いま」の帳簿が応答と一致する)
    assert episodes.get_open_episode(
        mgr, persona, kind=episodes.KIND_CONVERSATION) is not None


def test_recovered_utterance_conflict_skips_when_conversation_is_live(
    handler, tm, persona, manager_stub
):
    """既に会話が生きているなら activate しない (二重応対の回避)。

    案 Y では会話終了後も Track が running のまま残るので、判定は running では
    なく **開いている会話の出来事** で行う。
    """
    from saiverse import autonomy_wiring

    mgr, history_manager = manager_stub
    mgr.track_manager = tm
    track, _ = handler.get_or_create_track(persona, "1")  # 作成時に activate 済み
    episodes.open_conversation_episode(
        mgr, persona, building_id="test_building", participants=[persona, "1"],
    )
    history_manager.reset_mock()
    mgr.run_sea_user.reset_mock()

    outcome = _recover(mgr, persona, conversation_track_id=track.track_id)

    assert outcome == autonomy_wiring.RECOVERED_DISPATCH_OK
    assert mgr.run_sea_user.call_count == 0
    history_manager.add_to_persona_only.assert_not_called()


def test_recovered_utterance_conflict_reactivates_when_conversation_closed(
    handler, tm, persona, manager_stub
):
    """running のまま残っているだけ (会話の出来事なし) なら activate する。

    案 Y の残留 running を「応対済み」と読むと、この発話への応答が失われる。
    """
    from saiverse import autonomy_wiring

    mgr, history_manager = manager_stub
    mgr.track_manager = tm
    track, _ = handler.get_or_create_track(persona, "1")
    episodes.close_conversation_episode(mgr, persona)  # 会話は終了済み
    history_manager.reset_mock()
    mgr.run_sea_user.reset_mock()

    outcome = _recover(mgr, persona, conversation_track_id=track.track_id)

    assert outcome == autonomy_wiring.RECOVERED_DISPATCH_OK
    assert tm.get(track.track_id).status == STATUS_RUNNING
    mgr.run_sea_user.assert_called_once()


def test_recovered_utterance_conflict_without_target_is_unroutable(
    handler, tm, persona, manager_stub
):
    """応対先が凍結されていない古い payload は、外部イベント形へ縮退させない。

    ユーザーの発話を「外部イベント通知」として流し込むのが F4 の欠陥そのもの
    なので、応対先を決められないときは ERROR を残して打ち切る (再試行しても
    直らない)。
    """
    from saiverse import autonomy_wiring

    mgr, history_manager = manager_stub
    mgr.track_manager = tm
    handler.get_or_create_track(persona, "1")
    history_manager.reset_mock()
    mgr.run_sea_user.reset_mock()

    outcome = _recover(mgr, persona)  # conversation_track_id なし

    assert outcome == autonomy_wiring.RECOVERED_DISPATCH_UNROUTABLE
    assert mgr.run_sea_user.call_count == 0


def test_recovered_external_event_still_uses_the_event_dispatch(
    persona, manager_stub
):
    """外部イベント (仲裁でない) は従来どおり応対 Pulse の再構成を通る。"""
    from saiverse import autonomy_wiring

    mgr, _hm = manager_stub
    fired = []
    mgr.pulse_dispatcher = MagicMock()
    mgr.pulse_dispatcher.dispatch_schedule_fire.side_effect = (
        lambda **kw: fired.append(kw) or {"action": "execute",
                                          "runtime_outcome": "completed"}
    )

    outcome = autonomy_wiring._dispatch_recovered_response(
        mgr, persona, {"event_text": "掲示板の告知", "is_alert": False},
    )

    assert outcome == autonomy_wiring.RECOVERED_DISPATCH_OK
    assert len(fired) == 1
    assert "[外部イベント通知]" in fired[0]["user_input"]


# ---------------------------------------------------------------------------
# manager 未指定でも動く (テスト容易性 / 後方互換性のため)
# ---------------------------------------------------------------------------

def test_handler_works_without_manager_just_skips(tm, persona):
    """manager=None でもエラーにならない。新仕様では hook 経由の起動もスキップされる
    (manager 無しなので Pulse 起動経路は機能しないが例外で落ちない)。"""
    h = UserConversationTrackHandler(track_manager=tm, manager=None)
    tm.add_track_activated_observer(h.on_track_activated)
    invoked = []
    # 例外伝播しないことが重要
    h.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "hi"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    # manager=None かつ新規作成 → hook は走るが _start_main_line_pulse / _inject_track_context が
    # WARN 出してスキップ。invoke_main_line も新仕様では was_newly_created=True 時には呼ばれない。
    assert invoked == []


# ---------------------------------------------------------------------------
# suppress_pulse: ライフビュー停止パッケージのサイレント activate
# (persona_activity_view.md §6.3)
# ---------------------------------------------------------------------------

def test_silent_activate_injects_context_but_skips_pulse(handler, tm, persona, manager_stub):
    """activate(suppress_pulse=True) では Track 切替通知は注入されるが
    main_line Pulse は起動しない (= 停止ボタンで自動発言させない)。"""
    mgr, history_manager = manager_stub

    track, _ = handler.get_or_create_track(persona, "1")
    # 作成 (initial_status=running) 時点の hook 呼び出し分を控えておく
    inject_calls_after_create = history_manager.add_to_persona_only.call_count
    pulse_calls_after_create = mgr.run_sea_user.call_count

    tm.pause(track.track_id)
    tm.activate(track.track_id, suppress_pulse=True)

    assert tm.get(track.track_id).status == STATUS_RUNNING
    # Track 切替通知は注入される (ペルソナは「ユーザー待ちに戻った」と知る)
    assert history_manager.add_to_persona_only.call_count == inject_calls_after_create + 1
    # main_line Pulse は起動しない
    assert mgr.run_sea_user.call_count == pulse_calls_after_create


def test_normal_activate_still_starts_pulse(handler, tm, persona, manager_stub):
    """通常の activate (suppress_pulse 省略) では従来通り main_line Pulse が起動する。"""
    mgr, history_manager = manager_stub

    track, _ = handler.get_or_create_track(persona, "1")
    pulse_calls_after_create = mgr.run_sea_user.call_count

    tm.pause(track.track_id)
    tm.activate(track.track_id)

    assert tm.get(track.track_id).status == STATUS_RUNNING
    assert mgr.run_sea_user.call_count == pulse_calls_after_create + 1
