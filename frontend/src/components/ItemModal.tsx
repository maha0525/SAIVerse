import { useState, useEffect, useCallback } from 'react';
import { X, FileText, Code2, Pencil, Save, XCircle, Settings, ArrowRightLeft, Package, PackagePlus, PackageOpen, Check, Square, Image as ImageIcon, File } from 'lucide-react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import styles from './ItemModal.module.css';
import SaiverseLink from './SaiverseLink';
import ModalOverlay from './common/ModalOverlay';

interface Item {
    id: string;
    name: string;
    description?: string;
    type: string;
}

interface BagContentItem {
    id: string;
    name: string;
    type: string;
    description: string;
    is_open?: boolean;
    contained_items?: BagContentItem[];
    contained_count?: number;
}

interface BagItem {
    id: string;
    name: string;
}

/** まとめ収納の選択ビューに並べる、いまいる部屋のアイテム。 */
interface RoomItem {
    id: string;
    name: string;
    type: string;
    description?: string;
}

/** まとめ操作のビュー。null = 通常の中身表示。 */
type BulkMode = 'stow' | 'takeout' | null;

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
    CREATED_AT?: string | null;
}

interface ItemModalProps {
    isOpen: boolean;
    onClose: () => void;
    item: Item | null;
    onItemUpdated?: () => void;  // Callback when item is updated
    /** 世界のアイテム配置が変わったことを親へ知らせる。onItemUpdated と違い
     * モーダルを閉じない前提の通知で、まとめ収納のように操作を続ける経路で使う。 */
    onWorldChanged?: () => void;
    /** 親が把握している現在 Building ID。バッグ一覧取得用。
     * 省略すると server-global の user_current_building_id にフォールバックし、
     * マルチデバイス間で他クライアントの操作に汚染される (エリス上書き事故の遠因)。
     */
    currentBuildingId?: string | null;
}

export default function ItemModal({ isOpen, onClose, item, onItemUpdated, onWorldChanged, currentBuildingId }: ItemModalProps) {
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

    // Bag contents
    const [bagContents, setBagContents] = useState<BagContentItem[]>([]);
    const [isLoadingBagContents, setIsLoadingBagContents] = useState(false);

    // Bag items in current building (for location dropdown)
    const [bagItems, setBagItems] = useState<BagItem[]>([]);

    // Nested item modal for viewing items inside bags
    const [nestedItem, setNestedItem] = useState<Item | null>(null);

    // まとめ収納 (部屋 ⇔ Bag) の状態
    const [bulkMode, setBulkMode] = useState<BulkMode>(null);
    const [roomItems, setRoomItems] = useState<RoomItem[]>([]);
    const [isLoadingRoomItems, setIsLoadingRoomItems] = useState(false);
    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
    const [isBulkRunning, setIsBulkRunning] = useState(false);
    const [bulkError, setBulkError] = useState<string | null>(null);
    // サムネイルの読み込みに失敗したアイテム (アイコン表示へフォールバックする)
    const [thumbErrors, setThumbErrors] = useState<Set<string>>(new Set());

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

    // Bag の中身を取得する (初回表示と、まとめ操作の後の再取得で共用)
    const loadBagContents = useCallback(async (itemId: string) => {
        setIsLoadingBagContents(true);
        try {
            const res = await fetch(`/api/info/item/${itemId}/bag-contents`);
            if (!res.ok) throw new Error('Failed to load bag contents');
            const data = await res.json();
            setBagContents(data.items || []);
        } catch (err) {
            console.error(err);
            setError('バッグの中身の読み込みに失敗しました');
        } finally {
            setIsLoadingBagContents(false);
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
        } else if (isOpen && item && item.type === 'bag') {
            setError(null);
            setIsEditing(false);
            setIsMetaEditing(false);
            loadBagContents(item.id);
        } else {
            setContent(null);
            setEditContent('');
            setBagContents([]);
            setError(null);
            setIsEditing(false);
            setIsMetaEditing(false);
        }
        // Load item details and buildings (for location and creation date display)
        setItemDetails(null);
        setNestedItem(null);
        // 別のアイテムを開いたら、まとめ操作の途中状態は持ち越さない
        setBulkMode(null);
        setRoomItems([]);
        setSelectedIds(new Set());
        setBulkError(null);
        if (isOpen && item) {
            loadItemDetails(item.id);
            loadBuildings();
        }
    }, [isOpen, item, loadItemDetails, loadBuildings, loadBagContents]);

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
    const loadBagItemsInBuilding = useCallback(async () => {
        if (!currentBuildingId) {
            // 親が currentBuildingId を渡し忘れた場合の安全策。
            // server-global にフォールバックするとマルチデバイス汚染リスクがあるため即 return。
            console.warn('[ItemModal] loadBagItemsInBuilding skipped: currentBuildingId not provided');
            return;
        }
        try {
            const res = await fetch(`/api/info/details?building_id=${encodeURIComponent(currentBuildingId)}`);
            if (res.ok) {
                const data = await res.json();
                const bags = (data.items || []).filter(
                    (i: { type: string; id: string }) => i.type === 'bag' && i.id !== item?.id
                );
                setBagItems(bags.map((b: { id: string; name: string }) => ({ id: b.id, name: b.name })));
            }
        } catch (err) {
            console.error('Failed to load bag items:', err);
        }
    }, [item?.id, currentBuildingId]);

    const handleStartMetaEdit = async () => {
        if (!item) return;
        await Promise.all([
            loadItemDetails(item.id),
            loadBuildings(),
            loadBagItemsInBuilding(),
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

    // --- まとめ収納 (部屋 ⇔ Bag) ---

    // いまいる部屋のアイテムを取得する (この Bag 自身は除く)
    const loadRoomItems = useCallback(async () => {
        if (!currentBuildingId || !item) return;
        setIsLoadingRoomItems(true);
        try {
            const res = await fetch(`/api/info/details?building_id=${encodeURIComponent(currentBuildingId)}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const list: RoomItem[] = (data.items || [])
                .filter((i: RoomItem) => i.id !== item.id)
                .map((i: RoomItem) => ({ id: i.id, name: i.name, type: i.type, description: i.description }));
            setRoomItems(list);
        } catch (err) {
            console.error('Failed to load room items:', err);
            setRoomItems([]);
            setBulkError('部屋のアイテム一覧の取得に失敗しました');
        } finally {
            setIsLoadingRoomItems(false);
        }
    }, [currentBuildingId, item]);

    // 「部屋に出す」の行き先。親から渡された現在地を優先し、無ければこの Bag 自身の置き場所を使う。
    const takeoutBuildingId = currentBuildingId
        || (itemDetails?.OWNER_KIND === 'building' ? itemDetails.OWNER_ID : null)
        || null;

    const toggleSelected = (id: string) => {
        setSelectedIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    const handleStartStow = async () => {
        setBulkError(null);
        setSelectedIds(new Set());
        setBulkMode('stow');
        await loadRoomItems();
    };

    const handleStartTakeout = () => {
        setBulkError(null);
        setSelectedIds(new Set());
        setBulkMode('takeout');
    };

    const handleCancelBulk = () => {
        setBulkMode(null);
        setSelectedIds(new Set());
        setBulkError(null);
    };

    /** 選択したアイテムを 1 個ずつ順に移動する。途中で失敗したらそこで中断する。 */
    const runBulkMove = async (ids: string[], ownerKind: 'bag' | 'building', ownerId: string) => {
        if (!item || ids.length === 0) return;

        const nameOf = (id: string) =>
            roomItems.find(r => r.id === id)?.name
            || bagContents.find(c => c.id === id)?.name
            || id;

        setIsBulkRunning(true);
        setBulkError(null);

        let moved = 0;
        let failureMessage: string | null = null;

        for (const id of ids) {
            try {
                // 他のフィールドを保つため、いまの値を取ってから置き場所だけ差し替える
                const getRes = await fetch(`/api/world/items/${id}`);
                if (!getRes.ok) throw new Error(`アイテム情報を取得できませんでした (HTTP ${getRes.status})`);
                const current: ItemDetails = await getRes.json();

                const putRes = await fetch(`/api/world/items/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: current.NAME,
                        item_type: current.TYPE,
                        description: current.DESCRIPTION || '',
                        owner_kind: ownerKind,
                        owner_id: ownerId,
                        state_json: current.STATE_JSON || null,
                        file_path: current.FILE_PATH || null,
                    }),
                });
                if (!putRes.ok) {
                    let detail = `HTTP ${putRes.status}`;
                    try {
                        const errBody = await putRes.json();
                        if (errBody?.detail) detail = String(errBody.detail);
                    } catch {
                        // エラー本文が JSON でない場合はステータスコードのまま伝える
                    }
                    throw new Error(detail);
                }
                moved += 1;
            } catch (err) {
                const reason = err instanceof Error ? err.message : String(err);
                failureMessage = `${moved}個まで完了したところで「${nameOf(id)}」の移動に失敗しました: ${reason}`;
                break;
            }
        }

        if (moved > 0) {
            await loadBagContents(item.id);
            if (onWorldChanged) onWorldChanged();
        }

        if (failureMessage) {
            setBulkError(failureMessage);
            // 移動できなかった分は選択したまま残し、再実行できるようにする
            setSelectedIds(new Set(ids.slice(moved)));
            if (ownerKind === 'bag') await loadRoomItems();
        } else {
            setSelectedIds(new Set());
            setBulkMode(null);
        }

        setIsBulkRunning(false);
    };

    const handleRunStow = () => {
        if (!item) return;
        runBulkMove(Array.from(selectedIds), 'bag', item.id);
    };

    const handleRunTakeout = () => {
        if (!takeoutBuildingId) return;
        runBulkMove(Array.from(selectedIds), 'building', takeoutBuildingId);
    };

    const renderTypeIcon = (type: string) => (
        type === 'picture' ? <ImageIcon size={18} />
            : type === 'bag' ? <Package size={18} />
            : <File size={18} />
    );

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
                                    value={editOwnerKind === 'bag' ? `bag:${editOwnerId}` : editOwnerKind === 'building' ? editOwnerId : 'world'}
                                    onChange={(e) => {
                                        const val = e.target.value;
                                        if (val === 'world') {
                                            setEditOwnerKind('world');
                                            setEditOwnerId('');
                                        } else if (val.startsWith('bag:')) {
                                            setEditOwnerKind('bag');
                                            setEditOwnerId(val.slice(4));
                                        } else {
                                            setEditOwnerKind('building');
                                            setEditOwnerId(val);
                                        }
                                    }}
                                    className={styles.select}
                                    disabled={isSavingMeta || isLoadingBuildings}
                                >
                                    <option value="world">ワールド（どこにも配置しない）</option>
                                    <optgroup label="Building">
                                        {buildings.map(b => (
                                            <option key={b.id} value={b.id}>{b.name}</option>
                                        ))}
                                    </optgroup>
                                    {bagItems.length > 0 && (
                                        <optgroup label="Bag">
                                            {bagItems.map(b => (
                                                <option key={b.id} value={`bag:${b.id}`}>📦 {b.name}</option>
                                            ))}
                                        </optgroup>
                                    )}
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
                            {itemDetails?.CREATED_AT && (
                                <span className={styles.createdAt}>
                                    {new Date(itemDetails.CREATED_AT).toLocaleString('ja-JP', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
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
                                        <ReactMarkdown
                                            remarkPlugins={[remarkGfm, remarkBreaks]}
                                            urlTransform={(url) => url.startsWith('saiverse://') ? url : defaultUrlTransform(url)}
                                            components={{
                                                a: ({ href, children }) => <SaiverseLink href={href}>{children}</SaiverseLink>,
                                            }}
                                        >{content}</ReactMarkdown>
                                    </div>
                                ) : (
                                    <pre className={styles.documentContent}>
                                        {content}
                                    </pre>
                                )
                            )}
                        </div>
                    ) : item.type === 'audio' ? (
                        <div className={styles.imageContainer}>
                            <audio
                                controls
                                preload="metadata"
                                src={`/api/info/item/${item.id}`}
                                style={{ width: '100%' }}
                            >
                                お使いのブラウザは audio タグをサポートしていません。
                            </audio>
                        </div>
                    ) : item.type === 'video' ? (
                        <div className={styles.imageContainer}>
                            <video
                                controls
                                preload="metadata"
                                src={`/api/info/item/${item.id}`}
                                style={{ width: '100%', maxHeight: '70vh' }}
                            >
                                お使いのブラウザは video タグをサポートしていません。
                            </video>
                        </div>
                    ) : item.type === 'bag' ? (
                        <div className={styles.bagContainer}>
                            {/* まとめ収納の操作列。currentBuildingId が無いときは「しまう」を出さない
                                (loadBagItemsInBuilding と同じ守り: 部屋を特定できないまま操作させない)。 */}
                            <div className={styles.bulkBar}>
                                {bulkMode === null ? (
                                    <>
                                        {currentBuildingId && (
                                            <button
                                                className={styles.bulkBtn}
                                                onClick={handleStartStow}
                                                disabled={isLoadingBagContents}
                                            >
                                                <PackagePlus size={16} />
                                                <span>部屋のアイテムをしまう</span>
                                            </button>
                                        )}
                                        {takeoutBuildingId && bagContents.length > 0 && (
                                            <button
                                                className={styles.bulkBtn}
                                                onClick={handleStartTakeout}
                                                disabled={isLoadingBagContents}
                                            >
                                                <PackageOpen size={16} />
                                                <span>部屋に出す</span>
                                            </button>
                                        )}
                                    </>
                                ) : (
                                    <>
                                        <button
                                            className={`${styles.bulkBtn} ${styles.bulkRunBtn}`}
                                            onClick={bulkMode === 'stow' ? handleRunStow : handleRunTakeout}
                                            disabled={isBulkRunning || selectedIds.size === 0}
                                        >
                                            {bulkMode === 'stow' ? <PackagePlus size={16} /> : <PackageOpen size={16} />}
                                            <span>
                                                {isBulkRunning
                                                    ? '実行中...'
                                                    : bulkMode === 'stow'
                                                        ? `しまう (${selectedIds.size}個)`
                                                        : `部屋に出す (${selectedIds.size}個)`}
                                            </span>
                                        </button>
                                        <button
                                            className={`${styles.bulkBtn} ${styles.bulkCancelBtn}`}
                                            onClick={handleCancelBulk}
                                            disabled={isBulkRunning}
                                        >
                                            <XCircle size={16} />
                                            <span>キャンセル</span>
                                        </button>
                                    </>
                                )}
                            </div>
                            {bulkError && <div className={styles.error}>{bulkError}</div>}
                            {bulkMode === 'stow' ? (
                                <>
                                    {isLoadingRoomItems && <div className={styles.loading}>読み込み中...</div>}
                                    {!isLoadingRoomItems && (
                                        roomItems.length > 0 ? (
                                            <div className={styles.bagGrid}>
                                                {roomItems.map(ri => {
                                                    const checked = selectedIds.has(ri.id);
                                                    return (
                                                        <div
                                                            key={ri.id}
                                                            className={`${styles.bagCard} ${styles[`bagCard_${ri.type}`] || ''} ${checked ? styles.bagCardSelected : ''}`}
                                                            role="checkbox"
                                                            aria-checked={checked}
                                                            tabIndex={0}
                                                            onClick={() => { if (!isBulkRunning) toggleSelected(ri.id); }}
                                                            onKeyDown={(e) => {
                                                                if (e.key === 'Enter' || e.key === ' ') {
                                                                    e.preventDefault();
                                                                    if (!isBulkRunning) toggleSelected(ri.id);
                                                                }
                                                            }}
                                                        >
                                                            <div className={`${styles.bagCardCheck} ${checked ? styles.bagCardCheckOn : ''}`}>
                                                                {checked ? <Check size={14} /> : <Square size={14} />}
                                                            </div>
                                                            {ri.type === 'picture' && !thumbErrors.has(ri.id) ? (
                                                                <div className={styles.bagCardThumb}>
                                                                    <img
                                                                        src={`/api/info/item/${ri.id}?thumb=1`}
                                                                        alt={ri.name}
                                                                        loading="lazy"
                                                                        onError={() => setThumbErrors(prev => {
                                                                            const next = new Set(prev);
                                                                            next.add(ri.id);
                                                                            return next;
                                                                        })}
                                                                    />
                                                                </div>
                                                            ) : (
                                                                <div className={styles.bagCardIcon}>
                                                                    {renderTypeIcon(ri.type)}
                                                                </div>
                                                            )}
                                                            <div className={styles.bagCardInfo}>
                                                                <div className={styles.bagCardName}>{ri.name}</div>
                                                                {ri.description && (
                                                                    <div className={styles.bagCardDesc}>{ri.description}</div>
                                                                )}
                                                            </div>
                                                        </div>
                                                    );
                                                })}
                                            </div>
                                        ) : (
                                            <div className={styles.bagEmpty}>この部屋にしまえるアイテムはありません</div>
                                        )
                                    )}
                                </>
                            ) : (
                                <>
                                    {isLoadingBagContents && <div className={styles.loading}>読み込み中...</div>}
                                    {error && <div className={styles.error}>{error}</div>}
                                    {!isLoadingBagContents && (
                                        bagContents.length > 0 ? (
                                            <div className={styles.bagGrid}>
                                                {bagContents.map(ci => {
                                                    const selecting = bulkMode === 'takeout';
                                                    const checked = selectedIds.has(ci.id);
                                                    return (
                                                        <div
                                                            key={ci.id}
                                                            className={`${styles.bagCard} ${styles[`bagCard_${ci.type}`] || ''} ${selecting && checked ? styles.bagCardSelected : ''}`}
                                                            // 選択モード中はしまう側のカードと同じくキーボードでも切り替えられる形にする
                                                            role={selecting ? 'checkbox' : undefined}
                                                            aria-checked={selecting ? checked : undefined}
                                                            tabIndex={selecting ? 0 : undefined}
                                                            onKeyDown={selecting ? (e) => {
                                                                if (e.key === 'Enter' || e.key === ' ') {
                                                                    e.preventDefault();
                                                                    if (!isBulkRunning) toggleSelected(ci.id);
                                                                }
                                                            } : undefined}
                                                            onClick={() => {
                                                                if (selecting) {
                                                                    if (!isBulkRunning) toggleSelected(ci.id);
                                                                } else {
                                                                    setNestedItem({ id: ci.id, name: ci.name, type: ci.type, description: ci.description });
                                                                }
                                                            }}
                                                        >
                                                            {selecting && (
                                                                <div className={`${styles.bagCardCheck} ${checked ? styles.bagCardCheckOn : ''}`}>
                                                                    {checked ? <Check size={14} /> : <Square size={14} />}
                                                                </div>
                                                            )}
                                                            <div className={styles.bagCardIcon}>
                                                                {renderTypeIcon(ci.type)}
                                                            </div>
                                                            <div className={styles.bagCardInfo}>
                                                                <div className={styles.bagCardName}>
                                                                    {ci.name}
                                                                    {ci.type === 'bag' && ci.contained_count != null && (
                                                                        <span className={styles.bagCardCount}> ({ci.contained_count})</span>
                                                                    )}
                                                                </div>
                                                                {ci.description && (
                                                                    <div className={styles.bagCardDesc}>{ci.description}</div>
                                                                )}
                                                            </div>
                                                        </div>
                                                    );
                                                })}
                                            </div>
                                        ) : (
                                            <div className={styles.bagEmpty}>バッグは空です</div>
                                        )
                                    )}
                                </>
                            )}
                        </div>
                    ) : (
                        <div className={styles.unsupported}>
                            このアイテムタイプ ({item.type}) の表示はサポートされていません。
                        </div>
                    )}
                </div>

                {/* Nested item modal for items inside bags */}
                {nestedItem && (
                    <ItemModal
                        isOpen={true}
                        onClose={() => setNestedItem(null)}
                        item={nestedItem}
                        onItemUpdated={onItemUpdated}
                        onWorldChanged={() => {
                            // 入れ子の Bag で動かした分を、この階層の中身表示にも反映する
                            loadBagContents(item.id);
                            if (onWorldChanged) onWorldChanged();
                        }}
                        currentBuildingId={currentBuildingId}
                    />
                )}
            </div>
        </ModalOverlay>
    );
}
