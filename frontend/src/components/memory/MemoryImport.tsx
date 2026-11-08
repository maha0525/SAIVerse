
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { Loader2, RefreshCw } from 'lucide-react';
import { useState } from 'react';

import { getReembedStatus, startReembed } from '@/lib/memory/importClient';

import styles from './MemoryImport.module.css';
import ImportFlowModal from './ImportFlowModal';
import ImportSourcePicker from './ImportSourcePicker';
import { MemoryImportResult } from './MemoryImportResult';
import MuseFileMigrationPanel from './MuseFileMigrationPanel';
import PersonaPackPanel from './PersonaPackPanel';
import { formatProgress } from './formatters';
import { ImportSource, MemoryImportProps, MemoryImportUiResult } from './types';

/**
 * The memory screen's import tab: step 1 of the chat-log import (the source
 * cards, which open ImportFlowModal), then the separate sections for Muse
 * file migration, the persona pack and embedding management.
 * Design: docs/intent/import_flow_redesign.md
 */
export default function MemoryImport({ personaId, onImportComplete }: MemoryImportProps) {
  useLocale();
  const [flowSource, setFlowSource] = useState<ImportSource | null>(null);
  const [isReembedding, setIsReembedding] = useState(false);
  const [reembedProgress, setReembedProgress] = useState<string | null>(null);
  const [reembedResult, setReembedResult] = useState<MemoryImportUiResult | null>(null);

  const handleReembed = async (force: boolean) => {
    setIsReembedding(true); setReembedResult(null);
    setReembedProgress(uiText('components.memory.MemoryImport.reembedStarting'));
    let data: Awaited<ReturnType<typeof startReembed>>;
    try {
      data = await startReembed(personaId, force);
    } catch (e) {
      setIsReembedding(false); setReembedProgress(null);
      setReembedResult({ type: 'error', message: e instanceof Error ? e.message : String(e) });
      return;
    }
    if (!data.success) { setIsReembedding(false); setReembedProgress(null); setReembedResult({ type: 'error', message: data.detail || data.message || uiText('components.memory.ImportFlowModal.reembedStartFallback') }); return; }
    // 状態の問い合わせが連続で失敗したら追跡を止める (ImportFlowModal と同じ考え方)。
    const poll = async (failures: number) => {
      let status: Awaited<ReturnType<typeof getReembedStatus>>;
      try {
        status = await getReembedStatus(personaId);
      } catch (e) {
        console.error('Re-embed status polling error', e);
        if (failures + 1 >= 8) {
          setIsReembedding(false); setReembedProgress(null);
          setReembedResult({ type: 'error', message: uiText('components.memory.ImportFlowModal.embeddingUntracked') });
          return;
        }
        setTimeout(() => void poll(failures + 1), 1000);
        return;
      }
      if (status.running) { setReembedProgress(formatProgress(status.message, status.progress, status.total)); setTimeout(() => void poll(0), 1000); return; }
      setIsReembedding(false); setReembedProgress(null); if (status.message) setReembedResult({ type: 'success', message: status.message });
    };
    setTimeout(() => void poll(0), 1000);
  };

  return (
    <div className={styles.container}>
      <h2 data-i18n="components.memory.MemoryImport.text001" className={styles.title}>{uiText("components.memory.MemoryImport.text001")}</h2>
      <ImportSourcePicker onSelect={setFlowSource} />

      {flowSource && (
        <ImportFlowModal
          key={flowSource}
          personaId={personaId}
          source={flowSource}
          // メモリー画面では閉じたら①へ戻るだけで、取り込みの要約 (onClose の
          // 第 2 引数) は使わない — 実績一覧はペルソナ作成ウィザードだけが持つ。
          onClose={() => setFlowSource(null)}
          onImportComplete={onImportComplete}
        />
      )}

      {/* Muse の SOUL.md などの個別ファイルの取り込み (docs/intent/muse_export_import.md) */}
      <MuseFileMigrationPanel personaId={personaId} onImportComplete={onImportComplete} />

      {/* ペルソナまるごとパック (docs/intent/persona_pack.md) の取り込みとエクスポート */}
      <PersonaPackPanel personaId={personaId} />

      <div className={styles.reembedSection}>
        <h3 data-i18n="components.memory.MemoryImport.text002">{uiText("components.memory.MemoryImport.text002")}</h3>
        <p data-i18n="components.memory.MemoryImport.text003">{uiText("components.memory.MemoryImport.text003")}</p>
        <div className={styles.reembedActions}>
          <button data-i18n="components.memory.MemoryImport.text004" className={styles.reembedButton} onClick={() => void handleReembed(false)} disabled={isReembedding}>
            {isReembedding ? <Loader2 size={16} className={styles.loader} /> : <RefreshCw size={16} />}{uiText("components.memory.MemoryImport.text004")}</button>
          <button data-i18n="components.memory.MemoryImport.text005" className={styles.reembedButtonSecondary} onClick={() => void handleReembed(true)} disabled={isReembedding}>
            {isReembedding ? <Loader2 size={16} className={styles.loader} /> : <RefreshCw size={16} />}{uiText("components.memory.MemoryImport.text005")}</button>
        </div>
        {reembedProgress && <div className={styles.reembedProgress}><Loader2 size={14} className={styles.loader} /><span>{reembedProgress}</span></div>}
        <MemoryImportResult result={reembedResult} />
      </div>
    </div>
  );
}
