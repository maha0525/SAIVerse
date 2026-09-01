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

    // 「溜まった会話をあらすじにまとめる」— あらすじタブの生成ボタンと同じ背景
    // ジョブ (mode 既定 = compaction) を開始する。旧 organize-memory API は同期で
    // 畳み全体を待つ作りで、長走行するとフロントが先に切れて「通信に失敗」の誤報を
    // 出していたため 2026-09-01 に撤去した。
    // 結果はここでは追わない — ジョブの進捗・完了・エラー案内は記憶モーダルの
    // 「あらすじ」タブ (ArasujiViewer) がポーリングして表示する。
    const handleOrganizeMemory = async () => {
        if (!confirm(`${personaName}の溜まった会話をあらすじにまとめますか？\n古い側の会話があらすじ（Chronicle）に畳まれ、長期記憶になります。直近の会話はそのまま残ります。`)) return;

        setOrganizing(true);
        try {
            const res = await fetch(`/api/people/${personaId}/arasuji/generate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            if (res.ok) {
                alert('溜まった会話をあらすじにまとめ始めました。進捗は記憶モーダルの「あらすじ」タブで確認できます。');
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
                            <span>溜まった会話をあらすじにまとめる</span>
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
