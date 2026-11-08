'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { Bot, FileJson, MessagesSquare, Puzzle, SkipForward, Sparkles } from 'lucide-react';

import styles from './ImportFlowModal.module.css';
import { ImportSource } from './types';

/**
 * Step 1 of the chat-log import: choose where the logs come from. Choosing a
 * source opens ImportFlowModal. Used by the memory screen's import tab and by
 * PersonaWizard step 2 (which also passes onSkip).
 * Design: docs/intent/import_flow_redesign.md
 */

const SOURCES: { source: ImportSource; Icon: typeof Bot }[] = [
  { source: 'official', Icon: MessagesSquare },
  { source: 'claude', Icon: Bot },
  { source: 'muse', Icon: Sparkles },
  { source: 'extension', Icon: Puzzle },
  { source: 'native', Icon: FileJson },
];

// check-i18n.mjs は文字列リテラルのキーしか拾えないため、ループで使う文言は
// ここでリテラル呼び出しに束ねる。
export function importSourceName(source: ImportSource): string {
  switch (source) {
    case 'official': return uiText('components.memory.ImportSourcePicker.officialName');
    case 'claude': return uiText('components.memory.ImportSourcePicker.claudeName');
    case 'muse': return uiText('components.memory.ImportSourcePicker.museName');
    case 'extension': return uiText('components.memory.ImportSourcePicker.extensionName');
    case 'native': return uiText('components.memory.ImportSourcePicker.nativeName');
  }
}

function importSourceDesc(source: ImportSource): string {
  switch (source) {
    case 'official': return uiText('components.memory.ImportSourcePicker.officialDesc');
    case 'claude': return uiText('components.memory.ImportSourcePicker.claudeDesc');
    case 'muse': return uiText('components.memory.ImportSourcePicker.museDesc');
    case 'extension': return uiText('components.memory.ImportSourcePicker.extensionDesc');
    case 'native': return uiText('components.memory.ImportSourcePicker.nativeDesc');
  }
}

export default function ImportSourcePicker({ onSelect, onSkip }: {
  onSelect: (source: ImportSource) => void;
  /** When given, a card for not importing is shown last. */
  onSkip?: () => void;
}) {
  useLocale();
  return (
    <div className={styles.picker}>
      <div data-i18n="components.memory.ImportSourcePicker.heading" className={styles.pickerHeading}>{uiText('components.memory.ImportSourcePicker.heading')}</div>
      <div className={styles.pickerGrid}>
        {SOURCES.map(({ source, Icon }) => (
          <button key={source} type="button" className={styles.sourceCard} onClick={() => onSelect(source)}>
            <Icon size={22} className={styles.sourceIcon} />
            <span className={styles.sourceText}>
              <span className={styles.sourceName}>{importSourceName(source)}</span>
              <span className={styles.sourceDesc}>{importSourceDesc(source)}</span>
            </span>
          </button>
        ))}
        {onSkip && (
          <button type="button" className={`${styles.sourceCard} ${styles.sourceCardSkip}`} onClick={onSkip}>
            <SkipForward size={22} className={styles.sourceIcon} />
            <span className={styles.sourceText}>
              <span data-i18n="components.memory.ImportSourcePicker.skipName" className={styles.sourceName}>{uiText('components.memory.ImportSourcePicker.skipName')}</span>
              <span data-i18n="components.memory.ImportSourcePicker.skipDesc" className={styles.sourceDesc}>{uiText('components.memory.ImportSourcePicker.skipDesc')}</span>
            </span>
          </button>
        )}
      </div>
    </div>
  );
}
