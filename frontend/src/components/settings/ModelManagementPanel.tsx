import React, { useEffect, useState, useCallback } from 'react';
import { Plus, Edit2, Trash2, Copy, RefreshCw } from 'lucide-react';
import styles from './ModelManagementPanel.module.css';
import ModelEditorModal, { ModelEditorMode } from './ModelEditorModal';

interface ModelInfo {
    id: string;
    name: string;
    provider?: string | null;
    group?: string | null;
}

export default function ModelManagementPanel() {
    const [models, setModels] = useState<ModelInfo[]>([]);
    const [filter, setFilter] = useState('');
    const [loading, setLoading] = useState(false);
    const [editorOpen, setEditorOpen] = useState(false);
    const [editorMode, setEditorMode] = useState<ModelEditorMode>('create');
    const [editingKey, setEditingKey] = useState<string | undefined>();

    const loadModels = useCallback(async () => {
        setLoading(true);
        try {
            const res = await fetch('/api/config/models');
            if (res.ok) {
                const data = await res.json();
                setModels(data);
            }
        } catch (e) {
            console.error('Failed to load models', e);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        loadModels();
    }, [loadModels]);

    const openCreate = () => {
        setEditorMode('create');
        setEditingKey(undefined);
        setEditorOpen(true);
    };

    const openEdit = (key: string) => {
        setEditorMode('edit');
        setEditingKey(key);
        setEditorOpen(true);
    };

    const handleClone = async (m: ModelInfo) => {
        const newKey = prompt(`「${m.name}」を複製します。新しいキーを入力してください:`, `${m.id}-copy`);
        if (!newKey) return;
        try {
            const res = await fetch(`/api/config/models/${m.id}/clone`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ new_key: newKey }),
            });
            if (!res.ok) {
                const text = await res.text();
                alert(`複製に失敗しました: ${text}`);
                return;
            }
            loadModels();
        } catch (e) {
            alert(`複製に失敗しました: ${e}`);
        }
    };

    const handleDelete = async (m: ModelInfo) => {
        if (!confirm(`「${m.name}」(${m.id}) を削除しますか？\n\n※ user_data 配下のみ削除可能。builtin/expansion は削除できません（その場合は API がエラーを返します）。`)) return;
        try {
            const res = await fetch(`/api/config/models/${m.id}`, { method: 'DELETE' });
            if (!res.ok) {
                const text = await res.text();
                alert(`削除に失敗しました: ${text}`);
                return;
            }
            loadModels();
        } catch (e) {
            alert(`削除に失敗しました: ${e}`);
        }
    };

    const filtered = models.filter(m => {
        if (!filter) return true;
        const f = filter.toLowerCase();
        return m.name.toLowerCase().includes(f)
            || m.id.toLowerCase().includes(f)
            || (m.provider || '').toLowerCase().includes(f);
    });

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <h3>モデル ({models.length})</h3>
                <div className={styles.actions}>
                    <input
                        className={styles.filterInput}
                        type="text"
                        value={filter}
                        onChange={e => setFilter(e.target.value)}
                        placeholder="名前/ID/プロバイダで絞り込み..."
                    />
                    <button className={styles.btnSecondary} onClick={loadModels}>
                        <RefreshCw size={14} /> 再読み込み
                    </button>
                    <button className={styles.btnPrimary} onClick={openCreate}>
                        <Plus size={14} /> 新規追加
                    </button>
                </div>
            </div>

            {loading ? (
                <div className={styles.empty}>読み込み中...</div>
            ) : filtered.length === 0 ? (
                <div className={styles.empty}>
                    {models.length === 0
                        ? '利用可能なモデルがありません（API キー未設定の可能性）'
                        : 'フィルタに一致するモデルがありません'}
                </div>
            ) : (
                <div className={styles.list}>
                    {filtered.map(m => (
                        <div key={m.id} className={styles.row}>
                            <div className={styles.rowLeft}>
                                <div className={styles.rowName}>{m.name}</div>
                                <div className={styles.rowSub}>{m.id} ・ {m.provider || '?'}</div>
                            </div>
                            <div className={styles.rowActions}>
                                <button className={styles.iconBtn} onClick={() => openEdit(m.id)}>
                                    <Edit2 size={12} /> 編集
                                </button>
                                <button className={styles.iconBtn} onClick={() => handleClone(m)}>
                                    <Copy size={12} /> 複製
                                </button>
                                <button className={`${styles.iconBtn} ${styles.deleteBtn}`} onClick={() => handleDelete(m)}>
                                    <Trash2 size={12} /> 削除
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            <ModelEditorModal
                isOpen={editorOpen}
                mode={editorMode}
                modelKey={editingKey}
                onClose={() => setEditorOpen(false)}
                onSaved={loadModels}
            />
        </div>
    );
}
