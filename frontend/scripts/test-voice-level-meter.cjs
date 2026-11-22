// 鳴っている声と音量を追う決まり (lib/voiceLevelMeter.ts) を、偽物の audio 要素・偽物の
// MediaStream・偽物の AudioContext で確かめる。docs/intent/screen_avatar.md 設計 3 の「口パク」。
// ブラウザ・サーバー・ペルソナ・LLM には触らない。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const file = path.join(__dirname, '../src/lib/voiceLevelMeter.ts');
const sourceText = fs.readFileSync(file, 'utf8');
const compiled = ts.transpileModule(sourceText, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, removeComments: true },
}).outputText;
// 声を鳴らす経路を変えないため、createMediaElementSource を使わず、destination へ繋がない
// (説明書きの中の言及は数えないよう、コメントを除いたコードで調べる)。
assert.ok(!/createMediaElementSource/.test(compiled), 'voiceLevelMeter must not call createMediaElementSource');
assert.ok(!/\bdestination\b/.test(compiled), 'voiceLevelMeter must not touch AudioContext.destination');
assert.ok(!/require\(["']three/.test(compiled) && !/require\(["']@pixiv\//.test(compiled),
    'voiceLevelMeter must not import three or @pixiv/three-vrm');

// モジュールの中の状態 (AudioContext・見張っている要素) を場面ごとに分けるため、毎回読み直す。
function loadModule() {
    const mod = { exports: {} };
    new Function('require', 'module', 'exports', compiled)(require, mod, mod.exports);
    return mod.exports;
}

/** 読み直したモジュールに、new MediaStream([track]) の代わりの偽物を入れる。
 * 作った stream は返す配列に溜まる。 */
function loadWithStreamFactory() {
    const m = loadModule();
    const made = [];
    m.setVoiceLevelMediaStreamFactory((tracks) => {
        const s = { tracks: [...tracks], getAudioTracks() { return s.tracks.slice(); } };
        made.push(s);
        return s;
    });
    return { m, made };
}

// console.warn を黙らせる (失敗の場面でわざと出させるため)。
const originalWarn = console.warn;
console.warn = () => {};

/** 偽物の audio 要素。src / volume / muted / currentTime / crossOrigin への代入と、
 * play() / pause() の呼び出しは、呼ばれたら失敗にする。 */
function fakeAudio({ captureStream } = {}) {
    const listeners = new Map();
    const forbid = (name) => ({
        get() { return undefined; },
        set() { throw new Error(`voiceLevelMeter must not assign audio.${name}`); },
        configurable: true,
    });
    const audio = {
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, new Set());
            listeners.get(type).add(fn);
        },
        removeEventListener(type, fn) {
            listeners.get(type)?.delete(fn);
        },
        dispatch(type) {
            for (const fn of Array.from(listeners.get(type) ?? [])) fn({ type });
        },
        listenerCount() {
            let n = 0;
            for (const set of listeners.values()) n += set.size;
            return n;
        },
        play() { throw new Error('voiceLevelMeter must not call audio.play()'); },
        pause() { throw new Error('voiceLevelMeter must not call audio.pause()'); },
    };
    for (const name of ['src', 'volume', 'muted', 'currentTime', 'crossOrigin']) {
        Object.defineProperty(audio, name, forbid(name));
    }
    if (captureStream) audio.captureStream = captureStream;
    return audio;
}

/** 偽物の MediaStreamTrack。stop() で readyState が ended になる。 */
function fakeTrack() {
    const track = {
        kind: 'audio',
        readyState: 'live',
        stopCalls: 0,
        stop() { track.stopCalls++; track.readyState = 'ended'; },
    };
    return track;
}

/** 偽物の captureStream の MediaStream。tracks は最初に持っている audio の track の数。 */
function fakeStream(tracks = 1) {
    const listeners = new Map();
    const stream = {
        tracks: Array.from({ length: tracks }, () => fakeTrack()),
        removed: [],
        getAudioTracks() { return stream.tracks.slice(); },
        getTracks() { return stream.tracks.slice(); },
        removeTrack(track) {
            const i = stream.tracks.indexOf(track);
            if (i >= 0) stream.tracks.splice(i, 1);
            stream.removed.push(track);
        },
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, new Set());
            listeners.get(type).add(fn);
        },
        removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
        listenerCount(type) { return listeners.get(type)?.size ?? 0; },
        /** ブラウザが src の差し替えで新しい track を足したときと同じく、track を足して addtrack を起こす。 */
        deliverTrack(track = fakeTrack()) {
            stream.tracks.push(track);
            for (const fn of Array.from(listeners.get('addtrack') ?? [])) fn({ type: 'addtrack', track });
            return track;
        },
    };
    return stream;
}

/** 偽物の AudioContext。analyser が返す時間領域の値は ctx.samples で決める。
 * destination に触ったら失敗にする。 */
function fakeContext({ state = 'running', resumeTo = 'running' } = {}) {
    const ctx = {
        state,
        samples: 0.5,
        sources: [],
        analysers: [],
        resumeCalls: 0,
        suspendCalls: 0,
        createMediaStreamSource(stream) {
            if (stream.getAudioTracks().length === 0) throw new Error('InvalidStateError: no audio track');
            const source = {
                stream,
                connectedTo: [],
                disconnected: false,
                connect(node) {
                    if (node === undefined || !ctx.analysers.includes(node)) {
                        throw new Error('source must connect only to the analyser');
                    }
                    source.connectedTo.push(node);
                },
                disconnect() { source.disconnected = true; },
            };
            ctx.sources.push(source);
            return source;
        },
        createAnalyser() {
            const analyser = {
                fftSize: 2048,
                connect() { throw new Error('analyser must not be connected anywhere'); },
                disconnect() {},
                getFloatTimeDomainData(buf) {
                    for (let i = 0; i < buf.length; i++) buf[i] = (i % 2 === 0 ? 1 : -1) * ctx.samples;
                },
            };
            ctx.analysers.push(analyser);
            return analyser;
        },
        resume() { ctx.resumeCalls++; ctx.state = resumeTo; return Promise.resolve(); },
        suspend() { ctx.suspendCalls++; ctx.state = 'suspended'; return Promise.resolve(); },
    };
    Object.defineProperty(ctx, 'destination', {
        get() { throw new Error('voiceLevelMeter must not touch destination'); },
    });
    return ctx;
}

const close = (a, b) => Math.abs(a - b) < 1e-6;

// ── captureStream の無い audio: messageId は出て、音量は null ──
{
    const { m } = loadWithStreamFactory();
    let created = 0;
    m.setVoiceLevelAudioContextFactory(() => { created++; return fakeContext(); });
    const audio = fakeAudio();
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    assert.equal(m.getVoicedMessageId(), null);
    assert.equal(m.getVoicedMessageIdServerSnapshot(), null);
    audio.dispatch('playing');
    assert.equal(m.getVoicedMessageId(), 'msg-1');
    assert.equal(m.getVoiceLevel(), null);
    assert.equal(created, 0, 'no AudioContext without captureStream');
}

// ── captureStream があれば、track 一本だけの stream から音量 (RMS) が読める。destination へは繋がない ──
{
    const { m, made } = loadWithStreamFactory();
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    const stream = fakeStream(1);
    const audio = fakeAudio({ captureStream: () => stream });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    audio.dispatch('playing');
    assert.equal(ctx.sources.length, 1);
    assert.equal(made.length, 1);
    assert.deepEqual(made[0].tracks, [stream.tracks[0]], 'the analysis stream holds only the chosen track');
    assert.equal(ctx.sources[0].stream, made[0], 'the source is created from the single-track stream');
    assert.deepEqual(ctx.sources[0].connectedTo, [ctx.analysers[0]]);
    assert.ok(close(m.getVoiceLevel(), 0.5), `rms ${m.getVoiceLevel()}`);
    ctx.samples = 0;
    assert.equal(m.getVoiceLevel(), 0, 'silence reads 0');
    ctx.samples = 3;
    assert.equal(m.getVoiceLevel(), 1, 'rms is clamped to 1');
    // AudioContext が running でなければ null
    ctx.state = 'suspended';
    assert.equal(m.getVoiceLevel(), null);
}

// ── MediaStream を作れない (factory が null を返す・例外を投げる) ときは、取った stream をそのまま使う ──
for (const factory of [() => null, () => { throw new Error('MediaStream failed'); }]) {
    const m = loadModule();
    m.setVoiceLevelMediaStreamFactory(factory);
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    const stream = fakeStream(1);
    const audio = fakeAudio({ captureStream: () => stream });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    assert.doesNotThrow(() => audio.dispatch('playing'));
    assert.equal(ctx.sources.length, 1);
    assert.equal(ctx.sources[0].stream, stream);
    assert.ok(close(m.getVoiceLevel(), 0.5));
}

// ── 同じ要素で playing が何度来ても、captureStream は一度だけ呼ぶ ──
{
    const { m } = loadWithStreamFactory();
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    let captures = 0;
    const stream = fakeStream(1);
    const audio = fakeAudio({ captureStream: () => { captures++; return stream; } });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    for (let i = 0; i < 5; i++) {
        audio.dispatch('playing');
        audio.dispatch('pause');
    }
    audio.dispatch('playing');
    assert.equal(captures, 1, 'captureStream is called only once per element');
    assert.equal(stream.listenerCount('addtrack'), 1, 'only one addtrack listener is attached');
    assert.ok(close(m.getVoiceLevel(), 0.5));
}

// ── captureStream が例外を投げても外へ出さず、messageId は出て、音量は null。次の playing でも呼び直さない ──
{
    const { m } = loadWithStreamFactory();
    m.setVoiceLevelAudioContextFactory(() => fakeContext());
    let captures = 0;
    const audio = fakeAudio({ captureStream: () => { captures++; throw new Error('captureStream failed'); } });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    assert.doesNotThrow(() => audio.dispatch('playing'));
    assert.equal(m.getVoicedMessageId(), 'msg-1');
    assert.equal(m.getVoiceLevel(), null);
    assert.doesNotThrow(() => audio.dispatch('pause'));
    assert.equal(m.getVoicedMessageId(), null);
    assert.doesNotThrow(() => audio.dispatch('playing'));
    assert.equal(captures, 1, 'captureStream is not called again after it threw');
    assert.equal(m.getVoicedMessageId(), 'msg-1');
    assert.equal(m.getVoiceLevel(), null);
}

// ── AudioContext の生成が例外を投げても、同じ ──
{
    const { m } = loadWithStreamFactory();
    let calls = 0;
    m.setVoiceLevelAudioContextFactory(() => { calls++; throw new Error('AudioContext failed'); });
    const audio = fakeAudio({ captureStream: () => fakeStream(1) });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    assert.doesNotThrow(() => audio.dispatch('playing'));
    assert.equal(m.getVoicedMessageId(), 'msg-1');
    assert.equal(m.getVoiceLevel(), null);
    // 作れないと分かったら、次に鳴っても作り直さない
    audio.dispatch('playing');
    assert.equal(calls, 1);
}

// ── getMessageId が例外を投げても外へ出さない (鳴っている声としては扱わない) ──
{
    const { m } = loadWithStreamFactory();
    m.setVoiceLevelAudioContextFactory(() => fakeContext());
    const audio = fakeAudio({ captureStream: () => fakeStream(1) });
    m.attachVoiceLevelMeter(audio, () => { throw new Error('boom'); });
    assert.doesNotThrow(() => audio.dispatch('playing'));
    assert.equal(m.getVoicedMessageId(), null);
}

// ── messageId の無い音声 (unlock 用の無音の WAV など) は覚えない。AudioContext も captureStream も使わない ──
{
    const { m } = loadWithStreamFactory();
    let created = 0;
    m.setVoiceLevelAudioContextFactory(() => { created++; return fakeContext(); });
    let current;
    let captures = 0;
    const audio = fakeAudio({ captureStream: () => { captures++; return fakeStream(1); } });
    m.attachVoiceLevelMeter(audio, () => current);
    for (const value of [undefined, null, '']) {
        current = value;
        audio.dispatch('playing');
        assert.equal(m.getVoicedMessageId(), null, `messageId ${String(value)} is not remembered`);
        assert.equal(m.getVoiceLevel(), null);
    }
    assert.equal(created, 0);
    assert.equal(captures, 0);
    current = 'msg-2';
    audio.dispatch('playing');
    assert.equal(m.getVoicedMessageId(), 'msg-2');
    assert.equal(captures, 1);
}

// ── src の差し替え (emptied → addtrack → playing): 前の track を止めて外し、新しい track だけで繋ぎ直す ──
{
    const { m, made } = loadWithStreamFactory();
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    let captures = 0;
    const stream = fakeStream(1);
    const audio = fakeAudio({ captureStream: () => { captures++; return stream; } });
    let current = 'msg-1';
    m.attachVoiceLevelMeter(audio, () => current);
    audio.dispatch('playing');
    const first = stream.tracks[0];
    current = 'msg-2';
    audio.dispatch('emptied');
    const second = stream.deliverTrack();
    // 実測と同じく、playing の時点では前の track も live のまま残っている
    assert.equal(first.readyState, 'live');
    assert.equal(ctx.sources.length, 1, 'addtrack after a stop does not connect');
    audio.dispatch('playing');
    assert.equal(captures, 1, 'captureStream is not called again on src change');
    assert.equal(first.stopCalls, 1, 'the previous track is stopped');
    assert.ok(stream.removed.includes(first), 'the previous track is removed from the stream');
    assert.deepEqual(stream.tracks, [second], 'only the new track stays in the stream');
    assert.equal(second.stopCalls, 0);
    assert.equal(ctx.sources.length, 2);
    assert.equal(ctx.sources[0].disconnected, true, 'the previous source is disconnected');
    assert.equal(ctx.sources[1].disconnected, false);
    assert.deepEqual(made[made.length - 1].tracks, [second], 'the new source reads only the new track');
    assert.equal(ctx.sources[1].stream, made[made.length - 1]);
    assert.equal(m.getVoicedMessageId(), 'msg-2');
    assert.ok(close(m.getVoiceLevel(), 0.5));
}

// ── addtrack の event に track が無くても、getAudioTracks の live の末尾を使う ──
{
    const { m, made } = loadWithStreamFactory();
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    const stream = fakeStream(3);
    stream.tracks[2].readyState = 'ended';
    const audio = fakeAudio({ captureStream: () => stream });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    const [, middle] = stream.tracks;
    audio.dispatch('playing');
    assert.deepEqual(made[0].tracks, [middle], 'the last live track is chosen');
    assert.deepEqual(stream.tracks, [middle]);
}

// ── playing の時点で live の track が無ければ繋がず、後から来た addtrack で繋ぐ ──
{
    const { m } = loadWithStreamFactory();
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    const stream = fakeStream(0);
    const audio = fakeAudio({ captureStream: () => stream });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    assert.doesNotThrow(() => audio.dispatch('playing'));
    assert.equal(m.getVoicedMessageId(), 'msg-1');
    assert.equal(m.getVoiceLevel(), null, 'no level before the audio track arrives');
    assert.equal(ctx.sources.length, 0);
    assert.equal(ctx.suspendCalls, 0, 'waiting for a track keeps the AudioContext running');
    stream.deliverTrack();
    assert.equal(ctx.sources.length, 1);
    assert.ok(close(m.getVoiceLevel(), 0.5));
    // 繋がった後に来た addtrack では繋ぎ直さない
    stream.deliverTrack();
    assert.equal(ctx.sources.length, 1);
}

// ── 止まった後に来た addtrack では繋がない ──
{
    const { m } = loadWithStreamFactory();
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    const stream = fakeStream(0);
    const audio = fakeAudio({ captureStream: () => stream });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    audio.dispatch('playing');
    audio.dispatch('pause');
    assert.equal(ctx.state, 'suspended', 'nothing is playing or waiting, so the AudioContext is suspended');
    stream.deliverTrack();
    assert.equal(ctx.sources.length, 0);
    assert.equal(m.getVoiceLevel(), null);
    // 次の playing で、その track で繋ぐ
    audio.dispatch('playing');
    assert.equal(ctx.sources.length, 1);
    assert.ok(close(m.getVoiceLevel(), 0.5));
}

// ── pause / ended / emptied / error で messageId が null に戻り、解析だけを外す ──
for (const type of ['pause', 'ended', 'emptied', 'error']) {
    const { m } = loadWithStreamFactory();
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    let captures = 0;
    const stream = fakeStream(1);
    const audio = fakeAudio({ captureStream: () => { captures++; return stream; } });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    let notified = 0;
    m.subscribeVoicePlayback(() => { notified++; });
    audio.dispatch('playing');
    assert.equal(notified, 1);
    audio.dispatch(type);
    assert.equal(m.getVoicedMessageId(), null, `${type} clears the voiced message`);
    assert.equal(m.getVoiceLevel(), null);
    assert.equal(ctx.sources[0].disconnected, true, `${type} disconnects the analysis`);
    assert.equal(stream.tracks[0].stopCalls, 0, `${type} does not stop the track`);
    assert.equal(stream.listenerCount('addtrack'), 1, `${type} keeps the stream`);
    assert.equal(notified, 2);
    // 鳴っている要素が無くなったら AudioContext を suspend する
    assert.equal(ctx.state, 'suspended', `${type} suspends the AudioContext`);
    audio.dispatch('playing');
    assert.equal(captures, 1, `${type} does not lead to another captureStream`);
}

// ── 二つの要素: 後から鳴ったほうが出て、それが止まると先のほうへ戻る ──
{
    const { m } = loadWithStreamFactory();
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    const a = fakeAudio({ captureStream: () => fakeStream(1) });
    const b = fakeAudio({ captureStream: () => fakeStream(1) });
    m.attachVoiceLevelMeter(a, () => 'msg-a');
    m.attachVoiceLevelMeter(b, () => 'msg-b');
    a.dispatch('playing');
    assert.equal(m.getVoicedMessageId(), 'msg-a');
    b.dispatch('playing');
    assert.equal(m.getVoicedMessageId(), 'msg-b');
    b.dispatch('ended');
    assert.equal(m.getVoicedMessageId(), 'msg-a');
    assert.equal(ctx.suspendCalls, 0, 'still playing, so the AudioContext keeps running');
    assert.ok(close(m.getVoiceLevel(), 0.5));
    a.dispatch('pause');
    assert.equal(m.getVoicedMessageId(), null);
    assert.equal(ctx.suspendCalls, 1);
    // 次に鳴ったとき resume する
    a.dispatch('playing');
    assert.equal(ctx.resumeCalls, 1);
    assert.equal(ctx.state, 'running');
    assert.ok(close(m.getVoiceLevel(), 0.5));
}

// ── suspended の AudioContext は resume を試し、それでも running でなければ音量は null ──
{
    const { m } = loadWithStreamFactory();
    const ctx = fakeContext({ state: 'suspended', resumeTo: 'suspended' });
    m.setVoiceLevelAudioContextFactory(() => ctx);
    const audio = fakeAudio({ captureStream: () => fakeStream(1) });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    audio.dispatch('playing');
    assert.equal(ctx.resumeCalls, 1);
    assert.equal(m.getVoicedMessageId(), 'msg-1');
    assert.equal(m.getVoiceLevel(), null);
}

// ── resume / suspend が例外を投げても外へ出さない ──
{
    const { m } = loadWithStreamFactory();
    const ctx = fakeContext({ state: 'suspended' });
    ctx.resume = () => { throw new Error('resume failed'); };
    ctx.suspend = () => Promise.reject(new Error('suspend failed'));
    m.setVoiceLevelAudioContextFactory(() => ctx);
    const audio = fakeAudio({ captureStream: () => fakeStream(1) });
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    assert.doesNotThrow(() => audio.dispatch('playing'));
    assert.equal(m.getVoiceLevel(), null);
    ctx.state = 'running';
    assert.doesNotThrow(() => audio.dispatch('ended'));
}

// ── track.stop() や removeTrack() が例外を投げても外へ出さない ──
{
    const { m } = loadWithStreamFactory();
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    const stream = fakeStream(1);
    stream.tracks[0].stop = () => { throw new Error('stop failed'); };
    stream.removeTrack = () => { throw new Error('removeTrack failed'); };
    const audio = fakeAudio({ captureStream: () => stream });
    let current = 'msg-1';
    const detach = m.attachVoiceLevelMeter(audio, () => current);
    audio.dispatch('playing');
    current = 'msg-2';
    audio.dispatch('emptied');
    const second = stream.deliverTrack();
    assert.doesNotThrow(() => audio.dispatch('playing'));
    assert.equal(m.getVoicedMessageId(), 'msg-2');
    assert.equal(ctx.sources[ctx.sources.length - 1].disconnected, false, 'the new track is still connected');
    assert.ok(close(m.getVoiceLevel(), 0.5));
    second.stop = () => { throw new Error('stop failed'); };
    assert.doesNotThrow(() => detach());
    assert.equal(m.getVoicedMessageId(), null);
}

// ── 知らせるのは値が変わったときだけ ──
{
    const { m } = loadWithStreamFactory();
    m.setVoiceLevelAudioContextFactory(() => fakeContext());
    const audio = fakeAudio();
    m.attachVoiceLevelMeter(audio, () => 'msg-1');
    let notified = 0;
    const unsubscribe = m.subscribeVoicePlayback(() => { notified++; });
    audio.dispatch('playing');
    audio.dispatch('playing');
    assert.equal(notified, 1, 'same messageId twice notifies once');
    audio.dispatch('pause');
    audio.dispatch('ended');
    assert.equal(notified, 2);
    unsubscribe();
    audio.dispatch('playing');
    assert.equal(notified, 2, 'unsubscribed listener is not called');
    // listener が例外を投げても、他の listener と見張りは止まらない
    let other = 0;
    m.subscribeVoicePlayback(() => { throw new Error('listener failed'); });
    m.subscribeVoicePlayback(() => { other++; });
    assert.doesNotThrow(() => audio.dispatch('pause'));
    assert.equal(other, 1);
}

// ── 返した関数を呼ぶと listener が外れ、鳴っている声から外れ、解析が外れ、stream の track が全部止まる ──
{
    const { m } = loadWithStreamFactory();
    const ctx = fakeContext();
    m.setVoiceLevelAudioContextFactory(() => ctx);
    const stream = fakeStream(1);
    const audio = fakeAudio({ captureStream: () => stream });
    const detach = m.attachVoiceLevelMeter(audio, () => 'msg-1');
    assert.equal(audio.listenerCount(), 5);
    audio.dispatch('playing');
    assert.equal(m.getVoicedMessageId(), 'msg-1');
    // 止まった後に足された track も、見張りをやめるときに止める
    audio.dispatch('emptied');
    const extra = stream.deliverTrack();
    detach();
    assert.equal(audio.listenerCount(), 0, 'all listeners are removed');
    assert.equal(stream.listenerCount('addtrack'), 0, 'the addtrack listener is removed');
    for (const track of stream.tracks) assert.equal(track.stopCalls, 1, 'every track of the stream is stopped');
    assert.equal(extra.stopCalls, 1);
    assert.equal(m.getVoicedMessageId(), null);
    assert.equal(ctx.sources[0].disconnected, true);
    audio.dispatch('playing');
    assert.equal(m.getVoicedMessageId(), null, 'a detached element is not tracked');
    assert.doesNotThrow(() => detach(), 'calling detach twice is harmless');
    // 同じ要素を二度渡しても、listener は二重にならない
    const again = fakeAudio();
    m.attachVoiceLevelMeter(again, () => 'msg-2');
    m.attachVoiceLevelMeter(again, () => 'msg-3');
    assert.equal(again.listenerCount(), 5);
}

console.warn = originalWarn;
console.log('Voice level meter tracking, single captureStream, track replacement, failure isolation and detach rules passed.');
