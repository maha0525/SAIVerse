'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import {
  AlertCircle, CheckSquare, Download, Loader2, Square,
} from 'lucide-react';
import React, { useCallback, useRef, useState } from 'react';

import { importMuse, previewMuseImport } from '@/lib/memory/importClient';

import styles from './MemoryImport.module.css';
import { formatImportDate } from './formatters';
import { ImportStartRequest, MusePreviewData } from './types';

/**
 * Muse (Meta personal AI) 公式エクスポートの取り込み、ImportFlowModal の段階 2。
 *
 * Muse の「Download your agent data」で取得した EYI_Package の ZIP から
 * 会話を選び、onStart で取り込みを ImportFlowModal に渡す。Muse のエクスポートには
 * SOUL・長期記憶・アイコンは含まれず、ワークスペースファイルはメタデータだけを扱う。
 * SOUL.md などの個別ファイルの取り込みは、会話の取り込みの後に ImportFlowModal の
 * 必須二択ステップ (muse-files) で出る。あとから取り込む口は MuseFileMigrationPanel
 * (メモリー画面) にあり、どちらも MuseFileImport.tsx を共有する。
 * 設計: docs/intent/muse_export_import.md, docs/intent/import_flow_redesign.md
 */

type Phase = 'home' | 'select';

export default function MuseImportPanel({ personaId, onStart }: {
  personaId: string;
  onStart: (request: ImportStartRequest) => void;
}) {
  useLocale();
  const [phase, setPhase] = useState<Phase>('home');
  const [isLoading, setIsLoading] = useState(false);
  const [preview, setPreview] = useState<MusePreviewData | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const dragCounter = useRef(0);
  const zipFileInputRef = useRef<HTMLInputElement | null>(null);

  const reset = () => {
    setPhase('home'); setPreview(null); setSelectedIds(new Set()); setError(null);
  };

  const handleFile = useCallback(async (file: File) => {
    setIsLoading(true); setError(null);
    const { ok, data } = await previewMuseImport(personaId, file);
    setIsLoading(false);
    if (!ok || !('cache_key' in data)) {
      setError(uiText('components.memory.MuseImportPanel.text009'));
      return;
    }
    if (data.total_count === 0 || !data.cache_key) {
      setError(uiText('components.memory.MuseImportPanel.text010'));
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

  const startImport = () => {
    if (!preview || selectedIds.size === 0) return;
    const request = {
      cache_key: preview.cache_key,
      conversation_ids: Array.from(selectedIds).map(String),
      skip_embedding: true,
    };
    onStart({
      statusType: 'muse',
      initialProgress: uiText('components.memory.MuseImportPanel.text025'),
      selectThread: true,
      formatRunning: (data) => uiText('components.memory.MuseImportPanel.text026', {
        p1: data.progress ?? 0, p2: data.total ?? 0,
      }),
      failureMessage: () => uiText('components.memory.MuseImportPanel.text029'),
      start: async () => {
        const { ok } = await importMuse(personaId, request);
        return { ok, error: uiText('components.memory.MuseImportPanel.text029') };
      },
    });
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

  const errorBanner = error && (
    <div className={`${styles.result} ${styles.error}`}>
      <AlertCircle size={16} />
      <span>{error}</span>
    </div>
  );

  if (phase === 'select' && preview) {
    const allSelected = selectedIds.size === preview.conversations.length;
    return (
      <div className={styles.selectionContainer}>
        <div className={styles.selectionHeader}>
          <h3 data-i18n="components.memory.MuseImportPanel.text013">{uiText('components.memory.MuseImportPanel.text013')}</h3>
          <span data-i18n="components.memory.MuseImportPanel.text014" className={styles.selectionCount}>{uiText('components.memory.MuseImportPanel.text014', { p1: preview.total_count, p2: selectedIds.size })}</span>
        </div>
        {errorBanner}
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
          <button data-i18n="components.memory.MuseImportPanel.text023" className={styles.importButton} onClick={startImport} disabled={selectedIds.size === 0 || isLoading}>{uiText('components.memory.MuseImportPanel.text023', { p1: selectedIds.size })}</button>
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

      {errorBanner}

      <div
        className={`${styles.uploadArea} ${isDragOver ? styles.uploadAreaDragOver : ''}`}
        onClick={() => zipFileInputRef.current?.click()}
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
          ref={zipFileInputRef}
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
