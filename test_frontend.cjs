// Pure JavaScript tests with DOM/media stubs. No browser, network or camera.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'index.html'),'utf8');
const source=html.split('<script nonce="__TOKEN__">')[1].split('</script>')[0];
class Element extends EventTarget{
  constructor(){super();this.disabled=false;this.currentTime=0;this.paused=true;this.readyState=4;this.plays=0;this.dataset={}}
  setAttribute(){} removeAttribute(){} load(){} pause(){this.paused=true}
  play(){this.paused=false;this.plays++;return Promise.resolve()}
}
function fixture(){
  const elements=new Map(),videos=[new Element(),new Element()],loaders=[new Element(),new Element()];
  const arrows=['up','down','left','right'].map(dir=>{const button=new Element();button.dataset.dir=dir;return button});
  const ctx=vm.createContext({console,TextDecoder,TextEncoder,Uint8Array,Float32Array,DataView,ArrayBuffer,
    AbortController,DOMException,Event,EventTarget,URL,Promise,setTimeout,clearTimeout,setInterval,clearInterval,
    window:new EventTarget(),fetch:async()=>({ok:true}),
    document:{querySelector(selector){if(!elements.has(selector))elements.set(selector,new Element());return elements.get(selector)},
      querySelectorAll(selector){return selector==='video'?videos:selector==='.loading'?loaders:arrows}}});
  new vm.Script(source).runInContext(ctx);
  return {ctx,videos,elements,run:code=>vm.runInContext(code,ctx)};
}
function record(kind,data){const header=Buffer.alloc(5);header[0]=kind;header.writeUInt32BE(data.length,1);return Buffer.concat([header,data])}
function reader(bytes,chunkSize=1){let offset=0;return {async read(){if(offset>=bytes.length)return {done:true};const value=bytes.subarray(offset,offset+chunkSize);offset+=value.length;return {done:false,value}}}}

(async()=>{
  assert.ok(html.includes('lang="en"'));
  assert.equal((html.match(/<video /g)||[]).length,2);
  assert.equal(html.includes('name="lens"'),false);
  {
    const f=fixture(),seen=[];
    f.ctx.seen=seen;
    f.run('addVideo=async(s,n,d)=>seen.push([n,...d]);addAudio=(s,d)=>seen.push([2,...d]);metadata=(s,d)=>seen.push([3,...d])');
    const bytes=Buffer.concat([record(3,Buffer.from('{}')),record(0,Buffer.from([7,8])),record(1,Buffer.from([9])),record(2,Buffer.from([10]))]);
    f.ctx.session={reader:reader(bytes),controller:new AbortController()};
    await assert.rejects(f.run('readRecords(session)'),/The camera stream/);
    assert.deepEqual(seen.map(x=>Array.from(x)),[[3,123,125],[0,7,8],[1,9],[2,10]]);
    const oversized=Buffer.from([0,0,128,0,0]);
    f.ctx.session.reader=reader(oversized);
    await assert.rejects(f.run('readRecords(session)'),/Invalid video packet/);
    f.ctx.session.reader=reader(record(1,Buffer.from([1,2,3])).subarray(0,7));
    await assert.rejects(f.run('readRecords(session)'),/The camera stream/);
    console.log('PASS fragmented dual-video/audio envelope, length bounds and truncated payload');
  }
  {
    const f=fixture();f.ctx.session={audioQueue:[],audioOn:true};
    const pcm=Buffer.alloc(10);pcm.writeUInt32LE(1234);pcm.writeInt16LE(-32768,4);pcm.writeInt16LE(0,6);pcm.writeInt16LE(32767,8);f.ctx.data=pcm;
    f.run('addAudio(session,data)');
    assert.equal(f.ctx.session.audioQueue[0].pts,1.234);
    assert.deepEqual(Array.from(f.ctx.session.audioQueue[0].pcm),[-1,0,32767/32768]);
    for(let n=0;n<400;n++){pcm.writeUInt32LE(n*100);f.run('addAudio(session,data)')}
    assert.ok(f.ctx.session.audioQueue.length<=251);
    console.log('PASS PCM16 little-endian conversion and bounded audio history');
  }
  {
    const f=fixture(),scheduled=[];let stopped=0;
    const audio={currentTime:10,state:'running',destination:{},createBuffer(channels,length,rate){assert.equal(rate,8000);return {copyToChannel(){}}},
      createBufferSource(){return {connect(){},disconnect(){},start(when,offset){scheduled.push([when,offset])},stop(){stopped++}}}};
    f.videos[0].currentTime=4.5;f.videos[0].paused=false;
    f.ctx.session={audio,audioOn:true,audioAnchor:null,audioQueue:[{pts:4.6,pcm:new Float32Array(800),node:null}]};
    f.run('current=session;pumpAudio(session)');
    assert.ok(Math.abs(scheduled[0][0]-10.13)<1e-8);
    assert.equal(scheduled[0][1],0);
    f.run('pumpAudio(session)');assert.equal(scheduled.length,1);
    f.videos[0].paused=true;f.run('pumpAudio(session)');assert.equal(stopped,1);
    f.videos[0].paused=false;f.run('pumpAudio(session)');assert.equal(scheduled.length,2);
    f.ctx.session.audioQueue[0].node.onended();f.run('pumpAudio(session)');assert.equal(scheduled.length,2);
    console.log('PASS audio follows first video clock, schedules once, silences on pause');
  }
  {
    const f=fixture();f.ctx.session={audio:{state:'suspended',resume:async()=>{}},audioOn:true,audioQueue:[]};
    f.run('current=session;renderSound(session)');assert.equal(f.elements.get('#sound').textContent,'Enable sound');
    await f.elements.get('#sound').onclick();assert.equal(f.ctx.session.audioOn,false);
    f.ctx.data=Buffer.from('{"ptzError":true}');f.run('metadata(session,data)');
    assert.match(f.elements.get('#ptz-status').textContent,/did not confirm/);
    console.log('PASS suspended audio exposes unlock action and asynchronous PTZ errors are visible');
  }
  {
    const f=fixture();let edge=1.2,begin=0;
    class SourceBuffer extends EventTarget{
      constructor(){super();this.buffered={length:1,start:()=>begin,end:()=>edge}}
      appendBuffer(){queueMicrotask(()=>this.dispatchEvent(new Event('updateend')))}
      remove(a,b){begin=b;queueMicrotask(()=>this.dispatchEvent(new Event('updateend')))}
    }
    f.ctx.session={controller:new AbortController(),players:[{buffer:new SourceBuffer(),started:false}],audioQueue:[]};
    await f.run('addVideo(session,0,new Uint8Array([1]))');assert.equal(f.videos[0].plays,0);
    edge=2;await f.run('addVideo(session,0,new Uint8Array([1]))');assert.equal(f.videos[0].plays,1);
    f.videos[0].currentTime=3;edge=8;
    await f.run('addVideo(session,0,new Uint8Array([1]))');assert.equal(f.videos[0].currentTime,3);
    edge=40;await f.run('addVideo(session,0,new Uint8Array([1]))');assert.equal(begin,15);assert.equal(f.videos[0].currentTime,38);
    console.log('PASS initial video buffer, no repeated edge seeking, bounded long pause');
  }
  {
    const f=fixture(),sent=[];let release;
    f.ctx.fetch=async(url,options)=>{const action=JSON.parse(options.body).direction;sent.push(action);if(action==='left')await new Promise(resolve=>release=resolve);return {ok:true}};
    f.ctx.session={ptzChain:Promise.resolve(),held:'left'};f.run('current=session;queuePTZ(session,"left")');
    await new Promise(resolve=>setImmediate(resolve));
    f.run('queuePTZ(session,"left");stopPTZ(session)');release();
    await f.ctx.session.ptzChain;
    assert.deepEqual(sent,['left','stop']);assert.equal(f.ctx.session.held,null);
    await f.run('halt.onclick()');assert.deepEqual(sent,['left','stop','stop']);
    const cleared=[];
    f.ctx.setInterval=()=>37;f.ctx.clearInterval=timer=>cleared.push(timer);
    f.ctx.fetch=async(url,options)=>{sent.push(JSON.parse(options.body).direction);return {ok:true}};
    f.run('halt.disabled=false;arrows.find(button=>button.dataset.dir==="left").dispatchEvent(Object.assign(new Event("keydown"),{key:"Enter",repeat:false}))');
    await f.ctx.session.ptzChain;
    assert.equal(f.ctx.session.held,'left');assert.equal(f.ctx.session.ptzTimer,37);
    f.run('arrows.find(button=>button.dataset.dir==="left").dispatchEvent(new Event("blur"))');
    await f.ctx.session.ptzChain;
    assert.deepEqual(sent,['left','stop','stop','left','stop']);
    assert.equal(f.ctx.session.held,null);assert.ok(cleared.includes(37));
    console.log('PASS queued PTZ release, idle STOP button and keyboard focus loss stop movement');
  }
})().catch(error=>{console.error(error);process.exitCode=1});
