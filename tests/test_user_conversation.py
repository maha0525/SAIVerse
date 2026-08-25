"""saiverse/user_conversation.py — Track も出来事も経由しない会話経路のテスト。

芯 (autonomous_behavior_v3.md §7 / track_retirement.md §2 住人 2): 会話の実体は
**メモリ内の会話状態 + main_line 起動 + 沈黙タイマー**の三つ。Track はどこにも
登場せず、束 6c (2026-08-22) からは出来事 (Episode) の行も登場しない。始まりと
終わりは**どこにも記録しない** (2026-08-23 裁定。会話に区切りは保存しない =
docs/intent/episode.md の不変条件)。

覆う振る舞い:
- 会話が開いていれば直接メインライン起動 + 沈黙タイマーの張り直し
- 会話が閉じていれば会話開始 (状態を立てる → main_line → タイマー)
- 会話の開始でも終了でも Building ログに何も書かれない
- 別の活動中の仲裁経路 (v0.3 では供給源ゼロ。routing だけ回帰で固定する)
- 沈黙タイマーの対象外判定 (デバッグ完全手動モード / タイムアウト 0 以下)
- タイムアウト発火で会話状態が落ち、予約も解除される
- 再起動 (状態が空) では張り直す待ちが構造上存在しない

2026-08-21 Codex レビューで塞いだ穴のうち、器の差し替えを越えて生きているもの:
- 沈黙タイマーが仮想クロックで刻まれる (実時刻ではない)
- 取って代わられた予約の callback が新しい会話を閉じない (世代トークン)
- 同時発話で会話と Pulse が二重に作られない (ペルソナ単位のロック)
- 副作用の後の失敗を dispatcher が直接応答で肩代わりしない (二重応答の封じ)
- 新規会話の初回発話にも Pulse 起動オプションが届く

⚠ 旧「会話の出来事を開けなかったら応答しない」(Codex 指摘 6) の回帰は対象消滅した。
あの厳しさは出来事の行が「いま会話中か」の**正典**だったことに由来する。正典は
メモリ内状態だけが持ち、会話の開設に DB 書き込みは絡まなくなった。
"""
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User
from saiverse import clock, user_conversation as uc
from saiverse.event_scheduler import EventScheduler
from saiverse.pulse_dispatcher import PulseDispatcher

PERSONA_ID = "alice"
USER_ID = "1"


@pytest.fixture
def session_factory():
    # StaticPool + check_same_thread=False: 同時発話の再現テストが別スレッドから
    # 同じ in-memory DB を引く (既定の SingletonThreadPool だとスレッドごとに
    # 空の DB が生まれてしまう)。
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.commit()
    finally:
        db.close()
    yield Session
    engine.dispose()


@pytest.fixture
def manager(session_factory):
    persona = MagicMock()
    persona.persona_id = PERSONA_ID
    persona.current_building_id = "test_building"
    mgr = SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        event_scheduler=EventScheduler(),  # start() しない (発火は手で回す)
        run_sea_user=MagicMock(return_value=[]),
        add_building_event=MagicMock(return_value={}),
        occupants={"test_building": [PERSONA_ID]},
        id_to_name_map={PERSONA_ID: "Alice"},
        _active_sse_callbacks={},
        _debug_manual_mode_personas=set(),
        _autonomy_managers={},
        user_id=1,
    )
    return mgr


def _open_conversation(mgr):
    """いま開いている会話 (無ければ None)。"""
    return uc.get_open_conversation(mgr, PERSONA_ID)


def _start_conversation_state(mgr):
    """本番の入口を通さずに会話状態だけ立てる (前提条件のセットアップ用)。"""
    return uc._set_open_conversation(
        mgr, PERSONA_ID, building_id="test_building", participants=[PERSONA_ID],
    )


def _building_log_writes(mgr):
    """会話の開始 / 終了が Building ログへ書いたメッセージ (常に空であるべき)。

    会話に区切りは保存しない (docs/intent/episode.md の不変条件) ので、開始でも
    終了でも建物ログには一行も増えない。増やすと ``get_building_messages`` の
    取り込みがそれをペルソナの memory.db へ写し、文脈が汚れる (2026-08-23)。
    """
    return [call.args[1] for call in mgr.add_building_event.call_args_list]


@pytest.fixture(autouse=True)
def _clean_state():
    """どのテストも実クロック + 空の会話状態で始まる (持ち越さない)。"""
    clock.disable_virtual()
    uc._FALLBACK_STATE.clear()
    uc._FALLBACK_RESERVATIONS.clear()
    yield
    clock.disable_virtual()
    uc._FALLBACK_STATE.clear()
    uc._FALLBACK_RESERVATIONS.clear()


def _armed(mgr):
    entry = mgr.event_scheduler._entries_by_key.get(uc._timeout_key(PERSONA_ID))
    return entry is not None and not entry.cancelled


def _reservation(mgr):
    return mgr.event_scheduler._entries_by_key.get(uc._timeout_key(PERSONA_ID))


def _pretend_busy(monkeypatch, busy):
    """「別の活動中」を差し込む。

    v0.3 では :func:`uc._get_open_non_conversation_episode` が常に None を返す
    (出来事の書き手が全滅したため — v3 §7)。仲裁経路そのものは v0.4 のティック
    設計が作り直す口として残っているので、routing の正しさだけをここで固定する。
    """
    monkeypatch.setattr(
        uc, "_get_open_non_conversation_episode", lambda *_a, **_kw: busy,
    )


# ---------------------------------------------------------------------------
# on_user_utterance: 経路の分岐
# ---------------------------------------------------------------------------


def test_open_conversation_answers_directly_and_rearms_the_timeout(manager):
    """会話が開いていれば直接メインライン起動。応答後にタイマーを張り直す。"""
    _start_conversation_state(manager)
    invoke = MagicMock()

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "ただいま"}, invoke,
    )

    invoke.assert_called_once_with()
    # 会話開始経路 (空入力の main_line) は通らない
    manager.run_sea_user.assert_not_called()
    assert _armed(manager) is True
    # 会話は開き直されないし、建物ログにも何も書かれない
    assert _building_log_writes(manager) == []


def test_no_conversation_and_no_activity_starts_the_conversation(manager):
    """会話が閉じていて別の活動もなければ、判断を経ずに会話を開始する。"""
    invoke = MagicMock()

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "こんにちは"}, invoke,
    )

    # 直接応答経路は通らず、会話開始 (空入力 + auto_ingest) が走る
    invoke.assert_not_called()
    manager.run_sea_user.assert_called_once()
    args, kwargs = manager.run_sea_user.call_args
    assert args[1] == "test_building"
    assert args[2] == ""
    assert "origin_track_id" not in kwargs

    conv = _open_conversation(manager)
    assert conv is not None
    assert conv["building_id"] == "test_building"
    assert conv["participants"] == [PERSONA_ID, USER_ID]
    assert _armed(manager) is True


def test_the_start_and_the_end_write_nothing_to_the_building_log(manager):
    """会話の開始でも終了でも、建物ログには一行も書かれない。

    会話に終わりの合図は実在しないので、実在しない区切りを記録に書くとどう置いても
    捏造になる (docs/intent/episode.md の不変条件)。実害の形も具体的だった: 機構名義
    (role='host') の行は ``get_building_messages`` の取り込みが自動でペルソナの
    memory.db へ写すため、「会話が始まりました / 終わりました」が毎回ペルソナの文脈
    に載っていた (2026-08-23 にまはーが実機で発見し、機構ごと撤去)。
    """
    assert uc.start_conversation(manager, PERSONA_ID, USER_ID) is True
    assert _open_conversation(manager) is not None
    assert _building_log_writes(manager) == []

    uc.handle_conversation_timeout(manager, PERSONA_ID)

    assert _open_conversation(manager) is None
    assert _building_log_writes(manager) == []
    manager.add_building_event.assert_not_called()


def test_busy_with_another_activity_fires_the_on_event_judgment(manager, monkeypatch):
    """会話が閉じていて別の活動中なら on_event 判断点へ直結する。"""
    _pretend_busy(monkeypatch, {"kind": "work_session", "episode_ref": "episode:1"})
    seen = {}

    def _fake_conflict(mgr, persona_id, text, *, engage, user_id):
        seen.update({"text": text, "user_id": user_id})
        return "none:judged"

    monkeypatch.setattr(
        "saiverse.autonomy_wiring.handle_user_utterance_conflict", _fake_conflict,
    )
    invoke = MagicMock()

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "ちょっといい？"}, invoke,
    )

    assert seen == {"text": "ちょっといい？", "user_id": USER_ID}
    # 判断が engage_now を出さなければ応答しない
    invoke.assert_not_called()
    manager.run_sea_user.assert_not_called()
    assert _open_conversation(manager) is None


def test_busy_and_engage_now_starts_the_conversation(manager, monkeypatch):
    """仲裁が engage_now を選んだら、初回発火と同じ入口で会話が始まる。"""
    _pretend_busy(monkeypatch, {"kind": "work_session", "episode_ref": "episode:1"})

    def _fake_conflict(mgr, persona_id, text, *, engage, user_id):
        engage()
        return "judged:engage_now"

    monkeypatch.setattr(
        "saiverse.autonomy_wiring.handle_user_utterance_conflict", _fake_conflict,
    )

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "ちょっといい？"}, MagicMock(),
    )

    manager.run_sea_user.assert_called_once()
    assert _open_conversation(manager) is not None
    assert _armed(manager) is True


def test_the_busy_lookup_is_degraded_to_none_in_v03(manager):
    """v0.3 では「別の活動中」の供給源がゼロ — 仲裁は直接応答へ縮退する。

    旧 DB に閉じ損ねた open な出来事が残っていても仲裁が永久発火しないことを
    ここで固定する (読みごと止めた理由そのもの)。
    """
    assert uc._get_open_non_conversation_episode(manager, PERSONA_ID) is None


def test_main_line_failure_still_arms_the_timeout(manager):
    """応答が転んでも沈黙タイマーは張る (開いた会話が永久に閉じないのを防ぐ)。"""
    manager.run_sea_user.side_effect = RuntimeError("boom")

    with pytest.raises(uc.UserUtteranceError):
        uc.start_conversation(manager, PERSONA_ID, USER_ID)

    assert _armed(manager) is True
    assert _open_conversation(manager) is not None


def test_main_line_failure_is_reported_instead_of_swallowed(manager):
    """会話開始の失敗を握り潰さない — 飲むと画面に何も出ないまま応答が消える。

    分類は継続経路 (直接応答) と揃える。素の例外で上げるとディスパッチャの
    分類漏れ経路が invoke_main_line() を呼び直して二重発話になるため、必ず
    UserUtteranceError で包む。会話状態と Pulse は既に動いているので
    side_effects_done=True、肩代わりの再実行は禁止で fallback_safe=False。
    出自: docs/issues/user_utterance_path_failure_inventory.md (2026-08-24 実害)
    """
    manager.run_sea_user.side_effect = RuntimeError("boom")

    with pytest.raises(uc.UserUtteranceError) as excinfo:
        uc.start_conversation(manager, PERSONA_ID, USER_ID)

    assert excinfo.value.stage == "conversation_start"
    assert excinfo.value.side_effects_done is True
    assert excinfo.value.fallback_safe is False
    assert isinstance(excinfo.value.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# 沈黙タイマー
# ---------------------------------------------------------------------------


def test_debug_manual_mode_is_excluded_from_the_timeout(manager):
    manager._debug_manual_mode_personas.add(PERSONA_ID)
    assert uc.conversation_timeout_minutes(manager, PERSONA_ID) is None
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is False
    assert _armed(manager) is False


def test_zero_timeout_minutes_disables_the_timer(manager):
    db = manager.SessionLocal()
    try:
        db.query(AI).filter(AI.AIID == PERSONA_ID).update(
            {"USER_CONV_TIMEOUT_MINUTES": 0}
        )
        db.commit()
    finally:
        db.close()
    assert uc.conversation_timeout_minutes(manager, PERSONA_ID) is None
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is False


def test_unloaded_persona_is_excluded_from_the_timeout(manager):
    manager.personas.pop(PERSONA_ID)
    assert uc.conversation_timeout_minutes(manager, PERSONA_ID) is None


def test_only_if_absent_keeps_the_live_reservation(manager):
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is True
    first = manager.event_scheduler._entries_by_key[uc._timeout_key(PERSONA_ID)]
    assert uc.arm_conversation_timeout(manager, PERSONA_ID, only_if_absent=True) is False
    assert (
        manager.event_scheduler._entries_by_key[uc._timeout_key(PERSONA_ID)] is first
    )


def test_timeout_closes_the_conversation_and_releases_the_reservation(manager):
    uc.start_conversation(manager, PERSONA_ID, USER_ID)

    uc.handle_conversation_timeout(manager, PERSONA_ID)

    assert _open_conversation(manager) is None
    assert _armed(manager) is False
    assert _building_log_writes(manager) == []


def test_timeout_with_a_stale_expected_id_closes_nothing(manager):
    """検証時に見ていた会話が既に別のものへ入れ替わっていたら閉じない。"""
    uc.start_conversation(manager, PERSONA_ID, USER_ID)

    uc.handle_conversation_timeout(
        manager, PERSONA_ID, expected_conversation_id="conv:deadbeef",
    )

    assert _open_conversation(manager) is not None
    assert _building_log_writes(manager) == []


# ---------------------------------------------------------------------------
# 起動時の張り直し
# ---------------------------------------------------------------------------


def test_rearm_on_load_is_a_no_op_after_a_restart(manager):
    """再起動後は会話状態が空 = 「会話していない」— 張り直す待ちが存在しない。

    旧実装 (出来事の行) は閉じ損ねた行が再起動を跨いで「永遠に会話中」として
    残る事故を持っていた。器をメモリ内状態にしたことで供給源ごと消えた。
    """
    uc.rearm_conversation_timeout_on_load(manager, PERSONA_ID)
    assert _armed(manager) is False


def test_rearm_on_load_arms_while_a_conversation_is_live(manager):
    """同一プロセス内で会話が生きていれば、失われた予約は埋め直せる。"""
    _start_conversation_state(manager)
    uc.rearm_conversation_timeout_on_load(manager, PERSONA_ID)
    assert _armed(manager) is True


# ---------------------------------------------------------------------------
# 指摘 1: 沈黙タイマーは仮想クロックで刻む
# ---------------------------------------------------------------------------


def test_timeout_is_armed_and_fires_on_the_virtual_clock(manager):
    """仮想日付のシミュレーション中でも、期限は仮想時刻基準で来て会話が閉じる。

    実時刻 (``datetime.now()``) で刻むと、期限がシミュレーション終了後へ飛んで
    開いた会話が最後まで閉じない。
    """
    start = datetime(2026, 8, 21, 9, 0, 0)
    clock.enable_virtual(start)

    _start_conversation_state(manager)
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is True

    expected = start + timedelta(minutes=uc.DEFAULT_CONVERSATION_TIMEOUT_MINUTES)
    assert _reservation(manager).fire_at_ts == pytest.approx(expected.timestamp())

    # まだ期限前: 何も起きない
    clock.advance_to(start + timedelta(minutes=5))
    assert manager.event_scheduler.run_due(clock.now()) == 0
    assert _open_conversation(manager) is not None

    # 仮想時刻を期限の先へ進めると発火して会話が閉じる
    clock.advance_to(expected + timedelta(minutes=1))
    assert manager.event_scheduler.run_due(clock.now()) == 1

    assert _open_conversation(manager) is None
    assert _armed(manager) is False


# ---------------------------------------------------------------------------
# 指摘 2: 取って代わられた予約の callback は新しい会話を閉じない
# ---------------------------------------------------------------------------


def _detach_reservation(mgr):
    """EventScheduler が「期限到来で取り出した」状態を再現する。

    dispatch は heap と ``_entries_by_key`` の両方から外してから、ロックの外で
    callback を呼ぶ。その瞬間に居合わせた再予約は、古い callback を止められない。
    """
    key = uc._timeout_key(PERSONA_ID)
    entry = mgr.event_scheduler._entries_by_key.pop(key)
    mgr.event_scheduler._heap.remove(entry)
    return entry


def test_a_superseded_timeout_callback_does_not_close_the_new_conversation(manager):
    _start_conversation_state(manager)
    uc.arm_conversation_timeout(manager, PERSONA_ID)
    stale = _detach_reservation(manager)

    # 走り出した古い callback の隣で、ユーザー発話が同じ key へ再予約する
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is True
    fresh = _reservation(manager)

    stale.callback()

    # 古い世代は降りる: 会話は開いたまま、新しい予約も生きている
    assert _open_conversation(manager) is not None
    assert _reservation(manager) is fresh
    assert fresh.cancelled is False

    # 現行世代なら閉じる
    fresh.callback()
    assert _open_conversation(manager) is None


def test_rearming_the_same_conversation_also_supersedes_the_old_callback(manager):
    """同じ会話のままタイマーを延長した場合も、古い世代は無効になる。

    会話の識別子だけを照合値にすると、この並び (会話は同じ / 予約だけ新しい) を
    区別できず、延長したはずの会話が古い callback に閉じられる。
    """
    _start_conversation_state(manager)
    uc.arm_conversation_timeout(manager, PERSONA_ID)
    stale = _detach_reservation(manager)
    uc.arm_conversation_timeout(manager, PERSONA_ID)  # 同じ会話のまま延長

    stale.callback()

    assert _open_conversation(manager) is not None


def test_a_cancelled_reservation_cannot_fire_after_it_started(manager):
    _start_conversation_state(manager)
    uc.arm_conversation_timeout(manager, PERSONA_ID)
    stale = _detach_reservation(manager)

    uc.cancel_conversation_timeout(manager, PERSONA_ID)
    stale.callback()

    assert _open_conversation(manager) is not None


def test_each_reservation_gets_a_distinct_random_token(manager):
    """世代の識別は乱数 nonce — カウンタや時刻のような再到達可能な点を持たない。"""
    seen = set()
    for _ in range(5):
        uc.arm_conversation_timeout(manager, PERSONA_ID)
        seen.add(manager._conversation_timeout_reservations[PERSONA_ID]["token"])
    assert len(seen) == 5


# ---------------------------------------------------------------------------
# 2026-08-22 指摘 1: 予約は「どの会話を見張っているか」まで持つ
# ---------------------------------------------------------------------------


def test_a_timeout_armed_without_an_open_conversation_closes_nothing(manager):
    """会話が開いていないときに張られた予約は、閉じる相手を持たない。

    見張る会話を焼き付けずに発火させると、その予約は「いま開いている会話」を
    掴んで閉じてしまう — 自分とは無関係に始まった会話でも。
    """
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is True
    stale = _detach_reservation(manager)

    # 予約が走り出した後で、無関係な会話が始まる
    _start_conversation_state(manager)

    stale.callback()

    assert _open_conversation(manager) is not None


def test_cancelling_a_timeout_only_touches_that_conversation_s_reservation(manager):
    """予約の解除は「その会話のために張られた予約」だけに効く。

    会話の終了処理は状態を落としてから予約を解除する。その隙間に別の会話が
    始まっていたら、解除してよい相手はもう居ない (新しい会話は自分の予約を
    持っており、消されると永久に閉じない会話になる)。
    """
    _start_conversation_state(manager)
    uc.arm_conversation_timeout(manager, PERSONA_ID)
    conversation = _open_conversation(manager)["conversation_id"]

    # 別の会話の名前で解除しようとしても効かない
    assert uc.cancel_conversation_timeout(
        manager, PERSONA_ID, expected_conversation_id="conv:someone-else",
    ) is False
    assert _armed(manager) is True

    # 自分の会話の名前なら効く
    assert uc.cancel_conversation_timeout(
        manager, PERSONA_ID, expected_conversation_id=conversation,
    ) is True
    assert _armed(manager) is False


def test_cancelling_without_an_expected_conversation_is_unconditional(manager):
    """手動モード ON のように「このペルソナのタイマーを止める」意図は無条件。"""
    _start_conversation_state(manager)
    uc.arm_conversation_timeout(manager, PERSONA_ID)

    assert uc.cancel_conversation_timeout(manager, PERSONA_ID) is True
    assert _armed(manager) is False


def test_closing_a_stale_conversation_leaves_the_live_reservation_alone(manager):
    """会話 A の後片付けは、その後に始まった会話 B の予約を消さない。

    予約の解除は「閉じた会話の予約であるとき」だけ効く。無条件に解除すると、
    B は開いたままタイマーの無い会話として残り、永久に閉じない。
    """
    uc.start_conversation(manager, PERSONA_ID, USER_ID)  # 会話 A
    conversation_a = _open_conversation(manager)["conversation_id"]
    uc.handle_conversation_timeout(
        manager, PERSONA_ID, expected_conversation_id=conversation_a,
    )
    uc.start_conversation(manager, PERSONA_ID, USER_ID)  # 会話 B
    conversation_b = _open_conversation(manager)["conversation_id"]
    assert conversation_b != conversation_a

    # 会話 A のために張られた古い予約が、いまさら後片付けを走らせる
    uc.handle_conversation_timeout(
        manager, PERSONA_ID, expected_conversation_id=conversation_a,
    )

    assert _open_conversation(manager)["conversation_id"] == conversation_b
    assert _armed(manager) is True


def test_a_conversation_started_during_another_s_close_keeps_its_timer(
    manager, monkeypatch,
):
    """会話 A の終了処理の**最中**に会話 B が始まっても、B は無傷で残る。

    指摘 1 の現物。終了処理が開始処理と直列化されていないと、B は A の後片付けの
    途中で開き、A の予約解除が B の予約を巻き添えにする (B はタイマーの無い会話
    として残り、永久に閉じない)。

    A の終了処理を途中で止めるために、ロックの中の最後の一手 (予約の解除) を
    差し替えて待たせる。
    """
    uc.start_conversation(manager, PERSONA_ID, USER_ID)  # 会話 A
    conversation_a = _open_conversation(manager)["conversation_id"]
    stale = _detach_reservation(manager)

    closing = threading.Event()
    release = threading.Event()
    original_cancel = uc.cancel_conversation_timeout

    def _block_while_cancelling(*args, **kwargs):
        closing.set()
        release.wait(10)
        return original_cancel(*args, **kwargs)

    monkeypatch.setattr(uc, "cancel_conversation_timeout", _block_while_cancelling)

    started = {}

    def _start_b():
        started["opened"] = uc.start_conversation(manager, PERSONA_ID, USER_ID)

    closer = threading.Thread(target=stale.callback, name="close-a")
    starter = threading.Thread(target=_start_b, name="start-b")
    try:
        closer.start()
        assert closing.wait(10) is True

        starter.start()
        # 直列化されていれば starter は A の終了処理が終わるまで進めない
        starter.join(timeout=0.3)
        assert starter.is_alive() is True
    finally:
        # 途中で転んでもロックを握った closer を必ず解放する — 残すと後続の
        # テストが会話ロックで止まる。
        release.set()
        closer.join(timeout=10)
        if starter.ident is not None:
            starter.join(timeout=10)
    assert not closer.is_alive() and not starter.is_alive()

    assert started["opened"] is True
    conversation_b = _open_conversation(manager)
    assert conversation_b is not None
    assert conversation_b["conversation_id"] != conversation_a
    # B は自分の予約を持ったまま (A の後片付けに巻き込まれていない)
    assert _armed(manager) is True
    assert _building_log_writes(manager) == []


# ---------------------------------------------------------------------------
# 指摘 3: 同時発話でも会話と Pulse は一つだけ
# ---------------------------------------------------------------------------


def test_concurrent_start_opens_one_conversation_and_one_pulse(manager):
    barrier = threading.Barrier(2)
    started = threading.Event()

    def _slow_pulse(*_args, **_kwargs):
        started.set()
        # 勝った側がロックを握ったまま応答している間に、負けた側を再検査へ回す
        threading.Event().wait(0.2)
        return []

    manager.run_sea_user = MagicMock(side_effect=_slow_pulse)
    results = {}

    def _worker(name):
        barrier.wait()
        results[name] = uc.start_conversation(manager, PERSONA_ID, USER_ID)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)

    assert started.is_set()
    assert _open_conversation(manager) is not None
    assert manager.run_sea_user.call_count == 1
    assert _building_log_writes(manager) == []
    # 勝ちが 1、相乗りが 1
    assert sorted(results.values()) == [False, True]
    assert _armed(manager) is True


def test_start_conversation_rides_along_an_already_open_conversation(manager):
    existing = _start_conversation_state(manager)

    assert uc.start_conversation(manager, PERSONA_ID, USER_ID) is False

    manager.run_sea_user.assert_not_called()
    assert _open_conversation(manager) is existing
    # 相乗りでも会話は動いているので沈黙タイマーは張り直す
    assert _armed(manager) is True


# ---------------------------------------------------------------------------
# 指摘 5: 新規会話の初回発話にも Pulse 起動オプションが届く
# ---------------------------------------------------------------------------


def _pulse_options():
    return {
        "metadata": {"source": "chat"},
        "meta_playbook": "meta_user_custom",
        "args": {"topic": "weather"},
        "pre_spells": ["look_around"],
        "event_callback": MagicMock(name="sse_callback"),
    }


def _assert_options_forwarded(manager, options):
    args, kwargs = manager.run_sea_user.call_args
    assert args[1] == "test_building"
    assert args[2] == ""  # user_input は空 (auto_ingest が取り込む)
    assert kwargs["metadata"] == options["metadata"]
    assert kwargs["meta_playbook"] == options["meta_playbook"]
    assert kwargs["args"] == options["args"]
    assert kwargs["pre_spells"] == options["pre_spells"]
    assert kwargs["event_callback"] is options["event_callback"]


def test_first_utterance_of_a_new_conversation_keeps_the_pulse_options(manager):
    options = _pulse_options()

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "はじめまして"}, MagicMock(),
        pulse_options=options,
    )

    manager.run_sea_user.assert_called_once()
    _assert_options_forwarded(manager, options)


def test_the_arbitration_engage_closure_keeps_the_pulse_options(manager, monkeypatch):
    """仲裁が engage_now を選んだ経路でも、初回と同じオプションが届く。"""
    _pretend_busy(monkeypatch, {"kind": "work_session", "episode_ref": "episode:1"})

    def _fake_conflict(mgr, persona_id, text, *, engage, user_id):
        engage()
        return "judged:engage_now"

    monkeypatch.setattr(
        "saiverse.autonomy_wiring.handle_user_utterance_conflict", _fake_conflict,
    )
    options = _pulse_options()

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "ちょっといい？"}, MagicMock(),
        pulse_options=options,
    )

    manager.run_sea_user.assert_called_once()
    _assert_options_forwarded(manager, options)


def test_without_options_the_active_sse_callback_is_still_picked_up(manager):
    sse = MagicMock(name="active_sse")
    manager._active_sse_callbacks["test_building"] = sse

    uc.start_conversation(manager, PERSONA_ID, USER_ID)

    _, kwargs = manager.run_sea_user.call_args
    assert kwargs["event_callback"] is sse


def test_unknown_pulse_options_are_dropped(manager):
    uc.start_conversation(
        manager, PERSONA_ID, USER_ID,
        pulse_options={"origin_track_id": "track:1", "metadata": {"a": 1}},
    )

    _, kwargs = manager.run_sea_user.call_args
    assert "origin_track_id" not in kwargs
    assert kwargs["metadata"] == {"a": 1}


# ---------------------------------------------------------------------------
# 指摘 4: PulseDispatcher のフォールバック境界
# ---------------------------------------------------------------------------


def _dispatcher(manager):
    return PulseDispatcher(manager)


def _dispatch(manager, invoke, **kwargs):
    _dispatcher(manager).dispatch_user_utterance(
        persona_id=PERSONA_ID,
        user_id=USER_ID,
        event={"content": "やあ"},
        invoke_main_line=invoke,
        **kwargs,
    )


def test_dispatcher_falls_back_when_the_failure_precedes_any_side_effect(
    manager, monkeypatch
):
    def _boom(*_args, **_kwargs):
        raise uc.UserUtteranceError(
            "routing blew up",
            stage="lookup_activity",
            side_effects_done=False,
            fallback_safe=True,
        )

    monkeypatch.setattr(uc, "on_user_utterance", _boom)
    invoke = MagicMock()

    _dispatch(manager, invoke)

    invoke.assert_called_once_with()


def test_dispatcher_does_not_retry_after_side_effects(manager, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise uc.UserUtteranceError(
            "the response already ran",
            stage="direct_response",
            side_effects_done=True,
            fallback_safe=False,
        )

    monkeypatch.setattr(uc, "on_user_utterance", _boom)
    invoke = MagicMock()

    with pytest.raises(uc.UserUtteranceError):
        _dispatch(manager, invoke)

    # 呼び直さない (二重応答の封じ)。失敗そのものは上へ通す。
    invoke.assert_not_called()


def test_dispatcher_surfaces_the_original_error_after_side_effects(manager):
    """応答中の失敗は、元の例外のまま上へ通す。

    握り潰すと ``handle_user_input_stream`` の LLMError 変換に届かず、画面には
    何も出ないまま応答だけが消える。
    """
    boom = RuntimeError("the model refused")
    _start_conversation_state(manager)
    # 会話が開いているので直接応答経路 — 応答本体 (invoke_main_line) が転ぶ
    invoke = MagicMock(side_effect=boom)

    with pytest.raises(RuntimeError) as excinfo:
        _dispatch(manager, invoke)

    assert excinfo.value is boom
    invoke.assert_called_once_with()  # 1 回だけ (フォールバックで 2 回目を呼ばない)


def test_dispatcher_does_not_double_answer_when_arming_fails_after_the_pulse(
    manager, monkeypatch
):
    """指摘 4 の現物: 装填だけ転んでも、同じ発話をもう一度処理しない。"""
    def _boom_arm(*_args, **_kwargs):
        raise RuntimeError("scheduler is down")

    monkeypatch.setattr(uc, "arm_conversation_timeout", _boom_arm)
    invoke = MagicMock()

    _dispatch(manager, invoke)

    # 会話開始経路 (空入力の main_line) が 1 回だけ走り、直接応答は呼ばれない
    assert manager.run_sea_user.call_count == 1
    invoke.assert_not_called()
    assert _open_conversation(manager) is not None


def test_dispatcher_forwards_the_pulse_options(manager):
    options = _pulse_options()

    _dispatch(manager, MagicMock(), pulse_options=options)

    manager.run_sea_user.assert_called_once()
    _assert_options_forwarded(manager, options)


# ---------------------------------------------------------------------------
# 2026-08-22 掃討フェーズ 指摘 1: 応答の飛行中に古いタイマーが発火する並び
# ---------------------------------------------------------------------------
#
# 既存の世代テストは「張り直し → 古い callback が発火」という**安全な順序**しか
# 試していなかった。実機で踏むのは逆順で、沈黙の終わり際に来た発話への応答中に
# 前回の予約が発火する。会話 ID が変わらないため後片付けの照合をすり抜け、
# 応答後に張り直した予約まで巻き添えで取り消される。


def test_a_timer_firing_during_the_reply_does_not_close_the_live_conversation(manager):
    """沈黙の終わり際の発話: 応答中に前回の予約が発火しても会話は閉じない。

    再現する並び:
      1. 会話 A が開いていて、予約 T1 の期限が目前
      2. ユーザーが発話 → 継続経路が応答を起こす
      3. 応答の生成中に T1 が発火する (dispatch は取り出し済みなので止められない)

    ここで T1 が会話 A を閉じてしまうと、ユーザーがまさに話している最中に会話が
    切られ、次の発話が新しい会話になる。
    """
    _start_conversation_state(manager)
    uc.arm_conversation_timeout(manager, PERSONA_ID)
    stale = _detach_reservation(manager)

    fired: list[str] = []

    def _reply_while_the_old_timer_fires():
        # 応答の生成中に、取り出し済みの古い予約が期限を迎える
        stale.callback()
        fired.append("timer")

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "ただいま"},
        _reply_while_the_old_timer_fires,
    )

    assert fired == ["timer"], "古い予約が発火する並びを再現できていない"
    # 会話は開いたまま — 話している最中に閉じられていない
    assert _open_conversation(manager) is not None
    assert _building_log_writes(manager) == []
    # 応答後の張り直しが生きている (巻き添えで取り消されていない)
    assert _armed(manager) is True


def test_the_reply_path_arms_before_invoking_the_main_line(manager):
    """継続経路は応答を起こす**前**に張り直す (開始経路と同じ規律)。

    この一行が消えると上のテストが落ちる。順序そのものを固定して、
    「応答後だけで足りる」という読み違いで戻されるのを防ぐ。
    """
    _start_conversation_state(manager)
    uc.arm_conversation_timeout(manager, PERSONA_ID)
    before = _reservation(manager)

    seen: dict[str, object] = {}

    def _capture_reservation_at_reply_time():
        seen["at_reply"] = _reservation(manager)

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "ただいま"},
        _capture_reservation_at_reply_time,
    )

    # 応答が始まった時点で、既に別の予約へ入れ替わっている
    assert seen["at_reply"] is not None
    assert seen["at_reply"] is not before
