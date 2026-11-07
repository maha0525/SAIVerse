'use client';
import { apiFetch } from '@/i18n/api';
import { getFormatLocale, t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { formatCost } from '@/lib/formatCost';
import { AlertCircle, BookOpen, CheckCircle, Loader2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import importStyles from './MemoryImport.module.css';
import styles from './ChronicleOfferPanel.module.css';

/**
 * Step 5 of ImportFlowModal: explains what happens with and without a
 * Chronicle, shows the cost estimate, and can start the existing repair job
 * (POST /arasuji/generate with mode='repair'). onDone moves the flow to its
 * last step.
 * Design: docs/intent/import_chronicle_offer.md, docs/intent/import_flow_redesign.md
 *
 * - Shows a loading line while the estimate is fetched, then calls onDone()
 *   without showing the choice when unprocessed_messages or
 *   estimated_llm_calls is 0, or the estimate request fails.
 * - "Later" writes nothing.
 * - Going on while the job runs leaves the job running on the backend.
 */

// Same response shape as RepairEstimate in ArasujiViewer.tsx.
interface ChronicleCostEstimate {
  total_messages: number;
  processed_messages: number;
  unprocessed_messages: number;
  estimated_llm_calls: number;
  estimated_cost_usd: number;
  model_name: string;
  is_free_tier: boolean;
  currency: string;
  repair_incomplete?: boolean;
  consolidation_calls?: number;
}

interface JobState {
  jobId: string;
  status: string;
  progress: number | null;
  total: number | null;
  message: string | null;
  warning: string | null;
  error: string | null;
}

const FINISHED_STATUSES = new Set(['completed', 'failed', 'cancelled']);
const POLL_INTERVAL_MS = 1500;
// ジョブの台帳はバックエンドのプロセス内メモリなので、サーバー再起動を跨ぐと
// 参照が 404 になる。連続でこの回数失敗したら追跡を止めて案内に切り替える
// (止めないと「作成しています…」のスピナーが永遠に残る)。
const MAX_POLL_FAILURES = 8;
// 追跡不能の印 (バックエンドの status には無い、この画面だけの値)。
const UNTRACKED = 'untracked';

export default function ChronicleOfferPanel({ personaId, onDone }: {
  personaId: string;
  onDone: () => void;
}) {
  useLocale();
  const [estimate, setEstimate] = useState<ChronicleCostEstimate | null>(null);
  const [job, setJob] = useState<JobState | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const onDoneRef = useRef(onDone);
  useEffect(() => { onDoneRef.current = onDone; }, [onDone]);
  const pollingRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stopPolling = () => {
    if (pollingRef.current) {
      clearTimeout(pollingRef.current);
      pollingRef.current = null;
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await apiFetch(`/api/people/${personaId}/arasuji/cost-estimate`);
        if (cancelled) return;
        if (!res.ok) { onDoneRef.current(); return; }
        const data: ChronicleCostEstimate = await res.json();
        if (cancelled) return;
        // estimated_llm_calls は実行と同じ計画から数えるので (sai_memory/arasuji/
        // estimate.py — 表示と実走が違う数を言ってはならない)、0 のときは実行しても
        // 何も編纂されない。未処理が数件だけ残っていて編纂のまとまりに達しない
        // 状態で出すと、「作る」が空振りして成功の顔をするため、ここで閉じる。
        if (!data || !(data.unprocessed_messages > 0) || !(data.estimated_llm_calls > 0)) {
          onDoneRef.current();
          return;
        }
        setEstimate(data);
      } catch (e) {
        if (cancelled) return;
        console.error('Failed to load Chronicle cost estimate', e);
        onDoneRef.current();
      }
    })();
    // personaId が変わって見積もりを取り直すときは、前のジョブの追跡も止める
    // (止めないと古い persona/job の進捗を映し続ける)。アンマウント時も同じ。
    return () => { cancelled = true; stopPolling(); };
  }, [personaId]);

  // 前の応答を待ってから次の確認を予約する (setInterval だと応答が遅れた回に
  // 到着順が入れ替わり、進捗が一時的に逆行して見える)。
  const startPolling = (jobId: string) => {
    stopPolling();
    let failures = 0;
    const tick = async () => {
      try {
        const res = await apiFetch(`/api/people/${personaId}/arasuji/generate/${jobId}`);
        if (res.ok) {
          failures = 0;
          const data = await res.json();
          setJob({
            jobId: data.job_id,
            status: data.status,
            progress: data.progress ?? null,
            total: data.total ?? null,
            message: data.message ?? null,
            warning: data.warning || null,
            error: data.error || null,
          });
          if (FINISHED_STATUSES.has(data.status)) {
            pollingRef.current = null;
            return;
          }
        } else {
          failures += 1;
        }
      } catch (e) {
        console.error('Chronicle job polling error', e);
        failures += 1;
      }
      if (failures >= MAX_POLL_FAILURES) {
        setJob(prev => (prev ? { ...prev, status: UNTRACKED } : prev));
        pollingRef.current = null;
        return;
      }
      pollingRef.current = setTimeout(() => { void tick(); }, POLL_INTERVAL_MS);
    };
    pollingRef.current = setTimeout(() => { void tick(); }, POLL_INTERVAL_MS);
  };

  const startGeneration = async () => {
    if (!estimate || starting || job) return;
    setStarting(true);
    setStartError(null);
    try {
      const res = await apiFetch(`/api/people/${personaId}/arasuji/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: 'repair',
          confirmed_unprocessed_messages: estimate.unprocessed_messages,
        }),
      });
      if (res.ok) {
        const data = await res.json();
        setJob({
          jobId: data.job_id,
          status: 'started',
          progress: null,
          total: null,
          message: null,
          warning: null,
          error: null,
        });
        startPolling(data.job_id);
      } else {
        let detail = `HTTP ${res.status}`;
        try {
          const err = await res.json();
          if (err && typeof err.detail === 'string') detail = err.detail;
        } catch { /* keep the status code */ }
        setStartError(detail);
      }
    } catch (e) {
      console.error('Failed to start Chronicle generation', e);
      setStartError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  if (!estimate) {
    return (
      <div className={styles.panel}>
        <div className={styles.progressLine}>
          <Loader2 size={16} className={importStyles.loader} />
          <span data-i18n="components.memory.ChronicleOfferPanel.estimating">{uiText('components.memory.ChronicleOfferPanel.estimating')}</span>
        </div>
      </div>
    );
  }

  const formatCount = (n: number) => n.toLocaleString(getFormatLocale());

  if (job) {
    const finished = FINISHED_STATUSES.has(job.status) || job.status === UNTRACKED;
    const hasCounts = job.total !== null && job.total > 0 && job.progress !== null;
    return (
      <div className={styles.panel}>
        <div className={styles.header}>
          <BookOpen size={20} />
          <h3 data-i18n="components.memory.ChronicleOfferPanel.text001">{uiText('components.memory.ChronicleOfferPanel.text001')}</h3>
        </div>
        {!finished && (
          <>
            <div className={styles.progressLine}>
              <Loader2 size={16} className={importStyles.loader} />
              <span data-i18n="components.memory.ChronicleOfferPanel.text014 components.memory.ChronicleOfferPanel.text015">
                {hasCounts
                  ? uiText('components.memory.ChronicleOfferPanel.text014', {
                    p1: formatCount(job.progress ?? 0), p2: formatCount(job.total ?? 0),
                  })
                  : uiText('components.memory.ChronicleOfferPanel.text015')}
              </span>
            </div>
            {job.message && <p className={styles.serverMessage}>{job.message}</p>}
            <p data-i18n="components.memory.ChronicleOfferPanel.text016" className={styles.note}>{uiText('components.memory.ChronicleOfferPanel.text016')}</p>
          </>
        )}
        {job.status === 'completed' && (
          <>
            <div className={`${importStyles.result} ${importStyles.success} ${styles.outcome}`}>
              <CheckCircle size={16} />
              <span data-i18n="components.memory.ChronicleOfferPanel.text017">
                {uiText('components.memory.ChronicleOfferPanel.text017')}
                {job.warning ? ` ${job.warning}` : ''}
              </span>
            </div>
            {/* 補修は一部を処理できないまま completed になることがあり、その説明は
                message に入る (PR #458 Codex P2)。捨てずに併記する。 */}
            {job.message && <p className={styles.serverMessage}>{job.message}</p>}
          </>
        )}
        {job.status === 'failed' && (
          <div className={`${importStyles.result} ${importStyles.error} ${styles.outcome}`}>
            <AlertCircle size={16} />
            <span data-i18n="components.memory.ChronicleOfferPanel.text018">
              {uiText('components.memory.ChronicleOfferPanel.text018', { p1: job.error || job.message || '' })}
            </span>
          </div>
        )}
        {job.status === 'cancelled' && (
          <div className={`${importStyles.result} ${importStyles.error} ${styles.outcome}`}>
            <AlertCircle size={16} />
            <span data-i18n="components.memory.ChronicleOfferPanel.text019">{uiText('components.memory.ChronicleOfferPanel.text019')}</span>
          </div>
        )}
        {job.status === UNTRACKED && (
          <div className={`${importStyles.result} ${importStyles.error} ${styles.outcome}`}>
            <AlertCircle size={16} />
            <span data-i18n="components.memory.ChronicleOfferPanel.text022">{uiText('components.memory.ChronicleOfferPanel.text022')}</span>
          </div>
        )}
        <div className={importStyles.actions}>
          <button
            data-i18n="components.memory.ChronicleOfferPanel.text020"
            className={finished ? importStyles.importButton : importStyles.cancelButton}
            onClick={onDone}
          >
            {uiText('components.memory.ChronicleOfferPanel.text020')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <BookOpen size={20} />
        <h3 data-i18n="components.memory.ChronicleOfferPanel.text001">{uiText('components.memory.ChronicleOfferPanel.text001')}</h3>
      </div>
      <p data-i18n="components.memory.ChronicleOfferPanel.text002" className={styles.body}>{uiText('components.memory.ChronicleOfferPanel.text002')}</p>
      <p data-i18n="components.memory.ChronicleOfferPanel.text003" className={styles.body}>{uiText('components.memory.ChronicleOfferPanel.text003')}</p>
      <div className={styles.estimateBox}>
        <div className={styles.estimateRow}>
          <span data-i18n="components.memory.ChronicleOfferPanel.text004" className={styles.estimateLabel}>{uiText('components.memory.ChronicleOfferPanel.text004')}</span>
          <span data-i18n="components.memory.ChronicleOfferPanel.text005" className={styles.estimateValue}>
            {uiText('components.memory.ChronicleOfferPanel.text005', { p1: formatCount(estimate.unprocessed_messages) })}
          </span>
        </div>
        <div className={styles.estimateRow}>
          <span data-i18n="components.memory.ChronicleOfferPanel.text006" className={styles.estimateLabel}>{uiText('components.memory.ChronicleOfferPanel.text006')}</span>
          <span data-i18n="components.memory.ChronicleOfferPanel.text007" className={styles.estimateValue}>
            {uiText('components.memory.ChronicleOfferPanel.text007', {
              p1: formatCount(estimate.estimated_llm_calls), p2: estimate.model_name,
            })}
          </span>
        </div>
        <div className={styles.estimateRow}>
          <span data-i18n="components.memory.ChronicleOfferPanel.text008" className={styles.estimateLabel}>{uiText('components.memory.ChronicleOfferPanel.text008')}</span>
          <span data-i18n="components.memory.ChronicleOfferPanel.text009 components.memory.ChronicleOfferPanel.text010" className={styles.estimateValue}>
            {estimate.is_free_tier
              ? uiText('components.memory.ChronicleOfferPanel.text009')
              : uiText('components.memory.ChronicleOfferPanel.text010', {
                p1: formatCost(estimate.estimated_cost_usd, estimate.currency),
              })}
          </span>
        </div>
      </div>
      {startError && (
        <div className={`${importStyles.result} ${importStyles.error} ${styles.outcome}`}>
          <AlertCircle size={16} />
          <span data-i18n="components.memory.ChronicleOfferPanel.text021">{uiText('components.memory.ChronicleOfferPanel.text021', { p1: startError })}</span>
        </div>
      )}
      <div className={importStyles.actions}>
        <button
          data-i18n="components.memory.ChronicleOfferPanel.text012"
          className={importStyles.cancelButton}
          onClick={onDone}
          disabled={starting}
        >
          {uiText('components.memory.ChronicleOfferPanel.text012')}
        </button>
        <button
          data-i18n="components.memory.ChronicleOfferPanel.text011"
          className={importStyles.importButton}
          onClick={() => void startGeneration()}
          disabled={starting}
        >
          {starting ? <Loader2 size={16} className={importStyles.loader} /> : null}
          {uiText('components.memory.ChronicleOfferPanel.text011')}
        </button>
      </div>
      <p data-i18n="components.memory.ChronicleOfferPanel.text013" className={styles.laterNote}>{uiText('components.memory.ChronicleOfferPanel.text013')}</p>
    </div>
  );
}
