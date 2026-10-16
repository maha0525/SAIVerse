import { apiFetch } from '@/i18n/api';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useRef, useState } from 'react';
import { AlertCircle, FileArchive, Loader2, PackageOpen, X } from 'lucide-react';

import styles from './PersonaPack.module.css';
import PersonaPackResult, { PersonaPackImportResult } from './PersonaPackResult';

interface PersonaPackPanelProps {
    personaId: string;
}

/** Content-Disposition からファイル名を取り出す (RFC 5987 の filename* を優先) */
function filenameFromDisposition(disposition: string | null, fallback: string): string {
    if (!disposition) return fallback;
    const rfc5987 = disposition.match(/filename\*=UTF-8''(.+?)(?:;|$)/i);
    if (rfc5987) {
        try {
            return decodeURIComponent(rfc5987[1]);
        } catch {
            return fallback;
        }
    }
    const plain = disposition.match(/filename="?([^";]+)"?/i);
    return plain ? plain[1] : fallback;
}

async function errorDetail(res: Response): Promise<string> {
    const data = await res.json().catch(() => null);
    if (data && typeof data.detail === 'string') return data.detail;
    return `${res.status} ${res.statusText}`;
}

/**
 * ペルソナまるごとパック (docs/intent/persona_pack.md) — Memory モーダルのタブ。
 * このペルソナを ZIP にまとめる操作と、ZIP をこのペルソナへ合流させる操作を置く。
 */
export default function PersonaPackPanel({ personaId }: PersonaPackPanelProps) {
    useLocale();
    const fileInputRef = useRef<HTMLInputElement>(null);

    const [isExporting, setIsExporting] = useState(false);
    const [exportError, setExportError] = useState<string | null>(null);

    const [selectedFile, setSelectedFile] = useState<File | null>(null);
    const [overwriteProfile, setOverwriteProfile] = useState(false);
    const [isImporting, setIsImporting] = useState(false);
    const [importError, setImportError] = useState<string | null>(null);
    const [result, setResult] = useState<PersonaPackImportResult | null>(null);

    const handleExport = async () => {
        setIsExporting(true);
        setExportError(null);
        try {
            const res = await apiFetch(`/api/people/${encodeURIComponent(personaId)}/persona-pack`);
            if (!res.ok) {
                setExportError(uiText('components.memory.PersonaPack.exportFailed', { p1: await errorDetail(res) }));
                return;
            }
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filenameFromDisposition(res.headers.get('Content-Disposition'), `${personaId}_persona_pack.zip`);
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (error) {
            console.error('Persona pack export failed', error);
            setExportError(uiText('components.memory.PersonaPack.exportFailed', { p1: String(error) }));
        } finally {
            setIsExporting(false);
        }
    };

    const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0] ?? null;
        e.target.value = '';
        if (!file) return;
        setSelectedFile(file);
        setOverwriteProfile(false);
        setImportError(null);
        setResult(null);
    };

    const handleCancel = () => {
        setSelectedFile(null);
        setOverwriteProfile(false);
        setImportError(null);
    };

    const handleMerge = async () => {
        if (!selectedFile) return;
        setIsImporting(true);
        setImportError(null);
        setResult(null);
        try {
            const form = new FormData();
            form.append('file', selectedFile);
            form.append('overwrite_profile', overwriteProfile ? 'true' : 'false');
            const res = await apiFetch(`/api/people/${encodeURIComponent(personaId)}/persona-pack/import`, {
                method: 'POST',
                body: form,
            });
            if (!res.ok) {
                setImportError(uiText('components.memory.PersonaPack.importFailed', { p1: await errorDetail(res) }));
                return;
            }
            const data = (await res.json()) as PersonaPackImportResult;
            setResult(data);
            setSelectedFile(null);
        } catch (error) {
            console.error('Persona pack import failed', error);
            setImportError(uiText('components.memory.PersonaPack.importFailed', { p1: String(error) }));
        } finally {
            setIsImporting(false);
        }
    };

    return (
        <div className={styles.container}>
            <h2 data-i18n="components.memory.PersonaPack.title" className={styles.title}>{uiText('components.memory.PersonaPack.title')}</h2>
            <p data-i18n="components.memory.PersonaPack.lead" className={styles.lead}>{uiText('components.memory.PersonaPack.lead')}</p>

            {/* パックにまとめる (エクスポート、読み取り専用) */}
            <div className={styles.section}>
                <div data-i18n="components.memory.PersonaPack.exportAction" className={styles.sectionTitle}>{uiText('components.memory.PersonaPack.exportAction')}</div>
                <p data-i18n="components.memory.PersonaPack.exportDescription" className={styles.sectionText}>{uiText('components.memory.PersonaPack.exportDescription')}</p>
                <div className={styles.actions}>
                    <button data-i18n="components.memory.PersonaPack.exportAction" className={styles.primaryButton} onClick={() => void handleExport()} disabled={isExporting}>
                        {isExporting ? <Loader2 size={16} className={styles.spin} /> : <FileArchive size={16} />}
                        {uiText('components.memory.PersonaPack.exportAction')}
                    </button>
                    {isExporting && (
                        <span data-i18n="components.memory.PersonaPack.exporting" className={styles.busy}>{uiText('components.memory.PersonaPack.exporting')}</span>
                    )}
                </div>
                {exportError && (
                    <div className={styles.error}><AlertCircle size={16} /><span>{exportError}</span></div>
                )}
            </div>

            {/* パックを取り込む (このペルソナに合流させる) */}
            <div className={styles.section}>
                <div data-i18n="components.memory.PersonaPack.importAction" className={styles.sectionTitle}>{uiText('components.memory.PersonaPack.importAction')}</div>
                <p data-i18n="components.memory.PersonaPack.importDescription" className={styles.sectionText}>{uiText('components.memory.PersonaPack.importDescription')}</p>
                <input
                    ref={fileInputRef}
                    type="file"
                    accept=".zip,application/zip"
                    className={styles.hiddenInput}
                    onChange={handleFileChange}
                />
                <div className={styles.actions}>
                    <button data-i18n="components.memory.PersonaPack.chooseFile" className={styles.secondaryButton} onClick={() => fileInputRef.current?.click()} disabled={isImporting}>
                        <PackageOpen size={16} />
                        {uiText('components.memory.PersonaPack.chooseFile')}
                    </button>
                </div>

                {selectedFile && (
                    <div className={styles.confirmBox}>
                        <div className={styles.fileName}>{selectedFile.name}</div>
                        <label className={styles.checkboxRow}>
                            <input
                                type="checkbox"
                                checked={overwriteProfile}
                                onChange={(e) => setOverwriteProfile(e.target.checked)}
                                disabled={isImporting}
                            />
                            <span data-i18n="components.memory.PersonaPack.overwriteProfile">{uiText('components.memory.PersonaPack.overwriteProfile')}</span>
                        </label>
                        <p data-i18n="components.memory.PersonaPack.mergeHint" className={styles.hint}>{uiText('components.memory.PersonaPack.mergeHint')}</p>
                        <div className={styles.actions}>
                            <button data-i18n="components.memory.PersonaPack.mergeAction" className={styles.primaryButton} onClick={() => void handleMerge()} disabled={isImporting}>
                                {isImporting ? <Loader2 size={16} className={styles.spin} /> : <PackageOpen size={16} />}
                                {uiText('components.memory.PersonaPack.mergeAction')}
                            </button>
                            <button data-i18n="components.memory.PersonaPack.cancel" className={styles.secondaryButton} onClick={handleCancel} disabled={isImporting}>
                                <X size={16} />
                                {uiText('components.memory.PersonaPack.cancel')}
                            </button>
                            {isImporting && (
                                <span data-i18n="components.memory.PersonaPack.importing" className={styles.busy}>{uiText('components.memory.PersonaPack.importing')}</span>
                            )}
                        </div>
                    </div>
                )}

                {importError && (
                    <div className={styles.error}><AlertCircle size={16} /><span>{importError}</span></div>
                )}
                {result && <PersonaPackResult result={result} />}
            </div>
        </div>
    );
}
