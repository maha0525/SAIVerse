import React, { useState } from 'react';
import { Search, Loader2, AlertCircle, Brain, Bug, Trash2, FileDown, Activity } from 'lucide-react';
import styles from './MemoryRecall.module.css';

interface MemoryRecallProps {
    personaId: string;
}

interface DebugHit {
    rank: number;
    score: number;
    message_id: string;
    thread_id: string;
    role: string;
    content: string;
    created_at: number;
    created_at_str: string;
}

interface DebugResult {
    query: string;
    topk: number;
    total_hits: number;
    hits: DebugHit[];
}

export default function MemoryRecall({ personaId }: MemoryRecallProps) {
    const [query, setQuery] = useState('');
    const [keywords, setKeywords] = useState('');
    const [topk, setTopk] = useState(4);
    const [maxChars, setMaxChars] = useState(1200);
    const [result, setResult] = useState<string | null>(null);
    const [debugResult, setDebugResult] = useState<DebugResult | null>(null);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [debugMode, setDebugMode] = useState(false);
    const [useRrf, setUseRrf] = useState(false);
    const [useHybrid, setUseHybrid] = useState(false);
    const [startDate, setStartDate] = useState('');
    const [endDate, setEndDate] = useState('');
    const [isDeletingChronicle, setIsDeletingChronicle] = useState(false);
    const [isDeletingMemopedia, setIsDeletingMemopedia] = useState(false);
    const [confirmChronicle, setConfirmChronicle] = useState(false);
    const [confirmMemopedia, setConfirmMemopedia] = useState(false);
    const [deleteResult, setDeleteResult] = useState<string | null>(null);

    // Chronicle diagnosis state
    const [isDiagnosing, setIsDiagnosing] = useState(false);
    const [diagnosisError, setDiagnosisError] = useState<string | null>(null);

    const formatDiagnosisReport = (data: any): string => {
        const lines: string[] = [];
        const formatTime = (ts: number | null): string => {
            if (ts == null) return '(不明)';
            return new Date(ts * 1000).toISOString().replace('T', ' ').replace('.000Z', '') + ' UTC';
        };

        lines.push('Chronicle 診断レポート');
        lines.push('='.repeat(60));
        lines.push(`生成日時: ${formatTime(data.generated_at)}`);
        lines.push(`ペルソナ: ${data.persona_id}`);
        lines.push('');

        lines.push('== 全体サマリー ==');
        lines.push(`総メッセージ数: ${data.total_messages}`);
        lines.push(`Lv1 Chronicle がカバーするメッセージ数: ${data.messages_covered_by_lv1}`);
        lines.push(`最後の Chronicle 以降の未処理メッセージ数: ${data.messages_after_last_chronicle}`);
        lines.push(`Chronicle の最大レベル: ${data.max_level}`);
        if (data.last_chronicle_end_time != null) {
            lines.push(`最後の Chronicle 終端時刻: ${formatTime(data.last_chronicle_end_time)}`);
        }
        if (data.last_processed_at != null) {
            lines.push(`最後の Chronicle 生成日時: ${formatTime(data.last_processed_at)}`);
        }
        if (data.last_processed_message_id) {
            lines.push(`最後に処理したメッセージID: ${data.last_processed_message_id}`);
        }
        lines.push('');

        lines.push('== レベル別統計 ==');
        for (let level = 1; level <= data.max_level; level++) {
            const total = data.counts_by_level[level] ?? 0;
            const unconsolidated = data.unconsolidated_by_level[level] ?? 0;
            const consolidated = total - unconsolidated;
            lines.push(`  Level ${level}: 計 ${total} 件  (統合済み: ${consolidated}  未統合: ${unconsolidated})`);
        }
        lines.push('');

        // Per-level entry details
        for (let level = 1; level <= data.max_level; level++) {
            const entries: any[] = data.level_details[level] ?? [];
            lines.push(`== Level ${level} Chronicle エントリ詳細 (${entries.length} 件) ==`);
            entries.forEach((entry: any, idx: number) => {
                lines.push(`  [${idx + 1}] ID: ${entry.id}`);
                lines.push(`       期間: ${formatTime(entry.start_time)}  〜  ${formatTime(entry.end_time)}`);
                lines.push(`       含むメッセージ数: ${entry.message_count}  ソース数: ${entry.source_count}`);
                const consolidatedStr = entry.is_consolidated
                    ? `はい → 親ID: ${entry.parent_id ?? '(なし)'}`
                    : 'いいえ';
                lines.push(`       統合済み: ${consolidatedStr}`);
            });
            lines.push('');
        }

        // Gap analysis
        lines.push('== ギャップ分析 (Chronicle 間の孤立メッセージ) ==');
        if (data.gaps.length > 0) {
            data.gaps.forEach((gap: any, idx: number) => {
                lines.push(`  ギャップ ${idx + 1}:`);
                lines.push(`    孤立メッセージ数: ${gap.isolated_message_count}`);
                lines.push(`    期間: ${formatTime(gap.gap_start_time)}  〜  ${formatTime(gap.gap_end_time)}`);
                lines.push(`    直前の Chronicle ID: ${gap.prev_chronicle_id}`);
                lines.push(`    直後の Chronicle ID: ${gap.next_chronicle_id}`);
            });
        } else {
            lines.push('  ギャップなし (Level 1 Chronicle 間に孤立メッセージは検出されませんでした)');
        }

        return lines.join('\n');
    };

    const handleDownloadDiagnosis = async () => {
        setIsDiagnosing(true);
        setDiagnosisError(null);
        try {
            const backendUrl = 'http://127.0.0.1:8000';
            const res = await fetch(`${backendUrl}/api/people/${personaId}/arasuji/diagnosis`);
            if (!res.ok) {
                const text = await res.text();
                try {
                    const data = JSON.parse(text);
                    throw new Error(data.detail || 'Chronicle診断の取得に失敗しました');
                } catch {
                    throw new Error(`Server error: ${text.substring(0, 200)}`);
                }
            }
            const data = await res.json();
            const report = formatDiagnosisReport(data);
            const blob = new Blob([report], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
            a.download = `chronicle_diagnosis_${personaId}_${ts}.txt`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (err: any) {
            setDiagnosisError(err.message || 'エラーが発生しました');
        } finally {
            setIsDiagnosing(false);
        }
    };

    const handleRecall = async () => {
        const trimmedQuery = query.trim();
        const keywordList = keywords.split(/[,\s]+/).filter(k => k.trim());

        if (!trimmedQuery && keywordList.length === 0) {
            setError('検索クエリまたはキーワードを入力してね');
            return;
        }

        setIsLoading(true);
        setError(null);
        setResult(null);
        setDebugResult(null);

        try {
            if (debugMode) {
                // Debug mode: use recall-debug endpoint
                // Call backend directly to avoid Next.js proxy timeout
                const backendUrl = 'http://127.0.0.1:8000';
                const res = await fetch(`${backendUrl}/api/people/${personaId}/recall-debug`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        query: trimmedQuery,
                        keywords: keywordList,
                        topk,
                        use_rrf: useRrf,
                        use_hybrid: useHybrid,
                        start_date: startDate || null,
                        end_date: endDate || null,
                    }),
                });

                if (!res.ok) {
                    // Try to parse as JSON, fall back to text
                    const text = await res.text();
                    try {
                        const data = JSON.parse(text);
                        throw new Error(data.detail || 'Memory recall debug failed');
                    } catch {
                        throw new Error(`Server error: ${text.substring(0, 200)}`);
                    }
                }

                const data = await res.json();
                setDebugResult(data);
            } else {
                // Normal mode: use regular recall endpoint
                const res = await fetch(`/api/people/${personaId}/recall`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        query: query.trim(),
                        topk,
                        max_chars: maxChars,
                    }),
                });

                if (!res.ok) {
                    const data = await res.json();
                    throw new Error(data.detail || 'Memory recall failed');
                }

                const data = await res.json();
                setResult(data.result);
            }
        } catch (err: any) {
            setError(err.message || 'An error occurred');
        } finally {
            setIsLoading(false);
        }
    };

    const handleKeyPress = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleRecall();
        }
    };

    const getScoreColor = (score: number): string => {
        // High score (>0.9): green, Medium (0.7-0.9): yellow, Low (<0.7): red
        if (score >= 0.9) return '#2b8a3e';
        if (score >= 0.8) return '#74b816';
        if (score >= 0.7) return '#fab005';
        if (score >= 0.6) return '#fd7e14';
        return '#fa5252';
    };

    const handleDeleteAllChronicle = async () => {
        setIsDeletingChronicle(true);
        setDeleteResult(null);
        try {
            const backendUrl = 'http://127.0.0.1:8000';
            const res = await fetch(`${backendUrl}/api/people/${personaId}/arasuji`, {
                method: 'DELETE',
            });
            if (!res.ok) {
                const text = await res.text();
                throw new Error(text.substring(0, 200));
            }
            const data = await res.json();
            setDeleteResult(`Chronicle: ${data.deleted_count}件を削除しました`);
        } catch (err: any) {
            setDeleteResult(`Chronicle削除エラー: ${err.message}`);
        } finally {
            setIsDeletingChronicle(false);
            setConfirmChronicle(false);
        }
    };

    const handleDeleteAllMemopedia = async () => {
        setIsDeletingMemopedia(true);
        setDeleteResult(null);
        try {
            const backendUrl = 'http://127.0.0.1:8000';
            const res = await fetch(`${backendUrl}/api/people/${personaId}/memopedia/pages`, {
                method: 'DELETE',
            });
            if (!res.ok) {
                const text = await res.text();
                throw new Error(text.substring(0, 200));
            }
            const data = await res.json();
            setDeleteResult(`Memopedia: ${data.deleted_count}件のページを削除しました`);
        } catch (err: any) {
            setDeleteResult(`Memopedia削除エラー: ${err.message}`);
        } finally {
            setIsDeletingMemopedia(false);
            setConfirmMemopedia(false);
        }
    };

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <Brain size={24} className={styles.icon} />
                <div>
                    <h3 className={styles.title}>Memory Recall Test</h3>
                    <p className={styles.description}>
                        memory_recall ツールと同じロジックでペルソナの長期記憶を検索できるよ。
                        結果を確認してデバッグに使ってね。
                    </p>
                </div>
            </div>

            {/* Debug Mode Toggle */}
            <div className={styles.modeToggle}>
                <label className={styles.toggleLabel}>
                    <input
                        type="checkbox"
                        checked={debugMode}
                        onChange={(e) => {
                            setDebugMode(e.target.checked);
                            setResult(null);
                            setDebugResult(null);
                            // Reset topk to appropriate default
                            if (e.target.checked) {
                                setTopk(Math.min(topk, 100));
                            }
                        }}
                    />
                    <Bug size={16} />
                    Debug Mode
                    <span className={styles.toggleHint}>
                        {debugMode
                            ? '（生のスコア表示、周辺コンテキストなし）'
                            : '（通常モード）'}
                    </span>
                </label>
                {debugMode && (
                    <>
                        <label className={styles.toggleLabel} style={{ marginTop: '0.5rem' }}>
                            <input
                                type="checkbox"
                                checked={useHybrid}
                                onChange={(e) => {
                                    setUseHybrid(e.target.checked);
                                    if (e.target.checked) setUseRrf(false);
                                    setDebugResult(null);
                                }}
                            />
                            Hybrid Search
                            <span className={styles.toggleHint}>
                                （キーワード + セマンティック検索をRRFで統合）
                            </span>
                        </label>
                        <label className={styles.toggleLabel} style={{ marginTop: '0.5rem' }}>
                            <input
                                type="checkbox"
                                checked={useRrf}
                                disabled={useHybrid}
                                onChange={(e) => {
                                    setUseRrf(e.target.checked);
                                    setDebugResult(null);
                                }}
                            />
                            RRF (Reciprocal Rank Fusion)
                            <span className={styles.toggleHint}>
                                （クエリをスペースで分割して検索、順位を統合）
                            </span>
                        </label>
                    </>
                )}
            </div>

            <div className={styles.inputSection}>
                <label className={styles.label}>
                    {debugMode && useHybrid ? 'セマンティッククエリ（意味で検索）' : '検索クエリ'}
                </label>
                <textarea
                    className={styles.queryInput}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    onKeyDown={handleKeyPress}
                    placeholder={debugMode && useHybrid
                        ? "例: まはーの誕生日を祝った時の会話"
                        : "検索したい内容を入力してね"}
                    rows={2}
                />
            </div>

            {debugMode && useHybrid && (
                <div className={styles.inputSection}>
                    <label className={styles.label}>キーワード（部分一致検索）</label>
                    <input
                        type="text"
                        className={styles.queryInput}
                        value={keywords}
                        onChange={(e) => setKeywords(e.target.value)}
                        placeholder="例: 誕生日, 1月14日, おめでとう"
                        style={{ padding: '0.75rem' }}
                    />
                    <p className={styles.keywordHint}>
                        カンマまたはスペースで区切って複数指定可能
                    </p>
                </div>
            )}

            {debugMode && (
                <div className={styles.dateRangeSection}>
                    <label className={styles.label}>日時範囲（オプション）</label>
                    <div className={styles.dateInputs}>
                        <input
                            type="date"
                            value={startDate}
                            onChange={(e) => setStartDate(e.target.value)}
                            className={styles.dateInput}
                        />
                        <span className={styles.dateSeparator}>〜</span>
                        <input
                            type="date"
                            value={endDate}
                            onChange={(e) => setEndDate(e.target.value)}
                            className={styles.dateInput}
                        />
                        {(startDate || endDate) && (
                            <button
                                type="button"
                                className={styles.clearDateBtn}
                                onClick={() => { setStartDate(''); setEndDate(''); }}
                            >
                                クリア
                            </button>
                        )}
                    </div>
                </div>
            )}

            <div className={styles.paramsRow}>
                <div className={styles.param}>
                    <label className={styles.paramLabel}>
                        topk (取得するシード数)
                    </label>
                    <input
                        type="range"
                        min={1}
                        max={debugMode ? 100 : 20}
                        value={topk}
                        onChange={(e) => setTopk(Number(e.target.value))}
                        className={styles.slider}
                    />
                    <span className={styles.paramValue}>{topk}</span>
                </div>
                {!debugMode && (
                    <div className={styles.param}>
                        <label className={styles.paramLabel}>max_chars (出力文字数上限)</label>
                        <input
                            type="range"
                            min={100}
                            max={10000}
                            step={100}
                            value={maxChars}
                            onChange={(e) => setMaxChars(Number(e.target.value))}
                            className={styles.slider}
                        />
                        <span className={styles.paramValue}>{maxChars}</span>
                    </div>
                )}
            </div>

            <button
                className={styles.executeButton}
                onClick={handleRecall}
                disabled={isLoading || (!query.trim() && !keywords.trim())}
            >
                {isLoading ? (
                    <>
                        <Loader2 size={16} className={styles.loader} />
                        実行中...
                    </>
                ) : (
                    <>
                        <Search size={16} />
                        Memory Recall を実行
                    </>
                )}
            </button>

            {error && (
                <div className={styles.error}>
                    <AlertCircle size={16} />
                    <span>{error}</span>
                </div>
            )}

            {/* Normal mode result */}
            {result !== null && !debugMode && (
                <div className={styles.resultSection}>
                    <label className={styles.label}>実行結果</label>
                    <pre className={styles.resultBox}>{result}</pre>
                </div>
            )}

            {/* Debug mode result */}
            {debugResult !== null && debugMode && (
                <div className={styles.resultSection}>
                    <label className={styles.label}>
                        検索結果 ({debugResult.total_hits} hits)
                    </label>
                    <div className={styles.debugTable}>
                        <table>
                            <thead>
                                <tr>
                                    <th className={styles.rankCol}>#</th>
                                    <th className={styles.scoreCol}>Score</th>
                                    <th className={styles.roleCol}>Role</th>
                                    <th className={styles.dateCol}>Date</th>
                                    <th className={styles.contentCol}>Content</th>
                                </tr>
                            </thead>
                            <tbody>
                                {debugResult.hits.map((hit) => (
                                    <tr key={hit.message_id}>
                                        <td className={styles.rankCol}>{hit.rank}</td>
                                        <td
                                            className={styles.scoreCol}
                                            style={{ color: getScoreColor(hit.score) }}
                                        >
                                            {hit.score.toFixed(4)}
                                        </td>
                                        <td className={styles.roleCol}>
                                            {hit.role === 'assistant' || hit.role === 'model'
                                                ? 'AI'
                                                : hit.role}
                                        </td>
                                        <td className={styles.dateCol}>
                                            {hit.created_at_str}
                                        </td>
                                        <td className={styles.contentCol}>
                                            <div className={styles.contentPreview}>
                                                {hit.content}
                                            </div>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* Chronicle Diagnosis Download */}
            <div className={styles.header} style={{ marginTop: '2rem' }}>
                <Activity size={24} className={styles.icon} />
                <div>
                    <h3 className={styles.title}>Chronicle 診断</h3>
                    <p className={styles.description}>
                        Chronicle の構造情報（レベル別統計・各エントリの期間・ギャップ分析）をテキストファイルでダウンロードします。
                        チャットログや Chronicle の本文は含まれません。
                    </p>
                </div>
            </div>

            <button
                className={styles.executeButton}
                onClick={handleDownloadDiagnosis}
                disabled={isDiagnosing}
                style={{ background: '#495057' }}
            >
                {isDiagnosing ? (
                    <><Loader2 size={16} className={styles.loader} /> 取得中...</>
                ) : (
                    <><FileDown size={16} /> 診断レポートをダウンロード</>
                )}
            </button>

            {diagnosisError && (
                <div className={styles.error}>
                    <AlertCircle size={16} />
                    <span>{diagnosisError}</span>
                </div>
            )}

            {/* Danger Zone: Bulk Delete */}
            <div className={styles.dangerZone}>
                <h4 className={styles.dangerTitle}>Danger Zone</h4>
                <p className={styles.dangerDescription}>
                    データを一括削除します。この操作は取り消せません。
                </p>

                <div className={styles.dangerButtons}>
                    {!confirmChronicle ? (
                        <button
                            className={styles.dangerButton}
                            onClick={() => setConfirmChronicle(true)}
                            disabled={isDeletingChronicle}
                        >
                            <Trash2 size={14} />
                            Chronicle 全削除
                        </button>
                    ) : (
                        <div className={styles.confirmGroup}>
                            <span className={styles.confirmText}>本当に削除しますか？</span>
                            <button
                                className={styles.confirmYes}
                                onClick={handleDeleteAllChronicle}
                                disabled={isDeletingChronicle}
                            >
                                {isDeletingChronicle ? (
                                    <><Loader2 size={14} className={styles.loader} /> 削除中...</>
                                ) : (
                                    '削除する'
                                )}
                            </button>
                            <button
                                className={styles.confirmNo}
                                onClick={() => setConfirmChronicle(false)}
                                disabled={isDeletingChronicle}
                            >
                                キャンセル
                            </button>
                        </div>
                    )}

                    {!confirmMemopedia ? (
                        <button
                            className={styles.dangerButton}
                            onClick={() => setConfirmMemopedia(true)}
                            disabled={isDeletingMemopedia}
                        >
                            <Trash2 size={14} />
                            Memopedia 全削除
                        </button>
                    ) : (
                        <div className={styles.confirmGroup}>
                            <span className={styles.confirmText}>本当に削除しますか？</span>
                            <button
                                className={styles.confirmYes}
                                onClick={handleDeleteAllMemopedia}
                                disabled={isDeletingMemopedia}
                            >
                                {isDeletingMemopedia ? (
                                    <><Loader2 size={14} className={styles.loader} /> 削除中...</>
                                ) : (
                                    '削除する'
                                )}
                            </button>
                            <button
                                className={styles.confirmNo}
                                onClick={() => setConfirmMemopedia(false)}
                                disabled={isDeletingMemopedia}
                            >
                                キャンセル
                            </button>
                        </div>
                    )}
                </div>

                {deleteResult && (
                    <div className={styles.deleteResult}>
                        {deleteResult}
                    </div>
                )}
            </div>
        </div>
    );
}
