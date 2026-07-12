"""UserConversationTrackHandler unit tests.

Handler の責務:
- 対ユーザー Track の取得 / 自動作成
- Track が running なら invoke_main_line を直接呼ぶ
- Track が running 以外でも、別の running Track と衝突していなければ直接
  activate → on_track_activated hook 経由で main_line 起動 (2026-07-07 改訂)
- 別の running Track と衝突している場合のみ set_alert を発火 → MetaLayer 仲裁
- Track が running に**遷移したタイミング**で Track コンテキストを SAIMemory に注入
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.track_handlers import UserConversationTrackHandler
from saiverse.track_manager import (
    STATUS_ALERT,
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

    alert_observer_calls = []
    tm.add_alert_observer(
        lambda pid, tid, ctx: alert_observer_calls.append((pid, tid, ctx))
    )

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "おはよう"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    # 新規作成時は hook 経由なので invoke_main_line ハードコードは呼ばれない
    assert invoked == []
    # alert observer は呼ばれない (Track が新規 running なので)
    assert alert_observer_calls == []
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

    alert_observer_calls = []
    tm.add_alert_observer(
        lambda pid, tid, ctx: alert_observer_calls.append((pid, tid, ctx))
    )

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "二回目"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    # 既存 running の場合は直接経路で invoke_main_line が呼ばれる
    assert invoked == [True]
    assert alert_observer_calls == []
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
# on_user_utterance: pending + running 衝突なし → 直接 activate (2026-07-07 改訂)
# ---------------------------------------------------------------------------

def test_pending_track_without_running_conflict_directly_activates(handler, tm, persona, manager_stub):
    """pending Track + 別の running Track なし → set_alert せず直接 activate。
    on_track_activated hook 経由で Track コンテキスト注入 + main_line Pulse 起動
    (Idle への呼びかけは常に即応答、pulse_dispatch.md §4.2 Q2 改訂)。"""
    mgr, history_manager = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)  # running -> pending (他に running なし)
    history_manager.reset_mock()  # 初回注入をクリア
    mgr.run_sea_user.reset_mock()  # 初回 hook 経由 Pulse 起動をクリア

    alert_observer_calls = []
    tm.add_alert_observer(
        lambda pid, tid, ctx: alert_observer_calls.append((pid, tid, ctx))
    )

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "話しかけた"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    # メタ判断経路 (set_alert) は通らない
    assert alert_observer_calls == []
    # 直接 activate されて running になっている
    assert tm.get(track.track_id).status == STATUS_RUNNING
    # hook 経由で Track コンテキスト注入 + main_line Pulse 起動
    history_manager.add_to_persona_only.assert_called_once()
    mgr.run_sea_user.assert_called_once()
    # invoke_main_line のハードコード起動は無し (hook 経由に統一)
    assert invoked == []


# ---------------------------------------------------------------------------
# on_user_utterance: alert 経路 (別の running Track と衝突している場合のみ)
# ---------------------------------------------------------------------------

def _create_conflicting_running_track(tm, persona):
    """別種別の running Track (作業中の自律 Track 相当) を作る。"""
    return tm.create(
        persona_id=persona,
        track_type="autonomous",
        title="作業セッション",
        initial_status=STATUS_RUNNING,
    )


def test_pending_track_with_running_conflict_triggers_alert_no_response_when_not_activated(
    handler, tm, persona, manager_stub
):
    """pending Track + 別の running Track あり → 熟慮経路 (set_alert)。
    MetaLayer が activate しない場合は応答しない (pulse_dispatch.md §4.2)。
    invoke_main_line のハードコード起動は廃止された (§9.3 段階 3)。"""
    mgr, history_manager = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)  # running -> pending
    _create_conflicting_running_track(tm, persona)
    history_manager.reset_mock()  # 初回注入をクリア
    mgr.run_sea_user.reset_mock()  # 初回 hook 経由 Pulse 起動をクリア

    alert_observer_calls = []
    tm.add_alert_observer(
        lambda pid, tid, ctx: alert_observer_calls.append((pid, tid, ctx))
    )

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "話しかけた"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    assert len(alert_observer_calls) == 1
    # 熟慮経路: invoke_main_line ハードコード廃止 + activate されていないので応答なし
    assert invoked == []
    assert mgr.run_sea_user.call_count == 0
    # MetaLayer (= alert observer) が activate しないので Track は alert のまま
    # → running への遷移なし → hook 走らず、コンテキスト注入もなし
    assert tm.get(track.track_id).status == STATUS_ALERT
    history_manager.add_to_persona_only.assert_not_called()


def test_pending_track_with_metalayer_activating_starts_pulse_via_hook(
    handler, tm, persona, manager_stub
):
    """pending + running 衝突 → MetaLayer が activate して running になれば、
    on_track_activated hook 経由で Track コンテキスト注入 + main_line Pulse 起動が
    行われる (新仕様 §9.3 段階 3)。"""
    mgr, history_manager = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)
    _create_conflicting_running_track(tm, persona)
    history_manager.reset_mock()
    mgr.run_sea_user.reset_mock()

    # MetaLayer の代わりに、alert observer で activate を行う
    def mock_metalayer(pid, tid, ctx):
        tm.activate(tid)

    tm.add_alert_observer(mock_metalayer)

    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "話しかけた"},
        invoke_main_line=lambda *_a, **_kw: None,
    )
    # MetaLayer が activate したので running になっている
    assert tm.get(track.track_id).status == STATUS_RUNNING
    # → hook 経由で Track コンテキスト注入と main_line Pulse 起動が行われる
    history_manager.add_to_persona_only.assert_called_once()
    mgr.run_sea_user.assert_called_once()


def test_alert_observer_raise_does_not_propagate(handler, tm, persona, manager_stub):
    """alert observer が例外を出しても on_user_utterance は例外を伝播しない。
    (running 衝突あり = 熟慮経路。activate されないので Pulse 起動もしないが、
    エラーで落ちないことを確認。)"""
    mgr, _hm = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)
    _create_conflicting_running_track(tm, persona)
    mgr.run_sea_user.reset_mock()

    def bad_observer(*args):
        raise RuntimeError("boom")

    tm.add_alert_observer(bad_observer)

    invoked = []
    # 例外伝播しないこと
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "x"},
        invoke_main_line=lambda *_a, **_kw: invoked.append(True),
    )
    # activate されないので invoke_main_line も hook も呼ばれない
    assert invoked == []
    assert mgr.run_sea_user.call_count == 0


def test_alert_status_after_handler_pending_path(handler, tm, persona, manager_stub):
    """pending + running 衝突の経路を通った後、Track の status は alert になっている
    (MetaLayer 未起動時)。"""
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)
    _create_conflicting_running_track(tm, persona)
    assert tm.get(track.track_id).status == STATUS_PENDING

    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "x"},
        invoke_main_line=lambda *_a, **_kw: None,
    )
    assert tm.get(track.track_id).status == STATUS_ALERT


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
