'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import {
  AlertCircle, BookOpen, Bot, CheckCircle, CheckSquare, FolderOpen,
  Download, Loader2, MessageSquare, MessagesSquare, Square,
} from 'lucide-react';
import React, { useCallback, useRef, useState } from 'react';

import {
  activateThread,
  fetchThreads,
  getImportStatus,
  importClaude,
  importClaudeMemories,
  previewClaudeImport,
} from '@/lib/memory/importClient';

import styles from './MemoryImport.module.css';
import { MemoryImportProgress } from './MemoryImportProgress';
import { formatImportDate, formatProgress, formatThreadDateRange, sortThreadsByLastActivity } from './formatters';
import {
  ClaudeCategory, ClaudePreviewData, ClaudePreviewErrorDetail,
  MemoryImportProgress as ImportProgressData, MemoryImportUiResult, ThreadSummary,
} from './types';

/**
 * Claude (claude.ai) 公式エクスポートの取り込みタブ。
 *
 * 公式エクスポートは manifest JSON 経由で複数のカテゴリ別 ZIP に分かれて
 * ダウンロードされるため、カテゴリごとのドロップ枠と手順ガイドを置く。
 * conversations / claude_code / projects は会話選択 → スレッド取り込み、
 * memories は項目選択 → Memopedia 転記。frames は取り込み対象外。
 * 設計: docs/intent/claude_import.md
 */

type Phase = 'home' | 'select' | 'memory-select' | 'embedding' | 'importing' | 'thread-select';

interface CategoryRow {
  category: ClaudeCategory;
  fileHint: string;
  nameKey: string;
  descKey: string;
  Icon: typeof MessageSquare;
}

const CATEGORY_ROWS: CategoryRow[] = [
  {
    category: 'conversations', fileHint: 'conversations-000.zip', Icon: MessagesSquare,
    nameKey: 'components.memory.ClaudeImportPanel.text010',
    descKey: 'components.memory.ClaudeImportPanel.text011',
  },
  {
    category: 'claude_code', fileHint: 'claude_code_cloud-000.zip', Icon: Bot,
    nameKey: 'components.memory.ClaudeImportPanel.text012',
    descKey: 'components.memory.ClaudeImportPanel.text013',
  },
  {
    category: 'projects', fileHint: 'projects-000.zip', Icon: FolderOpen,
    nameKey: 'components.memory.ClaudeImportPanel.text014',
    descKey: 'components.memory.ClaudeImportPanel.text015',
  },
  {
    category: 'memories', fileHint: 'memories-000.zip', Icon: BookOpen,
    nameKey: 'components.memory.ClaudeImportPanel.text016',
    descKey: 'components.memory.ClaudeImportPanel.text017',
  },
];

// i18n の静的検査 (check-i18n.mjs) はリテラルのキー文字列しか拾えないため、
// 表のループで使うラベルはここでリテラル呼び出しに束ねる。
function categoryName(category: string): string {
  switch (category) {
    case 'conversations': return uiText('components.memory.ClaudeImportPanel.text010');
    case 'claude_code': return uiText('components.memory.ClaudeImportPanel.text012');
    case 'projects': return uiText('components.memory.ClaudeImportPanel.text014');
    case 'memories': return uiText('components.memory.ClaudeImportPanel.text016');
    default: return category;
  }
}

function categoryDesc(category: string): string {
  switch (category) {
    case 'conversations': return uiText('components.memory.ClaudeImportPanel.text011');
    case 'claude_code': return uiText('components.memory.ClaudeImportPanel.text013');
    case 'projects': return uiText('components.memory.ClaudeImportPanel.text015');
    case 'memories': return uiText('components.memory.ClaudeImportPanel.text017');
    default: return '';
  }
}

function kindLabel(kind: string): string {
  switch (kind) {
    case 'core_memory': return uiText('components.memory.ClaudeImportPanel.text030');
    case 'project_memory': return uiText('components.memory.ClaudeImportPanel.text031');
    case 'memory_file': return uiText('components.memory.ClaudeImportPanel.text032');
    default: return kind;
  }
}

function previewErrorMessage(detail: string | ClaudePreviewErrorDetail | undefined): string {
  if (!detail) return uiText('components.memory.ClaudeImportPanel.text020');
  if (typeof detail === 'string') return detail;
  switch (detail.code) {
    case 'frames_not_supported':
      return uiText('components.memory.ClaudeImportPanel.text021');
    case 'manifest_not_supported':
      return uiText('components.memory.ClaudeImportPanel.text022');
    case 'light_metadata_not_supported':
      return uiText('components.memory.ClaudeImportPanel.text023');
    case 'category_mismatch': {
      const detected = detail.detected_category || '';
      if (detected === 'frames') return uiText('components.memory.ClaudeImportPanel.text021');
      if (detected === 'manifest') return uiText('components.memory.ClaudeImportPanel.text022');
      const name = categoryName(detected);
      if (name && name !== detected) return uiText('components.memory.ClaudeImportPanel.text024', { p1: name });
      return uiText('components.memory.ClaudeImportPanel.text020');
    }
    default:
      return uiText('components.memory.ClaudeImportPanel.text020');
  }
}

export default function ClaudeImportPanel({ personaId, onImportComplete }: {
  personaId: string;
  onImportComplete?: () => void;
}) {
  useLocale();
  const [phase, setPhase] = useState<Phase>('home');
  const [isLoading, setIsLoading] = useState(false);
  const [preview, setPreview] = useState<ClaudePreviewData | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [appendInstructions, setAppendInstructions] = useState(false);
  const [result, setResult] = useState<MemoryImportUiResult | null>(null);
  const [importProgress, setImportProgress] = useState<string | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [dragCategory, setDragCategory] = useState<string | null>(null);
  const dragCounters = useRef<Record<string, number>>({});
  const fileInputRefs = useRef<Record<string, HTMLInputElement | null>>({});

  const reset = () => {
    setPhase('home'); setPreview(null); setSelectedIds(new Set());
    setAppendInstructions(false); setResult(null); setImportProgress(null);
    setThreads([]); setSelectedThreadId(null);
  };

  const handleFile = async (category: ClaudeCategory, file: File) => {
    setIsLoading(true); setResult(null);
    const { ok, data } = await previewClaudeImport(personaId, file, category);
    setIsLoading(false);
    if (!ok || !('cache_key' in data)) {
      const detail = 'detail' in data ? data.detail : undefined;
      setResult({ type: 'error', message: previewErrorMessage(detail) });
      return;
    }
    if (data.total_count === 0 || !data.cache_key) {
      setResult({ type: 'error', message: uiText('components.memory.ClaudeImportPanel.text025') });
      return;
    }
    setPreview(data);
    setSelectedIds(new Set());
    setAppendInstructions(false);
    setPhase(data.category === 'memories' ? 'memory-select' : 'select');
  };

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
      setResult({ type: 'success', message: uiText('components.memory.ClaudeImportPanel.text040', { p1: message }) });
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
      setTimeout(() => { void getImportStatus(personaId, 'claude').then(watchEmbedding); }, 1000);
      return;
    }
    if (data.embedding_error) setResult({ type: 'error', message: data.embedding_error });
  };

  const pollStatus = async () => {
    const data = await getImportStatus(personaId, 'claude');
    if (data.running) {
      setImportProgress(formatProgress(data.message, data.progress, data.total));
      setTimeout(() => void pollStatus(), 1000);
      return;
    }
    setImportProgress(null); setIsLoading(false);
    if (data.success) {
      await handleImportSuccess(data.message || uiText('components.memory.ClaudeImportPanel.text041'));
      watchEmbedding(data);
      return;
    }
    setPhase('select');
    setResult({ type: 'error', message: data.message || uiText('components.memory.ClaudeImportPanel.text042') });
  };

  const executeImport = async (skipEmbedding: boolean) => {
    if (!preview) return;
    setPhase('importing'); setIsLoading(true); setResult(null);
    setImportProgress(uiText('components.memory.ClaudeImportPanel.text043'));
    const { ok, data } = await importClaude(personaId, {
      cache_key: preview.cache_key,
      conversation_ids: Array.from(selectedIds).map(String),
      skip_embedding: skipEmbedding,
      append_instructions_to_system_prompt: preview.category === 'projects' && appendInstructions,
    });
    if (!ok) {
      setPhase('select'); setIsLoading(false); setImportProgress(null);
      setResult({ type: 'error', message: data.detail || uiText('components.memory.ClaudeImportPanel.text042') });
      return;
    }
    setTimeout(() => void pollStatus(), 1000);
  };

  const executeMemoryImport = async () => {
    if (!preview) return;
    setIsLoading(true); setResult(null);
    const { ok, data } = await importClaudeMemories(
      personaId, preview.cache_key, Array.from(selectedIds).map(String),
    );
    setIsLoading(false);
    if (!ok) {
      setResult({ type: 'error', message: (typeof data.detail === 'string' && data.detail) || uiText('components.memory.ClaudeImportPanel.text042') });
      return;
    }
    setPhase('home'); setPreview(null); setSelectedIds(new Set());
    const lines: string[] = [];
    if ((data.memopedia_requested ?? 0) > 0) {
      lines.push(uiText('components.memory.ClaudeImportPanel.text033', {
        p1: data.trunk_title ?? '', p2: data.created ?? 0, p3: data.skipped ?? 0,
      }));
    }
    if ((data.core_requested ?? 0) > 0) {
      lines.push(uiText('components.memory.ClaudeImportPanel.text039', {
        p1: data.core_created ?? 0, p2: data.core_skipped ?? 0,
      }));
      const total = data.core_total_chars ?? 0;
      const budget = data.core_budget ?? 0;
      if (budget > 0 && total > budget) {
        lines.push(uiText('components.memory.ClaudeImportPanel.text053', { p1: total, p2: budget }));
      }
    }
    setResult({ type: 'success', message: lines.join(' ') });
    onImportComplete?.();
  };

  const confirmThread = async () => {
    if (!selectedThreadId) return;
    setIsLoading(true);
    await activateThread(personaId, selectedThreadId);
    setIsLoading(false);
    setPhase('home');
    setResult({ type: 'success', message: uiText('components.memory.ClaudeImportPanel.text044') });
    onImportComplete?.();
  };

  const makeDropHandlers = useCallback((category: string, onFile: (file: File) => void) => ({
    onDragEnter: (e: React.DragEvent) => {
      e.preventDefault(); e.stopPropagation();
      dragCounters.current[category] = (dragCounters.current[category] || 0) + 1;
      setDragCategory(category);
    },
    onDragOver: (e: React.DragEvent) => { e.preventDefault(); e.stopPropagation(); },
    onDragLeave: (e: React.DragEvent) => {
      e.preventDefault(); e.stopPropagation();
      dragCounters.current[category] = (dragCounters.current[category] || 0) - 1;
      if (dragCounters.current[category] <= 0) {
        dragCounters.current[category] = 0;
        setDragCategory((prev) => (prev === category ? null : prev));
      }
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault(); e.stopPropagation();
      dragCounters.current[category] = 0; setDragCategory(null);
      const file = e.dataTransfer.files[0];
      if (file) onFile(file);
    },
  }), []);

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
    return <MemoryImportProgress message={importProgress} />;
  }

  if (phase === 'embedding') {
    return (
      <div className={styles.embeddingDialog}>
        <h3 data-i18n="components.memory.ClaudeImportPanel.text050">{uiText('components.memory.ClaudeImportPanel.text050')}</h3>
        <div className={styles.dialogActions}>
          <button data-i18n="components.memory.ClaudeImportPanel.text051" className={styles.cancelButton} onClick={() => void executeImport(true)}>{uiText('components.memory.ClaudeImportPanel.text051')}</button>
          <button data-i18n="components.memory.ClaudeImportPanel.text052" className={styles.importButton} onClick={() => void executeImport(false)}>{uiText('components.memory.ClaudeImportPanel.text052')}</button>
        </div>
      </div>
    );
  }

  if (phase === 'thread-select') {
    return (
      <div className={styles.threadSelectContainer}>
        <div className={styles.threadSelectHeader}><MessageSquare size={24} /><h3 data-i18n="components.memory.ClaudeImportPanel.text045">{uiText('components.memory.ClaudeImportPanel.text045')}</h3></div>
        {resultBanner}
        <div className={styles.threadList}>{threads.map((thread) => {
          const meta = [
            uiText('components.memory.ClaudeImportPanel.text046', { p1: thread.message_count ?? 0 }),
            formatThreadDateRange(thread.first_created_at, thread.last_created_at),
            thread.title ? (thread.suffix || thread.thread_id) : '',
          ].filter(Boolean).join(' · ');
          return (
            <div key={thread.thread_id} className={`${styles.threadItem} ${selectedThreadId === thread.thread_id ? styles.selected : ''}`} onClick={() => setSelectedThreadId(thread.thread_id)}>
              <div className={styles.threadContent}>
                <div className={styles.threadName}>{thread.title || thread.suffix || thread.thread_id}</div>
                <div className={styles.threadMetaLine}>{meta}</div>
                <div data-i18n="components.memory.ClaudeImportPanel.text047" className={styles.threadPreview}>{thread.preview || uiText('components.memory.ClaudeImportPanel.text047')}</div>
              </div>
            </div>
          );
        })}</div>
        <div className={styles.actions}>
          <button data-i18n="components.memory.ClaudeImportPanel.text048" className={styles.cancelButton} onClick={() => { setPhase('home'); onImportComplete?.(); }}>{uiText('components.memory.ClaudeImportPanel.text048')}</button>
          <button data-i18n="components.memory.ClaudeImportPanel.text049" className={styles.importButton} onClick={() => void confirmThread()} disabled={!selectedThreadId || isLoading}>{uiText('components.memory.ClaudeImportPanel.text049')}</button>
        </div>
      </div>
    );
  }

  if (phase === 'select' && preview) {
    const allSelected = selectedIds.size === preview.conversations.length;
    return (
      <div className={styles.selectionContainer}>
        <div className={styles.selectionHeader}>
          <h3 data-i18n="components.memory.ClaudeImportPanel.text060">{uiText('components.memory.ClaudeImportPanel.text060')}</h3>
          <span data-i18n="components.memory.ClaudeImportPanel.text061" className={styles.selectionCount}>{uiText('components.memory.ClaudeImportPanel.text061', { p1: preview.total_count, p2: selectedIds.size })}</span>
        </div>
        {resultBanner}
        <div className={styles.tableContainer}><table className={styles.table}>
          <thead><tr>
            <th className={styles.checkboxCell} onClick={() => toggleSelectAll(preview.conversations.length)}>{allSelected ? <CheckSquare size={18} /> : <Square size={18} />}</th>
            <th data-i18n="components.memory.ClaudeImportPanel.text062">{uiText('components.memory.ClaudeImportPanel.text062')}</th>
            <th data-i18n="components.memory.ClaudeImportPanel.text063">{uiText('components.memory.ClaudeImportPanel.text063')}</th>
            <th data-i18n="components.memory.ClaudeImportPanel.text064">{uiText('components.memory.ClaudeImportPanel.text064')}</th>
            <th data-i18n="components.memory.ClaudeImportPanel.text065">{uiText('components.memory.ClaudeImportPanel.text065')}</th>
          </tr></thead>
          <tbody>{preview.conversations.map((conv) => (
            <tr key={conv.idx} className={selectedIds.has(conv.idx) ? styles.selected : ''} onClick={() => toggleSelection(conv.idx)}>
              <td className={styles.checkboxCell}>{selectedIds.has(conv.idx) ? <CheckSquare size={18} /> : <Square size={18} />}</td>
              <td className={styles.titleCell}>
                {conv.title || uiText('components.memory.ClaudeImportPanel.text066')}
                {conv.has_instructions && <span data-i18n="components.memory.ClaudeImportPanel.text067" className={styles.claudeBadge}>{uiText('components.memory.ClaudeImportPanel.text067')}</span>}
              </td>
              <td>{conv.message_count}</td>
              <td>{formatImportDate(conv.create_time)}</td>
              <td className={styles.previewCell}>{conv.preview || '-'}</td>
            </tr>
          ))}</tbody>
        </table></div>
        {preview.category === 'projects' && (
          <label className={styles.claudeAppendOption}>
            <input type="checkbox" checked={appendInstructions} onChange={(e) => setAppendInstructions(e.target.checked)} />
            <span data-i18n="components.memory.ClaudeImportPanel.text068">{uiText('components.memory.ClaudeImportPanel.text068')}</span>
          </label>
        )}
        <div className={styles.actions}>
          <button data-i18n="components.memory.ClaudeImportPanel.text069" className={styles.cancelButton} onClick={reset}>{uiText('components.memory.ClaudeImportPanel.text069')}</button>
          <button data-i18n="components.memory.ClaudeImportPanel.text070" className={styles.importButton} onClick={() => { if (selectedIds.size > 0) setPhase('embedding'); }} disabled={selectedIds.size === 0 || isLoading}>{uiText('components.memory.ClaudeImportPanel.text070', { p1: selectedIds.size })}</button>
        </div>
      </div>
    );
  }

  if (phase === 'memory-select' && preview) {
    const allSelected = selectedIds.size === preview.memory_items.length;
    return (
      <div className={styles.selectionContainer}>
        <div className={styles.selectionHeader}>
          <h3 data-i18n="components.memory.ClaudeImportPanel.text034">{uiText('components.memory.ClaudeImportPanel.text034')}</h3>
          <span data-i18n="components.memory.ClaudeImportPanel.text061" className={styles.selectionCount}>{uiText('components.memory.ClaudeImportPanel.text061', { p1: preview.memory_items.length, p2: selectedIds.size })}</span>
        </div>
        {resultBanner}
        <p data-i18n="components.memory.ClaudeImportPanel.text035" className={styles.claudeNote}>{uiText('components.memory.ClaudeImportPanel.text035')}</p>
        <div className={styles.tableContainer}><table className={styles.table}>
          <thead><tr>
            <th className={styles.checkboxCell} onClick={() => toggleSelectAll(preview.memory_items.length)}>{allSelected ? <CheckSquare size={18} /> : <Square size={18} />}</th>
            <th data-i18n="components.memory.ClaudeImportPanel.text036">{uiText('components.memory.ClaudeImportPanel.text036')}</th>
            <th data-i18n="components.memory.ClaudeImportPanel.text062">{uiText('components.memory.ClaudeImportPanel.text062')}</th>
            <th data-i18n="components.memory.ClaudeImportPanel.text037">{uiText('components.memory.ClaudeImportPanel.text037')}</th>
            <th data-i18n="components.memory.ClaudeImportPanel.text065">{uiText('components.memory.ClaudeImportPanel.text065')}</th>
          </tr></thead>
          <tbody>{preview.memory_items.map((item) => (
            <tr key={item.idx} className={selectedIds.has(item.idx) ? styles.selected : ''} onClick={() => toggleSelection(item.idx)}>
              <td className={styles.checkboxCell}>{selectedIds.has(item.idx) ? <CheckSquare size={18} /> : <Square size={18} />}</td>
              <td><span className={styles.claudeBadge}>{kindLabel(item.kind)}</span></td>
              <td className={styles.titleCell}>{item.title}</td>
              <td>{item.char_count}</td>
              <td className={styles.previewCell}>{item.preview || '-'}</td>
            </tr>
          ))}</tbody>
        </table></div>
        <div className={styles.actions}>
          <button data-i18n="components.memory.ClaudeImportPanel.text069" className={styles.cancelButton} onClick={reset}>{uiText('components.memory.ClaudeImportPanel.text069')}</button>
          <button data-i18n="components.memory.ClaudeImportPanel.text038" className={styles.importButton} onClick={() => void executeMemoryImport()} disabled={selectedIds.size === 0 || isLoading}>
            {isLoading ? <Loader2 size={16} className={styles.loader} /> : null}
            {uiText('components.memory.ClaudeImportPanel.text038', { p1: selectedIds.size })}
          </button>
        </div>
      </div>
    );
  }

  // phase === 'home'
  return (
    <div className={styles.claudeContainer}>
      <div className={styles.claudeGuide}>
        <h3 data-i18n="components.memory.ClaudeImportPanel.text001">{uiText('components.memory.ClaudeImportPanel.text001')}</h3>
        <ol className={styles.claudeSteps}>
          <li data-i18n="components.memory.ClaudeImportPanel.text002">{uiText('components.memory.ClaudeImportPanel.text002')}</li>
          <li data-i18n="components.memory.ClaudeImportPanel.text003">{uiText('components.memory.ClaudeImportPanel.text003')}</li>
          <li data-i18n="components.memory.ClaudeImportPanel.text004">{uiText('components.memory.ClaudeImportPanel.text004')}</li>
          <li data-i18n="components.memory.ClaudeImportPanel.text005">{uiText('components.memory.ClaudeImportPanel.text005')}</li>
        </ol>
        <p data-i18n="components.memory.ClaudeImportPanel.text006" className={styles.claudeNote}>{uiText('components.memory.ClaudeImportPanel.text006')}</p>
      </div>

      {resultBanner}

      <div className={styles.claudeCategoryList}>
        {CATEGORY_ROWS.map(({ category, fileHint, nameKey, descKey, Icon }) => (
          <div
            key={category}
            className={`${styles.claudeCategoryRow} ${dragCategory === category ? styles.uploadAreaDragOver : ''}`}
            onClick={() => fileInputRefs.current[category]?.click()}
            {...makeDropHandlers(category, (file) => void handleFile(category, file))}
          >
            <Icon size={28} className={styles.claudeCategoryIcon} />
            <div className={styles.claudeCategoryInfo}>
              <div className={styles.claudeCategoryName}>
                <span data-i18n={nameKey}>{categoryName(category)}</span>
                <code className={styles.claudeCategoryFile}>{fileHint}</code>
              </div>
              <div data-i18n={descKey} className={styles.claudeCategoryDesc}>{categoryDesc(category)}</div>
            </div>
            {isLoading ? <Loader2 size={20} className={styles.loader} /> : <Download size={20} className={styles.claudeCategoryUpload} />}
            <input
              type="file"
              ref={(el) => { fileInputRefs.current[category] = el; }}
              className={styles.fileInput}
              accept=".zip,.json"
              disabled={isLoading}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void handleFile(category, file);
                e.target.value = '';
              }}
            />
          </div>
        ))}
        <div className={`${styles.claudeCategoryRow} ${styles.claudeCategoryDisabled}`}>
          <AlertCircle size={28} className={styles.claudeCategoryIcon} />
          <div className={styles.claudeCategoryInfo}>
            <div className={styles.claudeCategoryName}>
              <span data-i18n="components.memory.ClaudeImportPanel.text018">{uiText('components.memory.ClaudeImportPanel.text018')}</span>
              <code className={styles.claudeCategoryFile}>frames-000.zip</code>
            </div>
            <div data-i18n="components.memory.ClaudeImportPanel.text019" className={styles.claudeCategoryDesc}>{uiText('components.memory.ClaudeImportPanel.text019')}</div>
          </div>
        </div>
      </div>
    </div>
  );
}
