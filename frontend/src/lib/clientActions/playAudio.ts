/**
 * `play_audio` client action executor.
 *
 * source_metadata_key もしくは fallback_metadata_key で指定されたキーから
 * URL を解決し、`<audio>` 要素で再生する。前の再生が鳴っている途中で
 * 新しい発話が来た場合は前音声を停止してから切り替える（同時多重再生を防止）。
 *
 * autoplay 対策 (iOS Safari / Android Chrome):
 *   モバイルブラウザは「ユーザージェスチャー起点でない audio.play()」を拒否する。
 *   SSE コールバックからは gesture ではないため new Audio().play() は弾かれる。
 *   そのため: 単一の HTMLAudioElement を保持して、最初のユーザー操作時に
 *   短い silent WAV を再生して要素を unlock する。以降は同じ要素の src を
 *   差し替えて play() するだけなので gesture 無しでも再生できる。
 *
 * 失敗ケース:
 *  - URL が解決できない → Error throw (registry 側が on_failure_endpoint に POST)
 *  - audio.play() が Promise reject (autoplay 拒否等) → Error throw
 */
import type { ClientActionExecutor } from "@/lib/clientActionRegistry";
import { resolveActionValue } from "@/lib/clientActionRegistry";

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

// 連続した play_audio 呼び出しの直列化トークン。
// 呼び出しごとにインクリメントし、自分のトークンが最新でなければ自発的に中断する。
// これによりカスケード abort (前 src が読み込み中に次 src に差し替えられて
// AbortError になる) を「後続に上書きされた = benign」として扱い、本番失敗と
// 切り分ける。
let playbackToken = 0;

async function tryPlayUrl(
    audio: HTMLAudioElement,
    url: string,
): Promise<void> {
    audio.src = url;
    await audio.play();
}

export const playAudioExecutor: ClientActionExecutor = async (ctx) => {
    const { action } = ctx;

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

    const audio = getSharedAudio();
    const myToken = ++playbackToken;

    try {
        await tryPlayUrl(audio, firstUrl);
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);

        // 自分より後に別の play_audio 呼び出しが走った場合、src 差し替えで
        // AbortError になるのは想定通り (後勝ち)。失敗扱いにしない。
        if (myToken !== playbackToken && /abort/i.test(msg)) {
            return;
        }

        // Primary (stream URL 等) が失敗 かつ fallback URL (完了 wav 等) があれば
        // それでリトライする。ストリーミング中にネットワーク問題等で progressive
        // 再生が立ち上がらないときの保険。
        const shouldTryFallback =
            fallbackUrl && primaryUrl && fallbackUrl !== primaryUrl;
        if (!shouldTryFallback) {
            throw new Error(`play_audio: audio.play() rejected: ${msg}`);
        }

        // 自分がまだ最新トークンか再確認 (この間に新しい発話が来てれば譲る)
        if (myToken !== playbackToken) {
            return;
        }

        try {
            await tryPlayUrl(audio, fallbackUrl);
        } catch (fbErr) {
            const fbMsg = fbErr instanceof Error ? fbErr.message : String(fbErr);
            if (myToken !== playbackToken && /abort/i.test(fbMsg)) {
                return;
            }
            throw new Error(
                `play_audio: both primary and fallback failed (primary=${msg}, fallback=${fbMsg})`,
            );
        }
    }
};
