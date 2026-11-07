'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { AlertCircle, CheckCircle, CheckSquare, Loader2, Square } from 'lucide-react';
import { useCallback, useRef, useState } from 'react';

import { importMuseFiles, previewMuseFiles } from '@/lib/memory/importClient';

import styles from './MemoryImport.module.css';
import { MemoryImportUiResult, MuseFilePreviewData } from './types';

/**
 * Muse の SOUL.md・IDENTITY.md・MEMORY.md・AGENTS.md を個別にアップロードして
 * システムプロンプト・コア記憶・Memopedia へ取り込む節。チャットログの取り込みとは
 * 別物なので、メモリー画面の取り込みタブに独立した節として置く
 * (ペルソナ作成ウィザードには出さない)。
 * 設計: docs/intent/muse_export_import.md, docs/intent/import_flow_redesign.md
 */

function destinationLabel(destination: string | null): string {
  switch (destination) {
    case 'system_prompt': return uiText('components.memory.MuseImportPanel.text043');
    case 'core_memory': return uiText('components.memory.MuseImportPanel.text044');
    case 'memopedia': return uiText('components.memory.MuseImportPanel.text045');
    default: return '';
  }
}

function memopediaActionLabel(action: string | null | undefined): string {
  switch (action) {
    case 'created': return uiText('components.memory.MuseImportPanel.text054');
    case 'updated': return uiText('components.memory.MuseImportPanel.text055');
    default: return uiText('components.memory.MuseImportPanel.text056');
  }
}

export default function MuseFileMigrationPanel({ personaId, onImportComplete }: {
  personaId: string;
  onImportComplete?: () => void;
}) {
  useLocale();
  const [result, setResult] = useState<MemoryImportUiResult | null>(null);
  const [filePreview, setFilePreview] = useState<MuseFilePreviewData | null>(null);
  const [selectedKinds, setSelectedKinds] = useState<Set<string>>(new Set());
  const [fileBusy, setFileBusy] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const handleFilesSelected = useCallback(async (selectedFiles: FileList | File[]) => {
    const list = Array.from(selectedFiles);
    if (list.length === 0) return;
    // 新しいプレビュー要求を始める時点で以前の候補と選択を無効化する。
    // 失敗や対象0件のときに古いキャッシュが取り込まれる事故を防ぐため。
    setFilePreview(null);
    setSelectedKinds(new Set());
    setFileBusy(true);
    setResult(null);
    const { ok, data } = await previewMuseFiles(personaId, list);
    setFileBusy(false);
    if (!ok || !('cache_key' in data)) {
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text050') });
      return;
    }
    if (data.total_count === 0 || !data.cache_key) {
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text050') });
      return;
    }
    setFilePreview(data);
    const kinds = data.files
      .map((file) => file.kind)
      .filter((kind): kind is string => kind !== null);
    setSelectedKinds(new Set(kinds));
  }, [personaId]);

  const toggleFileKind = (kind: string) => {
    setSelectedKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind); else next.add(kind);
      return next;
    });
  };

  const cancelFileImport = () => {
    setFilePreview(null);
    setSelectedKinds(new Set());
  };

  const executeFileImport = async () => {
    if (!filePreview) return;
    setFileBusy(true);
    setResult(null);
    const { ok, data } = await importMuseFiles(
      personaId, filePreview.cache_key, Array.from(selectedKinds),
    );
    setFileBusy(false);
    if (!ok || data.success === false) {
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text051') });
      return;
    }
    setResult({
      type: 'success',
      message: [
        uiText('components.memory.MuseImportPanel.text052'),
        uiText('components.memory.MuseImportPanel.text053', {
          p1: (data.system_prompt_appended ?? []).length,
          p2: data.core_created ?? 0,
          p3: memopediaActionLabel(data.memopedia_action),
        }),
      ].join(' '),
    });
    setFilePreview(null);
    setSelectedKinds(new Set());
    onImportComplete?.();
  };

  return (
    <div className={styles.reembedSection}>
      <h3 data-i18n="components.memory.MuseImportPanel.text040">{uiText('components.memory.MuseImportPanel.text040')}</h3>
      <p data-i18n="components.memory.MuseImportPanel.text041">{uiText('components.memory.MuseImportPanel.text041')}</p>
      <div className={styles.reembedActions}>
        <button
          data-i18n="components.memory.MuseImportPanel.text042"
          className={styles.reembedButtonSecondary}
          onClick={() => fileInputRef.current?.click()}
          disabled={fileBusy}
        >
          {fileBusy ? <Loader2 size={16} className={styles.loader} /> : null}
          {uiText('components.memory.MuseImportPanel.text042')}
        </button>
      </div>
      <input
        type="file"
        ref={fileInputRef}
        className={styles.fileInput}
        accept=".md"
        multiple
        disabled={fileBusy}
        onChange={(e) => {
          const selected = e.target.files;
          if (selected && selected.length > 0) void handleFilesSelected(selected);
          e.target.value = '';
        }}
      />

      {result && (
        <div className={`${styles.result} ${result.type === 'success' ? styles.success : styles.error}`}>
          {result.type === 'success' ? <CheckCircle size={16} /> : <AlertCircle size={16} />}
          <span>{result.message}</span>
        </div>
      )}

      {filePreview && (
        <div style={{ marginTop: '1rem' }}>
          <div className={styles.tableContainer}>
            <table className={styles.table}>
              <tbody>{filePreview.files.map((file, idx) => {
                const selectable = file.kind !== null;
                const selected = selectable && selectedKinds.has(file.kind as string);
                return (
                  <tr
                    key={`${file.filename}-${file.kind ?? 'unsupported'}-${idx}`}
                    className={selected ? styles.selected : ''}
                    onClick={() => { if (selectable) toggleFileKind(file.kind as string); }}
                  >
                    <td className={styles.checkboxCell}>
                      {selectable ? (selected ? <CheckSquare size={18} /> : <Square size={18} />) : null}
                    </td>
                    <td className={styles.titleCell}>{file.filename}</td>
                    <td>
                      {file.kind === null
                        ? uiText('components.memory.MuseImportPanel.text046')
                        : destinationLabel(file.destination)}
                    </td>
                    <td>
                      {file.kind === 'memory'
                        ? uiText('components.memory.MuseImportPanel.text047', { p1: file.item_count })
                        : ''}
                    </td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
          <div className={styles.actions}>
            <button
              data-i18n="components.memory.MuseImportPanel.text049"
              className={styles.cancelButton}
              onClick={cancelFileImport}
              disabled={fileBusy}
            >
              {uiText('components.memory.MuseImportPanel.text049')}
            </button>
            <button
              data-i18n="components.memory.MuseImportPanel.text048"
              className={styles.importButton}
              onClick={() => void executeFileImport()}
              disabled={fileBusy || selectedKinds.size === 0}
            >
              {fileBusy ? <Loader2 size={16} className={styles.loader} /> : null}
              {uiText('components.memory.MuseImportPanel.text048')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
