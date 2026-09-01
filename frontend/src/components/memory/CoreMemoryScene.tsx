import React, { useCallback, useEffect, useState } from 'react';
import { Search, Loader2, AlertCircle, Anchor, Minus, Plus, CheckCircle2, ChevronRight, ChevronDown, Check, Pencil, Trash2, RotateCcw, X, Save } from 'lucide-react';
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
    confirmed: number;      // 1=確認済み / 0=未確認 (自動採取)
    created_at: number;
    updated_at: number;
    deleted_at: number | null;
}

interface CoreMemoryListResponse {
    items: CoreMemoryItem[];
    total_chars: number;
    budget: number;
    over_budget: boolean;
    unconfirmed_count: number;
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

    // --- Correction導線: confirm / edit / delete / restore ---
    const [rowBusy, setRowBusy] = useState<number | null>(null);   // 操作中の item id
    const [rowError, setRowError] = useState<string | null>(null);
    const [editingId, setEditingId] = useState<number | null>(null);
    const [editContent, setEditContent] = useState('');

    // ごみ箱
    const [trash, setTrash] = useState<CoreMemoryItem[]>([]);
    const [trashOpen, setTrashOpen] = useState(false);

    const loadTrash = useCallback(async () => {
        try {
            const res = await fetch(`/api/people/${personaId}/core-memory/trash`);
            if (!res.ok) return;
            const data: CoreMemoryListResponse = await res.json();
            setTrash(data.items);
        } catch {
            /* non-fatal */
        }
    }, [personaId]);

    useEffect(() => {
        loadTrash();
    }, [loadTrash]);

    // 変更系レスポンスで一覧・ごみ箱・バッジをまとめて更新する。
    const refreshAfterMutation = useCallback(async () => {
        await Promise.all([loadCoreList(), loadTrash()]);
    }, [loadCoreList, loadTrash]);

    const runRowAction = async (id: number, req: () => Promise<Response>) => {
        setRowBusy(id);
        setRowError(null);
        try {
            const res = await req();
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            await refreshAfterMutation();
            return true;
        } catch (e: any) {
            setRowError(e.message || '操作に失敗しました');
            return false;
        } finally {
            setRowBusy(null);
        }
    };

    const handleConfirm = (id: number) =>
        runRowAction(id, () =>
            fetch(`/api/people/${personaId}/core-memory/${id}/confirm`, { method: 'POST' })
        );

    const handleDelete = (id: number) =>
        runRowAction(id, () =>
            fetch(`/api/people/${personaId}/core-memory/${id}`, { method: 'DELETE' })
        );

    const handleRestore = (id: number) =>
        runRowAction(id, () =>
            fetch(`/api/people/${personaId}/core-memory/${id}/restore`, { method: 'POST' })
        );

    const startEdit = (it: CoreMemoryItem) => {
        setEditingId(it.id);
        setEditContent(it.content);
        setRowError(null);
    };

    const cancelEdit = () => {
        setEditingId(null);
        setEditContent('');
    };

    const handleSaveEdit = async (id: number) => {
        const content = editContent.trim();
        if (!content) {
            setRowError('本文が空です。');
            return;
        }
        const ok = await runRowAction(id, () =>
            fetch(`/api/people/${personaId}/core-memory/${id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content }),
            })
        );
        if (ok) cancelEdit();
    };

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
            {/* タブ全体の説明。下の検索フォームはコア記憶を刻むための一機能で
                あって、コア記憶そのものの説明ではない (2026-09-01 まはー指摘)。 */}
            <div className={styles.tabIntro}>
                <h3 className={styles.tabIntroTitle}>コア記憶</h3>
                <p className={styles.tabIntroText}>
                    コア記憶は、ペルソナが常に頭に置いている記憶です。会話のたびに毎回読み込まれ、
                    口調や自己認識の土台になります。いま刻まれているコア記憶は、このタブで確認・編集・削除ができます。
                </p>
            </div>

            <div className={styles.header}>
                <Anchor size={24} className={styles.icon} />
                <div>
                    <h3 className={styles.title}>会話を探して刻む</h3>
                    <p className={styles.description}>
                        過去の会話から「そのペルソナらしさが出た場面」を探して、原文のままコア記憶に刻む道具です。
                        口調が安定しないとき、言葉で説明するより実際の会話例のほうが強く効きます。
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

            {/* Existing core memory list (kept at the bottom so the
                search → carve flow comes first). 確認・訂正・削除ができる。 */}
            <div className={styles.coreList}>
                <div className={styles.coreListHeader}>
                    <span>
                        コア記憶（確認・訂正・削除ができます）
                        {coreList && coreList.unconfirmed_count > 0 && (
                            <span className={styles.unconfirmedBadge}>
                                未確認 {coreList.unconfirmed_count}
                            </span>
                        )}
                    </span>
                    {coreList && (
                        <span className={`${styles.budgetInfo} ${coreList.over_budget ? styles.budgetOver : ''}`}>
                            合計 {coreList.total_chars.toLocaleString()} 字 / 目安 {coreList.budget.toLocaleString()} 字
                        </span>
                    )}
                </div>

                {rowError && (
                    <div className={styles.error} style={{ marginBottom: '0.5rem' }}>
                        <AlertCircle size={16} />
                        <span>{rowError}</span>
                    </div>
                )}

                {coreList && coreList.items.length > 0 ? (
                    coreList.items.map((it) => {
                        const expanded = expandedIds.has(it.id);
                        const editing = editingId === it.id;
                        const busy = rowBusy === it.id;
                        const unconfirmed = it.confirmed === 0;
                        return (
                            <div
                                key={it.id}
                                className={`${styles.coreItem} ${unconfirmed ? styles.coreItemUnconfirmed : ''}`}
                            >
                                <div className={styles.coreItemRow}>
                                    <button
                                        type="button"
                                        className={styles.coreToggle}
                                        onClick={() => toggleExpanded(it.id)}
                                        aria-expanded={expanded}
                                        title={expanded ? '折りたたむ' : '全文を表示'}
                                        disabled={editing}
                                    >
                                        {expanded ? (
                                            <ChevronDown size={14} className={styles.coreChevron} />
                                        ) : (
                                            <ChevronRight size={14} className={styles.coreChevron} />
                                        )}
                                        <span className={`${styles.kindBadge} ${it.kind === 'scene' ? styles.kindScene : styles.kindNote}`}>
                                            {it.kind}
                                        </span>
                                        {unconfirmed && (
                                            <span className={styles.unconfirmedDot} title="自動採取・未確認">●</span>
                                        )}
                                        <span className={styles.corePreview}>
                                            <strong>{it.ref}</strong> {it.preview}
                                        </span>
                                    </button>
                                    <span className={styles.coreChars}>{it.char_count.toLocaleString()}字</span>
                                    {!editing && (
                                        <div className={styles.coreActions}>
                                            {unconfirmed && (
                                                <button
                                                    type="button"
                                                    className={styles.rowActionBtn}
                                                    onClick={() => handleConfirm(it.id)}
                                                    disabled={busy}
                                                    title="この採取内容を確認済みにする"
                                                >
                                                    {busy ? <Loader2 size={13} className={styles.loader} /> : <Check size={13} />}
                                                    確認
                                                </button>
                                            )}
                                            <button
                                                type="button"
                                                className={styles.rowActionBtn}
                                                onClick={() => startEdit(it)}
                                                disabled={busy}
                                                title="本文を訂正する"
                                            >
                                                <Pencil size={13} />
                                                編集
                                            </button>
                                            <button
                                                type="button"
                                                className={`${styles.rowActionBtn} ${styles.rowActionBtnDanger}`}
                                                onClick={() => handleDelete(it.id)}
                                                disabled={busy}
                                                title="ごみ箱へ移す（復元できます）"
                                            >
                                                {busy ? <Loader2 size={13} className={styles.loader} /> : <Trash2 size={13} />}
                                                削除
                                            </button>
                                        </div>
                                    )}
                                </div>

                                {editing ? (
                                    <div className={styles.editArea}>
                                        <textarea
                                            className={styles.editTextarea}
                                            value={editContent}
                                            onChange={(e) => setEditContent(e.target.value)}
                                            rows={Math.min(12, Math.max(3, editContent.split('\n').length + 1))}
                                        />
                                        <div className={styles.editButtons}>
                                            <button
                                                type="button"
                                                className={styles.rowActionBtn}
                                                onClick={() => handleSaveEdit(it.id)}
                                                disabled={busy}
                                            >
                                                {busy ? <Loader2 size={13} className={styles.loader} /> : <Save size={13} />}
                                                保存
                                            </button>
                                            <button
                                                type="button"
                                                className={styles.rowActionBtn}
                                                onClick={cancelEdit}
                                                disabled={busy}
                                            >
                                                <X size={13} />
                                                キャンセル
                                            </button>
                                        </div>
                                    </div>
                                ) : (
                                    expanded && <div className={styles.coreFull}>{it.content}</div>
                                )}
                            </div>
                        );
                    })
                ) : (
                    <div className={styles.emptyCore}>まだコア記憶はありません。</div>
                )}
            </div>

            {/* ごみ箱（soft-delete 済み・復元できる） */}
            {trash.length > 0 && (
                <div className={styles.coreList}>
                    <button
                        type="button"
                        className={styles.trashToggle}
                        onClick={() => setTrashOpen((v) => !v)}
                        aria-expanded={trashOpen}
                    >
                        {trashOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        <Trash2 size={14} />
                        ごみ箱（{trash.length}）
                    </button>
                    {trashOpen &&
                        trash.map((it) => (
                            <div key={it.id} className={styles.coreItem}>
                                <div className={styles.coreItemRow}>
                                    <span className={`${styles.kindBadge} ${it.kind === 'scene' ? styles.kindScene : styles.kindNote}`}>
                                        {it.kind}
                                    </span>
                                    <span className={styles.corePreview}>
                                        <strong>{it.ref}</strong> {it.preview}
                                    </span>
                                    <span className={styles.coreChars}>{it.char_count.toLocaleString()}字</span>
                                    <div className={styles.coreActions}>
                                        <button
                                            type="button"
                                            className={styles.rowActionBtn}
                                            onClick={() => handleRestore(it.id)}
                                            disabled={rowBusy === it.id}
                                            title="ごみ箱から戻す"
                                        >
                                            {rowBusy === it.id ? <Loader2 size={13} className={styles.loader} /> : <RotateCcw size={13} />}
                                            復元
                                        </button>
                                    </div>
                                </div>
                            </div>
                        ))}
                </div>
            )}
        </div>
    );
}
