import { useState, useEffect, useCallback } from 'react';
import { X, FileText, Code2, Pencil, Save, XCircle, Settings, ArrowRightLeft } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import styles from './ItemModal.module.css';
import ModalOverlay from './common/ModalOverlay';

interface Item {
    id: string;
    name: string;
    description?: string;
    type: string;
}

interface Building {
    id: string;
    name: string;
}

interface ItemDetails {
    ITEM_ID: string;
    NAME: string;
    TYPE: string;
    DESCRIPTION: string;
    FILE_PATH: string;
    STATE_JSON: string;
    OWNER_KIND: string;
    OWNER_ID: string;
}

interface ItemModalProps {
    isOpen: boolean;
    onClose: () => void;
    item: Item | null;
    onItemUpdated?: () => void;  // Callback when item is updated
}

export default function ItemModal({ isOpen, onClose, item, onItemUpdated }: ItemModalProps) {
    const [content, setContent] = useState<string | null>(null);
    const [editContent, setEditContent] = useState<string>('');
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [isMarkdown, setIsMarkdown] = useState(true);
    const [isEditing, setIsEditing] = useState(false);

    // Meta editing state
    const [isMetaEditing, setIsMetaEditing] = useState(false);
    const [itemDetails, setItemDetails] = useState<ItemDetails | null>(null);
    const [editName, setEditName] = useState('');
    const [editDescription, setEditDescription] = useState('');
    const [editOwnerKind, setEditOwnerKind] = useState('');
    const [editOwnerId, setEditOwnerId] = useState('');
    const [buildings, setBuildings] = useState<Building[]>([]);
    const [isLoadingBuildings, setIsLoadingBuildings] = useState(false);
    const [isSavingMeta, setIsSavingMeta] = useState(false);

    // Load buildings list
    const loadBuildings = useCallback(async () => {
        if (buildings.length > 0) return; // Already loaded
        setIsLoadingBuildings(true);
        try {
            const res = await fetch('/api/user/buildings');
            if (res.ok) {
                const data = await res.json();
                setBuildings(data.buildings || []);
            }
        } catch (err) {
            console.error('Failed to load buildings:', err);
        } finally {
            setIsLoadingBuildings(false);
        }
    }, [buildings.length]);

    // Load item details for meta editing
    const loadItemDetails = useCallback(async (itemId: string) => {
        try {
            const res = await fetch(`/api/world/items/${itemId}`);
            if (res.ok) {
                const data: ItemDetails = await res.json();
                setItemDetails(data);
                setEditName(data.NAME);
                setEditDescription(data.DESCRIPTION || '');
                setEditOwnerKind(data.OWNER_KIND || 'world');
                setEditOwnerId(data.OWNER_ID || '');
            }
        } catch (err) {
            console.error('Failed to load item details:', err);
        }
    }, []);

    useEffect(() => {
        if (isOpen && item && item.type === 'document') {
            setIsLoading(true);
            setError(null);
            setIsEditing(false);
            setIsMetaEditing(false);
            fetch(`/api/info/item/${item.id}`)
                .then(async res => {
                    if (!res.ok) throw new Error("Failed to load content");
                    const data = await res.json();
                    setContent(data.content);
                    setEditContent(data.content);
                })
                .catch(err => {
                    console.error(err);
                    setError("コンテンツの読み込みに失敗しました");
                })
                .finally(() => setIsLoading(false));
        } else {
            setContent(null);
            setEditContent('');
            setError(null);
            setIsEditing(false);
            setIsMetaEditing(false);
        }
        // Reset meta editing state when modal opens/closes
        setItemDetails(null);
    }, [isOpen, item]);

    const handleStartEdit = () => {
        setEditContent(content || '');
        setIsEditing(true);
        setIsMarkdown(false); // Switch to plain text mode for editing
    };

    const handleCancelEdit = () => {
        setEditContent(content || '');
        setIsEditing(false);
    };

    const handleSave = async () => {
        if (!item) return;

        setIsSaving(true);
        setError(null);

        try {
            const res = await fetch(`/api/info/item/${item.id}/content`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: editContent })
            });

            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to save');
            }

            setContent(editContent);
            setIsEditing(false);
        } catch (err) {
            console.error(err);
            setError(err instanceof Error ? err.message : "保存に失敗しました");
        } finally {
            setIsSaving(false);
        }
    };

    // Meta editing handlers
    const handleStartMetaEdit = async () => {
        if (!item) return;
        await Promise.all([
            loadItemDetails(item.id),
            loadBuildings()
        ]);
        setIsMetaEditing(true);
        setIsEditing(false);
    };

    const handleCancelMetaEdit = () => {
        if (itemDetails) {
            setEditName(itemDetails.NAME);
            setEditDescription(itemDetails.DESCRIPTION || '');
            setEditOwnerKind(itemDetails.OWNER_KIND || 'world');
            setEditOwnerId(itemDetails.OWNER_ID || '');
        }
        setIsMetaEditing(false);
    };

    const handleSaveMeta = async () => {
        if (!item || !itemDetails) return;

        setIsSavingMeta(true);
        setError(null);

        try {
            const res = await fetch(`/api/world/items/${item.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: editName,
                    item_type: itemDetails.TYPE,
                    description: editDescription,
                    owner_kind: editOwnerKind,
                    owner_id: editOwnerId || null,
                    state_json: itemDetails.STATE_JSON || null,
                    file_path: itemDetails.FILE_PATH || null,
                })
            });

            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to save');
            }

            // Update local item details
            setItemDetails({
                ...itemDetails,
                NAME: editName,
                DESCRIPTION: editDescription,
                OWNER_KIND: editOwnerKind,
                OWNER_ID: editOwnerId,
            });
            setIsMetaEditing(false);

            // Notify parent to refresh
            if (onItemUpdated) {
                onItemUpdated();
            }
        } catch (err) {
            console.error(err);
            setError(err instanceof Error ? err.message : "保存に失敗しました");
        } finally {
            setIsSavingMeta(false);
        }
    };

    // Get current building name
    const getCurrentBuildingName = () => {
        if (editOwnerKind !== 'building') return null;
        const building = buildings.find(b => b.id === editOwnerId);
        return building?.name || editOwnerId;
    };

    if (!isOpen || !item) return null;

    // Display name (use edited name if meta editing, otherwise item name)
    const displayName = isMetaEditing ? editName : (itemDetails?.NAME || item.name);
    const displayDescription = isMetaEditing ? editDescription : (itemDetails?.DESCRIPTION || item.description);

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2>{displayName}</h2>
                    <div className={styles.headerActions}>
                        {!isMetaEditing && (
                            <button
                                className={styles.metaEditBtn}
                                onClick={handleStartMetaEdit}
                                title="メタ情報を編集"
                            >
                                <Settings size={20} />
                            </button>
                        )}
                        <button className={styles.closeBtn} onClick={onClose}>
                            <X size={24} />
                        </button>
                    </div>
                </div>

                {isMetaEditing ? (
                    <div className={styles.metaEditSection}>
                        <div className={styles.metaEditForm}>
                            <div className={styles.formGroup}>
                                <label htmlFor="itemName">アイテム名</label>
                                <input
                                    id="itemName"
                                    type="text"
                                    value={editName}
                                    onChange={(e) => setEditName(e.target.value)}
                                    className={styles.input}
                                    disabled={isSavingMeta}
                                />
                            </div>
                            <div className={styles.formGroup}>
                                <label htmlFor="itemDescription">説明</label>
                                <textarea
                                    id="itemDescription"
                                    value={editDescription}
                                    onChange={(e) => setEditDescription(e.target.value)}
                                    className={styles.descriptionTextarea}
                                    rows={3}
                                    disabled={isSavingMeta}
                                />
                            </div>
                            <div className={styles.formGroup}>
                                <label htmlFor="itemLocation">
                                    <ArrowRightLeft size={16} style={{ marginRight: 6, verticalAlign: 'middle' }} />
                                    配置場所
                                </label>
                                <select
                                    id="itemLocation"
                                    value={editOwnerKind === 'building' ? editOwnerId : 'world'}
                                    onChange={(e) => {
                                        if (e.target.value === 'world') {
                                            setEditOwnerKind('world');
                                            setEditOwnerId('');
                                        } else {
                                            setEditOwnerKind('building');
                                            setEditOwnerId(e.target.value);
                                        }
                                    }}
                                    className={styles.select}
                                    disabled={isSavingMeta || isLoadingBuildings}
                                >
                                    <option value="world">ワールド（どこにも配置しない）</option>
                                    {buildings.map(b => (
                                        <option key={b.id} value={b.id}>{b.name}</option>
                                    ))}
                                </select>
                            </div>
                            <div className={styles.metaEditActions}>
                                <button
                                    className={`${styles.toggleBtn} ${styles.saveBtn}`}
                                    onClick={handleSaveMeta}
                                    disabled={isSavingMeta}
                                >
                                    <Save size={16} />
                                    <span>{isSavingMeta ? '保存中...' : '保存'}</span>
                                </button>
                                <button
                                    className={`${styles.toggleBtn} ${styles.cancelBtn}`}
                                    onClick={handleCancelMetaEdit}
                                    disabled={isSavingMeta}
                                >
                                    <XCircle size={16} />
                                    <span>キャンセル</span>
                                </button>
                            </div>
                        </div>
                        {error && <div className={styles.error}>{error}</div>}
                    </div>
                ) : (
                    <>
                        <div className={styles.meta}>
                            <span className={styles.badge}>{item.type}</span>
                            <span className={styles.id}>ID: <code>{item.id}</code></span>
                            {itemDetails && itemDetails.OWNER_KIND === 'building' && (
                                <span className={styles.location}>
                                    <ArrowRightLeft size={14} style={{ marginRight: 4 }} />
                                    {buildings.find(b => b.id === itemDetails.OWNER_ID)?.name || itemDetails.OWNER_ID}
                                </span>
                            )}
                        </div>

                        {displayDescription && (
                            <div className={styles.description}>
                                {displayDescription}
                            </div>
                        )}
                    </>
                )}

                <div className={styles.body}>
                    {item.type === 'picture' ? (
                        <div className={styles.imageContainer}>
                            <img
                                src={`/api/info/item/${item.id}`}
                                alt={item.name}
                                className={styles.image}
                            />
                        </div>
                    ) : item.type === 'document' ? (
                        <div className={styles.documentContainer}>
                            <div className={styles.documentHeader}>
                                <div className={styles.viewToggle}>
                                    <button
                                        className={`${styles.toggleBtn} ${isMarkdown && !isEditing ? styles.active : ''}`}
                                        onClick={() => { setIsMarkdown(true); setIsEditing(false); }}
                                        title="マークダウン表示"
                                        disabled={isEditing}
                                    >
                                        <FileText size={16} />
                                        <span>Markdown</span>
                                    </button>
                                    <button
                                        className={`${styles.toggleBtn} ${!isMarkdown && !isEditing ? styles.active : ''}`}
                                        onClick={() => { setIsMarkdown(false); setIsEditing(false); }}
                                        title="プレーンテキスト表示"
                                        disabled={isEditing}
                                    >
                                        <Code2 size={16} />
                                        <span>Plain</span>
                                    </button>
                                </div>
                                <div className={styles.editActions}>
                                    {!isEditing ? (
                                        <button
                                            className={`${styles.toggleBtn} ${styles.editBtn}`}
                                            onClick={handleStartEdit}
                                            title="編集"
                                            disabled={!content || isLoading}
                                        >
                                            <Pencil size={16} />
                                            <span>Edit</span>
                                        </button>
                                    ) : (
                                        <>
                                            <button
                                                className={`${styles.toggleBtn} ${styles.saveBtn}`}
                                                onClick={handleSave}
                                                title="保存"
                                                disabled={isSaving}
                                            >
                                                <Save size={16} />
                                                <span>{isSaving ? '保存中...' : 'Save'}</span>
                                            </button>
                                            <button
                                                className={`${styles.toggleBtn} ${styles.cancelBtn}`}
                                                onClick={handleCancelEdit}
                                                title="キャンセル"
                                                disabled={isSaving}
                                            >
                                                <XCircle size={16} />
                                                <span>Cancel</span>
                                            </button>
                                        </>
                                    )}
                                </div>
                            </div>
                            {isLoading && <div className={styles.loading}>読み込み中...</div>}
                            {error && <div className={styles.error}>{error}</div>}
                            {content !== null && !isLoading && (
                                isEditing ? (
                                    <textarea
                                        className={styles.editTextarea}
                                        value={editContent}
                                        onChange={(e) => setEditContent(e.target.value)}
                                        disabled={isSaving}
                                    />
                                ) : isMarkdown ? (
                                    <div className={styles.markdownContent}>
                                        <ReactMarkdown>{content}</ReactMarkdown>
                                    </div>
                                ) : (
                                    <pre className={styles.documentContent}>
                                        {content}
                                    </pre>
                                )
                            )}
                        </div>
                    ) : (
                        <div className={styles.unsupported}>
                            このアイテムタイプ ({item.type}) の表示はサポートされていません。
                        </div>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
