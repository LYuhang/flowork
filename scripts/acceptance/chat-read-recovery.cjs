// Opt-in: creates a QA project and makes two real Terra turns. Releases its
// sandbox on exit; preserves the QA conversation as review evidence.
// The browser uses the local Web build with the target application's real API.
const {chromium,request}=require('../../web/node_modules/@playwright/test');
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const root=process.env.FLOWORK_WEB_DIST || path.resolve(__dirname, '../../web/dist');
const base=process.env.FLOWORK_BASE_URL;
const auth=process.env.FLOWORK_STORAGE_STATE;
const output=process.env.FLOWORK_REVIEW_OUTPUT_DIR;
if (!base || !auth || !output) throw Error('Set FLOWORK_BASE_URL, FLOWORK_STORAGE_STATE, FLOWORK_REVIEW_OUTPUT_DIR');
fs.mkdirSync(output,{recursive:true});
const evidencePath=name=>path.join(output,name);
const evidence={startedAt:new Date().toISOString(),staticSource:'local build; target API and Terra'};
(async()=>{
 const state=JSON.parse(fs.readFileSync(auth));
 const csrf=state.cookies.find(c=>c.name.endsWith('-csrf'))?.value;
 const api=await request.newContext({baseURL:base,storageState:state,extraHTTPHeaders:{Origin:new URL(base).origin,...(csrf?{'X-CSRF-Token':csrf}:{})}});
 let browser,projectId; const chatId='chat_'+crypto.randomUUID().replaceAll('-','');
 async function json(r){if(!r.ok())throw Error('API failed '+r.status());return r.json()}
 try{
 const boot=await json(await api.get('/api/v1/chats/bootstrap?surface=chat'));
 const project=await json(await api.post('/api/v1/projects',{data:{name:'QA Architecture review 2026-10-10'}}));projectId=project.project_id;
 await json(await api.put(`/api/v1/chat-scopes/${boot.carrier_scope_id}/chats/${chatId}`,{data:{project_id:projectId}}));
 evidence.projectId=projectId;evidence.chatId=chatId;
 browser=await chromium.launch({headless:true,executablePath:process.env.FLOWORK_CHROMIUM_EXECUTABLE || undefined,args:['--no-sandbox']});
 const contexts=[]; const pages=[]; let failReads=false, failures=0, recoveryReads=0;
 for(let i=0;i<2;i++){
  const context=await browser.newContext({storageState:state,viewport:{width:1280,height:900}});contexts.push(context);
  await context.route(new URL(base).origin+'/**',async route=>{
   const req=route.request(),url=new URL(req.url());
   if(i===1 && req.method()==='GET' && (url.pathname.endsWith('/active-runs') || (url.pathname.includes(chatId)&&url.pathname.endsWith('/messages')))){
    if(failReads){failures++;return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"QA injected read outage"}'});}
    recoveryReads++;
   }
   if(url.pathname.startsWith('/assets/')){
    const file=path.join(root,url.pathname);
    if(fs.existsSync(file))return route.fulfill({path:file,contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':undefined});
   }
   if(req.resourceType()==='document' && url.pathname.startsWith('/chat'))return route.fulfill({path:path.join(root,'index.html'),contentType:'text/html'});
   return route.continue();
  });
  const page=await context.newPage();pages.push(page);
  await page.goto(new URL('/chat/open/'+chatId,base).href,{waitUntil:'domcontentloaded'});
  await page.locator('[data-role=agent-composer-input]').waitFor({timeout:60000});
 }
 const [a,b]=pages;
 await a.locator('[data-role=chat-model-select]').click();
 if(await a.locator('[data-role=chat-model-source-option]').count())await a.locator('[data-role=chat-model-source-option]').first().click();
 await a.locator('[data-role=chat-model-option]').filter({hasText:/GPT-5.6-Terra/i}).first().click();
 const transcript=p=>p.locator('[data-role=agent-message-list]');
 const waitText=async(p,text)=>{await p.waitForFunction(t=>document.querySelector('[data-role=agent-message-list]')?.innerText.includes(t),text,{timeout:180000})};
 async function send(text){await a.locator('[data-role=agent-composer-input]').fill(text);await a.locator('[data-action=agent-composer-send]').click();await a.locator('[data-action=agent-composer-stop]').waitFor({timeout:30000})}
 const one='ARCH_REVIEW_ONE_'+Date.now();
 failReads=true;
 await send('这是独立同步验收。请只回复 '+one+'，不要使用工具，不要继续其他任务。');
 await a.locator('[data-action=agent-composer-stop]').waitFor({state:'hidden',timeout:180000});
 await b.waitForTimeout(2500);
 failReads=false;
 await waitText(b,one);
 await b.locator('[data-action=agent-composer-stop]').waitFor({state:'hidden',timeout:30000});
 evidence.failedReads=failures;evidence.recoveryReads=recoveryReads;
 evidence.recoveredWithoutNewMessage=true;
 if(!failures)throw Error('fault was not exercised');
 console.log('read outage recovered',failures);
 const two='ARCH_REVIEW_TWO_'+Date.now();
 await send('这是第二轮同步验收。先回复“开始第二轮”，使用 shell 等待 8 秒，最后回复 '+two+'。不要修改文件，不要继续其他任务。');
 await b.locator('[data-action=agent-composer-stop]').waitFor({timeout:60000});
 await b.reload({waitUntil:'domcontentloaded'});
 await b.locator('[data-role=agent-composer-input]').waitFor({timeout:60000});
 await a.locator('[data-action=agent-composer-stop]').waitFor({state:'hidden',timeout:180000});
 await waitText(b,two);
 await b.locator('[data-action=agent-composer-stop]').waitFor({state:'hidden',timeout:60000});
 await b.waitForTimeout(2500);
 for(let i=0;i<2;i++){
  const text=await transcript(pages[i]).innerText();
  fs.writeFileSync(evidencePath('window-'+i+'.txt'),text);
  const userMessages=await pages[i].locator('[data-message-role=user]').allTextContents();
  const assistantText=(await pages[i].locator('[data-message-role=assistant]').allTextContents()).join('\n');
  evidence['window'+i]={userMessages:userMessages.length,hasFirst:text.includes(one),hasSecond:text.includes(two),stopped:!await pages[i].locator('[data-action=agent-composer-stop]').count()};
  if(userMessages.length!==2||!assistantText.includes(one)||!assistantText.includes(two))throw Error('history mismatch in window '+i+' count '+userMessages.length);
  await pages[i].screenshot({path:evidencePath('window-'+i+'.png'),fullPage:false});
 }
 evidence.passed=true; console.log(JSON.stringify(evidence));
 }catch(error){
  evidence.error=error.message;
  throw error;
 }finally{
  if(browser)await browser.close();
  if(projectId){const r=await api.delete(`/api/v1/projects/${projectId}/sandbox`);evidence.sandboxReleaseStatus=r.status();}
  fs.writeFileSync(evidencePath('result.json'),JSON.stringify(evidence,null,2));
  await api.dispose();
 }
})().catch(e=>{console.error(e.message);process.exitCode=1});
