'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import {
  AlertCircle, CheckCircle, CheckSquare, Download, Loader2, MessageSquare, Square,
} from 'lucide-react';
import React, { useCallback, useRef, useState } from 'react';

import {
  activateThread,
  fetchThreads,
  getImportStatus,
  importMuse,
  previewMuseImport,
} from '@/lib/memory/importClient';

import styles from './MemoryImport.module.css';
import { MemoryImportProgress } from './MemoryImportProgress';
import { formatImportDate, formatThreadDateRange, sortThreadsByLastActivity } from './formatters';
import {
  MemoryImportProgress as ImportProgressData, MemoryImportUiResult, MusePreviewData, ThreadSummary,
} from './types';

/**
 * Muse (Meta personal AI) 公式エクスポートの取り込みタブ。
 *
 * Muse の「Download your agent data」で取得した EYI_Package の ZIP を
 * 会話選択 → スレッド取り込みする。Muse のエクスポートには SOUL・長期記憶・
 * アイコンは含まれず、ワークスペースファイルはメタデータだけを扱う。
 * 設計: docs/intent/muse_export_import.md
 */

type Phase = 'home' | 'select' | 'embedding' | 'importing' | 'thread-select';

export default function MuseImportPanel({ personaId, onImportComplete }: {
  personaId: string;
  onImportComplete?: () => void;
}) {
  useLocale();
  const [phase, setPhase] = useState<Phase>('home');
  const [isLoading, setIsLoading] = useState(false);
  const [preview, setPreview] = useState<MusePreviewData | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [result, setResult] = useState<MemoryImportUiResult | null>(null);
  const [importProgress, setImportProgress] = useState<string | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const dragCounter = useRef(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const reset = () => {
    setPhase('home'); setPreview(null); setSelectedIds(new Set());
    setResult(null); setImportProgress(null); setThreads([]); setSelectedThreadId(null);
  };

  const handleFile = useCallback(async (file: File) => {
    setIsLoading(true); setResult(null);
    const { ok, data } = await previewMuseImport(personaId, file);
    setIsLoading(false);
    if (!ok || !('cache_key' in data)) {
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text009') });
      return;
    }
    if (data.total_count === 0 || !data.cache_key) {
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text010') });
      return;
    }
    setPreview(data);
    setSelectedIds(new Set());
    setPhase('select');
  }, [personaId]);

  const toggleSelection = (idx: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  };

  const toggleSelectAll = (total: number) => {
    setSelectedIds((prev) => (prev.size === total ? new Set() : new Set(Array.from({ length: total }, (_, i) => i))));
  };

  const handleImportSuccess = async (message: string) => {
    const threadList = sortThreadsByLastActivity(await fetchThreads(personaId));
    if (threadList.length <= 0) {
      setPhase('home');
      setResult({ type: 'success', message });
    } else if (threadList.length === 1) {
      await activateThread(personaId, threadList[0].thread_id);
      setPhase('home');
      setResult({
        type: 'success',
        message: uiText('components.memory.MuseImportPanel.text028', { p1: message }),
      });
      onImportComplete?.();
    } else {
      setThreads(threadList);
      setSelectedThreadId(threadList[0].thread_id);
      setPhase('thread-select');
      setResult({ type: 'success', message });
    }
    setPreview(null); setSelectedIds(new Set());
  };

  // 取り込み完了後の背景埋め込みを、画面を止めずに見送る (useMemoryImport と同じ)
  const watchEmbedding = (data: ImportProgressData) => {
    if (data.embedding_running === true) {
      setTimeout(() => { void getImportStatus(personaId, 'muse').then(watchEmbedding); }, 1000);
      return;
    }
    if (data.embedding_error) {
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text039') });
    }
  };

  const pollStatus = async () => {
    const data = await getImportStatus(personaId, 'muse');
    if (data.running) {
      setImportProgress(uiText('components.memory.MuseImportPanel.text026', {
        p1: data.progress ?? 0, p2: data.total ?? 0,
      }));
      setTimeout(() => void pollStatus(), 1000);
      return;
    }
    setImportProgress(null); setIsLoading(false);
    if (data.success) {
      const message = uiText('components.memory.MuseImportPanel.text027', {
        p1: data.conversations ?? 0, p2: data.messages ?? 0,
      });
      await handleImportSuccess(message);
      watchEmbedding(data);
      return;
    }
    setPhase('select');
    setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text029') });
  };

  const executeImport = async (skipEmbedding: boolean) => {
    if (!preview) return;
    setPhase('importing'); setIsLoading(true); setResult(null);
    setImportProgress(uiText('components.memory.MuseImportPanel.text025'));
    const { ok } = await importMuse(personaId, {
      cache_key: preview.cache_key,
      conversation_ids: Array.from(selectedIds).map(String),
      skip_embedding: skipEmbedding,
    });
    if (!ok) {
      setPhase('select'); setIsLoading(false); setImportProgress(null);
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text029') });
      return;
    }
    setTimeout(() => void pollStatus(), 1000);
  };

  const confirmThread = async () => {
    if (!selectedThreadId) return;
    setIsLoading(true);
    await activateThread(personaId, selectedThreadId);
    setIsLoading(false);
    setPhase('home');
    setResult({ type: 'success', message: uiText('components.memory.MuseImportPanel.text038') });
    onImportComplete?.();
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
    if (dragCounter.current <= 0) {
      dragCounter.current = 0;
      setIsDragOver(false);
    }
  }, []);
  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation();
    dragCounter.current = 0; setIsDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) void handleFile(file);
  }, [handleFile]);

  // 取り込みの成否は、どの画面に戻っても見えるよう各フェーズ共通で出す。
  // 失敗後に選択画面へ戻したとき、原因が見えないまま再実行して重複取り込み
  // を起こさないため。
  const resultBanner = result && (
    <div className={`${styles.result} ${result.type === 'success' ? styles.success : styles.error}`}>
      {result.type === 'success' ? <CheckCircle size={16} /> : <AlertCircle size={16} />}
      <span>{result.message}</span>
    </div>
  );

  if (phase === 'importing') {
    return (
      <div>
        {resultBanner}
        <MemoryImportProgress message={importProgress} />
      </div>
    );
  }

  if (phase === 'embedding') {
    return (
      <div className={styles.embeddingDialog}>
        {resultBanner}
        <h3 data-i18n="components.memory.MuseImportPanel.text030">{uiText('components.memory.MuseImportPanel.text030')}</h3>
        <div className={styles.dialogActions}>
          <button data-i18n="components.memory.MuseImportPanel.text031" className={styles.cancelButton} onClick={() => void executeImport(true)}>{uiText('components.memory.MuseImportPanel.text031')}</button>
          <button data-i18n="components.memory.MuseImportPanel.text032" className={styles.importButton} onClick={() => void executeImport(false)}>{uiText('components.memory.MuseImportPanel.text032')}</button>
        </div>
      </div>
    );
  }

  if (phase === 'thread-select') {
    return (
      <div className={styles.threadSelectContainer}>
        <div className={styles.threadSelectHeader}><MessageSquare size={24} /><h3 data-i18n="components.memory.MuseImportPanel.text033">{uiText('components.memory.MuseImportPanel.text033')}</h3></div>
        {resultBanner}
        <div className={styles.threadList}>{threads.map((thread) => {
          const meta = [
            uiText('components.memory.MuseImportPanel.text034', { p1: thread.message_count ?? 0 }),
            formatThreadDateRange(thread.first_created_at, thread.last_created_at),
            thread.title ? (thread.suffix || thread.thread_id) : '',
          ].filter(Boolean).join(' · ');
          return (
            <div key={thread.thread_id} className={`${styles.threadItem} ${selectedThreadId === thread.thread_id ? styles.selected : ''}`} onClick={() => setSelectedThreadId(thread.thread_id)}>
              <div className={styles.threadContent}>
                <div className={styles.threadName}>{thread.title || thread.suffix || thread.thread_id}</div>
                <div className={styles.threadMetaLine}>{meta}</div>
                <div data-i18n="components.memory.MuseImportPanel.text035" className={styles.threadPreview}>{thread.preview || uiText('components.memory.MuseImportPanel.text035')}</div>
              </div>
            </div>
          );
        })}</div>
        <div className={styles.actions}>
          <button data-i18n="components.memory.MuseImportPanel.text036" className={styles.cancelButton} onClick={() => { setPhase('home'); onImportComplete?.(); }}>{uiText('components.memory.MuseImportPanel.text036')}</button>
          <button data-i18n="components.memory.MuseImportPanel.text037" className={styles.importButton} onClick={() => void confirmThread()} disabled={!selectedThreadId || isLoading}>{uiText('components.memory.MuseImportPanel.text037')}</button>
        </div>
      </div>
    );
  }

  if (phase === 'select' && preview) {
    const allSelected = selectedIds.size === preview.conversations.length;
    return (
      <div className={styles.selectionContainer}>
        <div className={styles.selectionHeader}>
          <h3 data-i18n="components.memory.MuseImportPanel.text013">{uiText('components.memory.MuseImportPanel.text013')}</h3>
          <span data-i18n="components.memory.MuseImportPanel.text014" className={styles.selectionCount}>{uiText('components.memory.MuseImportPanel.text014', { p1: preview.total_count, p2: selectedIds.size })}</span>
        </div>
        {resultBanner}
        <p data-i18n="components.memory.MuseImportPanel.text020" className={styles.claudeNote}>{uiText('components.memory.MuseImportPanel.text020', { p1: preview.workspace_file_count })}</p>
        <p data-i18n="components.memory.MuseImportPanel.text021" className={styles.claudeNote}>{uiText('components.memory.MuseImportPanel.text021', { p1: formatImportDate(preview.exported_at) })}</p>
        <div className={styles.tableContainer}><table className={styles.table}>
          <thead><tr>
            <th className={styles.checkboxCell} onClick={() => toggleSelectAll(preview.conversations.length)}>{allSelected ? <CheckSquare size={18} /> : <Square size={18} />}</th>
            <th data-i18n="components.memory.MuseImportPanel.text015">{uiText('components.memory.MuseImportPanel.text015')}</th>
            <th data-i18n="components.memory.MuseImportPanel.text016">{uiText('components.memory.MuseImportPanel.text016')}</th>
            <th data-i18n="components.memory.MuseImportPanel.text017">{uiText('components.memory.MuseImportPanel.text017')}</th>
            <th data-i18n="components.memory.MuseImportPanel.text018">{uiText('components.memory.MuseImportPanel.text018')}</th>
          </tr></thead>
          <tbody>{preview.conversations.map((conv) => (
            <tr key={conv.idx} className={selectedIds.has(conv.idx) ? styles.selected : ''} onClick={() => toggleSelection(conv.idx)}>
              <td className={styles.checkboxCell}>{selectedIds.has(conv.idx) ? <CheckSquare size={18} /> : <Square size={18} />}</td>
              <td className={styles.titleCell}>{conv.title || uiText('components.memory.MuseImportPanel.text019')}</td>
              <td>{conv.message_count}</td>
              <td>{formatImportDate(conv.create_time)}</td>
              <td className={styles.previewCell}>{conv.preview || '-'}</td>
            </tr>
          ))}</tbody>
        </table></div>
        <div className={styles.actions}>
          <button data-i18n="components.memory.MuseImportPanel.text022" className={styles.cancelButton} onClick={reset}>{uiText('components.memory.MuseImportPanel.text022')}</button>
          <button data-i18n="components.memory.MuseImportPanel.text023" className={styles.importButton} onClick={() => { if (selectedIds.size > 0) setPhase('embedding'); }} disabled={selectedIds.size === 0 || isLoading}>{uiText('components.memory.MuseImportPanel.text023', { p1: selectedIds.size })}</button>
        </div>
      </div>
    );
  }

  // phase === 'home'
  return (
    <div className={styles.claudeContainer}>
      <div className={styles.claudeGuide}>
        <h3 data-i18n="components.memory.MuseImportPanel.text001">{uiText('components.memory.MuseImportPanel.text001')}</h3>
        <ol className={styles.claudeSteps}>
          <li data-i18n="components.memory.MuseImportPanel.text002">{uiText('components.memory.MuseImportPanel.text002')}</li>
          <li data-i18n="components.memory.MuseImportPanel.text003">{uiText('components.memory.MuseImportPanel.text003')}</li>
          <li data-i18n="components.memory.MuseImportPanel.text004">{uiText('components.memory.MuseImportPanel.text004')}</li>
        </ol>
        <p data-i18n="components.memory.MuseImportPanel.text005" className={styles.claudeNote}>{uiText('components.memory.MuseImportPanel.text005')}</p>
        <p data-i18n="components.memory.MuseImportPanel.text006" className={styles.claudeNote}>{uiText('components.memory.MuseImportPanel.text006')}</p>
      </div>

      {resultBanner}

      <div
        className={`${styles.uploadArea} ${isDragOver ? styles.uploadAreaDragOver : ''}`}
        onClick={() => fileInputRef.current?.click()}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {isLoading ? <Loader2 className={`${styles.uploadIcon} ${styles.loader}`} size={48} /> : <Download className={styles.uploadIcon} size={48} />}
        <div data-i18n="components.memory.MuseImportPanel.text007" className={styles.uploadText}>{uiText('components.memory.MuseImportPanel.text007')}</div>
        <code data-i18n="components.memory.MuseImportPanel.text008" className={styles.claudeCategoryFile}>{uiText('components.memory.MuseImportPanel.text008')}</code>
        <input
          type="file"
          ref={fileInputRef}
          className={styles.fileInput}
          accept=".zip"
          disabled={isLoading}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void handleFile(file);
            e.target.value = '';
          }}
        />
      </div>
    </div>
  );
}
