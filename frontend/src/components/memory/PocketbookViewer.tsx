
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
// 手帳 (アクティビティ + メモ) とタスク帳の読み口。
// 正典: docs/intent/autonomous_behavior_v3.md §13「手帳とタスク帳の読み口 (v0.3)」。
//
// 目的は「見えないと直せない」— 手帳とタスク帳はペルソナが自分で書く自己像の
// 一部で、スルースのたびに本人が読み返す。ユーザーから見えないと、間違った
// 約束や的外れな「やりたいこと」を抱え込んでも気づけない。
//
// v0.3 は読むだけ。編集・削除ボタンは置かない (訂正の口は v0.4 で、手帳の器の
// 作り直しとタスク帳の CAS との整合を決めてから)。
import React, { useState, useEffect } from 'react';
import { Notebook, Handshake, CalendarClock, User } from 'lucide-react';
import styles from './PocketbookViewer.module.css';

interface PocketbookMemo {
    id: number;
    date: string;                       // 'YYYY-MM-DD'
    kind: 'did' | 'want' | string;
    text: string;
    span_start_id: string | null;
    span_end_id: string | null;
}

interface PocketbookActivity {
    id: number;
    name: string;
    status: 'open' | 'closed' | string;
    origin: string;
    born_at: number;
    closed_at: number | null;
    last_memo_date: string | null;
    memos: PocketbookMemo[];
}

interface TaskBookItem {
    task_id: string;
    persona_id: string;
    content: string;
    due_at: number | null;
    counterpart: string | null;
    origin: string;
    status: string;
}

interface PocketbookViewerProps {
    personaId: string;
}

const MEMO_KIND_LABEL: Record<string, string> = {
    get want() { return uiText("components.memory.PocketbookViewer.text001"); },
    get did() { return uiText("components.memory.PocketbookViewer.text002"); },
};

// アクティビティの出自 (activities.origin の閉語彙)。
const ORIGIN_LABEL: Record<string, string> = {
    get sluice() { return uiText("components.memory.PocketbookViewer.text003"); },
    get user() { return uiText("components.memory.PocketbookViewer.text004"); },
    get initial() { return uiText("components.memory.PocketbookViewer.text005"); },
    get migration() { return uiText("components.memory.PocketbookViewer.text006"); },
};

const TASK_STATUS_LABEL: Record<string, string> = {
    get open() { return uiText("components.memory.PocketbookViewer.text007"); },
    get done() { return uiText("components.memory.PocketbookViewer.text008"); },
    get withdrawn() { return uiText("components.memory.PocketbookViewer.text009"); },
};

// 相手 (counterpart) は 'user' / ペルソナ ID / 'system' 等。既知の値だけ訳す。
const COUNTERPART_LABEL: Record<string, string> = {
    get user() { return uiText("components.memory.PocketbookViewer.text010"); },
    get system() { return uiText("components.memory.PocketbookViewer.text011"); },
};

function epochToDate(epoch: number | null): string | null {
    if (!epoch) return null;
    const d = new Date(epoch * 1000);
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
}

export default function PocketbookViewer({ personaId }: PocketbookViewerProps) {
    useLocale();
    const [activities, setActivities] = useState<PocketbookActivity[]>([]);
    const [tasks, setTasks] = useState<TaskBookItem[]>([]);
    const [includeClosed, setIncludeClosed] = useState(false);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        const load = async () => {
            setIsLoading(true);
            setError(null);
            try {
                const [pbRes, tbRes] = await Promise.all([
                    apiFetch(
                        `/api/people/${personaId}/pocketbook?include_closed=${includeClosed}`
                    ),
                    apiFetch(`/api/people/${personaId}/task-book`),
                ]);
                if (!pbRes.ok) throw new Error(`HTTP ${pbRes.status}`);
                if (!tbRes.ok) throw new Error(`HTTP ${tbRes.status}`);
                const pbData = await pbRes.json();
                const tbData = await tbRes.json();
                if (cancelled) return;
                setActivities(pbData.activities || []);
                setTasks(tbData.tasks || []);
            } catch (e) {
                if (!cancelled) setError(uiText("components.memory.PocketbookViewer.text012", { p1: e }));
            } finally {
                if (!cancelled) setIsLoading(false);
            }
        };
        load();
        return () => { cancelled = true; };
    }, [personaId, includeClosed]);

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <Notebook size={16} />
                <span data-i18n="components.memory.PocketbookViewer.text013">{uiText("components.memory.PocketbookViewer.text013")}</span>
            </div>

            {isLoading && <div data-i18n="components.memory.PocketbookViewer.text014" className={styles.notice}>{uiText("components.memory.PocketbookViewer.text014")}</div>}
            {error && <div className={styles.error}>{error}</div>}

            {!isLoading && !error && (
                <>
                    <section className={styles.section}>
                        <div className={styles.sectionHeader}>
                            <h4 data-i18n="components.memory.PocketbookViewer.text015" className={styles.sectionTitle}>
                                <Notebook size={14} />{uiText("components.memory.PocketbookViewer.text015")}</h4>
                            <label data-i18n="components.memory.PocketbookViewer.text016" className={styles.toggle}>
                                <input
                                    type="checkbox"
                                    checked={includeClosed}
                                    onChange={(e) => setIncludeClosed(e.target.checked)}
                                />{uiText("components.memory.PocketbookViewer.text016")}</label>
                        </div>

                        {activities.length === 0 ? (
                            <div data-i18n="components.memory.PocketbookViewer.text017" className={styles.emptyNote}>{uiText("components.memory.PocketbookViewer.text017")}</div>
                        ) : (
                            <ul className={styles.activityList}>
                                {activities.map((a) => (
                                    <li
                                        key={a.id}
                                        className={`${styles.activityCard} ${a.status === 'closed' ? styles.activityClosed : ''}`}
                                    >
                                        <div className={styles.activityHead}>
                                            <span className={styles.activityName}>{a.name}</span>
                                            <span data-i18n="components.memory.PocketbookViewer.text018 components.memory.PocketbookViewer.text019" className={styles.badge}>
                                                {a.status === 'closed' ? uiText("components.memory.PocketbookViewer.text018") : uiText("components.memory.PocketbookViewer.text019")}
                                            </span>
                                            <span className={styles.badgeMuted}>
                                                {ORIGIN_LABEL[a.origin] || a.origin}
                                            </span>
                                        </div>
                                        {a.memos.length === 0 ? (
                                            <div data-i18n="components.memory.PocketbookViewer.text020" className={styles.emptyNote}>{uiText("components.memory.PocketbookViewer.text020")}</div>
                                        ) : (
                                            <ul className={styles.memoList}>
                                                {a.memos.map((m) => (
                                                    <li key={m.id} className={styles.memoItem}>
                                                        <span className={styles.memoDate}>{m.date}</span>
                                                        <span
                                                            className={`${styles.memoKind} ${m.kind === 'want' ? styles.memoKindWant : styles.memoKindDid}`}
                                                        >
                                                            {MEMO_KIND_LABEL[m.kind] || m.kind}
                                                        </span>
                                                        <span className={styles.memoText}>{m.text}</span>
                                                    </li>
                                                ))}
                                            </ul>
                                        )}
                                    </li>
                                ))}
                            </ul>
                        )}
                    </section>

                    <section className={styles.section}>
                        <h4 data-i18n="components.memory.PocketbookViewer.text021" className={styles.sectionTitle}>
                            <Handshake size={14} />{uiText("components.memory.PocketbookViewer.text021")}</h4>
                        {tasks.length === 0 ? (
                            <div data-i18n="components.memory.PocketbookViewer.text022" className={styles.emptyNote}>{uiText("components.memory.PocketbookViewer.text022")}</div>
                        ) : (
                            <ul className={styles.taskList}>
                                {tasks.map((t) => (
                                    <li key={t.task_id} className={styles.taskItem}>
                                        <span className={styles.taskContent}>{t.content}</span>
                                        <div className={styles.taskMeta}>
                                            <span data-i18n="components.memory.PocketbookViewer.text023" className={styles.taskMetaItem}>
                                                <CalendarClock size={12} />
                                                {epochToDate(t.due_at) || uiText("components.memory.PocketbookViewer.text023")}
                                            </span>
                                            {t.counterpart && (
                                                <span className={styles.taskMetaItem}>
                                                    <User size={12} />
                                                    {COUNTERPART_LABEL[t.counterpart] || t.counterpart}
                                                </span>
                                            )}
                                            <span className={styles.badgeMuted}>
                                                {TASK_STATUS_LABEL[t.status] || t.status}
                                            </span>
                                        </div>
                                    </li>
                                ))}
                            </ul>
                        )}
                    </section>
                </>
            )}
        </div>
    );
}
