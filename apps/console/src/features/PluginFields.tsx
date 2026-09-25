import { Input, Switch, Typography } from 'antd';
import type { JsonObject, JsonValue } from '../api/contracts';

const { Text } = Typography;

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
        if (field.type === 'boolean') result[name] = value === 'on' || value === 'true';
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

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {properties(schema).map(([name, raw]) => {
                const field = object(raw);
                const label = String(field.title || field.description || name);
                const isRequired = required.includes(name);
                const initial = field.default;
                const values = Array.isArray(field.enum) ? field.enum : [];

                let inputNode;
                if (field.type === 'boolean') {
                    inputNode = (
                        <div style={{ marginTop: 4 }}>
                            <Switch
                                defaultChecked={initial === true}
                                onChange={(checked) => {
                                    // 模拟 input 表单行为
                                    const hidden = document.getElementById(
                                        `config-hidden-${name}`,
                                    ) as HTMLInputElement;
                                    if (hidden) hidden.value = checked ? 'true' : 'false';
                                }}
                            />
                            <input
                                id={`config-hidden-${name}`}
                                type="hidden"
                                name={`config.${name}`}
                                defaultValue={initial === true ? 'true' : 'false'}
                            />
                        </div>
                    );
                } else if (values.length) {
                    inputNode = (
                        <select
                            name={`config.${name}`}
                            required={isRequired}
                            defaultValue={String(initial ?? '')}
                            style={{
                                width: '100%',
                                padding: '6px 10px',
                                borderRadius: 6,
                                border: '1px solid #d9d9d9',
                                marginTop: 4,
                                fontSize: 13,
                            }}
                        >
                            <option value="">请选择</option>
                            {values.map((v) => (
                                <option key={String(v)} value={String(v)}>
                                    {String(v)}
                                </option>
                            ))}
                        </select>
                    );
                } else if (field.type === 'integer' || field.type === 'number') {
                    inputNode = (
                        <div style={{ marginTop: 4 }}>
                            <Input
                                name={`config.${name}`}
                                type="number"
                                required={isRequired}
                                step={field.type === 'integer' ? '1' : 'any'}
                                min={typeof field.minimum === 'number' ? field.minimum : undefined}
                                max={typeof field.maximum === 'number' ? field.maximum : undefined}
                                defaultValue={initial == null ? '' : String(initial)}
                            />
                        </div>
                    );
                } else if (field.type === 'string') {
                    inputNode = (
                        <div style={{ marginTop: 4 }}>
                            <Input.TextArea
                                name={`config.${name}`}
                                required={isRequired}
                                rows={name === 'prompt' ? 3 : 1}
                                maxLength={
                                    typeof field.maxLength === 'number' ? field.maxLength : 4000
                                }
                                defaultValue={String(initial ?? '')}
                            />
                        </div>
                    );
                } else {
                    inputNode = (
                        <div style={{ marginTop: 4 }}>
                            <Input.TextArea
                                name={`config.${name}`}
                                rows={4}
                                placeholder="输入符合规范的 JSON"
                                defaultValue={
                                    initial == null ? '' : JSON.stringify(initial, null, 2)
                                }
                            />
                        </div>
                    );
                }

                return (
                    <div key={name}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                            <Text strong style={{ fontSize: 13 }}>
                                {label}
                            </Text>
                            {isRequired ? <span style={{ color: '#ef4444' }}>*</span> : null}
                            <span className="mono" style={{ fontSize: 11, color: '#94a3b8' }}>
                                ({name})
                            </span>
                        </div>
                        {inputNode}
                    </div>
                );
            })}
        </div>
    );
}
