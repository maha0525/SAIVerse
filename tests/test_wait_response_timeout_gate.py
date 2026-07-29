"""wait_response タイムアウトを「張るか否か」を決める 2 つのゲートのテスト。

1. ``_wait_response_timeout_provider`` の AUTONOMY_ENABLED ゲート
   2026-07-07: user_conversation は自律 OFF でもタイマーを予約する例外を追加
   (会話 episode の close [A1] がこのタイマーに乗っており、記録系は
   「認知不変・全ペルソナ」が原則 — life_concept_map.md §8)。
   social 等それ以外の wait_response Track は従来通り自律 ON のみ。
   post_conversation 判断の AUTONOMY_ENABLED ゲートは fire_judgment_point 側 (別テスト)。

2. ``_should_rearm_wait_response_timeout`` の「会話が開いているか」ゲート
   2026-07-29: 起動時の再確立が running Track だけを条件にしていたため、
   案 Y (2026-07-13) 以降 running のまま残る対ユーザー会話 Track に対して
   起動 N 分後に空のタイムアウトが発火していた。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from saiverse.saiverse_manager import SAIVerseManager


def _fake_manager(autonomy_enabled: bool) -> SimpleNamespace:
    persona = SimpleNamespace(autonomy_enabled=autonomy_enabled, sai_memory=None)
    return SimpleNamespace(
        _debug_manual_mode_personas=set(),
        personas={"p1": persona},
        # USER_CONV_TIMEOUT_MINUTES の読み出しは例外 → 既定値フォールバック
        # (provider 内の try/except が WARNING を出して既定 30 分を使う)
        SessionLocal=None,
        _DEFAULT_WAIT_RESPONSE_TIMEOUT_MINUTES=(
            SAIVerseManager._DEFAULT_WAIT_RESPONSE_TIMEOUT_MINUTES
        ),
    )


def _track(track_type: str) -> SimpleNamespace:
    return SimpleNamespace(persona_id="p1", track_id="t1", track_type=track_type)


def _call_provider(mgr, track):
    handler = SimpleNamespace(post_complete_behavior="wait_response")
    with patch(
        "sea.pulse_root_context.get_handler_for_track", return_value=handler
    ):
        return SAIVerseManager._wait_response_timeout_provider(mgr, track)


class WaitResponseTimeoutGateTest(unittest.TestCase):
    def test_idle_user_conversation_schedules(self):
        """自律 OFF でも user_conversation はタイマー対象 (episode close のため)。"""
        result = _call_provider(_fake_manager(False), _track("user_conversation"))
        self.assertIsNotNone(result)
        timeout_minutes, _last = result
        self.assertGreater(timeout_minutes, 0)

    def test_idle_social_skips(self):
        """自律 OFF の user_conversation 以外は従来通り予約しない。"""
        result = _call_provider(_fake_manager(False), _track("social"))
        self.assertIsNone(result)

    def test_active_social_schedules(self):
        """自律 ON なら従来通り全 wait_response が対象。"""
        result = _call_provider(_fake_manager(True), _track("social"))
        self.assertIsNotNone(result)

    def test_debug_manual_mode_still_skips(self):
        """完全手動モードは user_conversation でも予約しない (既存仕様維持)。"""
        mgr = _fake_manager(True)
        mgr._debug_manual_mode_personas = {"p1"}
        result = _call_provider(mgr, _track("user_conversation"))
        self.assertIsNone(result)


def _rearm_manager(running_track, *, get_running_raises: bool = False):
    """_should_rearm_wait_response_timeout 用の最小 manager。"""
    def _get_running(_persona_id):
        if get_running_raises:
            raise RuntimeError("db down")
        return running_track

    return SimpleNamespace(
        track_manager=SimpleNamespace(get_running=_get_running),
        SessionLocal=None,
    )


def _call_rearm(mgr):
    return SAIVerseManager._should_rearm_wait_response_timeout(mgr, "p1")


class ShouldRearmWaitResponseTimeoutTest(unittest.TestCase):
    """起動時のタイマー再確立ゲート (対ユーザー会話は「会話が開いている」が条件)。"""

    def test_open_conversation_rearms(self):
        """会話の出来事が開いている = 再起動を跨いで会話中 → 張り直す。"""
        mgr = _rearm_manager(_track("user_conversation"))
        with patch(
            "saiverse.episodes.get_open_episode", return_value={"episode_ref": "episode:9"}
        ):
            self.assertTrue(_call_rearm(mgr))

    def test_closed_conversation_does_not_rearm(self):
        """回帰 (2026-07-29): running のままでも会話が閉じていれば張らない。

        これを張ってしまうと起動 N 分後に wait_response が発火し、何日も前に
        終わった会話に対して post_conversation 判断が空撃ちされる。
        """
        mgr = _rearm_manager(_track("user_conversation"))
        with patch("saiverse.episodes.get_open_episode", return_value=None):
            self.assertFalse(_call_rearm(mgr))

    def test_non_conversation_track_rearms_without_episode(self):
        """social 等は会話の出来事を持たないので従来通り張る。"""
        mgr = _rearm_manager(_track("social"))
        with patch("saiverse.episodes.get_open_episode", return_value=None) as spy:
            self.assertTrue(_call_rearm(mgr))
        spy.assert_not_called()

    def test_no_running_track_does_not_rearm(self):
        """running Track が無ければ張らない。"""
        self.assertFalse(_call_rearm(_rearm_manager(None)))

    def test_episode_lookup_failure_is_undecidable(self):
        """出来事を読めないときは None (判定不能)。False = 張らなくてよい とは別物。"""
        mgr = _rearm_manager(_track("user_conversation"))
        with patch("saiverse.episodes.get_open_episode", side_effect=RuntimeError("boom")):
            self.assertIsNone(_call_rearm(mgr))

    def test_running_track_lookup_failure_is_undecidable(self):
        """running Track を読めないときも判定不能。"""
        self.assertIsNone(_call_rearm(_rearm_manager(None, get_running_raises=True)))


def _load_manager(decisions):
    """_rearm_wait_response_timeout_on_load 用 manager。

    ``decisions`` は判定の戻り値を順に返すリスト (None=判定不能)。予約された
    再試行と ensure_wait_response_timeout の呼び出しを記録する。
    """
    state = {"ensure_calls": [], "scheduled": [], "decisions": list(decisions)}

    def _ensure(persona_id, *, only_if_absent=False):
        state["ensure_calls"].append((persona_id, only_if_absent))

    def _schedule(fire_at, callback, key):
        state["scheduled"].append({"fire_at": fire_at, "callback": callback, "key": key})

    mgr = SimpleNamespace(
        track_manager=SimpleNamespace(ensure_wait_response_timeout=_ensure),
        event_scheduler=SimpleNamespace(schedule=_schedule),
        _REARM_RETRY_DELAYS_SEC=SAIVerseManager._REARM_RETRY_DELAYS_SEC,
        _schedule_rearm_retry=None,
        _rearm_wait_response_timeout_on_load=None,
        _should_rearm_wait_response_timeout=lambda _pid: state["decisions"].pop(0),
    )
    # 未束縛メソッドを this manager に束ねる (SAIVerseManager を構築せずに検証する)
    mgr._schedule_rearm_retry = (
        lambda pid, attempt: SAIVerseManager._schedule_rearm_retry(mgr, pid, attempt)
    )
    mgr._rearm_wait_response_timeout_on_load = (
        lambda pid, attempt=0: SAIVerseManager._rearm_wait_response_timeout_on_load(
            mgr, pid, attempt=attempt
        )
    )
    return mgr, state


class RearmRetryTest(unittest.TestCase):
    """判定不能時の読み取り再試行 (2026-07-29, Codex 指摘②)。

    「張らない」で終わらせると、正当に開いている会話がタイマーを失ったまま
    永久に閉じなくなりうる。当初あてにしていた「次のユーザー発話で張り直される」は
    別 Track が running のとき alert 経路に入るため常には成立しない。
    """

    def test_decidable_true_arms_without_retry(self):
        mgr, state = _load_manager([True])
        mgr._rearm_wait_response_timeout_on_load("p1")
        self.assertEqual(state["ensure_calls"], [("p1", True)])
        self.assertEqual(state["scheduled"], [])

    def test_rearm_never_overwrites_an_existing_reservation(self):
        """復旧は必ず only_if_absent=True で呼ぶ (生きている予約を置き換えない)。

        上書きしてしまうと、待っている間に通常経路が張った予約を潰し、下流が
        base_time を now へ丸めるぶん会話終了の期限が後退する。
        「上書きしないこと」自体の原子性は EventScheduler 側の
        SchedulerScheduleIfAbsentTest が実物で担保する。
        """
        mgr, state = _load_manager([None, True])
        mgr._rearm_wait_response_timeout_on_load("p1")
        state["scheduled"][0]["callback"]()  # 遅れて再試行が発火
        self.assertEqual(state["ensure_calls"], [("p1", True)])

    def test_decidable_false_neither_arms_nor_retries(self):
        mgr, state = _load_manager([False])
        mgr._rearm_wait_response_timeout_on_load("p1")
        self.assertEqual(state["ensure_calls"], [])
        self.assertEqual(state["scheduled"], [])

    def test_undecidable_schedules_read_only_retry(self):
        """判定不能ならタイマーは張らず、読み取りの再試行だけを予約する。"""
        mgr, state = _load_manager([None])
        mgr._rearm_wait_response_timeout_on_load("p1")
        self.assertEqual(state["ensure_calls"], [])
        self.assertEqual(len(state["scheduled"]), 1)
        self.assertEqual(state["scheduled"][0]["key"], "wait_response_rearm_retry:p1")

    def test_retry_arms_once_readable(self):
        """再試行で読めたらタイマーが張られる。"""
        mgr, state = _load_manager([None, True])
        mgr._rearm_wait_response_timeout_on_load("p1")
        state["scheduled"][0]["callback"]()  # 予約された再試行を発火
        self.assertEqual(state["ensure_calls"], [("p1", True)])

    def test_retries_are_bounded(self):
        """再試行は上限で打ち切る (無限に予約し続けない)。"""
        attempts = len(SAIVerseManager._REARM_RETRY_DELAYS_SEC)
        mgr, state = _load_manager([None] * (attempts + 1))
        mgr._rearm_wait_response_timeout_on_load("p1")
        for _ in range(attempts):
            state["scheduled"][-1]["callback"]()
        self.assertEqual(len(state["scheduled"]), attempts)
        self.assertEqual(state["ensure_calls"], [])


if __name__ == "__main__":
    unittest.main()
