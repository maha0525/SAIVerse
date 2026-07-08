import React, { useCallback, useEffect, useState } from 'react';
import { Search, Loader2, AlertCircle, Anchor, Minus, Plus, CheckCircle2, ChevronRight, ChevronDown } from 'lucide-react';
import styles from './CoreMemoryScene.module.css';

interface CoreMemorySceneProps {
    personaId: string;
}

// API は相対パスで叩く。next.config.ts の rewrite が /api/:path* を
// バックエンドへプロキシするため、同一オリジンで届く。
// ここに http://127.0.0.1:8000 をハードコードすると、スマホ (Tailscale 経由)
// からはループバックが端末自身を指してしまい何も返らない。

interface SearchHit {
    id: string;
    role: string;
    excerpt: string;
    created_at: number;
}

interface SearchResponse {
    keyword: string;
    mode: string; // "keyword" | "semantic"
    total_hits: number;
    hits: SearchHit[];
}

interface WindowMessage {
    id: string;
    speaker: string;
    role: string;
    content: string;
    date: string;
}

interface WindowResponse {
    anchor_id: string;
    rounds: number;
    total_chars: number;
    messages: WindowMessage[];
}

interface CoreMemoryItem {
    id: number;
    ref: string;
    kind: string;
    preview: string;
    content: string;
    char_count: number;
}

interface CoreMemoryListResponse {
    items: CoreMemoryItem[];
    total_chars: number;
    budget: number;
    over_budget: boolean;
}

interface CreateSceneResponse {
    memory_id: number;
    ref: string;
    message_count: number;
    char_count: number;
    total_chars: number;
    budget: number;
    over_budget: boolean;
    date_start: string;
    date_end: string;
}

function formatDate(ts: number): string {
    if (!ts) return '';
    return new Date(ts * 1000).toISOString().slice(0, 10);
}

export default function CoreMemoryScene({ personaId }: CoreMemorySceneProps) {
    // Existing core memory list
    const [coreList, setCoreList] = useState<CoreMemoryListResponse | null>(null);
    const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());

    const toggleExpanded = (id: number) => {
        setExpandedIds((prev) => {
            const next = new Set(prev);
            if (next.has(id)) {
                next.delete(id);
            } else {
                next.add(id);
            }
            return next;
        });
    };

    const loadCoreList = useCallback(async () => {
        try {
            const res = await fetch(`/api/people/${personaId}/core-memory`);
            if (!res.ok) return;
            const data = await res.json();
            setCoreList(data);
        } catch {
            /* non-fatal */
        }
    }, [personaId]);

    useEffect(() => {
        loadCoreList();
    }, [loadCoreList]);

    // Search state
    const [keyword, setKeyword] = useState('');
    const [dateFrom, setDateFrom] = useState('');
    const [dateTo, setDateTo] = useState('');
    const [searching, setSearching] = useState(false);
    const [searchError, setSearchError] = useState<string | null>(null);
    const [searchResult, setSearchResult] = useState<SearchResponse | null>(null);

    const handleSearch = async () => {
        const q = keyword.trim();
        if (!q) {
            setSearchError('キーワードを入力してね');
            return;
        }
        setSearching(true);
        setSearchError(null);
        setSearchResult(null);
        setSelectedAnchor(null);
        setWindowData(null);
        try {
            const params = new URLSearchParams({ keyword: q, limit: '20' });
            if (dateFrom) params.set('date_from', dateFrom);
            if (dateTo) params.set('date_to', dateTo);
            const res = await fetch(
                `/api/people/${personaId}/memory/messages/search?${params.toString()}`
            );
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            const data = await res.json();
            setSearchResult(data);
        } catch (e: any) {
            setSearchError(e.message || '検索に失敗しました');
        } finally {
            setSearching(false);
        }
    };

    // Window preview state
    const [selectedAnchor, setSelectedAnchor] = useState<string | null>(null);
    const [rounds, setRounds] = useState(3);
    const [windowData, setWindowData] = useState<WindowResponse | null>(null);
    const [windowLoading, setWindowLoading] = useState(false);
    const [windowError, setWindowError] = useState<string | null>(null);

    const loadWindow = useCallback(async (anchorId: string, r: number) => {
        setWindowLoading(true);
        setWindowError(null);
        try {
            const res = await fetch(
                `/api/people/${personaId}/memory/messages/${encodeURIComponent(anchorId)}/window?rounds=${r}`
            );
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            const data = await res.json();
            setWindowData(data);
        } catch (e: any) {
            setWindowError(e.message || '会話窓の取得に失敗しました');
            setWindowData(null);
        } finally {
            setWindowLoading(false);
        }
    }, [personaId]);

    const handleSelectAnchor = (anchorId: string) => {
        setSelectedAnchor(anchorId);
        setRounds(3);
        setCarveToast(null);
        loadWindow(anchorId, 3);
    };

    const changeRounds = (delta: number) => {
        if (!selectedAnchor) return;
        const next = Math.max(1, Math.min(20, rounds + delta));
        if (next === rounds) return;
        setRounds(next);
        loadWindow(selectedAnchor, next);
    };

    // Carve state
    const [carving, setCarving] = useState(false);
    const [carveError, setCarveError] = useState<string | null>(null);
    const [carveToast, setCarveToast] = useState<string | null>(null);

    const handleCarve = async () => {
        if (!selectedAnchor) return;
        setCarving(true);
        setCarveError(null);
        setCarveToast(null);
        try {
            const res = await fetch(`/api/people/${personaId}/core-memory/scene`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ anchor_id: selectedAnchor, rounds }),
            });
            const data: CreateSceneResponse = await res.json();
            if (!res.ok) {
                throw new Error((data as any).detail || `HTTP ${res.status}`);
            }
            setCarveToast(
                `コア記憶 ${data.ref} を刻みました（${data.message_count} 発言・${data.char_count.toLocaleString()} 字）。` +
                `head への反映は次の記憶整理から。`
            );
            setSelectedAnchor(null);
            setWindowData(null);
            loadCoreList();
        } catch (e: any) {
            setCarveError(e.message || 'コア記憶への追加に失敗しました');
        } finally {
            setCarving(false);
        }
    };

    // Char preview: current total + this cut → new total (vs budget)
    const budget = coreList?.budget ?? 2000;
    const currentTotal = coreList?.total_chars ?? 0;
    const cutChars = windowData?.total_chars ?? 0;
    const newTotal = currentTotal + cutChars;
    const willBeOver = newTotal > budget;

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <Anchor size={24} className={styles.icon} />
                <div>
                    <h3 className={styles.title}>会話を探してコア記憶に刻む</h3>
                    <p className={styles.description}>
                        「そのペルソナらしさが最も出た会話」を探して、原文のままコア記憶（scene）として頭に常駐させます。
                        口調が安定しないとき、記述指示より実際の会話例のほうが強く効きます。
                    </p>
                </div>
            </div>

            {/* Search form */}
            <div className={styles.searchForm}>
                <div className={styles.searchRow}>
                    <input
                        className={styles.keywordInput}
                        value={keyword}
                        onChange={(e) => setKeyword(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter') handleSearch();
                        }}
                        placeholder="キーワード（空白区切りで AND 検索）"
                    />
                    <button
                        className={styles.searchButton}
                        onClick={handleSearch}
                        disabled={searching || !keyword.trim()}
                    >
                        {searching ? <Loader2 size={16} className={styles.loader} /> : <Search size={16} />}
                        検索
                    </button>
                </div>
                <div className={styles.dateRow}>
                    <label>
                        期間{' '}
                        <input
                            type="date"
                            className={styles.dateInput}
                            value={dateFrom}
                            onChange={(e) => setDateFrom(e.target.value)}
                        />
                    </label>
                    <span>〜</span>
                    <input
                        type="date"
                        className={styles.dateInput}
                        value={dateTo}
                        onChange={(e) => setDateTo(e.target.value)}
                    />
                    <span>（任意）</span>
                </div>
            </div>

            {searchError && (
                <div className={styles.error}>
                    <AlertCircle size={16} />
                    <span>{searchError}</span>
                </div>
            )}

            {carveToast && (
                <div className={styles.toast}>
                    <CheckCircle2 size={16} />
                    <span>{carveToast}</span>
                </div>
            )}

            {/* Search results */}
            {searchResult && (
                <>
                    {searchResult.mode === 'semantic' && (
                        <div className={styles.semanticNote}>
                            キーワード一致がなかったため、意味の近さで検索しました（セマンティック検索）。
                        </div>
                    )}
                    <div className={styles.results}>
                        {searchResult.hits.length === 0 ? (
                            <div className={styles.emptyResults}>該当する会話が見つかりませんでした。</div>
                        ) : (
                            searchResult.hits.map((hit) => (
                                <div
                                    key={hit.id}
                                    className={`${styles.resultItem} ${selectedAnchor === hit.id ? styles.selected : ''}`}
                                    onClick={() => handleSelectAnchor(hit.id)}
                                >
                                    <div className={styles.resultMeta}>
                                        <span>{formatDate(hit.created_at)}</span>
                                        <span>{hit.role}</span>
                                    </div>
                                    <div className={styles.resultExcerpt}>{hit.excerpt}</div>
                                </div>
                            ))
                        )}
                    </div>
                </>
            )}

            {/* Window preview */}
            {selectedAnchor && (
                <div className={styles.windowPanel}>
                    <div className={styles.windowHeader}>
                        <span className={styles.windowTitle}>この会話を刻む（前後の往復を確認）</span>
                        <div className={styles.roundsControl}>
                            往復数
                            <button
                                className={styles.roundsButton}
                                onClick={() => changeRounds(-1)}
                                disabled={windowLoading || rounds <= 1}
                                aria-label="往復を減らす"
                            >
                                <Minus size={14} />
                            </button>
                            <span className={styles.roundsValue}>{rounds}</span>
                            <button
                                className={styles.roundsButton}
                                onClick={() => changeRounds(1)}
                                disabled={windowLoading || rounds >= 20}
                                aria-label="往復を増やす"
                            >
                                <Plus size={14} />
                            </button>
                        </div>
                    </div>

                    {windowError && (
                        <div className={styles.error}>
                            <AlertCircle size={16} />
                            <span>{windowError}</span>
                        </div>
                    )}

                    {windowLoading && !windowData && (
                        <div className={styles.emptyResults}>
                            <Loader2 size={14} className={styles.loader} /> 読み込み中...
                        </div>
                    )}

                    {windowData && (
                        <>
                            <div className={styles.transcript}>
                                {windowData.messages.map((m) => {
                                    const isPersona = m.role === 'model' || m.role === 'assistant';
                                    return (
                                        <div
                                            key={m.id}
                                            className={`${styles.turn} ${isPersona ? styles.turnPersona : styles.turnUser}`}
                                        >
                                            <span className={styles.turnSpeaker}>
                                                {m.speaker} · {m.date}
                                            </span>
                                            <span className={styles.turnContent}>{m.content}</span>
                                        </div>
                                    );
                                })}
                            </div>

                            <div className={`${styles.charPreview} ${willBeOver ? styles.charPreviewOver : ''}`}>
                                この切り抜き: <span className={styles.charNum}>{cutChars.toLocaleString()}</span> 字
                                {' / '}現在のコア記憶合計 <span className={styles.charNum}>{currentTotal.toLocaleString()}</span> 字
                                {' → '}刻むと{' '}
                                <span className={willBeOver ? styles.charNumOver : styles.charNum}>
                                    {newTotal.toLocaleString()}
                                </span>{' '}
                                字（目安 {budget.toLocaleString()} 字）
                                {willBeOver && '　※ 目安を超えます'}
                            </div>

                            <button className={styles.carveButton} onClick={handleCarve} disabled={carving}>
                                {carving ? <Loader2 size={16} className={styles.loader} /> : <Anchor size={16} />}
                                コア記憶に刻む
                            </button>

                            {carveError && (
                                <div className={styles.error} style={{ marginTop: '0.75rem' }}>
                                    <AlertCircle size={16} />
                                    <span>{carveError}</span>
                                </div>
                            )}
                        </>
                    )}
                </div>
            )}

            {/* Existing core memory list (reference — kept at the bottom so the
                search → carve flow comes first) */}
            <div className={styles.coreList}>
                <div className={styles.coreListHeader}>
                    <span>既存のコア記憶（読み取り専用・編集はペルソナの領分）</span>
                    {coreList && (
                        <span className={`${styles.budgetInfo} ${coreList.over_budget ? styles.budgetOver : ''}`}>
                            合計 {coreList.total_chars.toLocaleString()} 字 / 目安 {coreList.budget.toLocaleString()} 字
                        </span>
                    )}
                </div>
                {coreList && coreList.items.length > 0 ? (
                    coreList.items.map((it) => {
                        const expanded = expandedIds.has(it.id);
                        return (
                            <div key={it.id} className={styles.coreItem}>
                                <button
                                    type="button"
                                    className={styles.coreItemRow}
                                    onClick={() => toggleExpanded(it.id)}
                                    aria-expanded={expanded}
                                    title={expanded ? '折りたたむ' : '全文を表示'}
                                >
                                    {expanded ? (
                                        <ChevronDown size={14} className={styles.coreChevron} />
                                    ) : (
                                        <ChevronRight size={14} className={styles.coreChevron} />
                                    )}
                                    <span className={`${styles.kindBadge} ${it.kind === 'scene' ? styles.kindScene : styles.kindNote}`}>
                                        {it.kind}
                                    </span>
                                    <span className={styles.corePreview}>
                                        <strong>{it.ref}</strong> {it.preview}
                                    </span>
                                    <span className={styles.coreChars}>{it.char_count.toLocaleString()}字</span>
                                </button>
                                {expanded && <div className={styles.coreFull}>{it.content}</div>}
                            </div>
                        );
                    })
                ) : (
                    <div className={styles.emptyCore}>まだコア記憶はありません。</div>
                )}
            </div>
        </div>
    );
}
