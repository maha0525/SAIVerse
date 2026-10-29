import { t as uiText } from '@/i18n/core';

// 返事を待つ間、スピナーの横に出す「いま何をしているか」。
// 段階はバックエンドの status イベントの phase (sea/progress_events.py) と、
// 画面側で分かる節目 (送信・発言の保存・本文の流し込み) から決まる。
// 文言はここだけが持つ。Playbook やノードの名前は利用者には意味が無いので出さない。
// 設計: docs/intent/chat_progress_display.md

export type ChatProgressPhase =
    | 'recording'
    | 'uploading'
    | 'preparing'
    | 'building_request'
    | 'awaiting_response'
    | 'thinking'
    | 'replying'
    | 'running_tool'
    | 'finishing';

export interface ChatProgressParams {
    provider?: string;
    persona?: string;
    tool?: string;
}

const text = (value: unknown): string | undefined =>
    typeof value === 'string' && value.trim() ? value.trim() : undefined;

/** 段階の表示文。分からない段階は null (呼び出し側は表示を変えない)。 */
export function describeChatProgress(phase: string, params: ChatProgressParams = {}): string | null {
    const provider = text(params.provider);
    const persona = text(params.persona);
    const tool = text(params.tool);
    switch (phase) {
        case 'recording':
            return uiText('chatProgress.recording');
        case 'uploading':
            return uiText('chatProgress.uploading');
        case 'preparing':
            return uiText('chatProgress.preparing');
        case 'building_request':
            return provider
                ? uiText('chatProgress.buildingRequest', { provider })
                : uiText('chatProgress.buildingRequestUnknown');
        case 'awaiting_response':
            return provider
                ? uiText('chatProgress.awaitingResponse', { provider })
                : uiText('chatProgress.awaitingResponseUnknown');
        case 'thinking':
            return persona
                ? uiText('chatProgress.thinking', { persona })
                : uiText('chatProgress.thinkingUnknown');
        case 'replying':
            return persona
                ? uiText('chatProgress.replying', { persona })
                : uiText('chatProgress.replyingUnknown');
        case 'running_tool':
            return tool
                ? uiText('chatProgress.runningTool', { tool })
                : uiText('chatProgress.runningToolUnknown');
        case 'finishing':
            return uiText('chatProgress.finishing');
        default:
            return null;
    }
}

/**
 * status イベントの表示文。null は「表示を変えない」。
 *
 * - phase のあるもの: 段階の文言。
 * - Playbook / ノードの名前だけのもの (各ノードが出す開発用の足跡) と、
 *   ストリームの最初の合図 ("processing"): 表示を変えない。
 * - それ以外: サーバーが利用者向けに書いた文 (記憶の整理など) をそのまま出す。
 */
export function progressTextForStatusEvent(event: Record<string, unknown>): string | null {
    if (typeof event.phase === 'string') {
        return describeChatProgress(event.phase, {
            provider: text(event.provider),
            persona: text(event.persona_name),
            tool: text(event.tool),
        });
    }
    if (event.playbook || event.node || event.playbook_chain) return null;
    const content = text(event.content);
    if (!content || content === 'processing') return null;
    return content;
}
