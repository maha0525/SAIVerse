/**
 * `play_audio` client action executor.
 *
 * Phase 1 (FIFO 順次再生) + Phase 2 (pulse_id ベース preempt):
 *   発火した audio_ready は ``<audio>`` の ``ended`` イベントで順次再生される
 *   queue で管理する。 ただし新着 audio_ready の ``pulse_id`` が現再生中の
 *   item と **異なる** 場合は、 queue を全クリア + 現再生を即中断 + 新 audio
 *   を即再生する (= 別 pulse の発話 = ユーザー新ターン等の割り込みを即時反映
 *   するため)。
 *
 *   pulse_id 比較ロジック:
 *     - 両方の pulse_id が定義済み + 異なる → preempt
 *     - それ以外 (= 片方/両方 undefined、 または同じ) → FIFO 末尾積み
 *
 *   片方 undefined の保守的扱いは、 voice-tts 以外の addon (= pulse 概念を
 *   持たない発話源) が play_audio を呼んでも誤って既存再生を切らないため。
 *
 * autoplay 対策 (iOS Safari / Android Chrome):
 *   モバイルブラウザは「ユーザージェスチャー起点でない audio.play()」を拒否する。
 *   SSE コールバックからは gesture ではないため new Audio().play() は弾かれる。
 *   そのため: 単一の HTMLAudioElement を保持して、最初のユーザー操作時に
 *   短い silent WAV を再生して要素を unlock する。以降は同じ要素の src を
 *   差し替えて play() するだけなので gesture 無しでも再生できる。
 *
 * 安全弁:
 *  - ``ended`` event: 正常終了 → 次 item を取り出して再生
 *  - ``error`` event: 破損 audio 等 → fallback URL があれば試行、 無ければ
 *    item を reject して次 item へ
 *  - watchdog timeout (10 分): ``ended`` も ``error`` も発火しない異常状態
 *    で queue が永久に止まらないための強制 dequeue
 *
 * 失敗ケース:
 *  - URL が解決できない → Error throw (registry 側が on_failure_endpoint に POST)
 *  - audio.play() が Promise reject (autoplay 拒否等) → Error throw
 */
import type { ClientActionExecutor } from "@/lib/clientActionRegistry";
import { resolveActionValue } from "@/lib/clientActionRegistry";

// ---------- 共有 <audio> 要素と iOS gesture unlock --------------------

// 再利用する単一の <audio> 要素。
let sharedAudio: HTMLAudioElement | null = null;
// 共有要素が gesture 中に play() を通されて unlock されたかどうか。
// iOS では「最初の gesture 同期タイミングで play() を呼んだ HTMLAudioElement」
// だけが以降 autoplay 許可される挙動のため、この状態を明示的に管理する。
let unlocked = false;

// 44 バイトの完全無音 WAV (data chunk 0 bytes) を Blob URL として生成する。
// data URL は iOS でロードがハングする既知問題があるため、バイト列から直接
// Blob を作って URL.createObjectURL で渡す (iOS が「本物のURL」と認識する)。
let silentBlobUrl: string | null = null;
function getSilentBlobUrl(): string {
    if (silentBlobUrl) return silentBlobUrl;
    const bytes = new Uint8Array([
        // RIFF header
        0x52, 0x49, 0x46, 0x46, 0x24, 0x00, 0x00, 0x00,
        0x57, 0x41, 0x56, 0x45,
        // fmt chunk
        0x66, 0x6d, 0x74, 0x20, 0x10, 0x00, 0x00, 0x00,
        0x01, 0x00, 0x01, 0x00,
        0x44, 0xac, 0x00, 0x00,
        0x88, 0x58, 0x01, 0x00,
        0x02, 0x00, 0x10, 0x00,
        // data chunk (0 bytes)
        0x64, 0x61, 0x74, 0x61, 0x00, 0x00, 0x00, 0x00,
    ]);
    const blob = new Blob([bytes], { type: "audio/wav" });
    silentBlobUrl = URL.createObjectURL(blob);
    return silentBlobUrl;
}

function getSharedAudio(): HTMLAudioElement {
    if (sharedAudio) return sharedAudio;
    if (typeof window === "undefined") {
        throw new Error("play_audio: HTMLAudioElement is not available (SSR context)");
    }
    sharedAudio = new Audio();
    sharedAudio.preload = "auto";
    attachAudioListeners(sharedAudio);
    return sharedAudio;
}

// gesture ハンドラ: **同期的に** audio.play() を通して共有要素を unlock する。
// iOS Safari はここで play() を通した HTMLAudioElement だけ、以降の非 gesture
// play() を許容する挙動になっている。失敗しても element が "gesture に触れた"
// 事実は残るので try/catch で静かに握りつぶす。
if (typeof window !== "undefined") {
    const onGesture = () => {
        if (unlocked) return;
        try {
            const audio = getSharedAudio();
            // unlock 用の silent WAV は queue を介さず src を直接差し替える。
            // queue item ではないので currentItem は触らない。 ended 発火時の
            // onCurrentEnded は currentItem === null を見て早期 return する。
            audio.src = getSilentBlobUrl();
            // 同期的に play() を呼ぶのが iOS unlock のキモ。await も .then() も
            // しないでよい (rejected でも iOS 側の "gesture 中に play() を呼んだ"
            // 記録は残る)。
            const p = audio.play();
            if (p && typeof p.then === "function") {
                // resolved でも rejected でも unlock 成功扱いにする
                // (iOS は rejected でも internal に gesture 使用を記録する)
                p.then(() => { unlocked = true; }).catch(() => { unlocked = true; });
            } else {
                unlocked = true;
            }
        } catch {
            // gesture handler 内の失敗は致命的ではない (次の gesture で再試行可)
        }
    };
    window.addEventListener("click", onGesture, { passive: true });
    window.addEventListener("touchstart", onGesture, { passive: true });
    window.addEventListener("keydown", onGesture, { passive: true });
}

// ---------- 再生 queue --------------------

type QueueItem = {
    url: string;
    fallbackUrl?: string;
    /** 同 pulse / 別 pulse 判定軸 (Phase 2)。 voice-tts 以外の発話源では undefined */
    pulseId?: string;
    /** ログ識別 + 個別 item を指す key */
    messageId?: string;
    /** 再生開始時刻 (= startNext で audio.play() を呼んだ moment)、 ended/error 時の実再生時間計算用 */
    playStartedAt?: number;
    resolve: () => void;
    reject: (err: Error) => void;
};

/**
 * 別 pulse 着信時に旧 queue を全クリアする (= ユーザー新ターン即時反映)。
 *
 * 引数 ``newPulseId`` と現 currentItem.pulseId を比較し、 両方が定義済み +
 * 異なる場合に true を返して preempt 経路を発動する。 この関数自体は判定
 * 専用で副作用を持たない (= 呼び出し側で実際の clear / pause を行う)。
 */
function shouldPreemptForNewPulse(newPulseId: string | undefined): boolean {
    if (currentItem === null) return false;
    if (newPulseId === undefined) return false;
    if (currentItem.pulseId === undefined) return false;
    return newPulseId !== currentItem.pulseId;
}

/**
 * 別 pulse preempt: queue を全クリアし、 現再生 audio を即中断する。
 *
 * 旧 queue 内の item は ``resolve()`` で benign 扱い (= reject すると
 * registry 側 runAndReport が on_failure_endpoint に POST してしまうが、
 * 「別 pulse に置き換わった」 のは本来 失敗ではないため)。 旧 currentItem
 * も同様に resolve。 audio element は ``pause()`` + ``src=""`` で完全停止
 * させる (= ended イベントは発火させない)。
 */
function preemptCurrentPulse(reason: string): void {
    const dropped = playbackQueue.splice(0);
    for (const item of dropped) {
        item.resolve();
    }
    if (currentItem !== null) {
        currentItem.resolve();
        currentItem = null;
    }
    clearWatchdog();
    const audio = sharedAudio;
    if (audio !== null) {
        audio.pause();
        // src="" で empty にすると ended は発火せず、 直後の startNext での
        // src 差し替え時に Audio element の状態がリセットされた状態で開始
        // できる。 emptied イベントは発火するが listener を付けていないので
        // 副作用なし。
        audio.src = "";
    }
    diag(`preempt fired (${reason})`, {
        droppedCount: dropped.length,
    });
}

// ---------- DIAG: Phase 1 検証用詳細ログ (= 確認後撤去 or DEBUG 化) ----
function diag(msg: string, extra?: Record<string, unknown>): void {
    if (extra) {
        console.log(`[play_audio:diag] ${msg}`, extra);
    } else {
        console.log(`[play_audio:diag] ${msg}`);
    }
}
function shortMsg(id?: string): string {
    return id ? id.slice(0, 8) : "?";
}

const playbackQueue: QueueItem[] = [];
let currentItem: QueueItem | null = null;
let listenersAttached = false;
let watchdogTimer: ReturnType<typeof setTimeout> | null = null;

// ``ended`` も ``error`` も発火しない hang 状態を防ぐための上限。
// 通常の発話 (最大数十秒) より十分大きく取り、 5 分を超える長尺 audio も
// 想定して 10 分にする。 Phase 3 で観測しながら調整するかもしれない。
const WATCHDOG_TIMEOUT_MS = 10 * 60 * 1000;

function attachAudioListeners(audio: HTMLAudioElement): void {
    if (listenersAttached) return;
    listenersAttached = true;
    audio.addEventListener("ended", onCurrentEnded);
    audio.addEventListener("error", onCurrentError);
}

function onCurrentEnded(): void {
    const audio = sharedAudio;
    if (currentItem === null) {
        // unlock 用 silent WAV 等、 queue 外の再生終了。 何もしない。
        diag("ended fired but currentItem=null (= silent WAV unlock or stale)", {
            duration: audio?.duration,
            currentTime: audio?.currentTime,
        });
        return;
    }
    const item = currentItem;
    const elapsed =
        item.playStartedAt !== undefined
            ? (performance.now() - item.playStartedAt) / 1000
            : undefined;
    diag(`ended fired msg=${shortMsg(item.messageId)}`, {
        audioDuration: audio?.duration,
        audioCurrentTime: audio?.currentTime,
        elapsedSinceStart: elapsed,
        queueLength: playbackQueue.length,
    });
    currentItem = null;
    clearWatchdog();
    item.resolve();
    void startNext();
}

function onCurrentError(_ev: Event): void {
    const audio = sharedAudio;
    if (currentItem === null) {
        diag("error fired but currentItem=null", {
            errorCode: audio?.error?.code,
            errorMessage: audio?.error?.message,
        });
        return;
    }
    const item = currentItem;
    diag(`error fired msg=${shortMsg(item.messageId)}`, {
        errorCode: audio?.error?.code,
        errorMessage: audio?.error?.message,
        audioDuration: audio?.duration,
        audioCurrentTime: audio?.currentTime,
    });
    currentItem = null;
    clearWatchdog();

    // fallback URL が別 URL として設定されていれば 1 度だけ試す。
    // ストリーミング src が壊れても完了 wav (audio_path) で復帰できるケース用。
    if (item.fallbackUrl && item.fallbackUrl !== item.url) {
        const audio = getSharedAudio();
        const retry: QueueItem = {
            ...item,
            url: item.fallbackUrl,
            fallbackUrl: undefined,
        };
        currentItem = retry;
        armWatchdog();
        audio.src = retry.url;
        audio.play().catch((err) => {
            const msg = err instanceof Error ? err.message : String(err);
            currentItem = null;
            clearWatchdog();
            retry.reject(new Error(`play_audio: fallback play() rejected: ${msg}`));
            void startNext();
        });
    } else {
        item.reject(new Error("play_audio: <audio> error event fired"));
        void startNext();
    }
}

function armWatchdog(): void {
    clearWatchdog();
    watchdogTimer = setTimeout(onWatchdogFire, WATCHDOG_TIMEOUT_MS);
}

function clearWatchdog(): void {
    if (watchdogTimer !== null) {
        clearTimeout(watchdogTimer);
        watchdogTimer = null;
    }
}

function onWatchdogFire(): void {
    if (currentItem === null) return;
    const item = currentItem;
    const audio = sharedAudio;
    currentItem = null;
    watchdogTimer = null;
    console.warn(
        `[play_audio] watchdog fired (${WATCHDOG_TIMEOUT_MS / 1000}s); ` +
        `force-advancing queue (msg=${item.messageId ?? "?"})`,
        {
            audioDuration: audio?.duration,
            audioCurrentTime: audio?.currentTime,
            audioPaused: audio?.paused,
            audioReadyState: audio?.readyState,
        },
    );
    // 呼び出し側の executor promise は reject ではなく resolve する
    // (= ユーザー視点では「再生できなかった」 ではなく「終わった扱い」 で次に
    // 進ませたいケース、 例えば audio_path が壊れているが executor を error
    // 扱いにしてフォールバック POST を発動する必要は無い)。 reject にしたい
    // ケースが出てきたら Phase 3 で見直す。
    item.resolve();
    void startNext();
}

async function startNext(): Promise<void> {
    if (currentItem !== null) {
        diag(`startNext called but currentItem exists msg=${shortMsg(currentItem.messageId)}`, {
            queueLength: playbackQueue.length,
        });
        return;
    }
    const next = playbackQueue.shift();
    if (!next) {
        diag("startNext called with empty queue");
        return;
    }
    currentItem = next;
    armWatchdog();

    const audio = getSharedAudio();
    diag(`startNext setting src msg=${shortMsg(next.messageId)} url=${next.url}`, {
        prevSrc: audio.src,
        queueLength: playbackQueue.length,
    });
    audio.src = next.url;
    next.playStartedAt = performance.now();
    try {
        await audio.play();
        // play() resolved = 再生開始成功。 完了は ended event で検出する。
        diag(`audio.play() resolved msg=${shortMsg(next.messageId)}`, {
            audioDuration: audio.duration,
            audioReadyState: audio.readyState,
        });
    } catch (err) {
        // play() reject 時は currentItem がまだ next を指している (Phase 1 では
        // 並行操作なし)。 fallback URL があれば試す、 無ければ即 reject + 次。
        if (currentItem !== next) return; // (Phase 2 で誰かが preempt した場合の防御)
        const msg = err instanceof Error ? err.message : String(err);
        if (next.fallbackUrl && next.fallbackUrl !== next.url) {
            const retry: QueueItem = {
                ...next,
                url: next.fallbackUrl,
                fallbackUrl: undefined,
            };
            currentItem = retry;
            audio.src = retry.url;
            try {
                await audio.play();
            } catch (fbErr) {
                const fbMsg = fbErr instanceof Error ? fbErr.message : String(fbErr);
                if (currentItem === retry) {
                    currentItem = null;
                    clearWatchdog();
                    retry.reject(new Error(
                        `play_audio: both primary and fallback play() rejected ` +
                        `(primary=${msg}, fallback=${fbMsg})`,
                    ));
                    void startNext();
                }
            }
        } else {
            currentItem = null;
            clearWatchdog();
            next.reject(new Error(`play_audio: audio.play() rejected: ${msg}`));
            void startNext();
        }
    }
}

// ---------- executor 本体 --------------------

export const playAudioExecutor: ClientActionExecutor = async (ctx) => {
    const { action, event } = ctx;

    const primaryUrl = resolveActionValue(ctx, action.source_metadata_key) as
        | string
        | undefined;
    const fallbackUrl = resolveActionValue(ctx, action.fallback_metadata_key) as
        | string
        | undefined;

    const firstUrl = primaryUrl ?? fallbackUrl;
    if (!firstUrl || typeof firstUrl !== "string") {
        throw new Error(
            `play_audio: no URL resolved from metadata (source=${action.source_metadata_key}, fallback=${action.fallback_metadata_key})`,
        );
    }

    // pulse_id は audio_ready event payload から拾う (= voice-tts addon が
    // Phase 2 で payload に追加した値)。 voice-tts 以外の発話源 (= 将来別
    // addon が play_audio を使うケース) では undefined のまま、 その場合は
    // FIFO に倒れる (= 既存再生を切らない保守動作)。
    const pulseId = event.data?.pulse_id as string | undefined;

    // executor の Promise は queue item が完了 (= ended / error / watchdog /
    // preempt) した時点で settle する。 preempt 経路でも resolve() を呼ぶ
    // (= 別 pulse に置き換わったのは本来失敗ではないため)。
    return new Promise<void>((resolve, reject) => {
        const item: QueueItem = {
            url: firstUrl,
            fallbackUrl:
                fallbackUrl && fallbackUrl !== primaryUrl
                    ? fallbackUrl
                    : undefined,
            pulseId,
            messageId: event.message_id,
            resolve,
            reject,
        };

        // 別 pulse 着信 → 旧 pulse の queue + 現再生を即破棄してから
        // 新 item を queue に積む (= startNext でそのまま新 audio が再生開始)。
        if (shouldPreemptForNewPulse(pulseId)) {
            preemptCurrentPulse(
                `new pulse=${pulseId ?? "?"} differs from current=${currentItem?.pulseId ?? "?"}`,
            );
        }

        playbackQueue.push(item);
        diag(`enqueue msg=${shortMsg(item.messageId)} url=${firstUrl}`, {
            currentItemMsg: currentItem ? shortMsg(currentItem.messageId) : null,
            queueLengthAfter: playbackQueue.length,
            pulseId: pulseId ?? null,
        });
        // 再生中でなければすぐ開始。 再生中なら ended 後に startNext が呼ばれる。
        void startNext();
    });
};
