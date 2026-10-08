// Execute the real TSX components, callbacks, and effects against synthetic GETs.
// The small hook/JSX renderer models dependency cleanup, keyed remounts, and
// unmounts. It deliberately does not test browser layout or fetch image bytes.
// No server, live persona, LLM, timer, or production data is involved.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const React = require('react');
const ts = require('typescript');
const source = path.resolve(__dirname, '../src');
const compiled = new Map();
const realFiles = new Set([
    'app/page.tsx', 'components/ItemReferenceModal.tsx', 'components/ItemModal.tsx',
    'components/InventoryModal.tsx', 'components/SaiverseLink.tsx',
]);
const css = new Proxy({}, { get: (_, key) => key });
const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
function all(node, predicate) {
    if (Array.isArray(node)) return node.flatMap(child => all(child, predicate));
    if (!node || typeof node !== 'object') return [];
    return [...(predicate(node) ? [node] : []), ...all(node.props?.children, predicate)];
}
function text(node) {
    if (Array.isArray(node)) return node.map(text).join('');
    if (node == null || typeof node === 'boolean') return '';
    return typeof node === 'object' ? text(node.props?.children) : String(node);
}
function one(tree, predicate, description) {
    const nodes = all(tree, predicate);
    assert.equal(nodes.length, 1, `expected one ${description}, got ${nodes.length}`);
    return nodes[0];
}
const hasClass = (node, value) => String(node.props?.className || '').split(/\s+/).includes(value);
const button = (tree, className) => one(tree, node => node.type === 'button' && hasClass(node, className), `${className} button`);
const header = tree => all(tree, node => node.type === 'h2').map(text);
const picture = { ITEM_ID: '11111111-1111-4111-8111-111111111111', SHORT_ID: 41, NAME: 'Synthetic sunrise', TYPE: 'picture', DESCRIPTION: 'A complete picture description.', OWNER_KIND: 'persona', OWNER_ID: 'synthetic-a' };
const documentItem = { ITEM_ID: '22222222-2222-4222-8222-222222222222', SHORT_ID: 42, NAME: 'Synthetic journal', TYPE: 'document', DESCRIPTION: 'A complete document description.', OWNER_KIND: 'persona', OWNER_ID: 'synthetic-a' };
const bag = { ITEM_ID: '33333333-3333-4333-8333-333333333333', NAME: 'Synthetic bag', TYPE: 'bag', DESCRIPTION: 'A nested inventory.', OWNER_KIND: 'building', OWNER_ID: 'synthetic-room' };
const itemProps = item => ({ id: item.ITEM_ID, name: item.NAME, type: item.TYPE, description: item.DESCRIPTION });
const inventoryRow = item => ({ ...itemProps(item), created_at: '2026-01-01T00:00:00Z' });
const documentBody = '# Synthetic journal\n\nA full paragraph, including **Markdown**.';

function createHarness(component, props = {}, relativePath = path.relative) {
    const instances = new Map(), modules = new Map(), componentIds = new WeakMap();
    const requests = [], notices = [], queued = new Map(), seenComponents = [];
    let active, cursor, effects = [], dirty = true, tree, rootProps = props, mounted = true, extra = null, nextId = 0;
    let binaryJsonReads = 0;
    const metadata = new Map([picture, documentItem, bag].flatMap(item => [
        [item.ITEM_ID, item], ...(item.SHORT_ID == null ? [] : [[String(item.SHORT_ID), item]]),
    ]));
    const inventories = new Map([
        ['synthetic-a', [inventoryRow(picture), inventoryRow(documentItem), inventoryRow(bag)]],
        ['synthetic-b', [inventoryRow(documentItem)]],
    ]);
    async function fetchMock(url, init = {}) {
        assert.equal(init.method || 'GET', 'GET', `viewer must never mutate: ${init.method} ${url}`);
        requests.push({ url, init });
        if (queued.get(url)?.length) return queued.get(url).shift();
        if (url === '/api/user/buildings') return response({ buildings: [{ id: 'synthetic-room', name: 'Synthetic room' }] });
        // The old page callback searched only the room, then fabricated a document.
        if (url.startsWith('/api/info/details')) return response({ items: [] });
        let match = url.match(/^\/api\/world\/items\/([^/?]+)$/);
        if (match) {
            const item = metadata.get(decodeURIComponent(match[1]));
            return item ? response(item) : response({ detail: 'Item not found' }, 404);
        }
        match = url.match(/^\/api\/people\/([^/]+)\/items$/);
        if (match) return response(inventories.get(decodeURIComponent(match[1])) || []);
        if (url === `/api/info/item/${bag.ITEM_ID}/bag-contents`) return response({ items: [inventoryRow(documentItem), inventoryRow(picture)] });
        if (url === `/api/info/item/${documentItem.ITEM_ID}`) return response({ content: documentBody });
        match = url.match(/^\/api\/info\/item\/([^/?]+)$/);
        if (match && metadata.get(decodeURIComponent(match[1]))?.TYPE === 'picture') {
            return { ok: true, status: 200, json: async () => { binaryJsonReads++; throw new SyntaxError('Unexpected token: image bytes are not JSON'); } };
        }
        throw new Error(`Unexpected synthetic GET: ${url}`);
    }
    const hooks = {
        ...React,
        useState(initial) {
            const instance = active, index = cursor++;
            if (!instance.slots[index]) {
                const slot = { value: typeof initial === 'function' ? initial() : initial };
                slot.set = value => {
                    if (!instance.mounted) return;
                    const next = typeof value === 'function' ? value(slot.value) : value;
                    if (!Object.is(next, slot.value)) { slot.value = next; dirty = true; }
                };
                instance.slots[index] = slot;
            }
            const slot = instance.slots[index];
            return [slot.value, slot.set];
        },
        useRef(initial) {
            const index = cursor++;
            return active.slots[index] ??= { current: initial };
        },
        useMemo(fn, deps) {
            const index = cursor++;
            if (!same(active.slots[index]?.deps, deps)) active.slots[index] = { deps, value: fn() };
            return active.slots[index].value;
        },
        useCallback(fn, deps) { return hooks.useMemo(() => fn, deps); },
        useEffect(fn, deps) {
            const instance = active, index = cursor++;
            // Home's unrelated server polling, presence, and addon subscriptions
            // are outside this unit boundary. Its real item-link callback and
            // rendered modal run; all viewer/inventory effects run normally.
            if (instance.type === Home) return;
            if (!same(instance.slots[index]?.deps, deps)) {
                const previous = instance.slots[index];
                const slot = instance.slots[index] = { deps };
                effects.push(() => { previous?.cleanup?.(); slot.cleanup = fn(); });
            }
        },
    };
    const Empty = () => null;
    const passThrough = ({ children }) => children;
    function load(file) {
        if (modules.has(file)) return modules.get(file);
        if (!compiled.has(file)) compiled.set(file, ts.transpileModule(fs.readFileSync(file, 'utf8'), {
            compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
        }).outputText);
        const localRequire = name => {
            if (name === 'react') return hooks;
            if (name === 'react/jsx-runtime') return require(name);
            if (name === 'react-dom') return { createPortal: child => child };
            if (name === 'lucide-react') return new Proxy({}, { get: () => Empty });
            if (name === 'react-markdown') return { __esModule: true, default: passThrough, defaultUrlTransform: url => url };
            if (name === 'rehype-sanitize') return { __esModule: true, default: Empty, defaultSchema: {} };
            if (name.startsWith('remark-') || name.startsWith('rehype-')) return Empty;
            if (name.endsWith('.css')) return { __esModule: true, default: css };
            if (name === '@/i18n/core') return { t: key => key, getFormatLocale: () => 'en-US' };
            if (name === '@/i18n/useLocale') return { useLocale: () => 'en' };
            if (name === '@/i18n/api') return { apiFetch: fetchMock, parseUIEvent: value => value };
            if (name.endsWith('ModalOverlay')) return { __esModule: true, default: passThrough };
            if (name.startsWith('@/hooks/')) return {
                useActivityTracker() {}, useAddonEvents() {},
                useActiveClientTab: () => ({ isActive: false }),
                useClientActions: () => ({ dispatch() {} }),
            };
            if (name === '@/lib/messageMarkdown') return { prepareMessageMarkdown: value => value };
            if (name.startsWith('@/lib/')) return {};
            if (name.startsWith('.') || name.startsWith('@/')) {
                const resolved = name.startsWith('@/') ? path.join(source, `${name.slice(2)}.tsx`) : path.resolve(path.dirname(file), `${name}.tsx`);
                // The allowlist uses repository-style separators on every host OS.
                if (realFiles.has(relativePath(source, resolved).replace(/\\/g, '/'))) return load(resolved);
                return { __esModule: true, default: Empty, ActiveClientIndicator: Empty };
            }
            throw new Error(`Unexpected dependency ${name}`);
        };
        const module = { exports: {} };
        modules.set(file, module.exports);
        new Function('require', 'module', 'exports', 'fetch', 'console', 'document', compiled.get(file))(
            localRequire, module, module.exports, fetchMock,
            { error: (...args) => notices.push(args), log() {}, warn() {} }, { body: {} },
        );
        modules.set(file, module.exports);
        return module.exports;
    }
    const Root = load(path.join(source, component)).default;
    const Home = component === 'app/page.tsx' ? Root : null;
    function expand(node, position, visited) {
        if (Array.isArray(node)) return node.map((child, i) => expand(child, `${position}/${child?.key ?? i}`, visited));
        if (!React.isValidElement(node)) return node;
        if (node.type === React.Fragment) return expand(node.props.children, `${position}/fragment`, visited);
        if (typeof node.type !== 'function') return { ...node, props: { ...node.props, children: expand(node.props.children, `${position}/children`, visited) } };
        if (!componentIds.has(node.type)) componentIds.set(node.type, ++nextId);
        const id = `${position}:${componentIds.get(node.type)}:${node.key ?? ''}`;
        let instance = instances.get(id);
        if (!instance) { instance = { type: node.type, slots: [], mounted: true }; instances.set(id, instance); }
        visited.add(id);
        active = instance; cursor = 0;
        seenComponents.push({ name: node.type.name, props: node.props, key: node.key });
        const result = node.type(node.props);
        return expand(result, `${id}/render`, visited);
    }
    function render() {
        dirty = false; effects = []; seenComponents.length = 0;
        const visited = new Set();
        tree = mounted ? expand([React.createElement(Root, rootProps), extra], 'root', visited) : null;
        for (const [id, instance] of instances) {
            if (visited.has(id)) continue;
            instance.mounted = false;
            instance.slots.forEach(slot => slot?.cleanup?.());
            instances.delete(id);
        }
        effects.splice(0).forEach(effect => effect());
        return tree;
    }
    async function settle() {
        for (let i = 0; i < 40; i++) {
            if (dirty) render();
            await new Promise(setImmediate);
            if (!dirty) return tree;
        }
        throw new Error('Component failed to settle');
    }
    return {
        settle, render, requests, notices, inventories, metadata, seenComponents,
        get tree() { return tree; }, get binaryJsonReads() { return binaryJsonReads; },
        queue(url, result) { const list = queued.get(url) || []; list.push(result); queued.set(url, list); },
        update(next) { rootProps = { ...rootProps, ...next }; dirty = true; return render(); },
        unmount() { mounted = false; dirty = true; return render(); },
        remount(next = rootProps) { mounted = true; rootProps = next; dirty = true; return render(); },
        async openChatLink(itemId) {
            assert.ok(Home, 'chat-link helper requires the real page');
            const home = [...instances.values()].find(instance => instance.type === Home);
            const components = home.slots.map(slot => slot?.value).find(value => typeof value?.a === 'function' && typeof value?.img === 'function');
            assert.ok(components, 'real page must supply Markdown link renderers');
            const href = `saiverse://item/${itemId}/content`;
            // Use the actual Markdown components.a -> SaiverseLink -> page callback.
            extra = components.a({ href, children: 'Synthetic item link' });
            dirty = true; render();
            await one(tree, node => node.type === 'a' && node.props.href === href, 'saiverse link').props.onClick({ preventDefault() {} });
            return settle();
        },
    };
}

function assertPicture(tree, item = picture) {
    assert.ok(header(tree).includes(item.NAME), 'resolved picture title');
    assert.ok(all(tree, node => hasClass(node, 'badge')).some(node => text(node) === 'picture'), 'resolved picture type');
    const img = one(tree, node => node.type === 'img' && node.props.src === `/api/info/item/${item.ITEM_ID}`, 'canonical picture');
    assert.equal(img.props.alt, item.NAME);
    assert.ok(text(tree).includes(item.DESCRIPTION), 'complete picture description');
}
function assertDocument(tree) {
    assert.ok(header(tree).includes(documentItem.NAME), 'resolved document title');
    assert.ok(all(tree, node => hasClass(node, 'badge')).some(node => text(node) === 'document'), 'resolved document type');
    assert.ok(text(tree).includes(documentItem.DESCRIPTION), 'complete document description');
    assert.ok(text(tree).includes(documentBody), 'document body');
}
function assertReadOnly(tree) {
    const forbidden = ['metaEditBtn', 'editBtn', 'saveBtn', 'deleteBtn', 'bulkBtn', 'bulkBar', 'metaEditSection', 'editTextarea'];
    for (const name of forbidden) assert.equal(all(tree, node => hasClass(node, name)).length, 0, `read-only view must hide ${name}`);
    assert.equal(all(tree, node => ['input', 'textarea', 'select'].includes(node.type)).length, 0, 'read-only view must have no edit inputs');
}

async function runCases(relativePath, label) {
    const harness = (component, props) => createHarness(component, props, relativePath);
    let cases = 0;
    // The real chat callback must resolve short and full references without
    // inspecting the current room. A picture must never enter the JSON branch.
    const chat = harness('app/page.tsx');
    await chat.settle();
    for (const id of ['41', picture.ITEM_ID]) {
        const tree = await chat.openChatLink(id);
        assertPicture(tree);
        assert.equal(chat.binaryJsonReads, 0, 'picture bytes must never be parsed as document JSON');
        assert.equal(chat.requests.filter(request => request.url.startsWith('/api/info/details')).length, 0, 'reference lookup must not depend on the current room');
        button(tree, 'closeBtn').props.onClick();
        assert.equal(all(await chat.settle(), node => hasClass(node, 'badge')).length, 0);
        cases++;
    }
    assertDocument(await chat.openChatLink('42'));
    assert.ok(chat.requests.some(request => request.url === `/api/info/item/${documentItem.ITEM_ID}`));
    button(chat.tree, 'closeBtn').props.onClick(); await chat.settle(); cases++;

    // Closing while metadata is still pending is a real visible close action,
    // and an ignored transport abort must not resurrect the page's viewer.
    const pendingLink = deferred();
    chat.queue('/api/world/items/41', pendingLink.promise);
    let pendingTree = await chat.openChatLink('41');
    assert.equal(all(pendingTree, node => node.props?.role === 'status').length, 1);
    button(pendingTree, 'closeBtn').props.onClick(); await chat.settle();
    pendingLink.resolve(response(picture)); pendingTree = await chat.settle();
    assert.equal(all(pendingTree, node => hasClass(node, 'badge') || node.props?.role === 'status').length, 0);
    assertDocument(await chat.openChatLink('42'));
    button(chat.tree, 'closeBtn').props.onClick(); await chat.settle(); cases++;

    // A binary response fixture demonstrates the previous fabricated-document
    // route really does fail, rather than merely asserting a URL string.
    const legacy = harness('components/ItemModal.tsx', { isOpen: true, item: { id: '41', name: '41', type: 'document' }, onClose() {} });
    const legacyTree = await legacy.settle();
    assert.ok(legacy.binaryJsonReads > 0, 'legacy fallback must reproduce image-as-JSON failure');
    assert.ok(all(legacyTree, node => hasClass(node, 'error')).length > 0); cases++;

    for (const result of [() => response({ detail: 'Item not found' }, 404), () => response({ detail: 'Deleted' }, 410), () => Promise.reject(new Error('Synthetic offline'))]) {
        const h = harness('components/ItemReferenceModal.tsx', { itemId: 'missing', onClose() {} });
        h.queue('/api/world/items/missing', result());
        const tree = await h.settle();
        assert.equal(all(tree, node => node.props?.role === 'alert').length, 1);
        assert.equal(all(tree, node => hasClass(node, 'badge')).length, 0, 'no fabricated item on lookup failure');
        assert.equal(h.requests.filter(request => request.url.startsWith('/api/info/item/')).length, 0);
        cases++;
    }

    const invalidKey = harness('components/ItemReferenceModal.tsx', { itemId: 'item:41', onClose() {} });
    assert.equal(all(await invalidKey.settle(), node => node.props?.role === 'alert').length, 1,
        'API accepts numeric short IDs or UUIDs, not the authored item:N label');
    assert.equal(invalidKey.requests[0].url, '/api/world/items/item%3A41', 'references are path-encoded'); cases++;

    const first = deferred(), next = deferred();
    const switching = harness('components/ItemReferenceModal.tsx', { itemId: '41', onClose() {} });
    switching.queue('/api/world/items/41', first.promise);
    switching.queue('/api/world/items/42', next.promise);
    assert.equal(all(await switching.settle(), node => node.props?.role === 'status').length, 1);
    switching.update({ itemId: '42' }); await switching.settle();
    assert.equal(switching.requests[0].init.signal.aborted, true, 'switch must abort prior lookup');
    next.resolve(response(documentItem)); assertDocument(await switching.settle());
    first.resolve(response(picture)); assertDocument(await switching.settle());
    assert.equal(all(switching.tree, node => node.type === 'img').length, 0, 'late picture cannot replace newer document');
    switching.update({ itemId: '41' }); assertPicture(await switching.settle());
    switching.update({ itemId: '42' }); assertDocument(await switching.settle()); cases++;

    // The metadata request can finish first while document bytes are still
    // pending. Switching must remount content state, so that older body cannot
    // overwrite a newer item or survive into a later reopen of that document.
    const body = deferred();
    const changingContent = harness('components/ItemReferenceModal.tsx', { itemId: '42', onClose() {} });
    changingContent.queue(`/api/info/item/${documentItem.ITEM_ID}`, body.promise);
    await changingContent.settle();
    changingContent.update({ itemId: '41' }); assertPicture(await changingContent.settle());
    body.resolve(response({ content: 'OBSOLETE DOCUMENT BODY' }));
    assertPicture(await changingContent.settle());
    assert.equal(text(changingContent.tree).includes('OBSOLETE DOCUMENT BODY'), false);
    changingContent.update({ itemId: '42' }); assertDocument(await changingContent.settle());
    assert.equal(text(changingContent.tree).includes('OBSOLETE DOCUMENT BODY'), false); cases++;

    const late = deferred();
    const closing = harness('components/ItemReferenceModal.tsx', { itemId: '41', onClose() {} });
    closing.queue('/api/world/items/41', late.promise);
    await closing.settle(); closing.unmount();
    assert.equal(closing.requests[0].init.signal.aborted, true, 'close must abort lookup');
    late.resolve(response(picture)); assert.equal(await closing.settle(), null, 'late response cannot reopen closed modal');
    closing.remount({ itemId: '42', onClose() {} }); assertDocument(await closing.settle()); cases++;

    const inventory = harness('components/InventoryModal.tsx', { isOpen: true, personaId: 'synthetic-a', onClose() {} });
    let tree = await inventory.settle();
    const cards = all(tree, node => hasClass(node, 'card'));
    assert.equal(cards.length, 3);
    for (const card of cards) assert.equal(card.type, 'button', 'inventory cards must support native keyboard activation');
    cards[0].props.onClick(); tree = await inventory.settle();
    assertPicture(tree); assertReadOnly(tree);
    assert.ok(inventory.seenComponents.some(node => node.name === 'ItemReferenceModal' && node.props.itemId === picture.ITEM_ID && node.props.readOnly === true));
    button(tree, 'closeBtn').props.onClick(); tree = await inventory.settle();
    assert.equal(all(tree, node => hasClass(node, 'card')).length, 3, 'closing details preserves inventory');
    all(tree, node => hasClass(node, 'card'))[1].props.onClick(); tree = await inventory.settle();
    assertDocument(tree); assertReadOnly(tree);
    button(tree, 'closeBtn').props.onClick(); tree = await inventory.settle();
    inventory.inventories.set('synthetic-a', [inventoryRow(documentItem)]);
    await button(tree, 'refreshBtn').props.onClick(); tree = await inventory.settle();
    assert.equal(all(tree, node => hasClass(node, 'card')).length, 1, 'refresh must display new inventory');
    assert.ok(text(all(tree, node => hasClass(node, 'card'))).includes(documentItem.NAME)); cases++;

    // Refreshes may finish out of order even if transport ignores abort.
    const oldRefresh = deferred(), newRefresh = deferred();
    inventory.queue('/api/people/synthetic-a/items', oldRefresh.promise);
    inventory.queue('/api/people/synthetic-a/items', newRefresh.promise);
    const refresh = button(tree, 'refreshBtn').props.onClick;
    const refreshing = refresh(); const newer = refresh();
    newRefresh.resolve(response([inventoryRow(picture)])); await newer; await inventory.settle();
    oldRefresh.resolve(response([inventoryRow(documentItem)])); await refreshing; tree = await inventory.settle();
    assert.ok(text(all(tree, node => hasClass(node, 'card'))).includes(picture.NAME), 'latest refresh wins'); cases++;

    const oldInventory = deferred();
    inventory.queue('/api/people/synthetic-a/items', oldInventory.promise);
    const refreshA = button(tree, 'refreshBtn').props.onClick(); await inventory.settle();
    inventory.update({ personaId: 'synthetic-b' }); tree = await inventory.settle();
    oldInventory.resolve(response([inventoryRow(picture)])); await refreshA; tree = await inventory.settle();
    assert.equal(all(tree, node => hasClass(node, 'card')).length, 1);
    assert.ok(text(all(tree, node => hasClass(node, 'card'))).includes(documentItem.NAME), 'persona switch ignores old list');
    all(tree, node => hasClass(node, 'card'))[0].props.onClick(); assertDocument(await inventory.settle());
    inventory.update({ personaId: 'synthetic-a' }); tree = await inventory.settle();
    assert.equal(all(tree, node => hasClass(node, 'badge')).length, 0, 'persona switch clears selected detail');
    all(tree, node => hasClass(node, 'card'))[0].props.onClick(); assertDocument(await inventory.settle());
    inventory.update({ isOpen: false }); assert.equal(text(await inventory.settle()), '');
    inventory.update({ isOpen: true }); tree = await inventory.settle();
    assert.equal(all(tree, node => hasClass(node, 'badge')).length, 0, 'reopening inventory must not reopen selected detail'); cases++;

    const closedList = deferred();
    inventory.queue('/api/people/synthetic-a/items', closedList.promise);
    const lastRefresh = button(tree, 'refreshBtn').props.onClick(); await inventory.settle();
    inventory.update({ isOpen: false }); await inventory.settle();
    closedList.resolve(response([inventoryRow(picture)])); await lastRefresh;
    assert.equal(text(await inventory.settle()), '', 'late inventory response cannot reopen list');
    inventory.update({ isOpen: true }); tree = await inventory.settle();
    assert.ok(text(all(tree, node => hasClass(node, 'card'))).includes(documentItem.NAME), 'reopen reloads current inventory');
    assert.equal(text(all(tree, node => hasClass(node, 'card'))).includes(picture.NAME), false); cases++;

    // A personal bag's nested item remains read-only, including inherited
    // stow/takeout controls even when the caller provides a current room.
    const bagViewer = harness('components/ItemReferenceModal.tsx', { itemId: bag.ITEM_ID, currentBuildingId: 'synthetic-room', readOnly: true, onClose() {} });
    tree = await bagViewer.settle(); assertReadOnly(tree);
    one(tree, node => hasClass(node, 'bagCard') && text(node).includes(documentItem.NAME), 'nested document card').props.onClick();
    tree = await bagViewer.settle(); assertDocument(tree); assertReadOnly(tree);
    assert.ok(bagViewer.seenComponents.filter(node => node.name === 'ItemModal').every(node => node.props.readOnly === true), 'nested viewers inherit read-only'); cases++;

    // Read-only is opt-in; existing room viewers retain their edit affordances.
    const editable = harness('components/ItemModal.tsx', { isOpen: true, item: itemProps(documentItem), onClose() {} });
    tree = await editable.settle(); button(tree, 'metaEditBtn'); button(tree, 'editBtn');
    button(tree, 'editBtn').props.onClick(); tree = await editable.settle(); button(tree, 'saveBtn');
    assert.equal(all(tree, node => node.type === 'textarea').length, 1); cases++;

    console.log(`${cases} item-viewer cases passed (${label}): real chat links, canonical media/document rendering, missing references, keyed lifetime/races, inventory buttons/refresh/persona isolation, and nested read-only controls.`);
}

(async () => {
    await runCases(path.relative, 'native paths');
    // Real Windows path semantics at the allowlist boundary, without replacing
    // host filesystem paths. This exercises child loading on non-Windows CI too.
    assert.ok(path.win32.relative(source, path.join(source, 'components/SaiverseLink.tsx')).includes('\\'));
    await runCases(path.win32.relative, 'Windows-relative paths');
})().catch(error => { console.error(error); process.exitCode = 1; });
