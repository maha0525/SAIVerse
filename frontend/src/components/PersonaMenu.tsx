import React, { useState } from 'react';
import styles from './PersonaMenu.module.css';
import { Home, Brain, Calendar, CheckSquare, Settings, X, RefreshCw, Network, Package } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';

interface PersonaMenuProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
    personaName: string;
    avatarUrl: string;
    onOpenMemory?: () => void;
    onOpenSchedule?: () => void;
    onOpenTasks?: () => void;
    onOpenSettings?: () => void;
    onOpenInventory?: () => void;
}

export default function PersonaMenu({ isOpen, onClose, personaId, personaName, avatarUrl, onOpenMemory, onOpenSchedule, onOpenTasks, onOpenSettings, onOpenInventory }: PersonaMenuProps) {
    const [loading, setLoading] = useState(false);

    if (!isOpen) return null;

    const handleDismiss = async () => {
        if (!confirm(`${personaName}を自室に戻しますか？`)) return;

        setLoading(true);
        try {
            const res = await fetch(`/api/people/dismiss/${personaId}`, { method: 'POST' });
            if (res.ok) {
                const data = await res.json();
                // Close menu
                onClose();
            } else {
                const err = await res.json();
                alert(`Failed to dismiss: ${err.detail}`);
            }
        } catch (e) {
            console.error(e);
            alert("Error communicating with server.");
        } finally {
            setLoading(false);
        }
    };

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.menu} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <div className={styles.profile}>
                        <img src={avatarUrl} alt={personaName} className={styles.avatar} />
                        <div className={styles.nameWrapper}>
                            <h3 className={styles.name}>{personaName}</h3>
                            <span className={styles.idDisplay}>@{personaId.split('-')[0]}</span>
                        </div>
                    </div>
                    <button className={styles.closeBtn} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.actions}>
                    <button className={styles.actionBtn} onClick={handleDismiss} disabled={loading}>
                        {loading ? <RefreshCw className={styles.spin} size={20} /> : <Home size={20} />}
                        <div className={styles.label}>
                            <span>Return to Room</span>
                            <span className={styles.subtext}>自室に戻す</span>
                        </div>
                    </button>

                    <button
                        className={`${styles.actionBtn} ${!onOpenMemory ? styles.disabled : ''}`}
                        onClick={() => {
                            if (onOpenMemory) {
                                onOpenMemory();
                                onClose(); // Close menu when opening modal
                            }
                        }}
                    >
                        <Brain size={20} />
                        <div className={styles.label}>
                            <span>Memory</span>
                            <span className={styles.subtext}>長期記憶 & Memopedia</span>
                        </div>
                    </button>

                    <button
                        className={`${styles.actionBtn} ${!onOpenInventory ? styles.disabled : ''}`}
                        onClick={() => {
                            if (onOpenInventory) {
                                onOpenInventory();
                                onClose();
                            }
                        }}
                    >
                        <Package size={20} />
                        <div className={styles.label}>
                            <span>Inventory</span>
                            <span className={styles.subtext}>所持品</span>
                        </div>
                    </button>

                    <button
                        className={`${styles.actionBtn} ${!onOpenSchedule ? styles.disabled : ''}`}
                        onClick={() => {
                            if (onOpenSchedule) {
                                onOpenSchedule();
                                onClose();
                            }
                        }}
                    >
                        <Calendar size={20} />
                        <div className={styles.label}>
                            <span>Schedule</span>
                            <span className={styles.subtext}>スケジュール管理</span>
                        </div>
                    </button>

                    <button
                        className={`${styles.actionBtn} ${!onOpenTasks ? styles.disabled : ''}`}
                        onClick={() => {
                            if (onOpenTasks) {
                                onOpenTasks();
                                onClose();
                            }
                        }}
                    >
                        <Network size={20} />
                        <div className={styles.label}>
                            <span>Tasks</span>
                            <span className={styles.subtext}>タスク管理</span>
                        </div>
                    </button>

                    <button
                        className={`${styles.actionBtn} ${!onOpenSettings ? styles.disabled : ''}`}
                        onClick={() => {
                            if (onOpenSettings) {
                                onOpenSettings();
                                onClose();
                            }
                        }}
                    >
                        <Settings size={20} />
                        <div className={styles.label}>
                            <span>Settings</span>
                            <span className={styles.subtext}>AI設定</span>
                        </div>
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
