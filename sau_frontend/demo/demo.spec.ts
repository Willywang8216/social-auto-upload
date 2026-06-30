import { test, chromium, Page, BrowserContext } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/**
 * TikTok API Audit Demo — 17 scenes with in-frame workflow tracker.
 *
 * 1920x1080 viewport. Right-side workflow panel shows all steps.
 * Pulsing highlights + reviewer callouts mark what the reviewer must verify.
 * Everything stays IN FRAME — no scrolling that hides content.
 *
 * Run from sau_frontend/:
 *   npx playwright test demo/demo.spec.ts
 */

const BASE = 'http://localhost:5409';
const OUTPUT_DIR = path.resolve(__dirname, 'output');
const TOTAL_SCENES = 17;
const SCENE_WAIT = 12000;
const AUTH_TOKEN = 'sdkjauashuaiuhHOHEIUFhfaphgeiusdiu545gfdgd';

// Width reserved for the workflow panel on the right
const PANEL_WIDTH = 340;

// Scene definitions: [step, requirementId, label]
const SCENE_DEFS: [number, string, string][] = [
  [1,  '',     'App Overview'],
  [2,  '',     'Publish Center'],
  [3,  '',     'Account Selection'],
  [4,  '1a',   'Creator Info Display'],
  [5,  '1b',   'Post Limit Check'],
  [6,  '1c',   'Media Upload & Duration'],
  [7,  '',     'AI Draft Generation'],
  [8,  '2a',   'Title Field'],
  [9,  '2b',   'Privacy Dropdown'],
  [10, '2c',   'Interaction Settings'],
  [11, '3a',   'Commercial Disclosure'],
  [12, '3b',   'Branded Content Guard'],
  [13, '4',    'Dynamic Declaration'],
  [14, '5a',   'Content Preview'],
  [15, '5c',   'Explicit Consent'],
  [16, '5c',   'Review Modal'],
  [17, '5d',   'Processing Notice'],
];

const MOCK_CREATOR_INFO = {
  code: 200,
  data: {
    creator_nickname: 'Demo Creator',
    remaining_post_count: 8,
    max_video_post_duration_sec: 600,
    privacy_level_options: [
      'PUBLIC_TO_EVERYONE',
      'FOLLOWER_OF_CREATOR',
      'MUTUAL_FOLLOW_FRIENDS',
      'SELF_ONLY',
    ],
    comment_disabled: false,
    duet_disabled: false,
    stitch_disabled: false,
  },
  msg: 'ok',
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Inject CSS keyframes once */
async function injectAnimations(page: Page) {
  await page.evaluate(() => {
    if (document.getElementById('demo-animations')) return;
    const style = document.createElement('style');
    style.id = 'demo-animations';
    style.textContent = `
      @keyframes demo-pulse {
        0%, 100% { border-color: #f0c040; box-shadow: 0 0 8px rgba(240,192,64,0.4); }
        50%      { border-color: #409eff; box-shadow: 0 0 20px rgba(64,158,255,0.6); }
      }
      @keyframes demo-arrow-bounce {
        0%, 100% { transform: translateX(0); }
        50%      { transform: translateX(6px); }
      }
      @keyframes demo-fade-in {
        from { opacity: 0; transform: translateY(-8px); }
        to   { opacity: 1; transform: translateY(0); }
      }
    `;
    document.head.appendChild(style);
  });
}

/**
 * Workflow tracker panel — fixed right sidebar showing all 17 steps.
 * Injected once, then updated per scene.
 */
async function workflowPanel(page: Page, currentStep: number) {
  await page.evaluate(([step, defs, panelW]) => {
    let panel = document.getElementById('demo-workflow-panel');
    if (!panel) {
      panel = document.createElement('div');
      panel.id = 'demo-workflow-panel';
      document.body.appendChild(panel);
    }
    Object.assign(panel.style, {
      position: 'fixed', top: '0', right: '0',
      width: panelW + 'px', height: '100vh',
      zIndex: '99990',
      background: 'rgba(15,15,25,0.92)',
      borderLeft: '2px solid rgba(240,192,64,0.6)',
      fontFamily: 'Arial, sans-serif',
      overflowY: 'auto',
      padding: '0',
      boxSizing: 'border-box',
      pointerEvents: 'none',
    });

    let html = `<div style="padding:16px 14px 10px;border-bottom:1px solid rgba(255,255,255,0.15);background:rgba(240,192,64,0.12)">
      <div style="font-size:13px;font-weight:bold;color:#f0c040;letter-spacing:0.5px">TIKTOK API REVIEW</div>
      <div style="font-size:11px;color:rgba(255,255,255,0.6);margin-top:2px">Complete Workflow — ${defs.length} Steps</div>
    </div><div style="padding:8px 0">`;

    for (const [num, reqId, label] of defs as [number,string,string][]) {
      const isCurrent = num === step;
      const isDone = num < step;
      const bg = isCurrent ? 'rgba(240,192,64,0.18)' : 'transparent';
      const textColor = isCurrent ? '#f0c040' : isDone ? '#67c23a' : 'rgba(255,255,255,0.4)';
      const icon = isDone ? '✓' : isCurrent ? '▶' : '○';
      const border = isCurrent ? '3px solid #f0c040' : '3px solid transparent';
      const reqTag = reqId ? `<span style="background:rgba(64,158,255,0.3);color:#409eff;font-size:9px;padding:1px 5px;border-radius:3px;margin-left:6px">${reqId}</span>` : '';
      html += `<div style="display:flex;align-items:center;padding:6px 14px;background:${bg};border-left:${border};margin:1px 0">
        <span style="font-size:12px;width:18px;color:${textColor};font-weight:bold">${icon}</span>
        <span style="font-size:11px;color:${textColor};flex:1">${label}</span>
        ${reqTag}
      </div>`;
    }
    html += '</div>';
    panel.innerHTML = html;
  }, [currentStep, SCENE_DEFS, PANEL_WIDTH]);
}

/** Scene title banner — top center */
async function sceneTitle(page: Page, step: number, reqId: string, label: string) {
  await page.evaluate(([s, r, l, panelW]) => {
    document.querySelectorAll('.demo-scene-title').forEach(el => el.remove());
    const div = document.createElement('div');
    div.className = 'demo-scene-title';
    const reqBadge = r ? ` <span style="background:#409eff;color:#fff;font-size:12px;padding:2px 8px;border-radius:4px;margin-left:8px">Req ${r}</span>` : '';
    div.innerHTML = `<span style="color:#f0c040">Scene ${s}/${17}</span> — ${l}${reqBadge}`;
    Object.assign(div.style, {
      position: 'fixed', top: '12px', left: '50%', transform: 'translateX(-50%)',
      zIndex: '99999', background: 'rgba(0,0,0,0.85)', color: '#fff',
      fontFamily: 'Arial, sans-serif', fontSize: '16px', fontWeight: 'bold',
      padding: '10px 28px', borderRadius: '8px',
      border: '2px solid #f0c040',
      pointerEvents: 'none', whiteSpace: 'nowrap',
      maxWidth: `calc(100vw - ${panelW + 40}px)`,
      textAlign: 'center',
      animation: 'demo-fade-in 0.4s ease-out',
    });
    document.body.appendChild(div);
  }, [step, reqId, label, PANEL_WIDTH]);
  await page.waitForTimeout(400);
}

/** Pulsing highlight box around an element with reviewer callout */
async function pulsingHighlight(page: Page, selector: string, reviewerNote: string) {
  await page.evaluate(([sel, note, panelW]) => {
    document.querySelectorAll('.demo-highlight,.demo-callout').forEach(el => el.remove());
    const el = document.querySelector(sel);
    if (!el) return;
    const rect = el.getBoundingClientRect();

    // Pulsing box
    const box = document.createElement('div');
    box.className = 'demo-highlight';
    Object.assign(box.style, {
      position: 'fixed',
      top: `${rect.top - 6}px`, left: `${rect.left - 6}px`,
      width: `${rect.width + 12}px`, height: `${rect.height + 12}px`,
      zIndex: '99998',
      border: '3px solid #f0c040', borderRadius: '6px',
      pointerEvents: 'none',
      animation: 'demo-pulse 1.2s ease-in-out infinite',
    });
    document.body.appendChild(box);

    // Reviewer callout below the box
    if (note) {
      const callout = document.createElement('div');
      callout.className = 'demo-callout';
      const top = Math.min(rect.bottom + 10, window.innerHeight - 60);
      const left = Math.min(rect.left, window.innerWidth - panelW - 320);
      Object.assign(callout.style, {
        position: 'fixed', top: `${top}px`, left: `${left}px`,
        zIndex: '99999', background: 'rgba(240,64,64,0.92)', color: '#fff',
        fontFamily: 'Arial, sans-serif', fontSize: '12px', fontWeight: 'bold',
        padding: '6px 14px', borderRadius: '4px', maxWidth: '320px',
        pointerEvents: 'none', whiteSpace: 'nowrap',
        animation: 'demo-fade-in 0.3s ease-out',
      });
      callout.innerHTML = `<span style="color:#ffeb3b">🔍 REVIEWER:</span> ${note}`;
      document.body.appendChild(callout);
    }
  }, [selector, reviewerNote, PANEL_WIDTH]);
  await page.waitForTimeout(300);
}

/** Simple highlight without reviewer note (for context scenes) */
async function simpleHighlight(page: Page, selector: string) {
  await page.evaluate((sel) => {
    document.querySelectorAll('.demo-highlight').forEach(el => el.remove());
    const el = document.querySelector(sel);
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const box = document.createElement('div');
    box.className = 'demo-highlight';
    Object.assign(box.style, {
      position: 'fixed',
      top: `${rect.top - 4}px`, left: `${rect.left - 4}px`,
      width: `${rect.width + 8}px`, height: `${rect.height + 8}px`,
      zIndex: '99998',
      border: '3px solid #409eff', borderRadius: '6px',
      pointerEvents: 'none',
      boxShadow: '0 0 12px rgba(64,158,255,0.4)',
    });
    document.body.appendChild(box);
  }, selector);
  await page.waitForTimeout(200);
}

/** Screenshot helper */
async function shot(page: Page, name: string) {
  const p = path.join(OUTPUT_DIR, `screenshot_${name}.png`);
  await page.screenshot({ path: p, fullPage: false });
  console.log(`  [shot] ${p}`);
}

// ─── Main test ───────────────────────────────────────────────────────────────

test('TikTok API Audit Demo — Full Walkthrough (17 scenes)', async () => {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    recordVideo: { dir: OUTPUT_DIR, size: { width: 1920, height: 1080 } },
  });

  await context.addInitScript((token) => {
    localStorage.setItem('sau-auth-token', token);
  }, AUTH_TOKEN);

  const page = await context.newPage();
  page.setDefaultTimeout(15000);

  // Mock TikTok creator_info API
  await page.route('**/tiktok/creator-info/**', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(MOCK_CREATOR_INFO),
    });
  });

  // Mock publish-center preview (AI draft generation)
  await page.route('**/publish-center/preview', async (route) => {
    const req = route.request();
    const body = req.postDataJSON ? req.postDataJSON() : {};
    const accountId = body.selectedAccountIds?.[0] || 101;
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        code: 200,
        data: {
          drafts: {
            [accountId]: {
              title: 'TikTok API Audit Demo — Direct Post',
              message: 'Showing the complete TikTok Content Posting API integration with full UX compliance. #TikTokAPI #DirectPost',
              firstComment: 'Built with Social Auto Upload — multi-platform content publisher.',
            },
          },
        },
        msg: 'ok',
      }),
    });
  });

  try {
    await injectAnimations(page);

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 1: App branding — landing page
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 1: App landing');
    await workflowPanel(page, 1);
    await sceneTitle(page, 1, '', 'Social Auto Upload — Multi-Platform Content Publisher');
    await page.goto(BASE, { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(2000);
    await shot(page, '01_app_landing');
    await page.waitForTimeout(SCENE_WAIT - 2400);

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 2: Navigate to Publish Center
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 2: Publish Center');
    await workflowPanel(page, 2);
    await sceneTitle(page, 2, '', 'Publish Center — Full Post Workflow');
    await page.goto(`${BASE}/#/publish/compose`, { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(2000);
    await shot(page, '02_publish_center');
    await page.waitForTimeout(SCENE_WAIT - 2400);

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 3: Select profile + TikTok account
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 3: Select profile + TikTok account');
    await workflowPanel(page, 3);
    await sceneTitle(page, 3, '', 'Selecting TikTok Account');

    const profileCheckbox = page.locator('.el-checkbox').filter({ hasText: /NW/i }).first();
    if (await profileCheckbox.isVisible({ timeout: 5000 }).catch(() => false)) {
      await profileCheckbox.click();
      await page.waitForTimeout(2000);
    }

    const tiktokCb = page.locator('.el-checkbox').filter({ hasText: /tiktok|Demo_TikTok/i }).first();
    if (await tiktokCb.isVisible({ timeout: 5000 }).catch(() => false)) {
      await tiktokCb.scrollIntoViewIfNeeded();
      await tiktokCb.click();
      await page.waitForTimeout(3000);
    }
    await shot(page, '03_account_selected');
    await page.waitForTimeout(Math.max(1000, SCENE_WAIT - 7000));

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 4: Creator info — nickname, avatar (Req 1a)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 4: Creator info (Req 1a)');
    await workflowPanel(page, 4);
    await sceneTitle(page, 4, '1a', 'Creator Info — Nickname + Avatar from API');

    const creatorHeader = page.locator('.tks-creator').first();
    if (await creatorHeader.isVisible({ timeout: 8000 }).catch(() => false)) {
      await creatorHeader.scrollIntoViewIfNeeded();
      await page.waitForTimeout(500);
      await pulsingHighlight(page, '.tks-creator', 'Nickname from creator_info/query API');
      await shot(page, '04_creator_info');
    } else {
      await shot(page, '04_creator_info');
    }
    await page.waitForTimeout(SCENE_WAIT - 1000);

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 5: Post limit check (Req 1b)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 5: Post limit (Req 1b)');
    await workflowPanel(page, 5);
    await sceneTitle(page, 5, '1b', 'Post Limit — Blocks Publishing When Limit Reached');

    const postLimit = page.locator('.el-tag').filter({ hasText: /剩餘|remaining|次/i }).first();
    if (await postLimit.isVisible({ timeout: 3000 }).catch(() => false)) {
      await postLimit.scrollIntoViewIfNeeded();
      await pulsingHighlight(page, '.tks-creator', 'Post limit enforced — publish blocked when exhausted');
    }
    await shot(page, '05_post_limit');
    await page.waitForTimeout(SCENE_WAIT - 500);

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 6: Media upload + duration validation (Req 1c)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 6: Media upload + duration check');
    await workflowPanel(page, 6);
    await sceneTitle(page, 6, '1c', 'Media Upload — Duration Validated Against TikTok Max');

    const videoPath = '/home/will/social-auto-upload/videos/demo.mp4';
    if (fs.existsSync(videoPath)) {
      const uploadInput = page.locator('input[type="file"]').first();
      if (await uploadInput.count() > 0) {
        await uploadInput.setInputFiles(videoPath);
        await page.waitForTimeout(3000);
        await pulsingHighlight(page, '.el-upload-list, .media-preview, .media-item', 'Duration checked against max_video_post_duration_sec (600s)');
        await shot(page, '06_media_uploaded');
      } else {
        await shot(page, '06_media_section');
      }
    } else {
      await shot(page, '06_media_section');
    }
    await page.waitForTimeout(Math.max(1000, SCENE_WAIT - 5000));

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 7: Brief + draft generation
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 7: Brief + draft generation');
    await workflowPanel(page, 7);
    await sceneTitle(page, 7, '', 'AI Draft Generation — Per-Account Content');

    const briefInput = page.locator('.el-textarea__inner').first();
    if (await briefInput.isVisible({ timeout: 3000 }).catch(() => false)) {
      await briefInput.fill('TikTok API audit demo — showing direct post with full UX compliance.');
    }

    const generateBtn = page.locator('button').filter({ hasText: /生成|Generate/i }).first();
    if (await generateBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await generateBtn.click();
      await page.waitForTimeout(5000);
    }
    await shot(page, '07_drafts');
    await page.waitForTimeout(Math.max(1000, SCENE_WAIT - 8000));

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 8: Title field — no default (Req 2a)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 8: Title field (Req 2a)');
    await workflowPanel(page, 8);
    await sceneTitle(page, 8, '2a', 'Title Field — Required, No Default Value');

    const titleInput = page.locator('.tiktok-post-settings input[placeholder*="標題"], .tiktok-post-settings input[placeholder*="title"]').first();
    if (await titleInput.isVisible({ timeout: 3000 }).catch(() => false)) {
      await titleInput.scrollIntoViewIfNeeded();
      await pulsingHighlight(page, '.tiktok-post-settings input[placeholder*="標題"]', 'Required field — no default, user must type');
      await shot(page, '08_title_empty');
      await page.waitForTimeout(3000);
      await titleInput.fill('TikTok API Audit Demo — Direct Post');
      await shot(page, '08_title_filled');
    } else {
      await shot(page, '08_title_field');
    }
    await page.waitForTimeout(Math.max(1000, SCENE_WAIT - 5000));

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 9: Privacy dropdown — no default, from API (Req 2b)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 9: Privacy dropdown (Req 2b)');
    await workflowPanel(page, 9);
    await sceneTitle(page, 9, '2b', 'Privacy Dropdown — No Default, Options from API');

    const privacySelect = page.locator('.tiktok-post-settings .el-select').first();
    if (await privacySelect.isVisible({ timeout: 3000 }).catch(() => false)) {
      await privacySelect.scrollIntoViewIfNeeded();
      await pulsingHighlight(page, '.tiktok-post-settings .el-select', 'No default — options from privacy_level_options API');
      await shot(page, '09_privacy_closed');
      await page.waitForTimeout(3000);
      await privacySelect.click();
      await page.waitForTimeout(800);
      await shot(page, '09_privacy_open');
      const publicOpt = page.locator('.el-select-dropdown__item').filter({ hasText: /公開|Public/i }).first();
      if (await publicOpt.isVisible({ timeout: 2000 }).catch(() => false)) {
        await publicOpt.click();
        await page.waitForTimeout(500);
      }
    } else {
      await shot(page, '09_privacy');
    }
    await page.waitForTimeout(Math.max(1000, SCENE_WAIT - 8000));

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 10: Interaction settings — all unchecked (Req 2c)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 10: Interaction settings (Req 2c)');
    await workflowPanel(page, 10);
    await sceneTitle(page, 10, '2c', 'Interactions — Comment / Duet / Stitch All OFF by Default');

    const interactions = page.locator('.tks-interactions').first();
    if (await interactions.isVisible({ timeout: 3000 }).catch(() => false)) {
      await interactions.scrollIntoViewIfNeeded();
      await pulsingHighlight(page, '.tks-interactions', 'All unchecked by default — user must enable manually');
    }
    await shot(page, '10_interactions');
    await page.waitForTimeout(SCENE_WAIT - 500);

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 11: Commercial content disclosure (Req 3a)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 11: Commercial disclosure (Req 3a)');
    await workflowPanel(page, 11);
    await sceneTitle(page, 11, '3a', 'Commercial Disclosure — OFF by Default');

    const disclosureSwitch = page.locator('.tiktok-post-settings .el-switch').first();
    if (await disclosureSwitch.isVisible({ timeout: 3000 }).catch(() => false)) {
      await disclosureSwitch.scrollIntoViewIfNeeded();
      await pulsingHighlight(page, '.tiktok-post-settings .el-switch', 'Toggle OFF by default — user must enable');
      await shot(page, '11_disclosure_off');
      await page.waitForTimeout(3000);
      await disclosureSwitch.click();
      await page.waitForTimeout(800);
      await shot(page, '11_disclosure_on');

      const yourBrand = page.locator('.el-checkbox').filter({ hasText: /你的品牌|Your Brand/i }).first();
      if (await yourBrand.isVisible({ timeout: 2000 }).catch(() => false)) {
        await yourBrand.click();
        await page.waitForTimeout(500);
      }
      const branded = page.locator('.el-checkbox').filter({ hasText: /品牌合作|Branded/i }).first();
      if (await branded.isVisible({ timeout: 2000 }).catch(() => false)) {
        await branded.click();
        await page.waitForTimeout(500);
      }
      await shot(page, '11_disclosure_both');
    } else {
      await shot(page, '11_disclosure');
    }
    await page.waitForTimeout(Math.max(1000, SCENE_WAIT - 8000));

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 12: Branded content + private visibility guard (Req 3b)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 12: Branded + private guard (Req 3b)');
    await workflowPanel(page, 12);
    await sceneTitle(page, 12, '3b', 'Branded Content — Private Option Disabled');

    const privacySelect2 = page.locator('.tiktok-post-settings .el-select').first();
    if (await privacySelect2.isVisible({ timeout: 3000 }).catch(() => false)) {
      await privacySelect2.click();
      await page.waitForTimeout(800);
      await pulsingHighlight(page, '.el-select-dropdown', 'SELF_ONLY disabled when Branded Content selected');
      await shot(page, '12_private_disabled');
      await page.keyboard.press('Escape');
      await page.waitForTimeout(500);
    }
    await shot(page, '12_branded_guard');
    await page.waitForTimeout(Math.max(1000, SCENE_WAIT - 6000));

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 13: Declaration text changes (Req 4)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 13: Declaration text (Req 4)');
    await workflowPanel(page, 13);
    await sceneTitle(page, 13, '4', 'Declaration Text — Changes Based on Disclosure');

    const declaration = page.locator('.tks-declaration').first();
    if (await declaration.isVisible({ timeout: 3000 }).catch(() => false)) {
      await declaration.scrollIntoViewIfNeeded();
      await pulsingHighlight(page, '.tks-declaration', 'Text updates dynamically based on disclosure selection');
    }
    await shot(page, '13_declaration');
    await page.waitForTimeout(SCENE_WAIT - 500);

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 14: Content preview (Req 5a)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 14: Content preview (Req 5a)');
    await workflowPanel(page, 14);
    await sceneTitle(page, 14, '5a', 'Content Preview — Video Thumbnail + Filename');

    const mediaPreview = page.locator('.el-upload-list, .media-preview, .media-item').first();
    if (await mediaPreview.isVisible({ timeout: 3000 }).catch(() => false)) {
      await mediaPreview.scrollIntoViewIfNeeded();
      await pulsingHighlight(page, '.el-upload-list, .media-preview, .media-item', 'Preview shown before publishing');
    }
    await shot(page, '14_content_preview');
    await page.waitForTimeout(SCENE_WAIT - 500);

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 15: Consent before upload (Req 5c)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 15: Consent (Req 5c)');
    await workflowPanel(page, 15);
    await sceneTitle(page, 15, '5c', 'Explicit Consent — Publish Disabled Until Checked');

    const consentCb = page.locator('.tiktok-post-settings .el-checkbox').filter({ hasText: /同意|agree|By posting|確認/i }).first();
    if (await consentCb.isVisible({ timeout: 3000 }).catch(() => false)) {
      await consentCb.scrollIntoViewIfNeeded();
      await pulsingHighlight(page, '.tiktok-post-settings .el-checkbox', 'Publish button DISABLED until consent checked');
      await shot(page, '15_publish_disabled');
      await page.waitForTimeout(3000);
      await consentCb.click();
      await page.waitForTimeout(500);
      await shot(page, '15_consent_checked');
    } else {
      await shot(page, '15_consent');
    }
    await page.waitForTimeout(Math.max(1000, SCENE_WAIT - 5000));

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 16: Review modal — final confirmation
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 16: Review modal');
    await workflowPanel(page, 16);
    await sceneTitle(page, 16, '5c', 'Review Modal — All Settings for Final Confirmation');

    const publishBtn = page.locator('button').filter({ hasText: /發佈|Publish|立即/i }).first();
    if (await publishBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      const disabled = await publishBtn.isDisabled();
      if (!disabled) {
        await publishBtn.click();
        await page.waitForTimeout(2000);
        const reviewModal = page.locator('.el-dialog').filter({ hasText: /TikTok|確認|Review|發佈|Creator/i }).first();
        if (await reviewModal.isVisible({ timeout: 5000 }).catch(() => false)) {
          await pulsingHighlight(page, '.el-dialog', 'Mandatory confirmation before API call');
          await shot(page, '16_review_modal');
          await page.waitForTimeout(5000);
          const cancelBtn = reviewModal.locator('button').filter({ hasText: /取消|Cancel|關閉|Close/i }).first();
          if (await cancelBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
            await cancelBtn.click();
            await page.waitForTimeout(500);
          }
        } else {
          await shot(page, '16_review_modal');
        }
      } else {
        await shot(page, '16_review_modal');
      }
    } else {
      await shot(page, '16_review_modal');
    }
    await page.waitForTimeout(Math.max(1000, SCENE_WAIT - 9000));

    // ═══════════════════════════════════════════════════════════════════
    // SCENE 17: Processing notice (Req 5d)
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  Scene 17: Processing notice (Req 5d)');
    await workflowPanel(page, 17);
    await sceneTitle(page, 17, '5d', 'Processing Notice — TikTok Content Takes Minutes');

    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(1000);
    await shot(page, '17_processing_notice');
    await page.waitForTimeout(SCENE_WAIT - 1000);

    // ═══════════════════════════════════════════════════════════════════
    // Done
    // ═══════════════════════════════════════════════════════════════════
    console.log('\n  ✓ Recording complete — closing browser...');

  } finally {
    await context.close();
    await browser.close();
  }

  const videoFiles = fs.readdirSync(OUTPUT_DIR).filter(f => f.endsWith('.webm'));
  if (videoFiles.length > 0) {
    const latest = videoFiles[videoFiles.length - 1];
    console.log(`\n  Video saved: ${path.join(OUTPUT_DIR, latest)}`);
  }
});
