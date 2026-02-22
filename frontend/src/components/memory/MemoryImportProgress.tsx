import { Loader2 } from 'lucide-react';
import styles from './MemoryImport.module.css';

interface Props {
  message: string | null;
}

export function MemoryImportProgress({ message }: Props) {
  return (
    <div className={styles.importingProgress}>
      <Loader2 className={styles.loader} size={48} />
      <div className={styles.progressText}>{message || 'インポート中...'}</div>
    </div>
  );
}
