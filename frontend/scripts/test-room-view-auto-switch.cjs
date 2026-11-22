// ログとルームビューの切り替えの決まり (lib/roomViewAutoSwitch.ts) を、実物の関数で確かめる。
// docs/intent/screen_avatar.md 設計 1。サーバー・ブラウザ・ペルソナ・LLM には触らない。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/lib/roomViewAutoSwitch.ts'), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const mod = { exports: {} };
new Function('require', 'module', 'exports', compiled)(require, mod, mod.exports);
const {
    initialRoomViewAutoSwitchState,
    nextRoomViewAutoSwitchState: next,
    showSpeakingIndicator,
    isUtteranceActive,
    RETURN_DELAY_AFTER_AUDIO_MS,
    RETURN_DELAY_AFTER_TEXT_MS,
} = mod.exports;

assert.equal(RETURN_DELAY_AFTER_AUDIO_MS, 400);
assert.equal(RETURN_DELAY_AFTER_TEXT_MS, 1500);

const signal = (fields = {}) => ({
    type: 'signal', enabled: true, speaking: false, audioPlaying: false, userBusy: false, ...fields,
});
const fire = (s) => next(s, { type: 'returnTimerFired', token: s.pendingToken });
// overlayMode に入って、誰も喋っていない状態
const idle = () => next(initialRoomViewAutoSwitchState(), signal());

// 最初は 'log'、印も出ない
{
    const s = idle();
    assert.equal(s.view, 'log');
    assert.equal(showSpeakingIndicator(s), false);
    assert.equal(isUtteranceActive(s), false);
}

// 決まり 1: 喋り出したら自動で 'room'。印は出ない。
{
    const s = next(idle(), signal({ speaking: true }));
    assert.equal(s.view, 'room');
    assert.equal(s.roomBy, 'auto');
    assert.equal(showSpeakingIndicator(s), false);
}

// 決まり 1・2: 入力中は切り替わらず、印が出る。印を押すと 'room' になる。
{
    let s = next(idle(), signal({ speaking: true, userBusy: true }));
    assert.equal(s.view, 'log', 'typing keeps the log');
    assert.equal(showSpeakingIndicator(s), true, 'indicator shows while typing');
    s = next(s, { type: 'pressIndicator' });
    assert.equal(s.view, 'room');
    assert.equal(showSpeakingIndicator(s), false);
    // 印で切り替えた場合も、発話が終われば戻る (決まり 3 と同じ)
    s = next(s, signal({ speaking: false }));
    assert.equal(s.pendingReturnMs, RETURN_DELAY_AFTER_TEXT_MS);
    s = fire(s);
    assert.equal(s.view, 'log', 'indicator-started room returns after the utterance');
}

// 決まり 1・2: 遡って読んでいる間も切り替わらず、印が出る (userBusy に同じく入る)。
// 喋っている途中で一番下まで戻っても、その発話の始まりで決めたことは変わらない。
{
    let s = next(idle(), signal({ speaking: true, userBusy: true }));
    assert.equal(s.view, 'log', 'scrolled-back reader keeps the log');
    assert.equal(showSpeakingIndicator(s), true);
    s = next(s, signal({ speaking: true, userBusy: false }));
    assert.equal(s.view, 'log', 'no switch mid-utterance when the reader returns to the bottom');
    assert.equal(showSpeakingIndicator(s), true);
    // 発話が終われば印も消える
    s = next(s, signal({ speaking: false }));
    assert.equal(showSpeakingIndicator(s), false);
}

// 印は 'log' で喋っている間しか押せない
{
    const s = idle();
    assert.equal(next(s, { type: 'pressIndicator' }), s);
}

// 決まり 3: 文字だけの発話は 1500ms 待ってから戻す。
{
    let s = next(idle(), signal({ speaking: true }));
    s = next(s, signal({ speaking: false }));
    assert.equal(s.view, 'room', 'still room while waiting');
    assert.equal(s.pendingReturnMs, RETURN_DELAY_AFTER_TEXT_MS);
    assert.equal(isUtteranceActive(s), true, 'waiting counts as the utterance');
    // 古いタイマーの知らせは捨てる
    assert.equal(next(s, { type: 'returnTimerFired', token: s.pendingToken - 1 }).view, 'room');
    s = fire(s);
    assert.equal(s.view, 'log');
    assert.equal(s.pendingReturnMs, null);
    assert.equal(isUtteranceActive(s), false);
}

// 決まり 3: 発話の間に読み上げの音声が鳴っていたら 400ms。
{
    let s = next(idle(), signal({ speaking: true }));
    s = next(s, signal({ speaking: true, audioPlaying: true }));
    s = next(s, signal({ speaking: true, audioPlaying: false }));
    s = next(s, signal({ speaking: false }));
    assert.equal(s.pendingReturnMs, RETURN_DELAY_AFTER_AUDIO_MS, 'audio during the utterance picks the short wait');
    s = fire(s);
    assert.equal(s.view, 'log');
    // 次の発話は音声の記録を持ち越さない
    s = next(s, signal({ speaking: true }));
    s = next(s, signal({ speaking: false }));
    assert.equal(s.pendingReturnMs, RETURN_DELAY_AFTER_TEXT_MS);
}

// 決まり 3: 待っている間に次の発話が始まったら戻らない。
{
    let s = next(idle(), signal({ speaking: true }));
    s = next(s, signal({ speaking: false }));
    const staleToken = s.pendingToken;
    s = next(s, signal({ speaking: true, audioPlaying: true }));
    assert.equal(s.view, 'room');
    assert.equal(s.pendingReturnMs, null, 'wait cancelled');
    s = next(s, { type: 'returnTimerFired', token: staleToken });
    assert.equal(s.view, 'room', 'a timer from the cancelled wait does nothing');
    s = next(s, signal({ speaking: false }));
    assert.equal(s.pendingReturnMs, RETURN_DELAY_AFTER_AUDIO_MS);
    assert.equal(fire(s).view, 'log');
}

// 決まり 4: ボタンで 'room' にした場合は、発話が終わっても戻さない。
{
    let s = next(idle(), { type: 'toggle' });
    assert.equal(s.view, 'room');
    assert.equal(s.roomBy, 'manual');
    s = next(s, signal({ speaking: true }));
    assert.equal(s.view, 'room');
    s = next(s, signal({ speaking: false }));
    s = fire(s);
    assert.equal(s.view, 'room', 'manual room stays after the utterance');
    s = next(s, { type: 'toggle' });
    assert.equal(s.view, 'log');
}

// 決まり 4: 手動でログへ戻した発話の間は切り替わらない (印は出す)。
{
    let s = next(idle(), signal({ speaking: true }));
    assert.equal(s.view, 'room');
    s = next(s, { type: 'toggle' });
    assert.equal(s.view, 'log');
    assert.equal(showSpeakingIndicator(s), true, 'indicator shows after choosing the log');
    // 文字が終わって、待っている間に音声が始まっても同じ発話の続き — 切り替えない
    s = next(s, signal({ speaking: false }));
    s = next(s, signal({ speaking: true, audioPlaying: true }));
    assert.equal(s.view, 'log', 'no switch while the same utterance continues');
    assert.equal(showSpeakingIndicator(s), true);
    // 発話が終わって待ちが過ぎれば、次の発話はまた自動で切り替わる
    s = next(s, signal({ speaking: false }));
    s = fire(s);
    s = next(s, signal({ speaking: true }));
    assert.equal(s.view, 'room', 'the next utterance switches again');
}

// 決まり 5: 見ている部屋が変わったら 'log' に戻す。
{
    let s = next(idle(), signal({ speaking: true }));
    s = next(s, { type: 'reset' });
    assert.equal(s.view, 'log');
    assert.equal(s.speaking, true, 'reset keeps the current speaking value');
    s = next(idle(), { type: 'toggle' });
    s = next(s, { type: 'reset' });
    assert.equal(s.view, 'log', 'manual room also returns on a room change');
}

// 決まり 5: overlayMode でなくなったら 'log' に戻し、その間は何も切り替えない。
{
    let s = next(idle(), signal({ speaking: true }));
    assert.equal(s.view, 'room');
    s = next(s, signal({ enabled: false, speaking: true }));
    assert.equal(s.view, 'log');
    assert.equal(s.enabled, false);
    assert.equal(showSpeakingIndicator(s), false, 'no indicator outside overlayMode');
    s = next(s, signal({ enabled: false, speaking: false }));
    s = next(s, signal({ enabled: false, speaking: true }));
    assert.equal(s.view, 'log', 'speaking outside overlayMode never switches');
    s = next(s, { type: 'toggle' });
    assert.equal(s.view, 'log', 'the toggle does nothing outside overlayMode');
    // overlayMode に入った瞬間に続いている発話では自動で切り替えない (印だけ)
    s = next(s, signal({ enabled: true, speaking: true }));
    assert.equal(s.view, 'log');
    assert.equal(showSpeakingIndicator(s), true);
}

// 過去のログを遡って読んでいる最中か: 利用者の操作によるスクロールでだけ立てる。
{
    const {
        SCROLLED_BACK_THRESHOLD_PX,
        USER_SCROLL_INPUT_WINDOW_MS,
        USER_SCROLL_KEYS,
        isUserScrollInputActive,
        nextScrolledBack,
    } = mod.exports;
    assert.equal(SCROLLED_BACK_THRESHOLD_PX, 200);
    assert.equal(USER_SCROLL_INPUT_WINDOW_MS, 1000);
    for (const key of ['PageUp', 'PageDown', 'ArrowUp', 'ArrowDown', 'Home', 'End', ' ']) {
        assert.ok(USER_SCROLL_KEYS.has(key), `${JSON.stringify(key)} counts as a scroll key`);
    }
    assert.equal(USER_SCROLL_KEYS.has('a'), false);

    // 利用者の操作: ポインタが押されている間は続いている、操作から 1 秒以内まで
    assert.equal(isUserScrollInputActive({ pointerDown: false, lastInputAt: null, now: 5000 }), false, 'no input yet');
    assert.equal(isUserScrollInputActive({ pointerDown: true, lastInputAt: null, now: 5000 }), true, 'scrollbar drag');
    assert.equal(isUserScrollInputActive({ pointerDown: false, lastInputAt: 4500, now: 5000 }), true);
    assert.equal(isUserScrollInputActive({ pointerDown: false, lastInputAt: 4000, now: 5000 }), true, 'exactly 1s is still the input');
    assert.equal(isUserScrollInputActive({ pointerDown: false, lastInputAt: 3999, now: 5000 }), false, 'older input does not count');

    // 一番下から 200px 以内なら、誰が起こしたスクロールでも false
    assert.equal(nextScrolledBack(true, 200, false), false, 'programmatic scroll to the bottom clears it');
    assert.equal(nextScrolledBack(true, 0, true), false);
    assert.equal(nextScrolledBack(false, 150, true), false);
    // 200px より上: 利用者の操作なら true
    assert.equal(nextScrolledBack(false, 201, true), true, 'the reader scrolled back');
    // 200px より上でもプログラムが起こしたスクロール (自動スクロールの途中・高さが伸びた直後) では変えない
    assert.equal(nextScrolledBack(false, 800, false), false, 'auto-scroll does not mark reading back');
    assert.equal(nextScrolledBack(true, 800, false), true, 'auto-scroll does not clear reading back above the bottom');

    // 起きた不具合の再現: 何も操作していない間に、メッセージが届いて高さが伸び、
    // 自動スクロールの途中で一番下から離れても、遡っていることにならない。
    let scrolledBack = false;
    const noInput = isUserScrollInputActive({ pointerDown: false, lastInputAt: null, now: 10_000 });
    for (const distance of [900, 600, 300, 120, 0]) {
        scrolledBack = nextScrolledBack(scrolledBack, distance, noInput);
        assert.equal(scrolledBack, false, `auto-scroll at ${distance}px from the bottom`);
    }
    // 利用者が wheel で遡り、その後の自動スクロール (操作から 1 秒より後) で
    // 200px より上に留まる間は、遡っているまま。一番下まで戻れば解ける。
    scrolledBack = nextScrolledBack(false, 500, isUserScrollInputActive({ pointerDown: false, lastInputAt: 20_000, now: 20_100 }));
    assert.equal(scrolledBack, true);
    scrolledBack = nextScrolledBack(scrolledBack, 700, isUserScrollInputActive({ pointerDown: false, lastInputAt: 20_000, now: 25_000 }));
    assert.equal(scrolledBack, true);
    scrolledBack = nextScrolledBack(scrolledBack, 10, isUserScrollInputActive({ pointerDown: false, lastInputAt: 20_000, now: 26_000 }));
    assert.equal(scrolledBack, false);
}

console.log('Room view auto switch: switches on speaking, stays on the log while typing or reading back (indicator instead), returns after 400ms/1500ms, keeps manual choices, resets on room or width change, and marks reading back only on the reader\'s own scrolls.');
