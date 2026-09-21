
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useEffect, useState, useCallback } from 'react';
import { Plus, Edit2, Trash2, Copy, RefreshCw } from 'lucide-react';
import styles from './ModelManagementPanel.module.css';
import ModelEditorModal, { ModelEditorMode, ModelCloneSource } from './ModelEditorModal';

interface ModelInfo {
    id: string;
    name: string;
    provider?: string | null;
    group?: string | null;
}

export default function ModelManagementPanel() {
    useLocale();
    const [models, setModels] = useState<ModelInfo[]>([]);
    const [filter, setFilter] = useState('');
    const [loading, setLoading] = useState(false);
    const [editorOpen, setEditorOpen] = useState(false);
    const [editorMode, setEditorMode] = useState<ModelEditorMode>('create');
    const [editingKey, setEditingKey] = useState<string | undefined>();
    const [cloneSource, setCloneSource] = useState<ModelCloneSource | undefined>();

    const loadModels = useCallback(async () => {
        setLoading(true);
        try {
            const res = await apiFetch('/api/config/models');
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
        setCloneSource(undefined);
        setEditorOpen(true);
    };

    const openEdit = (key: string) => {
        setEditorMode('edit');
        setEditingKey(key);
        setEditorOpen(true);
    };

    const handleClone = async (m: ModelInfo) => {
        try {
            const res = await apiFetch(`/api/config/models/${m.id}`);
            if (!res.ok) {
                alert(uiText("components.settings.ModelManagementPanel.text001", { p1: res.status }));
                return;
            }
            const data = await res.json();
            setEditorMode('create');
            setEditingKey(undefined);
            setCloneSource({ key: m.id, config: data.config ?? {} });
            setEditorOpen(true);
        } catch (e) {
            alert(uiText("components.settings.ModelManagementPanel.text002", { p1: e }));
        }
    };

    const handleDelete = async (m: ModelInfo) => {
        if (!confirm(uiText("components.settings.ModelManagementPanel.text003", { p1: m.name, p2: m.id }))) return;
        try {
            const res = await apiFetch(`/api/config/models/${m.id}`, { method: 'DELETE' });
            if (!res.ok) {
                const text = await res.text();
                alert(uiText("components.settings.ModelManagementPanel.text004", { p1: text }));
                return;
            }
            // 削除で決め直したときに、新しい設定に切り替えられなかったペルソナの知らせ
            const data = await res.json().catch(() => null);
            const notices: string[] = Array.isArray(data?.notices) ? data.notices : [];
            if (notices.length > 0) {
                alert(notices.join('\n\n'));
            }
            loadModels();
        } catch (e) {
            alert(uiText("components.settings.ModelManagementPanel.text005", { p1: e }));
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
                <h3 data-i18n="components.settings.ModelManagementPanel.text006">{uiText("components.settings.ModelManagementPanel.text006")}{models.length})</h3>
                <div className={styles.actions}>
                    <input data-i18n="components.settings.ModelManagementPanel.text007"
                        className={styles.filterInput}
                        type="text"
                        value={filter}
                        onChange={e => setFilter(e.target.value)}
                        placeholder={uiText("components.settings.ModelManagementPanel.text007")}
                    />
                    <button data-i18n="components.settings.ModelManagementPanel.text008" className={styles.btnSecondary} onClick={loadModels}>
                        <RefreshCw size={14} />{uiText("components.settings.ModelManagementPanel.text008")}</button>
                    <button data-i18n="components.settings.ModelManagementPanel.text009" className={styles.btnPrimary} onClick={openCreate}>
                        <Plus size={14} />{uiText("components.settings.ModelManagementPanel.text009")}</button>
                </div>
            </div>

            {loading ? (
                <div data-i18n="components.settings.ModelManagementPanel.text010" className={styles.empty}>{uiText("components.settings.ModelManagementPanel.text010")}</div>
            ) : filtered.length === 0 ? (
                <div data-i18n="components.settings.ModelManagementPanel.text011 components.settings.ModelManagementPanel.text012" className={styles.empty}>
                    {models.length === 0
                        ? uiText("components.settings.ModelManagementPanel.text011")
                        : uiText("components.settings.ModelManagementPanel.text012")}
                </div>
            ) : (
                <div className={styles.list}>
                    {filtered.map(m => (
                        <div key={m.id} className={styles.row}>
                            <div className={styles.rowLeft}>
                                <div className={styles.rowName}>{m.name}</div>
                                <div data-i18n="components.settings.ModelManagementPanel.text013" className={styles.rowSub}>{m.id}{uiText("components.settings.ModelManagementPanel.text013")}{m.provider || '?'}</div>
                            </div>
                            <div className={styles.rowActions}>
                                <button data-i18n="components.settings.ModelManagementPanel.text014" className={styles.iconBtn} onClick={() => openEdit(m.id)}>
                                    <Edit2 size={12} />{uiText("components.settings.ModelManagementPanel.text014")}</button>
                                <button data-i18n="components.settings.ModelManagementPanel.text015" className={styles.iconBtn} onClick={() => handleClone(m)}>
                                    <Copy size={12} />{uiText("components.settings.ModelManagementPanel.text015")}</button>
                                <button data-i18n="components.settings.ModelManagementPanel.text016" className={`${styles.iconBtn} ${styles.deleteBtn}`} onClick={() => handleDelete(m)}>
                                    <Trash2 size={12} />{uiText("components.settings.ModelManagementPanel.text016")}</button>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            <ModelEditorModal
                isOpen={editorOpen}
                mode={editorMode}
                modelKey={editingKey}
                cloneSource={cloneSource}
                onClose={() => setEditorOpen(false)}
                onSaved={loadModels}
            />
        </div>
    );
}
