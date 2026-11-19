import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({plugins:[react()],base:'/',build:{outDir:'../static/app',emptyOutDir:true,rollupOptions:{output:{manualChunks:{vendor:['react','react-dom','antd','@ant-design/icons']}}}},server:{proxy:{'/api':'http://127.0.0.1:8000'}}});
