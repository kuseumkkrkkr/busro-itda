import React,{useState,useEffect,useRef} from 'react';
import {ArrowRight,LocateFixed,ShieldCheck,LockKeyhole} from 'lucide-react';
import {checkLocation} from './location-check.js';
import './national-trip.css';

export function NationalTicket({trip,openPlan,go}){
 const active=trip?.mode==='long'&&trip.live;
 return <><div className="section-heading"><h2>나의 국토종주</h2><span className="small-caps">BUS ONLY</span></div><article className="ticket national-ticket"><div className="ticket-top"><span>시내버스 국토종주</span><span>{active?(trip.startCheck?'여행 진행 중':'출발 대기'):'여행을 담아주세요'}</span></div><div className="ticket-route"><div><span>출발</span><strong>{active?trip.from_stop.node_name:'어디에서'}</strong></div><ArrowRight size={23}/><div><span>도착</span><strong>{active?trip.to_stop.node_name:'어디까지'}</strong></div></div><div className="ticket-perforation"/><div className="ticket-bottom"><p>{active?`${(trip.steps||[]).filter(s=>s.kind==='ride').length}개 승차 구간`:'버스를 타고, 다음 도시로.'}</p></div><div className="national-ticket-action"><button className="button primary" onClick={()=>active?go('travel'):openPlan()}>{active?(trip.startCheck?'이어서 여행하기':'출발 확인하기'):'국토종주 경로 찾기'}<ArrowRight size={18}/></button></div><div className="ticket-caption">실제 노선 연결 · 전체 구간 운행 가능 여부는 별도 확인</div></article>{active&&!trip.startCheck&&<button className="plan-link" onClick={openPlan}>다른 경로 찾기<ArrowRight size={16}/></button>}<p className="local-note">국토종주는 버스를 이어 타는 여행입니다.<br/>나드리 장소 추천은 나드리 모드에서 확인해 주세요.</p></>;
}

export function StartGate({trip,onStart}){
 const [busy,setBusy]=useState(false),[error,setError]=useState('');
 const alive=useRef(true),pending=useRef(false);
 useEffect(()=>()=>{alive.current=false},[]);
 function start(){
  if(pending.current)return;
  if(!navigator.geolocation){setError('이 브라우저에서는 위치 확인을 지원하지 않아요.');return;}
  pending.current=true;setBusy(true);setError('');
  navigator.geolocation.getCurrentPosition(p=>{
   if(!alive.current)return;
   pending.current=false;setBusy(false);
   const result=checkLocation(p,trip.from_stop);
   if(result.ok)onStart({...trip,startCheck:result});else setError(result.message);
  },e=>{if(!alive.current)return;pending.current=false;setBusy(false);setError(e.code===1?'위치 권한이 꺼져 있어요. 브라우저 설정에서 허용한 뒤 다시 눌러주세요.':e.code===3?'위치 확인 시간이 초과됐어요. 다시 시도해 주세요.':'현재 위치를 확인하지 못했어요.');},{enableHighAccuracy:true,maximumAge:0,timeout:15000});
 }
 return <div className="national-start scroll"><span className="small-caps">READY TO DEPART</span><h1>여기서, 출발합니다.</h1><h2>{trip.from_stop.node_name}</h2><p>{trip.to_stop.node_name}까지 이어갈 경로를 담았어요.</p><div className="start-rule"><LocateFixed size={24}/><div><strong>출발 정류장에서 위치 1회 확인</strong><p>정류장 200m 이내, 위치 오차 100m 이하일 때 시작할 수 있어요. 계속 추적하지 않으며 원본 좌표는 저장하지 않습니다.</p></div></div><button className="button primary" disabled={busy} onClick={start}>{busy?'현재 위치 확인 중…':'위치 확인하고 시작'}<ArrowRight size={18}/></button>{error&&<p className="api-error" role="alert">{error}</p>}<p className="local-note">기기 위치 확인은 실제 버스 탑승이나 국토종주 완주 인증이 아닙니다. 기록은 이 브라우저에만 남아요.</p></div>;
}

export function BadgeShelf({history,trip}){
 const count=history.filter(t=>t.mode==='long'&&t.startCheck?.ok).length+(trip?.mode==='long'&&trip.startCheck?.ok?1:0);
 return <section className="badge-shelf"><h2>국토종주 인증 배지</h2><div className="badge-line"><ShieldCheck size={26}/><div><strong>출발 위치 확인 {count?`· ${count}회`:''}</strong><p>{count?'기기 위치 확인 기록이 있어요':'출발 정류장에서 여행을 시작해 보세요'}</p></div></div><div className="badge-line locked"><LockKeyhole size={26}/><div><strong>시내버스 국토종주 완주</strong><p>잠김 · 탑승·전체 구간 검증 연동 전에는 발급하지 않습니다.</p></div></div><p className="local-note">기기 기록과 공식 완주 인증은 구분됩니다.</p></section>;
}
