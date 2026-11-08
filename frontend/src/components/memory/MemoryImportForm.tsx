
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { AlertCircle, CheckSquare, Download, FileText, Loader2, Square } from 'lucide-react';
import React, { useState, useRef, useCallback } from 'react';

import {
  importExtension,
  importNative,
  importOfficial,
  previewNativeImport,
  previewOfficialImport,
} from '@/lib/memory/importClient';

import styles from './MemoryImport.module.css';
import { formatImportDate } from './formatters';
import { ImportStartRequest, MemoryImportUiResult, NativePreviewData, PreviewData } from './types';

/**
 * Step 2 of the import flow for the ChatGPT official export, the browser
 * extension export and the native SAIVerse package: file upload and
 * conversation selection. Running the import (step 3 onward) belongs to
 * ImportFlowModal, which receives an ImportStartRequest through onStart.
 * Design: docs/intent/import_flow_redesign.md
 */

type Source = 'official' | 'extension' | 'native';
type Phase = 'upload' | 'select';

interface Props {
  personaId: string;
  source: Source;
  onStart: (request: ImportStartRequest) => void;
}

const failure = (data: { message?: string }) => data.message || uiText('components.memory.ImportFlowModal.importFailed');

export function MemoryImportForm({ personaId, source, onStart }: Props) {
  useLocale();
  const [phase, setPhase] = useState<Phase>('upload');
  const [isLoading, setIsLoading] = useState(false);
  const [result, setResult] = useState<MemoryImportUiResult | null>(null);
  const [previewData, setPreviewData] = useState<PreviewData | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [pendingExtensionFile, setPendingExtensionFile] = useState<File | null>(null);
  const [nativePreview, setNativePreview] = useState<NativePreviewData | null>(null);
  const [pendingNativeFile, setPendingNativeFile] = useState<File | null>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const dragCounter = useRef(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const reset = () => {
    setPhase('upload'); setPreviewData(null); setSelectedIds(new Set()); setResult(null);
    setPendingExtensionFile(null); setNativePreview(null); setPendingNativeFile(null);
  };

  const handleFile = async (file: File) => {
    setIsLoading(true); setResult(null);
    if (source === 'official') {
      const { ok, data } = await previewOfficialImport(personaId, file);
      if (ok && 'conversations' in data && data.conversations.length > 0) {
        setPreviewData(data); setSelectedIds(new Set()); setPhase('select');
      } else {
        setResult({ type: 'error', message: ('detail' in data && data.detail) || uiText("components.memory.MemoryImportForm.noConversations") });
      }
    } else if (source === 'native') {
      const { ok, data } = await previewNativeImport(personaId, file);
      if (ok && 'threads' in data) { setNativePreview(data); setPendingNativeFile(file); setPhase('select'); }
      else { setResult({ type: 'error', message: ('detail' in data && data.detail) || uiText('components.memory.MemoryImportForm.previewFailed') }); }
    } else {
      setPendingExtensionFile(file); setPhase('select');
    }
    setIsLoading(false);
  };

  const startOfficial = () => {
    if (!previewData || selectedIds.size === 0) return;
    const request = {
      cache_key: previewData.cache_key,
      conversation_ids: Array.from(selectedIds).map(String),
      skip_embedding: true,
    };
    onStart({
      statusType: 'official',
      initialProgress: uiText("components.memory.MemoryImportForm.starting"),
      selectThread: true,
      failureMessage: failure,
      start: async () => {
        const { ok, data } = await importOfficial(personaId, request);
        return { ok, error: data.detail || uiText('components.memory.ImportFlowModal.importFailed') };
      },
    });
  };

  const startExtension = () => {
    const file = pendingExtensionFile;
    if (!file) return;
    onStart({
      statusType: 'extension',
      initialProgress: uiText("components.memory.MemoryImportForm.starting"),
      selectThread: true,
      failureMessage: failure,
      start: async () => {
        const { ok, data } = await importExtension(personaId, file, true);
        return { ok, error: data.detail || uiText('components.memory.ImportFlowModal.importFailed') };
      },
    });
  };

  const startNative = () => {
    const file = pendingNativeFile;
    if (!file) return;
    onStart({
      statusType: 'native',
      initialProgress: uiText("components.memory.MemoryImportForm.nativeStarting"),
      selectThread: false,
      failureMessage: failure,
      start: async () => {
        const { ok, data } = await importNative(personaId, file, true);
        return { ok, error: data.detail || uiText('components.memory.ImportFlowModal.importFailed') };
      },
    });
  };

  const toggleSelection = (idx: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (!previewData) return;
    if (selectedIds.size === previewData.conversations.length) { setSelectedIds(new Set()); return; }
    setSelectedIds(new Set(previewData.conversations.map((c) => c.idx)));
  };

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation();
    dragCounter.current++;
    if (dragCounter.current === 1) setIsDragOver(true);
  }, []);
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation();
  }, []);
  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation();
    dragCounter.current--;
    if (dragCounter.current === 0) setIsDragOver(false);
  }, []);
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation();
    dragCounter.current = 0; setIsDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) void handleFile(file);
  };

  const resultBanner = result && (
    <div className={`${styles.result} ${result.type === 'success' ? styles.success : styles.error}`}>
      <AlertCircle size={16} />
      <span>{result.message}</span>
    </div>
  );

  if (phase === 'select' && source === 'native' && nativePreview) {
    return <div className={styles.selectionContainer}><div className={styles.selectionHeader}><h3 data-i18n="components.memory.MemoryImportForm.text009">{uiText("components.memory.MemoryImportForm.text009")}</h3><span data-i18n="components.memory.MemoryImportForm.text010 components.memory.MemoryImportForm.text011" className={styles.selectionCount}>{nativePreview.thread_count}{uiText("components.memory.MemoryImportForm.text010")}{nativePreview.total_messages}{uiText("components.memory.MemoryImportForm.text011")}</span></div>
      {nativePreview.source_persona !== personaId && <div className={`${styles.result} ${styles.error}`}><AlertCircle size={16} /><span data-i18n="components.memory.MemoryImportForm.text012 components.memory.MemoryImportForm.text013 components.memory.MemoryImportForm.text014">{uiText("components.memory.MemoryImportForm.text012")}{nativePreview.source_persona}{uiText("components.memory.MemoryImportForm.text013")}{personaId}{uiText("components.memory.MemoryImportForm.text014")}</span></div>}
      {/* スレッド一覧と上書きの注意は 2026-02-22 の分割リファクタで落ちていた。2026-09-03 に復元。 */}
      <div className={styles.tableContainer}><table className={styles.table}><thead><tr><th data-i18n="components.memory.MemoryImportForm.text015">{uiText("components.memory.MemoryImportForm.text015")}</th><th data-i18n="components.memory.MemoryImportForm.text016">{uiText("components.memory.MemoryImportForm.text016")}</th><th>{uiText("components.memory.MemoryImportForm.label001")}</th><th data-i18n="components.memory.MemoryImportForm.text017">{uiText("components.memory.MemoryImportForm.text017")}</th></tr></thead><tbody>{nativePreview.threads.map((t, idx) => <tr key={idx}><td className={styles.titleCell}>{t.thread_id}</td><td>{t.message_count}</td><td data-i18n="components.memory.MemoryImportForm.text018">{t.has_stelis ? uiText("components.memory.MemoryImportForm.text018") : '-'}</td><td className={styles.previewCell}>{t.preview || '-'}</td></tr>)}</tbody></table></div>
      <div className={`${styles.result} ${styles.error}`} style={{ margin: '1rem 0 0 0' }}><AlertCircle size={16} /><span data-i18n="components.memory.MemoryImportForm.text019">{uiText("components.memory.MemoryImportForm.text019")}</span></div>
      <div className={styles.actions}><button data-i18n="components.memory.MemoryImportForm.text020" className={styles.cancelButton} onClick={reset}>{uiText("components.memory.MemoryImportForm.text020")}</button><button data-i18n="components.memory.MemoryImportForm.text021" className={styles.importButton} onClick={startNative}>{uiText("components.memory.MemoryImportForm.text021")}</button></div>
    </div>;
  }

  if (phase === 'select' && source === 'extension' && pendingExtensionFile) {
    return <div className={styles.selectionContainer}>
      <div className={styles.selectionHeader}><h3 data-i18n="components.memory.MemoryImportForm.extensionConfirm">{uiText("components.memory.MemoryImportForm.extensionConfirm")}</h3></div>
      <div className={styles.selectedFile}><FileText size={18} /><span>{pendingExtensionFile.name}</span></div>
      <div className={styles.actions}><button data-i18n="components.memory.MemoryImportForm.text020" className={styles.cancelButton} onClick={reset}>{uiText("components.memory.MemoryImportForm.text020")}</button><button data-i18n="components.memory.MemoryImportForm.text021" className={styles.importButton} onClick={startExtension}>{uiText("components.memory.MemoryImportForm.text021")}</button></div>
    </div>;
  }

  if (phase === 'select' && source === 'official' && previewData) {
    const allSelected = selectedIds.size === previewData.conversations.length;
    return <div className={styles.selectionContainer}><div className={styles.selectionHeader}><h3 data-i18n="components.memory.MemoryImportForm.text024">{uiText("components.memory.MemoryImportForm.text024")}</h3><span data-i18n="components.memory.MemoryImportForm.text025 components.memory.MemoryImportForm.text026" className={styles.selectionCount}>{previewData.total_count}{uiText("components.memory.MemoryImportForm.text025")}{selectedIds.size}{uiText("components.memory.MemoryImportForm.text026")}</span></div>
      {/* 列 (メッセージ数 / 作成日 / プレビュー) は 2026-02-22 の分割リファクタで
          落ちていた (API は返し続けていた)。2026-09-03 に復元。 */}
      <div className={styles.tableContainer}><table className={styles.table}><thead><tr><th className={styles.checkboxCell} onClick={toggleSelectAll}>{allSelected ? <CheckSquare size={18} /> : <Square size={18} />}</th><th data-i18n="components.memory.MemoryImportForm.text027">{uiText("components.memory.MemoryImportForm.text027")}</th><th data-i18n="components.memory.MemoryImportForm.text028">{uiText("components.memory.MemoryImportForm.text028")}</th><th data-i18n="components.memory.MemoryImportForm.text029">{uiText("components.memory.MemoryImportForm.text029")}</th><th data-i18n="components.memory.MemoryImportForm.text030">{uiText("components.memory.MemoryImportForm.text030")}</th></tr></thead><tbody>{previewData.conversations.map((conv) => <tr key={conv.idx} className={selectedIds.has(conv.idx) ? styles.selected : ''} onClick={() => toggleSelection(conv.idx)}><td className={styles.checkboxCell}>{selectedIds.has(conv.idx) ? <CheckSquare size={18} /> : <Square size={18} />}</td><td data-i18n="components.memory.MemoryImportForm.text031" className={styles.titleCell}>{conv.title || uiText("components.memory.MemoryImportForm.text031")}</td><td>{conv.message_count}</td><td>{formatImportDate(conv.create_time)}</td><td className={styles.previewCell}>{conv.preview || '-'}</td></tr>)}</tbody></table></div>
      <div className={styles.actions}><button data-i18n="components.memory.MemoryImportForm.text032" className={styles.cancelButton} onClick={reset}>{uiText("components.memory.MemoryImportForm.text032")}</button><button data-i18n="components.memory.MemoryImportForm.text033" className={styles.importButton} onClick={startOfficial} disabled={selectedIds.size === 0 || isLoading}>{selectedIds.size}{uiText("components.memory.MemoryImportForm.text033")}</button></div>
    </div>;
  }

  const accept = source === 'official' ? '.json,.zip' : source === 'native' ? '.json' : '.json,.md,.txt';
  return <>
    <div className={`${styles.uploadArea} ${isDragOver ? styles.uploadAreaDragOver : ''}`} onClick={() => fileInputRef.current?.click()} onDragEnter={handleDragEnter} onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={handleDrop}>
      {isLoading ? <Loader2 className={`${styles.uploadIcon} ${styles.loader}`} size={48} /> : <Download className={styles.uploadIcon} size={48} />}
      <div data-i18n="components.memory.MemoryImportForm.text034 components.memory.MemoryImportForm.text035" className={styles.uploadText}>{isDragOver ? uiText("components.memory.MemoryImportForm.text034") : uiText("components.memory.MemoryImportForm.text035")}</div>
      <input type="file" ref={fileInputRef} className={styles.fileInput} onChange={(e) => { const file = e.target.files?.[0]; if (file) void handleFile(file); e.target.value = ''; }} accept={accept} disabled={isLoading} />
    </div>
    {resultBanner}
  </>;
}
