/** 鳴っている声と、その音量を追う (docs/intent/screen_avatar.md 設計 3 の「口パク」)。
 *
 * audio 要素のイベント (`playing` / `pause` / `ended` / `emptied` / `error`) を聞くだけで、
 * 声の再生には手を出さない。具体的には次のことを守る。
 * - `AudioContext.createMediaElementSource()` を使わない。これは audio 要素の出力を
 *   AudioContext の中へ付け替えるので、AudioContext が suspended になると声が鳴らなくなる。
 *   代わりに `HTMLMediaElement.captureStream()` で取った MediaStream の track を
 *   `createMediaStreamSource()` で読む。audio 要素の出力はそのまま鳴り続ける。
 * - 音量を読むために作るノードを `AudioContext.destination` へ繋がない。
 * - audio 要素の `src` / `volume` / `muted` / `currentTime` / `crossOrigin` に代入せず、
 *   `play()` / `pause()` を呼ばない。
 * - どんな失敗でも例外を外へ出さない。失敗したときは音量を null (分からない) にする。
 *
 * captureStream の扱い (Chromium で確かめた挙動に合わせている):
 * - 同じ audio 要素で `captureStream()` を呼ぶたびに新しい MediaStream が返り、止めていない
 *   track は要素の `src` を差し替えた後も `live` のまま残る。呼ぶたびに取り直すと、声が鳴った
 *   回数だけ MediaStream が積もる。だから要素ごとに `captureStream()` は一度だけ呼び、その
 *   MediaStream を見張りをやめるまで持ち続ける。
 * - 一度取った MediaStream は、要素の `src` が差し替わるたびに新しい audio の track を一本
 *   受け取る (`emptied` → `loadedmetadata` → stream の `addtrack` → 要素の `playing` の順)。
 *   `playing` の時点では前の `src` の track も `live` のまま残っていることがあり、そこからは
 *   音が出ない。だから `playing` のたびに一番新しい `live` の track を一本だけ残し、ほかの
 *   track は `stop()` して `removeTrack()` で外す。
 * - 要素が最後まで鳴り切ると、その track は `ended` になって stream から外れる。
 * - captureStream で取った track を `stop()` しても、要素の再生は続く。
 * - 残した一本だけを入れた `new MediaStream([track])` から `createMediaStreamSource()` を作る。
 * Chromium 以外のブラウザでの挙動は確かめていない。
 *
 * `captureStream` の無いブラウザでは音量を読まない (接頭辞つきの `mozCaptureStream` は使わない)。
 * そのときも、どの声が鳴っているか (messageId) は出す。
 *
 * このモジュールは three に依らない。lib/vrmAvatarEngine.ts と画面の部品の両方から読む。 */

/** このモジュールが使う AudioContext の機能だけを並べた形 (テストで偽物に差し替えるため)。 */
export interface VoiceLevelAudioContext {
    readonly state: string;
    createMediaStreamSource(stream: MediaStream): MediaStreamAudioSourceNode;
    createAnalyser(): AnalyserNode;
    resume(): Promise<void>;
    suspend(): Promise<void>;
}

export type VoiceLevelAudioContextFactory = () => VoiceLevelAudioContext | null;

/** 一本の track だけを入れた MediaStream の作り方。null を返したときは、captureStream で
 * 取った MediaStream をそのまま `createMediaStreamSource()` に渡す。 */
export type VoiceLevelMediaStreamFactory = (tracks: MediaStreamTrack[]) => MediaStream | null;

/** 解析に使う AnalyserNode の fftSize。時間領域の値を一度に読む数でもある。 */
const ANALYSER_FFT_SIZE = 1024;

interface CaptureCapableAudio extends HTMLAudioElement {
    captureStream?: () => MediaStream;
}

interface TrackedAudio {
    audio: HTMLAudioElement;
    getMessageId: () => string | null | undefined;
    /** 鳴っている間の messageId。鳴っていないときと、messageId の無い音声のときは null。 */
    messageId: string | null;
    /** 最後に `playing` になった順番。大きいほど新しい。 */
    playingOrder: number;
    /** この要素で一度だけ取った captureStream の MediaStream。見張りをやめるまで持ち続ける。 */
    stream: MediaStream | null;
    /** `captureStream()` が例外を投げた要素では、二度と呼ばない。 */
    captureFailed: boolean;
    /** stream の `addtrack` で最後に足された track。 */
    latestTrack: MediaStreamTrack | null;
    /** stream に付けた `addtrack` の listener。 */
    onAddTrack: ((event: Event) => void) | null;
    source: MediaStreamAudioSourceNode | null;
    analyser: AnalyserNode | null;
    buffer: Float32Array<ArrayBuffer> | null;
}

function defaultAudioContextFactory(): VoiceLevelAudioContext | null {
    const Ctor = (globalThis as { AudioContext?: new () => AudioContext }).AudioContext;
    if (typeof Ctor !== 'function') return null;
    return new Ctor();
}

function defaultMediaStreamFactory(tracks: MediaStreamTrack[]): MediaStream | null {
    const Ctor = (globalThis as { MediaStream?: new (tracks: MediaStreamTrack[]) => MediaStream }).MediaStream;
    if (typeof Ctor !== 'function') return null;
    return new Ctor(tracks);
}

let audioContextFactory: VoiceLevelAudioContextFactory = defaultAudioContextFactory;
let mediaStreamFactory: VoiceLevelMediaStreamFactory = defaultMediaStreamFactory;
let audioContext: VoiceLevelAudioContext | null = null;
/** AudioContext を作れないと分かったら、二度と作ろうとしない。 */
let audioContextUnavailable = false;
const tracked = new Map<HTMLAudioElement, TrackedAudio>();
let playingCounter = 0;
let notifiedMessageId: string | null = null;
const voiceListeners = new Set<() => void>();

/** AudioContext の作り方を差し替える (テスト用)。null を渡すと既定 (`new AudioContext()`) に戻す。
 * すでに作った AudioContext は捨て、次に必要になったときに新しい作り方で作る。 */
export function setVoiceLevelAudioContextFactory(factory: VoiceLevelAudioContextFactory | null): void {
    audioContextFactory = factory ?? defaultAudioContextFactory;
    audioContext = null;
    audioContextUnavailable = false;
}

/** 一本の track だけを入れた MediaStream の作り方を差し替える (テスト用)。null を渡すと既定
 * (`new MediaStream(tracks)`、`MediaStream` が無ければ null) に戻す。 */
export function setVoiceLevelMediaStreamFactory(factory: VoiceLevelMediaStreamFactory | null): void {
    mediaStreamFactory = factory ?? defaultMediaStreamFactory;
}

function warn(message: string, err: unknown): void {
    try {
        console.warn(`[voice_level] ${message}`, err);
    } catch {
        // console が使えない環境でも、外へ例外を出さない。
    }
}

function getAudioContext(): VoiceLevelAudioContext | null {
    if (audioContext) return audioContext;
    if (audioContextUnavailable) return null;
    try {
        audioContext = audioContextFactory();
    } catch (err) {
        warn('AudioContext could not be created', err);
        audioContext = null;
    }
    if (!audioContext) audioContextUnavailable = true;
    return audioContext;
}

function resumeAudioContext(ctx: VoiceLevelAudioContext): void {
    if (ctx.state === 'running') return;
    try {
        const p = ctx.resume();
        if (p && typeof p.catch === 'function') p.catch(() => { /* 音量が分からないだけなので握りつぶす */ });
    } catch {
        // 音量が分からないだけなので握りつぶす。
    }
}

/** 鳴っている声として覚えていて、stream は取れているが、live の track が来ていないために
 * 解析をまだ繋げていない要素か。 */
function isWaitingForTrack(t: TrackedAudio): boolean {
    return t.messageId !== null && t.stream !== null && t.source === null;
}

function suspendAudioContextIfIdle(): void {
    const ctx = audioContext;
    if (!ctx) return;
    for (const t of Array.from(tracked.values())) {
        // 繋がっている解析か、track が来るのを待っている要素があれば、止めない。
        if (t.source || isWaitingForTrack(t)) return;
    }
    if (ctx.state !== 'running') return;
    try {
        const p = ctx.suspend();
        if (p && typeof p.catch === 'function') p.catch(() => { /* 次に鳴ったとき resume する */ });
    } catch {
        // 次に鳴ったとき resume する。
    }
}

function stopTrack(track: MediaStreamTrack): void {
    try { track.stop(); } catch (err) { warn('track could not be stopped', err); }
}

function audioTracksOf(stream: MediaStream): MediaStreamTrack[] {
    try {
        return typeof stream.getAudioTracks === 'function' ? Array.from(stream.getAudioTracks()) : [];
    } catch (err) {
        warn('getAudioTracks failed', err);
        return [];
    }
}

/** 解析 (source と analyser) だけを外す。captureStream の MediaStream と、そこに残っている
 * track はそのまま持ち続ける (次の `playing` で整理する)。 */
function disconnectAnalysis(t: TrackedAudio): void {
    if (t.source) {
        try { t.source.disconnect(); } catch { /* 既に外れている */ }
    }
    if (t.analyser) {
        try { t.analyser.disconnect(); } catch { /* 既に外れている */ }
    }
    t.source = null;
    t.analyser = null;
    t.buffer = null;
}

/** 見張りをやめるときの片付け。解析を外し、`addtrack` の listener を外し、stream の track を
 * 全部 `stop()` して、stream への参照を捨てる。 */
function releaseStream(t: TrackedAudio): void {
    disconnectAnalysis(t);
    const stream = t.stream;
    if (stream) {
        if (t.onAddTrack) {
            try { stream.removeEventListener('addtrack', t.onAddTrack); } catch { /* 外せなくても害は無い */ }
        }
        let tracks: MediaStreamTrack[] = [];
        try {
            tracks = typeof stream.getTracks === 'function' ? Array.from(stream.getTracks()) : audioTracksOf(stream);
        } catch (err) {
            warn('getTracks failed', err);
            tracks = audioTracksOf(stream);
        }
        for (const track of tracks) stopTrack(track);
        if (t.latestTrack && !tracks.includes(t.latestTrack)) stopTrack(t.latestTrack);
    }
    t.stream = null;
    t.onAddTrack = null;
    t.latestTrack = null;
}

/** この要素の captureStream の MediaStream を返す。まだ取っていなければ一度だけ取り、
 * `addtrack` の listener を一つ付ける。取れない要素では null。 */
function ensureStream(t: TrackedAudio): MediaStream | null {
    if (t.stream) return t.stream;
    if (t.captureFailed) return null;
    const audio = t.audio as CaptureCapableAudio;
    if (typeof audio.captureStream !== 'function') return null;
    let stream: MediaStream;
    try {
        stream = audio.captureStream();
    } catch (err) {
        t.captureFailed = true;
        warn('captureStream failed', err);
        return null;
    }
    if (!stream) {
        t.captureFailed = true;
        return null;
    }
    t.stream = stream;
    const onAddTrack = (event: Event) => {
        try {
            if (tracked.get(t.audio) !== t || t.stream !== stream) return;
            const track = (event as MediaStreamTrackEvent | undefined)?.track;
            if (track && track.kind === 'audio') t.latestTrack = track;
            // 鳴っている声として覚えていて、解析がまだ繋がっていなければ、ここで繋ぐ。
            // 止まった後に来た track では繋がない (次の `playing` で繋ぐ)。
            if (t.messageId === null || t.source) return;
            const ctx = getAudioContext();
            if (!ctx) return;
            connectLatestTrack(t, ctx, stream);
        } catch (err) {
            warn('voice level analysis could not be connected after addtrack', err);
            disconnectAnalysis(t);
        }
    };
    t.onAddTrack = onAddTrack;
    try {
        stream.addEventListener('addtrack', onAddTrack);
    } catch (err) {
        warn('addtrack listener could not be added', err);
    }
    return stream;
}

/** 使う track を決める。`addtrack` で覚えた track が live ならそれ、そうでなければ
 * `getAudioTracks()` の中の live の末尾 (実測では末尾が新しい track だったが、並び順が
 * 足された順だという保証は確かめていない)。live の track が無ければ null。 */
function pickLiveTrack(t: TrackedAudio, tracks: MediaStreamTrack[]): MediaStreamTrack | null {
    if (t.latestTrack && t.latestTrack.readyState === 'live') return t.latestTrack;
    for (let i = tracks.length - 1; i >= 0; i--) {
        if (tracks[i].readyState === 'live') return tracks[i];
    }
    return null;
}

/** 一番新しい live の track を一本だけ残し、ほかの track を `stop()` して stream から外し、
 * 残した一本だけを入れた MediaStream から解析を繋ぐ。live の track が無ければ繋がない。 */
function connectLatestTrack(t: TrackedAudio, ctx: VoiceLevelAudioContext, stream: MediaStream): void {
    disconnectAnalysis(t);
    const tracks = audioTracksOf(stream);
    const track = pickLiveTrack(t, tracks);
    if (!track) return;
    for (const other of tracks) {
        if (other === track) continue;
        stopTrack(other);
        try { stream.removeTrack(other); } catch (err) { warn('track could not be removed', err); }
    }
    t.latestTrack = track;
    let analysisStream: MediaStream | null = null;
    try {
        analysisStream = mediaStreamFactory([track]);
    } catch (err) {
        warn('MediaStream could not be created', err);
        analysisStream = null;
    }
    resumeAudioContext(ctx);
    const source = ctx.createMediaStreamSource(analysisStream ?? stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = ANALYSER_FFT_SIZE;
    // destination へは繋がない。声は audio 要素がそのまま鳴らす。
    source.connect(analyser);
    t.source = source;
    t.analyser = analyser;
    t.buffer = new Float32Array(new ArrayBuffer(analyser.fftSize * Float32Array.BYTES_PER_ELEMENT));
}

/** `playing` (messageId がある場合) で解析を繋ぎ直す。captureStream は最初の一回だけ取る。 */
function reconnectAnalysis(t: TrackedAudio): void {
    disconnectAnalysis(t);
    const audio = t.audio as CaptureCapableAudio;
    if (!t.stream && (t.captureFailed || typeof audio.captureStream !== 'function')) return;
    const ctx = getAudioContext();
    if (!ctx) return;
    const stream = ensureStream(t);
    if (!stream) return;
    connectLatestTrack(t, ctx, stream);
}

function latestVoiced(): TrackedAudio | null {
    let latest: TrackedAudio | null = null;
    for (const t of Array.from(tracked.values())) {
        if (t.messageId === null) continue;
        if (!latest || t.playingOrder > latest.playingOrder) latest = t;
    }
    return latest;
}

function notifyIfChanged(): void {
    const messageId = latestVoiced()?.messageId ?? null;
    if (messageId === notifiedMessageId) return;
    notifiedMessageId = messageId;
    for (const listener of Array.from(voiceListeners)) {
        try {
            listener();
        } catch (err) {
            warn('voice playback listener failed', err);
        }
    }
}

function onPlaying(t: TrackedAudio): void {
    let messageId: string | null = null;
    try {
        const value = t.getMessageId();
        messageId = typeof value === 'string' && value ? value : null;
    } catch (err) {
        warn('getMessageId failed', err);
    }
    t.messageId = messageId;
    t.playingOrder = ++playingCounter;
    if (messageId !== null) {
        try {
            reconnectAnalysis(t);
        } catch (err) {
            warn('voice level analysis could not be connected', err);
            disconnectAnalysis(t);
        }
    } else {
        // messageId の無い音声 (unlock 用の無音の WAV など) では音量を読まない。
        disconnectAnalysis(t);
    }
    notifyIfChanged();
    suspendAudioContextIfIdle();
}

/** `pause` / `ended` / `emptied` / `error` のとき。解析だけを外し、stream は持ち続ける。 */
function onStopped(t: TrackedAudio): void {
    t.messageId = null;
    disconnectAnalysis(t);
    notifyIfChanged();
    suspendAudioContextIfIdle();
}

/** audio 要素を見張り始める。返す関数で見張りをやめ、その要素の解析と captureStream の
 * MediaStream を片付ける (track は全部 `stop()` する)。
 *
 * getMessageId は `playing` のたびに呼ぶ。文字列を返したときだけ、その要素を「鳴っている声」
 * として扱う。同じ要素を二度渡したときは、二度目は何もせず、何もしない関数を返す。 */
export function attachVoiceLevelMeter(
    audio: HTMLAudioElement,
    getMessageId: () => string | null | undefined,
): () => void {
    try {
        if (tracked.has(audio)) return () => {};
        const t: TrackedAudio = {
            audio, getMessageId, messageId: null, playingOrder: 0,
            stream: null, captureFailed: false, latestTrack: null, onAddTrack: null,
            source: null, analyser: null, buffer: null,
        };
        const handlePlaying = () => {
            try { onPlaying(t); } catch (err) { warn('playing handler failed', err); }
        };
        const handleStopped = () => {
            try { onStopped(t); } catch (err) { warn('stop handler failed', err); }
        };
        const stopEvents = ['pause', 'ended', 'emptied', 'error'] as const;
        audio.addEventListener('playing', handlePlaying);
        for (const type of stopEvents) audio.addEventListener(type, handleStopped);
        tracked.set(audio, t);
        let detached = false;
        return () => {
            if (detached) return;
            detached = true;
            try {
                audio.removeEventListener('playing', handlePlaying);
                for (const type of stopEvents) audio.removeEventListener(type, handleStopped);
            } catch (err) {
                warn('listeners could not be removed', err);
            }
            try {
                if (tracked.get(audio) === t) tracked.delete(audio);
                t.messageId = null;
                releaseStream(t);
                notifyIfChanged();
                suspendAudioContextIfIdle();
            } catch (err) {
                warn('detach failed', err);
            }
        };
    } catch (err) {
        warn('attachVoiceLevelMeter failed', err);
        return () => {};
    }
}

/** いま鳴っている声 (複数あれば最後に `playing` になったもの) の RMS (0〜1)。
 * 解析が繋がっていて AudioContext が running のときだけ数値を返し、それ以外は null。
 * 呼ばれたときに `getFloatTimeDomainData` から計算する。 */
export function getVoiceLevel(): number | null {
    try {
        const t = latestVoiced();
        if (!t || !t.analyser || !t.buffer) return null;
        const ctx = audioContext;
        if (!ctx || ctx.state !== 'running') return null;
        const buffer = t.buffer;
        t.analyser.getFloatTimeDomainData(buffer);
        if (buffer.length === 0) return null;
        let sum = 0;
        for (let i = 0; i < buffer.length; i++) {
            const v = buffer[i];
            sum += v * v;
        }
        const rms = Math.sqrt(sum / buffer.length);
        if (!Number.isFinite(rms)) return null;
        return Math.min(1, Math.max(0, rms));
    } catch {
        return null;
    }
}

/** 鳴っている声の messageId の変化を購読する。useSyncExternalStore で読める形。 */
export function subscribeVoicePlayback(listener: () => void): () => void {
    voiceListeners.add(listener);
    return () => {
        voiceListeners.delete(listener);
    };
}

/** いま鳴っている声 (複数あれば最後に `playing` になったもの) の messageId。無ければ null。 */
export function getVoicedMessageId(): string | null {
    return notifiedMessageId;
}

/** サーバー側の描画とハイドレーションの間の値 (鳴っていない)。 */
export function getVoicedMessageIdServerSnapshot(): string | null {
    return null;
}
