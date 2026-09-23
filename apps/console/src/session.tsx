import { createContext, useContext } from 'react';
import type { Identity } from './api/contracts';

export const SessionContext = createContext<Identity | null>(null);
export function useSession() {
    const value = useContext(SessionContext);
    if (!value) throw new Error('Session context missing');
    return value;
}
export function usePermission(permission: string) {
    return useSession().permissions.includes(permission);
}
