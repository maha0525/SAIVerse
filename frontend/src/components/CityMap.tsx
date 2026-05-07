"use client";

import { useEffect, useState, useRef, useCallback } from 'react';
import { Home as HomeIcon, X, Edit3, Save, Image as ImageIcon, Loader2, Trash2 } from 'lucide-react';
import styles from './CityMap.module.css';
import PersonaMenu from './PersonaMenu';
import MemoryModal from './MemoryModal';
import ScheduleModal from './ScheduleModal';
import TasksModal from './TasksModal';
import SettingsModal from './SettingsModal';
import InventoryModal from './InventoryModal';

interface Occupant {
    id: string;
    name: string;
    avatar?: string | null;
}

interface CityMapBuilding {
    id: string;
    name: string;
    description?: string | null;
    image_path?: string | null;
    map_x?: number | null;
    map_y?: number | null;
    occupants: Occupant[];
}

interface CityMapResponse {
    city_id: number | null;
    user_current_building_id: string | null;
    map_background_image?: string | null;
    buildings: CityMapBuilding[];
}

interface CityMapProps {
    /** 親が把握している現在地。優先で枠を出す。 */
    currentBuildingId?: string | null;
    /** Building クリック時にチャット遷移を起こす */
    onSelectBuilding: (buildingId: string) => void;
    /** ポーリング契機の外部トリガー (move 等) */
    refreshTrigger?: number;
    /** モーダル閉じるトリガー。指定時は右上に × ボタンを出す。 */
    onClose?: () => void;
}

const POLL_INTERVAL_MS = 7000;
const MAX_VISIBLE_OCCUPANTS = 6;
// ミニマル時はアイコンが大きく場所を取るので少なめ
const MAX_VISIBLE_OCCUPANTS_MINIMAL = 4;

// world サイズ。BuildingCell 200×~220 の擬似グリッドが余裕で収まる広さ。
const WORLD_WIDTH = 3200;
const WORLD_HEIGHT = 2200;
const MIN_SCALE = 0.3;
const MAX_SCALE = 2.5;
const DRAG_THRESHOLD_PX = 4;
// scale がこの値以上ならリッチ表示 (枠+内装+description)、未満ならミニマル表示 (アイコン+ラベルのみ)
const RICH_MODE_SCALE_THRESHOLD = 1.2;

interface ViewState {
    x: number;
    y: number;
    scale: number;
}

// 擬似座標生成: building.id のハッシュをジッタとして加味した擬似グリッド配置。
// MAP_X/MAP_Y が DB に追加された後はそちらを優先する。
function hashStr(s: string): number {
    let h = 5381;
    for (let i = 0; i < s.length; i++) {
        h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
    }
    return h;
}

function pseudoBuildingPosition(id: string, idx: number, total: number): { x: number; y: number } {
    const cols = Math.max(4, Math.ceil(Math.sqrt(Math.max(1, total) * 1.5)));
    const col = idx % cols;
    const row = Math.floor(idx / cols);
    const h = hashStr(id);
    const jx = (h % 80) - 40;
    const jy = (Math.floor(h / 100) % 80) - 40;
    const baseX = 600;
    const baseY = 400;
    const stepX = 360;
    const stepY = 320;
    return {
        x: baseX + col * stepX + jx,
        y: baseY + row * stepY + jy,
    };
}

export default function CityMap({ currentBuildingId, onSelectBuilding, refreshTrigger, onClose }: CityMapProps) {
    const [data, setData] = useState<CityMapResponse | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [selectedPersona, setSelectedPersona] = useState<Occupant | null>(null);

    // モーダル制御 (RightSidebar と同様)
    const [activeModalPersonaId, setActiveModalPersonaId] = useState<string | null>(null);
    const [activeModalPersonaName, setActiveModalPersonaName] = useState<string | null>(null);
    const [showMemory, setShowMemory] = useState(false);
    const [showSchedule, setShowSchedule] = useState(false);
    const [showTasks, setShowTasks] = useState(false);
    const [showSettings, setShowSettings] = useState(false);
    const [showInventory, setShowInventory] = useState(false);

    const cancelledRef = useRef(false);

    // 画像読み込み失敗 building の id 集合 → アイコンにフォールバック
    const [imageFailed, setImageFailed] = useState<Set<string>>(new Set());

    // ── pan/zoom 状態 ──
    const viewportRef = useRef<HTMLDivElement | null>(null);
    // state は再レンダ用、ref はイベントハンドラから最新値を読むために併用
    // (handleMouseDown の closure に古い view が固定されるのを避ける)
    const viewRef = useRef<ViewState>({ x: 0, y: 0, scale: 1 });
    const [view, setView] = useState<ViewState>(viewRef.current);
    const [isDragging, setIsDragging] = useState(false);
    const initializedRef = useRef(false);

    const updateView = useCallback((next: ViewState) => {
        viewRef.current = next;
        setView(next);
    }, []);

    // パン (背景ドラッグ) 状態。dragged が true の時のみ buildingCell の onClick を抑制する。
    const dragRef = useRef({
        active: false,
        startClientX: 0,
        startClientY: 0,
        startView: { x: 0, y: 0, scale: 1 } as ViewState,
        dragged: false,
    });

    // ── 編集モード ── 各セルをドラッグして座標変更
    const [isEditMode, setIsEditMode] = useState(false);
    const [editedPositions, setEditedPositions] = useState<Record<string, { x: number; y: number }>>({});
    const [isSaving, setIsSaving] = useState(false);

    // セル個別ドラッグ状態 (編集モード時のみ active になる)
    const cellDragRef = useRef({
        active: false,
        buildingId: '' as string,
        startClientX: 0,
        startClientY: 0,
        startCellX: 0,
        startCellY: 0,
        startScale: 1,
        dragged: false,
    });

    // pointer/touch handler は useEffect 内の closure に固定されるため、最新の
    // isEditMode と building → 座標 マップを ref 経由で読めるようにする。
    const latestRef = useRef<{
        isEditMode: boolean;
        positionsMap: Map<string, { x: number; y: number }>;
    }>({ isEditMode: false, positionsMap: new Map() });

    // 背景画像変更
    const bgFileInputRef = useRef<HTMLInputElement | null>(null);
    const [isBgUploading, setIsBgUploading] = useState(false);

    const fetchData = async () => {
        try {
            const res = await fetch('/api/info/city-map');
            if (!res.ok) {
                setError(`Failed to load city map (${res.status})`);
                return;
            }
            if (cancelledRef.current) return;
            const json: CityMapResponse = await res.json();
            setData(json);
            setError(null);
        } catch (e) {
            console.error('CityMap fetch error', e);
            setError('街マップの取得に失敗しました');
        }
    };

    useEffect(() => {
        cancelledRef.current = false;
        fetchData();
        const id = setInterval(fetchData, POLL_INTERVAL_MS);
        return () => {
            cancelledRef.current = true;
            clearInterval(id);
        };
    }, [refreshTrigger]);

    // 初回: viewport が描画されたら world の中心がビューポート中央に来るよう view を初期化
    useEffect(() => {
        if (initializedRef.current) return;
        const vp = viewportRef.current;
        if (!vp) return;
        const w = vp.clientWidth;
        const h = vp.clientHeight;
        if (w === 0 || h === 0) return;
        updateView({
            x: w / 2 - WORLD_WIDTH / 2,
            y: h / 2 - WORLD_HEIGHT / 2,
            scale: 1,
        });
        initializedRef.current = true;
    }, [data, updateView]);

    // ホイールズーム: cursor 位置を中心にスケール
    // React の onWheel は passive: true で preventDefault が効かないため直接購読
    useEffect(() => {
        const vp = viewportRef.current;
        if (!vp) return;
        const onWheel = (e: WheelEvent) => {
            e.preventDefault();
            const rect = vp.getBoundingClientRect();
            const cx = e.clientX - rect.left;
            const cy = e.clientY - rect.top;
            const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
            const cur = viewRef.current;
            const newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, cur.scale * factor));
            const ratio = newScale / cur.scale;
            updateView({
                x: cx - (cx - cur.x) * ratio,
                y: cy - (cy - cur.y) * ratio,
                scale: newScale,
            });
        };
        vp.addEventListener('wheel', onWheel, { passive: false });
        return () => vp.removeEventListener('wheel', onWheel);
    }, [updateView]);

    // ── Pointer events で mouse/touch/pen を統一処理 ──
    // 1本指: 背景パン or 編集モード時のセル個別ドラッグ
    // 2本指: ピンチズーム (1本指側はキャンセル)
    // pointermove/up は viewport 外でも追従するよう window に貼る (mouse と同じ流儀)。
    //
    // 同時に viewport の native touchstart/touchmove を capture phase で
    // stopImmediatePropagation し、Sidebar.tsx の window touchstart listener と
    // page.tsx の RightSidebar swipe ハンドラへの伝播を遮断する。
    // preventDefault は tap → click を潰さないよう使わない。標準ジェスチャ抑制は
    // CSS の touch-action: none に任せる。
    useEffect(() => {
        const vp = viewportRef.current;
        if (!vp) return;

        // pointerId → 最新クライアント座標
        const pointers = new Map<number, { x: number; y: number }>();

        // pointers.size === 2 の間だけ有効になるピンチ状態
        let pinchState: {
            startDistance: number;
            startView: ViewState;
            // ピンチ中心の viewport ローカル座標 (rect.left/top を引いた値)
            startCenterLocal: { x: number; y: number };
        } | null = null;

        const onPointerDown = (e: PointerEvent) => {
            if (e.pointerType === 'mouse' && e.button !== 0) return;

            pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

            if (pointers.size === 2) {
                // 2本目: ピンチ開始。1本指側のパン/セルドラッグは破棄
                const pts = Array.from(pointers.values());
                const dx = pts[0].x - pts[1].x;
                const dy = pts[0].y - pts[1].y;
                const distance = Math.hypot(dx, dy) || 1;
                const cx = (pts[0].x + pts[1].x) / 2;
                const cy = (pts[0].y + pts[1].y) / 2;
                const rect = vp.getBoundingClientRect();
                pinchState = {
                    startDistance: distance,
                    startView: { ...viewRef.current },
                    startCenterLocal: { x: cx - rect.left, y: cy - rect.top },
                };
                dragRef.current.active = false;
                cellDragRef.current.active = false;
                setIsDragging(false);
                return;
            }

            if (pointers.size !== 1) return;

            // 1本目: 編集モード && セル上ならセルドラッグ、それ以外は背景パン
            const target = e.target as HTMLElement | null;
            const cellEl = target?.closest<HTMLElement>('[data-building-cell-id]') ?? null;
            const buildingId = cellEl?.dataset.buildingCellId ?? null;
            const { isEditMode: editing, positionsMap } = latestRef.current;

            if (editing && buildingId) {
                const pos = positionsMap.get(buildingId);
                if (pos) {
                    cellDragRef.current = {
                        active: true,
                        buildingId,
                        startClientX: e.clientX,
                        startClientY: e.clientY,
                        startCellX: pos.x,
                        startCellY: pos.y,
                        startScale: viewRef.current.scale,
                        dragged: false,
                    };
                    return;
                }
            }

            dragRef.current = {
                active: true,
                startClientX: e.clientX,
                startClientY: e.clientY,
                startView: { ...viewRef.current },
                dragged: false,
            };
        };

        const onPointerMove = (e: PointerEvent) => {
            const tracked = pointers.get(e.pointerId);
            if (!tracked) return;
            tracked.x = e.clientX;
            tracked.y = e.clientY;

            // ピンチ
            if (pinchState && pointers.size >= 2) {
                const pts = Array.from(pointers.values());
                const dx = pts[0].x - pts[1].x;
                const dy = pts[0].y - pts[1].y;
                const distance = Math.hypot(dx, dy) || 1;
                const newScaleRaw = pinchState.startView.scale * (distance / pinchState.startDistance);
                const newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, newScaleRaw));
                const ratio = newScale / pinchState.startView.scale;
                const cx = pinchState.startCenterLocal.x;
                const cy = pinchState.startCenterLocal.y;
                updateView({
                    x: cx - (cx - pinchState.startView.x) * ratio,
                    y: cy - (cy - pinchState.startView.y) * ratio,
                    scale: newScale,
                });
                return;
            }

            // セル個別ドラッグ
            const cell = cellDragRef.current;
            if (cell.active) {
                const dxRaw = e.clientX - cell.startClientX;
                const dyRaw = e.clientY - cell.startClientY;
                if (!cell.dragged && (Math.abs(dxRaw) > DRAG_THRESHOLD_PX || Math.abs(dyRaw) > DRAG_THRESHOLD_PX)) {
                    cell.dragged = true;
                }
                if (cell.dragged) {
                    const dx = dxRaw / cell.startScale;
                    const dy = dyRaw / cell.startScale;
                    setEditedPositions(prev => ({
                        ...prev,
                        [cell.buildingId]: {
                            x: cell.startCellX + dx,
                            y: cell.startCellY + dy,
                        },
                    }));
                }
                return;
            }

            // 背景パン
            const drag = dragRef.current;
            if (!drag.active) return;
            const dx = e.clientX - drag.startClientX;
            const dy = e.clientY - drag.startClientY;
            if (!drag.dragged && (Math.abs(dx) > DRAG_THRESHOLD_PX || Math.abs(dy) > DRAG_THRESHOLD_PX)) {
                drag.dragged = true;
                setIsDragging(true);
            }
            if (drag.dragged) {
                updateView({
                    x: drag.startView.x + dx,
                    y: drag.startView.y + dy,
                    scale: drag.startView.scale,
                });
            }
        };

        const onPointerUp = (e: PointerEvent) => {
            pointers.delete(e.pointerId);

            if (pinchState && pointers.size < 2) {
                // ピンチ終了。残り1本指でパンを継続させると視覚的にジャンプするので
                // 残ったポインタも破棄して何もせず終わる。
                pinchState = null;
                pointers.clear();
                return;
            }

            if (pointers.size > 0) return;

            const cell = cellDragRef.current;
            if (cell.active) {
                cell.active = false;
                // dragged は click 発火直後にリセットしてセル click を抑制
                setTimeout(() => { cell.dragged = false; }, 0);
                return;
            }

            const drag = dragRef.current;
            if (drag.active) {
                drag.active = false;
                setIsDragging(false);
                setTimeout(() => { drag.dragged = false; }, 0);
            }
        };

        const onTouchBlock = (e: TouchEvent) => {
            // capture phase で先に握って window/document までバブルさせない。
            // preventDefault は呼ばない (tap → click を潰さないため)。
            e.stopImmediatePropagation();
        };

        vp.addEventListener('pointerdown', onPointerDown);
        window.addEventListener('pointermove', onPointerMove);
        window.addEventListener('pointerup', onPointerUp);
        window.addEventListener('pointercancel', onPointerUp);
        vp.addEventListener('touchstart', onTouchBlock, { passive: false, capture: true });
        vp.addEventListener('touchmove', onTouchBlock, { passive: false, capture: true });

        return () => {
            vp.removeEventListener('pointerdown', onPointerDown);
            window.removeEventListener('pointermove', onPointerMove);
            window.removeEventListener('pointerup', onPointerUp);
            window.removeEventListener('pointercancel', onPointerUp);
            vp.removeEventListener('touchstart', onTouchBlock, { capture: true } as EventListenerOptions);
            vp.removeEventListener('touchmove', onTouchBlock, { capture: true } as EventListenerOptions);
        };
    }, [updateView]);

    // 効果的な座標 = 編集中の値 > DBの値 > 擬似座標
    const resolvePosition = useCallback((b: CityMapBuilding, idx: number, total: number): { x: number; y: number } => {
        const edited = editedPositions[b.id];
        if (edited) return edited;
        if (b.map_x != null && b.map_y != null) return { x: b.map_x, y: b.map_y };
        return pseudoBuildingPosition(b.id, idx, total);
    }, [editedPositions]);

    // pointer handler が closure 経由で最新の isEditMode と building→座標 マップを
    // 参照できるよう、毎レンダ後に ref を更新する。
    const buildings = data?.buildings ?? [];
    useEffect(() => {
        const map = new Map<string, { x: number; y: number }>();
        buildings.forEach((b, idx) => {
            map.set(b.id, resolvePosition(b, idx, buildings.length));
        });
        latestRef.current = { isEditMode, positionsMap: map };
    });

    const editedCount = Object.keys(editedPositions).length;

    const cancelEdit = () => {
        setEditedPositions({});
        setIsEditMode(false);
    };

    // 背景画像変更: アップロード → PATCH → 即時 fetch
    const cityIdRef = data?.city_id;
    const handleBgUploadClick = () => {
        bgFileInputRef.current?.click();
    };

    const handleBgFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;
        if (cityIdRef == null) {
            alert('City ID が未取得です');
            return;
        }
        setIsBgUploading(true);
        try {
            // 1) WebP変換のみの hires エンドポイントへアップロード
            const fd = new FormData();
            fd.append('file', file);
            const upRes = await fetch('/api/media/upload-hires', { method: 'POST', body: fd });
            if (!upRes.ok) {
                alert('画像のアップロードに失敗しました');
                return;
            }
            const upJson = await upRes.json();
            // 2) City の背景画像だけ PATCH で即時更新
            const patchRes = await fetch(`/api/world/cities/${cityIdRef}/map-background`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ map_background_image: upJson.url }),
            });
            if (!patchRes.ok) {
                alert(`背景の保存に失敗しました (${patchRes.status})`);
                return;
            }
            await fetchData();
        } catch (err) {
            console.error('Background change error', err);
            alert('背景の更新に失敗しました');
        } finally {
            setIsBgUploading(false);
            if (bgFileInputRef.current) bgFileInputRef.current.value = '';
        }
    };

    const handleBgClear = async () => {
        if (cityIdRef == null) return;
        if (!confirm('街マップの背景画像を削除しますか？')) return;
        try {
            const patchRes = await fetch(`/api/world/cities/${cityIdRef}/map-background`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ map_background_image: null }),
            });
            if (!patchRes.ok) {
                alert(`背景の削除に失敗しました (${patchRes.status})`);
                return;
            }
            await fetchData();
        } catch (err) {
            console.error('Background clear error', err);
        }
    };

    const savePositions = async () => {
        const positions = Object.entries(editedPositions).map(([building_id, pos]) => ({
            building_id,
            x: pos.x,
            y: pos.y,
        }));
        if (positions.length === 0) {
            setIsEditMode(false);
            return;
        }
        setIsSaving(true);
        try {
            const res = await fetch('/api/world/buildings/positions', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ positions }),
            });
            if (!res.ok) {
                alert(`保存に失敗しました (${res.status})`);
                return;
            }
            setEditedPositions({});
            setIsEditMode(false);
            await fetchData();
        } catch (e) {
            console.error('Save positions error', e);
            alert('保存に失敗しました');
        } finally {
            setIsSaving(false);
        }
    };

    // RightSidebar と同様: 別ペルソナのモーダルが既に開いていたら閉じてから開き直す
    const openModal = (type: 'memory' | 'schedule' | 'tasks' | 'settings' | 'inventory') => {
        if (!selectedPersona) return;
        const newId = selectedPersona.id;
        const newName = selectedPersona.name;

        const anyOpen = showMemory || showSchedule || showTasks || showSettings || showInventory;
        const sameTarget = anyOpen && activeModalPersonaId === newId;

        const applyOpen = () => {
            setActiveModalPersonaId(newId);
            setActiveModalPersonaName(newName);
            if (type === 'memory') setShowMemory(true);
            if (type === 'schedule') setShowSchedule(true);
            if (type === 'tasks') setShowTasks(true);
            if (type === 'settings') setShowSettings(true);
            if (type === 'inventory') setShowInventory(true);
        };

        setSelectedPersona(null);

        if (anyOpen && !sameTarget) {
            setShowMemory(false);
            setShowSchedule(false);
            setShowTasks(false);
            setShowSettings(false);
            setShowInventory(false);
            setTimeout(applyOpen, 0);
            return;
        }

        applyOpen();
    };

    const userBuildingId = currentBuildingId ?? data?.user_current_building_id ?? null;
    // ズームアウト時のみ 1 超えになり、子要素のサイズ補正に使う (上限 2.0)
    const inverseScale = Math.min(2.0, Math.max(1, 1 / view.scale));
    // ある程度ズームインしたらリッチ表示 (内装画像+description)
    const isRichMode = view.scale >= RICH_MODE_SCALE_THRESHOLD;

    return (
        <div className={styles.container}>
            <div className={styles.starfield} />
            {(onClose || !isEditMode) && (
                <div className={styles.headerControls}>
                    {onClose && (
                        <button
                            className={styles.closeBtn}
                            onClick={onClose}
                            aria-label="街マップを閉じる"
                            title="閉じる (Esc)"
                        >
                            <X size={18} />
                        </button>
                    )}
                    {!isEditMode && (
                        <button
                            className={styles.editToggleBtn}
                            onClick={() => setIsEditMode(true)}
                            title="配置を編集する"
                        >
                            <Edit3 size={16} />
                        </button>
                    )}
                </div>
            )}
            {isEditMode ? (
                <div className={`${styles.titleBar} ${styles.editBar}`}>
                    <div className={styles.editLabel}>
                        <Edit3 size={14} /> 配置編集中{editedCount > 0 && ` · ${editedCount}件変更`}
                    </div>
                    <div className={styles.editActions}>
                        <input
                            ref={bgFileInputRef}
                            type="file"
                            accept="image/*"
                            style={{ display: 'none' }}
                            onChange={handleBgFileChange}
                        />
                        {data?.map_background_image && (
                            <div className={styles.bgThumbWrap} title="現在の背景画像">
                                <img src={data.map_background_image} alt="" className={styles.bgThumb} />
                                <button
                                    className={styles.bgThumbClear}
                                    onClick={handleBgClear}
                                    title="背景画像を削除"
                                    disabled={isBgUploading}
                                >
                                    <Trash2 size={11} />
                                </button>
                            </div>
                        )}
                        <button
                            className={styles.editBgBtn}
                            onClick={handleBgUploadClick}
                            disabled={isBgUploading}
                            title={data?.map_background_image ? '背景画像を変更' : '背景画像を設定'}
                        >
                            {isBgUploading
                                ? <Loader2 size={14} className={styles.spin} />
                                : <ImageIcon size={14} />
                            }
                            {' '}{data?.map_background_image ? '背景を変更' : '背景を設定'}
                        </button>
                        <button
                            className={styles.editCancelBtn}
                            onClick={cancelEdit}
                            disabled={isSaving}
                        >
                            キャンセル
                        </button>
                        <button
                            className={styles.editSaveBtn}
                            onClick={savePositions}
                            disabled={isSaving}
                        >
                            <Save size={14} /> {isSaving ? '保存中...' : '保存'}
                        </button>
                    </div>
                </div>
            ) : (
                <div className={styles.titleBar}>
                    <h2 className={styles.title}>S A I V e r s e &nbsp;C i t y</h2>
                    <div className={styles.subtitle}>
                        {data ? `${buildings.length} buildings · ${buildings.reduce((acc, b) => acc + b.occupants.length, 0)} residents` : ' '}
                    </div>
                </div>
            )}

            {error && <div className={styles.errorMsg}>{error}</div>}
            {!data && !error && <div className={styles.loading}>読み込み中...</div>}

            <div
                ref={viewportRef}
                className={`${styles.viewport} ${isDragging ? styles.dragging : ''}`}
            >
                <div
                    className={styles.world}
                    style={{
                        transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
                        // ズームアウト時のみ要素サイズを補正 (上限2倍)。
                        // ズームインでは普通に大きくなるので 1.0 で固定。
                        ['--inv-scale' as string]: String(Math.min(2.0, Math.max(1, 1 / view.scale))),
                    } as React.CSSProperties}
                >
                    {data?.map_background_image && (
                        <>
                            <img
                                className={styles.worldBg}
                                src={data.map_background_image}
                                alt=""
                                draggable={false}
                                onError={(e) => { e.currentTarget.style.display = 'none'; }}
                            />
                            <div className={styles.worldBgOverlay} />
                        </>
                    )}
                    {buildings.map((b, idx) => {
                        const isCurrent = b.id === userBuildingId;
                        const hasImage = !!b.image_path && !imageFailed.has(b.id);
                        const maxVisible = isRichMode ? MAX_VISIBLE_OCCUPANTS : MAX_VISIBLE_OCCUPANTS_MINIMAL;
                        const visible = b.occupants.slice(0, maxVisible);
                        const overflow = b.occupants.length - visible.length;
                        const pos = resolvePosition(b, idx, buildings.length);
                        const isMoved = !!editedPositions[b.id];
                        // ミニマルモード時は内装画像も枠も出さない (アイコン+ラベルのみ)
                        const showRichBg = isRichMode && hasImage;
                        const showHouseIcon = !showRichBg;
                        return (
                            <div
                                key={b.id}
                                data-building-cell-id={b.id}
                                className={`${styles.buildingCell} ${isRichMode ? styles.rich : styles.minimal} ${isCurrent ? styles.current : ''} ${showRichBg ? styles.withImage : ''} ${isEditMode ? styles.editing : ''} ${isMoved ? styles.moved : ''}`}
                                style={{ left: pos.x, top: pos.y }}
                                onClick={() => {
                                    if (isEditMode) return;
                                    if (dragRef.current.dragged) return;
                                    if (cellDragRef.current.dragged) return;
                                    onSelectBuilding(b.id);
                                }}
                                role="button"
                                tabIndex={0}
                                onKeyDown={e => {
                                    if (e.key === 'Enter' || e.key === ' ') {
                                        e.preventDefault();
                                        if (!isEditMode) onSelectBuilding(b.id);
                                    }
                                }}
                            >
                                {showRichBg && (
                                    <div className={styles.cellBgWrapper}>
                                        <img
                                            className={styles.cellBg}
                                            src={b.image_path!}
                                            alt=""
                                            draggable={false}
                                            onError={() => {
                                                setImageFailed(prev => {
                                                    if (prev.has(b.id)) return prev;
                                                    const next = new Set(prev);
                                                    next.add(b.id);
                                                    return next;
                                                });
                                            }}
                                        />
                                        <div className={styles.cellBgOverlay} />
                                    </div>
                                )}
                                {showHouseIcon && (
                                    <div className={styles.houseIcon}>
                                        <HomeIcon size={Math.round(32 * inverseScale)} />
                                    </div>
                                )}
                                <div className={styles.buildingName}>{b.name}</div>
                                {isRichMode && b.description && (
                                    <div className={styles.buildingDescription}>{b.description}</div>
                                )}

                                {b.occupants.length === 0 ? (
                                    isRichMode && <div className={styles.empty}>無人</div>
                                ) : (
                                    <div className={styles.occupantStrip}>
                                        {visible.map(occ => (
                                            <div
                                                key={occ.id}
                                                className={styles.occupant}
                                                title={occ.name}
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    if (isEditMode) return;
                                                    if (dragRef.current.dragged) return;
                                                    if (cellDragRef.current.dragged) return;
                                                    setSelectedPersona(occ);
                                                }}
                                                onKeyDown={(e) => {
                                                    if (e.key === 'Enter' || e.key === ' ') {
                                                        e.preventDefault();
                                                        e.stopPropagation();
                                                        setSelectedPersona(occ);
                                                    }
                                                }}
                                                role="button"
                                                tabIndex={0}
                                            >
                                                <img
                                                    src={occ.avatar || '/api/static/builtin_icons/host.png'}
                                                    alt={occ.name}
                                                    draggable={false}
                                                    onError={(e) => {
                                                        e.currentTarget.src = 'https://placehold.co/48x48?text=?';
                                                    }}
                                                />
                                            </div>
                                        ))}
                                        {overflow > 0 && (
                                            <div className={styles.occupantOverflow} title={`他 ${overflow} 人`}>
                                                +{overflow}
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            </div>

            {selectedPersona && (
                <PersonaMenu
                    isOpen={!!selectedPersona}
                    onClose={() => setSelectedPersona(null)}
                    personaId={selectedPersona.id}
                    personaName={selectedPersona.name}
                    avatarUrl={selectedPersona.avatar || '/api/static/builtin_icons/host.png'}
                    onOpenMemory={() => openModal('memory')}
                    onOpenSchedule={() => openModal('schedule')}
                    onOpenTasks={() => openModal('tasks')}
                    onOpenSettings={() => openModal('settings')}
                    onOpenInventory={() => openModal('inventory')}
                />
            )}

            {activeModalPersonaId && (
                <>
                    <MemoryModal
                        isOpen={showMemory}
                        onClose={() => setShowMemory(false)}
                        personaId={activeModalPersonaId}
                        personaName={activeModalPersonaName || undefined}
                    />
                    <ScheduleModal
                        isOpen={showSchedule}
                        onClose={() => setShowSchedule(false)}
                        personaId={activeModalPersonaId}
                    />
                    <TasksModal
                        isOpen={showTasks}
                        onClose={() => setShowTasks(false)}
                        personaId={activeModalPersonaId}
                    />
                    <SettingsModal
                        isOpen={showSettings}
                        onClose={() => setShowSettings(false)}
                        personaId={activeModalPersonaId}
                    />
                    <InventoryModal
                        isOpen={showInventory}
                        onClose={() => setShowInventory(false)}
                        personaId={activeModalPersonaId}
                    />
                </>
            )}
        </div>
    );
}
