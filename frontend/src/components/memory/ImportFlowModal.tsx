'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { AlertCircle, CheckCircle, Loader2, MessageSquare, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import ModalOverlay from '@/components/common/ModalOverlay';
import {
  activateThread,
  fetchThreads,
  getImportStatus,
  getReembedStatus,
  startReembed,
} from '@/lib/memory/importClient';

import ChronicleOfferPanel from './ChronicleOfferPanel';
import ClaudeImportPanel from './ClaudeImportPanel';
import styles from './ImportFlowModal.module.css';
import importStyles from './MemoryImport.module.css';
import { importSourceName } from './ImportSourcePicker';
import { MemoryImportForm } from './MemoryImportForm';
import { MemoryImportProgress } from './MemoryImportProgress';
import { MuseFileImportView, useMuseFileImport } from './MuseFileImport';
import MuseImportPanel from './MuseImportPanel';
import { formatProgress, formatThreadDateRange, sortThreadsByLastActivity } from './formatters';
import {
  ImportDirectResult, ImportFlowSummary, ImportSource, ImportStartRequest, MemoryImportProgress as ImportProgressData,
  ThreadSummary,
} from './types';

/**
 * The chat-log import as one path, opened from step 1 (ImportSourcePicker).
 * Design: docs/intent/import_flow_redesign.md
 *
 *   input        step 2: the source's own upload / selection screen
 *   importing    step 3: the import runs (status polling)
 *   thread-select     : which thread to continue from, when several exist
 *   muse-files        : Muse only — import SOUL / IDENTITY / MEMORY / AGENTS
 *                       or not (two buttons; 不変条件 6)
 *   embedding    step 4: create embeddings or not (two buttons, nothing else)
 *   chronicle    step 5: create a Chronicle or not (ChronicleOfferPanel)
 *   done         step 6: summary and the close button
 *
 * The modal can be closed only in `input` (nothing imported yet) and `done`.
 * From `importing` on there is no close button and the overlay click is
 * ignored, so a finished import always passes steps 4 and 5 (invariant 1).
 * Imports run with skip_embedding=true and step 4 starts POST /reembed, which
 * embeds the messages that have no vector.
 */

type FlowStep = 'input' | 'importing' | 'thread-select' | 'muse-files' | 'embedding' | 'chronicle' | 'done';

type EmbeddingState =
  | { kind: 'none' }
  | { kind: 'skipped' }
  | { kind: 'running'; progress: string | null }
  | { kind: 'ended'; message: string }
  | { kind: 'start-failed'; message: string }
  | { kind: 'untracked' };

const POLL_INTERVAL_MS = 1000;
// 連続でこの回数、状態の問い合わせに失敗したら追跡を止める (ChronicleOfferPanel と同じ考え方)。
const MAX_POLL_FAILURES = 8;

// 段階表示の段。Muse のときだけ取り込みと④の間に「ファイル」の段が入る
// (docs/intent/import_flow_redesign.md「Muse のファイル取り込み」)。
function stagesFor(source: ImportSource): { steps: FlowStep[]; label: string }[] {
  const stages: { steps: FlowStep[]; label: string }[] = [
    { steps: ['input'], label: uiText('components.memory.ImportFlowModal.stageFiles') },
    { steps: ['importing', 'thread-select'], label: uiText('components.memory.ImportFlowModal.stageImport') },
  ];
  if (source === 'muse') {
    stages.push({ steps: ['muse-files'], label: uiText('components.memory.ImportFlowModal.stageMuseFiles') });
  }
  stages.push(
    { steps: ['embedding'], label: uiText('components.memory.ImportFlowModal.stageEmbedding') },
    { steps: ['chronicle'], label: uiText('components.memory.ImportFlowModal.stageChronicle') },
    { steps: ['done'], label: uiText('components.memory.ImportFlowModal.stageDone') },
  );
  return stages;
}

export default function ImportFlowModal({ personaId, source, onClose, onImportComplete }: {
  personaId: string;
  source: ImportSource;
  /**
   * finished is true when the flow reached step 6, false for a cancel before
   * the import. summary is given only when finished is true.
   */
  onClose: (finished: boolean, summary?: ImportFlowSummary) => void;
  /** Called once conversations (or Claude memories) are stored. */
  onImportComplete?: () => void;
}) {
  useLocale();
  const [step, setStep] = useState<FlowStep>('input');
  const [inputError, setInputError] = useState<string | null>(null);
  const [importProgress, setImportProgress] = useState<string | null>(null);
  const [importUntracked, setImportUntracked] = useState(false);
  const [importSummary, setImportSummary] = useState<string | null>(null);
  const [threadNote, setThreadNote] = useState<string | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [threadBusy, setThreadBusy] = useState(false);
  const [embeddingBusy, setEmbeddingBusy] = useState(false);
  const [embedding, setEmbedding] = useState<EmbeddingState>({ kind: 'none' });
  const [directResult, setDirectResult] = useState<ImportDirectResult | null>(null);
  // ⑥に出す件数。閉じるときに要約として親 (PersonaWizard の実績一覧) へ返す。
  const [importCounts, setImportCounts] = useState<{ conversations: number; messages: number } | null>(null);
  const mountedRef = useRef(true);
  const timersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  const onImportCompleteRef = useRef(onImportComplete);
  useEffect(() => { onImportCompleteRef.current = onImportComplete; }, [onImportComplete]);
  // Muse の流れのファイル取り込み (SOUL / IDENTITY / MEMORY / AGENTS)。
  // コールバックはこのファイルの他の経路と同じく ref 経由で最新を呼ぶ。取り込みが
  // 成功すると goAfterImport の分とで onImportComplete が 2 回呼ばれるが、どちらも
  // 「記憶が増えたので読み直してよい」の合図であり、2 回目も正しい。
  const museFiles = useMuseFileImport(personaId, () => onImportCompleteRef.current?.());

  useEffect(() => {
    mountedRef.current = true;
    const timers = timersRef.current;
    return () => {
      mountedRef.current = false;
      timers.forEach((id) => clearTimeout(id));
      timers.clear();
    };
  }, []);

  const later = (fn: () => void, ms: number) => {
    const id = setTimeout(() => { timersRef.current.delete(id); if (mountedRef.current) fn(); }, ms);
    timersRef.current.add(id);
  };

  const closable = step === 'input' || step === 'done';

  const buildSummary = (): ImportFlowSummary => ({
    source,
    conversations: importCounts?.conversations,
    messages: importCounts?.messages,
    message: directResult?.message || importSummary || '',
    museFilesImported: museFiles.imported,
  });

  const requestClose = () => {
    if (!closable) return;
    if (step === 'done') onClose(true, buildSummary());
    else onClose(false);
  };

  // ③以降は閉じられない設計 (不変条件 1) なので、Tab キーでフォーカスが背後の
  // 画面 (PersonaWizard のカードなど) へ抜けて別の操作が走らないよう、
  // フォーカスをこのウィンドウの中に閉じ込める。
  const modalRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    modalRef.current?.focus();
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      const root = modalRef.current;
      if (!root) return;
      const focusables = Array.from(root.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )).filter((el) => el.offsetParent !== null);
      if (focusables.length === 0) { e.preventDefault(); root.focus(); return; }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !root.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault(); first.focus();
      } else if (e.shiftKey && (active === first || active === root)) {
        e.preventDefault(); last.focus();
      }
    };
    document.addEventListener('keydown', handler, true);
    return () => document.removeEventListener('keydown', handler, true);
  }, []);

  // ---- step 3: import ----

  const summarize = (data: ImportProgressData): string => {
    if (typeof data.conversations === 'number' && typeof data.messages === 'number') {
      return uiText('components.memory.ImportFlowModal.importedCounts', { p1: data.conversations, p2: data.messages });
    }
    return data.message || uiText('components.memory.ImportFlowModal.importDone');
  };

  // ③ (とスレッド選択) の次へ。Muse のときはファイル取り込みの二択を④の前に挟む
  // — AGENTS.md から作る Memopedia のページも④のエンベディング作成で拾えるように
  // (docs/intent/import_flow_redesign.md「Muse のファイル取り込み」、不変条件 6)。
  const goAfterImport = () => {
    onImportCompleteRef.current?.();
    setStep(source === 'muse' ? 'muse-files' : 'embedding');
  };

  // 追跡不能 (importUntracked) から「次へ」で進むときもスレッド設定を試せるよう、
  // 走っている取り込みの要求を覚えておく。
  const activeRequestRef = useRef<ImportStartRequest | null>(null);

  const afterImport = async (request: ImportStartRequest, data: ImportProgressData) => {
    setImportSummary(summarize(data));
    if (typeof data.conversations === 'number' && typeof data.messages === 'number') {
      setImportCounts({ conversations: data.conversations, messages: data.messages });
    }
    await threadStep(request);
  };

  const threadStep = async (request: ImportStartRequest) => {
    if (!request.selectThread) { goAfterImport(); return; }
    try {
      const threadList = sortThreadsByLastActivity(await fetchThreads(personaId));
      if (!mountedRef.current) return;
      if (threadList.length === 1) {
        await activateThread(personaId, threadList[0].thread_id);
        if (!mountedRef.current) return;
        setThreadNote(uiText('components.memory.ImportFlowModal.threadAutoActivated'));
        goAfterImport();
      } else if (threadList.length > 1) {
        setThreads(threadList);
        setSelectedThreadId(threadList[0].thread_id);
        setStep('thread-select');
      } else {
        goAfterImport();
      }
    } catch (e) {
      // スレッドの設定に失敗しても取り込みは済んでいるので、④へ進める。
      console.error('Failed to set the thread after import', e);
      if (mountedRef.current) goAfterImport();
    }
  };

  const pollImport = (request: ImportStartRequest, failures: number) => {
    later(() => {
      void (async () => {
        let data: ImportProgressData;
        try {
          data = await getImportStatus(personaId, request.statusType);
        } catch (e) {
          console.error('Import status polling error', e);
          if (!mountedRef.current) return;
          if (failures + 1 >= MAX_POLL_FAILURES) { setImportUntracked(true); return; }
          pollImport(request, failures + 1);
          return;
        }
        if (!mountedRef.current) return;
        await applyImportStatus(request, data);
      })();
    }, POLL_INTERVAL_MS);
  };

  const applyImportStatus = async (request: ImportStartRequest, data: ImportProgressData) => {
    if (data.running) {
      setImportProgress(request.formatRunning
        ? request.formatRunning(data)
        : formatProgress(data.message, data.progress, data.total));
      pollImport(request, 0);
      return;
    }
    setImportProgress(null);
    if (data.success) { await afterImport(request, data); return; }
    // 失敗: 取り込みは完了していないので②へ戻し、閉じられる状態にする。
    setInputError(request.failureMessage(data));
    setStep('input');
  };

  // 追跡不能からの「次へ」。取り込みがまだ走っているのに④へ進むと、その後に
  // 取り込まれた分がエンベディングから漏れるので、先に終了状態を確かめ直す。
  const resumeFromUntracked = async () => {
    const request = activeRequestRef.current;
    if (!request) { goAfterImport(); return; }
    setImportUntracked(false);
    let data: ImportProgressData;
    try {
      data = await getImportStatus(personaId, request.statusType);
    } catch (e) {
      // 再確認も届かないときは、閉じられないウィンドウに閉じ込めないことを優先して
      // 先へ進む (漏れた分はメモリー画面の「未処理を埋める」で後から補完できる)。
      console.error('Import status re-check failed', e);
      if (mountedRef.current) await threadStep(request);
      return;
    }
    if (!mountedRef.current) return;
    await applyImportStatus(request, data);
  };

  const handleStart = async (request: ImportStartRequest) => {
    if (step !== 'input') return;
    setInputError(null);
    setImportUntracked(false);
    setImportProgress(request.initialProgress);
    setStep('importing');
    activeRequestRef.current = request;
    let started: { ok: boolean; error?: string };
    try {
      started = await request.start();
    } catch (e) {
      started = { ok: false, error: e instanceof Error ? e.message : String(e) };
    }
    if (!mountedRef.current) return;
    if (!started.ok) {
      setImportProgress(null);
      setInputError(started.error || uiText('components.memory.ImportFlowModal.importFailed'));
      setStep('input');
      return;
    }
    pollImport(request, 0);
  };

  const handleDirectDone = (result: ImportDirectResult) => {
    setDirectResult(result);
    onImportCompleteRef.current?.();
    setStep('done');
  };

  // ---- thread selection ----

  const confirmThread = async () => {
    if (!selectedThreadId || threadBusy) return;
    setThreadBusy(true);
    try {
      await activateThread(personaId, selectedThreadId);
      if (!mountedRef.current) return;
      setThreadNote(uiText('components.memory.ImportFlowModal.threadActivated'));
    } catch (e) {
      console.error('Failed to activate thread', e);
    } finally {
      if (mountedRef.current) setThreadBusy(false);
    }
    if (mountedRef.current) goAfterImport();
  };

  // ---- step 4: embedding ----

  const pollReembed = (failures: number) => {
    later(() => {
      void (async () => {
        try {
          const status = await getReembedStatus(personaId);
          if (!mountedRef.current) return;
          if (status.running) {
            setEmbedding({ kind: 'running', progress: formatProgress(status.message, status.progress, status.total) });
            pollReembed(0);
            return;
          }
          setEmbedding({ kind: 'ended', message: status.message || '' });
        } catch (e) {
          console.error('Re-embed status polling error', e);
          if (failures + 1 >= MAX_POLL_FAILURES) { setEmbedding({ kind: 'untracked' }); return; }
          pollReembed(failures + 1);
        }
      })();
    }, POLL_INTERVAL_MS);
  };

  const chooseEmbedding = async (create: boolean) => {
    if (embeddingBusy) return;
    if (!create) {
      setEmbedding({ kind: 'skipped' });
      setStep('chronicle');
      return;
    }
    setEmbeddingBusy(true);
    try {
      const res = await startReembed(personaId, false);
      if (!mountedRef.current) return;
      if (res.success) {
        setEmbedding({ kind: 'running', progress: null });
        pollReembed(0);
      } else {
        setEmbedding({ kind: 'start-failed', message: res.detail || res.message || uiText('components.memory.ImportFlowModal.reembedStartFallback') });
      }
    } catch (e) {
      if (!mountedRef.current) return;
      setEmbedding({ kind: 'start-failed', message: e instanceof Error ? e.message : String(e) });
    } finally {
      if (mountedRef.current) setEmbeddingBusy(false);
    }
    if (mountedRef.current) setStep('chronicle');
  };

  // ---- rendering ----

  const embeddingLine = () => {
    switch (embedding.kind) {
      case 'running':
        return (
          <div className={styles.statusLine}>
            <Loader2 size={14} className={importStyles.loader} />
            <span data-i18n="components.memory.ImportFlowModal.embeddingRunning">
              {uiText('components.memory.ImportFlowModal.embeddingRunning', { p1: embedding.progress || '' })}
            </span>
          </div>
        );
      case 'ended':
        return (
          <div className={styles.statusLine}>
            <span data-i18n="components.memory.ImportFlowModal.embeddingEnded">
              {uiText('components.memory.ImportFlowModal.embeddingEnded', { p1: embedding.message })}
            </span>
          </div>
        );
      case 'start-failed':
        return (
          <div className={`${styles.statusLine} ${styles.statusError}`}>
            <AlertCircle size={14} />
            <span data-i18n="components.memory.ImportFlowModal.embeddingStartFailed">
              {uiText('components.memory.ImportFlowModal.embeddingStartFailed', { p1: embedding.message })}
            </span>
          </div>
        );
      case 'untracked':
        return (
          <div className={`${styles.statusLine} ${styles.statusError}`}>
            <AlertCircle size={14} />
            <span data-i18n="components.memory.ImportFlowModal.embeddingUntracked">{uiText('components.memory.ImportFlowModal.embeddingUntracked')}</span>
          </div>
        );
      case 'skipped':
        return (
          <div className={styles.statusLine}>
            <span data-i18n="components.memory.ImportFlowModal.embeddingSkipped">{uiText('components.memory.ImportFlowModal.embeddingSkipped')}</span>
          </div>
        );
      default:
        return null;
    }
  };

  const renderInput = () => (
    <>
      {inputError && (
        <div className={`${importStyles.result} ${importStyles.error} ${styles.inputError}`}>
          <AlertCircle size={16} />
          <span>{inputError}</span>
        </div>
      )}
      {source === 'claude' && <ClaudeImportPanel personaId={personaId} onStart={(r) => void handleStart(r)} onDirectDone={handleDirectDone} />}
      {source === 'muse' && <MuseImportPanel personaId={personaId} onStart={(r) => void handleStart(r)} />}
      {(source === 'official' || source === 'extension' || source === 'native') && (
        <MemoryImportForm personaId={personaId} source={source} onStart={(r) => void handleStart(r)} />
      )}
      <p data-i18n="components.memory.ImportFlowModal.inputNote" className={styles.footNote}>{uiText('components.memory.ImportFlowModal.inputNote')}</p>
    </>
  );

  const renderImporting = () => (
    <>
      {!importUntracked && <MemoryImportProgress message={importProgress} />}
      {importUntracked && (
        <>
          <div className={`${importStyles.result} ${importStyles.error}`}>
            <AlertCircle size={16} />
            <span data-i18n="components.memory.ImportFlowModal.importUntracked">{uiText('components.memory.ImportFlowModal.importUntracked')}</span>
          </div>
          <div className={importStyles.actions}>
            <button
              data-i18n="components.memory.ImportFlowModal.next"
              className={importStyles.importButton}
              onClick={() => void resumeFromUntracked()}
            >{uiText('components.memory.ImportFlowModal.next')}</button>
          </div>
        </>
      )}
      <p data-i18n="components.memory.ImportFlowModal.importingNote" className={styles.footNote}>{uiText('components.memory.ImportFlowModal.importingNote')}</p>
    </>
  );

  const renderThreadSelect = () => (
    <div className={importStyles.threadSelectContainer}>
      <div className={importStyles.threadSelectHeader}><MessageSquare size={24} /><h3 data-i18n="components.memory.MemoryImportForm.text001">{uiText('components.memory.MemoryImportForm.text001')}</h3></div>
      {importSummary && <p className={styles.summaryLine}>{importSummary}</p>}
      <div className={importStyles.threadList}>{threads.map((thread) => {
        // 1 行目 = 題名 (無ければ suffix)、2 行目 = 件数・期間 (題名があるときは suffix も)、3 行目 = 冒頭プレビュー
        const meta = [
          uiText('components.memory.MemoryImportForm.text002', { p1: thread.message_count ?? 0 }),
          formatThreadDateRange(thread.first_created_at, thread.last_created_at),
          thread.title ? (thread.suffix || thread.thread_id) : '',
        ].filter(Boolean).join(' · ');
        return (
          <div key={thread.thread_id} className={`${importStyles.threadItem} ${selectedThreadId === thread.thread_id ? importStyles.selected : ''}`} onClick={() => setSelectedThreadId(thread.thread_id)}>
            <div className={importStyles.threadContent}>
              <div className={importStyles.threadName}>{thread.title || thread.suffix || thread.thread_id}</div>
              <div className={importStyles.threadMetaLine}>{meta}</div>
              <div data-i18n="components.memory.MemoryImportForm.text003" className={importStyles.threadPreview}>{thread.preview || uiText('components.memory.MemoryImportForm.text003')}</div>
            </div>
          </div>
        );
      })}</div>
      <div className={importStyles.actions}>
        <button data-i18n="components.memory.MemoryImportForm.text004" className={importStyles.cancelButton} onClick={goAfterImport} disabled={threadBusy}>{uiText('components.memory.MemoryImportForm.text004')}</button>
        <button data-i18n="components.memory.MemoryImportForm.text005" className={importStyles.importButton} onClick={() => void confirmThread()} disabled={!selectedThreadId || threadBusy}>
          {threadBusy ? <Loader2 size={16} className={importStyles.loader} /> : null}
          {uiText('components.memory.MemoryImportForm.text005')}
        </button>
      </div>
    </div>
  );

  const renderEmbedding = () => (
    <div className={importStyles.embeddingDialog}>
      {importSummary && (
        <div className={`${importStyles.result} ${importStyles.success} ${styles.stepSummary}`}>
          <CheckCircle size={16} />
          <span>{importSummary}</span>
        </div>
      )}
      <h3 data-i18n="components.memory.MemoryImportForm.text006">{uiText('components.memory.MemoryImportForm.text006')}</h3>
      <p data-i18n="components.memory.ImportFlowModal.embeddingBody" className={styles.stepBody}>{uiText('components.memory.ImportFlowModal.embeddingBody')}</p>
      <div className={importStyles.dialogActions}>
        <button data-i18n="components.memory.MemoryImportForm.text007" className={importStyles.cancelButton} onClick={() => void chooseEmbedding(false)} disabled={embeddingBusy}>{uiText('components.memory.MemoryImportForm.text007')}</button>
        <button data-i18n="components.memory.MemoryImportForm.text008" className={importStyles.importButton} onClick={() => void chooseEmbedding(true)} disabled={embeddingBusy}>
          {embeddingBusy ? <Loader2 size={16} className={importStyles.loader} /> : null}
          {uiText('components.memory.MemoryImportForm.text008')}
        </button>
      </div>
    </div>
  );

  // Muse だけの必須二択: SOUL / IDENTITY / MEMORY / AGENTS を取り込むか。
  // 押す以外に先へ進む方法は無く、ウィンドウも閉じられない (closable に含めない)。
  // preview / 実行はメモリー画面の「ファイルからの引っ越し」と同じ MuseFileImport を使う。
  // 失敗はこの中に出し、選び直すか「あとで」で④へ進める (行き止まりにしない)。
  const renderMuseFiles = () => (
    <div className={importStyles.embeddingDialog}>
      {importSummary && (
        <div className={`${importStyles.result} ${importStyles.success} ${styles.stepSummary}`}>
          <CheckCircle size={16} />
          <span>{importSummary}</span>
        </div>
      )}
      <h3 data-i18n="components.memory.ImportFlowModal.museFilesTitle">{uiText('components.memory.ImportFlowModal.museFilesTitle')}</h3>
      <p data-i18n="components.memory.ImportFlowModal.museFilesBody" className={styles.stepBody}>{uiText('components.memory.ImportFlowModal.museFilesBody')}</p>
      <p data-i18n="components.memory.ImportFlowModal.museFilesLaterNote" className={styles.stepBody}>{uiText('components.memory.ImportFlowModal.museFilesLaterNote')}</p>
      <div className={styles.museFilesDetail}>
        <MuseFileImportView state={museFiles} />
      </div>
      <div className={importStyles.dialogActions}>
        {museFiles.imported ? (
          <button
            data-i18n="components.memory.ImportFlowModal.next"
            className={importStyles.importButton}
            onClick={() => setStep('embedding')}
            disabled={museFiles.busy}
          >{uiText('components.memory.ImportFlowModal.next')}</button>
        ) : (
          <>
            <button
              data-i18n="components.memory.MemoryImportForm.text007"
              className={importStyles.cancelButton}
              onClick={() => setStep('embedding')}
              disabled={museFiles.busy}
            >{uiText('components.memory.MemoryImportForm.text007')}</button>
            {!museFiles.preview && (
              <button
                data-i18n="components.memory.ImportFlowModal.museFilesImport"
                className={importStyles.importButton}
                onClick={museFiles.openPicker}
                disabled={museFiles.busy}
              >
                {museFiles.busy ? <Loader2 size={16} className={importStyles.loader} /> : null}
                {uiText('components.memory.ImportFlowModal.museFilesImport')}
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );

  const renderChronicle = () => (
    <>
      <ChronicleOfferPanel personaId={personaId} onDone={() => setStep('done')} />
      {embeddingLine()}
    </>
  );

  const renderDone = () => (
    <div className={styles.doneContainer}>
      <CheckCircle size={48} className={styles.doneIcon} />
      <h3 data-i18n="components.memory.ImportFlowModal.doneTitle" className={styles.doneTitle}>{uiText('components.memory.ImportFlowModal.doneTitle')}</h3>
      {importSummary && <p className={styles.summaryLine}>{importSummary}</p>}
      {threadNote && <p className={styles.summaryLine}>{threadNote}</p>}
      {directResult && <p className={styles.summaryLine}>{directResult.message}</p>}
      {museFiles.imported && museFiles.result?.type === 'success' && <p className={styles.summaryLine}>{museFiles.result.message}</p>}
      {embeddingLine()}
      {embedding.kind === 'running' && (
        <p data-i18n="components.memory.ImportFlowModal.embeddingBackground" className={styles.footNote}>{uiText('components.memory.ImportFlowModal.embeddingBackground')}</p>
      )}
      <div className={importStyles.actions}>
        <button data-i18n="components.memory.ImportFlowModal.close" className={importStyles.importButton} onClick={() => onClose(true, buildSummary())}>{uiText('components.memory.ImportFlowModal.close')}</button>
      </div>
    </div>
  );

  const renderBody = () => {
    switch (step) {
      case 'input':
      case 'importing':
        // ②の画面は③の間も隠して残す。取り込みが失敗して②へ戻ったとき、
        // 選んだ会話を選び直さずに済むように。
        return (
          <>
            <div hidden={step !== 'input'}>{renderInput()}</div>
            {step === 'importing' && renderImporting()}
          </>
        );
      case 'thread-select': return renderThreadSelect();
      case 'muse-files': return renderMuseFiles();
      case 'embedding': return renderEmbedding();
      case 'chronicle': return renderChronicle();
      case 'done': return renderDone();
    }
  };

  const stages = stagesFor(source);
  const stageIndex = stages.findIndex((group) => group.steps.includes(step));
  const stageLabels = stages.map((group) => group.label);

  return (
    <ModalOverlay onClose={requestClose} className={styles.overlay}>
      <div className={styles.modal} onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" ref={modalRef} tabIndex={-1}>
        <div className={styles.header}>
          <h2 data-i18n="components.memory.ImportFlowModal.title" className={styles.title}>{uiText('components.memory.ImportFlowModal.title', { p1: importSourceName(source) })}</h2>
          {closable && (
            <button className={styles.closeButton} onClick={requestClose} aria-label={uiText('components.memory.ImportFlowModal.close')}>
              <X size={20} />
            </button>
          )}
        </div>
        <ol className={styles.stages}>
          {stageLabels.map((label, idx) => (
            <li
              key={idx}
              className={`${styles.stage} ${idx === stageIndex ? styles.stageActive : ''} ${idx < stageIndex ? styles.stageDone : ''}`}
            >
              <span className={styles.stageNumber}>{idx + 2}</span>
              <span>{label}</span>
            </li>
          ))}
        </ol>
        <div className={styles.body}>{renderBody()}</div>
      </div>
    </ModalOverlay>
  );
}
