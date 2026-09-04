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

/**
 * スレッドの期間 (epoch 秒 → ローカル日付)。同じ日なら 1 日だけ、片方しか無ければ
 * その日だけ、両方無ければ空文字。
 */
export function formatThreadDateRange(first?: number | null, last?: number | null): string {
  const toDate = (v?: number | null) => {
    if (typeof v !== 'number' || !Number.isFinite(v)) return '';
    return new Date(v * 1000).toLocaleDateString();
  };
  const a = toDate(first);
  const b = toDate(last);
  if (a && b) return a === b ? a : `${a}〜${b}`;
  return a || b;
}

export function formatProgress(message?: string, progress?: number, total?: number): string {
  if (message) return message;
  if (typeof progress === 'number' && typeof total === 'number') {
    return `Processing ${progress}/${total}...`;
  }
  return '処理中...';
}
