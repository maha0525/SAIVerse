'use client';

import React from 'react';
import { X, Newspaper } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import EventsTimeline from './EventsTimeline';
import styles from './EventsModal.module.css';

/**
 * できごと (画面 A) をチャット画面の上にモーダルで開く。
 *
 * Sidebar の「できごと」からページ遷移 (/events) すると書きかけの
 * メッセージが消えるため、通常の導線はこのモーダルを使う。中身は
 * /events ページと同じ EventsTimeline。/events は直リンク用に残っている。
 */

interface EventsModalProps {
    isOpen: boolean;
    onClose: () => void;
    /** 開いた時点のペルソナ絞り込み (ライフビューの「今日のできごとを見る」用)。
        モーダルは閉じると unmount されるため、次に開く時にまた反映される。 */
    initialPersonaId?: string | null;
    /** 「いま」行のペルソナ名クリックでその子のライフビューを開く (EventsTimeline へ中継)。 */
    onOpenLifeView?: (persona: { id: string; name: string }) => void;
}

export default function EventsModal({ isOpen, onClose, initialPersonaId, onOpenLifeView }: EventsModalProps) {
    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 className={styles.title}><Newspaper size={18} /> できごと</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={22} /></button>
                </div>
                <div className={styles.body}>
                    <EventsTimeline initialPersonaId={initialPersonaId} onOpenLifeView={onOpenLifeView} />
                </div>
            </div>
        </ModalOverlay>
    );
}
