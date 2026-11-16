"use strict";
const fs=require("fs"),path=require("path"),os=require("os"),cp=require("child_process"),net=require("net");
const ROOT=path.resolve(__dirname,".."),CLI=path.join(ROOT,"bin/cli.js"),PY=process.env.CLAUDELINK_PYTHON||"python";
const delay=n=>new Promise(r=>setTimeout(r,n));
const port=()=>new Promise(r=>{const s=net.createServer();s.listen(0,"127.0.0.1",()=>{const p=s.address().port;s.close(()=>r(p));});});
async function command(args,env){return new Promise((resolve,reject)=>{const proc=cp.spawn(process.execPath,[CLI,...args],{cwd:ROOT,env,windowsHide:true});let out="";proc.stdout.on('data',b=>out+=b);proc.stderr.on('data',b=>out+=b);proc.on('error',reject);proc.on('exit',code=>resolve({code,out}));});}
async function scenario(version,busy=false,revision){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),"clh-lifecycle-")),p=await port(),url='http://127.0.0.1:'+p;
 const cfg={server:{host:'127.0.0.1',port:p,auth:true,allow_shutdown:true},workspace:{cwd:dir},agents:{},permissions:{mode:'read-only'},claudelink:{mirror:false},relay:{enabled:false}};
 fs.writeFileSync(path.join(dir,'config.json'),JSON.stringify(cfg));
 const env={...process.env,CLAUDELINK_HOME:dir,CLAUDELINK_CONFIG:path.join(dir,'config.json'),CLAUDELINK_DB:path.join(dir,'hub.db'),CLAUDELINK_RUNTIME_DIR:path.join(dir,'instances'),CLAUDELINK_AGENTS:path.join(dir,'detected.json'),CLAUDELINK_PYTHON:PY,PYTHONIOENCODING:'utf-8'};
 const log=fs.openSync(path.join(dir,'server.log'),'a');
 const processEnv={...env,CLAUDELINK_FIXTURE_VERSION:version,...(busy?{CLAUDELINK_FIXTURE_BUSY:'1'}:{}),...(revision?{CLAUDELINK_FIXTURE_REVISION:revision}:{})};
 const server=cp.spawn(PY,[path.join(__dirname,'ui_fixture.py')],{cwd:ROOT,env:processEnv,windowsHide:true,stdio:['ignore',log,log]});
 for(let i=0;i<80;i++){try{if((await fetch(url+'/api/health')).ok)break;}catch{}await delay(100);}
 const token=fs.readFileSync(path.join(dir,'hub.token'),'utf8').trim();
 async function api(route,body){return(await fetch(url+route,{method:body?'POST':'GET',headers:{'X-ClaudeLink-Token':token,'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})})).json();}
 return{dir,env,url,server,log,api};
}
let count=0;
function check(label,value,detail){if(!value)throw new Error(label+': '+(detail||''));count++;console.log('PASS '+label);}
(async()=>{
 let s;
 try{
  s=await scenario('0.3.1');await s.api('/api/memory',{kind:'long',text:'upgrade-survives',source:'test'});
  let r=await command(['start','--no-open','--no-setup','--detach'],s.env);check('idle old service upgraded',r.code===0,r.out);
  let health=await(await fetch(s.url+'/api/health')).json();check('new backend version visible',health.version==='0.4.0');
  check('existing memory survives upgrade',(await s.api('/api/memory')).memories.some(m=>m.text==='upgrade-survives'));
  r=await command(['tui','--render-once','--no-start','--width','120','--height','30'],s.env);check('CLI loads the visible new terminal workspace',r.code===0&&r.out.includes('协作工作台')&&r.out.includes('0.4.0'),r.out.slice(-400));
  r=await command(['ui','--no-open'],s.env);check('web command resolves same instance',r.code===0,r.out);
  r=await command(['stop'],s.env);check('stop uses authenticated resolved instance',r.code===0,r.out);
 }finally{if(s){s.server.kill();fs.closeSync(s.log);}}
 try{
  s=await scenario('0.3.1',true);
  const r=await command(['start','--no-open','--no-setup','--detach'],s.env);check('busy old service is not killed or duplicated',r.code!==0&&r.out.includes('有任务'),r.out);
  check('busy original process still serves requests',(await(await fetch(s.url+'/api/health')).json()).version==='0.3.1');
  const stop=await command(['stop'],s.env);check('explicit stop can stop busy instance',stop.code===0,stop.out);
 }finally{if(s){s.server.kill();fs.closeSync(s.log);}}
 try{
  s=await scenario('0.4.0',false,'stale-source');
  const r=await command(['start','--no-open','--no-setup','--detach'],s.env);check('same version with stale loaded code is refreshed',r.code===0,r.out);
  check('stale code revision replaced',(await(await fetch(s.url+'/api/health')).json()).code_revision!=='stale-source');
  await command(['stop'],s.env);
 }finally{if(s){s.server.kill();fs.closeSync(s.log);}}
 console.log('Lifecycle checks: '+count);
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
