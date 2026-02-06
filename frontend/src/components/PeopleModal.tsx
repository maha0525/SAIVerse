import React, { useEffect, useState } from 'react';
import styles from './PeopleModal.module.css';
import { X, UserPlus, RefreshCw, Home } from 'lucide-react';

interface Persona {
    id: string;
    name: string;
    avatar: string;
    status: string;
}

interface Occupant {
    id: string;
    name: string;
    avatar?: string;
}

interface PeopleModalProps {
    isOpen: boolean;
    onClose: () => void;
}

export default function PeopleModal({ isOpen, onClose }: PeopleModalProps) {
    const [personas, setPersonas] = useState<Persona[]>([]);
    const [occupants, setOccupants] = useState<Occupant[]>([]);
    const [loading, setLoading] = useState(false);
    const [summoningId, setSummoningId] = useState<string | null>(null);
    const [dismissingId, setDismissingId] = useState<string | null>(null);
    const [activeTab, setActiveTab] = useState<'call' | 'here'>('call');

    useEffect(() => {
        if (isOpen) {
            fetchData();
        }
    }, [isOpen]);

    const fetchData = async () => {
        setLoading(true);
        try {
            const [summonableRes, detailsRes] = await Promise.all([
                fetch('/api/people/summonable'),
                fetch('/api/info/details')
            ]);
            if (summonableRes.ok) {
                const data = await summonableRes.json();
                setPersonas(data);
            }
            if (detailsRes.ok) {
                const data = await detailsRes.json();
                // Filter out user (non-AI occupants) - only show personas
                setOccupants(data.occupants || []);
            }
        } catch (e) {
            console.error("Failed to fetch data", e);
        } finally {
            setLoading(false);
        }
    };

    const handleSummon = async (personaId: string, name: string) => {
        setSummoningId(personaId);
        try {
            const res = await fetch(`/api/people/summon/${personaId}`, {
                method: 'POST'
            });
            if (res.ok) {
                // Refresh data to update lists
                fetchData();
            } else {
                const err = await res.json();
                alert(`召喚に失敗しました: ${err.detail}`);
            }
        } catch (e) {
            console.error("Summon failed", e);
            alert("召喚中にエラーが発生しました。");
        } finally {
            setSummoningId(null);
        }
    };

    const handleDismiss = async (personaId: string, name: string) => {
        if (!confirm(`${name}を自室に戻しますか？`)) return;

        setDismissingId(personaId);
        try {
            const res = await fetch(`/api/people/dismiss/${personaId}`, {
                method: 'POST'
            });
            if (res.ok) {
                // Refresh data to update lists
                fetchData();
            } else {
                const err = await res.json();
                alert(`戻すのに失敗しました: ${err.detail}`);
            }
        } catch (e) {
            console.error("Dismiss failed", e);
            alert("処理中にエラーが発生しました。");
        } finally {
            setDismissingId(null);
        }
    };

    if (!isOpen) return null;

    return (
        <div
            className={styles.overlay}
            onTouchStart={(e) => e.stopPropagation()}
            onTouchMove={(e) => e.stopPropagation()}
        >
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2><UserPlus className={styles.icon} size={24} /> ペルソナ管理</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={24} /></button>
                </div>

                {/* Tab Switcher */}
                <div className={styles.tabs}>
                    <button
                        className={`${styles.tab} ${activeTab === 'call' ? styles.active : ''}`}
                        onClick={() => setActiveTab('call')}
                    >
                        <UserPlus size={16} />
                        呼び出し ({personas.length})
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'here' ? styles.active : ''}`}
                        onClick={() => setActiveTab('here')}
                    >
                        <Home size={16} />
                        帰ってもらう ({occupants.length})
                    </button>
                </div>

                <div className={styles.content}>
                    {loading ? (
                        <div className={styles.loading}>
                            <RefreshCw className={styles.spinner} size={24} />
                            <span>読み込み中...</span>
                        </div>
                    ) : activeTab === 'call' ? (
                        // Call tab - summonable personas
                        personas.length === 0 ? (
                            <div className={styles.empty}>
                                <p>呼び出せる住人がいません。</p>
                                <span className={styles.subtext}>みんな忙しいか、既にここにいます。</span>
                            </div>
                        ) : (
                            <div className={styles.grid}>
                                {personas.map(p => (
                                    <div key={p.id} className={styles.card} onClick={() => handleSummon(p.id, p.name)}>
                                        <div className={styles.avatarWrapper}>
                                            <img src={p.avatar} alt={p.name} className={styles.avatar} />
                                            {summoningId === p.id && (
                                                <div className={styles.summoningOverlay}>
                                                    <RefreshCw className={styles.spinner} size={20} />
                                                </div>
                                            )}
                                        </div>
                                        <div className={styles.info}>
                                            <div className={styles.name}>{p.name}</div>
                                            <div className={styles.status}>呼び出し可能</div>
                                        </div>
                                        <button className={styles.summonBtn} disabled={!!summoningId}>
                                            呼ぶ
                                        </button>
                                    </div>
                                ))}
                            </div>
                        )
                    ) : (
                        // Here tab - current occupants
                        occupants.length === 0 ? (
                            <div className={styles.empty}>
                                <p>ここには誰もいません。</p>
                                <span className={styles.subtext}>「呼び出し」タブからペルソナを呼び出しましょう。</span>
                            </div>
                        ) : (
                            <div className={styles.grid}>
                                {occupants.map(p => (
                                    <div key={p.id} className={styles.card}>
                                        <div className={styles.avatarWrapper}>
                                            <img
                                                src={p.avatar || "/api/static/icons/host.png"}
                                                alt={p.name}
                                                className={styles.avatar}
                                            />
                                            {dismissingId === p.id && (
                                                <div className={styles.summoningOverlay}>
                                                    <RefreshCw className={styles.spinner} size={20} />
                                                </div>
                                            )}
                                        </div>
                                        <div className={styles.info}>
                                            <div className={styles.name}>{p.name}</div>
                                            <div className={styles.status}>滞在中</div>
                                        </div>
                                        <button
                                            className={styles.dismissBtn}
                                            onClick={() => handleDismiss(p.id, p.name)}
                                            disabled={!!dismissingId}
                                            title="自室に戻す"
                                        >
                                            <Home size={16} />
                                        </button>
                                    </div>
                                ))}
                            </div>
                        )
                    )}
                </div>
            </div>
        </div>
    );
}
