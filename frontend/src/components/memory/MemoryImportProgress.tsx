
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { Loader2 } from 'lucide-react';
import styles from './MemoryImport.module.css';

interface Props {
  message: string | null;
}

export function MemoryImportProgress({ message }: Props) {
    useLocale();
  return (
    <div className={styles.importingProgress}>
      <Loader2 className={styles.loader} size={48} />
      <div data-i18n="components.memory.MemoryImportProgress.text001" className={styles.progressText}>{message || uiText("components.memory.MemoryImportProgress.text001")}</div>
    </div>
  );
}
