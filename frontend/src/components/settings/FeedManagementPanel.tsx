import React, { useEffect, useState, useCallback } from 'react';
import { Plus, Trash2, RefreshCw, DownloadCloud, Rss, Newspaper, AlertTriangle, ExternalLink, X } from 'lucide-react';
import styles from './FeedManagementPanel.module.css';

interface FeedSubscriptionInfo {
    subscription_id: string;
    title: string;
    feed_url: string;
    site_url?: string | null;
    enabled: boolean;
    last_ok_at?: string | null;
    last_error?: string | null;
    consecutive_failures: number;
}

interface FeedFixtureInfo {
    fixture_id: string;
    building_id: string;
    building_name?: string | null;
    name: string;
    description: string;
    subscriptions: FeedSubscriptionInfo[];
}

interface FeedPresetInfo {
    id: string;
    name: string;
    description: string;
    feed_count: number;
    feed_titles: string[];
}

interface BuildingOption {
    id: string;
    name: string;
}

interface FeedCandidate {
    url: string;
    title: string;
}

interface FeedItemInfo {
    title: string;
    summary: string;
    link: string;
    published_at?: string | null;
    subscription_title: string;
}

const FAILURE_WARN_THRESHOLD = 3;

function formatDateTime(iso?: string | null): string {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleString('ja-JP', {
        year: 'numeric', month: 'numeric', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
    });
}

export default function FeedManagementPanel() {
    const [fixtures, setFixtures] = useState<FeedFixtureInfo[]>([]);
    const [presets, setPresets] = useState<FeedPresetInfo[]>([]);
    const [buildings, setBuildings] = useState<BuildingOption[]>([]);
    const [loading, setLoading] = useState(false);
    const [notice, setNotice] = useState<string | null>(null);

    // 施設の新規作成フォーム
    const [showCreate, setShowCreate] = useState(false);
    const [createBuildingId, setCreateBuildingId] = useState('');
    const [createPresetId, setCreatePresetId] = useState(''); // '' = カスタム (空の施設)
    const [createName, setCreateName] = useState('');
    const [createDesc, setCreateDesc] = useState('');
    const [creating, setCreating] = useState(false);
    const [createError, setCreateError] = useState<string | null>(null);

    // 購読追加 (施設ごとの入力状態)
    const [urlInputs, setUrlInputs] = useState<Record<string, string>>({});
    const [candidates, setCandidates] = useState<Record<string, FeedCandidate[]>>({});
    const [subErrors, setSubErrors] = useState<Record<string, string>>({});
    const [addingFixtureId, setAddingFixtureId] = useState<string | null>(null);

    // 記事ビューア
    const [openItemsFixtureId, setOpenItemsFixtureId] = useState<string | null>(null);
    const [items, setItems] = useState<FeedItemInfo[]>([]);
    const [itemsLoading, setItemsLoading] = useState(false);

    const loadFixtures = useCallback(async () => {
        setLoading(true);
        try {
            const res = await fetch('/api/feeds/fixtures');
            if (res.ok) setFixtures(await res.json());
        } catch (e) {
            console.error('Failed to load feed fixtures', e);
        } finally {
            setLoading(false);
        }
    }, []);

    const loadPresetsAndBuildings = useCallback(async () => {
        try {
            const [pRes, bRes] = await Promise.all([
                fetch('/api/feeds/presets'),
                fetch('/api/user/buildings'),
            ]);
            if (pRes.ok) setPresets(await pRes.json());
            if (bRes.ok) {
                const data = await bRes.json();
                setBuildings((data.buildings || []).map((b: { id: string; name: string }) => ({ id: b.id, name: b.name })));
            }
        } catch (e) {
            console.error('Failed to load feed presets / buildings', e);
        }
    }, []);

    useEffect(() => {
        loadFixtures();
        loadPresetsAndBuildings();
    }, [loadFixtures, loadPresetsAndBuildings]);

    const handleCreateFixture = async () => {
        setCreateError(null);
        if (!createBuildingId) {
            setCreateError('設置先の Building を選んでください。');
            return;
        }
        if (!createPresetId && !createName.trim()) {
            setCreateError('プリセットを選ぶか、施設の名前を入力してください。');
            return;
        }
        setCreating(true);
        try {
            const body = createPresetId
                ? { building_id: createBuildingId, preset_id: createPresetId }
                : { building_id: createBuildingId, name: createName.trim(), description: createDesc.trim() };
            const res = await fetch('/api/feeds/fixtures', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) {
                const data = await res.json().catch(() => null);
                setCreateError(data?.detail || `作成に失敗しました (HTTP ${res.status})`);
                return;
            }
            setShowCreate(false);
            setCreatePresetId('');
            setCreateName('');
            setCreateDesc('');
            await loadFixtures();
        } catch (e) {
            setCreateError(`作成に失敗しました: ${e}`);
        } finally {
            setCreating(false);
        }
    };

    const postSubscription = async (fixtureId: string, url: string) => {
        setSubErrors(prev => ({ ...prev, [fixtureId]: '' }));
        setAddingFixtureId(fixtureId);
        try {
            const res = await fetch('/api/feeds/subscriptions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ fixture_id: fixtureId, url }),
            });
            const data = await res.json().catch(() => null);
            if (!res.ok) {
                setSubErrors(prev => ({ ...prev, [fixtureId]: data?.detail || `追加に失敗しました (HTTP ${res.status})` }));
                return;
            }
            if (data?.status === 'candidates') {
                // 複数のフィードが見つかった: ユーザーに選んでもらう
                setCandidates(prev => ({ ...prev, [fixtureId]: data.candidates || [] }));
                return;
            }
            // 購読成功
            setUrlInputs(prev => ({ ...prev, [fixtureId]: '' }));
            setCandidates(prev => ({ ...prev, [fixtureId]: [] }));
            await loadFixtures();
        } catch (e) {
            setSubErrors(prev => ({ ...prev, [fixtureId]: `追加に失敗しました: ${e}` }));
        } finally {
            setAddingFixtureId(null);
        }
    };

    const handleAddSubscription = (fixtureId: string) => {
        const url = (urlInputs[fixtureId] || '').trim();
        if (!url) {
            setSubErrors(prev => ({ ...prev, [fixtureId]: 'URL を入力してください。' }));
            return;
        }
        setCandidates(prev => ({ ...prev, [fixtureId]: [] }));
        postSubscription(fixtureId, url);
    };

    const handleDeleteSubscription = async (sub: FeedSubscriptionInfo) => {
        const label = sub.title || sub.feed_url;
        if (!confirm(`購読「${label}」を削除しますか？\n取得済みの記事も一緒に消えます。`)) return;
        try {
            const res = await fetch(`/api/feeds/subscriptions/${sub.subscription_id}`, { method: 'DELETE' });
            if (!res.ok) {
                const data = await res.json().catch(() => null);
                alert(`削除に失敗しました: ${data?.detail || res.status}`);
                return;
            }
            await loadFixtures();
        } catch (e) {
            alert(`削除に失敗しました: ${e}`);
        }
    };

    const handleFetchNow = async () => {
        setNotice(null);
        try {
            const res = await fetch('/api/feeds/fetch', { method: 'POST' });
            if (res.ok) {
                setNotice('取得を開始しました。少し待ってから「再読み込み」を押すと結果が反映されます。');
            } else {
                // 409 (取得処理が既に実行中です) 等はサーバーの detail をそのまま見せる
                const data = await res.json().catch(() => null);
                setNotice(data?.detail || `取得の開始に失敗しました (HTTP ${res.status})`);
            }
        } catch (e) {
            setNotice(`取得の開始に失敗しました: ${e}`);
        }
    };

    const loadItems = useCallback(async (fixtureId: string) => {
        setItemsLoading(true);
        try {
            const res = await fetch(`/api/feeds/items?fixture_id=${encodeURIComponent(fixtureId)}&limit=50`);
            if (res.ok) {
                const data = await res.json();
                setItems(data.items || []);
            } else {
                setItems([]);
            }
        } catch (e) {
            console.error('Failed to load feed items', e);
            setItems([]);
        } finally {
            setItemsLoading(false);
        }
    }, []);

    const toggleItems = (fixtureId: string) => {
        if (openItemsFixtureId === fixtureId) {
            setOpenItemsFixtureId(null);
            setItems([]);
            return;
        }
        setOpenItemsFixtureId(fixtureId);
        loadItems(fixtureId);
    };

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <h3><Rss size={18} /> フィード</h3>
                <div className={styles.actions}>
                    <button className={styles.btnSecondary} onClick={handleFetchNow}>
                        <DownloadCloud size={14} /> 今すぐ取得
                    </button>
                    <button className={styles.btnSecondary} onClick={loadFixtures}>
                        <RefreshCw size={14} /> 再読み込み
                    </button>
                    <button className={styles.btnPrimary} onClick={() => setShowCreate(v => !v)}>
                        <Plus size={14} /> 施設を追加
                    </button>
                </div>
            </div>

            <p className={styles.intro}>
                フィードは、ニュースサイトやブログが公開している「新着記事の一覧」です。
                Building にフィード施設を置くと、そこにいるペルソナが新着記事に出会えるようになります。
                ここではユーザーもペルソナと同じ記事を読めます。
            </p>

            {notice && <div className={styles.notice}>{notice}</div>}

            {showCreate && (
                <div className={styles.createForm}>
                    <div className={styles.formRow}>
                        <label>設置先の Building</label>
                        <select value={createBuildingId} onChange={e => setCreateBuildingId(e.target.value)}>
                            <option value="">選択してください</option>
                            {buildings.map(b => (
                                <option key={b.id} value={b.id}>{b.name}</option>
                            ))}
                        </select>
                    </div>
                    <div className={styles.formRow}>
                        <label>プリセット</label>
                        <select value={createPresetId} onChange={e => setCreatePresetId(e.target.value)}>
                            <option value="">使わない (空の施設を作る)</option>
                            {presets.map(p => (
                                <option key={p.id} value={p.id}>{p.name}（フィード{p.feed_count}本）</option>
                            ))}
                        </select>
                    </div>
                    {createPresetId ? (
                        (() => {
                            const preset = presets.find(p => p.id === createPresetId);
                            return preset ? (
                                <div className={styles.presetInfo}>
                                    {preset.description && <div>{preset.description}</div>}
                                    <div className={styles.presetFeeds}>
                                        収録フィード: {preset.feed_titles.filter(t => t).join(' / ') || '(なし)'}
                                    </div>
                                </div>
                            ) : null;
                        })()
                    ) : (
                        <>
                            <div className={styles.formRow}>
                                <label>施設の名前</label>
                                <input
                                    type="text"
                                    value={createName}
                                    onChange={e => setCreateName(e.target.value)}
                                    placeholder="例: 新聞スタンド"
                                />
                            </div>
                            <div className={styles.formRow}>
                                <label>説明 (任意)</label>
                                <input
                                    type="text"
                                    value={createDesc}
                                    onChange={e => setCreateDesc(e.target.value)}
                                    placeholder="ペルソナに見える施設の説明"
                                />
                            </div>
                        </>
                    )}
                    {createError && <div className={styles.errorText}>{createError}</div>}
                    <div className={styles.formActions}>
                        <button className={styles.btnSecondary} onClick={() => setShowCreate(false)}>キャンセル</button>
                        <button className={styles.btnPrimary} onClick={handleCreateFixture} disabled={creating}>
                            {creating ? '作成中...' : '作成'}
                        </button>
                    </div>
                </div>
            )}

            {loading ? (
                <div className={styles.empty}>読み込み中...</div>
            ) : fixtures.length === 0 ? (
                <div className={styles.empty}>
                    フィード施設はまだありません。「施設を追加」から作成できます。
                </div>
            ) : (
                <div className={styles.list}>
                    {fixtures.map(fixture => (
                        <div key={fixture.fixture_id} className={styles.card}>
                            <div className={styles.cardHeader}>
                                <div className={styles.cardTitle}>
                                    <span className={styles.fixtureName}>{fixture.name}</span>
                                    <span className={styles.buildingName}>
                                        {fixture.building_name || fixture.building_id}
                                    </span>
                                </div>
                                <button
                                    className={styles.btnSecondary}
                                    onClick={() => toggleItems(fixture.fixture_id)}
                                >
                                    <Newspaper size={14} />
                                    {openItemsFixtureId === fixture.fixture_id ? '記事を閉じる' : '記事を読む'}
                                </button>
                            </div>
                            {fixture.description && (
                                <div className={styles.cardDesc}>{fixture.description}</div>
                            )}

                            {/* 購読一覧 */}
                            {fixture.subscriptions.length === 0 ? (
                                <div className={styles.noSubs}>購読しているフィードはまだありません。</div>
                            ) : (
                                <div className={styles.subList}>
                                    {fixture.subscriptions.map(sub => {
                                        const unhealthy = sub.consecutive_failures >= FAILURE_WARN_THRESHOLD;
                                        return (
                                            <div
                                                key={sub.subscription_id}
                                                className={`${styles.subRow} ${unhealthy ? styles.subRowWarn : ''}`}
                                            >
                                                <div className={styles.subInfo}>
                                                    <div className={styles.subTitle}>
                                                        {sub.title || '(無題のフィード)'}
                                                        {unhealthy && (
                                                            <span className={styles.warnBadge}>
                                                                <AlertTriangle size={12} />
                                                                取得失敗 {sub.consecutive_failures} 回連続
                                                            </span>
                                                        )}
                                                    </div>
                                                    <div className={styles.subUrl}>{sub.feed_url}</div>
                                                    {unhealthy && sub.last_error && (
                                                        <div className={styles.subError}>{sub.last_error}</div>
                                                    )}
                                                    {sub.last_ok_at && (
                                                        <div className={styles.subMeta}>
                                                            最終取得成功: {formatDateTime(sub.last_ok_at)}
                                                        </div>
                                                    )}
                                                </div>
                                                <button
                                                    className={`${styles.iconBtn} ${styles.deleteBtn}`}
                                                    onClick={() => handleDeleteSubscription(sub)}
                                                    title="購読を削除"
                                                >
                                                    <Trash2 size={12} /> 削除
                                                </button>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}

                            {/* 購読追加 */}
                            <div className={styles.addSubRow}>
                                <input
                                    type="text"
                                    value={urlInputs[fixture.fixture_id] || ''}
                                    onChange={e => setUrlInputs(prev => ({ ...prev, [fixture.fixture_id]: e.target.value }))}
                                    onKeyDown={e => { if (e.key === 'Enter') handleAddSubscription(fixture.fixture_id); }}
                                    placeholder="サイトの URL を貼るだけで OK (フィードは自動で探します)"
                                />
                                <button
                                    className={styles.btnPrimary}
                                    onClick={() => handleAddSubscription(fixture.fixture_id)}
                                    disabled={addingFixtureId === fixture.fixture_id}
                                >
                                    {addingFixtureId === fixture.fixture_id ? '確認中...' : '購読を追加'}
                                </button>
                            </div>
                            {subErrors[fixture.fixture_id] && (
                                <div className={styles.errorText}>{subErrors[fixture.fixture_id]}</div>
                            )}
                            {(candidates[fixture.fixture_id] || []).length > 0 && (
                                <div className={styles.candidateBox}>
                                    <div className={styles.candidateHeader}>
                                        複数のフィードが見つかりました。購読するものを選んでください:
                                        <button
                                            className={styles.iconBtn}
                                            onClick={() => setCandidates(prev => ({ ...prev, [fixture.fixture_id]: [] }))}
                                        >
                                            <X size={12} />
                                        </button>
                                    </div>
                                    {(candidates[fixture.fixture_id] || []).map(c => (
                                        <button
                                            key={c.url}
                                            className={styles.candidateRow}
                                            onClick={() => postSubscription(fixture.fixture_id, c.url)}
                                            disabled={addingFixtureId === fixture.fixture_id}
                                        >
                                            <span className={styles.candidateTitle}>{c.title || '(無題)'}</span>
                                            <span className={styles.candidateUrl}>{c.url}</span>
                                        </button>
                                    ))}
                                </div>
                            )}

                            {/* 記事ビューア */}
                            {openItemsFixtureId === fixture.fixture_id && (
                                <div className={styles.itemsBox}>
                                    {itemsLoading ? (
                                        <div className={styles.empty}>読み込み中...</div>
                                    ) : items.length === 0 ? (
                                        <div className={styles.empty}>
                                            記事はまだ届いていません。「今すぐ取得」を押すと取得できます。
                                        </div>
                                    ) : (
                                        items.map((item, idx) => (
                                            <div key={idx} className={styles.itemRow}>
                                                <div className={styles.itemHeader}>
                                                    {item.link ? (
                                                        <a
                                                            href={item.link}
                                                            target="_blank"
                                                            rel="noopener noreferrer"
                                                            className={styles.itemTitle}
                                                        >
                                                            {item.title || '(無題の記事)'} <ExternalLink size={12} />
                                                        </a>
                                                    ) : (
                                                        <span className={styles.itemTitle}>{item.title || '(無題の記事)'}</span>
                                                    )}
                                                </div>
                                                <div className={styles.itemMeta}>
                                                    {item.subscription_title || '(提供元不明)'}
                                                    {item.published_at && ` ・ ${formatDateTime(item.published_at)}`}
                                                </div>
                                                {item.summary && (
                                                    <div className={styles.itemSummary}>{item.summary}</div>
                                                )}
                                            </div>
                                        ))
                                    )}
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
