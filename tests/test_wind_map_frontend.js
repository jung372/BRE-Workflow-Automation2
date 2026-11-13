'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { webcrypto, createHash } = require('node:crypto');
const W = require('../assets/wind-map.js');
const N = require('../assets/navigation.js');
const bytes = obj => new TextEncoder().encode(JSON.stringify(obj));
const hash = data => createHash('sha256').update(data).digest('hex');
const regions = [{ id:'KR',name:'전국',parent_id:null },{ id:'47',name:'경북',parent_id:'KR' },{ id:'47130',name:'경주',parent_id:'47' },{ id:'47770',name:'영덕',parent_id:'47' }];
const projects = [{project_id:'siru',name:'시루',aliases:['과거명'],coordinate:null,permits:[{permit_id:'p',status:'valid',capacity_w:60000000,allocations:[{region_id:'47130',capacity_w:35000000},{region_id:'47770',capacity_w:25000000}]}]}];
function version(id, options = {}) {
  const envelope = {schema_version:1,snapshot_id:id};
  const files = {};
  files['regions.json'] = bytes({...envelope,preview:false,as_of:'2026-11-12',regions,totals:Object.fromEntries(['valid','review','approved_pending','cancelled','all'].map(s=>[s,[]]))});
  files['index.json'] = bytes({...envelope,projects});
  files['projects/siru.json'] = bytes({...envelope,project_id:'siru',sources:[],milestones:[]});
  const manifest = bytes({...envelope,preview:false,as_of:'2026-11-12',files:Object.fromEntries(Object.entries(files).map(([p,b])=>[p,hash(b)])),...options});
  const base = 'data/wind-map/versions/' + id + '/';
  const descriptor = {snapshot_id:id,manifest:base+'manifest.json',sha256:hash(manifest)};
  return {descriptor, map:{...Object.fromEntries(Object.entries(files).map(([p,b])=>[base+p,b])),[base+'manifest.json']:manifest}};
}
function fixture(current, previous) {
  const map = {...previous?.map,...current.map};
  map['data/wind-map/current.json'] = bytes({schema_version:1,current:current.descriptor,previous:previous?.descriptor||null});
  let failure = false;
  const fetcher = async path => {
    if (failure) throw new Error('offline');
    return {ok:!!map[path],arrayBuffer:async()=>map[path]};
  };
  return {map,loader:W.createLoader(fetcher,webcrypto),fail:()=>{failure=true;}};
}
test('wind-map routes and invalid project IDs remain recoverable',()=>{
  assert.equal(N.parseRoute('#/wind-map').page,'wind-map');
  assert.equal(N.parseRoute('#/wind-map/project/siru?region=47').projectId,'siru');
  assert.equal(N.parseRoute('#/wind-map/project/../../x').projectId,'invalid');
  assert.equal(N.parseRoute('#/notices').page,'notices');
  assert.equal(N.parseRoute('#/daily').page,'daily');
  assert.equal(N.parseRoute('#/solar').page,'solar');
});
test('filters and viewport round trip through detail and back',()=>{
  const f={region:'47130',scope:'review',q:'시루 & 풍력',lat:35.8,lng:129.2,level:5};
  const route=N.parseRoute(W.buildHash(f,'siru'));
  assert.deepEqual(W.readFilters(route.params),f);
  assert.deepEqual(W.readFilters(N.parseRoute(W.buildHash(f)).params),f);
});
test('invalid filter values use bounded defaults',()=>{
  const f=W.readFilters(new URLSearchParams('region=../x&scope=__proto__&lat=999&lng=0&level=-1'));
  assert.equal(f.region,'KR');assert.equal(f.scope,'valid');assert.equal(f.lat,36.1);assert.equal(f.level,12);
});
test('unknown capacity is distinct from confirmed zero',()=>{
  assert.equal(W.mw(null),'미확인');assert.equal(W.mw(0),'0 MW');assert.equal(W.mw(60000000),'60 MW');
});
test('date precision is preserved',()=>{
  assert.equal(W.dateLabel({start:'2026-05-01',end:'2026-05-31',precision:'month',estimated:false}),'2026년 5월');
  assert.equal(W.dateLabel({start:'2026-05-01',end:'2026-05-31',precision:'month',estimated:true}),'2026년 5월 추정');
  assert.equal(W.dateLabel({precision:'unknown'}),'일자 미확인');
  assert.match(W.dateLabel({start:'2026-05-13',end:'2026-05-13',precision:'before'}),/이전/);
});
test('cross-region permit appears once and missing coordinates do not remove it',()=>{
  assert.equal(W.selectProjects(projects,{region:'47',scope:'valid'},regions).length,1);
  assert.equal(W.projectCapacity(projects[0],{region:'47',scope:'valid'},regions),60000000);
  assert.equal(W.projectCapacity(projects[0],{region:'47130',scope:'valid'},regions),35000000);
  assert.equal(W.selectProjects(projects,{region:'47',scope:'review'},regions).length,0);
});
test('viewport changes cannot change regional membership or amounts',()=>{
  const a={region:'47',scope:'valid',lat:35,lng:129,level:5},b={...a,lat:38,lng:125,level:12};
  assert.deepEqual(W.selectProjects(projects,a,regions),W.selectProjects(projects,b,regions));
  assert.equal(W.projectCapacity(projects[0],a,regions),W.projectCapacity(projects[0],b,regions));
});
test('search recognizes former names without merging projects',()=>{
  assert.equal(W.searchProjects(projects,'과거').length,1);
  assert.equal(W.searchProjects(projects,'없음').length,0);
});
test('untrusted source links and path traversal are rejected',()=>{
  for(const value of ['javascript:alert(1)','data:text/html,x','http://example.com','https://x:y@example.com','https://example.com?token=x']) assert.equal(W.sourceURL(value),null);
  for(const path of ['../index.json','projects/../x.json','projects/%2e%2e.json','projects/x.json?x=1','/index.json','projects/a\\b.json']) assert.equal(W.relativeOK(path),false,path);
  assert.equal(W.relativeOK('projects/siru.json'),true);
});
test('source bytes require exact SHA-256',async()=>{
  const b=bytes({schema_version:1});await W.verifyBytes(b,hash(b),webcrypto);
  await assert.rejects(()=>W.verifyBytes(b,'0'.repeat(64),webcrypto),/해시/);
});
test('complete current version loads and detail stays within that version',async()=>{
  const v=version('1'.repeat(24)),f=fixture(v);
  const s=await f.loader.load();const d=await f.loader.detail(s,'siru');
  assert.equal(d.state.descriptor.snapshot_id,v.descriptor.snapshot_id);assert.equal(d.detail.project_id,'siru');
});
test('corrupt current index falls back to previous complete version',async()=>{
  const first=version('1'.repeat(24)),second=version('2'.repeat(24)),f=fixture(second,first);
  f.map['data/wind-map/versions/'+second.descriptor.snapshot_id+'/index.json']=bytes({schema_version:1});
  const s=await f.loader.load();assert.equal(s.descriptor.snapshot_id,first.descriptor.snapshot_id);assert.equal(s.stale,true);
});
test('detail failure switches whole version instead of mixing current totals with old details',async()=>{
  const first=version('1'.repeat(24)),second=version('2'.repeat(24)),f=fixture(second,first);
  delete f.map['data/wind-map/versions/'+second.descriptor.snapshot_id+'/projects/siru.json'];
  const s=await f.loader.load();assert.equal(s.descriptor.snapshot_id,second.descriptor.snapshot_id);
  const result=await f.loader.detail(s,'siru');assert.equal(result.state.descriptor.snapshot_id,first.descriptor.snapshot_id);assert.equal(result.state.stale,true);
});
test('temporary pointer failure preserves in-memory last good version',async()=>{
  const f=fixture(version('1'.repeat(24)));await f.loader.load();f.fail();
  assert.equal((await f.loader.load()).stale,true);
});
test('mixed-version body cannot pass a valid file hash',async()=>{
  const v=version('1'.repeat(24));
  const base='data/wind-map/versions/'+v.descriptor.snapshot_id+'/';
  const b=bytes({schema_version:1,snapshot_id:'2'.repeat(24),projects});
  v.map[base+'index.json']=b;
  const manifest=JSON.parse(new TextDecoder().decode(v.map[base+'manifest.json']));
  manifest.files['index.json']=hash(b);v.map[base+'manifest.json']=bytes(manifest);v.descriptor.sha256=hash(bytes(manifest));
  await assert.rejects(()=>fixture(v).loader.load(),/버전/);
});
test('unknown ID does not fetch arbitrary files',async()=>{
  const f=fixture(version('1'.repeat(24))),s=await f.loader.load();
  await assert.rejects(()=>f.loader.detail(s,'unknown'),/찾을 수/);
  await assert.rejects(()=>f.loader.detail(s,'../index'),/찾을 수/);
});
test('unsafe manifest path is rejected even if manifest checksum is correct',async()=>{
  const v=version('1'.repeat(24),{files:{'regions.json':'a'.repeat(64),'index.json':'b'.repeat(64),'../private.json':'c'.repeat(64)}});
  await assert.rejects(()=>fixture(v).loader.load(),/목록/);
});