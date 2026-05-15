/**
 * `play_audio` client action executor.
 *
 * Phase 1 (FIFO 順次再生):
 *   発火した audio_ready は **queue 末尾** に積まれ、 共有 ``<audio>`` 要素の
 *   ``ended`` イベントで次の item を順次再生する。 これにより 「同じ pulse
 *   から複数発話が連続発火しても 1 つも切れずに最後まで再生」 が成立する。
 *   旧来 (= Phase 0) は新発話が来るたびに src を上書きしていたため、 後発が
 *   前発を強制中断していた。
 *
 *   queue item は ``pulseId`` / ``messageId`` を保持する (Phase 2 prep)。
 *   Phase 2 で 「同 pulse → queue 末尾、 別 pulse → queue 全クリア + 現 audio
 *   を pause() で即中断 + 新 audio 即再生」 という pulse_id 比較分岐を追加
 *   する。 Phase 1 の現状ではどの item も同じ扱いで FIFO に並ぶ。
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
    /** Phase 2 で同 pulse / 別 pulse 判定に使う。 Phase 1 では undefined のまま */
    pulseId?: string;
    /** ログ識別 + Phase 2 で個別 item を指す key */
    messageId?: string;
    resolve: () => void;
    reject: (err: Error) => void;
};

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
    if (currentItem === null) {
        // unlock 用 silent WAV 等、 queue 外の再生終了。 何もしない。
        return;
    }
    const item = currentItem;
    currentItem = null;
    clearWatchdog();
    item.resolve();
    void startNext();
}

function onCurrentError(_ev: Event): void {
    if (currentItem === null) return;
    const item = currentItem;
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
    currentItem = null;
    watchdogTimer = null;
    console.warn(
        `[play_audio] watchdog fired (${WATCHDOG_TIMEOUT_MS / 1000}s); ` +
        `force-advancing queue (msg=${item.messageId ?? "?"})`,
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
    if (currentItem !== null) return; // 既に再生中 → ended で進む
    const next = playbackQueue.shift();
    if (!next) return;
    currentItem = next;
    armWatchdog();

    const audio = getSharedAudio();
    audio.src = next.url;
    try {
        await audio.play();
        // play() resolved = 再生開始成功。 完了は ended event で検出する。
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

    // Phase 2 の preempt 判定軸用に pulse_id を event payload から拾う。
    // Phase 1 段階では server 側 (= voice-tts の audio_ready emit) がまだ
    // pulse_id を載せていないので undefined のままになる。 Phase 2 で server
    // 側 emit_addon_event payload に pulse_id を足したらここが意味を持つ。
    const pulseId = event.data?.pulse_id as string | undefined;

    // executor の Promise は queue item が完了 (= ended / error / watchdog)
    // した時点で settle する。 これで registry 側の runAndReport が
    // on_failure_endpoint POST を撃つタイミングが queue 完了基準で揃う。
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
        playbackQueue.push(item);
        // 再生中でなければすぐ開始。 再生中なら ended 後に startNext が呼ばれる。
        void startNext();
    });
};
