'use client';

/**
 * 通話モード (Gemini Live API) のフロント側 UI。
 *
 * 設計は docs/intent/voice_call.md。 第一巡のプロトタイプなので、
 * 「マイクを取って、バックエンドの WebSocket に流して、返ってきた音を隙間なく
 * 鳴らす」 ところまでを最短距離で作る。
 *
 * バックエンドとの契約 (intent と同じ):
 *   - 接続直後にテキストフレーム {"type":"start", persona_id, building_id, voice, model}
 *   - {"type":"ready"} を受けてからマイクのバイナリ送信を開始する
 *   - クライアント→サーバのバイナリ = 16kHz PCM16 LE mono
 *   - サーバ→クライアントのバイナリ = 24kHz PCM16 LE mono
 *   - 切断は {"type":"end"} を送り、 {"type":"call_ended"} を待ってから閉じる
 *
 * Next.js の /api rewrite は WebSocket の upgrade を通さないので、 ここだけは
 * バックエンド (既定 :8000) に直結する。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Phone, PhoneOff, Mic, MicOff, X, Loader, AlertTriangle } from 'lucide-react';
import ModalOverlay from '@/components/common/ModalOverlay';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import styles from './VoiceCallModal.module.css';

/** Live API が受け付ける入力のサンプリングレート */
const INPUT_RATE = 16000;
/** Live API が返してくる音声のサンプリングレート */
const OUTPUT_RATE = 24000;
/** 1 回の送信に載せるサンプル数 (16kHz なので 2048 サンプル = 128ms) */
const SEND_CHUNK_SAMPLES = 2048;
/** 再生を始めるまでに置く余裕。 これより手前に予約すると詰まって途切れる */
const PLAYBACK_LEAD_SEC = 0.06;
/** {"type":"end"} を送ってから call_ended を待つ上限 */
const END_TIMEOUT_MS = 5000;

const VOICE_PRESETS = ['Puck', 'Charon', 'Kore', 'Fenrir', 'Aoede', 'Leda', 'Orus', 'Zephyr'];
const MODEL_OPTIONS = ['gemini-3.8-live', 'gemini-3.8-live-extended-thinking'];
const DEFAULT_VOICE = 'Kore';
const DEFAULT_MODEL = MODEL_OPTIONS[0];

/**
 * マイクの生の波形を 1024 サンプルずつまとめてメインスレッドへ渡すだけの
 * AudioWorklet。 別ファイルとして配信する手間を避けるため Blob URL で登録する。
 * ダウンサンプルと Int16 への変換はメインスレッド側で行う。
 */
const CAPTURE_WORKLET_SOURCE = `
class SaiverseVoiceCapture extends AudioWorkletProcessor {
    constructor() {
        super();
        this._chunk = new Float32Array(1024);
        this._filled = 0;
    }
    process(inputs) {
        const input = inputs[0];
        const channel = input && input[0];
        if (!channel) return true;
        for (let i = 0; i < channel.length; i++) {
            this._chunk[this._filled++] = channel[i];
            if (this._filled === this._chunk.length) {
                this.port.postMessage(this._chunk.slice(0));
                this._filled = 0;
            }
        }
        return true;
    }
}
registerProcessor('saiverse-voice-capture', SaiverseVoiceCapture);
`;

/**
 * サーバーの error フレームの code → 画面の文言。
 *
 * サーバーは言語を持たない安定した識別子 (code) だけを返し、 文言はここで
 * 画面の言語から引く。 知らない code が来たときは汎用の文言に、 開発者向けの
 * message を添えて出す (黙って落とすと原因がどこにも出ない)。
 */
const ERROR_TEXT: Record<string, () => string> = {
    bad_start: () => uiText('components.VoiceCallModal.error_bad_start'),
    missing_persona_id: () => uiText('components.VoiceCallModal.error_missing_persona_id'),
    persona_not_found: () => uiText('components.VoiceCallModal.error_persona_not_found'),
    persona_location_unknown: () => uiText('components.VoiceCallModal.error_persona_location_unknown'),
    no_api_key: () => uiText('components.VoiceCallModal.error_no_api_key'),
    manager_not_ready: () => uiText('components.VoiceCallModal.error_manager_not_ready'),
    unauthorized: () => uiText('components.VoiceCallModal.error_unauthorized'),
    bad_origin: () => uiText('components.VoiceCallModal.error_bad_origin'),
    invalid_voice: () => uiText('components.VoiceCallModal.error_invalid_voice'),
    invalid_model: () => uiText('components.VoiceCallModal.error_invalid_model'),
    already_in_call: () => uiText('components.VoiceCallModal.error_already_in_call'),
    session_ending: () => uiText('components.VoiceCallModal.error_session_ending'),
    call_failed: () => uiText('components.VoiceCallModal.error_call_failed'),
};

function errorTextFor(code: string | undefined, message: string | undefined): string {
    const resolve = code ? ERROR_TEXT[code] : undefined;
    if (resolve) return resolve();
    const generic = uiText('components.VoiceCallModal.error_unknown');
    return message ? `${generic} (${message})` : generic;
}

type CallStatus = 'idle' | 'connecting' | 'active' | 'ending' | 'ended' | 'error';

interface TranscriptEntry {
    id: number;
    who: 'user' | 'persona';
    text: string;
}

interface VoiceCallModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
    personaName: string;
    /**
     * このペルソナが今いると画面が思っている部屋。
     *
     * 通話の舞台は**サーバーがペルソナの現在地から決める**ので、 これは
     * 送るだけで使われない。 ここが null のときだけは「現在地が取れていない」
     * ので、 通話ボタンを押させずにその旨を出す。
     */
    buildingId: string | null;
}

/** ペルソナごとの声・モデルの既定値を置いておく localStorage のキー */
function prefsKey(personaId: string): string {
    return `saiverse-voice-call:${personaId}`;
}

function loadPrefs(personaId: string): { voice: string; model: string } {
    try {
        const raw = localStorage.getItem(prefsKey(personaId));
        if (raw) {
            const parsed = JSON.parse(raw) as { voice?: unknown; model?: unknown };
            return {
                voice: typeof parsed.voice === 'string' && parsed.voice ? parsed.voice : DEFAULT_VOICE,
                model: typeof parsed.model === 'string' && parsed.model ? parsed.model : DEFAULT_MODEL,
            };
        }
    } catch (err) {
        console.warn('[voice-call] Cannot read saved voice settings', err);
    }
    return { voice: DEFAULT_VOICE, model: DEFAULT_MODEL };
}

function savePrefs(personaId: string, voice: string, model: string): void {
    try {
        localStorage.setItem(prefsKey(personaId), JSON.stringify({ voice, model }));
    } catch (err) {
        console.warn('[voice-call] Cannot save voice settings', err);
    }
}

/** バックエンドの WebSocket URL。 Next.js の rewrite は通さない */
function buildWebSocketUrl(): string {
    const configured = process.env.NEXT_PUBLIC_SAIVERSE_BACKEND_WS_HOST;
    const host = configured && configured.trim() ? configured.trim() : `${window.location.hostname}:8000`;
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${scheme}://${host}/api/voice/call`;
}

function formatElapsed(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

export default function VoiceCallModal({ isOpen, onClose, personaId, personaName, buildingId }: VoiceCallModalProps) {
    useLocale();

    const [status, setStatus] = useState<CallStatus>('idle');
    const [errorText, setErrorText] = useState<string | null>(null);
    const [entries, setEntries] = useState<TranscriptEntry[]>([]);
    const [elapsed, setElapsed] = useState(0);
    const [muted, setMuted] = useState(false);
    const [voice, setVoice] = useState(DEFAULT_VOICE);
    const [model, setModel] = useState(DEFAULT_MODEL);
    // null = まだ判定していない (SSR とクライアントで食い違わせないため)
    const [micSupported, setMicSupported] = useState<boolean | null>(null);

    const wsRef = useRef<WebSocket | null>(null);
    const streamRef = useRef<MediaStream | null>(null);
    const inputCtxRef = useRef<AudioContext | null>(null);
    const sourceNodeRef = useRef<MediaStreamAudioSourceNode | null>(null);
    const workletNodeRef = useRef<AudioWorkletNode | null>(null);
    // 無音のまま destination まで繋ぐための栓。 出力側に何も繋がっていない
    // ワークレットは処理対象から外されうるので、 音量 0 で経路だけ通しておく。
    const sinkNodeRef = useRef<GainNode | null>(null);
    const outputCtxRef = useRef<AudioContext | null>(null);
    const scheduledRef = useRef<Set<AudioBufferSourceNode>>(new Set());
    const nextPlayTimeRef = useRef(0);
    const readyRef = useRef(false);
    const mutedRef = useRef(false);
    const endTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    // 「こちらから切った」 かどうか。 サーバ都合の切断と区別してエラー表示を出し分ける
    const closingRef = useRef(false);
    // サーバから「まもなくセッションを終える」 と予告されたときの文言。
    // 実際に切れたときにこれを出すと、 ただの「切断されました」 より理由が残る
    const closeReasonRef = useRef<string | null>(null);

    // リサンプラの持ち越し状態 (チャンク境界で位相がずれないよう跨いで持つ)
    const leftoverRef = useRef<Float32Array>(new Float32Array(0));
    const resamplePosRef = useRef(0);
    const pendingRef = useRef<number[]>([]);

    // 文字起こしの「いま開いている行」。 turn_complete で閉じて次から新しい行にする
    const nextEntryIdRef = useRef(1);
    const openUserRef = useRef<number | null>(null);
    const openPersonaRef = useRef<number | null>(null);

    const transcriptRef = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        setMicSupported(
            typeof navigator !== 'undefined'
            && !!navigator.mediaDevices
            && typeof navigator.mediaDevices.getUserMedia === 'function'
            && typeof window !== 'undefined'
            && typeof window.AudioContext !== 'undefined'
        );
    }, []);

    useEffect(() => {
        mutedRef.current = muted;
    }, [muted]);

    // 開くたびに、このペルソナに保存してある声とモデルを既定値として読み直す
    useEffect(() => {
        if (!isOpen || !personaId) return;
        const prefs = loadPrefs(personaId);
        setVoice(prefs.voice);
        setModel(prefs.model);
    }, [isOpen, personaId]);

    // ---------- 再生 ----------

    const stopPlayback = useCallback(() => {
        scheduledRef.current.forEach(src => {
            try {
                src.onended = null;
                src.stop();
            } catch {
                // 既に終わっているソースの stop() は無視してよい
            }
        });
        scheduledRef.current.clear();
        nextPlayTimeRef.current = 0;
    }, []);

    const playChunk = useCallback((data: ArrayBuffer) => {
        const ctx = outputCtxRef.current;
        if (!ctx) return;
        // PCM16 なので奇数バイトは切り捨てる (途中で切れたフレームの保険)
        const usable = data.byteLength - (data.byteLength % 2);
        if (usable <= 0) return;
        const pcm = new Int16Array(data, 0, usable / 2);
        const buffer = ctx.createBuffer(1, pcm.length, OUTPUT_RATE);
        const channel = buffer.getChannelData(0);
        for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;

        const src = ctx.createBufferSource();
        src.buffer = buffer;
        src.connect(ctx.destination);
        const now = ctx.currentTime;
        if (nextPlayTimeRef.current < now + PLAYBACK_LEAD_SEC) {
            nextPlayTimeRef.current = now + PLAYBACK_LEAD_SEC;
        }
        src.start(nextPlayTimeRef.current);
        nextPlayTimeRef.current += buffer.duration;
        scheduledRef.current.add(src);
        src.onended = () => { scheduledRef.current.delete(src); };
    }, []);

    // ---------- 後片付け ----------

    const teardown = useCallback(() => {
        if (endTimerRef.current !== null) {
            clearTimeout(endTimerRef.current);
            endTimerRef.current = null;
        }
        readyRef.current = false;

        stopPlayback();

        const ws = wsRef.current;
        wsRef.current = null;
        if (ws) {
            ws.onmessage = null;
            ws.onerror = null;
            ws.onclose = null;
            ws.onopen = null;
            try { ws.close(); } catch { /* 既に閉じている */ }
        }

        const node = workletNodeRef.current;
        workletNodeRef.current = null;
        if (node) {
            node.port.onmessage = null;
            try { node.disconnect(); } catch { /* 切断済み */ }
        }

        const sink = sinkNodeRef.current;
        sinkNodeRef.current = null;
        if (sink) {
            try { sink.disconnect(); } catch { /* 切断済み */ }
        }

        const source = sourceNodeRef.current;
        sourceNodeRef.current = null;
        if (source) {
            try { source.disconnect(); } catch { /* 切断済み */ }
        }

        const stream = streamRef.current;
        streamRef.current = null;
        if (stream) {
            for (const track of stream.getTracks()) {
                try { track.stop(); } catch { /* 停止済み */ }
            }
        }

        const inputCtx = inputCtxRef.current;
        inputCtxRef.current = null;
        if (inputCtx && inputCtx.state !== 'closed') {
            void inputCtx.close().catch(() => { /* 二重 close は無視 */ });
        }

        const outputCtx = outputCtxRef.current;
        outputCtxRef.current = null;
        if (outputCtx && outputCtx.state !== 'closed') {
            void outputCtx.close().catch(() => { /* 二重 close は無視 */ });
        }

        leftoverRef.current = new Float32Array(0);
        resamplePosRef.current = 0;
        pendingRef.current = [];
    }, [stopPlayback]);

    // ---------- マイク ----------

    /**
     * ワークレットから届いた波形を 16kHz に落とし、 Int16 LE にして送る。
     * 端数のサンプルと小数位置をチャンクを跨いで持ち越すので、 長く喋っても
     * 位相がずれない。
     */
    const handleMicChunk = useCallback((chunk: Float32Array) => {
        const ws = wsRef.current;
        const inputCtx = inputCtxRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN || !readyRef.current || !inputCtx) return;

        // ミュート中も無音を送り続ける。 途中で流れが止まると Live API 側の
        // 発話区間の判定が乱れるため、 送信そのものは止めない。
        const samples = mutedRef.current ? new Float32Array(chunk.length) : chunk;

        const ratio = inputCtx.sampleRate / INPUT_RATE;
        const prev = leftoverRef.current;
        const merged = new Float32Array(prev.length + samples.length);
        merged.set(prev, 0);
        merged.set(samples, prev.length);

        let pos = resamplePosRef.current;
        const pending = pendingRef.current;
        while (pos + 1 < merged.length) {
            const index = Math.floor(pos);
            const frac = pos - index;
            pending.push(merged[index] * (1 - frac) + merged[index + 1] * frac);
            pos += ratio;
        }
        // 読み終えた分だけ捨てる。 最後の一歩が配列の外まで進んだときは
        // 行き過ぎた分を小数位置に残し、 次のチャンクの頭で読み飛ばす
        // (ここを切り捨てると喋り続けるうちに位相がずれていく)。
        const consumed = Math.min(Math.floor(pos), merged.length);
        leftoverRef.current = merged.slice(consumed);
        resamplePosRef.current = pos - consumed;

        while (pending.length >= SEND_CHUNK_SAMPLES) {
            const frame = pending.splice(0, SEND_CHUNK_SAMPLES);
            const out = new ArrayBuffer(SEND_CHUNK_SAMPLES * 2);
            const view = new DataView(out);
            for (let i = 0; i < SEND_CHUNK_SAMPLES; i++) {
                const clamped = Math.max(-1, Math.min(1, frame[i]));
                view.setInt16(i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
            }
            try {
                ws.send(out);
            } catch (err) {
                console.warn('[voice-call] Failed to send microphone audio', err);
                return;
            }
        }
    }, []);

    // ---------- 文字起こし ----------

    const appendTranscript = useCallback((who: 'user' | 'persona', text: string) => {
        if (!text) return;
        const openRef = who === 'user' ? openUserRef : openPersonaRef;
        const openId = openRef.current;
        if (openId === null) {
            const id = nextEntryIdRef.current++;
            openRef.current = id;
            setEntries(prev => [...prev, { id, who, text }]);
        } else {
            setEntries(prev => prev.map(e => (e.id === openId ? { ...e, text: e.text + text } : e)));
        }
    }, []);

    useEffect(() => {
        const el = transcriptRef.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, [entries]);

    // ---------- 開始 / 終了 ----------

    const finishCall = useCallback((nextStatus: CallStatus, message: string | null) => {
        teardown();
        setStatus(nextStatus);
        setErrorText(message);
    }, [teardown]);

    const startCall = useCallback(async () => {
        if (!buildingId) {
            setStatus('error');
            setErrorText(uiText('components.VoiceCallModal.text024'));
            return;
        }
        setErrorText(null);
        setEntries([]);
        setElapsed(0);
        setMuted(false);
        openUserRef.current = null;
        openPersonaRef.current = null;
        closingRef.current = false;
        closeReasonRef.current = null;
        setStatus('connecting');
        savePrefs(personaId, voice, model);

        // 音声の文脈はマイクの許可を求める**前**に作って resume する。
        // 許可ダイアログで数秒待たされたあとに resume すると、 クリックによる
        // 活性化のウィンドウを外れて suspended のまま返ってこない環境がある。
        let inputCtx: AudioContext | null = null;
        try {
            // 出力は 24kHz の文脈を直接作る。 作れない環境では既定レートに落ちるが、
            // その場合も AudioBuffer 側のレート指定でブラウザが合わせてくれる。
            let outputCtx: AudioContext;
            try {
                outputCtx = new AudioContext({ sampleRate: OUTPUT_RATE });
            } catch {
                outputCtx = new AudioContext();
            }
            outputCtxRef.current = outputCtx;
            if (outputCtx.state === 'suspended') await outputCtx.resume();

            inputCtx = new AudioContext();
            inputCtxRef.current = inputCtx;
            if (inputCtx.state === 'suspended') await inputCtx.resume();

            const blob = new Blob([CAPTURE_WORKLET_SOURCE], { type: 'application/javascript' });
            const moduleUrl = URL.createObjectURL(blob);
            try {
                await inputCtx.audioWorklet.addModule(moduleUrl);
            } finally {
                URL.revokeObjectURL(moduleUrl);
            }
        } catch (err) {
            console.error('[voice-call] Audio setup failed', err);
            // teardown が作りかけの文脈を閉じる (ref に入れてから失敗しても拾える)
            finishCall('error', uiText('components.VoiceCallModal.text023'));
            return;
        }
        if (!inputCtx) {
            finishCall('error', uiText('components.VoiceCallModal.text023'));
            return;
        }

        let stream: MediaStream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({
                audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
            });
        } catch (err) {
            console.error('[voice-call] getUserMedia failed', err);
            finishCall('error', uiText('components.VoiceCallModal.text015'));
            return;
        }
        streamRef.current = stream;

        try {
            const sourceNode = inputCtx.createMediaStreamSource(stream);
            sourceNodeRef.current = sourceNode;
            const workletNode = new AudioWorkletNode(inputCtx, 'saiverse-voice-capture');
            workletNodeRef.current = workletNode;
            workletNode.port.onmessage = (event: MessageEvent) => {
                handleMicChunk(event.data as Float32Array);
            };
            sourceNode.connect(workletNode);
            // 自分の声がスピーカーに返ると回り込むので、 音量 0 の栓を挟んで
            // destination まで繋ぐ。 経路が destination まで通っていないと
            // ワークレットの process が呼ばれなくなる実装があるため。
            const sink = inputCtx.createGain();
            sink.gain.value = 0;
            sinkNodeRef.current = sink;
            workletNode.connect(sink);
            sink.connect(inputCtx.destination);
        } catch (err) {
            console.error('[voice-call] Audio setup failed', err);
            finishCall('error', uiText('components.VoiceCallModal.text023'));
            return;
        }

        let ws: WebSocket;
        try {
            ws = new WebSocket(buildWebSocketUrl());
        } catch (err) {
            console.error('[voice-call] WebSocket creation failed', err);
            finishCall('error', uiText('components.VoiceCallModal.text016'));
            return;
        }
        ws.binaryType = 'arraybuffer';
        wsRef.current = ws;

        ws.onopen = () => {
            try {
                ws.send(JSON.stringify({
                    type: 'start',
                    persona_id: personaId,
                    building_id: buildingId,
                    voice,
                    model,
                }));
            } catch (err) {
                console.error('[voice-call] Failed to send start frame', err);
                finishCall('error', uiText('components.VoiceCallModal.text016'));
            }
        };

        ws.onmessage = (event: MessageEvent) => {
            if (event.data instanceof ArrayBuffer) {
                playChunk(event.data);
                return;
            }
            if (typeof event.data !== 'string') return;
            let payload: { type?: string; text?: string; code?: string; message?: string };
            try {
                payload = JSON.parse(event.data);
            } catch (err) {
                console.warn('[voice-call] Unparsable frame', event.data, err);
                return;
            }
            switch (payload.type) {
                case 'ready':
                    readyRef.current = true;
                    setStatus('active');
                    break;
                case 'input_transcript':
                    appendTranscript('user', payload.text ?? '');
                    break;
                case 'output_transcript':
                    appendTranscript('persona', payload.text ?? '');
                    break;
                case 'interrupted':
                    // ユーザーが被せて喋り始めた → 予約済みの再生を全部捨てる
                    stopPlayback();
                    // バックエンドはこの合図で両側の発話を確定する
                    // (flush_turn)。 画面の区切りも記憶と揃える。
                    openUserRef.current = null;
                    openPersonaRef.current = null;
                    break;
                case 'turn_complete':
                    openUserRef.current = null;
                    openPersonaRef.current = null;
                    break;
                case 'error': {
                    console.error('[voice-call] Server error', payload.code, payload.message);
                    const text = errorTextFor(payload.code, payload.message);
                    if (payload.code === 'session_ending') {
                        // まだ切れてはいない。 予告として出しておき、 実際に
                        // 切れたときの文言にも使う。
                        closeReasonRef.current = text;
                        setErrorText(text);
                        break;
                    }
                    closingRef.current = true;
                    finishCall('error', text);
                    break;
                }
                case 'call_ended':
                    closingRef.current = true;
                    finishCall('ended', null);
                    break;
                default:
                    console.debug('[voice-call] Unknown frame type', payload.type);
            }
        };

        ws.onerror = (event) => {
            console.error('[voice-call] WebSocket error', event);
        };

        ws.onclose = () => {
            if (closingRef.current) return;
            finishCall('error', closeReasonRef.current || uiText('components.VoiceCallModal.text017'));
        };
    }, [appendTranscript, buildingId, finishCall, handleMicChunk, model, personaId, playChunk, stopPlayback, voice]);

    const hangUp = useCallback(() => {
        const ws = wsRef.current;
        closingRef.current = true;
        readyRef.current = false;
        stopPlayback();
        if (ws && ws.readyState === WebSocket.OPEN) {
            setStatus('ending');
            try {
                ws.send(JSON.stringify({ type: 'end' }));
            } catch (err) {
                console.warn('[voice-call] Failed to send end frame', err);
                finishCall('ended', null);
                return;
            }
            endTimerRef.current = setTimeout(() => {
                console.warn('[voice-call] call_ended did not arrive; closing anyway');
                finishCall('ended', null);
            }, END_TIMEOUT_MS);
        } else {
            finishCall('ended', null);
        }
    }, [finishCall, stopPlayback]);

    // 経過時間。 通話中だけ進める
    useEffect(() => {
        if (status !== 'active') return;
        const timer = setInterval(() => setElapsed(prev => prev + 1), 1000);
        return () => clearInterval(timer);
    }, [status]);

    // 閉じられたら (アンマウントも含めて) マイク・音声文脈・WS を必ず落とす
    useEffect(() => {
        if (isOpen) return;
        closingRef.current = true;
        teardown();
        setStatus('idle');
        setErrorText(null);
        setEntries([]);
        setElapsed(0);
    }, [isOpen, teardown]);

    useEffect(() => {
        return () => {
            closingRef.current = true;
            teardown();
        };
    }, [teardown]);

    const handleClose = useCallback(() => {
        // 通話中に閉じられたら、 せめて終了の合図だけは投げてから落とす
        // (バックエンドは異常切断でも書き戻すが、 正規の終了の方が確実)。
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify({ type: 'end' })); } catch { /* 閉じる途中 */ }
        }
        closingRef.current = true;
        teardown();
        onClose();
    }, [onClose, teardown]);

    if (!isOpen) return null;

    const inCall = status === 'connecting' || status === 'active' || status === 'ending';
    const statusLabel =
        status === 'connecting' ? uiText('components.VoiceCallModal.text002')
            : status === 'active' ? uiText('components.VoiceCallModal.text003')
                : status === 'ending' ? uiText('components.VoiceCallModal.text019')
                    : status === 'ended' ? uiText('components.VoiceCallModal.text004')
                        : status === 'error' ? uiText('components.VoiceCallModal.text005')
                            : uiText('components.VoiceCallModal.text006');

    return (
        <ModalOverlay onClose={handleClose}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <div className={styles.headerInfo}>
                        <Phone size={18} className={styles.headerIcon} />
                        <div>
                            <h2 className={styles.title}>{personaName}</h2>
                            <div className={styles.subtitle} data-i18n="components.VoiceCallModal.text001">
                                {uiText('components.VoiceCallModal.text001')}
                            </div>
                        </div>
                    </div>
                    <button className={styles.closeBtn} onClick={handleClose} title={uiText('components.VoiceCallModal.text020')}>
                        <X size={18} />
                    </button>
                </div>

                <div className={styles.statusRow}>
                    <span className={`${styles.statusBadge} ${styles[`status_${status}`] ?? ''}`}>
                        {status === 'connecting' || status === 'ending' ? <Loader size={14} className={styles.spin} /> : null}
                        {statusLabel}
                    </span>
                    {(status === 'active' || status === 'ending' || status === 'ended') && (
                        <span className={styles.elapsed} data-i18n="components.VoiceCallModal.text022">
                            {uiText('components.VoiceCallModal.text022')} {formatElapsed(elapsed)}
                        </span>
                    )}
                </div>

                {micSupported === false && (
                    <div className={styles.warning} data-i18n="components.VoiceCallModal.text014">
                        <AlertTriangle size={16} />
                        <span>{uiText('components.VoiceCallModal.text014')}</span>
                    </div>
                )}

                {errorText && (
                    <div className={styles.error}>
                        <AlertTriangle size={16} />
                        <span>{errorText}</span>
                    </div>
                )}

                {!inCall && (
                    <div className={styles.settings}>
                        <label className={styles.field}>
                            <span className={styles.fieldLabel} data-i18n="components.VoiceCallModal.text007">
                                {uiText('components.VoiceCallModal.text007')}
                            </span>
                            <input
                                className={styles.fieldInput}
                                list="saiverse-voice-presets"
                                value={voice}
                                onChange={e => setVoice(e.target.value)}
                            />
                            <datalist id="saiverse-voice-presets">
                                {VOICE_PRESETS.map(name => <option key={name} value={name} />)}
                            </datalist>
                            <span className={styles.fieldHint} data-i18n="components.VoiceCallModal.text021">
                                {uiText('components.VoiceCallModal.text021')}
                            </span>
                        </label>

                        <label className={styles.field}>
                            <span className={styles.fieldLabel} data-i18n="components.VoiceCallModal.text008">
                                {uiText('components.VoiceCallModal.text008')}
                            </span>
                            <select
                                className={styles.fieldInput}
                                value={model}
                                onChange={e => setModel(e.target.value)}
                            >
                                {MODEL_OPTIONS.map(name => <option key={name} value={name}>{name}</option>)}
                            </select>
                        </label>
                    </div>
                )}

                <div className={styles.transcript} ref={transcriptRef}>
                    {entries.length === 0 ? (
                        <div className={styles.transcriptEmpty} data-i18n="components.VoiceCallModal.text013">
                            {uiText('components.VoiceCallModal.text013')}
                        </div>
                    ) : (
                        entries.map(entry => (
                            <div key={entry.id} className={`${styles.line} ${entry.who === 'user' ? styles.lineUser : styles.linePersona}`}>
                                <span className={styles.speaker}>
                                    {entry.who === 'user' ? uiText('components.VoiceCallModal.text018') : personaName}
                                </span>
                                <span className={styles.lineText}>{entry.text}</span>
                            </div>
                        ))
                    )}
                </div>

                <div className={styles.controls}>
                    {!inCall ? (
                        <button
                            className={styles.startBtn}
                            onClick={() => { void startCall(); }}
                            disabled={micSupported !== true || !buildingId}
                            data-i18n="components.VoiceCallModal.text009 components.VoiceCallModal.text025"
                        >
                            <Phone size={18} />
                            {status === 'ended' || status === 'error'
                                ? uiText('components.VoiceCallModal.text025')
                                : uiText('components.VoiceCallModal.text009')}
                        </button>
                    ) : (
                        <>
                            <button
                                className={`${styles.muteBtn} ${muted ? styles.muteActive : ''}`}
                                onClick={() => setMuted(v => !v)}
                                disabled={status !== 'active'}
                                data-i18n="components.VoiceCallModal.text011 components.VoiceCallModal.text012"
                            >
                                {muted ? <MicOff size={18} /> : <Mic size={18} />}
                                {muted ? uiText('components.VoiceCallModal.text012') : uiText('components.VoiceCallModal.text011')}
                            </button>
                            <button
                                className={styles.hangUpBtn}
                                onClick={hangUp}
                                disabled={status === 'ending'}
                                data-i18n="components.VoiceCallModal.text010"
                            >
                                <PhoneOff size={18} />
                                {uiText('components.VoiceCallModal.text010')}
                            </button>
                        </>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
