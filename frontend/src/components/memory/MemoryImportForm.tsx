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
        <div className={styles.threadSelectHeader}><MessageSquare size={24} /><h3>どのスレッドから会話を続けますか？</h3></div>
        <div className={styles.threadList}>{props.threads.map((thread) => {
          // 1 行目 = 題名 (無ければ suffix)、2 行目 = 件数・期間 (題名があるときは suffix も)、3 行目 = 冒頭プレビュー
          const meta = [
            `${thread.message_count ?? 0} 件`,
            formatThreadDateRange(thread.first_created_at, thread.last_created_at),
            thread.title ? (thread.suffix || thread.thread_id) : '',
          ].filter(Boolean).join(' · ');
          return <div key={thread.thread_id} className={`${styles.threadItem} ${props.selectedThreadId === thread.thread_id ? styles.selected : ''}`} onClick={() => props.onSelectThread(thread.thread_id)}><div className={styles.threadContent}><div className={styles.threadName}>{thread.title || thread.suffix || thread.thread_id}</div><div className={styles.threadMetaLine}>{meta}</div><div className={styles.threadPreview}>{thread.preview || '(プレビューなし)'}</div></div></div>;
        })}</div>
        <div className={styles.actions}><button className={styles.cancelButton} onClick={props.onSkipThread}>スキップ</button><button className={styles.importButton} onClick={props.onConfirmThread} disabled={!props.selectedThreadId || props.isLoading}>このスレッドを使用</button></div>
      </div>;
    }
    if (props.step === 'embedding-dialog') {
      return <div className={styles.embeddingDialog}><h3>記憶想起用のエンベディングを作成しますか？</h3><div className={styles.dialogActions}><button className={styles.cancelButton} onClick={() => props.onEmbeddingChoice(true)}>スキップ</button><button className={styles.importButton} onClick={() => props.onEmbeddingChoice(false)}>作成する</button></div></div>;
    }
    if (props.activeSubTab === 'native') {
      if (props.step === 'select' && props.nativePreview) {
        return <div className={styles.selectionContainer}><div className={styles.selectionHeader}><h3>Native インポートプレビュー</h3><span className={styles.selectionCount}>{props.nativePreview.thread_count}スレッド, {props.nativePreview.total_messages}メッセージ</span></div>
          {props.nativePreview.source_persona !== props.personaId && <div className={`${styles.result} ${styles.error}`}><AlertCircle size={16} /><span>元のペルソナ ({props.nativePreview.source_persona}) とインポート先 ({props.personaId}) が異なります。スレッドIDはそのまま保持されます。</span></div>}
          {/* スレッド一覧と上書きの注意は 2026-02-22 の分割リファクタで落ちていた。2026-09-03 に復元。 */}
          <div className={styles.tableContainer}><table className={styles.table}><thead><tr><th>スレッドID</th><th>メッセージ数</th><th>Stelis</th><th>プレビュー</th></tr></thead><tbody>{props.nativePreview.threads.map((t, idx) => <tr key={idx}><td className={styles.titleCell}>{t.thread_id}</td><td>{t.message_count}</td><td>{t.has_stelis ? 'あり' : '-'}</td><td className={styles.previewCell}>{t.preview || '-'}</td></tr>)}</tbody></table></div>
          <div className={`${styles.result} ${styles.error}`} style={{ margin: '1rem 0 0 0' }}><AlertCircle size={16} /><span>同じIDのスレッドが既に存在する場合は上書きされます。</span></div>
          <div className={styles.actions}><button className={styles.cancelButton} onClick={props.onReset}>キャンセル</button><button className={styles.importButton} onClick={props.onOpenNativeImport}>インポート</button></div>
        </div>;
      }
      return <div className={`${styles.uploadArea} ${isDragOver ? styles.uploadAreaDragOver : ''}`} onClick={() => props.nativeFileInputRef.current?.click()} onDragEnter={handleDragEnter} onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={makeDropHandler(props.onNativeFileChange)}><Download className={styles.uploadIcon} size={48} /><div className={styles.uploadText}>{isDragOver ? 'ドロップしてアップロード' : 'クリックまたはドラッグ&ドロップでアップロード'}</div><input type="file" ref={props.nativeFileInputRef} className={styles.fileInput} onChange={(e) => e.target.files?.[0] && props.onNativeFileChange(e.target.files[0])} accept=".json" disabled={props.isLoading} /></div>;
    }
    if (props.activeSubTab === 'official' && props.step === 'select' && props.previewData) {
      return <div className={styles.selectionContainer}><div className={styles.selectionHeader}><h3>インポートする会話を選択</h3><span className={styles.selectionCount}>{props.previewData.total_count}件中 {props.selectedIds.size}件選択</span></div>
        {/* 列 (メッセージ数 / 作成日 / プレビュー) は 2026-02-22 の分割リファクタで
            落ちていた (API は返し続けていた)。2026-09-03 に復元。 */}
        <div className={styles.tableContainer}><table className={styles.table}><thead><tr><th className={styles.checkboxCell} onClick={props.onToggleSelectAll}>{allSelected ? <CheckSquare size={18} /> : <Square size={18} />}</th><th>タイトル</th><th>メッセージ数</th><th>作成日</th><th>プレビュー</th></tr></thead><tbody>{props.previewData.conversations.map((conv) => <tr key={conv.idx} className={props.selectedIds.has(conv.idx) ? styles.selected : ''} onClick={() => props.onToggleSelection(conv.idx)}><td className={styles.checkboxCell}>{props.selectedIds.has(conv.idx) ? <CheckSquare size={18} /> : <Square size={18} />}</td><td className={styles.titleCell}>{conv.title || '(無題)'}</td><td>{conv.message_count}</td><td>{formatImportDate(conv.create_time)}</td><td className={styles.previewCell}>{conv.preview || '-'}</td></tr>)}</tbody></table></div>
        <div className={styles.actions}><button className={styles.cancelButton} onClick={props.onReset}>キャンセル</button><button className={styles.importButton} onClick={props.onConfirmOfficial} disabled={props.selectedIds.size === 0 || props.isLoading}>{props.selectedIds.size}件をインポート</button></div>
      </div>;
    }
    return <div className={`${styles.uploadArea} ${isDragOver ? styles.uploadAreaDragOver : ''}`} onClick={() => props.fileInputRef.current?.click()} onDragEnter={handleDragEnter} onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={makeDropHandler(props.onFileChange)}>{props.isLoading ? <Loader2 className={`${styles.uploadIcon} ${styles.loader}`} size={48} /> : <Download className={styles.uploadIcon} size={48} />}<div className={styles.uploadText}>{isDragOver ? 'ドロップしてアップロード' : 'クリックまたはドラッグ&ドロップでアップロード'}</div><input type="file" ref={props.fileInputRef} className={styles.fileInput} onChange={(e) => e.target.files?.[0] && props.onFileChange(e.target.files[0])} accept={props.activeSubTab === 'official' ? '.json,.zip' : '.json,.md,.txt'} disabled={props.isLoading} /></div>;
  };

  return (
    <>
      <div className={styles.subTabs}>
        <button className={`${styles.subTab} ${props.activeSubTab === 'official' ? styles.active : ''}`} onClick={() => props.onTabChange('official')} disabled={props.step === 'importing'}>ChatGPT 公式エクスポート</button>
        <button className={`${styles.subTab} ${props.activeSubTab === 'extension' ? styles.active : ''}`} onClick={() => props.onTabChange('extension')} disabled={props.step === 'importing'}>拡張機能エクスポート</button>
        <button className={`${styles.subTab} ${props.activeSubTab === 'native' ? styles.active : ''}`} onClick={() => props.onTabChange('native')} disabled={props.step === 'importing'}>Native (SAIVerse)</button>
      </div>
      {mainContent()}
    </>
  );
}
