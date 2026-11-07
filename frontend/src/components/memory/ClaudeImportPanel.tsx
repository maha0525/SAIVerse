'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import {
  AlertCircle, BookOpen, Bot, CheckCircle, CheckSquare, FolderOpen,
  Download, Loader2, MessageSquare, MessagesSquare, Square,
} from 'lucide-react';
import React, { useCallback, useRef, useState } from 'react';

import {
  importClaude,
  importClaudeMemories,
  previewClaudeImport,
} from '@/lib/memory/importClient';

import styles from './MemoryImport.module.css';
import { formatImportDate } from './formatters';
import {
  ClaudeCategory, ClaudePreviewData, ClaudePreviewErrorDetail,
  ImportDirectResult, ImportStartRequest, MemoryImportUiResult,
} from './types';

/**
 * Claude (claude.ai) 公式エクスポートの取り込み、ImportFlowModal の段階 2。
 *
 * 公式エクスポートは manifest JSON 経由で複数のカテゴリ別 ZIP に分かれて
 * ダウンロードされるため、カテゴリごとのドロップ枠と手順ガイドを置く。
 * conversations / claude_code / projects は会話選択のあと onStart で取り込みを
 * ImportFlowModal に渡す。memories は項目選択 → Memopedia 転記をここで実行し、
 * onDirectDone で完了の段階へ進む。frames は取り込み対象外。
 * 設計: docs/intent/claude_import.md, docs/intent/import_flow_redesign.md
 */

type Phase = 'home' | 'select' | 'memory-select';

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

export default function ClaudeImportPanel({ personaId, onStart, onDirectDone }: {
  personaId: string;
  /** Runs a conversation import (steps 3-6 of ImportFlowModal). */
  onStart: (request: ImportStartRequest) => void;
  /** A memories import stores no conversations; the flow goes to its last step. */
  onDirectDone: (result: ImportDirectResult) => void;
}) {
  useLocale();
  const [phase, setPhase] = useState<Phase>('home');
  const [isLoading, setIsLoading] = useState(false);
  const [preview, setPreview] = useState<ClaudePreviewData | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [appendInstructions, setAppendInstructions] = useState(false);
  const [result, setResult] = useState<MemoryImportUiResult | null>(null);
  const [dragCategory, setDragCategory] = useState<string | null>(null);
  const dragCounters = useRef<Record<string, number>>({});
  const fileInputRefs = useRef<Record<string, HTMLInputElement | null>>({});

  const reset = () => {
    setPhase('home'); setPreview(null); setSelectedIds(new Set());
    setAppendInstructions(false); setResult(null);
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

  const startImport = () => {
    if (!preview || selectedIds.size === 0) return;
    const request = {
      cache_key: preview.cache_key,
      conversation_ids: Array.from(selectedIds).map(String),
      skip_embedding: true,
      append_instructions_to_system_prompt: preview.category === 'projects' && appendInstructions,
    };
    onStart({
      statusType: 'claude',
      initialProgress: uiText('components.memory.ClaudeImportPanel.text043'),
      selectThread: true,
      failureMessage: (data) => data.message || uiText('components.memory.ClaudeImportPanel.text042'),
      start: async () => {
        const { ok, data } = await importClaude(personaId, request);
        return { ok, error: data.detail || uiText('components.memory.ClaudeImportPanel.text042') };
      },
    });
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
    onDirectDone({ message: lines.join(' ') });
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
          <button data-i18n="components.memory.ClaudeImportPanel.text070" className={styles.importButton} onClick={startImport} disabled={selectedIds.size === 0 || isLoading}>{uiText('components.memory.ClaudeImportPanel.text070', { p1: selectedIds.size })}</button>
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
