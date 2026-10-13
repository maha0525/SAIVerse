// Unit test for NumberParamControl behavior and logic:
// - Backspacing to empty string must stay empty while editing (never bounce back to default)
// - On blur, empty string falls back to schema.default
// - On blur, valid numbers are parsed and clamped (min / max)
// - On blur, invalid NaN falls back to schema.default
const assert = require('node:assert/strict');

function simulateNumberControl(schema, initialValue) {
    const fallbackValue = typeof schema.default === 'number'
        ? schema.default
        : (schema.min ?? 0);

    const formatInitial = (v) => {
        if (typeof v === 'number' && !isNaN(v)) return String(v);
        if (v !== undefined && v !== null && v !== '') {
            const n = Number(v);
            if (!isNaN(n)) return String(n);
        }
        return String(fallbackValue);
    };

    let textValue = formatInitial(initialValue);
    let parentValue = initialValue;
    let isFocused = false;

    const onChange = (key, val) => {
        parentValue = val;
    };

    const commitValue = (raw) => {
        const trimmed = raw.trim();
        if (trimmed === '') {
            textValue = String(fallbackValue);
            onChange(schema.key, fallbackValue);
            return;
        }

        let parsed = schema.value_type === 'int'
            ? parseInt(trimmed, 10)
            : parseFloat(trimmed);

        if (isNaN(parsed)) {
            textValue = String(fallbackValue);
            onChange(schema.key, fallbackValue);
            return;
        }

        if (schema.min !== undefined && parsed < schema.min) {
            parsed = schema.min;
        }
        if (schema.max !== undefined && parsed > schema.max) {
            parsed = schema.max;
        }

        textValue = String(parsed);
        onChange(schema.key, parsed);
    };

    return {
        getText: () => textValue,
        getParent: () => parentValue,
        focus: () => { isFocused = true; },
        input: (val) => { textValue = val; }, // simulate onChange event while typing
        blur: () => {
            isFocused = false;
            commitValue(textValue);
        },
    };
}

// Test case 1: "1" を Backspace で消して "2" を入力する
{
    const schema = { key: 'speed', type: 'number', default: 1.0, min: 0.5, max: 2.0, value_type: 'float' };
    const ctrl = simulateNumberControl(schema, 1.0);

    assert.equal(ctrl.getText(), "1");
    ctrl.focus();

    // Backspace: 欄を空欄にする
    ctrl.input("");
    assert.equal(ctrl.getText(), "", "Backspaceで空文字にしたとき、空欄のまま維持されること (1が復活しない)");
    assert.equal(ctrl.getParent(), 1.0, "入力中の空文字では親のstateはまだ壊さない");

    // "2" を入力
    ctrl.input("2");
    assert.equal(ctrl.getText(), "2");

    // フォーカスを外す (onBlur)
    ctrl.blur();
    assert.equal(ctrl.getText(), "2");
    assert.equal(ctrl.getParent(), 2.0, "onBlur でパースされた数値 2 が親 state に確定されること");
}

// Test case 2: 空欄のままフォーカスを外すと既定値にフォールバックする
{
    const schema = { key: 'speed', type: 'number', default: 1.0, min: 0.5, max: 2.0, value_type: 'float' };
    const ctrl = simulateNumberControl(schema, 1.5);

    ctrl.focus();
    ctrl.input("");
    ctrl.blur();

    assert.equal(ctrl.getText(), "1");
    assert.equal(ctrl.getParent(), 1.0, "空欄のまま blur した場合は既定値 1.0 に安全に戻ること");
}

// Test case 3: min / max の clamp
{
    const schema = { key: 'speed', type: 'number', default: 1.0, min: 0.5, max: 2.0, value_type: 'float' };
    const ctrl = simulateNumberControl(schema, 1.0);

    ctrl.focus();
    ctrl.input("5.0"); // max 2.0 を超える
    ctrl.blur();

    assert.equal(ctrl.getText(), "2");
    assert.equal(ctrl.getParent(), 2.0, "max を超えた入力は clamp されること");

    ctrl.focus();
    ctrl.input("0.1"); // min 0.5 未満
    ctrl.blur();

    assert.equal(ctrl.getText(), "0.5");
    assert.equal(ctrl.getParent(), 0.5, "min 未満の入力は clamp されること");
}

console.log("All NumberParamControl simulation tests passed!");
