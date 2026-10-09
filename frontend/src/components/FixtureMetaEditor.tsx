import { apiFetch } from '@/i18n/api';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { useState } from 'react';
import { Save, Settings, Trash2, XCircle } from 'lucide-react';
// 見た目と振る舞いはアイテムのメタ情報編集 (ItemModal) と揃える — 同じ CSS を使う
import styles from './ItemModal.module.css';

/** 設置物のメタ情報編集が必要とする最小限の形 (RightSidebar の Fixture と互換)。 */
export interface FixtureMetaTarget {
    id: string;
    name: string;
    description: string;
    type: string;
}

/** 設置物モーダルのヘッダーに置く「メタ情報を編集」ボタン (ItemModal の歯車と同じ)。 */
export function FixtureMetaEditButton({ onClick }: { onClick: () => void }) {
    useLocale();
    return (
        <button data-i18n="components.FixtureMetaEditor.text001"
            className={styles.metaEditBtn}
            onClick={onClick}
            title={uiText("components.FixtureMetaEditor.text001")}
            aria-label={uiText("components.FixtureMetaEditor.text001")}
        >
            <Settings size={18} />
        </button>
    );
}

interface FixtureMetaEditorProps {
    fixture: FixtureMetaTarget;
    /** 編集をやめる (何も変えずにフォームを閉じる)。 */
    onCancel: () => void;
    /** 保存・削除のどちらかが成功した。親は部屋の表示を取り直してモーダルを閉じる
     * (ItemModal の onItemUpdated と同じ扱い)。 */
    onChanged: () => void;
}

function errorDetail(data: unknown): string | null {
    // FastAPI の 422 (入力検証) は detail が配列で届くので、文字列のときだけ使う
    if (data && typeof data === 'object' && typeof (data as { detail?: unknown }).detail === 'string') {
        return (data as { detail: string }).detail;
    }
    return null;
}

/**
 * 設置物の名前・説明文の編集と削除のフォーム。汎用の設置物モーダルと
 * フィードスタンドのモーダルの両方がこれを使う。
 *
 * 削除の手順はアイテム (ItemModal.handleDelete) と同じ: ブラウザの確認
 * ダイアログ → DELETE → 親へ通知。
 */
export default function FixtureMetaEditor({ fixture, onCancel, onChanged }: FixtureMetaEditorProps) {
    useLocale();
    const [editName, setEditName] = useState(fixture.name);
    const [editDescription, setEditDescription] = useState(fixture.description || '');
    const [isSaving, setIsSaving] = useState(false);
    const [isDeleting, setIsDeleting] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const busy = isSaving || isDeleting;

    const handleSave = async () => {
        setIsSaving(true);
        setError(null);
        try {
            const res = await apiFetch(`/api/observer/fixture/${encodeURIComponent(fixture.id)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: editName, description: editDescription }),
            });
            if (!res.ok) {
                const data = await res.json().catch(() => null);
                throw new Error(errorDetail(data) ?? uiText("components.FixtureMetaEditor.text011"));
            }
            onChanged();
        } catch (err) {
            console.error(err);
            setError(err instanceof Error ? err.message : uiText("components.FixtureMetaEditor.text011"));
        } finally {
            setIsSaving(false);
        }
    };

    const handleDelete = async () => {
        // フィードスタンドは購読と取得済み記事も道連れに消えることを確認文で伝える
        const message = fixture.type === 'feed_stand'
            ? uiText("components.FixtureMetaEditor.text010")
            : uiText("components.FixtureMetaEditor.text009");
        if (!window.confirm(message)) return;

        setIsDeleting(true);
        setError(null);
        try {
            const res = await apiFetch(`/api/observer/fixture/${encodeURIComponent(fixture.id)}`, {
                method: 'DELETE',
            });
            if (!res.ok) {
                const data = await res.json().catch(() => null);
                throw new Error(errorDetail(data) ?? uiText("components.FixtureMetaEditor.text012"));
            }
            onChanged();
        } catch (err) {
            console.error(err);
            setError(err instanceof Error ? err.message : uiText("components.FixtureMetaEditor.text012"));
        } finally {
            setIsDeleting(false);
        }
    };

    const nameId = `fixture-name-${fixture.id}`;
    const descriptionId = `fixture-description-${fixture.id}`;

    return (
        <div className={styles.metaEditSection}>
            <div className={styles.metaEditForm}>
                <div className={styles.formGroup}>
                    <label data-i18n="components.FixtureMetaEditor.text002" htmlFor={nameId}>{uiText("components.FixtureMetaEditor.text002")}</label>
                    <input
                        id={nameId}
                        type="text"
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                        className={styles.input}
                        maxLength={255}
                        disabled={busy}
                    />
                </div>
                <div className={styles.formGroup}>
                    <label data-i18n="components.FixtureMetaEditor.text003" htmlFor={descriptionId}>{uiText("components.FixtureMetaEditor.text003")}</label>
                    <textarea
                        id={descriptionId}
                        value={editDescription}
                        onChange={(e) => setEditDescription(e.target.value)}
                        className={styles.descriptionTextarea}
                        rows={3}
                        maxLength={2048}
                        disabled={busy}
                    />
                </div>
                <div className={styles.metaEditActions}>
                    <button
                        className={`${styles.toggleBtn} ${styles.deleteBtn}`}
                        onClick={handleDelete}
                        disabled={busy}
                    >
                        <Trash2 size={16} />
                        <span data-i18n="components.FixtureMetaEditor.text007 components.FixtureMetaEditor.text008">{isDeleting ? uiText("components.FixtureMetaEditor.text007") : uiText("components.FixtureMetaEditor.text008")}</span>
                    </button>
                    <button
                        className={`${styles.toggleBtn} ${styles.saveBtn}`}
                        onClick={handleSave}
                        disabled={busy}
                    >
                        <Save size={16} />
                        <span data-i18n="components.FixtureMetaEditor.text004 components.FixtureMetaEditor.text005">{isSaving ? uiText("components.FixtureMetaEditor.text004") : uiText("components.FixtureMetaEditor.text005")}</span>
                    </button>
                    <button
                        className={`${styles.toggleBtn} ${styles.cancelBtn}`}
                        onClick={onCancel}
                        disabled={busy}
                    >
                        <XCircle size={16} />
                        <span data-i18n="components.FixtureMetaEditor.text006">{uiText("components.FixtureMetaEditor.text006")}</span>
                    </button>
                </div>
            </div>
            {error && <div className={styles.error}>{error}</div>}
        </div>
    );
}
