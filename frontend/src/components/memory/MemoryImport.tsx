import React, { useState, useRef } from 'react';
import { Upload, FileText, CheckCircle, AlertCircle, Loader2 } from 'lucide-react';
import styles from './MemoryImport.module.css';

interface MemoryImportProps {
    personaId: string;
}

export default function MemoryImport({ personaId }: MemoryImportProps) {
    const [activeSubTab, setActiveSubTab] = useState<'official' | 'extension'>('official');
    const [isLoading, setIsLoading] = useState(false);
    const [result, setResult] = useState<{ type: 'success' | 'error', message: string } | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        setIsLoading(true);
        setResult(null);

        const formData = new FormData();
        formData.append('file', file);

        const endpoint = activeSubTab === 'official'
            ? `/api/people/${personaId}/import/official`
            : `/api/people/${personaId}/import/extension`;

        try {
            const res = await fetch(endpoint, {
                method: 'POST',
                body: formData,
            });
            const data = await res.json();

            if (res.ok) {
                setResult({ type: 'success', message: data.message || "Import successful" });
            } else {
                setResult({ type: 'error', message: data.detail || "Import failed" });
            }
        } catch (error) {
            setResult({ type: 'error', message: "Network error occurred." });
        } finally {
            setIsLoading(false);
            if (fileInputRef.current) fileInputRef.current.value = '';
        }
    };

    return (
        <div className={styles.container}>
            <h2 className={styles.title}>Import Chat Logs</h2>

            <div className={styles.subTabs}>
                <button
                    className={`${styles.subTab} ${activeSubTab === 'official' ? styles.active : ''}`}
                    onClick={() => setActiveSubTab('official')}
                >
                    Official Data Export
                </button>
                <button
                    className={`${styles.subTab} ${activeSubTab === 'extension' ? styles.active : ''}`}
                    onClick={() => setActiveSubTab('extension')}
                >
                    Extension Export
                </button>
            </div>

            <div className={styles.uploadArea} onClick={() => fileInputRef.current?.click()}>
                {isLoading ? (
                    <Loader2 className={`${styles.uploadIcon} ${styles.loader}`} size={48} />
                ) : (
                    <Upload className={styles.uploadIcon} size={48} />
                )}
                <div className={styles.uploadText}>
                    {isLoading ? "Importing..." : "Click to upload file"}
                </div>
                <div className={styles.uploadSubtext}>
                    {activeSubTab === 'official'
                        ? "Supports conversations.json or export ZIP"
                        : "Supports JSON or Markdown files from extensions"}
                </div>
                <input
                    type="file"
                    ref={fileInputRef}
                    className={styles.fileInput}
                    onChange={handleFileSelect}
                    accept={activeSubTab === 'official' ? ".json,.zip" : ".json,.md,.txt"}
                    disabled={isLoading}
                />
            </div>

            {result && (
                <div className={`${styles.result} ${styles[result.type]}`}>
                    {result.type === 'success' ? <CheckCircle size={20} /> : <AlertCircle size={20} />}
                    <span>{result.message}</span>
                </div>
            )}
        </div>
    );
}
