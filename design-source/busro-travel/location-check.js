// Device evidence only. This cannot establish bus boarding or certified completion.
export function checkLocation(position,stop,now=Date.now()){
  const c=position?.coords;
  const valid=(v,max)=>typeof v==='number'&&Number.isFinite(v)&&Math.abs(v)<=max;
  if(!c||!valid(c.latitude,90)||!valid(c.longitude,180)||!valid(stop?.latitude,90)||!valid(stop?.longitude,180))return {ok:false,message:'정류장 또는 기기 좌표를 확인할 수 없어요.'};
  if(!Number.isFinite(position.timestamp)||now-position.timestamp>30000||position.timestamp>now+5000)return {ok:false,message:'오래된 위치예요. 다시 확인해 주세요.'};
  if(!Number.isFinite(c.accuracy)||c.accuracy<0||c.accuracy>100)return {ok:false,message:'위치 오차가 커요. 탁 트인 곳에서 다시 시도해 주세요.'};
  const rad=n=>n*Math.PI/180;
  const a=Math.sin(rad(c.latitude-stop.latitude)/2)**2+Math.cos(rad(c.latitude))*Math.cos(rad(stop.latitude))*Math.sin(rad(c.longitude-stop.longitude)/2)**2;
  const distance=6371000*2*Math.asin(Math.sqrt(Math.min(1,a)));
  if(distance+c.accuracy>200)return {ok:false,message:'출발 정류장 200m 안에서 위치를 확인해 주세요. 위치 오차도 포함합니다.'};
  return {ok:true,checkedAt:position.timestamp,scope:'device-start-only'};
}
