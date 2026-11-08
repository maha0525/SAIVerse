'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { AlertCircle, CheckCircle, CheckSquare, Loader2, Square } from 'lucide-react';
import { RefObject, useCallback, useRef, useState } from 'react';

import { importMuseFiles, previewMuseFiles } from '@/lib/memory/importClient';

import styles from './MemoryImport.module.css';
import { MemoryImportUiResult, MuseFilePreviewData } from './types';

/**
 * Muse の SOUL.md・IDENTITY.md・MEMORY.md・AGENTS.md の取り込み
 * (`muse/files/preview` → `muse/files`) の共通部分。入口は二つあり、
 * メモリー画面の「ファイルからの引っ越し」の節 (MuseFileMigrationPanel) と、
 * 取り込みの専用ウィンドウの Muse の流れの必須二択ステップ (ImportFlowModal) が
 * どちらもこれを使う。ファイルを選ぶボタンは置き場所ごとに違うので各呼び出し側が
 * 持ち、ここはファイル選択の input・結果表示・プレビューの表と実行ボタンを持つ。
 * 設計: docs/intent/muse_export_import.md「後段：ファイル取り込み」,
 *       docs/intent/import_flow_redesign.md「Muse のファイル取り込み」
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

export interface MuseFileImportState {
  fileInputRef: RefObject<HTMLInputElement | null>;
  openPicker: () => void;
  busy: boolean;
  result: MemoryImportUiResult | null;
  preview: MuseFilePreviewData | null;
  selectedKinds: Set<string>;
  /** 取り込みが一度でも成功したか (ImportFlowModal が完了の要約に載せる)。 */
  imported: boolean;
  handleFilesSelected: (files: FileList | File[]) => Promise<void>;
  toggleKind: (kind: string) => void;
  cancelPreview: () => void;
  execute: () => Promise<void>;
}

export function useMuseFileImport(personaId: string, onImportComplete?: () => void): MuseFileImportState {
  const [result, setResult] = useState<MemoryImportUiResult | null>(null);
  const [preview, setPreview] = useState<MuseFilePreviewData | null>(null);
  const [selectedKinds, setSelectedKinds] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [imported, setImported] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const handleFilesSelected = useCallback(async (selectedFiles: FileList | File[]) => {
    const list = Array.from(selectedFiles);
    if (list.length === 0) return;
    // 新しいプレビュー要求を始める時点で以前の候補と選択を無効化する。
    // 失敗や対象0件のときに古いキャッシュが取り込まれる事故を防ぐため。
    setPreview(null);
    setSelectedKinds(new Set());
    setBusy(true);
    setResult(null);
    let response: Awaited<ReturnType<typeof previewMuseFiles>>;
    try {
      response = await previewMuseFiles(personaId, list);
    } catch (e) {
      // 通信の失敗も結果の欄に出して、選び直せる状態に戻す (行き止まりにしない)。
      console.error('Muse file preview failed', e);
      setBusy(false);
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text050') });
      return;
    }
    setBusy(false);
    const { ok, data } = response;
    if (!ok || !('cache_key' in data)) {
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text050') });
      return;
    }
    if (data.total_count === 0 || !data.cache_key) {
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text050') });
      return;
    }
    setPreview(data);
    const kinds = data.files
      .map((file) => file.kind)
      .filter((kind): kind is string => kind !== null);
    setSelectedKinds(new Set(kinds));
  }, [personaId]);

  const toggleKind = (kind: string) => {
    setSelectedKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind); else next.add(kind);
      return next;
    });
  };

  const cancelPreview = () => {
    setPreview(null);
    setSelectedKinds(new Set());
  };

  const execute = async () => {
    if (!preview) return;
    setBusy(true);
    setResult(null);
    let response: Awaited<ReturnType<typeof importMuseFiles>>;
    try {
      response = await importMuseFiles(personaId, preview.cache_key, Array.from(selectedKinds));
    } catch (e) {
      // プレビューは残し、そのまま「取り込む」を押し直せるようにする。
      console.error('Muse file import failed', e);
      setBusy(false);
      setResult({ type: 'error', message: uiText('components.memory.MuseImportPanel.text051') });
      return;
    }
    setBusy(false);
    const { ok, data } = response;
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
    setPreview(null);
    setSelectedKinds(new Set());
    setImported(true);
    onImportComplete?.();
  };

  return {
    fileInputRef,
    openPicker: () => fileInputRef.current?.click(),
    busy,
    result,
    preview,
    selectedKinds,
    imported,
    handleFilesSelected,
    toggleKind,
    cancelPreview,
    execute,
  };
}

/** ファイル選択の input (非表示)・結果表示・プレビューの表と実行ボタン。 */
export function MuseFileImportView({ state }: { state: MuseFileImportState }) {
  useLocale();
  const { fileInputRef, busy, result, preview, selectedKinds } = state;
  return (
    <>
      <input
        type="file"
        ref={fileInputRef}
        className={styles.fileInput}
        accept=".md"
        multiple
        disabled={busy}
        onChange={(e) => {
          const selected = e.target.files;
          if (selected && selected.length > 0) void state.handleFilesSelected(selected);
          e.target.value = '';
        }}
      />

      {result && (
        <div className={`${styles.result} ${result.type === 'success' ? styles.success : styles.error}`}>
          {result.type === 'success' ? <CheckCircle size={16} /> : <AlertCircle size={16} />}
          <span>{result.message}</span>
        </div>
      )}

      {preview && (
        <div style={{ marginTop: '1rem' }}>
          <div className={styles.tableContainer}>
            <table className={styles.table}>
              <tbody>{preview.files.map((file, idx) => {
                const selectable = file.kind !== null;
                const selected = selectable && selectedKinds.has(file.kind as string);
                return (
                  <tr
                    key={`${file.filename}-${file.kind ?? 'unsupported'}-${idx}`}
                    className={selected ? styles.selected : ''}
                    onClick={() => { if (selectable) state.toggleKind(file.kind as string); }}
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
              onClick={state.cancelPreview}
              disabled={busy}
            >
              {uiText('components.memory.MuseImportPanel.text049')}
            </button>
            <button
              data-i18n="components.memory.MuseImportPanel.text048"
              className={styles.importButton}
              onClick={() => void state.execute()}
              disabled={busy || selectedKinds.size === 0}
            >
              {busy ? <Loader2 size={16} className={styles.loader} /> : null}
              {uiText('components.memory.MuseImportPanel.text048')}
            </button>
          </div>
        </div>
      )}
    </>
  );
}
