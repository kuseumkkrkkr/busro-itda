import React,{useState,useRef,useEffect} from 'react';
import {Bookmark,BusFront,ArrowUpRight,ChevronLeft,ChevronRight,MapPin} from 'lucide-react';

// Native scrolling keeps swipes off the JS hot path and needs no carousel dependency.
export function NadriFeed({places,select,saved=[],toggleSave,compact=false}) {
  const [region,setRegion]=useState('전체'),[index,setIndex]=useState(0);
  const track=useRef(null),current=useRef(0);
  const list=places.filter(p=>region==='전체'||p.region===region);
  const jump=(i,smooth=true)=>{
    const n=Math.max(0,Math.min(list.length-1,i)),el=track.current;
    if(!el?.children[n])return;
    const reduce=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    el.scrollTo({left:el.children[n].offsetLeft-el.children[0].offsetLeft,behavior:smooth&&!reduce?'smooth':'instant'});
    if(!smooth){current.current=n;setIndex(n)}
  };
  useEffect(()=>{jump(0,false)},[region]);
  useEffect(()=>{
    const el=track.current;
    const observer=new ResizeObserver(()=>{
      const child=el.children[current.current];
      if(child)el.scrollTo({left:child.offsetLeft-el.children[0].offsetLeft,behavior:'instant'});
    });
    observer.observe(el);return()=>observer.disconnect();
  },[]);
  function onScroll(){
    const el=track.current,step=el.children[1]?el.children[1].offsetLeft-el.children[0].offsetLeft:el.clientWidth;
    const n=Math.max(0,Math.min(list.length-1,Math.round(el.scrollLeft/step)));
    if(current.current!==n){current.current=n;setIndex(n)}
  }
  return <section className={'nadri-feed '+(compact?'compact-feed':'')} aria-label="나드리 추천" aria-roledescription="캐러셀">
    <div className="section-heading"><h2>{compact?'잠깐 내려, 나드리':'도시 밖으로, 나드리'}</h2><span className="small-caps">대전 · 청주</span></div>
    <div className="feed-toolbar"><div className="feed-regions" aria-label="추천 지역">{['전체','대전','청주'].map(r=><button key={r} aria-pressed={r===region} onClick={()=>setRegion(r)}>{r}</button>)}</div><span className="feed-counter" aria-live="polite">{index+1} / {list.length}</span></div>
    <div className="feed-track" ref={track} onScroll={onScroll} tabIndex={0} aria-label="좌우로 넘기는 나드리 장소" onKeyDown={e=>{if(e.target!==e.currentTarget)return;const keys={ArrowLeft:index-1,ArrowRight:index+1,Home:0,End:list.length-1};if(e.key in keys){e.preventDefault();jump(keys[e.key])}}}>
      {list.map((place,i)=><article key={place.id} className="feed-slide" aria-roledescription="슬라이드" aria-label={`${i+1}/${list.length} ${place.title}`} aria-hidden={i!==index} inert={i!==index?'':undefined}>
        <div className="feed-place-header"><span><MapPin size={14}/>{place.area}</span><span>{place.tag}</span></div>
        <button className="feed-story" data-theme={place.tag} onClick={()=>select(place)} aria-label={`${place.title} 상세 보기`}>
          <span className="feed-story-label">시내버스로 만나는 {place.tag==='호수·물가'?'물가':place.tag==='숲·수목원'?'숲':'전원'}</span>
          <h3>{place.title}</h3><p>{place.desc}</p><span className="feed-story-footer">어떤 곳인지 살펴보기<ArrowUpRight size={22}/></span>
        </button>
        <div className="feed-caption"><div className="feed-access"><BusFront size={18}/><p>{place.access}</p><button className={'icon-button '+(saved.includes(place.id)?'saved':'')} aria-label={`${place.title} ${saved.includes(place.id)?'저장 해제':'저장'}`} aria-pressed={saved.includes(place.id)} onClick={()=>toggleSave(place.id)}><Bookmark size={21}/></button></div><small>{place.origin?'대표 승차 지점 기준 · 귀가편은 출발 전 확인':'승차 지점·환승 수 미확인 · 상세 안내 확인'}</small></div>
      </article>)}
    </div>
    <div className="feed-navigation"><button aria-label="이전 나드리" disabled={index===0} onClick={()=>jump(index-1)}><ChevronLeft size={20}/></button><span>옆으로 넘겨 다음 풍경 보기</span><button aria-label="다음 나드리" disabled={index===list.length-1} onClick={()=>jump(index+1)}><ChevronRight size={20}/></button></div>
  </section>;
}
