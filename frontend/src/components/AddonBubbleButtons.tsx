"use client";

import React, { useEffect, useRef, useState } from 'react';
import {
    Play,
    Loader,
    RefreshCw,
    Mic,
    Volume2,
    VolumeX,
    Wand2,
    Sparkles,
    BookOpen,
    Languages,
    Pause,
    Square,
    Trash2,
    Download,
    Upload,
    Save,
    Edit,
    Copy,
    Eye,
    EyeOff,
    Search,
    Star,
    Heart,
    Bookmark,
    Tag,
    Check,
    X,
    Plus,
    Minus,
    type LucideIcon,
} from 'lucide-react';
import styles from './AddonBubbleButtons.module.css';

// addon.json の "icon" 文字列を lucide コンポーネントへ解決するホワイトリスト。
// addon.json で安全に指定できる name の集合を絞り、未知の値は Play にフォールバック。
const ICON_MAP: Record<string, LucideIcon> = {
    'play': Play,
    'refresh-cw': RefreshCw,
    'mic': Mic,
    'volume-2': Volume2,
    'volume-x': VolumeX,
    'wand-2': Wand2,
    'sparkles': Sparkles,
    'book-open': BookOpen,
    'languages': Languages,
    'pause': Pause,
    'square': Square,
    'trash-2': Trash2,
    'download': Download,
    'upload': Upload,
    'save': Save,
    'edit': Edit,
    'copy': Copy,
    'eye': Eye,
    'eye-off': EyeOff,
    'search': Search,
    'star': Star,
    'heart': Heart,
    'bookmark': Bookmark,
    'tag': Tag,
    'check': Check,
    'x': X,
    'plus': Plus,
    'minus': Minus,
};

function resolveIcon(name?: string): LucideIcon {
    if (!name) return Play;
    return ICON_MAP[name] ?? Play;
}

export interface BubbleButtonDef {
    id: string;
    icon: string;
    label: string;
    action?: string;       // "play_audio" など
    tool?: string;         // api_routes.py のエンドポイントパス
    metadata_key?: string; // addonMetadata から参照するキー
    show_when?: string;    // "metadata_exists" | "always"
    addon_name: string;    // 所属アドオン名
}

export interface AddonBubbleButtonsProps {
    messageId: string;
    /** メッセージ本文。tool ボタンクリック時に POST body で addon に渡される */
    messageText?: string;
    /** assistant メッセージの発話ペルソナ ID。tool ボタンクリック時に POST body で addon に渡される */
    personaId?: string;
    /** { addon_name: { key: value } } 形式のメタデータ */
    addonMetadata: Record<string, Record<string, unknown>>;
    /** 有効なアドオンのバブルボタン定義一覧 */
    buttons: BubbleButtonDef[];
}

function PlayAudioButton({
    audioUrl,
    label,
    icon,
}: {
    audioUrl: string;
    label: string;
    icon: string;
}) {
    const audioRef = useRef<HTMLAudioElement | null>(null);
    const [playing, setPlaying] = React.useState(false);
    const [error, setError] = React.useState(false);

    // audioUrl が変わったら (例: 再生成によりサーバ側で wav が差し替わった場合)
    // 既存の Audio オブジェクトを破棄して新しい URL を読み直す。これが無いと
    // 一度再生したバブルは初回ロードした URL の音声をそのまま再生し続ける。
    useEffect(() => {
        if (audioRef.current) {
            try { audioRef.current.pause(); } catch { /* noop */ }
            audioRef.current = null;
        }
        setPlaying(false);
        setError(false);
    }, [audioUrl]);

    const handleClick = async () => {
        if (error) {
            setError(false);
        }
        if (!audioRef.current) {
            audioRef.current = new Audio(audioUrl);
            audioRef.current.onended = () => setPlaying(false);
            audioRef.current.onerror = (e) => {
                const mediaErr = audioRef.current?.error;
                console.error('[PlayAudioButton] audio error', mediaErr?.code, mediaErr?.message, e);
                setPlaying(false);
                setError(true);
            };
        }

        if (playing) {
            audioRef.current.pause();
            audioRef.current.currentTime = 0;
            setPlaying(false);
        } else {
            try {
                await audioRef.current.play();
                setPlaying(true);
            } catch (err) {
                console.error('[PlayAudioButton] play() rejected', err);
                setError(true);
            }
        }
    };

    const Icon = resolveIcon(icon);

    return (
        <button
            className={`${styles.bubbleBtn} ${playing ? styles.playing : ''} ${error ? styles.error : ''}`}
            onClick={handleClick}
            title={error ? '音声の再生に失敗しました' : (playing ? '停止' : label)}
        >
            {playing ? <Square size={13} /> : <Icon size={13} />}
        </button>
    );
}

// tool ボタン: addon ローカル endpoint に POST してから metadata 値の変化で
// 完了を検知する。再生成中は spinner を表示し、二重起動を防ぐ。
function ToolBubbleButton({
    btn,
    messageId,
    messageText,
    personaId,
    metaValue,
}: {
    btn: BubbleButtonDef;
    messageId: string;
    messageText?: string;
    personaId?: string;
    /** btn.metadata_key で参照される metadata の現在値。pack 側で URL に
     *  ``?v=<version>`` 等の cache-bust トークンを付けてもらえば、再生成完了を
     *  この値の変化で検知できる。 */
    metaValue: unknown;
}) {
    const [regenerating, setRegenerating] = useState(false);
    const [error, setError] = useState(false);
    // クリック時点の値を覚えて、変化を検知できるようにする
    const lastSeenValue = useRef<unknown>(metaValue);

    // metaValue が変化したら regenerating を解除 (pack 側の audio_ready 完了)
    useEffect(() => {
        if (regenerating && metaValue !== undefined && metaValue !== lastSeenValue.current) {
            setRegenerating(false);
            lastSeenValue.current = metaValue;
        }
    }, [metaValue, regenerating]);

    // タイムアウト保険: 5 分で自動解除 (SSE が来ない異常時の救済)。
    // TTS 合成は長文 + 重いモデル (GPT-SoVITS 等) で 60 秒以上かかるケースが
    // あるので余裕を持たせる。実合成完了時には audio_completed event で
    // metaValue が変化し、上の useEffect で正常に解除される。
    useEffect(() => {
        if (!regenerating) return;
        const timer = setTimeout(() => {
            setRegenerating(false);
            setError(true);
            console.warn(`[AddonBubbleButtons] tool ${btn.tool} timed out (300s)`);
        }, 300_000);
        return () => clearTimeout(timer);
    }, [regenerating, btn.tool]);

    const Icon = resolveIcon(btn.icon);

    const handleClick = async () => {
        if (regenerating || !btn.tool) return;
        setError(false);
        lastSeenValue.current = metaValue;
        setRegenerating(true);
        try {
            const res = await fetch(
                `/api/addon/${btn.addon_name}/${btn.tool}`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        message_id: messageId,
                        text: messageText ?? '',
                        persona_id: personaId ?? null,
                    }),
                },
            );
            if (!res.ok) {
                console.error(
                    `[AddonBubbleButtons] tool ${btn.tool} returned ${res.status}`,
                );
                setRegenerating(false);
                setError(true);
            }
            // 成功時は metadata 変化を待ってスピナー解除
        } catch (err) {
            console.error(
                `[AddonBubbleButtons] tool ${btn.tool} on ${btn.addon_name} failed:`,
                err,
            );
            setRegenerating(false);
            setError(true);
        }
    };

    return (
        <button
            className={`${styles.bubbleBtn} ${regenerating ? styles.pending : ''} ${error ? styles.error : ''}`}
            title={error ? '失敗しました' : (regenerating ? `${btn.label} (実行中)` : btn.label)}
            onClick={handleClick}
            disabled={regenerating}
        >
            {regenerating ? <Loader size={13} className={styles.spinner} /> : <Icon size={13} />}
        </button>
    );
}

export default function AddonBubbleButtons({
    messageId,
    messageText,
    personaId,
    addonMetadata,
    buttons,
}: AddonBubbleButtonsProps) {
    if (buttons.length === 0) return null;

    const visibleButtons = buttons.filter((btn) => {
        if (btn.show_when === 'metadata_exists' && btn.metadata_key) {
            const meta = addonMetadata[btn.addon_name];
            return meta && meta[btn.metadata_key] !== undefined;
        }
        // "always" またはshow_when未指定はメタデータなしでも表示
        return btn.show_when === 'always' || !btn.show_when;
    });

    // メタデータ待ち中のボタン（show_when: metadata_exists でまだ値がないもの）
    const pendingButtons = buttons.filter((btn) => {
        if (btn.show_when !== 'metadata_exists') return false;
        const meta = addonMetadata[btn.addon_name];
        return !meta || meta[btn.metadata_key ?? ''] === undefined;
    });

    return (
        <>
            {/* ローディング中のプレースホルダー */}
            {pendingButtons.map((btn) => (
                <button
                    key={`pending-${btn.addon_name}-${btn.id}`}
                    className={`${styles.bubbleBtn} ${styles.pending}`}
                    title={`${btn.label}（準備中）`}
                    disabled
                >
                    <Loader size={13} className={styles.spinner} />
                </button>
            ))}

            {/* 有効化済みのボタン */}
            {visibleButtons.map((btn) => {
                const meta = addonMetadata[btn.addon_name] ?? {};

                if (btn.action === 'play_audio' && btn.metadata_key) {
                    const audioUrl = meta[btn.metadata_key] as string | undefined;
                    if (audioUrl) {
                        return (
                            <PlayAudioButton
                                key={`${btn.addon_name}-${btn.id}`}
                                audioUrl={audioUrl}
                                label={btn.label}
                                icon={btn.icon}
                            />
                        );
                    }
                }

                // tool ボタン: addon ローカル endpoint に POST。再生成中は
                // metadata 値の変化を待ってスピナーを解除する (ToolBubbleButton)。
                if (btn.tool) {
                    const metaValue = btn.metadata_key ? meta[btn.metadata_key] : undefined;
                    return (
                        <ToolBubbleButton
                            key={`${btn.addon_name}-${btn.id}`}
                            btn={btn}
                            messageId={messageId}
                            messageText={messageText}
                            personaId={personaId}
                            metaValue={metaValue}
                        />
                    );
                }

                // tool も action も無いボタン (定義ミス) はクリックしてもエラーで沈黙させる
                const Icon = resolveIcon(btn.icon);
                return (
                    <button
                        key={`${btn.addon_name}-${btn.id}`}
                        className={styles.bubbleBtn}
                        title={btn.label}
                        disabled
                    >
                        <Icon size={13} />
                    </button>
                );
            })}
        </>
    );
}
