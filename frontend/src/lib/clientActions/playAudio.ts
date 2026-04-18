/**
 * `play_audio` client action executor.
 *
 * source_metadata_key もしくは fallback_metadata_key で指定されたキーから
 * URL を解決し、`<audio>` 要素で再生する。前の再生が鳴っている途中で
 * 新しい発話が来た場合は前音声を停止してから切り替える（同時多重再生を防止）。
 *
 * 失敗ケース:
 *  - URL が解決できない → Error throw (registry 側が on_failure_endpoint に POST)
 *  - audio.play() が Promise reject (autoplay 拒否等) → Error throw
 */
import type { ClientActionExecutor } from "@/lib/clientActionRegistry";
import { resolveActionValue } from "@/lib/clientActionRegistry";

// 単一の <audio> 要素を保持して、新発話が来たら前を止める。
let currentAudio: HTMLAudioElement | null = null;

export const playAudioExecutor: ClientActionExecutor = async (ctx) => {
    const { action } = ctx;

    const url =
        (resolveActionValue(ctx, action.source_metadata_key) as string | undefined) ??
        (resolveActionValue(ctx, action.fallback_metadata_key) as string | undefined);

    if (!url || typeof url !== "string") {
        throw new Error(
            `play_audio: no URL resolved from metadata (source=${action.source_metadata_key}, fallback=${action.fallback_metadata_key})`,
        );
    }

    // 前の音声を停止
    if (currentAudio) {
        try {
            currentAudio.pause();
            currentAudio.src = "";
        } catch {
            // no-op
        }
        currentAudio = null;
    }

    const audio = new Audio(url);
    currentAudio = audio;

    audio.onended = () => {
        if (currentAudio === audio) {
            currentAudio = null;
        }
    };

    try {
        await audio.play();
    } catch (err) {
        if (currentAudio === audio) {
            currentAudio = null;
        }
        const msg = err instanceof Error ? err.message : String(err);
        throw new Error(`play_audio: audio.play() rejected: ${msg}`);
    }
};
