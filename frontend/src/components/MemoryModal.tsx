import React, { useState } from 'react';
import { X, MessageSquare, Download, Book, Bug, Layers, Activity, Boxes, GitBranch } from 'lucide-react';
import styles from './MemoryModal.module.css';
import MemoryBrowser from './memory/MemoryBrowser';
import MemoryImport from './memory/MemoryImport';
import MemopediaViewer from './memory/MemopediaViewer';
import MemoryRecall from './memory/MemoryRecall';
import ArasujiViewer from './memory/ArasujiViewer';
import PulseLogsViewer from './memory/PulseLogsViewer';
import StorageLayersViewer from './memory/StorageLayersViewer';
import TracksViewer from './memory/TracksViewer';
import ModalOverlay from './common/ModalOverlay';

interface MemoryModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
    personaName?: string;
}

type Tab = 'browser' | 'arasuji' | 'memopedia' | 'storage_layers' | 'tracks' | 'pulse_logs' | 'import' | 'debug';

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
                    <button
                        className={`${styles.tab} ${activeTab === 'storage_layers' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('storage_layers')}
                    >
                        <Boxes size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        7層ストレージ
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'tracks' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('tracks')}
                    >
                        <GitBranch size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        Tracks
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'pulse_logs' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('pulse_logs')}
                    >
                        <Activity size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        Pulse Logs
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
                    {activeTab === 'arasuji' && <ArasujiViewer personaId={personaId} />}
                    {activeTab === 'memopedia' && <MemopediaViewer personaId={personaId} />}
                    {activeTab === 'storage_layers' && <StorageLayersViewer personaId={personaId} />}
                    {activeTab === 'tracks' && <TracksViewer personaId={personaId} />}
                    {activeTab === 'pulse_logs' && <PulseLogsViewer personaId={personaId} />}
                    {activeTab === 'import' && <MemoryImport personaId={personaId} />}
                    {activeTab === 'debug' && <MemoryRecall personaId={personaId} />}
                </div>
            </div>
        </ModalOverlay>
    );
}
