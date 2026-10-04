import { chromium } from 'playwright';
import AxeBuilder from '@axe-core/playwright';
import fs from 'node:fs';

const url = process.env.VIDIGEN_URL || 'https://www.vidigen.online/';
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
const page = await context.newPage();

const consoleErrors = [];
page.on('console', msg => {
  if (msg.type() === 'error') consoleErrors.push(msg.text());
});
page.on('pageerror', err => consoleErrors.push(String(err)));

const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForLoadState('load', { timeout: 30000 }).catch(() => {});
await page.waitForTimeout(5000);
if (!response || !response.ok()) throw new Error(`Frontend returned HTTP ${response?.status()}`);

await page.screenshot({ path: 'vidigen-production.png', fullPage: true });
const bodyText = (await page.locator('body').innerText()).trim();
if (bodyText.length < 100) throw new Error('Production page rendered as effectively blank.');
if (/Something went wrong|Internal Server Error/i.test(bodyText)) throw new Error('Production page contains an application error state.');

const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
fs.writeFileSync('axe-results.json', JSON.stringify(results, null, 2));
if (results.violations.length) {
  console.error(JSON.stringify(results.violations, null, 2));
  throw new Error(`WCAG AA audit found ${results.violations.length} violation group(s).`);
}
if (consoleErrors.length) {
  console.error(JSON.stringify(consoleErrors, null, 2));
  throw new Error(`Browser reported ${consoleErrors.length} console/page error(s).`);
}

console.log(JSON.stringify({
  url,
  status: response.status(),
  title: await page.title(),
  bodyCharacters: bodyText.length,
  wcagViolations: 0,
  consoleErrors: 0
}, null, 2));

await context.close();
await browser.close();
