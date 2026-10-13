// Unit test for NumberParamControl behavior and logic:
// Loads real AddonManagerModal.tsx via TypeScript transpileModule.
// Tests both extracted pure functions (resolveNumberParamCommit, formatNumberInitial)
// and the actual NumberParamControl React component under simulated user interactions:
// - Backspacing to empty string must stay empty while editing (never bounce back to default)
// - On blur, empty string falls back to schema.default
// - On blur, valid numbers are parsed and clamped (min / max)
// - On blur, invalid NaN falls back to schema.default
// - Pressing Enter triggers blur and commits the value
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');

const file = path.resolve(__dirname, '../src/components/AddonManagerModal.tsx');
const source = fs.readFileSync(file, 'utf8');

const output = ts.transpileModule(source, {
    compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
        target: ts.ScriptTarget.ES2022,
        esModuleInterop: true,
    },
}).outputText;

let activeHooks = {
    useState: () => { throw new Error('No active harness'); },
    useRef: () => { throw new Error('No active harness'); },
    useEffect: () => {},
    useCallback: (fn) => fn,
};

const reactShim = {
    ...React,
    useState: (...args) => activeHooks.useState(...args),
    useRef: (...args) => activeHooks.useRef(...args),
    useEffect: (...args) => activeHooks.useEffect(...args),
    useCallback: (...args) => activeHooks.useCallback(...args),
};

const localRequire = (name) => {
    if (name === 'react') return reactShim;
    if (name === '@/i18n/api') return { apiFetch: () => Promise.resolve() };
    if (name === '@/i18n/core') return { t: (k) => k };
    if (name === '@/i18n/useLocale') return { useLocale: () => 'ja' };
    if (name === '@/i18n/resolve') return { resolveI18nText: (t) => t };
    if (name === 'lucide-react') return new Proxy({}, { get: () => () => null });
    if (name === './common/ModalOverlay') return { __esModule: true, default: ({ children }) => children };
    if (name === './MCPSection' || name === './ActionsPanel' || name === './OAuthFlowSection' || name === './AddonCatalogPanel' || name === './AddonActionConfirmDialog' || name === './AddonInstallProgressDialog') {
        return { __esModule: true, default: () => null };
    }
    if (name === '../addon-panels.generated') return { ADDON_PANELS: [] };
    if (name.endsWith('.css') || name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_, k) => k }) };
    return require(name);
};

const context = {
    exports: {},
    module: { exports: {} },
    require: localRequire,
    __filename: file,
    __dirname: path.dirname(file),
};

const fn = new Function('exports', 'require', 'module', '__filename', '__dirname', output);
fn(context.exports, context.require, context.module, context.__filename, context.__dirname);

const { NumberParamControl, resolveNumberParamCommit, formatNumberInitial } = context.exports;

assert.equal(typeof NumberParamControl, 'function', 'NumberParamControl must be exported');
assert.equal(typeof resolveNumberParamCommit, 'function', 'resolveNumberParamCommit must be exported');
assert.equal(typeof formatNumberInitial, 'function', 'formatNumberInitial must be exported');

// ===========================================================================
// Part 1: Direct tests of extracted core logic functions
// ===========================================================================
{
    const floatSchema = { key: 'speed', type: 'number', default: 1.0, min: 0.5, max: 2.0, value_type: 'float' };
    const intSchema = { key: 'count', type: 'number', default: 3, min: 1, max: 10, value_type: 'int' };

    // formatNumberInitial
    assert.equal(formatNumberInitial(floatSchema, 1.0), '1');
    assert.equal(formatNumberInitial(floatSchema, 1.25), '1.25');
    assert.equal(formatNumberInitial(floatSchema, undefined), '1');
    assert.equal(formatNumberInitial(floatSchema, null), '1');
    assert.equal(formatNumberInitial(floatSchema, ''), '1');
    assert.equal(formatNumberInitial(floatSchema, NaN), '1');

    // resolveNumberParamCommit - empty string fallback
    assert.deepEqual(resolveNumberParamCommit(floatSchema, ''), { text: '1', value: 1.0 });
    assert.deepEqual(resolveNumberParamCommit(floatSchema, '   '), { text: '1', value: 1.0 });

    // resolveNumberParamCommit - valid float
    assert.deepEqual(resolveNumberParamCommit(floatSchema, '1.5'), { text: '1.5', value: 1.5 });
    assert.deepEqual(resolveNumberParamCommit(floatSchema, '2'), { text: '2', value: 2.0 });

    // resolveNumberParamCommit - valid int
    assert.deepEqual(resolveNumberParamCommit(intSchema, '5'), { text: '5', value: 5 });
    assert.deepEqual(resolveNumberParamCommit(intSchema, '5.8'), { text: '5', value: 5 });

    // resolveNumberParamCommit - invalid NaN fallback
    assert.deepEqual(resolveNumberParamCommit(floatSchema, 'not-a-number'), { text: '1', value: 1.0 });

    // resolveNumberParamCommit - clamp min / max
    assert.deepEqual(resolveNumberParamCommit(floatSchema, '5.0'), { text: '2', value: 2.0 });
    assert.deepEqual(resolveNumberParamCommit(floatSchema, '0.1'), { text: '0.5', value: 0.5 });
    assert.deepEqual(resolveNumberParamCommit(intSchema, '99'), { text: '10', value: 10 });
    assert.deepEqual(resolveNumberParamCommit(intSchema, '0'), { text: '1', value: 1 });
}

// ===========================================================================
// Part 2: Component mounting and event execution tests
// ===========================================================================
function mountNumberParamControl(schema, initialValue) {
    const states = [];
    const stateSetters = [];
    const refs = [];
    let stateIdx = 0;
    let refIdx = 0;

    let parentValue = initialValue;
    const parentCalls = [];
    const onParentChange = (key, val) => {
        parentValue = val;
        parentCalls.push({ key, val });
    };

    activeHooks = {
        useState(initial) {
            const i = stateIdx++;
            if (!(i in states)) {
                states[i] = typeof initial === 'function' ? initial() : initial;
            }
            if (!stateSetters[i]) {
                stateSetters[i] = (v) => {
                    const next = typeof v === 'function' ? v(states[i]) : v;
                    states[i] = next;
                };
            }
            return [states[i], stateSetters[i]];
        },
        useRef(initial) {
            const i = refIdx++;
            if (!(i in refs)) {
                refs[i] = { current: initial };
            }
            return refs[i];
        },
        useEffect(fn, deps) {
            // No-op for synchronous render inspection
        },
        useCallback: (fn) => fn,
    };

    function render() {
        stateIdx = 0;
        refIdx = 0;
        return NumberParamControl({
            schema,
            value: parentValue,
            onChange: onParentChange,
        });
    }

    return {
        render,
        getParentValue: () => parentValue,
        getParentCalls: () => parentCalls,
    };
}

// Component Test 1: "1" を Backspace で消して空文字 -> その後 "2" を入力 -> onBlur で 2.0 が親に渡る
{
    const schema = { key: 'speed', type: 'number', default: 1.0, min: 0.5, max: 2.0, value_type: 'float' };
    const harness = mountNumberParamControl(schema, 1.0);

    let vdom = harness.render();
    assert.equal(vdom.props.value, '1', '初期表示は "1"');

    // フォーカス
    vdom.props.onFocus();

    // Backspace: 入力欄を空文字にする
    vdom.props.onChange({ target: { value: '' } });
    vdom = harness.render();
    assert.equal(vdom.props.value, '', 'Backspace で空文字にしたとき、空欄のまま維持されること');
    assert.equal(harness.getParentCalls().length, 0, '編集中は親 onChange を呼ばない');

    // "2" を入力
    vdom.props.onChange({ target: { value: '2' } });
    vdom = harness.render();
    assert.equal(vdom.props.value, '2', '"2" が入力欄に反映されること');

    // フォーカスアウト (onBlur)
    vdom.props.onBlur({ target: { value: '2' } });
    vdom = harness.render();
    assert.equal(vdom.props.value, '2', '確定後も表示は "2"');
    assert.equal(harness.getParentValue(), 2.0, 'onBlur で親 state に数値 2.0 が確定されること');
    assert.equal(harness.getParentCalls().length, 1);
    assert.deepEqual(harness.getParentCalls()[0], { key: 'speed', val: 2.0 });
}

// Component Test 2: 空欄のまま blur すると既定値にフォールバック
{
    const schema = { key: 'speed', type: 'number', default: 1.0, min: 0.5, max: 2.0, value_type: 'float' };
    const harness = mountNumberParamControl(schema, 1.5);

    let vdom = harness.render();
    assert.equal(vdom.props.value, '1.5');

    vdom.props.onFocus();
    vdom.props.onChange({ target: { value: '' } });
    vdom = harness.render();
    assert.equal(vdom.props.value, '');

    vdom.props.onBlur({ target: { value: '' } });
    vdom = harness.render();
    assert.equal(vdom.props.value, '1', '空欄で blur すると表示が既定値 "1" に戻る');
    assert.equal(harness.getParentValue(), 1.0, '親 state に既定値 1.0 が渡る');
}

// Component Test 3: min / max の clamp がコンポーネント経由で動作する
{
    const schema = { key: 'speed', type: 'number', default: 1.0, min: 0.5, max: 2.0, value_type: 'float' };
    const harness = mountNumberParamControl(schema, 1.0);

    let vdom = harness.render();
    vdom.props.onFocus();
    vdom.props.onChange({ target: { value: '99' } });
    vdom.props.onBlur({ target: { value: '99' } });
    vdom = harness.render();

    assert.equal(vdom.props.value, '2', 'max 超過が 2 に clamp される');
    assert.equal(harness.getParentValue(), 2.0);
}

// Component Test 4: Enter キー押下で blur が呼ばれる
{
    const schema = { key: 'speed', type: 'number', default: 1.0, min: 0.5, max: 2.0, value_type: 'float' };
    const harness = mountNumberParamControl(schema, 1.0);

    let vdom = harness.render();
    let blurred = false;
    vdom.props.onKeyDown({
        key: 'Enter',
        currentTarget: {
            blur() {
                blurred = true;
            },
        },
    });
    assert.equal(blurred, true, 'Enter 押下で currentTarget.blur() が呼ばれること');
}

console.log('All NumberParamControl component & logic tests passed!');
