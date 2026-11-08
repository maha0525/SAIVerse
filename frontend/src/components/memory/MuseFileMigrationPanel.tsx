'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { Loader2 } from 'lucide-react';

import styles from './MemoryImport.module.css';
import { MuseFileImportView, useMuseFileImport } from './MuseFileImport';

/**
 * Muse の SOUL.md・IDENTITY.md・MEMORY.md・AGENTS.md を個別にアップロードして
 * システムプロンプト・コア記憶・Memopedia へ取り込む、メモリー画面の
 * 「ファイルからの引っ越し」の節。あとから取り込む口として残す。同じ取り込みは
 * 取り込みの専用ウィンドウの Muse の流れの中にも必須二択ステップとしてあり、
 * preview / 実行の部分は MuseFileImport.tsx を共有する。
 * 設計: docs/intent/muse_export_import.md, docs/intent/import_flow_redesign.md
 */
export default function MuseFileMigrationPanel({ personaId, onImportComplete }: {
  personaId: string;
  onImportComplete?: () => void;
}) {
  useLocale();
  const fileImport = useMuseFileImport(personaId, onImportComplete);

  return (
    <div className={styles.reembedSection}>
      <h3 data-i18n="components.memory.MuseImportPanel.text040">{uiText('components.memory.MuseImportPanel.text040')}</h3>
      <p data-i18n="components.memory.MuseImportPanel.text041">{uiText('components.memory.MuseImportPanel.text041')}</p>
      <div className={styles.reembedActions}>
        <button
          data-i18n="components.memory.MuseImportPanel.text042"
          className={styles.reembedButtonSecondary}
          onClick={fileImport.openPicker}
          disabled={fileImport.busy}
        >
          {fileImport.busy ? <Loader2 size={16} className={styles.loader} /> : null}
          {uiText('components.memory.MuseImportPanel.text042')}
        </button>
      </div>
      <MuseFileImportView state={fileImport} />
    </div>
  );
}
