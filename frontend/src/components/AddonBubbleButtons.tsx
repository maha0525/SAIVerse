"use client";

import React, { useRef } from 'react';
import { Play, Loader } from 'lucide-react';
import styles from './AddonBubbleButtons.module.css';

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
    /** { addon_name: { key: value } } 形式のメタデータ */
    addonMetadata: Record<string, Record<string, unknown>>;
    /** 有効なアドオンのバブルボタン定義一覧 */
    buttons: BubbleButtonDef[];
}

function PlayAudioButton({
    audioUrl,
    label,
}: {
    audioUrl: string;
    label: string;
}) {
    const audioRef = useRef<HTMLAudioElement | null>(null);
    const [playing, setPlaying] = React.useState(false);
    const [error, setError] = React.useState(false);

    const handleClick = async () => {
        if (error) {
            setError(false);
        }
        if (!audioRef.current) {
            audioRef.current = new Audio(audioUrl);
            audioRef.current.onended = () => setPlaying(false);
            audioRef.current.onerror = () => {
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
            } catch {
                setError(true);
            }
        }
    };

    return (
        <button
            className={`${styles.bubbleBtn} ${playing ? styles.playing : ''} ${error ? styles.error : ''}`}
            onClick={handleClick}
            title={error ? '音声の再生に失敗しました' : label}
        >
            <Play size={13} />
        </button>
    );
}

export default function AddonBubbleButtons({
    messageId,
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
                            />
                        );
                    }
                }

                // その他のアクション（拡張用プレースホルダー）
                return (
                    <button
                        key={`${btn.addon_name}-${btn.id}`}
                        className={styles.bubbleBtn}
                        title={btn.label}
                        onClick={async () => {
                            if (btn.tool) {
                                await fetch(
                                    `/api/addon/${btn.addon_name}/${btn.tool}?message_id=${encodeURIComponent(messageId)}`,
                                );
                            }
                        }}
                    >
                        <Play size={13} />
                    </button>
                );
            })}
        </>
    );
}
