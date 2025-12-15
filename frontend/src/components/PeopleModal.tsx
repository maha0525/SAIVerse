import React, { useEffect, useState } from 'react';
import styles from './PeopleModal.module.css';
import { X, UserPlus, RefreshCw } from 'lucide-react';

interface Persona {
    id: string;
    name: string;
    avatar: string;
    status: string;
}

interface PeopleModalProps {
    isOpen: boolean;
    onClose: () => void;
}

export default function PeopleModal({ isOpen, onClose }: PeopleModalProps) {
    const [personas, setPersonas] = useState<Persona[]>([]);
    const [loading, setLoading] = useState(false);
    const [summoningId, setSummoningId] = useState<string | null>(null);

    useEffect(() => {
        if (isOpen) {
            fetchSummonable();
        }
    }, [isOpen]);

    const fetchSummonable = async () => {
        setLoading(true);
        try {
            const res = await fetch('/api/people/summonable');
            if (res.ok) {
                const data = await res.json();
                setPersonas(data);
            }
        } catch (e) {
            console.error("Failed to fetch summonable personas", e);
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
                // Success! Close modal.
                // Optionally show toast or relying on chat system message
                onClose();
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

    if (!isOpen) return null;

    return (
        <div
            className={styles.overlay}
            onTouchStart={(e) => e.stopPropagation()}
            onTouchMove={(e) => e.stopPropagation()}
        >
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2><UserPlus className={styles.icon} size={24} /> Call Persona</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={24} /></button>
                </div>

                <div className={styles.content}>
                    {loading ? (
                        <div className={styles.loading}>
                            <RefreshCw className={styles.spinner} size={24} />
                            <span>Loading...</span>
                        </div>
                    ) : personas.length === 0 ? (
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
                                        <div className={styles.status}>Available</div>
                                    </div>
                                    <button className={styles.summonBtn} disabled={!!summoningId}>
                                        Summon
                                    </button>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
