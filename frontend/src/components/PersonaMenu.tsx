import React, { useState } from 'react';
import styles from './PersonaMenu.module.css';
import { Home, Brain, AlarmClock, Settings, X, RefreshCw, Package, Sparkles } from 'lucide-react';
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
    onOpenMemory?: () => void;
    onOpenSchedule?: () => void;
    onOpenSettings?: () => void;
    onOpenInventory?: () => void;
    /** dismiss 成功直後に呼ばれる。 親 (RightSidebar → ChatPage) が
     * 滞在ペルソナ表示を即時更新するための callback。 省略すると
     * 10 秒ポーリングか building 切替まで古い表示のままになる。 */
    onDismissed?: () => void;
}

export default function PersonaMenu({ isOpen, onClose, personaId, personaName, avatarUrl, buildingId, onOpenMemory, onOpenSchedule, onOpenSettings, onOpenInventory, onDismissed }: PersonaMenuProps) {
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
        if (!confirm(`${personaName}の記憶を整理しますか？\n古い会話履歴があらすじ（Chronicle）に畳まれます。直近の会話はそのまま残ります。`)) return;

        setOrganizing(true);
        try {
            const res = await fetch(`/api/people/${personaId}/organize-memory`, { method: 'POST' });
            if (res.ok) {
                const data = await res.json();
                const messages: Record<string, string> = {
                    ok: '記憶の整理が完了しました',
                    noop: '整理できる履歴がまだありません',
                    failed: '記憶の整理に失敗しました（あらすじ生成が完了しませんでした）。もう一度実行すると再試行できます。',
                    deferred: '別の整理が同じ範囲を処理中または処理済みです。しばらく待って再実行してください。',
                    deferred_sluice_unseen: '記憶の整理を見送りました（今回の採取で読めていない範囲があったため、畳みは次回の整理で続きから進みます）。',
                    disabled: 'Chronicle生成が無効のため整理できません（ペルソナ設定で「Chronicle 自動生成」を「有効」にしてください）',
                    unavailable: '整理できる状態ではありません（会話履歴がまだ無い可能性があります）',
                };
                alert(messages[data.compaction] ?? '記憶の整理の結果が不明です');
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
                        <AlarmClock size={20} />
                        <div className={styles.label}>
                            <span>Alarm</span>
                            <span className={styles.subtext}>アラーム管理</span>
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
