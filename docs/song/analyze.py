"""
"Moongate" 작곡 분석기.  실행: python3 analyze.py

킬 포인트 / 당김·밀기 / 리듬 격자 / 음절 밀도 / 보컬 공백 / 베이스 충돌.
귀로 판단하기 전에 눈으로 잡을 수 있는 것만 본다.
"""
import collections, importlib.util, os, struct
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('mg', os.path.join(HERE, 'moongate_build.py'))
mg = importlib.util.module_from_spec(spec); spec.loader.exec_module(mg)
N=['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
nm=lambda p:f"{N[p%12]}{p//12-1}"

SEC = {'Verse':(mg.VERSE_MEL,5,mg.VERSE), 'Pre':(mg.PRE_MEL,13,None),
       'Chorus':(mg.CHORUS_MEL,17,None), 'Bridge':(mg.BRIDGE_MEL,53,None)}

print("="*70); print("[A] 킬 포인트 후보 — 최고음 / 최장음 / 위치")
for name,(data,sb,_) in SEC.items():
    top = max(p for *_x,p,_s in data)
    longest = max(d for _b,_bt,d,_p,_s in data)
    for bar,beat,dur,p,syl in data:
        if p==top or dur==longest:
            strong = "강박" if beat in (0.0,2.0) else ("뒷박" if beat%1==0.5 else "약박")
            tag=[]
            if p==top: tag.append("최고음")
            if dur==longest: tag.append(f"최장음 {dur}박")
            print(f"  {name:7s} {bar}마디 '{syl}' {nm(p)} {beat}박({strong}) — {'·'.join(tag)}")

print("="*70); print("[B] 프레이즈 시작 — 당김(앞마디 3.5박) / 제자리(1박) / 밀기")
for name,(data,sb,_) in SEC.items():
    evs=sorted((mg.b(bar,beat),dur,p,s) for bar,beat,dur,p,s in data)
    starts=[]; last_end=None
    for bt,d,p,s in evs:
        if last_end is None or bt-last_end>=0.49: starts.append((bt,s))
        last_end=max(last_end or 0, bt+d)
    kind=collections.Counter()
    for bt,s in starts:
        pos=round(bt%4,2)
        kind['당김' if pos==3.5 else ('제자리' if pos==0.0 else f'{pos}박')]+=1
    print(f"  {name:7s} 프레이즈 {len(starts)}개 — "+", ".join(f"{k}×{v}" for k,v in kind.items()))
print("="*70); print("[C] 리듬 격자 — 온셋이 어디에 찍히는가")
allpos=collections.Counter()
for name,(data,sb,_) in SEC.items():
    pos=collections.Counter(round(beat%1,2) for _bar,beat,_d,_p,_s in data)
    allpos.update(pos)
    tot=sum(pos.values())
    print(f"  {name:7s} "+", ".join(f"{k}={v*100//tot}%" for k,v in sorted(pos.items())))
tot=sum(allpos.values())
print(f"  전체    "+", ".join(f"{k}={v*100//tot}%" for k,v in sorted(allpos.items())))
print(f"  → 16분 위치(0.25/0.75) 사용률: {(allpos[0.25]+allpos[0.75])*100//tot}%")

print("="*70); print("[D] 섹션별 음절 밀도 (마디당) 와 노트 길이")
for name,(data,sb,_) in SEC.items():
    per=collections.Counter(bar for bar,*_ in data)
    durs=[d for _b,_bt,d,_p,_s in data]
    print(f"  {name:7s} 마디당 {min(per.values())}~{max(per.values())}음절 "
          f"(평균 {sum(per.values())/len(per):.1f})  평균길이 {sum(durs)/len(durs):.2f}박  "
          f"1박이상 비율 {sum(1 for d in durs if d>=1)*100//len(durs)}%")

print("="*70); print("[E] 보컬이 쉬는 자리와, 그때 누가 소리내는가")
def parse(path):
    d=open(path,'rb').read(); _,_,nt,div=struct.unpack('>IHHH',d[4:14]); i=14; out={}
    def vlq(j):
        n=0
        while True:
            b=d[j]; j+=1; n=(n<<7)|(b&0x7F)
            if not b&0x80: return n,j
    for _ in range(nt):
        if d[i:i+4]!=b'MTrk': break
        ln=struct.unpack('>I',d[i+4:i+8])[0]; i+=8; end=i+ln; t=0; name=''; run=None; ons=[]
        while i<end:
            dt,i=vlq(i); t+=dt; s=d[i]
            if s==0xFF:
                mt=d[i+1]; l,j=vlq(i+2)
                if mt==0x03: name=d[j:j+l].decode('utf8','replace')
                i=j+l; continue
            if s<0x80: s=run
            else: i+=1; run=s
            k=s&0xF0
            if k in (0x80,0x90,0xA0,0xB0,0xE0):
                if k==0x90 and d[i+1]>0: ons.append(t/div)
                i+=2
            elif k in (0xC0,0xD0): i+=1
        i=end; out[name]=sorted(ons)
    return out
ins=parse(os.path.join(HERE,'05_instruments.mid'))
for label,sb,data in (('Verse 1',5,SEC['Verse'][0]),('Verse 2',29,SEC['Verse'][0]),('Chorus',17,SEC['Chorus'][0])):
    evs=sorted((mg.b(sb+bar-1,beat),dur) for bar,beat,dur,_p,_s in data)
    gaps=[]; last=evs[0][0]
    for bt,d in evs:
        if bt-last>=0.49: gaps.append((last,bt-last))
        last=max(last,bt+d)
    print(f"\n  ── {label} (마디 {sb}~) 보컬 공백 {len(gaps)}곳")
    for st,ln in gaps:
        who=[k.split(' (')[0] for k,v in ins.items()
             if any(st-0.02<=o<st+ln for o in v) and 'Drum' not in k]
        print(f"     {mg.bar_of(st)}마디 {st%4:.2f}박부터 {ln:.2f}박 — 채우는 파트: "
              f"{', '.join(who) if who else '★없음'}")

print("="*70); print("[F] 베이스 — 보컬과 같은 순간을 치는가, 피하는가")
bass=[round(o,2) for o in ins['Bass']]
for label,(data,sb,_) in (('Verse',SEC['Verse']),('Chorus',SEC['Chorus'])):
    voc={round(mg.b(sb+bar-1,beat),2) for bar,beat,_d,_p,_s in data}
    rng=[o for o in bass if sb-1<=o/4<sb-1+8]
    hit=sum(1 for o in rng if any(abs(o-v)<0.06 for v in voc))
    print(f"  {label}: 베이스 온셋 {len(rng)}개 중 보컬과 같은 순간 {hit}개 "
          f"({hit*100//max(1,len(rng))}%) — 나머지는 보컬 사이를 메운다")
