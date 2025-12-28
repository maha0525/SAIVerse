import React, { useState, useRef } from 'react';
import { Upload, CheckCircle, AlertCircle, Loader2, CheckSquare, Square, RefreshCw } from 'lucide-react';
import styles from './MemoryImport.module.css';

interface MemoryImportProps {
    personaId: string;
}

interface ConversationSummary {
    idx: number;
    id: string;
    conversation_id: string | null;
    title: string;
    create_time: string | null;
    update_time: string | null;
    message_count: number;
    preview: string | null;
}

interface PreviewData {
    conversations: ConversationSummary[];
    cache_key: string;
    total_count: number;
}

type Step = 'upload' | 'select' | 'importing';

export default function MemoryImport({ personaId }: MemoryImportProps) {
    const [activeSubTab, setActiveSubTab] = useState<'official' | 'extension'>('official');
    const [isLoading, setIsLoading] = useState(false);
    const [result, setResult] = useState<{ type: 'success' | 'error', message: string } | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    // Official import state
    const [step, setStep] = useState<Step>('upload');
    const [previewData, setPreviewData] = useState<PreviewData | null>(null);
    const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
    const [skipEmbedding, setSkipEmbedding] = useState(true);

    const resetState = () => {
        setStep('upload');
        setPreviewData(null);
        setSelectedIds(new Set());
        setResult(null);
    };

    const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        setIsLoading(true);
        setResult(null);

        const formData = new FormData();
        formData.append('file', file);

        if (activeSubTab === 'official') {
            // Preview flow for official export
            try {
                const res = await fetch(`/api/people/${personaId}/import/official/preview`, {
                    method: 'POST',
                    body: formData,
                });
                const data = await res.json();

                if (res.ok) {
                    if (data.conversations && data.conversations.length > 0) {
                        setPreviewData(data);
                        setSelectedIds(new Set()); // Start with none selected
                        setStep('select');
                    } else {
                        setResult({ type: 'error', message: 'No conversations found in the file.' });
                    }
                } else {
                    setResult({ type: 'error', message: data.detail || 'Preview failed' });
                }
            } catch (error) {
                setResult({ type: 'error', message: 'Network error occurred.' });
            } finally {
                setIsLoading(false);
                if (fileInputRef.current) fileInputRef.current.value = '';
            }
        } else {
            // Direct import for extension export
            try {
                const res = await fetch(`/api/people/${personaId}/import/extension`, {
                    method: 'POST',
                    body: formData,
                });
                const data = await res.json();

                if (res.ok) {
                    setResult({ type: 'success', message: data.message || 'Import successful' });
                } else {
                    setResult({ type: 'error', message: data.detail || 'Import failed' });
                }
            } catch (error) {
                setResult({ type: 'error', message: 'Network error occurred.' });
            } finally {
                setIsLoading(false);
                if (fileInputRef.current) fileInputRef.current.value = '';
            }
        }
    };

    const toggleSelection = (idx: number) => {
        setSelectedIds(prev => {
            const next = new Set(prev);
            if (next.has(idx)) {
                next.delete(idx);
            } else {
                next.add(idx);
            }
            return next;
        });
    };

    const toggleSelectAll = () => {
        if (!previewData) return;
        if (selectedIds.size === previewData.conversations.length) {
            setSelectedIds(new Set());
        } else {
            setSelectedIds(new Set(previewData.conversations.map(c => c.idx)));
        }
    };

    const handleImport = async () => {
        if (!previewData || selectedIds.size === 0) {
            setResult({ type: 'error', message: 'Please select at least one conversation to import.' });
            return;
        }

        setStep('importing');
        setIsLoading(true);
        setResult(null);

        try {
            const res = await fetch(`/api/people/${personaId}/import/official`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    cache_key: previewData.cache_key,
                    conversation_ids: Array.from(selectedIds).map(String),
                    skip_embedding: skipEmbedding,
                }),
            });
            const data = await res.json();

            if (res.ok) {
                setResult({ type: 'success', message: data.message || 'Import successful' });
                setStep('upload');
                setPreviewData(null);
                setSelectedIds(new Set());
            } else {
                setResult({ type: 'error', message: data.detail || 'Import failed' });
                setStep('select');
            }
        } catch (error) {
            setResult({ type: 'error', message: 'Network error occurred.' });
            setStep('select');
        } finally {
            setIsLoading(false);
        }
    };

    const [isReembedding, setIsReembedding] = useState(false);
    const [reembedProgress, setReembedProgress] = useState<string | null>(null);

    const pollReembedStatus = async () => {
        try {
            const res = await fetch(`/api/people/${personaId}/reembed/status`);
            const data = await res.json();

            if (data.running) {
                setReembedProgress(data.message || `Processing ${data.progress || 0}/${data.total || 0}...`);
                // Continue polling
                setTimeout(pollReembedStatus, 1000);
            } else {
                // Task completed
                setIsReembedding(false);
                setReembedProgress(null);
                if (data.message) {
                    setResult({ type: 'success', message: data.message });
                }
            }
        } catch (error) {
            setIsReembedding(false);
            setReembedProgress(null);
            setResult({ type: 'error', message: 'Failed to get status.' });
        }
    };

    const handleReembed = async (force: boolean = false) => {
        setIsReembedding(true);
        setResult(null);
        setReembedProgress('Starting...');

        try {
            const res = await fetch(`/api/people/${personaId}/reembed`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ force }),
            });
            const data = await res.json();

            if (res.ok && data.success) {
                // Start polling for status
                setTimeout(pollReembedStatus, 1000);
            } else {
                setIsReembedding(false);
                setReembedProgress(null);
                setResult({ type: 'error', message: data.detail || data.message || 'Re-embedding failed' });
            }
        } catch (error) {
            setIsReembedding(false);
            setReembedProgress(null);
            setResult({ type: 'error', message: 'Network error occurred.' });
        }
    };


    const renderUploadArea = () => (
        <div className={styles.uploadArea} onClick={() => fileInputRef.current?.click()}>
            {isLoading ? (
                <Loader2 className={`${styles.uploadIcon} ${styles.loader}`} size={48} />
            ) : (
                <Upload className={styles.uploadIcon} size={48} />
            )}
            <div className={styles.uploadText}>
                {isLoading ? 'Loading...' : 'Click to upload file'}
            </div>
            <div className={styles.uploadSubtext}>
                {activeSubTab === 'official'
                    ? 'Supports conversations.json or export ZIP'
                    : 'Supports JSON or Markdown files from extensions'}
            </div>
            <input
                type="file"
                ref={fileInputRef}
                className={styles.fileInput}
                onChange={handleFileSelect}
                accept={activeSubTab === 'official' ? '.json,.zip' : '.json,.md,.txt'}
                disabled={isLoading}
            />
        </div>
    );

    const renderConversationTable = () => {
        if (!previewData) return null;

        const allSelected = selectedIds.size === previewData.conversations.length;

        return (
            <div className={styles.selectionContainer}>
                <div className={styles.selectionHeader}>
                    <h3>Select Conversations to Import</h3>
                    <span className={styles.selectionCount}>
                        {selectedIds.size} of {previewData.total_count} selected
                    </span>
                </div>

                <div className={styles.options}>
                    <label className={styles.checkbox}>
                        <input
                            type="checkbox"
                            checked={skipEmbedding}
                            onChange={(e) => setSkipEmbedding(e.target.checked)}
                        />
                        <span>Skip embedding (faster import)</span>
                    </label>
                </div>

                <div className={styles.tableContainer}>
                    <table className={styles.table}>
                        <thead>
                            <tr>
                                <th className={styles.checkboxCell} onClick={toggleSelectAll}>
                                    {allSelected ? <CheckSquare size={18} /> : <Square size={18} />}
                                </th>
                                <th>Title</th>
                                <th>Messages</th>
                                <th>Created</th>
                                <th>Preview</th>
                            </tr>
                        </thead>
                        <tbody>
                            {previewData.conversations.map((conv) => (
                                <tr
                                    key={conv.idx}
                                    className={selectedIds.has(conv.idx) ? styles.selected : ''}
                                    onClick={() => toggleSelection(conv.idx)}
                                >
                                    <td className={styles.checkboxCell}>
                                        {selectedIds.has(conv.idx) ? <CheckSquare size={18} /> : <Square size={18} />}
                                    </td>
                                    <td className={styles.titleCell}>{conv.title || '(Untitled)'}</td>
                                    <td>{conv.message_count}</td>
                                    <td>{conv.create_time || '-'}</td>
                                    <td className={styles.previewCell}>{conv.preview || '-'}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>

                <div className={styles.actions}>
                    <button className={styles.cancelButton} onClick={resetState}>
                        Cancel
                    </button>
                    <button
                        className={styles.importButton}
                        onClick={handleImport}
                        disabled={selectedIds.size === 0 || isLoading}
                    >
                        {isLoading ? <Loader2 size={16} className={styles.loader} /> : null}
                        Import {selectedIds.size} Conversation{selectedIds.size !== 1 ? 's' : ''}
                    </button>
                </div>
            </div>
        );
    };

    return (
        <div className={styles.container}>
            <h2 className={styles.title}>Import Chat Logs</h2>

            <div className={styles.subTabs}>
                <button
                    className={`${styles.subTab} ${activeSubTab === 'official' ? styles.active : ''}`}
                    onClick={() => { setActiveSubTab('official'); resetState(); }}
                >
                    Official Data Export
                </button>
                <button
                    className={`${styles.subTab} ${activeSubTab === 'extension' ? styles.active : ''}`}
                    onClick={() => { setActiveSubTab('extension'); resetState(); }}
                >
                    Extension Export
                </button>
            </div>

            {activeSubTab === 'official' && step === 'select' ? (
                renderConversationTable()
            ) : (
                renderUploadArea()
            )}

            {result && (
                <div className={`${styles.result} ${styles[result.type]}`}>
                    {result.type === 'success' ? <CheckCircle size={20} /> : <AlertCircle size={20} />}
                    <span>{result.message}</span>
                </div>
            )}

            {/* Re-embed Section */}
            <div className={styles.reembedSection}>
                <h3>Embedding Maintenance</h3>
                <p>Run embedding on messages that are missing vector embeddings (e.g., imported with "Skip embedding").</p>
                <div className={styles.reembedActions}>
                    <button
                        className={styles.reembedButton}
                        onClick={() => handleReembed(false)}
                        disabled={isReembedding}
                    >
                        {isReembedding ? <Loader2 size={16} className={styles.loader} /> : <RefreshCw size={16} />}
                        Fill Missing Embeddings
                    </button>
                    <button
                        className={styles.reembedButtonSecondary}
                        onClick={() => handleReembed(true)}
                        disabled={isReembedding}
                    >
                        {isReembedding ? <Loader2 size={16} className={styles.loader} /> : <RefreshCw size={16} />}
                        Re-embed All
                    </button>
                </div>
                {reembedProgress && (
                    <div className={styles.reembedProgress}>
                        <Loader2 size={14} className={styles.loader} />
                        <span>{reembedProgress}</span>
                    </div>
                )}
            </div>
        </div>
    );
}
