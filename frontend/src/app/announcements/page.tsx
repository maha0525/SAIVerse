"use client";

import { useEffect, useState } from 'react';
import { ArrowLeft, Info, AlertTriangle, AlertCircle, ExternalLink } from 'lucide-react';
import styles from './page.module.css';

interface Announcement {
    id: string;
    date: string;
    title: string;
    content: string;
    severity: 'info' | 'warning' | 'critical';
    link?: string;
}

/** Compute a simple hash string from announcements content for unread detection. */
function computeAnnouncementsHash(announcements: Announcement[]): string {
    const raw = JSON.stringify(announcements);
    // Simple djb2 hash → hex string
    let hash = 5381;
    for (let i = 0; i < raw.length; i++) {
        hash = ((hash << 5) + hash + raw.charCodeAt(i)) | 0;
    }
    return (hash >>> 0).toString(16);
}

function SeverityIcon({ severity }: { severity: string }) {
    switch (severity) {
        case 'critical':
            return <AlertCircle size={18} className={`${styles.severityIcon} ${styles.severityCritical}`} />;
        case 'warning':
            return <AlertTriangle size={18} className={`${styles.severityIcon} ${styles.severityWarning}`} />;
        default:
            return <Info size={18} className={`${styles.severityIcon} ${styles.severityInfo}`} />;
    }
}

function severityCardClass(severity: string): string {
    switch (severity) {
        case 'critical': return styles.cardCritical;
        case 'warning': return styles.cardWarning;
        default: return styles.cardInfo;
    }
}

export default function AnnouncementsPage() {
    const [announcements, setAnnouncements] = useState<Announcement[]>([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        fetch('/api/system/announcements')
            .then(res => res.ok ? res.json() : null)
            .then(data => {
                if (data?.announcements) {
                    // Sort by date descending (newest first)
                    const sorted = [...data.announcements].sort(
                        (a: Announcement, b: Announcement) => b.date.localeCompare(a.date)
                    );
                    setAnnouncements(sorted);

                    // Mark as read: save hash to localStorage
                    const hash = computeAnnouncementsHash(sorted);
                    localStorage.setItem('saiverse_announcements_hash', hash);
                }
            })
            .catch(() => { /* ignore */ })
            .finally(() => setLoading(false));
    }, []);

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <button
                    className={styles.backButton}
                    onClick={() => { window.location.href = '/'; }}
                >
                    <ArrowLeft size={16} />
                    戻る
                </button>
                <h1 className={styles.title}>お知らせ</h1>
                <div style={{ width: '80px' }} />
            </div>

            {loading && <div className={styles.loading}>読み込み中...</div>}

            {!loading && announcements.length === 0 && (
                <div className={styles.empty}>お知らせはありません</div>
            )}

            {!loading && announcements.length > 0 && (
                <div className={styles.list}>
                    {announcements.map(item => (
                        <div
                            key={item.id}
                            className={`${styles.card} ${severityCardClass(item.severity)}`}
                        >
                            <div className={styles.cardHeader}>
                                <SeverityIcon severity={item.severity} />
                                <h2 className={styles.cardTitle}>{item.title}</h2>
                                <span className={styles.cardDate}>{item.date}</span>
                            </div>
                            <p className={styles.cardContent}>{item.content}</p>
                            {item.link && (
                                <a
                                    href={item.link}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className={styles.cardLink}
                                >
                                    詳細を見る <ExternalLink size={14} />
                                </a>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
