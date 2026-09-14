
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { AlertCircle, CheckSquare, Download, Loader2, MessageSquare, Square } from 'lucide-react';
import React, { useState, useRef, useCallback } from 'react';

import styles from './MemoryImport.module.css';
import { formatImportDate, formatThreadDateRange } from './formatters';
import { ImportSubTab, MemoryImportStep, NativePreviewData, PreviewData, ThreadSummary } from './types';

interface Props {
  personaId: string;
  activeSubTab: ImportSubTab;
  step: MemoryImportStep;
  isLoading: boolean;
  previewData: PreviewData | null;
  selectedIds: Set<number>;
  nativePreview: NativePreviewData | null;
  threads: ThreadSummary[];
  selectedThreadId: string | null;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  nativeFileInputRef: React.RefObject<HTMLInputElement | null>;
  onTabChange: (tab: ImportSubTab) => void;
  onReset: () => void;
  onFileChange: (file: File) => void;
  onNativeFileChange: (file: File) => void;
  onToggleSelection: (idx: number) => void;
  onToggleSelectAll: () => void;
  onConfirmOfficial: () => void;
  onEmbeddingChoice: (skip: boolean) => void;
  onSelectThread: (id: string) => void;
  onConfirmThread: () => void;
  onSkipThread: () => void;
  onOpenNativeImport: () => void;
}

export function MemoryImportForm(props: Props) {
    useLocale();
  const allSelected = props.previewData && props.selectedIds.size === props.previewData.conversations.length;
  const [isDragOver, setIsDragOver] = useState(false);
  const dragCounter = useRef(0);

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
  const makeDropHandler = useCallback((onFile: (file: File) => void) => (e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation();
    dragCounter.current = 0; setIsDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) onFile(file);
  }, []);

  const mainContent = () => {
    if (props.step === 'thread-select') {
      return <div className={styles.threadSelectContainer}>
        <div className={styles.threadSelectHeader}><MessageSquare size={24} /><h3 data-i18n="components.memory.MemoryImportForm.text001">{uiText("components.memory.MemoryImportForm.text001")}</h3></div>
        <div data-i18n="components.memory.MemoryImportForm.text002" className={styles.threadList}>{props.threads.map((thread) => {
          // 1 行目 = 題名 (無ければ suffix)、2 行目 = 件数・期間 (題名があるときは suffix も)、3 行目 = 冒頭プレビュー
          const meta = [
            uiText("components.memory.MemoryImportForm.text002", { p1: thread.message_count ?? 0 }),
            formatThreadDateRange(thread.first_created_at, thread.last_created_at),
            thread.title ? (thread.suffix || thread.thread_id) : '',
          ].filter(Boolean).join(' · ');
          return <div key={thread.thread_id} className={`${styles.threadItem} ${props.selectedThreadId === thread.thread_id ? styles.selected : ''}`} onClick={() => props.onSelectThread(thread.thread_id)}><div className={styles.threadContent}><div className={styles.threadName}>{thread.title || thread.suffix || thread.thread_id}</div><div className={styles.threadMetaLine}>{meta}</div><div data-i18n="components.memory.MemoryImportForm.text003" className={styles.threadPreview}>{thread.preview || uiText("components.memory.MemoryImportForm.text003")}</div></div></div>;
        })}</div>
        <div className={styles.actions}><button data-i18n="components.memory.MemoryImportForm.text004" className={styles.cancelButton} onClick={props.onSkipThread}>{uiText("components.memory.MemoryImportForm.text004")}</button><button data-i18n="components.memory.MemoryImportForm.text005" className={styles.importButton} onClick={props.onConfirmThread} disabled={!props.selectedThreadId || props.isLoading}>{uiText("components.memory.MemoryImportForm.text005")}</button></div>
      </div>;
    }
    if (props.step === 'embedding-dialog') {
      return <div className={styles.embeddingDialog}><h3 data-i18n="components.memory.MemoryImportForm.text006">{uiText("components.memory.MemoryImportForm.text006")}</h3><div className={styles.dialogActions}><button data-i18n="components.memory.MemoryImportForm.text007" className={styles.cancelButton} onClick={() => props.onEmbeddingChoice(true)}>{uiText("components.memory.MemoryImportForm.text007")}</button><button data-i18n="components.memory.MemoryImportForm.text008" className={styles.importButton} onClick={() => props.onEmbeddingChoice(false)}>{uiText("components.memory.MemoryImportForm.text008")}</button></div></div>;
    }
    if (props.activeSubTab === 'native') {
      if (props.step === 'select' && props.nativePreview) {
        return <div className={styles.selectionContainer}><div className={styles.selectionHeader}><h3 data-i18n="components.memory.MemoryImportForm.text009">{uiText("components.memory.MemoryImportForm.text009")}</h3><span data-i18n="components.memory.MemoryImportForm.text010 components.memory.MemoryImportForm.text011" className={styles.selectionCount}>{props.nativePreview.thread_count}{uiText("components.memory.MemoryImportForm.text010")}{props.nativePreview.total_messages}{uiText("components.memory.MemoryImportForm.text011")}</span></div>
          {props.nativePreview.source_persona !== props.personaId && <div className={`${styles.result} ${styles.error}`}><AlertCircle size={16} /><span data-i18n="components.memory.MemoryImportForm.text012 components.memory.MemoryImportForm.text013 components.memory.MemoryImportForm.text014">{uiText("components.memory.MemoryImportForm.text012")}{props.nativePreview.source_persona}{uiText("components.memory.MemoryImportForm.text013")}{props.personaId}{uiText("components.memory.MemoryImportForm.text014")}</span></div>}
          {/* スレッド一覧と上書きの注意は 2026-02-22 の分割リファクタで落ちていた。2026-09-03 に復元。 */}
          <div className={styles.tableContainer}><table className={styles.table}><thead><tr><th data-i18n="components.memory.MemoryImportForm.text015">{uiText("components.memory.MemoryImportForm.text015")}</th><th data-i18n="components.memory.MemoryImportForm.text016">{uiText("components.memory.MemoryImportForm.text016")}</th><th>{uiText("components.memory.MemoryImportForm.label001")}</th><th data-i18n="components.memory.MemoryImportForm.text017">{uiText("components.memory.MemoryImportForm.text017")}</th></tr></thead><tbody>{props.nativePreview.threads.map((t, idx) => <tr key={idx}><td className={styles.titleCell}>{t.thread_id}</td><td>{t.message_count}</td><td data-i18n="components.memory.MemoryImportForm.text018">{t.has_stelis ? uiText("components.memory.MemoryImportForm.text018") : '-'}</td><td className={styles.previewCell}>{t.preview || '-'}</td></tr>)}</tbody></table></div>
          <div className={`${styles.result} ${styles.error}`} style={{ margin: '1rem 0 0 0' }}><AlertCircle size={16} /><span data-i18n="components.memory.MemoryImportForm.text019">{uiText("components.memory.MemoryImportForm.text019")}</span></div>
          <div className={styles.actions}><button data-i18n="components.memory.MemoryImportForm.text020" className={styles.cancelButton} onClick={props.onReset}>{uiText("components.memory.MemoryImportForm.text020")}</button><button data-i18n="components.memory.MemoryImportForm.text021" className={styles.importButton} onClick={props.onOpenNativeImport}>{uiText("components.memory.MemoryImportForm.text021")}</button></div>
        </div>;
      }
      return <div className={`${styles.uploadArea} ${isDragOver ? styles.uploadAreaDragOver : ''}`} onClick={() => props.nativeFileInputRef.current?.click()} onDragEnter={handleDragEnter} onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={makeDropHandler(props.onNativeFileChange)}><Download className={styles.uploadIcon} size={48} /><div data-i18n="components.memory.MemoryImportForm.text022 components.memory.MemoryImportForm.text023" className={styles.uploadText}>{isDragOver ? uiText("components.memory.MemoryImportForm.text022") : uiText("components.memory.MemoryImportForm.text023")}</div><input type="file" ref={props.nativeFileInputRef} className={styles.fileInput} onChange={(e) => e.target.files?.[0] && props.onNativeFileChange(e.target.files[0])} accept=".json" disabled={props.isLoading} /></div>;
    }
    if (props.activeSubTab === 'official' && props.step === 'select' && props.previewData) {
      return <div className={styles.selectionContainer}><div className={styles.selectionHeader}><h3 data-i18n="components.memory.MemoryImportForm.text024">{uiText("components.memory.MemoryImportForm.text024")}</h3><span data-i18n="components.memory.MemoryImportForm.text025 components.memory.MemoryImportForm.text026" className={styles.selectionCount}>{props.previewData.total_count}{uiText("components.memory.MemoryImportForm.text025")}{props.selectedIds.size}{uiText("components.memory.MemoryImportForm.text026")}</span></div>
        {/* 列 (メッセージ数 / 作成日 / プレビュー) は 2026-02-22 の分割リファクタで
            落ちていた (API は返し続けていた)。2026-09-03 に復元。 */}
        <div className={styles.tableContainer}><table className={styles.table}><thead><tr><th className={styles.checkboxCell} onClick={props.onToggleSelectAll}>{allSelected ? <CheckSquare size={18} /> : <Square size={18} />}</th><th data-i18n="components.memory.MemoryImportForm.text027">{uiText("components.memory.MemoryImportForm.text027")}</th><th data-i18n="components.memory.MemoryImportForm.text028">{uiText("components.memory.MemoryImportForm.text028")}</th><th data-i18n="components.memory.MemoryImportForm.text029">{uiText("components.memory.MemoryImportForm.text029")}</th><th data-i18n="components.memory.MemoryImportForm.text030">{uiText("components.memory.MemoryImportForm.text030")}</th></tr></thead><tbody>{props.previewData.conversations.map((conv) => <tr key={conv.idx} className={props.selectedIds.has(conv.idx) ? styles.selected : ''} onClick={() => props.onToggleSelection(conv.idx)}><td className={styles.checkboxCell}>{props.selectedIds.has(conv.idx) ? <CheckSquare size={18} /> : <Square size={18} />}</td><td data-i18n="components.memory.MemoryImportForm.text031" className={styles.titleCell}>{conv.title || uiText("components.memory.MemoryImportForm.text031")}</td><td>{conv.message_count}</td><td>{formatImportDate(conv.create_time)}</td><td className={styles.previewCell}>{conv.preview || '-'}</td></tr>)}</tbody></table></div>
        <div className={styles.actions}><button data-i18n="components.memory.MemoryImportForm.text032" className={styles.cancelButton} onClick={props.onReset}>{uiText("components.memory.MemoryImportForm.text032")}</button><button data-i18n="components.memory.MemoryImportForm.text033" className={styles.importButton} onClick={props.onConfirmOfficial} disabled={props.selectedIds.size === 0 || props.isLoading}>{props.selectedIds.size}{uiText("components.memory.MemoryImportForm.text033")}</button></div>
      </div>;
    }
    return <div className={`${styles.uploadArea} ${isDragOver ? styles.uploadAreaDragOver : ''}`} onClick={() => props.fileInputRef.current?.click()} onDragEnter={handleDragEnter} onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={makeDropHandler(props.onFileChange)}>{props.isLoading ? <Loader2 className={`${styles.uploadIcon} ${styles.loader}`} size={48} /> : <Download className={styles.uploadIcon} size={48} />}<div data-i18n="components.memory.MemoryImportForm.text034 components.memory.MemoryImportForm.text035" className={styles.uploadText}>{isDragOver ? uiText("components.memory.MemoryImportForm.text034") : uiText("components.memory.MemoryImportForm.text035")}</div><input type="file" ref={props.fileInputRef} className={styles.fileInput} onChange={(e) => e.target.files?.[0] && props.onFileChange(e.target.files[0])} accept={props.activeSubTab === 'official' ? '.json,.zip' : '.json,.md,.txt'} disabled={props.isLoading} /></div>;
  };

  return (
    <>
      <div className={styles.subTabs}>
        <button data-i18n="components.memory.MemoryImportForm.text036" className={`${styles.subTab} ${props.activeSubTab === 'official' ? styles.active : ''}`} onClick={() => props.onTabChange('official')} disabled={props.step === 'importing'}>{uiText("components.memory.MemoryImportForm.text036")}</button>
        <button data-i18n="components.memory.MemoryImportForm.text037" className={`${styles.subTab} ${props.activeSubTab === 'extension' ? styles.active : ''}`} onClick={() => props.onTabChange('extension')} disabled={props.step === 'importing'}>{uiText("components.memory.MemoryImportForm.text037")}</button>
        <button className={`${styles.subTab} ${props.activeSubTab === 'native' ? styles.active : ''}`} onClick={() => props.onTabChange('native')} disabled={props.step === 'importing'}>{uiText("components.memory.MemoryImportForm.label002")}</button>
      </div>
      {mainContent()}
    </>
  );
}
