import React, { useState } from 'react';
import { X, MessageSquare, Download, Book, Bug, Layers, Activity, Anchor, Footprints, Notebook } from 'lucide-react';
import styles from './MemoryModal.module.css';
import MemoryBrowser from './memory/MemoryBrowser';
import MemoryImport from './memory/MemoryImport';
import MemopediaViewer from './memory/MemopediaViewer';
import MemoryRecall from './memory/MemoryRecall';
import CoreMemoryScene from './memory/CoreMemoryScene';
import ArasujiViewer from './memory/ArasujiViewer';
import ExperienceLedgerViewer from './memory/ExperienceLedgerViewer';
import PocketbookViewer from './memory/PocketbookViewer';
import PulseTimelineViewer from './memory/PulseTimelineViewer';
import ModalOverlay from './common/ModalOverlay';

interface MemoryModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
    personaName?: string;
}

type Tab = 'browser' | 'core_memory' | 'pocketbook' | 'arasuji' | 'memopedia' | 'experience' | 'pulse_timeline' | 'import' | 'debug';

export default function MemoryModal({ isOpen, onClose, personaId, personaName }: MemoryModalProps) {
    const [activeTab, setActiveTab] = useState<Tab>('browser');

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 className={styles.title}>{personaName || personaId} のメモリー</h2>
                    <button className={styles.closeButton} onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                <div className={styles.tabs}>
                    <button
                        className={`${styles.tab} ${activeTab === 'browser' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('browser')}
                    >
                        <MessageSquare size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        チャットログ
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'core_memory' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('core_memory')}
                    >
                        <Anchor size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        コア記憶
                    </button>
                    {/* 手帳 (autonomous_behavior_v3.md §13)。ペルソナが自分で書く
                        アクティビティ・メモと、引き受けている約束 (タスク帳) の
                        読み口。v0.3 は読むだけ。 */}
                    <button
                        className={`${styles.tab} ${activeTab === 'pocketbook' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('pocketbook')}
                    >
                        <Notebook size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        手帳
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'arasuji' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('arasuji')}
                    >
                        <Layers size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        Chronicle
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'memopedia' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('memopedia')}
                    >
                        <Book size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        Memopedia
                    </button>
                    {/* 経験の台帳 (experience_ledger.md §3)。コア記憶タブ拡張でなく
                        新タブ: コア記憶=常駐・編集可の実データ / 台帳=参照専用の
                        動的合成ビュー、という性質差を画面でも分ける。 */}
                    <button
                        className={`${styles.tab} ${activeTab === 'experience' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('experience')}
                    >
                        <Footprints size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        経験
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'pulse_timeline' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('pulse_timeline')}
                    >
                        <Activity size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        Pulse タイムライン
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'import' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('import')}
                    >
                        <Download size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        インポート
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'debug' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('debug')}
                    >
                        <Bug size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        デバッグ
                    </button>
                </div>

                <div className={styles.content}>
                    {activeTab === 'browser' && <MemoryBrowser personaId={personaId} />}
                    {activeTab === 'core_memory' && <CoreMemoryScene personaId={personaId} />}
                    {activeTab === 'pocketbook' && <PocketbookViewer personaId={personaId} />}
                    {activeTab === 'arasuji' && <ArasujiViewer personaId={personaId} />}
                    {activeTab === 'memopedia' && <MemopediaViewer personaId={personaId} />}
                    {activeTab === 'experience' && <ExperienceLedgerViewer personaId={personaId} />}
                    {activeTab === 'pulse_timeline' && <PulseTimelineViewer personaId={personaId} />}
                    {activeTab === 'import' && <MemoryImport personaId={personaId} />}
                    {activeTab === 'debug' && <MemoryRecall personaId={personaId} />}
                </div>
            </div>
        </ModalOverlay>
    );
}
