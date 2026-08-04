import React, { useEffect, useState, useCallback } from 'react';
import { Plus, Edit2, Trash2, RefreshCw } from 'lucide-react';
import styles from './ProviderManagementPanel.module.css';
import ProviderEditorModal, { ProviderEditorMode } from './ProviderEditorModal';

interface ProviderInfo {
    id: string;
    display_name: string;
    protocol: string;
    base_url?: string | null;
    api_key_env?: string | null;
    builtin: boolean;
    api_key_configured?: boolean | null;
}

export default function ProviderManagementPanel() {
    const [providers, setProviders] = useState<ProviderInfo[]>([]);
    const [loading, setLoading] = useState(false);
    const [editorOpen, setEditorOpen] = useState(false);
    const [editorMode, setEditorMode] = useState<ProviderEditorMode>('create');
    const [editingId, setEditingId] = useState<string | undefined>();
    // Personas that have already spoken keep the connection they built, so a
    // change here does not reach them until restart. Saying so at the moment of
    // the change, rather than only in the docs, keeps it from looking like the
    // setting was ignored.
    const [notice, setNotice] = useState<string | null>(null);

    const loadProviders = useCallback(async () => {
        setLoading(true);
        try {
            const res = await fetch('/api/providers');
            if (res.ok) {
                const data = await res.json();
                setProviders(data);
            }
        } catch (e) {
            console.error('Failed to load providers', e);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        loadProviders();
    }, [loadProviders]);

    const openCreate = () => {
        setEditorMode('create');
        setEditingId(undefined);
        setEditorOpen(true);
    };

    const openEdit = (id: string) => {
        setEditorMode('edit');
        setEditingId(id);
        setEditorOpen(true);
    };

    const handleDelete = async (provider: ProviderInfo) => {
        // Check usage first
        let usingModels: string[] = [];
        try {
            const res = await fetch(`/api/providers/${provider.id}/models`);
            if (res.ok) {
                usingModels = await res.json();
            }
        } catch (e) {
            console.error('Failed to check provider usage', e);
        }

        const confirmMsg = usingModels.length > 0
            ? `「${provider.display_name}」を削除しますか？\n\nこのプロバイダを参照しているモデル (${usingModels.length}件):\n${usingModels.slice(0, 10).join('\n')}${usingModels.length > 10 ? '\n...' : ''}\n\n削除すると、これらのモデルは動かなくなります。`
            : `「${provider.display_name}」を削除しますか？`;

        if (!confirm(confirmMsg)) return;

        try {
            const res = await fetch(`/api/providers/${provider.id}`, { method: 'DELETE' });
            if (!res.ok) {
                const text = await res.text();
                alert(`削除に失敗しました: ${text}`);
                return;
            }
            setNotice('削除しました。SAIVerse を起動してから既にこのプロバイダで喋ったペルソナは、再起動するまで削除前の接続へ送り続けます。通常用と軽量用の接続は別々に作られるため、同じペルソナの中で新旧が混ざることもあります。');
            loadProviders();
        } catch (e) {
            alert(`削除に失敗しました: ${e}`);
        }
    };

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <h3>プロバイダ</h3>
                <div className={styles.actions}>
                    <button className={styles.btnSecondary} onClick={loadProviders}>
                        <RefreshCw size={14} /> 再読み込み
                    </button>
                    <button className={styles.btnPrimary} onClick={openCreate}>
                        <Plus size={14} /> 新規追加
                    </button>
                </div>
            </div>

            {notice && <div className={styles.notice}>{notice}</div>}

            {loading ? (
                <div className={styles.empty}>読み込み中...</div>
            ) : providers.length === 0 ? (
                <div className={styles.empty}>プロバイダがありません</div>
            ) : (
                <div className={styles.list}>
                    {providers.map(p => (
                        <div key={p.id} className={styles.row}>
                            <div className={styles.rowLeft}>
                                <div className={styles.rowName}>
                                    {p.display_name}
                                    <span className={`${styles.badge} ${p.builtin ? '' : styles.badgeUser}`}>
                                        {p.builtin ? 'builtin' : 'user_data'}
                                    </span>
                                    {p.api_key_env && (
                                        <span className={`${styles.badge} ${p.api_key_configured ? styles.badgeKeyOk : styles.badgeKeyMissing}`}>
                                            {p.api_key_configured ? 'KEY OK' : 'KEY 未設定'}
                                        </span>
                                    )}
                                </div>
                                <div className={styles.rowSub}>
                                    {p.id} ・ {p.protocol}
                                    {p.base_url && ` ・ ${p.base_url}`}
                                </div>
                            </div>
                            <div className={styles.rowActions}>
                                <button className={styles.iconBtn} onClick={() => openEdit(p.id)}>
                                    <Edit2 size={12} /> {p.builtin ? '上書き編集' : '編集'}
                                </button>
                                <button
                                    className={`${styles.iconBtn} ${styles.deleteBtn}`}
                                    onClick={() => handleDelete(p)}
                                    disabled={p.builtin}
                                    title={p.builtin ? 'builtin は削除できません' : '削除'}
                                >
                                    <Trash2 size={12} /> 削除
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            <ProviderEditorModal
                isOpen={editorOpen}
                mode={editorMode}
                providerId={editingId}
                onClose={() => setEditorOpen(false)}
                onSaved={() => {
                    setNotice('保存しました。SAIVerse を起動してから既にこのプロバイダで喋ったペルソナは、再起動するまで変更前の接続を使い続けます。通常用と軽量用の接続は別々に作られるため、同じペルソナの中で新旧が混ざることもあります。');
                    loadProviders();
                }}
            />
        </div>
    );
}
