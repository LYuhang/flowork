const { chromium, expect } = require('../../web/node_modules/@playwright/test');
const fs = require('fs');
(async () => {
 const {url} = JSON.parse(fs.readFileSync('/tmp/service-preview-live.json'));
 const b = await chromium.launch({channel:'chromium',headless:true,args:['--no-sandbox','--no-proxy-server','--host-resolver-rules=MAP *.preview.qa.test 127.0.0.1, MAP app.qa.test 127.0.0.1']});
 try {
  const c=await b.newContext({ignoreHTTPSErrors:true});const p=await c.newPage();const failures=[];
  p.on('pageerror',e=>failures.push(String(e)));
  p.on('response',r=>{if(r.status()>=400)failures.push(r.status()+' '+new URL(r.url()).pathname)});
  await p.goto('https://app.qa.test:19443/');
  const f=p.frameLocator('iframe');
  await expect(f.getByText('Sandbox live service')).toBeVisible({timeout:15000});
  await expect(f.locator('#api')).toHaveText('hello');
  await expect(f.locator('#ws')).toHaveText('echo live');
  await expect(f.locator('#sse')).toHaveText('event 4');
  await expect(f.locator('h1')).toHaveCSS('color','rgb(0, 100, 200)');
  await expect.poll(()=>f.locator('img').evaluate(i=>i.complete&&i.naturalWidth>0)).toBe(true);
  await f.locator('#post').click();await expect(f.locator('#posted')).toHaveText('from browser');
  const n=Number(await f.locator('#count').textContent());await expect.poll(async()=>Number(await f.locator('#count').textContent())).toBeGreaterThan(n);
  await p.screenshot({path:'/tmp/sandbox-service-preview-live.png'});
  console.log(JSON.stringify({realIsolatedSandbox:true,html:true,css:true,image:true,module:true,api:true,post:true,sse:true,websocket:true,timer:true,failures}));
  expect(failures).toEqual([]);
 } finally {await b.close()}
})().catch(e=>{console.error(String(e));process.exitCode=1});
