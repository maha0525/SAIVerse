import { CheckCircle, AlertCircle } from 'lucide-react';
import { MemoryImportUiResult } from './types';

export function getResultMeta(result: MemoryImportUiResult) {
  if (result.type === 'success') {
    return { Icon: CheckCircle, tone: 'success' as const };
  }
  return { Icon: AlertCircle, tone: 'error' as const };
}

/** インポート一覧の作成日。ISO 文字列なら日付だけに、読めなければ元の文字列のまま、無ければ '-'。 */
export function formatImportDate(value: string | null | undefined): string {
  if (!value) return '-';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString();
}

export function formatProgress(message?: string, progress?: number, total?: number): string {
  if (message) return message;
  if (typeof progress === 'number' && typeof total === 'number') {
    return `Processing ${progress}/${total}...`;
  }
  return '処理中...';
}
