import { Input, Typography } from 'antd';
import type { JsonObject, JsonValue } from '../api/contracts';

const object = (value: JsonValue | undefined): JsonObject =>
    value !== null && typeof value === 'object' && !Array.isArray(value) ? value : {};
const fieldName = (path: string[]) => `config:${JSON.stringify(path)}`;

function resolve(schema: JsonObject, root: JsonObject): JsonObject {
    if (typeof schema.$ref !== 'string' || !schema.$ref.startsWith('#/')) return schema;
    let target: JsonValue | undefined = root;
    for (const part of schema.$ref.slice(2).split('/')) {
        target = object(target)[part.replace(/~1/g, '/').replace(/~0/g, '~')];
    }
    const merged = { ...object(target), ...schema };
    delete merged.$ref;
    return merged;
}

function fieldType(schema: JsonObject) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    return { type: types.find((t) => t !== 'null'), nullable: types.includes('null') };
}

// 默认值和最终约束由服务端统一计算；省略字段与显式 null 使用不同语义。
export function readConfig(form: FormData, schema: JsonObject | undefined): JsonObject {
    const root = schema || {};
    const read = (raw: JsonObject, path: string[], depth: number): JsonValue | undefined => {
        const field = resolve(raw, root);
        const { type, nullable } = fieldType(field);
        const name = fieldName(path);
        if (nullable && form.get(`${name}:null`) === 'on') return null;
        if (type === 'object' && field.properties && depth < 8) {
            const result: JsonObject = {};
            for (const [key, child] of Object.entries(object(field.properties))) {
                const value = read(object(child), [...path, key], depth + 1);
                if (value !== undefined) result[key] = value;
            }
            return Object.keys(result).length || !path.length ? result : undefined;
        }
        const value = form.get(name);
        if (value === null || value === '') return undefined;
        const text = String(value);
        if (Array.isArray(field.enum)) return JSON.parse(text) as JsonValue;
        if (type === 'string') return text;
        if (type === 'boolean') return text === 'true';
        if (type === 'number' || type === 'integer') {
            const number = Number(text);
            if (!Number.isFinite(number) || (type === 'integer' && !Number.isInteger(number))) {
                throw new Error(
                    `字段 ${path.join('.')} 需要${type === 'integer' ? '整数' : '有效数字'}`,
                );
            }
            return number;
        }
        return JSON.parse(text) as JsonValue;
    };
    return object(read(root, [], 0));
}

function SchemaField({
    raw,
    root,
    path,
    required,
    initial,
    depth,
}: {
    raw: JsonObject;
    root: JsonObject;
    path: string[];
    required: boolean;
    initial?: JsonValue;
    depth: number;
}) {
    const field = resolve(raw, root);
    const { type, nullable } = fieldType(field);
    const value = initial === undefined ? field.default : initial;
    const name = fieldName(path);
    const label = String(field.title || path[path.length - 1] || '配置');
    const requiredFields = Array.isArray(field.required) ? field.required : [];
    const nested = type === 'object' && field.properties && depth < 8;
    const enums = Array.isArray(field.enum) ? field.enum : [];
    return (
        <div style={{ marginBottom: 14 }}>
            <label htmlFor={name} style={{ display: 'block', marginBottom: 5 }}>
                <Typography.Text strong>{label}</Typography.Text>
                {required ? ' *' : ''}
            </label>
            {nested ? (
                <fieldset style={{ border: '1px solid #e2e8f0', padding: 12, borderRadius: 6 }}>
                    {Object.entries(object(field.properties))
                        .slice(0, 128)
                        .map(([key, child]) => (
                            <SchemaField
                                key={key}
                                raw={object(child)}
                                root={root}
                                path={[...path, key]}
                                required={requiredFields.includes(key)}
                                initial={object(value)[key]}
                                depth={depth + 1}
                            />
                        ))}
                </fieldset>
            ) : enums.length || type === 'boolean' ? (
                <select
                    id={name}
                    name={name}
                    defaultValue={
                        value === undefined
                            ? ''
                            : enums.length
                              ? JSON.stringify(value)
                              : String(value)
                    }
                    required={required && !nullable}
                    style={{ width: '100%', padding: 8, borderRadius: 6 }}
                >
                    <option value="">使用默认值 / 未填写</option>
                    {(enums.length ? enums : [true, false]).map((item) => (
                        <option
                            key={JSON.stringify(item)}
                            value={enums.length ? JSON.stringify(item) : String(item)}
                        >
                            {item === null ? 'null' : String(item)}
                        </option>
                    ))}
                </select>
            ) : type === 'integer' || type === 'number' ? (
                <Input
                    id={name}
                    name={name}
                    type="number"
                    step={type === 'integer' ? 1 : 'any'}
                    required={required && !nullable}
                    min={typeof field.minimum === 'number' ? field.minimum : undefined}
                    max={typeof field.maximum === 'number' ? field.maximum : undefined}
                    defaultValue={value == null ? '' : String(value)}
                />
            ) : (
                <Input.TextArea
                    id={name}
                    name={name}
                    required={required && !nullable}
                    rows={type === 'string' ? 2 : 4}
                    maxLength={65536}
                    placeholder={type === 'string' ? '' : '输入对象或数组 JSON'}
                    defaultValue={
                        value == null
                            ? ''
                            : type === 'string'
                              ? String(value)
                              : JSON.stringify(value, null, 2)
                    }
                />
            )}
            {nullable ? (
                <label style={{ display: 'block', marginTop: 5 }}>
                    <input type="checkbox" name={`${name}:null`} defaultChecked={value === null} />{' '}
                    设置为 null
                </label>
            ) : null}
            {field.description ? (
                <Typography.Text type="secondary">{String(field.description)}</Typography.Text>
            ) : null}
        </div>
    );
}

export function PluginFields({ schema }: { schema: JsonObject | undefined }) {
    const root = schema || {};
    return (
        <SchemaField raw={root} root={root} path={[]} required initial={root.default} depth={0} />
    );
}
