
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useEffect, useRef, useCallback } from 'react';
import styles from './WorldEditor.module.css';
import { Layers, MapPin, Cpu, Box, FileText, Wrench, ArrowRight, BookOpen, Upload, ChevronLeft, ChevronRight } from 'lucide-react';
import ImageUpload from '../common/ImageUpload';
import FileUpload from '../common/FileUpload';
import { DB_TABLE_PAGE_SIZE, fetchAllTableRows, fetchTablePage } from '../../lib/dbTable';

// Helper Form Components - defined outside component to prevent re-creation on each render
const Field = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <div className={styles.field}><label>{label}</label>{children}</div>
);
const Input = (props: React.InputHTMLAttributes<HTMLInputElement>) => <input type="text" {...props} />;
const NumInput = (props: React.InputHTMLAttributes<HTMLInputElement>) => <input type="number" {...props} />;
const TextArea = (props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} />;
const Select = ({ children, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) => <select {...props}>{children}</select>;

interface City {
    CITYID: number;
    /** 内部の識別子 (英数字・アンダースコア)。作成後は変更不可 */
    CITY_SLUG: string;
    /** 表示名 (自由な文字列)。空なら CITY_SLUG を代わりに表示する */
    CITYNAME: string;
    DESCRIPTION: string;
    UI_PORT: number;
    API_PORT: number;
    START_IN_ONLINE_MODE: boolean;
    TIMEZONE: string;
    LANGUAGE?: string;
    MAP_BACKGROUND_IMAGE?: string | null;
}

interface Building {
    BUILDINGID: string;
    BUILDINGNAME: string;
    DESCRIPTION: string;
    CAPACITY: number;
    SYSTEM_INSTRUCTION: string;
    CITYID: number;
    AUTO_INTERVAL_SEC: number;
    IMAGE_PATH?: string;  // Building interior image for visual context
    EXTRA_PROMPT_FILES?: string;  // JSON array of extra prompt file names
    /** 部屋の様子に出すアイテムの個数の上限。null なら既定の 10 個
     *  (docs/intent/room_item_display_cap.md 設計 4)。0 も有効な値。 */
    ITEM_DISPLAY_LIMIT?: number | null;
}

interface Tool {
    TOOLID: number;
    TOOLNAME: string;
    MODULE_PATH: string;
    FUNCTION_NAME: string;
    DESCRIPTION: string;
}

interface AI {
    AIID: string;
    AINAME: string;
    DESCRIPTION: string;
    SYSTEMPROMPT: string;
    HOME_CITYID: number;
    DEFAULT_MODEL: string;
    LIGHTWEIGHT_MODEL: string;
    AUTONOMY_ENABLED: boolean;  // 自律行動 (自分から考えて動くこと) の ON/OFF
    AVATAR_IMAGE: string;
    APPEARANCE_IMAGE_PATH?: string;  // Persona appearance image for visual context
    IS_DISPATCHED: boolean;
}

interface Item {
    ITEM_ID: string;
    NAME: string;
    TYPE: string;
    DESCRIPTION: string;
    FILE_PATH: string;
    STATE_JSON: string;
    // Location derived/fetched separately or in same table?
    // In models.py ItemLocation is separate, but generic DB view might not join.
    // However, world_editor.py uses `get_item_details` which joins.
    // Our generic DB API `get_table_data` only returns the table.
    // We should probably rely on `get_item_details` equivalent or just exposing ItemLocation table.
    // For now, let's just edit ITEM table properties. Owner editing might require specialized API or editing ItemLocation.
    // `api/world/items` expects owner_kind/id.
}

interface Blueprint {
    BLUEPRINT_ID: number;
    NAME: string;
    DESCRIPTION: string;
    CITYID: number;
    ENTITY_TYPE: string;
    BASE_SYSTEM_PROMPT: string;
}

interface Playbook {
    id: number;
    name: string;
    description: string;
    scope: string;
    router_callable: boolean;
    user_selectable: boolean;
    nodes_json?: string;
    schema_json?: string;
}

interface ModelChoice {
    id: string;
    name: string;
}

/** Wrapper around fetch that checks res.ok and shows alert on error.
 * Returns the parsed JSON body on success (true when the body is not JSON), false on error. */
async function apiCall(url: string, options?: RequestInit): Promise<any> {
    try {
        const res = await apiFetch(url, options);
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            let msg = uiText("components.settings.WorldEditor.text001");
            if (Array.isArray(err.detail)) {
                msg = err.detail.map((e: any) => {
                    const loc = e.loc?.slice(1).join('.') || '?';
                    return `${loc}: ${e.msg}`;
                }).join('\n');
            } else if (typeof err.detail === 'string') {
                msg = err.detail;
            } else if (err.detail) {
                msg = JSON.stringify(err.detail);
            }
            alert(uiText("components.settings.WorldEditor.text002", { p1: msg }));
            return false;
        }
        const text = await res.text();
        if (!text) return true;
        try {
            return JSON.parse(text);
        } catch {
            return true;
        }
    } catch (e) {
        console.error('API call failed:', url, e);
        alert(uiText("components.settings.WorldEditor.text003"));
        return false;
    }
}

/**
 * 一覧タブ 1 つ分のページ送り状態。
 *
 * `/api/db/tables/{table}` は 1 回で 100 行までしか返さないので、一覧は
 * ページ単位で読む。`total` (総件数) を持っておかないと「これで全部なのか、
 * 続きがあるのか」が画面から分からない。
 *
 * ただし総件数は応答ヘッダ頼みで、古いバックエンドやキャッシュされた古い
 * フロントとの組み合わせでは読めない (`null` になる)。**「次へ」を出すかは
 * 総件数ではなく「ページが満杯だったか」で決める** — 総件数が無い状況で
 * 「見えている件数 = 全件」と推定すると、続きがあるのに先頭ページで
 * 「続きなし」になり、一覧が静かに切り捨てられる (lib/dbTable.ts 参照)。
 */
interface TableList<T> {
    /** 今表示しているページの行 */
    rows: T[];
    /** テーブル全体の件数。応答ヘッダが読めなければ null */
    total: number | null;
    /** 今のページの先頭が何行目か (0 始まり) */
    offset: number;
    loading: boolean;
    /** ページを読む。引数を省くと今のページを読み直す */
    load: (nextOffset?: number) => Promise<void>;
}

function useTableList<T>(table: string): TableList<T> {
    const [rows, setRows] = useState<T[]>([]);
    const [total, setTotal] = useState<number | null>(0);
    const [offset, setOffset] = useState(0);
    const [loading, setLoading] = useState(false);
    // load を毎回作り直さずに「今のページ」を参照するための控え
    const offsetRef = useRef(0);

    const load = useCallback(async (nextOffset?: number) => {
        let target = nextOffset ?? offsetRef.current;
        setLoading(true);
        try {
            let page = await fetchTablePage<T>(table, target, DB_TABLE_PAGE_SIZE);
            // 末尾ページの行を消すと、そのページが空になって「101〜100 件目」の
            // ような表示が残る。件数から最終ページを割り出して読み直す。
            // 件数が取れない場合は 1 ページ手前へ下がる (最終ページを一発で
            // 割り出せないだけで、空ページに留まるよりは正しい位置に近い)
            if (page.rows.length === 0 && target > 0) {
                const lastPage = page.total === null
                    ? Math.max(0, target - DB_TABLE_PAGE_SIZE)
                    : page.total > 0
                        ? Math.floor((page.total - 1) / DB_TABLE_PAGE_SIZE) * DB_TABLE_PAGE_SIZE
                        : 0;
                if (lastPage !== target) {
                    target = lastPage;
                    page = await fetchTablePage<T>(table, target, DB_TABLE_PAGE_SIZE);
                }
            }
            offsetRef.current = target;
            setOffset(target);
            setRows(page.rows);
            setTotal(page.total);
        } catch (e) {
            console.error(`load ${table} failed:`, e);
        } finally {
            setLoading(false);
        }
    }, [table]);

    return { rows, total, offset, loading, load };
}

/** 一覧の下に出すページ送り。1 ページに収まっていても件数は見せる */
const Pagination = ({ list, onNavigate }: { list: TableList<any>; onNavigate: () => void }) => {
    useLocale();
    // 総件数が読めた上でゼロ、または件数不明で先頭ページが空 = 見せるものが無い
    if (list.total === 0) return null;
    if (list.total === null && list.offset === 0 && list.rows.length === 0) return null;
    const first = list.rows.length === 0 ? 0 : list.offset + 1;
    const last = list.offset + list.rows.length;
    const hasPrev = list.offset > 0;
    // 件数不明のときはページが満杯だったかで続きを判断する
    const hasNext = list.total === null
        ? list.rows.length === DB_TABLE_PAGE_SIZE
        : list.offset + list.rows.length < list.total;
    const go = (nextOffset: number) => {
        onNavigate();
        list.load(nextOffset);
    };
    return (
        <div className={styles.pager}>
            <button data-i18n="components.settings.WorldEditor.text004 components.settings.WorldEditor.text005"
                type="button"
                className={styles.pagerBtn}
                disabled={!hasPrev || list.loading}
                onClick={() => go(Math.max(0, list.offset - DB_TABLE_PAGE_SIZE))}
                aria-label={uiText("components.settings.WorldEditor.text004")}
            ><ChevronLeft size={14} />{uiText("components.settings.WorldEditor.text005")}</button>
            <span data-i18n="components.settings.WorldEditor.text006 components.settings.WorldEditor.text007" className={styles.pagerStatus}>
                {first}〜{last}{uiText("components.settings.WorldEditor.text006")}{list.total === null ? '' : uiText("components.settings.WorldEditor.text007", { p1: list.total })}
            </span>
            <button data-i18n="components.settings.WorldEditor.text008 components.settings.WorldEditor.text009"
                type="button"
                className={styles.pagerBtn}
                disabled={!hasNext || list.loading}
                onClick={() => go(list.offset + DB_TABLE_PAGE_SIZE)}
                aria-label={uiText("components.settings.WorldEditor.text008")}
            >{uiText("components.settings.WorldEditor.text009")}<ChevronRight size={14} /></button>
        </div>
    );
};

export default function WorldEditor() {
    useLocale();
    const [subTab, setSubTab] = useState('city');

    // Data State — 各タブの一覧はページ送りで読む
    const cityList = useTableList<City>('city');
    const buildingList = useTableList<Building>('building');
    const toolList = useTableList<Tool>('tool');
    const aiList = useTableList<AI>('ai');
    const itemList = useTableList<Item>('item');
    const blueprintList = useTableList<Blueprint>('blueprint');

    // フォームの選択肢用。こちらは「全部そろっていること」が前提なので全件読む
    // (100 件で切れると、選べるはずの Building がリストから消える)
    const [cityOptions, setCityOptions] = useState<City[]>([]);
    const [buildingOptions, setBuildingOptions] = useState<Building[]>([]);
    const [toolOptions, setToolOptions] = useState<Tool[]>([]);
    const [aiOptions, setAiOptions] = useState<AI[]>([]);
    const [bagOptions, setBagOptions] = useState<Item[]>([]);

    const [modelChoices, setModelChoices] = useState<ModelChoice[]>([]);
    const [playbooks, setPlaybooks] = useState<Playbook[]>([]);
    const [availablePrompts, setAvailablePrompts] = useState<string[]>([]);

    // Selection State
    const [selectedCity, setSelectedCity] = useState<City | null>(null);
    const [selectedBuilding, setSelectedBuilding] = useState<Building | null>(null);
    const [selectedAI, setSelectedAI] = useState<AI | null>(null);
    const [selectedItem, setSelectedItem] = useState<Item | null>(null);
    const [selectedBlueprint, setSelectedBlueprint] = useState<Blueprint | null>(null);
    const [selectedTool, setSelectedTool] = useState<Tool | null>(null);
    const [selectedPlaybook, setSelectedPlaybook] = useState<Playbook | null>(null);

    // Form & Action State
    const [formData, setFormData] = useState<any>({});

    // Playbook import D&D
    const [isPlaybookDragOver, setIsPlaybookDragOver] = useState(false);
    const playbookDragCounter = useRef(0);
    const playbookFileInputRef = useRef<HTMLInputElement>(null);

    const handlePlaybookImport = useCallback(async (file: File) => {
        try {
            const text = await file.text();
            const res = await apiFetch('/api/world/playbooks/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ playbook_json: text })
            });
            const data = await res.json();
            if (res.ok) {
                const actionText = data.action === 'created' ? uiText("components.settings.WorldEditor.text010") : uiText("components.settings.WorldEditor.text011");
                alert(uiText("components.settings.WorldEditor.text012", { p1: actionText, p2: data.name }));
                loadPlaybooks();
            } else {
                alert(uiText("components.settings.WorldEditor.text013", { p1: data.detail || uiText("common.extra015") }));
            }
        } catch (err) {
            alert(uiText("components.settings.WorldEditor.text014", { p1: err }));
        }
        if (playbookFileInputRef.current) playbookFileInputRef.current.value = '';
    }, []);

    const handlePlaybookDragEnter = useCallback((e: React.DragEvent) => {
        e.preventDefault(); e.stopPropagation();
        playbookDragCounter.current++;
        if (playbookDragCounter.current === 1) setIsPlaybookDragOver(true);
    }, []);
    const handlePlaybookDragOver = useCallback((e: React.DragEvent) => {
        e.preventDefault(); e.stopPropagation();
    }, []);
    const handlePlaybookDragLeave = useCallback((e: React.DragEvent) => {
        e.preventDefault(); e.stopPropagation();
        playbookDragCounter.current--;
        if (playbookDragCounter.current === 0) setIsPlaybookDragOver(false);
    }, []);
    const handlePlaybookDrop = useCallback((e: React.DragEvent) => {
        e.preventDefault(); e.stopPropagation();
        playbookDragCounter.current = 0; setIsPlaybookDragOver(false);
        const file = e.dataTransfer.files[0];
        if (file) handlePlaybookImport(file);
    }, [handlePlaybookImport]);

    // Load Data
    useEffect(() => {
        if (subTab === 'city') { cityList.load(0); }
        if (subTab === 'building') { buildingList.load(0); loadCityOptions(); loadToolOptions(); loadAvailablePrompts(); }
        if (subTab === 'ai') { aiList.load(0); loadCityOptions(); loadBuildingOptions(); loadModels(); }
        if (subTab === 'item') { itemList.load(0); loadBuildingOptions(); loadAiOptions(); loadBagOptions(); }
        if (subTab === 'blueprint') { blueprintList.load(0); loadCityOptions(); loadBuildingOptions(); } // Buildings for spawn
        if (subTab === 'tool') { toolList.load(0); }
        if (subTab === 'playbook') { loadPlaybooks(); }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [subTab]);

    const loadCityOptions = async () => { try { setCityOptions(await fetchAllTableRows<City>('city')); } catch (e) { console.error('loadCityOptions failed:', e); } };
    const loadBuildingOptions = async () => { try { setBuildingOptions(await fetchAllTableRows<Building>('building')); } catch (e) { console.error('loadBuildingOptions failed:', e); } };
    const loadToolOptions = async () => { try { setToolOptions(await fetchAllTableRows<Tool>('tool')); } catch (e) { console.error('loadToolOptions failed:', e); } };
    const loadAiOptions = async () => { try { setAiOptions(await fetchAllTableRows<AI>('ai')); } catch (e) { console.error('loadAiOptions failed:', e); } };
    // Bag は「アイテムの入れ物になっているアイテム」なので、選択肢を作るには
    // 一覧の 1 ページではなく全アイテムから拾う必要がある
    const loadBagOptions = async () => { try { const rows = await fetchAllTableRows<Item>('item'); setBagOptions(rows.filter(i => i.TYPE === 'bag')); } catch (e) { console.error('loadBagOptions failed:', e); } };
    const loadModels = async () => { try { const res = await apiFetch('/api/info/models'); if (res.ok) setModelChoices(await res.json()); } catch (e) { console.error('loadModels failed:', e); } };
    const loadPlaybooks = async () => { try { const res = await apiFetch('/api/world/playbooks'); if (res.ok) setPlaybooks(await res.json()); } catch (e) { console.error('loadPlaybooks failed:', e); } };
    const loadAvailablePrompts = async () => { try { const res = await apiFetch('/api/world/prompts/available'); if (res.ok) setAvailablePrompts(await res.json()); } catch (e) { console.error('loadAvailablePrompts failed:', e); } };

    // --- City Handlers ---
    const handleCitySelect = (city: City) => {
        setSelectedCity(city);
        setFormData({ name: city.CITYNAME, slug: city.CITY_SLUG, description: city.DESCRIPTION, ui_port: city.UI_PORT, api_port: city.API_PORT, timezone: city.TIMEZONE, language: city.LANGUAGE || 'ja', online_mode: city.START_IN_ONLINE_MODE, map_background_image: city.MAP_BACKGROUND_IMAGE || '' });
    };
    const handleCreateCity = async () => { if (await apiCall('/api/world/cities', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) })) { cityList.load(); setFormData({}); } };
    const handleUpdateCity = async () => { if (await apiCall(`/api/world/cities/${selectedCity!.CITYID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) })) { cityList.load(); } };
    const handleDeleteCity = async () => { if (confirm(uiText("components.settings.WorldEditor.text015")) && await apiCall(`/api/world/cities/${selectedCity!.CITYID}`, { method: 'DELETE' })) { setSelectedCity(null); setFormData({}); cityList.load(); } };

    // --- Building Handlers ---
    // いま選ばれている Building の ID。紐付け表を読み切る前に別の Building を
    // 選ぶと、先に投げた読みの応答が後から返って編集フォームを前の Building の
    // 値で上書きしてしまう (そのまま保存すると、選んでいる Building に別の
    // Building の設定が書かれる)。応答を当てる前にここと照合して、古い応答は捨てる。
    const selectedBuildingIdRef = useRef<string | null>(null);
    // アイテム・Playbook の選択にも同じ競合がある (速い選び直しで古い応答が
    // 新しい選択のフォームを上書きする)。同じ照合で古い応答を捨てる。
    const selectedItemIdRef = useRef<string | null>(null);
    const selectedPlaybookIdRef = useRef<string | number | null>(null);
    const handleBuildingSelect = (b: Building) => {
        setSelectedBuilding(b);
        selectedBuildingIdRef.current = b.BUILDINGID;
        // Parse extra prompt files from JSON
        let extraPrompts: string[] = [];
        if (b.EXTRA_PROMPT_FILES) {
            try { extraPrompts = JSON.parse(b.EXTRA_PROMPT_FILES); } catch (e) { console.error('Failed to parse EXTRA_PROMPT_FILES:', e); extraPrompts = []; }
        }
        // 紐付け表は全件そろっていないと「チェックが外れている」という嘘の
        // 表示になるので、ページ送りではなく最後まで読み切る
        fetchAllTableRows<any>('building_tool_link').then(links => {
            // 読んでいる間に別の Building へ移っていたら、この応答はもう古い
            if (selectedBuildingIdRef.current !== b.BUILDINGID) return;
            const ids = links.filter((l: any) => l.BUILDINGID === b.BUILDINGID).map((l: any) => l.TOOLID);
            // item_display_limit は 0 も有効な値 (アイテムを様子に出さない部屋) なので
            // `||` で潰さない。null = 設定なし = 既定の 10 個。
            setFormData({ name: b.BUILDINGNAME, description: b.DESCRIPTION, capacity: b.CAPACITY, system_instruction: b.SYSTEM_INSTRUCTION, city_id: b.CITYID, auto_interval: b.AUTO_INTERVAL_SEC, tool_ids: ids, image_path: b.IMAGE_PATH || '', extra_prompt_files: extraPrompts, item_display_limit: b.ITEM_DISPLAY_LIMIT ?? null });
        }).catch(e => console.error('load building_tool_link failed:', e));
    };
    const handleCreateBuilding = async () => { if (await apiCall('/api/world/buildings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: formData.name, description: formData.description || "", capacity: formData.capacity || 1, system_instruction: formData.system_instruction || "", city_id: formData.city_id, building_id: formData.building_id || null }) })) { buildingList.load(); setFormData({}); } };
    const handleUpdateBuilding = async () => { if (await apiCall(`/api/world/buildings/${selectedBuilding!.BUILDINGID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...formData, tool_ids: formData.tool_ids || [] }) })) { buildingList.load(); } };
    const handleDeleteBuilding = async () => {
        const deletedId = selectedBuilding!.BUILDINGID;
        if (confirm(uiText("components.settings.WorldEditor.text016")) && await apiCall(`/api/world/buildings/${deletedId}`, { method: 'DELETE' })) {
            setSelectedBuilding(null);
            selectedBuildingIdRef.current = null;
            setFormData({});
            buildingList.load();
            // Notify main page so it can update if the deleted building was current
            window.dispatchEvent(new CustomEvent('building-deleted', { detail: { buildingId: deletedId } }));
        }
    };

    // --- AI Handlers ---
    const handleAISelect = (ai: AI) => {
        setSelectedAI(ai);
        setFormData({
            name: ai.AINAME, description: ai.DESCRIPTION, system_prompt: ai.SYSTEMPROMPT,
            home_city_id: ai.HOME_CITYID, default_model: ai.DEFAULT_MODEL, lightweight_model: ai.LIGHTWEIGHT_MODEL,
            autonomy_enabled: ai.AUTONOMY_ENABLED, avatar_path: ai.AVATAR_IMAGE,
            appearance_image_path: ai.APPEARANCE_IMAGE_PATH || ''
        });
    };
    const handleCreateAI = async () => { if (await apiCall('/api/world/ais', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: formData.name, system_prompt: formData.system_prompt, home_city_id: formData.home_city_id }) })) { aiList.load(); setFormData({}); } };
    const handleUpdateAI = async () => {
        const result = await apiCall(`/api/world/ais/${selectedAI!.AIID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) });
        if (!result) return;
        // 保存しなかったモデル設定や、新しい設定に切り替えられなかったことの知らせ (ペルソナ設定の画面と同じ文面)
        if (typeof result === 'object' && typeof result.warning === 'string' && result.warning) {
            alert(`設定は保存されましたが、警告があります:\n${result.warning}`);
        }
        aiList.load();
    };
    const handleDeleteAI = async () => { if (confirm(uiText("components.settings.WorldEditor.text017")) && await apiCall(`/api/world/ais/${selectedAI!.AIID}`, { method: 'DELETE' })) { setSelectedAI(null); setFormData({}); aiList.load(); } };
    const handleMoveAI = async () => {
        if (!selectedAI || !formData.target_building_name) return;
        if (await apiCall(`/api/world/ais/${selectedAI.AIID}/move`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target_building_name: formData.target_building_name }) })) {
            alert(uiText("components.settings.WorldEditor.text018"));
        }
    };

    // --- Blueprint Handlers ---
    const handleBlueprintSelect = (bp: Blueprint) => {
        setSelectedBlueprint(bp);
        setFormData({ name: bp.NAME, description: bp.DESCRIPTION, city_id: bp.CITYID, entity_type: bp.ENTITY_TYPE, system_prompt: bp.BASE_SYSTEM_PROMPT });
    };
    const handleCreateBlueprint = async () => { if (await apiCall('/api/world/blueprints', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) })) { blueprintList.load(); setFormData({}); } };
    const handleUpdateBlueprint = async () => { if (await apiCall(`/api/world/blueprints/${selectedBlueprint!.BLUEPRINT_ID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) })) { blueprintList.load(); } };
    const handleDeleteBlueprint = async () => { if (confirm(uiText("components.settings.WorldEditor.text019")) && await apiCall(`/api/world/blueprints/${selectedBlueprint!.BLUEPRINT_ID}`, { method: 'DELETE' })) { setSelectedBlueprint(null); setFormData({}); blueprintList.load(); } };
    const handleSpawnBlueprint = async () => {
        if (!selectedBlueprint || !formData.spawn_entity_name || !formData.spawn_building_name) return;
        if (await apiCall(`/api/world/blueprints/${selectedBlueprint.BLUEPRINT_ID}/spawn`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ entity_name: formData.spawn_entity_name, building_name: formData.spawn_building_name })
        })) { alert(uiText("components.settings.WorldEditor.text020")); }
    };

    // --- Tool Handlers ---
    const handleToolSelect = (t: Tool) => { setSelectedTool(t); setFormData({ name: t.TOOLNAME, description: t.DESCRIPTION, module_path: t.MODULE_PATH, function_name: t.FUNCTION_NAME }); };
    const handleCreateTool = async () => { if (await apiCall('/api/world/tools', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) })) { toolList.load(); setFormData({}); } };
    const handleUpdateTool = async () => { if (await apiCall(`/api/world/tools/${selectedTool!.TOOLID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) })) { toolList.load(); } };
    const handleDeleteTool = async () => { if (confirm(uiText("components.settings.WorldEditor.text021")) && await apiCall(`/api/world/tools/${selectedTool!.TOOLID}`, { method: 'DELETE' })) { setSelectedTool(null); setFormData({}); toolList.load(); } };

    // --- Item Handlers ---
    const handleItemSelect = async (i: Item) => {
        setSelectedItem(i);
        selectedItemIdRef.current = i.ITEM_ID;
        // Fetch item details to get owner info
        try {
            const res = await apiFetch(`/api/world/items/${i.ITEM_ID}`);
            if (selectedItemIdRef.current !== i.ITEM_ID) return; // 選び直し済み — 古い応答を捨てる
            if (res.ok) {
                const details = await res.json();
                if (selectedItemIdRef.current !== i.ITEM_ID) return;
                setFormData({
                    name: details.NAME || i.NAME,
                    item_type: details.TYPE || i.TYPE,
                    description: details.DESCRIPTION || i.DESCRIPTION,
                    owner_kind: details.OWNER_KIND || 'world',
                    owner_id: details.OWNER_ID || '',
                    state_json: details.STATE_JSON || i.STATE_JSON,
                    file_path: details.FILE_PATH || i.FILE_PATH
                });
            } else {
                // Fallback if API fails
                setFormData({ name: i.NAME, item_type: i.TYPE, description: i.DESCRIPTION, owner_kind: 'world', owner_id: '', state_json: i.STATE_JSON, file_path: i.FILE_PATH });
            }
        } catch (e) {
            // Fallback on error
            setFormData({ name: i.NAME, item_type: i.TYPE, description: i.DESCRIPTION, owner_kind: 'world', owner_id: '', state_json: i.STATE_JSON, file_path: i.FILE_PATH });
        }
    };
    const handleCreateItem = async () => { if (await apiCall('/api/world/items', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) })) { itemList.load(); loadBagOptions(); setFormData({}); } };
    const handleUpdateItem = async () => { if (await apiCall(`/api/world/items/${selectedItem!.ITEM_ID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) })) { itemList.load(); loadBagOptions(); } };
    const handleDeleteItem = async () => { if (confirm(uiText("components.settings.WorldEditor.text022")) && await apiCall(`/api/world/items/${selectedItem!.ITEM_ID}`, { method: 'DELETE' })) { setSelectedItem(null); setFormData({}); itemList.load(); loadBagOptions(); } };

    // --- Playbook Handlers ---
    const handlePlaybookSelect = async (pb: Playbook) => {
        selectedPlaybookIdRef.current = pb.id;
        // Fetch full details
        try {
            const res = await apiFetch(`/api/world/playbooks/${pb.id}`);
            if (selectedPlaybookIdRef.current !== pb.id) return; // 選び直し済み — 古い応答を捨てる
            if (res.ok) {
                const detail = await res.json();
                if (selectedPlaybookIdRef.current !== pb.id) return;
                setSelectedPlaybook(detail);
                setFormData({
                    name: detail.name,
                    description: detail.description,
                    scope: detail.scope,
                    router_callable: detail.router_callable,
                    user_selectable: detail.user_selectable,
                    nodes_json: detail.nodes_json,
                    schema_json: detail.schema_json,
                });
            }
        } catch (e) { console.error('handlePlaybookSelect failed:', e); }
    };
    const handleCreatePlaybook = async () => {
        if (await apiCall('/api/world/playbooks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) })) {
            loadPlaybooks(); setFormData({}); setSelectedPlaybook(null);
        }
    };
    const handleUpdatePlaybook = async () => {
        if (await apiCall(`/api/world/playbooks/${selectedPlaybook!.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) })) {
            loadPlaybooks();
        }
    };
    const handleDeletePlaybook = async () => { if (confirm(uiText("components.settings.WorldEditor.text023")) && await apiCall(`/api/world/playbooks/${selectedPlaybook!.id}`, { method: 'DELETE' })) { setSelectedPlaybook(null); setFormData({}); loadPlaybooks(); } };




    const renderFormActions = (selected: any, create: any, update: any, remove: any) => (
        <div className={styles.actions}>
            {selected ? <><button data-i18n="components.settings.WorldEditor.text024" className={styles.primaryBtn} onClick={update}>{uiText("components.settings.WorldEditor.text024")}</button><button data-i18n="components.settings.WorldEditor.text025" className={styles.dangerBtn} onClick={remove}>{uiText("components.settings.WorldEditor.text025")}</button></>
                : <button data-i18n="components.settings.WorldEditor.text026" className={styles.primaryBtn} onClick={create}>{uiText("components.settings.WorldEditor.text026")}</button>}
        </div>
    );

    return (
        <div className={styles.container}>
            <div className={styles.tabs}>
                <button className={`${styles.tab} ${subTab === 'city' ? styles.active : ''}`} onClick={() => { setSubTab('city'); setSelectedCity(null); setFormData({}); }}><MapPin size={16} /> {uiText("components.settings.WorldEditor.label001")}</button>
                <button className={`${styles.tab} ${subTab === 'building' ? styles.active : ''}`} onClick={() => { setSubTab('building'); setSelectedBuilding(null); selectedBuildingIdRef.current = null; setFormData({}); }}><Layers size={16} /> {uiText("components.settings.WorldEditor.label002")}</button>
                <button data-i18n="components.settings.WorldEditor.text027" className={`${styles.tab} ${subTab === 'ai' ? styles.active : ''}`} onClick={() => { setSubTab('ai'); setSelectedAI(null); setFormData({}); }}><Cpu size={16} /> {uiText("components.settings.WorldEditor.text027")}</button>
                <button className={`${styles.tab} ${subTab === 'blueprint' ? styles.active : ''}`} onClick={() => { setSubTab('blueprint'); setSelectedBlueprint(null); setFormData({ entity_type: 'ai' }); }}><FileText size={16} /> {uiText("components.settings.WorldEditor.label003")}</button>
                <button data-i18n="components.settings.WorldEditor.text028" className={`${styles.tab} ${subTab === 'tool' ? styles.active : ''}`} onClick={() => { setSubTab('tool'); setSelectedTool(null); setFormData({}); }}><Wrench size={16} /> {uiText("components.settings.WorldEditor.text028")}</button>
                <button data-i18n="components.settings.WorldEditor.text029" className={`${styles.tab} ${subTab === 'item' ? styles.active : ''}`} onClick={() => { setSubTab('item'); setSelectedItem(null); setFormData({ item_type: 'object', owner_kind: 'world' }); }}><Box size={16} /> {uiText("components.settings.WorldEditor.text029")}</button>
                <button className={`${styles.tab} ${subTab === 'playbook' ? styles.active : ''}`} onClick={() => { setSubTab('playbook'); setSelectedPlaybook(null); setFormData({}); }}><BookOpen size={16} /> {uiText("components.settings.WorldEditor.label004")}</button>
            </div>

            <div className={styles.content}>
                {subTab === 'city' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3 data-i18n="components.settings.WorldEditor.text030">{uiText("components.settings.WorldEditor.text030")}</h3>
                            {cityList.rows.map(c => <div key={c.CITYID} className={`${styles.item} ${selectedCity?.CITYID === c.CITYID ? styles.selected : ''}`} onClick={() => handleCitySelect(c)}>{c.CITYNAME || c.CITY_SLUG}</div>)}
                            <Pagination list={cityList} onNavigate={() => { setSelectedCity(null); setFormData({}); }} />
                            <button data-i18n="components.settings.WorldEditor.text031" className={styles.newBtn} onClick={() => { setSelectedCity(null); setFormData({}); }}>{uiText("components.settings.WorldEditor.text031")}</button>
                        </div>
                        <div className={styles.form}>
                            <h3 data-i18n="components.settings.WorldEditor.text032 components.settings.WorldEditor.text033">{selectedCity ? uiText("components.settings.WorldEditor.text032") : uiText("components.settings.WorldEditor.text033")}</h3>
                            <Field label={uiText("components.settings.WorldEditor.text034")}><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            {/* 内部の識別子は作成時にしか決められない。起動引数・部屋の
                                BUILDINGID・ペルソナ ID・ログの保存先がこの文字列から
                                作られるため、後から変えると食い違う
                                (docs/intent/city_identity.md §4 不変条件 2)。 */}
                            {selectedCity
                                ? <Field label={uiText("components.settings.WorldEditor.text035")}><Input value={selectedCity.CITY_SLUG} disabled style={{ opacity: 0.7, cursor: 'not-allowed' }} /></Field>
                                : <Field label={uiText("components.settings.WorldEditor.text036")}><Input value={formData.slug || ''} onChange={(e: any) => setFormData({ ...formData, slug: e.target.value.replace(/[^a-zA-Z0-9_]/g, '') })} /></Field>
                            }
                            <Field label={uiText("components.settings.WorldEditor.text037")}><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            <div className={styles.row}>
                                <Field label={uiText("components.settings.WorldEditor.text038")}><NumInput value={formData.ui_port || ''} onChange={(e: any) => setFormData({ ...formData, ui_port: parseInt(e.target.value) })} /></Field>
                                <Field label={uiText("components.settings.WorldEditor.text039")}><NumInput value={formData.api_port || ''} onChange={(e: any) => setFormData({ ...formData, api_port: parseInt(e.target.value) })} /></Field>
                            </div>
                            <Field label={uiText("components.settings.WorldEditor.text040")}><Input value={formData.timezone || ''} onChange={(e: any) => setFormData({ ...formData, timezone: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.language")}>
                                <Select value={formData.language || 'ja'} onChange={(e: any) => setFormData({ ...formData, language: e.target.value })}>
                                    <option value="ja">日本語 (Japanese)</option>
                                    <option value="en">English</option>
                                </Select>
                            </Field>
                            {selectedCity && <Field label={uiText("components.settings.WorldEditor.text041")}>
                                <ImageUpload
                                    value={formData.map_background_image || ''}
                                    onChange={(url: string) => setFormData({ ...formData, map_background_image: url })}
                                    uploadEndpoint="hires"
                                />
                                <small data-i18n="components.settings.WorldEditor.text042" style={{ color: '#666', fontSize: '0.8rem' }}>{uiText("components.settings.WorldEditor.text042")}</small>
                            </Field>}
                            {selectedCity && <label data-i18n="components.settings.WorldEditor.text043"><input type="checkbox" checked={formData.online_mode || false} onChange={(e: any) => setFormData({ ...formData, online_mode: e.target.checked })} />{uiText("components.settings.WorldEditor.text043")}</label>}
                            {renderFormActions(selectedCity, handleCreateCity, handleUpdateCity, handleDeleteCity)}
                        </div>
                    </div>
                )}

                {subTab === 'building' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3 data-i18n="components.settings.WorldEditor.text044">{uiText("components.settings.WorldEditor.text044")}</h3>
                            {buildingList.rows.map(b => <div key={b.BUILDINGID} className={`${styles.item} ${selectedBuilding?.BUILDINGID === b.BUILDINGID ? styles.selected : ''}`} onClick={() => handleBuildingSelect(b)}>{b.BUILDINGNAME}</div>)}
                            <Pagination list={buildingList} onNavigate={() => { setSelectedBuilding(null); selectedBuildingIdRef.current = null; setFormData({}); }} />
                            <button data-i18n="components.settings.WorldEditor.text045" className={styles.newBtn} onClick={() => { setSelectedBuilding(null); selectedBuildingIdRef.current = null; setFormData({}); }}>{uiText("components.settings.WorldEditor.text045")}</button>
                        </div>
                        <div className={styles.form}>
                            <h3 data-i18n="components.settings.WorldEditor.text046 components.settings.WorldEditor.text047">{selectedBuilding ? uiText("components.settings.WorldEditor.text046") : uiText("components.settings.WorldEditor.text047")}</h3>
                            <Field label={uiText("components.settings.WorldEditor.text048")}><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            {selectedBuilding
                                ? <Field label={uiText("components.settings.WorldEditor.label005")}><Input value={selectedBuilding.BUILDINGID} disabled style={{ opacity: 0.7, cursor: 'not-allowed' }} /></Field>
                                : <Field label={uiText("components.settings.WorldEditor.text049")}><Input value={formData.building_id || ''} placeholder={uiText("components.settings.WorldEditor.text050")} onChange={(e: any) => setFormData({ ...formData, building_id: e.target.value })} /></Field>
                            }
                            {/* 既存 Building の City 変更は不可 (W7 柱5: 参照 scope を跨ぐため
                                サーバ側でも拒否する)。編集時は表示のみ。 */}
                            <Field label={uiText("components.settings.WorldEditor.text051")}><Select value={formData.city_id || ''} disabled={!!selectedBuilding} style={selectedBuilding ? { opacity: 0.7, cursor: 'not-allowed' } : undefined} onChange={(e: any) => setFormData({ ...formData, city_id: parseInt(e.target.value) })}>
                                <option data-i18n="components.settings.WorldEditor.text052" value="">{uiText("components.settings.WorldEditor.text052")}</option>{cityOptions.map(c => <option key={c.CITYID} value={c.CITYID}>{c.CITYNAME || c.CITY_SLUG}</option>)}
                            </Select></Field>
                            <div className={styles.row}>
                                <Field label={uiText("components.settings.WorldEditor.text053")}><NumInput value={formData.capacity || 1} onChange={(e: any) => setFormData({ ...formData, capacity: parseInt(e.target.value) })} /></Field>
                                <Field label={uiText("components.settings.WorldEditor.text054")}><NumInput value={formData.auto_interval || 10} onChange={(e: any) => setFormData({ ...formData, auto_interval: parseInt(e.target.value) })} /></Field>
                            </div>
                            {selectedBuilding && <Field label={uiText("components.settings.WorldEditor.itemDisplayLimit")}>
                                <NumInput
                                    min={0}
                                    placeholder="10"
                                    value={formData.item_display_limit ?? ''}
                                    onChange={(e: any) => {
                                        const raw = e.target.value;
                                        const parsed = parseInt(raw, 10);
                                        setFormData({ ...formData, item_display_limit: raw === '' || Number.isNaN(parsed) ? null : parsed });
                                    }}
                                />
                                <small data-i18n="components.settings.WorldEditor.itemDisplayLimitHint" className={styles.hint}>{uiText("components.settings.WorldEditor.itemDisplayLimitHint")}</small>
                            </Field>}
                            <Field label={uiText("components.settings.WorldEditor.text055")}><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text056")}><TextArea style={{ minHeight: 150 }} value={formData.system_instruction || ''} onChange={(e: any) => setFormData({ ...formData, system_instruction: e.target.value })} /></Field>
                            {selectedBuilding && <Field label={uiText("components.settings.WorldEditor.text057")}>
                                <ImageUpload
                                    value={formData.image_path || ''}
                                    onChange={(url: string) => setFormData({ ...formData, image_path: url })}
                                />
                                <small data-i18n="components.settings.WorldEditor.text058" style={{ color: '#666', fontSize: '0.8rem' }}>{uiText("components.settings.WorldEditor.text058")}</small>
                            </Field>}
                            {selectedBuilding && <div className={styles.field}>
                                <label data-i18n="components.settings.WorldEditor.text059">{uiText("components.settings.WorldEditor.text059")}</label>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                                    {(formData.extra_prompt_files || []).map((file: string, idx: number) => (
                                        <div key={idx} style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                                            <Select
                                                value={file}
                                                onChange={(e: any) => {
                                                    const updated = [...(formData.extra_prompt_files || [])];
                                                    updated[idx] = e.target.value;
                                                    setFormData({ ...formData, extra_prompt_files: updated });
                                                }}
                                                style={{ flex: 1 }}
                                            >
                                                <option data-i18n="components.settings.WorldEditor.text060" value="">{uiText("components.settings.WorldEditor.text060")}</option>
                                                {availablePrompts.map(p => <option key={p} value={p}>{p}</option>)}
                                            </Select>
                                            <button
                                                type="button"
                                                onClick={() => {
                                                    const updated = (formData.extra_prompt_files || []).filter((_: string, i: number) => i !== idx);
                                                    setFormData({ ...formData, extra_prompt_files: updated });
                                                }}
                                                style={{ padding: '0.25rem 0.5rem', background: '#f87171', color: 'white', border: 'none', borderRadius: '4px', cursor: 'pointer' }}
                                            >
                                                ×
                                            </button>
                                        </div>
                                    ))}
                                    <button data-i18n="components.settings.WorldEditor.text061"
                                        type="button"
                                        onClick={() => setFormData({ ...formData, extra_prompt_files: [...(formData.extra_prompt_files || []), ''] })}
                                        style={{ padding: '0.5rem', background: '#e2e8f0', border: 'none', borderRadius: '4px', cursor: 'pointer' }}
                                    >{uiText("components.settings.WorldEditor.text061")}</button>
                                </div>
                                <small data-i18n="components.settings.WorldEditor.text062" style={{ color: '#666', fontSize: '0.8rem' }}>{uiText("components.settings.WorldEditor.text062")}</small>
                            </div>}
                            {selectedBuilding && <div className={styles.field}><label data-i18n="components.settings.WorldEditor.text063">{uiText("components.settings.WorldEditor.text063")}</label><div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem' }}>{toolOptions.map(t => (<label key={t.TOOLID} style={{ background: '#f1f5f9', padding: '0.25rem' }}><input type="checkbox" checked={(formData.tool_ids || []).includes(t.TOOLID)} onChange={e => { const c = formData.tool_ids || []; if (e.target.checked) setFormData({ ...formData, tool_ids: [...c, t.TOOLID] }); else setFormData({ ...formData, tool_ids: c.filter((id: any) => id !== t.TOOLID) }); }} /> {t.TOOLNAME}</label>))}</div></div>}
                            {renderFormActions(selectedBuilding, handleCreateBuilding, handleUpdateBuilding, handleDeleteBuilding)}
                        </div>
                    </div>
                )}

                {subTab === 'ai' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3 data-i18n="components.settings.WorldEditor.text064">{uiText("components.settings.WorldEditor.text064")}</h3>
                            {aiList.rows.map(a => <div key={a.AIID} className={`${styles.item} ${selectedAI?.AIID === a.AIID ? styles.selected : ''}`} onClick={() => handleAISelect(a)}>{a.AINAME}</div>)}
                            <Pagination list={aiList} onNavigate={() => { setSelectedAI(null); setFormData({}); }} />
                            <button data-i18n="components.settings.WorldEditor.text065" className={styles.newBtn} onClick={() => { setSelectedAI(null); setFormData({ autonomy_enabled: true }); }}>{uiText("components.settings.WorldEditor.text065")}</button>
                        </div>
                        <div className={styles.form}>
                            <h3 data-i18n="components.settings.WorldEditor.text066 components.settings.WorldEditor.text067">{selectedAI ? uiText("components.settings.WorldEditor.text066") : uiText("components.settings.WorldEditor.text067")}</h3>
                            <Field label={uiText("components.settings.WorldEditor.text068")}><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text069")}><Select value={formData.home_city_id || ''} onChange={(e: any) => setFormData({ ...formData, home_city_id: parseInt(e.target.value) })}>
                                <option data-i18n="components.settings.WorldEditor.text070" value="">{uiText("components.settings.WorldEditor.text070")}</option>{cityOptions.map(c => <option key={c.CITYID} value={c.CITYID}>{c.CITYNAME || c.CITY_SLUG}</option>)}
                            </Select></Field>
                            {selectedAI && <>
                                <Field label={uiText("components.settings.WorldEditor.text071")}><Select value={formData.default_model || ''} onChange={(e: any) => setFormData({ ...formData, default_model: e.target.value })}>
                                    <option data-i18n="components.settings.WorldEditor.text072" value="">{uiText("components.settings.WorldEditor.text072")}</option>
                                    {formData.default_model && !modelChoices.some(m => m.id === formData.default_model) && (
                                        <option data-i18n="components.settings.WorldEditor.text073" value={formData.default_model}>{uiText("components.settings.WorldEditor.text073")}{formData.default_model}</option>
                                    )}
                                    {modelChoices.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
                                </Select></Field>
                                <Field label={uiText("components.settings.WorldEditor.text074")}><Select value={formData.lightweight_model || ''} onChange={(e: any) => setFormData({ ...formData, lightweight_model: e.target.value })}>
                                    <option data-i18n="components.settings.WorldEditor.text075" value="">{uiText("components.settings.WorldEditor.text075")}</option>
                                    {formData.lightweight_model && !modelChoices.some(m => m.id === formData.lightweight_model) && (
                                        <option data-i18n="components.settings.WorldEditor.text076" value={formData.lightweight_model}>{uiText("components.settings.WorldEditor.text076")}{formData.lightweight_model}</option>
                                    )}
                                    {modelChoices.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
                                </Select></Field>
                                <Field label={uiText("components.settings.WorldEditor.text077")}>
                                    <label data-i18n="components.settings.WorldEditor.text078 components.settings.WorldEditor.text079" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                                        <input
                                            type="checkbox"
                                            checked={formData.autonomy_enabled ?? true}
                                            onChange={(e: any) => setFormData({ ...formData, autonomy_enabled: e.target.checked })}
                                        />
                                        {(formData.autonomy_enabled ?? true) ? uiText("components.settings.WorldEditor.text078") : uiText("components.settings.WorldEditor.text079")}
                                    </label>
                                </Field>
                                <Field label={uiText("components.settings.WorldEditor.text080")}>
                                    <ImageUpload
                                        value={formData.avatar_path || ''}
                                        onChange={(url: string) => setFormData({ ...formData, avatar_path: url })}
                                        circle={true}
                                    />
                                </Field>
                                <Field label={uiText("components.settings.WorldEditor.text081")}>
                                    <ImageUpload
                                        value={formData.appearance_image_path || ''}
                                        onChange={(url: string) => setFormData({ ...formData, appearance_image_path: url })}
                                    />
                                    <small data-i18n="components.settings.WorldEditor.text082" style={{ color: '#666', fontSize: '0.8rem' }}>{uiText("components.settings.WorldEditor.text082")}</small>
                                </Field>
                            </>}
                            <Field label={uiText("components.settings.WorldEditor.text083")}><TextArea style={{ minHeight: 200 }} value={formData.system_prompt || ''} onChange={(e: any) => setFormData({ ...formData, system_prompt: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text084")}><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            {renderFormActions(selectedAI, handleCreateAI, handleUpdateAI, handleDeleteAI)}
                            {selectedAI && <div style={{ marginTop: '2rem', borderTop: '1px solid #eee', paddingTop: '1rem' }}>
                                <h4 data-i18n="components.settings.WorldEditor.text085">{uiText("components.settings.WorldEditor.text085")}</h4>
                                <div className={styles.row}>
                                    <Select value={formData.target_building_name || ''} onChange={(e: any) => setFormData({ ...formData, target_building_name: e.target.value })}>
                                        <option data-i18n="components.settings.WorldEditor.text086" value="">{uiText("components.settings.WorldEditor.text086")}</option>{buildingOptions.map(b => <option key={b.BUILDINGID} value={b.BUILDINGNAME}>{b.BUILDINGNAME}</option>)}
                                    </Select>
                                    <button data-i18n="components.settings.WorldEditor.text087" className={styles.primaryBtn} onClick={handleMoveAI}>{uiText("components.settings.WorldEditor.text087")}</button>
                                </div>
                            </div>}
                        </div>
                    </div>
                )}

                {subTab === 'blueprint' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3 data-i18n="components.settings.WorldEditor.text088">{uiText("components.settings.WorldEditor.text088")}</h3>
                            {blueprintList.rows.map(b => <div key={b.BLUEPRINT_ID} className={`${styles.item} ${selectedBlueprint?.BLUEPRINT_ID === b.BLUEPRINT_ID ? styles.selected : ''}`} onClick={() => handleBlueprintSelect(b)}>{b.NAME}</div>)}
                            <Pagination list={blueprintList} onNavigate={() => { setSelectedBlueprint(null); setFormData({ entity_type: 'ai' }); }} />
                            <button data-i18n="components.settings.WorldEditor.text089" className={styles.newBtn} onClick={() => { setSelectedBlueprint(null); setFormData({ entity_type: 'ai' }); }}>{uiText("components.settings.WorldEditor.text089")}</button>
                        </div>
                        <div className={styles.form}>
                            <h3 data-i18n="components.settings.WorldEditor.text090 components.settings.WorldEditor.text091">{selectedBlueprint ? uiText("components.settings.WorldEditor.text090") : uiText("components.settings.WorldEditor.text091")}</h3>
                            <Field label={uiText("components.settings.WorldEditor.text092")}><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text093")}><Input value={formData.entity_type || 'ai'} onChange={(e: any) => setFormData({ ...formData, entity_type: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text094")}><Select value={formData.city_id || ''} onChange={(e: any) => setFormData({ ...formData, city_id: parseInt(e.target.value) })}>
                                <option data-i18n="components.settings.WorldEditor.text095" value="">{uiText("components.settings.WorldEditor.text095")}</option>{cityOptions.map(c => <option key={c.CITYID} value={c.CITYID}>{c.CITYNAME || c.CITY_SLUG}</option>)}
                            </Select></Field>
                            <Field label={uiText("components.settings.WorldEditor.text096")}><TextArea style={{ minHeight: 200 }} value={formData.system_prompt || ''} onChange={(e: any) => setFormData({ ...formData, system_prompt: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text097")}><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            {renderFormActions(selectedBlueprint, handleCreateBlueprint, handleUpdateBlueprint, handleDeleteBlueprint)}
                            {selectedBlueprint && <div style={{ marginTop: '2rem', borderTop: '1px solid #eee', paddingTop: '1rem' }}>
                                <h4 data-i18n="components.settings.WorldEditor.text098">{uiText("components.settings.WorldEditor.text098")}</h4>
                                <Field label={uiText("components.settings.WorldEditor.text099")}><Input value={formData.spawn_entity_name || ''} onChange={(e: any) => setFormData({ ...formData, spawn_entity_name: e.target.value })} /></Field>
                                <div className={styles.row}>
                                    <Select value={formData.spawn_building_name || ''} onChange={(e: any) => setFormData({ ...formData, spawn_building_name: e.target.value })}>
                                        <option data-i18n="components.settings.WorldEditor.text100" value="">{uiText("components.settings.WorldEditor.text100")}</option>{buildingOptions.map(b => <option key={b.BUILDINGID} value={b.BUILDINGNAME}>{b.BUILDINGNAME}</option>)}
                                    </Select>
                                    <button data-i18n="components.settings.WorldEditor.text101" className={styles.primaryBtn} onClick={handleSpawnBlueprint}>{uiText("components.settings.WorldEditor.text101")}</button>
                                </div>
                            </div>}
                        </div>
                    </div>
                )}

                {subTab === 'tool' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3 data-i18n="components.settings.WorldEditor.text102">{uiText("components.settings.WorldEditor.text102")}</h3>
                            {toolList.rows.map(t => <div key={t.TOOLID} className={`${styles.item} ${selectedTool?.TOOLID === t.TOOLID ? styles.selected : ''}`} onClick={() => handleToolSelect(t)}>{t.TOOLNAME}</div>)}
                            <Pagination list={toolList} onNavigate={() => { setSelectedTool(null); setFormData({}); }} />
                            <button data-i18n="components.settings.WorldEditor.text103" className={styles.newBtn} onClick={() => { setSelectedTool(null); setFormData({}); }}>{uiText("components.settings.WorldEditor.text103")}</button>
                        </div>
                        <div className={styles.form}>
                            <h3 data-i18n="components.settings.WorldEditor.text104 components.settings.WorldEditor.text105">{selectedTool ? uiText("components.settings.WorldEditor.text104") : uiText("components.settings.WorldEditor.text105")}</h3>
                            <Field label={uiText("components.settings.WorldEditor.text106")}><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text107")}><Input value={formData.module_path || ''} onChange={(e: any) => setFormData({ ...formData, module_path: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text108")}><Input value={formData.function_name || ''} onChange={(e: any) => setFormData({ ...formData, function_name: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text109")}><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            {renderFormActions(selectedTool, handleCreateTool, handleUpdateTool, handleDeleteTool)}
                        </div>
                    </div>
                )}

                {subTab === 'item' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3 data-i18n="components.settings.WorldEditor.text110">{uiText("components.settings.WorldEditor.text110")}</h3>
                            {itemList.rows.map(i => <div key={i.ITEM_ID} className={`${styles.item} ${selectedItem?.ITEM_ID === i.ITEM_ID ? styles.selected : ''}`} onClick={() => handleItemSelect(i)}>{i.NAME}</div>)}
                            <Pagination list={itemList} onNavigate={() => { setSelectedItem(null); setFormData({ item_type: 'picture', owner_kind: 'world' }); }} />
                            <button data-i18n="components.settings.WorldEditor.text111" className={styles.newBtn} onClick={() => { setSelectedItem(null); setFormData({ item_type: 'picture', owner_kind: 'world' }); }}>{uiText("components.settings.WorldEditor.text111")}</button>
                        </div>
                        <div className={styles.form}>
                            <h3 data-i18n="components.settings.WorldEditor.text112 components.settings.WorldEditor.text113">{selectedItem ? uiText("components.settings.WorldEditor.text112") : uiText("components.settings.WorldEditor.text113")}</h3>
                            <Field label={uiText("components.settings.WorldEditor.text114")}><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text115")}><Select value={formData.item_type || 'object'} onChange={(e: any) => setFormData({ ...formData, item_type: e.target.value })}>
                                <option data-i18n="components.settings.WorldEditor.text116" value="picture">{uiText("components.settings.WorldEditor.text116")}</option>
                                <option data-i18n="components.settings.WorldEditor.text117" value="document">{uiText("components.settings.WorldEditor.text117")}</option>
                                <option data-i18n="components.settings.WorldEditor.text118" value="object">{uiText("components.settings.WorldEditor.text118")}</option>
                                <option data-i18n="components.settings.WorldEditor.text119" value="bag">{uiText("components.settings.WorldEditor.text119")}</option>
                            </Select></Field>
                            <div className={styles.row}>
                                <Field label={uiText("components.settings.WorldEditor.text120")}>
                                    <Select value={formData.owner_kind || 'world'} onChange={(e: any) => setFormData({ ...formData, owner_kind: e.target.value, owner_id: '' })}>
                                        <option data-i18n="components.settings.WorldEditor.text121" value="world">{uiText("components.settings.WorldEditor.text121")}</option>
                                        <option value="building">{uiText("components.settings.WorldEditor.label006")}</option>
                                        <option data-i18n="components.settings.WorldEditor.text122" value="persona">{uiText("components.settings.WorldEditor.text122")}</option>
                                        <option data-i18n="components.settings.WorldEditor.text123" value="bag">{uiText("components.settings.WorldEditor.text123")}</option>
                                    </Select>
                                </Field>
                                {formData.owner_kind === 'building' && (
                                    <Field label={uiText("components.settings.WorldEditor.label007")}>
                                        <Select value={formData.owner_id || ''} onChange={(e: any) => setFormData({ ...formData, owner_id: e.target.value })}>
                                            <option data-i18n="components.settings.WorldEditor.text124" value="">{uiText("components.settings.WorldEditor.text124")}</option>
                                            {buildingOptions.map(b => <option key={b.BUILDINGID} value={b.BUILDINGID}>{b.BUILDINGNAME}</option>)}
                                        </Select>
                                    </Field>
                                )}
                                {formData.owner_kind === 'persona' && (
                                    <Field label={uiText("components.settings.WorldEditor.text125")}>
                                        <Select value={formData.owner_id || ''} onChange={(e: any) => setFormData({ ...formData, owner_id: e.target.value })}>
                                            <option data-i18n="components.settings.WorldEditor.text126" value="">{uiText("components.settings.WorldEditor.text126")}</option>
                                            {aiOptions.map(a => <option key={a.AIID} value={a.AIID}>{a.AINAME}</option>)}
                                        </Select>
                                    </Field>
                                )}
                                {formData.owner_kind === 'bag' && (
                                    <Field label={uiText("components.settings.WorldEditor.label008")}>
                                        <Select value={formData.owner_id || ''} onChange={(e: any) => setFormData({ ...formData, owner_id: e.target.value })}>
                                            <option data-i18n="components.settings.WorldEditor.text127" value="">{uiText("components.settings.WorldEditor.text127")}</option>
                                            {bagOptions.filter(i => i.ITEM_ID !== selectedItem?.ITEM_ID).map(i => (
                                                <option key={i.ITEM_ID} value={i.ITEM_ID}>{i.NAME}</option>
                                            ))}
                                        </Select>
                                    </Field>
                                )}
                            </div>
                            {(formData.item_type === 'picture' || formData.item_type === 'document') && (
                                <Field label={uiText("components.settings.WorldEditor.text128")}>
                                    <FileUpload
                                        value={formData.file_path || null}
                                        onChange={(path, type) => {
                                            setFormData({ ...formData, file_path: path });
                                        }}
                                        onClear={() => setFormData({ ...formData, file_path: '' })}
                                        acceptImages={formData.item_type === 'picture'}
                                        acceptDocuments={formData.item_type === 'document'}
                                        placeholder={formData.item_type === 'picture' ? uiText("components.settings.WorldEditor.text129") : uiText("components.settings.WorldEditor.text130")}
                                    />
                                </Field>
                            )}
                            <Field label={uiText("components.settings.WorldEditor.text131")}>
                                <TextArea
                                    value={formData.description || ''}
                                    onChange={(e: any) => setFormData({ ...formData, description: e.target.value })}
                                    placeholder={uiText("components.settings.WorldEditor.text132")}
                                />
                            </Field>
                            <Field label={uiText("components.settings.WorldEditor.label009")}><TextArea value={formData.state_json || ''} onChange={(e: any) => setFormData({ ...formData, state_json: e.target.value })} /></Field>
                            {renderFormActions(selectedItem, handleCreateItem, handleUpdateItem, handleDeleteItem)}
                        </div>
                    </div>
                )}

                {subTab === 'playbook' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3 data-i18n="components.settings.WorldEditor.text133">{uiText("components.settings.WorldEditor.text133")}</h3>
                            {playbooks.map(pb => <div key={pb.id} className={`${styles.item} ${selectedPlaybook?.id === pb.id ? styles.selected : ''}`} onClick={() => handlePlaybookSelect(pb)}>{pb.name}</div>)}
                            <button data-i18n="components.settings.WorldEditor.text134" className={styles.newBtn} onClick={() => { setSelectedPlaybook(null); setFormData({ scope: 'public', router_callable: false, user_selectable: false, nodes_json: '[]', schema_json: '{"input_schema": [], "start_node": "start"}' }); }}>{uiText("components.settings.WorldEditor.text134")}</button>
                            <div
                                style={{
                                    marginTop: '1rem',
                                    borderTop: '1px solid #eee',
                                    paddingTop: '1rem',
                                }}
                            >
                                <h4 data-i18n="components.settings.WorldEditor.text135" style={{ marginBottom: '0.5rem', fontSize: '0.9rem' }}>{uiText("components.settings.WorldEditor.text135")}</h4>
                                <div
                                    style={{
                                        border: `2px dashed ${isPlaybookDragOver ? '#06b6d4' : '#4b5563'}`,
                                        borderRadius: '8px',
                                        padding: '1rem',
                                        display: 'flex',
                                        flexDirection: 'column',
                                        alignItems: 'center',
                                        gap: '0.25rem',
                                        cursor: 'pointer',
                                        transition: 'all 0.2s',
                                        backgroundColor: isPlaybookDragOver ? 'rgba(6, 182, 212, 0.1)' : 'transparent',
                                    }}
                                    onClick={() => playbookFileInputRef.current?.click()}
                                    onDragEnter={handlePlaybookDragEnter}
                                    onDragOver={handlePlaybookDragOver}
                                    onDragLeave={handlePlaybookDragLeave}
                                    onDrop={handlePlaybookDrop}
                                >
                                    <Upload size={20} style={{ opacity: 0.6 }} />
                                    <span data-i18n="components.settings.WorldEditor.text136 components.settings.WorldEditor.text137" style={{ fontSize: '0.8rem', opacity: 0.7 }}>
                                        {isPlaybookDragOver ? uiText("components.settings.WorldEditor.text136") : uiText("components.settings.WorldEditor.text137")}
                                    </span>
                                    <input
                                        type="file"
                                        ref={playbookFileInputRef}
                                        accept=".json"
                                        style={{ display: 'none' }}
                                        onChange={(e) => {
                                            const file = e.target.files?.[0];
                                            if (file) handlePlaybookImport(file);
                                        }}
                                    />
                                </div>
                            </div>
                        </div>
                        <div className={styles.form}>
                            <h3 data-i18n="components.settings.WorldEditor.text138 components.settings.WorldEditor.text139">{selectedPlaybook ? uiText("components.settings.WorldEditor.text138") : uiText("components.settings.WorldEditor.text139")}</h3>
                            <Field label={uiText("components.settings.WorldEditor.text140")}><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text141")}><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            <div className={styles.row}>
                                <Field label={uiText("components.settings.WorldEditor.text142")}><Select value={formData.scope || 'public'} onChange={(e: any) => setFormData({ ...formData, scope: e.target.value })}>
                                    <option value="public">{uiText("components.settings.WorldEditor.label010")}</option><option value="personal">{uiText("components.settings.WorldEditor.label011")}</option><option value="building">{uiText("components.settings.WorldEditor.label012")}</option>
                                </Select></Field>
                            </div>
                            <div className={styles.row}>
                                <label data-i18n="components.settings.WorldEditor.text143"><input type="checkbox" checked={formData.router_callable || false} onChange={(e: any) => setFormData({ ...formData, router_callable: e.target.checked })} />{uiText("components.settings.WorldEditor.text143")}</label>
                                <label data-i18n="components.settings.WorldEditor.text144" style={{ marginLeft: '1rem' }}><input type="checkbox" checked={formData.user_selectable || false} onChange={(e: any) => setFormData({ ...formData, user_selectable: e.target.checked })} />{uiText("components.settings.WorldEditor.text144")}</label>
                            </div>
                            <Field label={uiText("components.settings.WorldEditor.text145")}><TextArea style={{ minHeight: 120, fontFamily: 'monospace' }} value={formData.schema_json || ''} onChange={(e: any) => setFormData({ ...formData, schema_json: e.target.value })} /></Field>
                            <Field label={uiText("components.settings.WorldEditor.text146")}><TextArea style={{ minHeight: 200, fontFamily: 'monospace' }} value={formData.nodes_json || ''} onChange={(e: any) => setFormData({ ...formData, nodes_json: e.target.value })} /></Field>
                            {renderFormActions(selectedPlaybook, handleCreatePlaybook, handleUpdatePlaybook, handleDeletePlaybook)}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
