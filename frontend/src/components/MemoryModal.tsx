import React, { useState } from 'react';
import { X, MessageSquare, Upload, Book, Search } from 'lucide-react';
import styles from './MemoryModal.module.css';
import MemoryBrowser from './memory/MemoryBrowser';
import MemoryImport from './memory/MemoryImport';
import MemopediaViewer from './memory/MemopediaViewer';
import MemoryRecall from './memory/MemoryRecall';

interface MemoryModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
}

type Tab = 'browser' | 'import' | 'memopedia' | 'recall';

export default function MemoryModal({ isOpen, onClose, personaId }: MemoryModalProps) {
    const [activeTab, setActiveTab] = useState<Tab>('browser');

    if (!isOpen) return null;

    return (
        <div
            className={styles.overlay}
            onClick={onClose}
            onTouchStart={(e) => e.stopPropagation()}
            onTouchMove={(e) => e.stopPropagation()}
        >
            <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 className={styles.title}>Memory & Knowledge: {personaId}</h2>
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
                        Chat Logs
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'import' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('import')}
                    >
                        <Upload size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        Import Logs
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'memopedia' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('memopedia')}
                    >
                        <Book size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        Memopedia
                    </button>
                    <button
                        className={`${styles.tab} ${activeTab === 'recall' ? styles.activeTab : ''}`}
                        onClick={() => setActiveTab('recall')}
                    >
                        <Search size={16} style={{ display: 'inline', marginRight: 8, verticalAlign: 'text-bottom' }} />
                        Recall Test
                    </button>
                </div>

                <div className={styles.content}>
                    {activeTab === 'browser' && <MemoryBrowser personaId={personaId} />}
                    {activeTab === 'import' && <MemoryImport personaId={personaId} />}
                    {activeTab === 'memopedia' && <MemopediaViewer personaId={personaId} />}
                    {activeTab === 'recall' && <MemoryRecall personaId={personaId} />}
                </div>
            </div>
        </div>
    );
}

