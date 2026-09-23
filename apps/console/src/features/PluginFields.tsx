import type { JsonObject, JsonValue } from '../api/contracts';

function object(value: JsonValue | undefined): JsonObject {
    return value !== null && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function properties(schema: JsonObject | undefined) {
    return Object.entries(object(schema?.properties));
}

// 字段来自插件 Manifest；这里只解释表单输入，最终约束由服务端 JSON Schema 校验。
export function readConfig(form: FormData, schema: JsonObject | undefined): JsonObject {
    const result: JsonObject = {};
    for (const [name, raw] of properties(schema)) {
        const field = object(raw);
        const value = form.get(`config.${name}`);
        if (field.type === 'boolean') result[name] = value === 'on';
        else if (value !== null && value !== '') {
            const text = String(value);
            const enums = Array.isArray(field.enum) ? field.enum : [];
            if (enums.length) {
                result[name] = enums.find((item) => String(item) === text) ?? text;
                continue;
            }
            result[name] =
                field.type === 'integer' || field.type === 'number'
                    ? Number(text)
                    : field.type === 'string'
                      ? text
                      : JSON.parse(text);
        }
    }
    return result;
}

export function PluginFields({ schema }: { schema: JsonObject | undefined }) {
    const required = Array.isArray(schema?.required) ? schema.required : [];
    return properties(schema).map(([name, raw]) => {
        const field = object(raw);
        const label = String(field.title || field.description || name);
        const common = { name: `config.${name}`, required: required.includes(name) };
        const initial = field.default;
        const values = Array.isArray(field.enum) ? field.enum : [];
        let input;
        if (field.type === 'boolean')
            input = (
                <input
                    {...common}
                    required={false}
                    type="checkbox"
                    defaultChecked={initial === true}
                />
            );
        else if (values.length)
            input = (
                <select {...common} defaultValue={String(initial ?? '')}>
                    <option value="">请选择</option>
                    {values.map((value) => (
                        <option key={String(value)} value={String(value)}>
                            {String(value)}
                        </option>
                    ))}
                </select>
            );
        else if (field.type === 'integer' || field.type === 'number')
            input = (
                <input
                    {...common}
                    type="number"
                    step={field.type === 'integer' ? 1 : 'any'}
                    min={typeof field.minimum === 'number' ? field.minimum : undefined}
                    max={typeof field.maximum === 'number' ? field.maximum : undefined}
                    defaultValue={initial == null ? '' : String(initial)}
                />
            );
        else if (field.type === 'string')
            input = (
                <textarea
                    {...common}
                    rows={name === 'prompt' ? 3 : 1}
                    maxLength={typeof field.maxLength === 'number' ? field.maxLength : 4000}
                    defaultValue={String(initial ?? '')}
                />
            );
        else
            input = (
                <textarea
                    {...common}
                    rows={4}
                    placeholder="JSON"
                    defaultValue={initial == null ? '' : JSON.stringify(initial, null, 2)}
                />
            );
        return (
            <label className={field.type === 'boolean' ? 'checkbox' : ''} key={name}>
                {label}
                {input}
            </label>
        );
    });
}
