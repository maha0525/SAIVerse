import { useRef, useState } from 'react';

import {
  activateThread,
  fetchThreads,
  getImportStatus,
  getReembedStatus,
  importExtension,
  importNative,
  importOfficial,
  previewNativeImport,
  previewOfficialImport,
  startReembed,
} from '@/lib/memory/importClient';

import { formatProgress } from './formatters';
import { ImportSubTab, MemoryImportStep, MemoryImportUiResult, NativePreviewData, PreviewData, ThreadSummary } from './types';

export function useMemoryImport(personaId: string, onImportComplete?: () => void) {
  const [activeSubTab, setActiveSubTab] = useState<ImportSubTab>('official');
  const [isLoading, setIsLoading] = useState(false);
  const [result, setResult] = useState<MemoryImportUiResult | null>(null);
  const [step, setStep] = useState<MemoryImportStep>('upload');
  const [previewData, setPreviewData] = useState<PreviewData | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [pendingExtensionFile, setPendingExtensionFile] = useState<File | null>(null);
  const [importProgress, setImportProgress] = useState<string | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [nativePreview, setNativePreview] = useState<NativePreviewData | null>(null);
  const [pendingNativeFile, setPendingNativeFile] = useState<File | null>(null);
  const [isReembedding, setIsReembedding] = useState(false);
  const [reembedProgress, setReembedProgress] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const nativeFileInputRef = useRef<HTMLInputElement>(null);

  const resetState = () => {
    setStep('upload'); setPreviewData(null); setSelectedIds(new Set()); setResult(null);
    setPendingExtensionFile(null); setImportProgress(null); setThreads([]); setSelectedThreadId(null);
    setNativePreview(null); setPendingNativeFile(null);
  };

  const handleImportSuccess = async (message: string) => {
    const threadList = await fetchThreads(personaId);
    if (threadList.length <= 0) {
      setStep('upload');
      setResult({ type: 'success', message });
    } else if (threadList.length === 1) {
      await activateThread(personaId, threadList[0].thread_id);
      setStep('upload');
      setResult({ type: 'success', message: `${message} スレッドを自動設定しました。` });
      onImportComplete?.();
    } else {
      setThreads(threadList);
      setSelectedThreadId(threadList[0].thread_id);
      setStep('thread-select');
      setResult({ type: 'success', message });
    }
    setPreviewData(null); setSelectedIds(new Set());
  };

  const pollImportStatus = async (type: 'extension' | 'official' | 'native') => {
    const data = await getImportStatus(personaId, type);
    if (data.running) {
      setImportProgress(formatProgress(data.message, data.progress, data.total));
      setTimeout(() => void pollImportStatus(type), 1000);
      return;
    }
    setImportProgress(null); setIsLoading(false);
    if (data.success) {
      if (type === 'native') {
        setStep('upload'); setResult({ type: 'success', message: data.message || 'Native import complete' }); onImportComplete?.();
      } else {
        await handleImportSuccess(data.message || 'Import successful');
      }
      return;
    }
    setStep(type === 'official' ? 'select' : 'upload');
    setResult({ type: 'error', message: data.message || 'Import failed' });
  };

  const handleFileSelect = async (file: File) => {
    setIsLoading(true); setResult(null);
    if (activeSubTab === 'official') {
      const { ok, data } = await previewOfficialImport(personaId, file);
      if (ok && 'conversations' in data && data.conversations.length > 0) {
        setPreviewData(data); setSelectedIds(new Set()); setStep('select');
      } else {
        setResult({ type: 'error', message: ('detail' in data && data.detail) || 'ファイルに会話が見つかりませんでした。' });
      }
    } else {
      setPendingExtensionFile(file); setStep('embedding-dialog');
    }
    setIsLoading(false);
  };

  const executeExtensionImport = async (skipEmbedding: boolean) => {
    if (!pendingExtensionFile) return;
    setStep('importing'); setIsLoading(true); setResult(null); setImportProgress('インポート開始...');
    const { ok, data } = await importExtension(personaId, pendingExtensionFile, skipEmbedding);
    setPendingExtensionFile(null);
    if (!ok) { setStep('upload'); setIsLoading(false); setImportProgress(null); setResult({ type: 'error', message: data.detail || 'Import failed' }); return; }
    setTimeout(() => void pollImportStatus('extension'), 1000);
  };

  const executeOfficialImport = async (skipEmbedding: boolean) => {
    if (!previewData) return;
    setStep('importing'); setIsLoading(true); setResult(null); setImportProgress('インポート開始...');
    const { ok, data } = await importOfficial(personaId, {
      cache_key: previewData.cache_key,
      conversation_ids: Array.from(selectedIds).map(String),
      skip_embedding: skipEmbedding,
    });
    if (!ok) { setStep('select'); setIsLoading(false); setImportProgress(null); setResult({ type: 'error', message: data.detail || 'Import failed' }); return; }
    setTimeout(() => void pollImportStatus('official'), 1000);
  };

  const handleNativeFileSelect = async (file: File) => {
    setIsLoading(true); setResult(null);
    const { ok, data } = await previewNativeImport(personaId, file);
    if (ok && 'threads' in data) { setNativePreview(data); setPendingNativeFile(file); setStep('select'); }
    else { setResult({ type: 'error', message: ('detail' in data && data.detail) || 'Preview failed' }); }
    setIsLoading(false);
  };

  const executeNativeImport = async (skipEmbedding: boolean) => {
    if (!pendingNativeFile) return;
    setStep('importing'); setIsLoading(true); setResult(null); setImportProgress('Nativeインポート開始...');
    const { ok, data } = await importNative(personaId, pendingNativeFile, skipEmbedding);
    setPendingNativeFile(null); setNativePreview(null);
    if (!ok) { setStep('upload'); setIsLoading(false); setImportProgress(null); setResult({ type: 'error', message: data.detail || 'Import failed' }); return; }
    setTimeout(() => void pollImportStatus('native'), 1000);
  };


  const handleThreadSelectConfirm = async () => {
    if (!selectedThreadId) return;
    setIsLoading(true);
    await activateThread(personaId, selectedThreadId);
    setIsLoading(false);
    setStep('upload');
    setResult({ type: 'success', message: 'インポート完了！アクティブスレッドを設定しました。' });
    onImportComplete?.();
  };

  const handleReembed = async (force: boolean) => {
    setIsReembedding(true); setResult(null); setReembedProgress('Starting...');
    const data = await startReembed(personaId, force);
    if (!data.success) { setIsReembedding(false); setReembedProgress(null); setResult({ type: 'error', message: data.detail || data.message || 'Re-embedding failed' }); return; }
    const poll = async () => {
      const status = await getReembedStatus(personaId);
      if (status.running) { setReembedProgress(formatProgress(status.message, status.progress, status.total)); setTimeout(() => void poll(), 1000); return; }
      setIsReembedding(false); setReembedProgress(null); if (status.message) setResult({ type: 'success', message: status.message });
    };
    setTimeout(() => void poll(), 1000);
  };

  return {
    activeSubTab, isLoading, result, step, previewData, selectedIds, importProgress, threads, selectedThreadId,
    nativePreview, isReembedding, reembedProgress, fileInputRef, nativeFileInputRef,
    setActiveSubTab, setStep, setSelectedIds, setSelectedThreadId, resetState,
    handleFileSelect, handleNativeFileSelect, executeExtensionImport, executeOfficialImport, executeNativeImport,
    handleThreadSelectConfirm, handleReembed, setResult,
  };
}
