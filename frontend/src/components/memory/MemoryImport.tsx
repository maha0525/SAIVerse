import React, { useState, useRef } from 'react';
import { Upload, CheckCircle, AlertCircle, Loader2, CheckSquare, Square, RefreshCw, MessageSquare } from 'lucide-react';
import styles from './MemoryImport.module.css';

interface MemoryImportProps {
    personaId: string;
    onImportComplete?: () => void;
}

interface ThreadSummary {
    thread_id: string;
    suffix: string;
    preview: string;
    active: boolean;
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

type Step = 'upload' | 'select' | 'embedding-dialog' | 'importing' | 'thread-select';

export default function MemoryImport({ personaId, onImportComplete }: MemoryImportProps) {
    const [activeSubTab, setActiveSubTab] = useState<'official' | 'extension'>('official');
    const [isLoading, setIsLoading] = useState(false);
    const [result, setResult] = useState<{ type: 'success' | 'error', message: string } | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    // Official import state
    const [step, setStep] = useState<Step>('upload');
    const [previewData, setPreviewData] = useState<PreviewData | null>(null);
    const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

    // Extension import state
    const [pendingExtensionFile, setPendingExtensionFile] = useState<File | null>(null);

    // Import progress
    const [importProgress, setImportProgress] = useState<string | null>(null);

    // Thread selection state
    const [threads, setThreads] = useState<ThreadSummary[]>([]);
    const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);

    const resetState = () => {
        setStep('upload');
        setPreviewData(null);
        setSelectedIds(new Set());
        setResult(null);
        setPendingExtensionFile(null);
        setImportProgress(null);
        setThreads([]);
        setSelectedThreadId(null);
    };

    // Fetch threads and handle thread selection after import
    const handleImportSuccess = async (message: string) => {
        try {
            const res = await fetch(`/api/people/${personaId}/threads`);
            if (res.ok) {
                const threadList: ThreadSummary[] = await res.json();

                if (threadList.length === 0) {
                    // No threads - just show success
                    setStep('upload');
                    setResult({ type: 'success', message });
                } else if (threadList.length === 1) {
                    // Single thread - auto-activate
                    await activateThread(threadList[0].thread_id);
                    setStep('upload');
                    setResult({ type: 'success', message: `${message} スレッドを自動設定しました。` });
                    if (onImportComplete) onImportComplete();
                } else {
                    // Multiple threads - show selection
                    // Sort by preview length (approximation for recency - ideally we'd have timestamp)
                    // Default select the first one (which should be most recent based on API)
                    setThreads(threadList);
                    setSelectedThreadId(threadList[0].thread_id);
                    setStep('thread-select');
                    setResult({ type: 'success', message });
                }
            } else {
                setStep('upload');
                setResult({ type: 'success', message });
            }
        } catch (error) {
            setStep('upload');
            setResult({ type: 'success', message });
        }
        setPreviewData(null);
        setSelectedIds(new Set());
    };

    const activateThread = async (threadId: string) => {
        try {
            await fetch(`/api/people/${personaId}/threads/${encodeURIComponent(threadId)}/activate`, {
                method: 'PUT',
            });
        } catch (error) {
            console.error('Failed to activate thread', error);
        }
    };

    const handleThreadSelectConfirm = async () => {
        if (!selectedThreadId) return;

        setIsLoading(true);
        await activateThread(selectedThreadId);
        setIsLoading(false);
        setStep('upload');
        setResult({ type: 'success', message: 'インポート完了！アクティブスレッドを設定しました。' });
        if (onImportComplete) onImportComplete();
    };

    // Polling for import status
    const pollImportStatus = async (type: 'extension' | 'official') => {
        try {
            const res = await fetch(`/api/people/${personaId}/import/${type}/status`);
            const data = await res.json();

            if (data.running) {
                setImportProgress(data.message || `Processing ${data.progress || 0}/${data.total || 0}...`);
                setTimeout(() => pollImportStatus(type), 1000);
            } else {
                // Task completed
                setImportProgress(null);
                setIsLoading(false);
                if (data.success) {
                    await handleImportSuccess(data.message || 'Import successful');
                } else {
                    setStep('upload');
                    setResult({ type: 'error', message: data.message || 'Import failed' });
                }
            }
        } catch (error) {
            setStep('upload');
            setImportProgress(null);
            setIsLoading(false);
            setResult({ type: 'error', message: 'Failed to get import status.' });
        }
    };

    const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        setIsLoading(true);
        setResult(null);

        if (activeSubTab === 'official') {
            // Preview flow for official export
            const formData = new FormData();
            formData.append('file', file);

            try {
                const res = await fetch(`/api/people/${personaId}/import/official/preview`, {
                    method: 'POST',
                    body: formData,
                });
                const data = await res.json();

                if (res.ok) {
                    if (data.conversations && data.conversations.length > 0) {
                        setPreviewData(data);
                        setSelectedIds(new Set());
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
            // Extension: Show embedding dialog before import
            setPendingExtensionFile(file);
            setStep('embedding-dialog');
            setIsLoading(false);
            if (fileInputRef.current) fileInputRef.current.value = '';
        }
    };

    const executeExtensionImport = async (skipEmbedding: boolean) => {
        if (!pendingExtensionFile) return;

        setStep('importing');
        setIsLoading(true);
        setResult(null);
        setImportProgress('Starting import...');

        const formData = new FormData();
        formData.append('file', pendingExtensionFile);
        formData.append('skip_embedding', skipEmbedding.toString());

        try {
            const res = await fetch(`/api/people/${personaId}/import/extension`, {
                method: 'POST',
                body: formData,
            });
            const data = await res.json();

            if (res.ok) {
                // Start polling for status
                setTimeout(() => pollImportStatus('extension'), 1000);
            } else {
                setStep('upload');
                setIsLoading(false);
                setImportProgress(null);
                setResult({ type: 'error', message: data.detail || 'Import failed' });
            }
        } catch (error) {
            setStep('upload');
            setIsLoading(false);
            setImportProgress(null);
            setResult({ type: 'error', message: 'Network error occurred.' });
        } finally {
            setPendingExtensionFile(null);
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

    const handleOfficialImportClick = () => {
        if (!previewData || selectedIds.size === 0) {
            setResult({ type: 'error', message: 'Please select at least one conversation to import.' });
            return;
        }
        // Show embedding dialog
        setStep('embedding-dialog');
    };

    const executeOfficialImport = async (skipEmbedding: boolean) => {
        if (!previewData) return;

        setStep('importing');
        setIsLoading(true);
        setResult(null);
        setImportProgress('Starting import...');

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
                // Start polling for status
                setTimeout(() => pollImportStatus('official'), 1000);
            } else {
                setStep('select');
                setIsLoading(false);
                setImportProgress(null);
                setResult({ type: 'error', message: data.detail || 'Import failed' });
            }
        } catch (error) {
            setStep('select');
            setIsLoading(false);
            setImportProgress(null);
            setResult({ type: 'error', message: 'Network error occurred.' });
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
                setTimeout(pollReembedStatus, 1000);
            } else {
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

    const renderEmbeddingDialog = () => (
        <div className={styles.embeddingDialog}>
            <h3>記憶想起用のエンベディングを作成しますか？</h3>
            <ul className={styles.dialogInfo}>
                <li>バックグラウンドで実行されます</li>
                <li>CPU実行では時間がかかります</li>
                <li>スキップした場合、別途「Fill Missing Embeddings」から再実行可能です</li>
            </ul>
            <div className={styles.dialogActions}>
                <button
                    className={styles.cancelButton}
                    onClick={() => {
                        if (activeSubTab === 'extension') {
                            executeExtensionImport(true);
                        } else {
                            executeOfficialImport(true);
                        }
                    }}
                >
                    スキップ
                </button>
                <button
                    className={styles.importButton}
                    onClick={() => {
                        if (activeSubTab === 'extension') {
                            executeExtensionImport(false);
                        } else {
                            executeOfficialImport(false);
                        }
                    }}
                >
                    作成する
                </button>
            </div>
        </div>
    );

    const renderImportingProgress = () => (
        <div className={styles.importingProgress}>
            <Loader2 className={styles.loader} size={48} />
            <div className={styles.progressText}>{importProgress || 'Importing...'}</div>
        </div>
    );

    const renderThreadSelect = () => (
        <div className={styles.threadSelectContainer}>
            <div className={styles.threadSelectHeader}>
                <MessageSquare size={24} />
                <h3>どのスレッドから会話を続けますか？</h3>
            </div>
            <p className={styles.threadSelectSubtext}>
                複数のスレッドがインポートされました。アクティブにするスレッドを選択してください。
            </p>

            <div className={styles.threadList}>
                {threads.map((thread) => (
                    <div
                        key={thread.thread_id}
                        className={`${styles.threadItem} ${selectedThreadId === thread.thread_id ? styles.selected : ''}`}
                        onClick={() => setSelectedThreadId(thread.thread_id)}
                    >
                        <div className={styles.threadRadio}>
                            <div className={`${styles.radioCircle} ${selectedThreadId === thread.thread_id ? styles.checked : ''}`} />
                        </div>
                        <div className={styles.threadContent}>
                            <div className={styles.threadName}>
                                {thread.suffix || thread.thread_id}
                                {thread.active && <span className={styles.activeBadge}>現在アクティブ</span>}
                            </div>
                            <div className={styles.threadPreview}>
                                {thread.preview || '(プレビューなし)'}
                            </div>
                        </div>
                    </div>
                ))}
            </div>

            <div className={styles.actions}>
                <button
                    className={styles.cancelButton}
                    onClick={() => {
                        setStep('upload');
                        if (onImportComplete) onImportComplete();
                    }}
                >
                    スキップ
                </button>
                <button
                    className={styles.importButton}
                    onClick={handleThreadSelectConfirm}
                    disabled={!selectedThreadId || isLoading}
                >
                    {isLoading ? <Loader2 size={16} className={styles.loader} /> : null}
                    このスレッドを使用
                </button>
            </div>
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
                        onClick={handleOfficialImportClick}
                        disabled={selectedIds.size === 0 || isLoading}
                    >
                        {isLoading ? <Loader2 size={16} className={styles.loader} /> : null}
                        Import {selectedIds.size} Conversation{selectedIds.size !== 1 ? 's' : ''}
                    </button>
                </div>
            </div>
        );
    };

    const renderMainContent = () => {
        if (step === 'thread-select') {
            return renderThreadSelect();
        }
        if (step === 'embedding-dialog') {
            return renderEmbeddingDialog();
        }
        if (step === 'importing') {
            return renderImportingProgress();
        }
        if (activeSubTab === 'official' && step === 'select') {
            return renderConversationTable();
        }
        return renderUploadArea();
    };

    return (
        <div className={styles.container}>
            <h2 className={styles.title}>Import Chat Logs</h2>

            <div className={styles.subTabs}>
                <button
                    className={`${styles.subTab} ${activeSubTab === 'official' ? styles.active : ''}`}
                    onClick={() => { setActiveSubTab('official'); resetState(); }}
                    disabled={step === 'importing'}
                >
                    Official Data Export
                </button>
                <button
                    className={`${styles.subTab} ${activeSubTab === 'extension' ? styles.active : ''}`}
                    onClick={() => { setActiveSubTab('extension'); resetState(); }}
                    disabled={step === 'importing'}
                >
                    Extension Export
                </button>
            </div>

            {renderMainContent()}

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
