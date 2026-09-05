import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const places=JSON.parse(await readFile(new URL('./data/nadri.generated.json',import.meta.url))).places;
const base=process.env.CHECK_URL||'http://127.0.0.1:8289/opendesign/mockups/busro-travel/';
const browser=await chromium.launch({executablePath:'C:/Users/user/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe',headless:true});
const page=await browser.newPage({viewport:{width:390,height:844},hasTouch:true,isMobile:true}),errors=[];
page.on('pageerror',e=>errors.push(e.message));
await page.goto(base);await page.getByRole('tab',{name:'나드리',exact:true}).click();
const access=await page.locator('.feed-access').allTextContents();assert(access.every(t=>!t.includes('833')&&!t.includes('→')&&!t.includes('환승')&&!t.includes('터미널')));assert(places.every(p=>p.route_scope==='destination_stop'&&!('origin' in p)&&!('transfers' in p)));const mid=places.find(p=>p.id==='midongsan');assert.deepEqual(mid.routes,['211']);
const track=page.locator('.feed-track'),active=page.locator('.feed-slide[aria-hidden=false]');
async function at(i){await page.waitForFunction(i=>document.querySelector('.feed-counter')?.textContent?.startsWith(i+' /'),i);await page.waitForTimeout(500);assert.equal(await active.count(),1);assert.equal(await page.locator('.feed-slide[inert]').count(),await page.locator('.feed-slide').count()-1);}
await at(1);assert(await page.getByRole('button',{name:'이전 나드리',exact:true}).isDisabled());await page.getByRole('button',{name:'다음 나드리',exact:true}).click();await at(2);
await track.focus();await page.keyboard.press('End');await at(places.length);assert(await page.getByRole('button',{name:'다음 나드리',exact:true}).isDisabled());await page.keyboard.press('Home');await at(1);await page.keyboard.press('ArrowRight');await at(2);await page.keyboard.press('ArrowLeft');await at(1);
await active.scrollIntoViewIfNeeded();const box=await track.boundingBox(),y=Math.min(box.y+150,650),cdp=await page.context().newCDPSession(page);
await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:box.x+box.width-25,y}]});
for(let i=1;i<=12;i++){await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:box.x+box.width-25-(box.width-55)*i/12,y}]});await page.waitForTimeout(18)}
await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await at(2);
const title=await active.locator('h3').innerText();for(const [width,height] of [[360,800],[390,844],[1440,900]]){await page.setViewportSize({width,height});await page.waitForTimeout(250);assert.equal(await active.locator('h3').innerText(),title);assert(!(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)));await active.scrollIntoViewIfNeeded();await page.screenshot({path:`C:/Users/user/Documents/Codex/2026-08-31/new-chat-3/outputs/nadri-feed-${width}.png`});}
await page.locator('.feed-regions').getByRole('button',{name:'청주',exact:true}).click();await at(1);assert.equal(await page.locator('.feed-slide').count(),places.filter(p=>p.region==='청주').length);const savedTitle=await active.locator('h3').innerText();await active.locator('.feed-access button').click();await page.getByRole('button',{name:'내 기록',exact:true}).click();assert((await page.locator('.record-row').innerText()).includes(savedTitle));await page.locator('.record-row>button').first().click();assert((await page.locator('.sheet-title').innerText()).includes(savedTitle));assert.deepEqual(errors,[]);
console.log(JSON.stringify({passed:true,places:places.length,checks:['CDP native touch swipe','arrows/Home/End','boundary controls','resize retains active slide','region reset','offscreen inert','save-record-detail'],errors}));await browser.close();
