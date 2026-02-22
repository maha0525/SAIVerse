import styles from './MemoryImport.module.css';
import { getResultMeta } from './formatters';
import { MemoryImportUiResult } from './types';

interface Props {
  result: MemoryImportUiResult | null;
}

export function MemoryImportResult({ result }: Props) {
  if (!result) return null;
  const { Icon, tone } = getResultMeta(result);

  return (
    <div className={`${styles.result} ${styles[tone]}`}>
      <Icon size={20} />
      <span>{result.message}</span>
    </div>
  );
}
