
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
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
    return d.toLocaleString(getFormatLocale(), {
        year: 'numeric', month: 'numeric', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
    });
}

export default function FeedManagementPanel() {
    useLocale();
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
            const res = await apiFetch('/api/feeds/fixtures');
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
                apiFetch('/api/feeds/presets'),
                apiFetch('/api/user/buildings'),
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
            setCreateError(uiText("components.settings.FeedManagementPanel.text001"));
            return;
        }
        if (!createPresetId && !createName.trim()) {
            setCreateError(uiText("components.settings.FeedManagementPanel.text002"));
            return;
        }
        setCreating(true);
        try {
            const body = createPresetId
                ? { building_id: createBuildingId, preset_id: createPresetId }
                : { building_id: createBuildingId, name: createName.trim(), description: createDesc.trim() };
            const res = await apiFetch('/api/feeds/fixtures', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) {
                const data = await res.json().catch(() => null);
                setCreateError(data?.detail || uiText("components.settings.FeedManagementPanel.text003", { p1: res.status }));
                return;
            }
            setShowCreate(false);
            setCreatePresetId('');
            setCreateName('');
            setCreateDesc('');
            await loadFixtures();
        } catch (e) {
            setCreateError(uiText("components.settings.FeedManagementPanel.text004", { p1: e }));
        } finally {
            setCreating(false);
        }
    };

    const postSubscription = async (fixtureId: string, url: string) => {
        setSubErrors(prev => ({ ...prev, [fixtureId]: '' }));
        setAddingFixtureId(fixtureId);
        try {
            const res = await apiFetch('/api/feeds/subscriptions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ fixture_id: fixtureId, url }),
            });
            const data = await res.json().catch(() => null);
            if (!res.ok) {
                setSubErrors(prev => ({ ...prev, [fixtureId]: data?.detail || uiText("components.settings.FeedManagementPanel.text005", { p1: res.status }) }));
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
            setSubErrors(prev => ({ ...prev, [fixtureId]: uiText("components.settings.FeedManagementPanel.text006", { p1: e }) }));
        } finally {
            setAddingFixtureId(null);
        }
    };

    const handleAddSubscription = (fixtureId: string) => {
        const url = (urlInputs[fixtureId] || '').trim();
        if (!url) {
            setSubErrors(prev => ({ ...prev, [fixtureId]: uiText("components.settings.FeedManagementPanel.text007") }));
            return;
        }
        setCandidates(prev => ({ ...prev, [fixtureId]: [] }));
        postSubscription(fixtureId, url);
    };

    const handleDeleteSubscription = async (sub: FeedSubscriptionInfo) => {
        const label = sub.title || sub.feed_url;
        if (!confirm(uiText("components.settings.FeedManagementPanel.text008", { p1: label }))) return;
        try {
            const res = await apiFetch(`/api/feeds/subscriptions/${sub.subscription_id}`, { method: 'DELETE' });
            if (!res.ok) {
                const data = await res.json().catch(() => null);
                alert(uiText("components.settings.FeedManagementPanel.text009", { p1: data?.detail || res.status }));
                return;
            }
            await loadFixtures();
        } catch (e) {
            alert(uiText("components.settings.FeedManagementPanel.text010", { p1: e }));
        }
    };

    const handleFetchNow = async () => {
        setNotice(null);
        try {
            const res = await apiFetch('/api/feeds/fetch', { method: 'POST' });
            if (res.ok) {
                setNotice(uiText("components.settings.FeedManagementPanel.text011"));
            } else {
                // 409 (取得処理が既に実行中です) 等はサーバーの detail をそのまま見せる
                const data = await res.json().catch(() => null);
                setNotice(data?.detail || uiText("components.settings.FeedManagementPanel.text012", { p1: res.status }));
            }
        } catch (e) {
            setNotice(uiText("components.settings.FeedManagementPanel.text013", { p1: e }));
        }
    };

    const loadItems = useCallback(async (fixtureId: string) => {
        setItemsLoading(true);
        try {
            const res = await apiFetch(`/api/feeds/items?fixture_id=${encodeURIComponent(fixtureId)}&limit=50`);
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
                <h3 data-i18n="components.settings.FeedManagementPanel.text014"><Rss size={18} />{uiText("components.settings.FeedManagementPanel.text014")}</h3>
                <div className={styles.actions}>
                    <button data-i18n="components.settings.FeedManagementPanel.text015" className={styles.btnSecondary} onClick={handleFetchNow}>
                        <DownloadCloud size={14} />{uiText("components.settings.FeedManagementPanel.text015")}</button>
                    <button data-i18n="components.settings.FeedManagementPanel.text016" className={styles.btnSecondary} onClick={loadFixtures}>
                        <RefreshCw size={14} />{uiText("components.settings.FeedManagementPanel.text016")}</button>
                    <button data-i18n="components.settings.FeedManagementPanel.text017" className={styles.btnPrimary} onClick={() => setShowCreate(v => !v)}>
                        <Plus size={14} />{uiText("components.settings.FeedManagementPanel.text017")}</button>
                </div>
            </div>

            <p data-i18n="components.settings.FeedManagementPanel.text018" className={styles.intro}>{uiText("components.settings.FeedManagementPanel.text018")}</p>

            {notice && <div className={styles.notice}>{notice}</div>}

            {showCreate && (
                <div className={styles.createForm}>
                    <div className={styles.formRow}>
                        <label data-i18n="components.settings.FeedManagementPanel.text019">{uiText("components.settings.FeedManagementPanel.text019")}</label>
                        <select value={createBuildingId} onChange={e => setCreateBuildingId(e.target.value)}>
                            <option data-i18n="components.settings.FeedManagementPanel.text020" value="">{uiText("components.settings.FeedManagementPanel.text020")}</option>
                            {buildings.map(b => (
                                <option key={b.id} value={b.id}>{b.name}</option>
                            ))}
                        </select>
                    </div>
                    <div className={styles.formRow}>
                        <label data-i18n="components.settings.FeedManagementPanel.text021">{uiText("components.settings.FeedManagementPanel.text021")}</label>
                        <select value={createPresetId} onChange={e => setCreatePresetId(e.target.value)}>
                            <option data-i18n="components.settings.FeedManagementPanel.text022" value="">{uiText("components.settings.FeedManagementPanel.text022")}</option>
                            {presets.map(p => (
                                <option data-i18n="components.settings.FeedManagementPanel.text023 components.settings.FeedManagementPanel.text024" key={p.id} value={p.id}>{p.name}{uiText("components.settings.FeedManagementPanel.text023")}{p.feed_count}{uiText("components.settings.FeedManagementPanel.text024")}</option>
                            ))}
                        </select>
                    </div>
                    {createPresetId ? (
                        (() => {
                            const preset = presets.find(p => p.id === createPresetId);
                            return preset ? (
                                <div className={styles.presetInfo}>
                                    {preset.description && <div>{preset.description}</div>}
                                    <div data-i18n="components.settings.FeedManagementPanel.text025 components.settings.FeedManagementPanel.text026" className={styles.presetFeeds}>{uiText("components.settings.FeedManagementPanel.text025")}{preset.feed_titles.filter(t => t).join(' / ') || uiText("components.settings.FeedManagementPanel.text026")}
                                    </div>
                                </div>
                            ) : null;
                        })()
                    ) : (
                        <>
                            <div className={styles.formRow}>
                                <label data-i18n="components.settings.FeedManagementPanel.text027">{uiText("components.settings.FeedManagementPanel.text027")}</label>
                                <input data-i18n="components.settings.FeedManagementPanel.text028"
                                    type="text"
                                    value={createName}
                                    onChange={e => setCreateName(e.target.value)}
                                    placeholder={uiText("components.settings.FeedManagementPanel.text028")}
                                />
                            </div>
                            <div className={styles.formRow}>
                                <label data-i18n="components.settings.FeedManagementPanel.text029">{uiText("components.settings.FeedManagementPanel.text029")}</label>
                                <input data-i18n="components.settings.FeedManagementPanel.text030"
                                    type="text"
                                    value={createDesc}
                                    onChange={e => setCreateDesc(e.target.value)}
                                    placeholder={uiText("components.settings.FeedManagementPanel.text030")}
                                />
                            </div>
                        </>
                    )}
                    {createError && <div className={styles.errorText}>{createError}</div>}
                    <div className={styles.formActions}>
                        <button data-i18n="components.settings.FeedManagementPanel.text031" className={styles.btnSecondary} onClick={() => setShowCreate(false)}>{uiText("components.settings.FeedManagementPanel.text031")}</button>
                        <button data-i18n="components.settings.FeedManagementPanel.text032 components.settings.FeedManagementPanel.text033" className={styles.btnPrimary} onClick={handleCreateFixture} disabled={creating}>
                            {creating ? uiText("components.settings.FeedManagementPanel.text032") : uiText("components.settings.FeedManagementPanel.text033")}
                        </button>
                    </div>
                </div>
            )}

            {loading ? (
                <div data-i18n="components.settings.FeedManagementPanel.text034" className={styles.empty}>{uiText("components.settings.FeedManagementPanel.text034")}</div>
            ) : fixtures.length === 0 ? (
                <div data-i18n="components.settings.FeedManagementPanel.text035" className={styles.empty}>{uiText("components.settings.FeedManagementPanel.text035")}</div>
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
                                <button data-i18n="components.settings.FeedManagementPanel.text036 components.settings.FeedManagementPanel.text037"
                                    className={styles.btnSecondary}
                                    onClick={() => toggleItems(fixture.fixture_id)}
                                >
                                    <Newspaper size={14} />
                                    {openItemsFixtureId === fixture.fixture_id ? uiText("components.settings.FeedManagementPanel.text036") : uiText("components.settings.FeedManagementPanel.text037")}
                                </button>
                            </div>
                            {fixture.description && (
                                <div className={styles.cardDesc}>{fixture.description}</div>
                            )}

                            {/* 購読一覧 */}
                            {fixture.subscriptions.length === 0 ? (
                                <div data-i18n="components.settings.FeedManagementPanel.text038" className={styles.noSubs}>{uiText("components.settings.FeedManagementPanel.text038")}</div>
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
                                                    <div data-i18n="components.settings.FeedManagementPanel.text039" className={styles.subTitle}>
                                                        {sub.title || uiText("components.settings.FeedManagementPanel.text039")}
                                                        {unhealthy && (
                                                            <span data-i18n="components.settings.FeedManagementPanel.text040 components.settings.FeedManagementPanel.text041" className={styles.warnBadge}>
                                                                <AlertTriangle size={12} />{uiText("components.settings.FeedManagementPanel.text040")}{sub.consecutive_failures}{uiText("components.settings.FeedManagementPanel.text041")}</span>
                                                        )}
                                                    </div>
                                                    <div className={styles.subUrl}>{sub.feed_url}</div>
                                                    {unhealthy && sub.last_error && (
                                                        <div className={styles.subError}>{sub.last_error}</div>
                                                    )}
                                                    {sub.last_ok_at && (
                                                        <div data-i18n="components.settings.FeedManagementPanel.text042" className={styles.subMeta}>{uiText("components.settings.FeedManagementPanel.text042")}{formatDateTime(sub.last_ok_at)}
                                                        </div>
                                                    )}
                                                </div>
                                                <button data-i18n="components.settings.FeedManagementPanel.text043 components.settings.FeedManagementPanel.text044"
                                                    className={`${styles.iconBtn} ${styles.deleteBtn}`}
                                                    onClick={() => handleDeleteSubscription(sub)}
                                                    title={uiText("components.settings.FeedManagementPanel.text043")}
                                                >
                                                    <Trash2 size={12} />{uiText("components.settings.FeedManagementPanel.text044")}</button>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}

                            {/* 購読追加 */}
                            <div className={styles.addSubRow}>
                                <input data-i18n="components.settings.FeedManagementPanel.text045"
                                    type="text"
                                    value={urlInputs[fixture.fixture_id] || ''}
                                    onChange={e => setUrlInputs(prev => ({ ...prev, [fixture.fixture_id]: e.target.value }))}
                                    onKeyDown={e => { if (e.key === 'Enter') handleAddSubscription(fixture.fixture_id); }}
                                    placeholder={uiText("components.settings.FeedManagementPanel.text045")}
                                />
                                <button data-i18n="components.settings.FeedManagementPanel.text046 components.settings.FeedManagementPanel.text047"
                                    className={styles.btnPrimary}
                                    onClick={() => handleAddSubscription(fixture.fixture_id)}
                                    disabled={addingFixtureId === fixture.fixture_id}
                                >
                                    {addingFixtureId === fixture.fixture_id ? uiText("components.settings.FeedManagementPanel.text046") : uiText("components.settings.FeedManagementPanel.text047")}
                                </button>
                            </div>
                            {subErrors[fixture.fixture_id] && (
                                <div className={styles.errorText}>{subErrors[fixture.fixture_id]}</div>
                            )}
                            {(candidates[fixture.fixture_id] || []).length > 0 && (
                                <div className={styles.candidateBox}>
                                    <div data-i18n="components.settings.FeedManagementPanel.text048" className={styles.candidateHeader}>{uiText("components.settings.FeedManagementPanel.text048")}<button
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
                                            <span data-i18n="components.settings.FeedManagementPanel.text049" className={styles.candidateTitle}>{c.title || uiText("components.settings.FeedManagementPanel.text049")}</span>
                                            <span className={styles.candidateUrl}>{c.url}</span>
                                        </button>
                                    ))}
                                </div>
                            )}

                            {/* 記事ビューア */}
                            {openItemsFixtureId === fixture.fixture_id && (
                                <div className={styles.itemsBox}>
                                    {itemsLoading ? (
                                        <div data-i18n="components.settings.FeedManagementPanel.text050" className={styles.empty}>{uiText("components.settings.FeedManagementPanel.text050")}</div>
                                    ) : items.length === 0 ? (
                                        <div data-i18n="components.settings.FeedManagementPanel.text051" className={styles.empty}>{uiText("components.settings.FeedManagementPanel.text051")}</div>
                                    ) : (
                                        items.map((item, idx) => (
                                            <div key={idx} className={styles.itemRow}>
                                                <div className={styles.itemHeader}>
                                                    {item.link ? (
                                                        <a data-i18n="components.settings.FeedManagementPanel.text052"
                                                            href={item.link}
                                                            target="_blank"
                                                            rel="noopener noreferrer"
                                                            className={styles.itemTitle}
                                                        >
                                                            {item.title || uiText("components.settings.FeedManagementPanel.text052")} <ExternalLink size={12} />
                                                        </a>
                                                    ) : (
                                                        <span data-i18n="components.settings.FeedManagementPanel.text053" className={styles.itemTitle}>{item.title || uiText("components.settings.FeedManagementPanel.text053")}</span>
                                                    )}
                                                </div>
                                                <div data-i18n="components.settings.FeedManagementPanel.text054 components.settings.FeedManagementPanel.text055" className={styles.itemMeta}>
                                                    {item.subscription_title || uiText("components.settings.FeedManagementPanel.text054")}
                                                    {item.published_at && uiText("components.settings.FeedManagementPanel.text055", { p1: formatDateTime(item.published_at) })}
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
