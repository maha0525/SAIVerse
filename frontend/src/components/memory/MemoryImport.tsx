
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { Loader2, RefreshCw } from 'lucide-react';

import styles from './MemoryImport.module.css';
import { MemoryImportForm } from './MemoryImportForm';
import { MemoryImportProgress } from './MemoryImportProgress';
import { MemoryImportResult } from './MemoryImportResult';
import { MemoryImportProps } from './types';
import { useMemoryImport } from './useMemoryImport';

export default function MemoryImport({ personaId, onImportComplete }: MemoryImportProps) {
    useLocale();
  const memoryImport = useMemoryImport(personaId, onImportComplete);

  const toggleSelection = (idx: number) => {
    memoryImport.setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (!memoryImport.previewData) return;
    if (memoryImport.selectedIds.size === memoryImport.previewData.conversations.length) {
      memoryImport.setSelectedIds(new Set());
      return;
    }
    memoryImport.setSelectedIds(new Set(memoryImport.previewData.conversations.map((c) => c.idx)));
  };

  return (
    <div className={styles.container}>
      <h2 data-i18n="components.memory.MemoryImport.text001" className={styles.title}>{uiText("components.memory.MemoryImport.text001")}</h2>
      {memoryImport.step === 'importing' ? <MemoryImportProgress message={memoryImport.importProgress} /> : (
        <MemoryImportForm
          personaId={personaId}
          activeSubTab={memoryImport.activeSubTab}
          step={memoryImport.step}
          isLoading={memoryImport.isLoading}
          previewData={memoryImport.previewData}
          selectedIds={memoryImport.selectedIds}
          nativePreview={memoryImport.nativePreview}
          threads={memoryImport.threads}
          selectedThreadId={memoryImport.selectedThreadId}
          fileInputRef={memoryImport.fileInputRef}
          nativeFileInputRef={memoryImport.nativeFileInputRef}
          onTabChange={(tab) => { memoryImport.setActiveSubTab(tab); memoryImport.resetState(); }}
          onReset={memoryImport.resetState}
          onFileChange={(file) => { void memoryImport.handleFileSelect(file); }}
          onNativeFileChange={(file) => { void memoryImport.handleNativeFileSelect(file); }}
          onToggleSelection={toggleSelection}
          onToggleSelectAll={toggleSelectAll}
          onConfirmOfficial={() => {
            if (memoryImport.selectedIds.size === 0) return;
            memoryImport.setStep('embedding-dialog');
          }}
          onEmbeddingChoice={(skip) => {
            if (memoryImport.activeSubTab === 'native') void memoryImport.executeNativeImport(skip);
            else if (memoryImport.activeSubTab === 'extension') void memoryImport.executeExtensionImport(skip);
            else void memoryImport.executeOfficialImport(skip);
          }}
          onSelectThread={memoryImport.setSelectedThreadId}
          onConfirmThread={() => { void memoryImport.handleThreadSelectConfirm(); }}
          onSkipThread={() => { memoryImport.setStep('upload'); onImportComplete?.(); }}
          onOpenNativeImport={() => memoryImport.setStep('embedding-dialog')}
        />
      )}

      <MemoryImportResult result={memoryImport.result} />

      <div className={styles.reembedSection}>
        <h3 data-i18n="components.memory.MemoryImport.text002">{uiText("components.memory.MemoryImport.text002")}</h3>
        <p data-i18n="components.memory.MemoryImport.text003">{uiText("components.memory.MemoryImport.text003")}</p>
        <div className={styles.reembedActions}>
          <button data-i18n="components.memory.MemoryImport.text004" className={styles.reembedButton} onClick={() => void memoryImport.handleReembed(false)} disabled={memoryImport.isReembedding}>
            {memoryImport.isReembedding ? <Loader2 size={16} className={styles.loader} /> : <RefreshCw size={16} />}{uiText("components.memory.MemoryImport.text004")}</button>
          <button data-i18n="components.memory.MemoryImport.text005" className={styles.reembedButtonSecondary} onClick={() => void memoryImport.handleReembed(true)} disabled={memoryImport.isReembedding}>
            {memoryImport.isReembedding ? <Loader2 size={16} className={styles.loader} /> : <RefreshCw size={16} />}{uiText("components.memory.MemoryImport.text005")}</button>
        </div>
        {memoryImport.reembedProgress && <div className={styles.reembedProgress}><Loader2 size={14} className={styles.loader} /><span>{memoryImport.reembedProgress}</span></div>}
      </div>
    </div>
  );
}
