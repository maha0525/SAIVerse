
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
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
    /** 親 (ChatPage) が把握している現在 Building ID。
     * 省略すると server-global の user_current_building_id にフォールバックし、
     * マルチデバイス間で他クライアントの操作に details が汚染される (エリス上書き事故の遠因)。
     */
    currentBuildingId?: string | null;
    /** summon / dismiss が成功した直後に呼ばれる。 親 (ChatPage) が
     * moveTrigger を bump して RightSidebar / Sidebar の表示を即時更新するための callback。
     * 省略するとモーダル内 fetchData() だけが走り、 サイドバー類は
     * 10 秒ポーリングか building 切替まで古い表示のままになる。 */
    onChanged?: () => void;
}

export default function PeopleModal({ isOpen, onClose, currentBuildingId, onChanged }: PeopleModalProps) {
    useLocale();
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
        if (!currentBuildingId) {
            // 親 (ChatPage) が currentBuildingId を渡し忘れた場合の安全策。
            // server-global にフォールバックすると、マルチデバイスで他クライアントの
            // building が見える事故 (エリス上書き事故の遠因) になるため即 return。
            console.warn('[PeopleModal] fetchData skipped: currentBuildingId not provided');
            return;
        }
        setLoading(true);
        try {
            const [summonableRes, detailsRes] = await Promise.all([
                apiFetch(`/api/people/summonable?building_id=${encodeURIComponent(currentBuildingId)}`),
                apiFetch(`/api/info/details?building_id=${encodeURIComponent(currentBuildingId)}`)
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
        if (!currentBuildingId) {
            console.warn('[PeopleModal] handleSummon skipped: currentBuildingId not provided');
            return;
        }
        setSummoningId(personaId);
        try {
            const res = await apiFetch(
                `/api/people/summon/${personaId}?building_id=${encodeURIComponent(currentBuildingId)}`,
                { method: 'POST' }
            );
            if (res.ok) {
                // Refresh data to update lists
                fetchData();
                onChanged?.();
            } else {
                const err = await res.json();
                alert(uiText("components.PeopleModal.text001", { p1: err.detail }));
            }
        } catch (e) {
            console.error("Summon failed", e);
            alert(uiText("components.PeopleModal.text002"));
        } finally {
            setSummoningId(null);
        }
    };

    const handleDismiss = async (personaId: string, name: string) => {
        if (!currentBuildingId) {
            console.warn('[PeopleModal] handleDismiss skipped: currentBuildingId not provided');
            return;
        }
        if (!confirm(uiText("components.PeopleModal.text003", { p1: name }))) return;

        setDismissingId(personaId);
        try {
            const res = await apiFetch(
                `/api/people/dismiss/${personaId}?building_id=${encodeURIComponent(currentBuildingId)}`,
                { method: 'POST' }
            );
            if (res.ok) {
                // Refresh data to update lists
                fetchData();
                onChanged?.();
            } else {
                const err = await res.json();
                alert(uiText("components.PeopleModal.text004", { p1: err.detail }));
            }
        } catch (e) {
            console.error("Dismiss failed", e);
            alert(uiText("components.PeopleModal.text005"));
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
                    <h2 data-i18n="components.PeopleModal.text006"><UserPlus className={styles.icon} size={24} />{uiText("components.PeopleModal.text006")}</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={24} /></button>
                </div>

                {/* Tab Switcher */}
                <div className={styles.tabs}>
                    <button data-i18n="components.PeopleModal.text007"
                        className={`${styles.tab} ${activeTab === 'call' ? styles.active : ''}`}
                        onClick={() => setActiveTab('call')}
                    >
                        <UserPlus size={16} />{uiText("components.PeopleModal.text007")}{personas.length})
                    </button>
                    <button data-i18n="components.PeopleModal.text008"
                        className={`${styles.tab} ${activeTab === 'here' ? styles.active : ''}`}
                        onClick={() => setActiveTab('here')}
                    >
                        <Home size={16} />{uiText("components.PeopleModal.text008")}{occupants.length})
                    </button>
                </div>

                <div className={styles.content}>
                    {loading ? (
                        <div className={styles.loading}>
                            <RefreshCw className={styles.spinner} size={24} />
                            <span data-i18n="components.PeopleModal.text009">{uiText("components.PeopleModal.text009")}</span>
                        </div>
                    ) : activeTab === 'call' ? (
                        // Call tab - summonable personas
                        personas.length === 0 ? (
                            <div className={styles.empty}>
                                <p data-i18n="components.PeopleModal.text010">{uiText("components.PeopleModal.text010")}</p>
                                <span data-i18n="components.PeopleModal.text011" className={styles.subtext}>{uiText("components.PeopleModal.text011")}</span>
                            </div>
                        ) : (
                            <div className={styles.grid}>
                                {personas.map(p => (
                                    <div key={p.id} className={styles.card} onClick={() => handleSummon(p.id, p.name)}>
                                        <div className={styles.avatarWrapper}>
                                            <img src={p.avatar || "/api/static/icons/host.png"} alt={p.name} className={styles.avatar} />
                                            {summoningId === p.id && (
                                                <div className={styles.summoningOverlay}>
                                                    <RefreshCw className={styles.spinner} size={20} />
                                                </div>
                                            )}
                                        </div>
                                        <div className={styles.info}>
                                            <div className={styles.name}>{p.name}</div>
                                            <div data-i18n="components.PeopleModal.text012" className={styles.status}>{uiText("components.PeopleModal.text012")}</div>
                                        </div>
                                        <button data-i18n="components.PeopleModal.text013" className={styles.summonBtn} disabled={!!summoningId}>{uiText("components.PeopleModal.text013")}</button>
                                    </div>
                                ))}
                            </div>
                        )
                    ) : (
                        // Here tab - current occupants
                        occupants.length === 0 ? (
                            <div className={styles.empty}>
                                <p data-i18n="components.PeopleModal.text014">{uiText("components.PeopleModal.text014")}</p>
                                <span data-i18n="components.PeopleModal.text015" className={styles.subtext}>{uiText("components.PeopleModal.text015")}</span>
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
                                            <div data-i18n="components.PeopleModal.text016" className={styles.status}>{uiText("components.PeopleModal.text016")}</div>
                                        </div>
                                        <button data-i18n="components.PeopleModal.text017"
                                            className={styles.dismissBtn}
                                            onClick={() => handleDismiss(p.id, p.name)}
                                            disabled={!!dismissingId}
                                            title={uiText("components.PeopleModal.text017")}
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
