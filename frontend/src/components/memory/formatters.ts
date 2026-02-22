import { CheckCircle, AlertCircle } from 'lucide-react';
import { MemoryImportUiResult } from './types';

export function getResultMeta(result: MemoryImportUiResult) {
  if (result.type === 'success') {
    return { Icon: CheckCircle, tone: 'success' as const };
  }
  return { Icon: AlertCircle, tone: 'error' as const };
}

export function formatProgress(message?: string, progress?: number, total?: number): string {
  if (message) return message;
  if (typeof progress === 'number' && typeof total === 'number') {
    return `Processing ${progress}/${total}...`;
  }
  return '処理中...';
}
