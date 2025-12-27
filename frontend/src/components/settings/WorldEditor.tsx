import React, { useState, useEffect } from 'react';
import styles from './WorldEditor.module.css';
import { Layers, MapPin, Cpu, Box, FileText, Wrench, ArrowRight, BookOpen } from 'lucide-react';
import ImageUpload from '../common/ImageUpload';
import FileUpload from '../common/FileUpload';

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
    CITYNAME: string;
    DESCRIPTION: string;
    UI_PORT: number;
    API_PORT: number;
    START_IN_ONLINE_MODE: boolean;
    TIMEZONE: string;
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
    INTERACTION_MODE: string;
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

export default function WorldEditor() {
    const [subTab, setSubTab] = useState('city');
    const [isLoading, setIsLoading] = useState(false);

    // Data State
    const [cities, setCities] = useState<City[]>([]);
    const [buildings, setBuildings] = useState<Building[]>([]);
    const [tools, setTools] = useState<Tool[]>([]);
    const [ais, setAis] = useState<AI[]>([]);
    const [items, setItems] = useState<Item[]>([]); // Note: pure items
    const [blueprints, setBlueprints] = useState<Blueprint[]>([]);
    const [modelChoices, setModelChoices] = useState<ModelChoice[]>([]);
    const [playbooks, setPlaybooks] = useState<Playbook[]>([]);

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

    // Load Data
    useEffect(() => {
        loadCities(); // Always load cities as they are needed for ID resolution
        if (subTab === 'building') { loadBuildings(); loadTools(); }
        if (subTab === 'ai') { loadBuildings(); loadAIs(); loadModels(); }
        if (subTab === 'item') { loadItems(); loadBuildings(); loadAIs(); }
        if (subTab === 'blueprint') { loadBlueprints(); loadBuildings(); } // Buildings for spawn
        if (subTab === 'tool') { loadTools(); }
        if (subTab === 'playbook') { loadPlaybooks(); }
    }, [subTab]);

    const loadCities = async () => { try { const res = await fetch('/api/db/tables/city'); if (res.ok) setCities(await res.json()); } catch (e) { } };
    const loadBuildings = async () => { try { const res = await fetch('/api/db/tables/building'); if (res.ok) setBuildings(await res.json()); } catch (e) { } };
    const loadTools = async () => { try { const res = await fetch('/api/db/tables/tool'); if (res.ok) setTools(await res.json()); } catch (e) { } };
    const loadAIs = async () => { try { const res = await fetch('/api/db/tables/ai'); if (res.ok) setAis(await res.json()); } catch (e) { } };
    const loadItems = async () => { try { const res = await fetch('/api/db/tables/item'); if (res.ok) setItems(await res.json()); } catch (e) { } };
    const loadBlueprints = async () => { try { const res = await fetch('/api/db/tables/blueprint'); if (res.ok) setBlueprints(await res.json()); } catch (e) { } };
    const loadModels = async () => { try { const res = await fetch('/api/info/models'); if (res.ok) setModelChoices(await res.json()); } catch (e) { } };
    const loadPlaybooks = async () => { try { const res = await fetch('/api/world/playbooks'); if (res.ok) setPlaybooks(await res.json()); } catch (e) { } };

    // --- City Handlers ---
    const handleCitySelect = (city: City) => {
        setSelectedCity(city);
        setFormData({ name: city.CITYNAME, description: city.DESCRIPTION, ui_port: city.UI_PORT, api_port: city.API_PORT, timezone: city.TIMEZONE, online_mode: city.START_IN_ONLINE_MODE });
    };
    const handleCreateCity = async () => { try { await fetch('/api/world/cities', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) }); loadCities(); setFormData({}); } catch (e) { } };
    const handleUpdateCity = async () => { try { await fetch(`/api/world/cities/${selectedCity!.CITYID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) }); loadCities(); } catch (e) { } };
    const handleDeleteCity = async () => { if (confirm("Are you sure?")) { await fetch(`/api/world/cities/${selectedCity!.CITYID}`, { method: 'DELETE' }); setSelectedCity(null); setFormData({}); loadCities(); } };

    // --- Building Handlers ---
    const handleBuildingSelect = (b: Building) => {
        setSelectedBuilding(b);
        // Fetch tools ... (simplified for now to empty list or need separate fetch)
        fetch(`/api/db/tables/building_tool_link`).then(r => r.json()).then(links => {
            const ids = links.filter((l: any) => l.BUILDINGID === b.BUILDINGID).map((l: any) => l.TOOLID);
            setFormData({ name: b.BUILDINGNAME, description: b.DESCRIPTION, capacity: b.CAPACITY, system_instruction: b.SYSTEM_INSTRUCTION, city_id: b.CITYID, auto_interval: b.AUTO_INTERVAL_SEC, tool_ids: ids, image_path: b.IMAGE_PATH || '' });
        });
    };
    const handleCreateBuilding = async () => { try { await fetch('/api/world/buildings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: formData.name, description: formData.description || "", capacity: formData.capacity || 1, system_instruction: formData.system_instruction || "", city_id: formData.city_id, building_id: formData.building_id || null }) }); loadBuildings(); setFormData({}); } catch (e) { } };
    const handleUpdateBuilding = async () => { try { await fetch(`/api/world/buildings/${selectedBuilding!.BUILDINGID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...formData, tool_ids: formData.tool_ids || [] }) }); loadBuildings(); } catch (e) { } };
    const handleDeleteBuilding = async () => { if (confirm("Are you sure?")) { await fetch(`/api/world/buildings/${selectedBuilding!.BUILDINGID}`, { method: 'DELETE' }); setSelectedBuilding(null); setFormData({}); loadBuildings(); } };

    // --- AI Handlers ---
    const handleAISelect = (ai: AI) => {
        setSelectedAI(ai);
        setFormData({
            name: ai.AINAME, description: ai.DESCRIPTION, system_prompt: ai.SYSTEMPROMPT,
            home_city_id: ai.HOME_CITYID, default_model: ai.DEFAULT_MODEL, lightweight_model: ai.LIGHTWEIGHT_MODEL,
            interaction_mode: ai.INTERACTION_MODE, avatar_path: ai.AVATAR_IMAGE,
            appearance_image_path: ai.APPEARANCE_IMAGE_PATH || ''
        });
    };
    const handleCreateAI = async () => { try { await fetch('/api/world/ais', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: formData.name, system_prompt: formData.system_prompt, home_city_id: formData.home_city_id }) }); loadAIs(); setFormData({}); } catch (e) { } };
    const handleUpdateAI = async () => { try { await fetch(`/api/world/ais/${selectedAI!.AIID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) }); loadAIs(); } catch (e) { } };
    const handleDeleteAI = async () => { if (confirm("Are you sure?")) { await fetch(`/api/world/ais/${selectedAI!.AIID}`, { method: 'DELETE' }); setSelectedAI(null); setFormData({}); loadAIs(); } };
    const handleMoveAI = async () => {
        if (!selectedAI || !formData.target_building_name) return;
        await fetch(`/api/world/ais/${selectedAI.AIID}/move`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target_building_name: formData.target_building_name }) });
        alert("Move requested");
        // Ideally refresh location display
    };

    // --- Blueprint Handlers ---
    const handleBlueprintSelect = (bp: Blueprint) => {
        setSelectedBlueprint(bp);
        setFormData({ name: bp.NAME, description: bp.DESCRIPTION, city_id: bp.CITYID, entity_type: bp.ENTITY_TYPE, system_prompt: bp.BASE_SYSTEM_PROMPT });
    };
    const handleCreateBlueprint = async () => { await fetch('/api/world/blueprints', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) }); loadBlueprints(); setFormData({}); };
    const handleUpdateBlueprint = async () => { await fetch(`/api/world/blueprints/${selectedBlueprint!.BLUEPRINT_ID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) }); loadBlueprints(); };
    const handleDeleteBlueprint = async () => { if (confirm("Are you sure?")) { await fetch(`/api/world/blueprints/${selectedBlueprint!.BLUEPRINT_ID}`, { method: 'DELETE' }); setSelectedBlueprint(null); setFormData({}); loadBlueprints(); } };
    const handleSpawnBlueprint = async () => {
        if (!selectedBlueprint || !formData.spawn_entity_name || !formData.spawn_building_name) return;
        const res = await fetch(`/api/world/blueprints/${selectedBlueprint.BLUEPRINT_ID}/spawn`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ entity_name: formData.spawn_entity_name, building_name: formData.spawn_building_name })
        });
        if (res.ok) alert("Spawned!"); else alert("Failed");
    };

    // --- Tool Handlers ---
    const handleToolSelect = (t: Tool) => { setSelectedTool(t); setFormData({ name: t.TOOLNAME, description: t.DESCRIPTION, module_path: t.MODULE_PATH, function_name: t.FUNCTION_NAME }); };
    const handleCreateTool = async () => { await fetch('/api/world/tools', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) }); loadTools(); setFormData({}); };
    const handleUpdateTool = async () => { await fetch(`/api/world/tools/${selectedTool!.TOOLID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) }); loadTools(); };
    const handleDeleteTool = async () => { if (confirm("Are you sure?")) { await fetch(`/api/world/tools/${selectedTool!.TOOLID}`, { method: 'DELETE' }); setSelectedTool(null); setFormData({}); loadTools(); } };

    // --- Item Handlers ---
    const handleItemSelect = (i: Item) => { setSelectedItem(i); setFormData({ name: i.NAME, item_type: i.TYPE, description: i.DESCRIPTION, owner_kind: 'world', owner_id: '', state_json: i.STATE_JSON, file_path: i.FILE_PATH }); }; // Owner info missing in generic list, default to world
    const handleCreateItem = async () => { await fetch('/api/world/items', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) }); loadItems(); setFormData({}); };
    const handleUpdateItem = async () => { await fetch(`/api/world/items/${selectedItem!.ITEM_ID}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) }); loadItems(); };
    const handleDeleteItem = async () => { if (confirm("Are you sure?")) { await fetch(`/api/world/items/${selectedItem!.ITEM_ID}`, { method: 'DELETE' }); setSelectedItem(null); setFormData({}); loadItems(); } };

    // --- Playbook Handlers ---
    const handlePlaybookSelect = async (pb: Playbook) => {
        // Fetch full details
        try {
            const res = await fetch(`/api/world/playbooks/${pb.id}`);
            if (res.ok) {
                const detail = await res.json();
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
        } catch (e) { }
    };
    const handleCreatePlaybook = async () => {
        try {
            const res = await fetch('/api/world/playbooks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) });
            if (!res.ok) { const err = await res.json(); alert(err.detail || 'Error'); return; }
            loadPlaybooks(); setFormData({}); setSelectedPlaybook(null);
        } catch (e) { }
    };
    const handleUpdatePlaybook = async () => {
        try {
            const res = await fetch(`/api/world/playbooks/${selectedPlaybook!.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formData) });
            if (!res.ok) { const err = await res.json(); alert(err.detail || 'Error'); return; }
            loadPlaybooks();
        } catch (e) { }
    };
    const handleDeletePlaybook = async () => { if (confirm("Are you sure?")) { await fetch(`/api/world/playbooks/${selectedPlaybook!.id}`, { method: 'DELETE' }); setSelectedPlaybook(null); setFormData({}); loadPlaybooks(); } };




    const renderFormActions = (selected: any, create: any, update: any, remove: any) => (
        <div className={styles.actions}>
            {selected ? <><button className={styles.primaryBtn} onClick={update}>Update</button><button className={styles.dangerBtn} onClick={remove}>Delete</button></>
                : <button className={styles.primaryBtn} onClick={create}>Create</button>}
        </div>
    );

    return (
        <div className={styles.container}>
            <div className={styles.tabs}>
                <button className={`${styles.tab} ${subTab === 'city' ? styles.active : ''}`} onClick={() => { setSubTab('city'); setSelectedCity(null); setFormData({}); }}><MapPin size={16} /> Cities</button>
                <button className={`${styles.tab} ${subTab === 'building' ? styles.active : ''}`} onClick={() => { setSubTab('building'); setSelectedBuilding(null); setFormData({}); }}><Layers size={16} /> Buildings</button>
                <button className={`${styles.tab} ${subTab === 'ai' ? styles.active : ''}`} onClick={() => { setSubTab('ai'); setSelectedAI(null); setFormData({}); }}><Cpu size={16} /> AIs</button>
                <button className={`${styles.tab} ${subTab === 'blueprint' ? styles.active : ''}`} onClick={() => { setSubTab('blueprint'); setSelectedBlueprint(null); setFormData({}); }}><FileText size={16} /> Blueprints</button>
                <button className={`${styles.tab} ${subTab === 'tool' ? styles.active : ''}`} onClick={() => { setSubTab('tool'); setSelectedTool(null); setFormData({}); }}><Wrench size={16} /> Tools</button>
                <button className={`${styles.tab} ${subTab === 'item' ? styles.active : ''}`} onClick={() => { setSubTab('item'); setSelectedItem(null); setFormData({}); }}><Box size={16} /> Items</button>
                <button className={`${styles.tab} ${subTab === 'playbook' ? styles.active : ''}`} onClick={() => { setSubTab('playbook'); setSelectedPlaybook(null); setFormData({}); }}><BookOpen size={16} /> Playbooks</button>
            </div>

            <div className={styles.content}>
                {subTab === 'city' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3>Cities</h3>
                            {cities.map(c => <div key={c.CITYID} className={`${styles.item} ${selectedCity?.CITYID === c.CITYID ? styles.selected : ''}`} onClick={() => handleCitySelect(c)}>{c.CITYNAME}</div>)}
                            <button className={styles.newBtn} onClick={() => { setSelectedCity(null); setFormData({}); }}>+ New City</button>
                        </div>
                        <div className={styles.form}>
                            <h3>{selectedCity ? `Edit City` : 'New City'}</h3>
                            <Field label="Name"><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label="Description"><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            <div className={styles.row}>
                                <Field label="UI Port"><NumInput value={formData.ui_port || ''} onChange={(e: any) => setFormData({ ...formData, ui_port: parseInt(e.target.value) })} /></Field>
                                <Field label="API Port"><NumInput value={formData.api_port || ''} onChange={(e: any) => setFormData({ ...formData, api_port: parseInt(e.target.value) })} /></Field>
                            </div>
                            <Field label="Timezone"><Input value={formData.timezone || ''} onChange={(e: any) => setFormData({ ...formData, timezone: e.target.value })} /></Field>
                            {selectedCity && <label><input type="checkbox" checked={formData.online_mode || false} onChange={(e: any) => setFormData({ ...formData, online_mode: e.target.checked })} /> Start Online Mode</label>}
                            {renderFormActions(selectedCity, handleCreateCity, handleUpdateCity, handleDeleteCity)}
                        </div>
                    </div>
                )}

                {subTab === 'building' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3>Buildings</h3>
                            {buildings.map(b => <div key={b.BUILDINGID} className={`${styles.item} ${selectedBuilding?.BUILDINGID === b.BUILDINGID ? styles.selected : ''}`} onClick={() => handleBuildingSelect(b)}>{b.BUILDINGNAME}</div>)}
                            <button className={styles.newBtn} onClick={() => { setSelectedBuilding(null); setFormData({}); }}>+ New Building</button>
                        </div>
                        <div className={styles.form}>
                            <h3>{selectedBuilding ? `Edit Building` : 'New Building'}</h3>
                            <Field label="Name"><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            {selectedBuilding
                                ? <Field label="ID"><Input value={selectedBuilding.BUILDINGID} disabled style={{ opacity: 0.7, cursor: 'not-allowed' }} /></Field>
                                : <Field label="ID (optional)"><Input value={formData.building_id || ''} placeholder="Leave empty to auto-generate" onChange={(e: any) => setFormData({ ...formData, building_id: e.target.value })} /></Field>
                            }
                            <Field label="City"><Select value={formData.city_id || ''} onChange={(e: any) => setFormData({ ...formData, city_id: parseInt(e.target.value) })}>
                                <option value="">Select City...</option>{cities.map(c => <option key={c.CITYID} value={c.CITYID}>{c.CITYNAME}</option>)}
                            </Select></Field>
                            <div className={styles.row}>
                                <Field label="Capacity"><NumInput value={formData.capacity || 1} onChange={(e: any) => setFormData({ ...formData, capacity: parseInt(e.target.value) })} /></Field>
                                <Field label="Interval (s)"><NumInput value={formData.auto_interval || 10} onChange={(e: any) => setFormData({ ...formData, auto_interval: parseInt(e.target.value) })} /></Field>
                            </div>
                            <Field label="Description"><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            <Field label="System Instruction"><TextArea style={{ minHeight: 150 }} value={formData.system_instruction || ''} onChange={(e: any) => setFormData({ ...formData, system_instruction: e.target.value })} /></Field>
                            {selectedBuilding && <Field label="Interior Image (Visual Context)">
                                <ImageUpload
                                    value={formData.image_path || ''}
                                    onChange={(url: string) => setFormData({ ...formData, image_path: url })}
                                />
                                <small style={{ color: '#666', fontSize: '0.8rem' }}>Building interior image for LLM visual context</small>
                            </Field>}
                            {selectedBuilding && <div className={styles.field}><label>Tools</label><div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem' }}>{tools.map(t => (<label key={t.TOOLID} style={{ background: '#f1f5f9', padding: '0.25rem' }}><input type="checkbox" checked={(formData.tool_ids || []).includes(t.TOOLID)} onChange={e => { const c = formData.tool_ids || []; if (e.target.checked) setFormData({ ...formData, tool_ids: [...c, t.TOOLID] }); else setFormData({ ...formData, tool_ids: c.filter((id: any) => id !== t.TOOLID) }); }} /> {t.TOOLNAME}</label>))}</div></div>}
                            {renderFormActions(selectedBuilding, handleCreateBuilding, handleUpdateBuilding, handleDeleteBuilding)}
                        </div>
                    </div>
                )}

                {subTab === 'ai' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3>AIs</h3>
                            {ais.map(a => <div key={a.AIID} className={`${styles.item} ${selectedAI?.AIID === a.AIID ? styles.selected : ''}`} onClick={() => handleAISelect(a)}>{a.AINAME}</div>)}
                            <button className={styles.newBtn} onClick={() => { setSelectedAI(null); setFormData({ interaction_mode: 'auto' }); }}>+ New AI</button>
                        </div>
                        <div className={styles.form}>
                            <h3>{selectedAI ? `Edit AI` : 'New AI'}</h3>
                            <Field label="Name"><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label="Home City"><Select value={formData.home_city_id || ''} onChange={(e: any) => setFormData({ ...formData, home_city_id: parseInt(e.target.value) })}>
                                <option value="">Select City...</option>{cities.map(c => <option key={c.CITYID} value={c.CITYID}>{c.CITYNAME}</option>)}
                            </Select></Field>
                            {selectedAI && <>
                                <Field label="Default Model"><Select value={formData.default_model || ''} onChange={(e: any) => setFormData({ ...formData, default_model: e.target.value })}>
                                    <option value="">Use System Default</option>{modelChoices.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
                                </Select></Field>
                                <Field label="Lightweight Model"><Select value={formData.lightweight_model || ''} onChange={(e: any) => setFormData({ ...formData, lightweight_model: e.target.value })}>
                                    <option value="">Use System Default</option>{modelChoices.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
                                </Select></Field>
                                <Field label="Interaction Mode"><Select value={formData.interaction_mode || 'auto'} onChange={(e: any) => setFormData({ ...formData, interaction_mode: e.target.value })}>
                                    <option value="auto">Auto</option><option value="manual">Manual</option><option value="sleep">Sleep</option>
                                </Select></Field>
                                <Field label="Avatar">
                                    <ImageUpload
                                        value={formData.avatar_path || ''}
                                        onChange={(url: string) => setFormData({ ...formData, avatar_path: url })}
                                        circle={true}
                                    />
                                </Field>
                                <Field label="Appearance Image (Visual Context)">
                                    <ImageUpload
                                        value={formData.appearance_image_path || ''}
                                        onChange={(url: string) => setFormData({ ...formData, appearance_image_path: url })}
                                    />
                                    <small style={{ color: '#666', fontSize: '0.8rem' }}>Detailed persona appearance image for LLM visual context (separate from avatar)</small>
                                </Field>
                            </>}
                            <Field label="System Prompt"><TextArea style={{ minHeight: 200 }} value={formData.system_prompt || ''} onChange={(e: any) => setFormData({ ...formData, system_prompt: e.target.value })} /></Field>
                            <Field label="Description"><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            {renderFormActions(selectedAI, handleCreateAI, handleUpdateAI, handleDeleteAI)}
                            {selectedAI && <div style={{ marginTop: '2rem', borderTop: '1px solid #eee', paddingTop: '1rem' }}>
                                <h4>Move AI</h4>
                                <div className={styles.row}>
                                    <Select value={formData.target_building_name || ''} onChange={(e: any) => setFormData({ ...formData, target_building_name: e.target.value })}>
                                        <option value="">Select Destination...</option>{buildings.map(b => <option key={b.BUILDINGID} value={b.BUILDINGNAME}>{b.BUILDINGNAME}</option>)}
                                    </Select>
                                    <button className={styles.primaryBtn} onClick={handleMoveAI}>Move</button>
                                </div>
                            </div>}
                        </div>
                    </div>
                )}

                {subTab === 'blueprint' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3>Blueprints</h3>
                            {blueprints.map(b => <div key={b.BLUEPRINT_ID} className={`${styles.item} ${selectedBlueprint?.BLUEPRINT_ID === b.BLUEPRINT_ID ? styles.selected : ''}`} onClick={() => handleBlueprintSelect(b)}>{b.NAME}</div>)}
                            <button className={styles.newBtn} onClick={() => { setSelectedBlueprint(null); setFormData({ entity_type: 'ai' }); }}>+ New Blueprint</button>
                        </div>
                        <div className={styles.form}>
                            <h3>{selectedBlueprint ? `Edit Blueprint` : 'New Blueprint'}</h3>
                            <Field label="Name"><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label="Type"><Input value={formData.entity_type || 'ai'} onChange={(e: any) => setFormData({ ...formData, entity_type: e.target.value })} /></Field>
                            <Field label="City"><Select value={formData.city_id || ''} onChange={(e: any) => setFormData({ ...formData, city_id: parseInt(e.target.value) })}>
                                <option value="">Select City...</option>{cities.map(c => <option key={c.CITYID} value={c.CITYID}>{c.CITYNAME}</option>)}
                            </Select></Field>
                            <Field label="System Prompt"><TextArea style={{ minHeight: 200 }} value={formData.system_prompt || ''} onChange={(e: any) => setFormData({ ...formData, system_prompt: e.target.value })} /></Field>
                            <Field label="Description"><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            {renderFormActions(selectedBlueprint, handleCreateBlueprint, handleUpdateBlueprint, handleDeleteBlueprint)}
                            {selectedBlueprint && <div style={{ marginTop: '2rem', borderTop: '1px solid #eee', paddingTop: '1rem' }}>
                                <h4>Spawn Entity</h4>
                                <Field label="New Entity Name"><Input value={formData.spawn_entity_name || ''} onChange={(e: any) => setFormData({ ...formData, spawn_entity_name: e.target.value })} /></Field>
                                <div className={styles.row}>
                                    <Select value={formData.spawn_building_name || ''} onChange={(e: any) => setFormData({ ...formData, spawn_building_name: e.target.value })}>
                                        <option value="">Select Building...</option>{buildings.map(b => <option key={b.BUILDINGID} value={b.BUILDINGNAME}>{b.BUILDINGNAME}</option>)}
                                    </Select>
                                    <button className={styles.primaryBtn} onClick={handleSpawnBlueprint}>Spawn</button>
                                </div>
                            </div>}
                        </div>
                    </div>
                )}

                {subTab === 'tool' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3>Tools</h3>
                            {tools.map(t => <div key={t.TOOLID} className={`${styles.item} ${selectedTool?.TOOLID === t.TOOLID ? styles.selected : ''}`} onClick={() => handleToolSelect(t)}>{t.TOOLNAME}</div>)}
                            <button className={styles.newBtn} onClick={() => { setSelectedTool(null); setFormData({}); }}>+ New Tool</button>
                        </div>
                        <div className={styles.form}>
                            <h3>{selectedTool ? `Edit Tool` : 'New Tool'}</h3>
                            <Field label="Name"><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label="Module Path"><Input value={formData.module_path || ''} onChange={(e: any) => setFormData({ ...formData, module_path: e.target.value })} /></Field>
                            <Field label="Function Name"><Input value={formData.function_name || ''} onChange={(e: any) => setFormData({ ...formData, function_name: e.target.value })} /></Field>
                            <Field label="Description"><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            {renderFormActions(selectedTool, handleCreateTool, handleUpdateTool, handleDeleteTool)}
                        </div>
                    </div>
                )}

                {subTab === 'item' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3>Items</h3>
                            {items.map(i => <div key={i.ITEM_ID} className={`${styles.item} ${selectedItem?.ITEM_ID === i.ITEM_ID ? styles.selected : ''}`} onClick={() => handleItemSelect(i)}>{i.NAME}</div>)}
                            <button className={styles.newBtn} onClick={() => { setSelectedItem(null); setFormData({ item_type: 'picture', owner_kind: 'world' }); }}>+ New Item</button>
                        </div>
                        <div className={styles.form}>
                            <h3>{selectedItem ? `Edit Item` : 'New Item'}</h3>
                            <Field label="Name"><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label="Type"><Select value={formData.item_type || 'object'} onChange={(e: any) => setFormData({ ...formData, item_type: e.target.value })}>
                                <option value="picture">Picture </option>
                                <option value="document">Document </option>
                                <option value="object">Object (no file)</option>
                            </Select></Field>
                            <div className={styles.row}>
                                <Field label="Owner">
                                    <Select value={formData.owner_kind || 'world'} onChange={(e: any) => setFormData({ ...formData, owner_kind: e.target.value, owner_id: '' })}>
                                        <option value="world">World (Global)</option>
                                        <option value="building">Building</option>
                                        <option value="persona">Persona</option>
                                    </Select>
                                </Field>
                                {formData.owner_kind === 'building' && (
                                    <Field label="Building">
                                        <Select value={formData.owner_id || ''} onChange={(e: any) => setFormData({ ...formData, owner_id: e.target.value })}>
                                            <option value="">Select Building...</option>
                                            {buildings.map(b => <option key={b.BUILDINGID} value={b.BUILDINGID}>{b.BUILDINGNAME}</option>)}
                                        </Select>
                                    </Field>
                                )}
                                {formData.owner_kind === 'persona' && (
                                    <Field label="Persona">
                                        <Select value={formData.owner_id || ''} onChange={(e: any) => setFormData({ ...formData, owner_id: e.target.value })}>
                                            <option value="">Select Persona...</option>
                                            {ais.map(a => <option key={a.AIID} value={a.AIID}>{a.AINAME}</option>)}
                                        </Select>
                                    </Field>
                                )}
                            </div>
                            {(formData.item_type === 'picture' || formData.item_type === 'document') && (
                                <Field label="File">
                                    <FileUpload
                                        value={formData.file_path || null}
                                        onChange={(path, type) => {
                                            setFormData({ ...formData, file_path: path });
                                        }}
                                        onClear={() => setFormData({ ...formData, file_path: '' })}
                                        acceptImages={formData.item_type === 'picture'}
                                        acceptDocuments={formData.item_type === 'document'}
                                        placeholder={formData.item_type === 'picture' ? 'Select Image' : 'Select Text File'}
                                    />
                                </Field>
                            )}
                            <Field label="Description (AI要約)">
                                <TextArea
                                    value={formData.description || ''}
                                    onChange={(e: any) => setFormData({ ...formData, description: e.target.value })}
                                    placeholder="空のままでOKファイルから自動生成"
                                />
                            </Field>
                            <Field label="State JSON"><TextArea value={formData.state_json || ''} onChange={(e: any) => setFormData({ ...formData, state_json: e.target.value })} /></Field>
                            {renderFormActions(selectedItem, handleCreateItem, handleUpdateItem, handleDeleteItem)}
                        </div>
                    </div>
                )}

                {subTab === 'playbook' && (
                    <div className={styles.pane}>
                        <div className={styles.list}>
                            <h3>Playbooks</h3>
                            {playbooks.map(pb => <div key={pb.id} className={`${styles.item} ${selectedPlaybook?.id === pb.id ? styles.selected : ''}`} onClick={() => handlePlaybookSelect(pb)}>{pb.name}</div>)}
                            <button className={styles.newBtn} onClick={() => { setSelectedPlaybook(null); setFormData({ scope: 'public', router_callable: false, user_selectable: false, nodes_json: '[]', schema_json: '{"input_schema": [], "start_node": "start"}' }); }}>+ New Playbook</button>
                        </div>
                        <div className={styles.form}>
                            <h3>{selectedPlaybook ? `Edit Playbook` : 'New Playbook'}</h3>
                            <Field label="Name (lowercase, underscore)"><Input value={formData.name || ''} onChange={(e: any) => setFormData({ ...formData, name: e.target.value })} /></Field>
                            <Field label="Description"><TextArea value={formData.description || ''} onChange={(e: any) => setFormData({ ...formData, description: e.target.value })} /></Field>
                            <div className={styles.row}>
                                <Field label="Scope"><Select value={formData.scope || 'public'} onChange={(e: any) => setFormData({ ...formData, scope: e.target.value })}>
                                    <option value="public">Public</option><option value="personal">Personal</option><option value="building">Building</option>
                                </Select></Field>
                            </div>
                            <div className={styles.row}>
                                <label><input type="checkbox" checked={formData.router_callable || false} onChange={(e: any) => setFormData({ ...formData, router_callable: e.target.checked })} /> Router Callable</label>
                                <label style={{ marginLeft: '1rem' }}><input type="checkbox" checked={formData.user_selectable || false} onChange={(e: any) => setFormData({ ...formData, user_selectable: e.target.checked })} /> User Selectable</label>
                            </div>
                            <Field label="Schema JSON (input_schema, start_node, etc.)"><TextArea style={{ minHeight: 120, fontFamily: 'monospace' }} value={formData.schema_json || ''} onChange={(e: any) => setFormData({ ...formData, schema_json: e.target.value })} /></Field>
                            <Field label="Nodes JSON"><TextArea style={{ minHeight: 200, fontFamily: 'monospace' }} value={formData.nodes_json || ''} onChange={(e: any) => setFormData({ ...formData, nodes_json: e.target.value })} /></Field>
                            {renderFormActions(selectedPlaybook, handleCreatePlaybook, handleUpdatePlaybook, handleDeletePlaybook)}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
