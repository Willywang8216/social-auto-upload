import { defineConfig } from '@playwright/test';
import * as os from 'os';

const hasDisplay = !!process.env.DISPLAY || !!process.env.XAUTHORITY || os.platform() !== 'linux';

export default defineConfig({
  testDir: 'demo',
  timeout: 300000,
  use: {
    browserName: 'chromium',
    headless: true,
    viewport: { width: 1920, height: 1080 },
    video: 'on',
    recordVideo: {
      dir: 'demo/output',
      size: { width: 1920, height: 1080 },
    },
    trace: 'retain-on-failure',
    baseURL: 'http://localhost:5409',
  },
  reporter: [['list']],
  outputDir: 'demo/output',
});