import React, { useState } from 'react';
import styles from './PersonaMenu.module.css';
import { Home, Brain, Calendar, Settings, X, RefreshCw, Network, Package, Sparkles, Activity, Heart } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';

interface PersonaMenuProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
    personaName: string;
    avatarUrl: string;
    /** dismiss 操作で対象とする building (= この persona がいる部屋)。
     * C-1 閲覧モード以降、 サーバ side の user_current_building_id だけに頼ると
     * viewing 中の部屋で帰ってもらえなくなるため明示する。
     */
    buildingId?: string | null;
    /** ライフビュー (自律行動の観察面) を開く。persona_activity_view.md §4 */
    onOpenLifeView?: () => void;
    /** プロフィール (この子はどんな子？) を開く。life_concept_map.md §15 */
    onOpenProfile?: () => void;
    onOpenMemory?: () => void;
    onOpenSchedule?: () => void;
    onOpenTasks?: () => void;
    onOpenSettings?: () => void;
    onOpenInventory?: () => void;
    /** dismiss 成功直後に呼ばれる。 親 (RightSidebar → ChatPage) が
     * 滞在ペルソナ表示を即時更新するための callback。 省略すると
     * 10 秒ポーリングか building 切替まで古い表示のままになる。 */
    onDismissed?: () => void;
}

export default function PersonaMenu({ isOpen, onClose, personaId, personaName, avatarUrl, buildingId, onOpenLifeView, onOpenProfile, onOpenMemory, onOpenSchedule, onOpenTasks, onOpenSettings, onOpenInventory, onDismissed }: PersonaMenuProps) {
    const [loading, setLoading] = useState(false);
    const [organizing, setOrganizing] = useState(false);

    if (!isOpen) return null;

    const handleDismiss = async () => {
        if (!confirm(`${personaName}を自室に戻しますか？`)) return;

        setLoading(true);
        try {
            const url = buildingId
                ? `/api/people/dismiss/${personaId}?building_id=${encodeURIComponent(buildingId)}`
                : `/api/people/dismiss/${personaId}`;
            const res = await fetch(url, { method: 'POST' });
            if (res.ok) {
                const data = await res.json();
                onDismissed?.();
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

    const handleOrganizeMemory = async () => {
        if (!confirm(`${personaName}の記憶を整理しますか？\n会話履歴のキャッシュがリセットされ、Chronicleが生成されます。`)) return;

        setOrganizing(true);
        try {
            const res = await fetch(`/api/people/${personaId}/organize-memory`, { method: 'POST' });
            if (res.ok) {
                const data = await res.json();
                const msg = data.chronicle_generated
                    ? '記憶の整理が完了しました（Chronicle生成済み）'
                    : '記憶の整理が完了しました';
                alert(msg);
            } else {
                const err = await res.json();
                alert(`失敗: ${err.detail}`);
            }
        } catch (e) {
            console.error(e);
            alert("サーバーとの通信に失敗しました。");
        } finally {
            setOrganizing(false);
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
                    <button
                        className={`${styles.actionBtn} ${!onOpenLifeView ? styles.disabled : ''}`}
                        onClick={() => {
                            if (onOpenLifeView) {
                                onOpenLifeView();
                                // onOpenLifeView 側で menu を閉じる (selectedPersona を引き継ぐため)
                            }
                        }}
                    >
                        <Activity size={20} />
                        <div className={styles.label}>
                            <span>Life View</span>
                            <span className={styles.subtext}>ようすを見る・自律行動</span>
                        </div>
                    </button>

                    <button
                        className={`${styles.actionBtn} ${!onOpenProfile ? styles.disabled : ''}`}
                        onClick={() => {
                            if (onOpenProfile) {
                                onOpenProfile();
                                onClose(); // Close menu when opening modal
                            }
                        }}
                    >
                        <Heart size={20} />
                        <div className={styles.label}>
                            <span>Profile</span>
                            <span className={styles.subtext}>この子はどんな子？</span>
                        </div>
                    </button>

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

                    <button className={styles.actionBtn} onClick={handleOrganizeMemory} disabled={organizing}>
                        {organizing ? <RefreshCw className={styles.spin} size={20} /> : <Sparkles size={20} />}
                        <div className={styles.label}>
                            <span>Organize Memory</span>
                            <span className={styles.subtext}>記憶を整理</span>
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
