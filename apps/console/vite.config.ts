import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
    plugins: [react()],
    build: { assetsDir: 'static' },
    server: {
        port: 5173,
        strictPort: true,
        proxy: Object.fromEntries(
            ['/v1', '/auth', '/admin'].map((path) => [
                path,
                {
                    target: process.env.SENSORYPLEX_API_URL || 'http://127.0.0.1:8091',
                    changeOrigin: false,
                },
            ]),
        ),
    },
});
