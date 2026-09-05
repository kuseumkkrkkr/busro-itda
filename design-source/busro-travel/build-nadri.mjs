import {DatabaseSync} from 'node:sqlite';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const seed=JSON.parse(await readFile(new URL('./data/nadri.seed.json',import.meta.url),'utf8'));
const additional=JSON.parse(await readFile(new URL('./data/nadri.additional.json',import.meta.url),'utf8'));
seed.places.push(...additional.places);
seed.version=2;
await mkdir(new URL('./data/',import.meta.url),{recursive:true});
const db=new DatabaseSync(new URL('./data/nadri.sqlite',import.meta.url));
db.exec(`PRAGMA foreign_keys=ON; CREATE TABLE IF NOT EXISTS places(id TEXT PRIMARY KEY, region TEXT NOT NULL, theme TEXT NOT NULL, published INTEGER NOT NULL CHECK(published IN(0,1)), sponsored INTEGER NOT NULL CHECK(sponsored=0), rank INTEGER NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload))); CREATE TABLE IF NOT EXISTS sources(place_id TEXT REFERENCES places(id) ON DELETE CASCADE,url TEXT NOT NULL,label TEXT NOT NULL,claim TEXT NOT NULL,reviewed_at TEXT NOT NULL,PRIMARY KEY(place_id,url));`);
db.exec('BEGIN IMMEDIATE');
try {
  db.exec('UPDATE places SET published=0');
  const up=db.prepare('INSERT INTO places VALUES(?,?,?,1,0,?,?) ON CONFLICT(id) DO UPDATE SET region=excluded.region,theme=excluded.theme,published=1,rank=excluded.rank,payload=excluded.payload');
  const source=db.prepare('INSERT INTO sources VALUES(?,?,?,?,?)');
  const ids=new Set();
  seed.places.forEach((p,i)=>{
    assert(!ids.has(p.id));ids.add(p.id);assert(['대전','청주'].includes(p.region));assert(['호수·물가','숲·수목원','산성·전원'].includes(p.tag));assert(p.sources.length>=2);assert(p.point.length===2&&p.point.every(Number.isFinite));assert(p.node_id&&p.city_code&&p.query);assert(p.transfers===null||(Number.isInteger(p.transfers)&&p.transfers>=0&&p.transfers<=1));assert(p.transfers===null||p.origin);
    const {sources,...place}=p;
    up.run(p.id,p.region,p.tag,i,JSON.stringify({...place,reviewed_at:seed.reviewed_at,point_kind:'bus_stop',sponsored:false}));
    db.prepare('DELETE FROM sources WHERE place_id=?').run(p.id);
    sources.forEach(s=>{assert(new URL(s.url).protocol==='https:');source.run(p.id,s.url,s.label,s.claim,seed.reviewed_at)});
    source.run(p.id,'https://busro-itda.vercel.app/api/network/stops?'+new URLSearchParams({q:p.query,city_code:p.city_code,limit:'8'}),'정류장 데이터 · 버스로 잇다',`${p.node_id}: 정류장 좌표·노선 데이터 등록 여부. 장소 좌표나 운행 보장이 아님.`,seed.reviewed_at);
  });
  db.exec('COMMIT');
}catch(e){db.exec('ROLLBACK');db.close();throw e}
assert.equal(db.prepare('PRAGMA integrity_check').get().integrity_check,'ok');
const places=db.prepare('SELECT id,payload FROM places WHERE published=1 AND sponsored=0 ORDER BY rank,id').all().map(r=>({...JSON.parse(r.payload),sources:db.prepare('SELECT url,label,claim,reviewed_at FROM sources WHERE place_id=? ORDER BY rowid').all(r.id)}));
db.close();
await writeFile(new URL('./data/nadri.generated.json',import.meta.url),JSON.stringify({version:seed.version,reviewed_at:seed.reviewed_at,policy:seed.policy,places},null,2)+'\n');
console.log(`Nadri DB: ${places.length} places; integrity ok; no sponsored records`);
