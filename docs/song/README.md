# "Moongate" — 왕도적 작곡 순서 작업 일지 & 악보

기획서: [`../song-plan-moongate.md`](../song-plan-moongate.md)
빌더: [`moongate_build.py`](moongate_build.py) — `python3 moongate_build.py` 로 MIDI 3종 재생성
검사: [`check.py`](check.py) — 빌드 후 `python3 check.py`
사전 점검 프로토콜 · 잔여 작업: [`PREFLIGHT.md`](PREFLIGHT.md)
보컬(Synthesizer V Mai 2): [`VOCAL-MAI2.md`](VOCAL-MAI2.md)

| 파일 | 단계 | 내용 |
|---|---|---|
| `01_motif.mid` | STEP 1 | 시그니처 모티프 8마디 (휘슬 + 로즈) |
| `02_structure.mid` | STEP 2 | 76마디 전체 구조 골격 — 코드·베이스·드럼만, 멜로디 없음 |
| `03_full.mid` | STEP 3–7 | 전곡 10트랙 |
| `04_vocals.mid` | 작업용 분리본 | **보컬 3트랙만 → Synthesizer V (Mai 2)**. 전 노트에 가사 포함 |
| `05_instruments.mid` | 작업용 분리본 | **악기 7트랙만 → DAW**. 휴머나이즈·CC 포함 |

**03_full.mid 트랙 구성** — 10트랙 (괄호는 GM 프로그램)

1 Lead Vocal 가이드 (54) · 2 **Vocal Harmony 3rd below** (54) · 3 **Vocal Harmony 3rd above** (54) ·
4 Signature Whistle (73) · 5 Rhodes (4) · 6 Guitar 16th chops (27) · 7 Bass (33) ·
8 Celtic Harp (46) · 9 Strings (48) · 10 Drums (ch10)

**보컬 스펙(확정)** — 여성 메조소프라노 1인 다중녹음.
리드 D4–E5 · 아래 하모니 B3–B4 · 위 하모니 G4–E5. 세 성부를 한 사람이 부를 수 있는 음역에
묶어 두께가 아니라 **밀도**로 들리게 한다. E5는 마지막 코러스 클라이맥스 한 음뿐(믹스보이스).

> DAW에서 열고 각 트랙을 기획서 §5 음색 팔레트대로 교체하면 그대로 편곡 베드가 된다.
> 휘슬은 틴/로우 휘슬, 로즈는 MK1, 스트링스는 소편성으로.

---

## 왕도 순서대로 실제 결정된 것

### STEP 1 — 모티프 먼저 (외워지지 않으면 나머지는 무의미)

4마디, `G△7 | A7 | F#m7 | Bm7` 위. 이 곡의 전부.

```
A4(♪) B4(♪) D5(롱톤 2박) | C#5 B4 A4 F#4 | F#4(♩.) → D5(♩. 6도 도약) C#5 B4 | A4 B4(롱톤 3박)
```

장치는 셋뿐 —
① 1마디 상행 후 **D5 롱톤 착지**, ② 2마디 **하행 4음(C#–B–A–F#) 한숨**,
③ 3마디 **F#4→D5 6도 도약**(F#m7 위의 D는 ♭13 → C#로 해결하는 애포지아투라 = JRPG 특유의 애수).
음역 F#4~D5, 한 옥타브 미만 — 누구나 흥얼거릴 수 있는 상한.

### STEP 2 — 구조를 시간으로 먼저 확정 (가사·멜로디 없이)

76마디 / 112BPM / **2:42**. `02_structure.mid` 로 길이 감각을 먼저 검증했다.

| 마디 | 섹션 | 편성 처리 |
|---|---|---|
| 1–4 | Intro | 휘슬 모티프 + 하프, 드럼 없음(셰이커만) |
| 5–12 | Verse 1 | 브로큰 킥 + 림샷, 기타 16비트 커팅 |
| 13–16 | Pre 1 | 16비트 하이햇 가속 |
| 17–24 | **Chorus 1** | 4-on-the-floor 전환 + 크래시 |
| 25–28 | Post-Chorus 1 | **휘슬 + 보컬 보칼리즈 유니즌** |
| 29–36 | Verse 2 | V1 편성 + 셰이커 |
| 37–40 | Pre 2 | |
| 41–48 | **Chorus 2** | |
| 49–52 | Post-Chorus 2 | |
| 53–60 | Bridge | 53–56 로즈+보컬만(드럼 아웃) → 57–60 스트링스 합류·빌드 → 60마디 후반 **G.P. + 필** |
| 61–68 | **Final Chorus (E)** | 61–64 **낙사비**(로즈+보컬, 베이스·드럼 아웃) → 65–68 풀밴드 |
| 69–76 | Outro | 휘슬 모티프 ×2 (E), 스트링스, 73마디부터 페이드 |

### STEP 3 — 코러스부터 (팝은 훅 우선)

`G△9 | A7 | F#m9 | Bm9 | G△9 | A7 | D6/9 | A7sus4` (왕도진행 + 귀결·턴어라운드)

멜로디 설계 3원칙:
- 훅 첫 소절의 **"moon"에 최고음 D5** — 제목이 곡의 정점을 가져가야 각인된다.
- 7마디 "**mor**—ning"에 **G4→B4 도약 + 2박 롱톤**(마디 넘김) = 애니송식 클라이맥스 롱톤.
- 마지막 "day"는 **A7sus4 위의 D4** — 해결을 미룬 채 포스트코러스로 넘긴다.

### STEP 4 — 프리코러스와 벌스 (역방향)

- **프리**: `Bm9 | C#m7♭5 F#7♭9 | G△9 | A7sus4 A7`. 2마디 끝 **A#4→B4 "(ooh)"** 애드립이
  F#7♭9의 3음을 훑고 코러스로 밀어 넣는다. 4마디는 G4→A4→B4 상행으로 압력을 만든다.
- **벌스**: `Em9 | A13 | F#m9 | B7♭13` 순환. 음역 D4~B4로 좁히고 8분 싱커페이션 —
  코러스와 대비를 만드는 게 유일한 목적. B7♭13의 **D#4**가 시티팝 특유의 반음 색채.

### STEP 5 — 가사 프로소디 정렬

강세 음절을 강박에 올리는 과정에서 원안 두 곳을 고쳤다.

- 코러스 4행 `of another day` → **`of a new day`** (8마디에 음절이 넘쳐 강세가 밀렸다)
- 벌스 2절 전면 재작성 — 1절과 **마디당 음절 수를 6/6/7/7/6/7/7/7로 일치**시켰다.

### STEP 6 — 편곡·전조

마지막 코러스에서 **D → E (+2도)**. 코러스 최고음 D5는 전조 후 **E5**가 된다(가이드 MIDI 기준).
지르지 않는 톤을 유지하려면 믹스보이스 전제 — 부담되면 마지막 코러스의 "moon"만 B4(→전조 C#5)로
내리면 된다. 나머지 음은 그대로 성립한다.

---

## 최종 가사

```
[Verse 1]
Rain on the lantern glass, / the harbor turning gold,
a fiddle in the market / and a story someone told.
I've worn a hundred names, / but I keep this one for you —
the one you called me softly / when the summer was still new.

[Pre-Chorus]
Every ending is a door / I've walked before (ooh)
count the feathers on the floor —
one, two, and we begin

[Chorus]
Open the moongate, let the evening in,
I have fallen a thousand times just to land here again.
Give me one white feather and a reason to stay,
and I'll find you in the morning of a new day.

[Post-Chorus]   ← 휘슬과 완전 유니즌
Oh-oh, oh-oh-oh… moongate, take me home.

[Verse 2]
Smoke from the kettle rings, / the market closing down;
we camp beside the water / and the fire keeps the cold out.
The world can end Sunday / and open up on Monday,
and I'll still be standing here, / growing older, holding on.

[Bridge]
If this is the last life, tell me now;
I'll spend it slower, I'll spend it loud.
No more waiting for the sky to fall —
you were the reason I came back at all.

[Final Chorus — E major, 앞 4마디는 로즈+보컬만]
Open the moongate, let the evening in…
…and I'll find you in the mor—ning of a new day.
```

---

## 멜로디 (음절 = 음)

**Verse** — 음역 D4~B4

| 마디 | 코드 | 멜로디 |
|---|---|---|
| 1 | Em9 | Rain=E4 · on=G4 · the=G4 · lan=A4 · tern=G4 · glass=E4 |
| 2 | A13 | the=E4 · har=F#4 · bor=A4 · turn=G4 · ing=F#4 · gold=E4 |
| 3 | F#m9 | a=F#4 · fid=A4 · dle=A4 · in=G4 · the=A4 · mar=B4 · ket=A4 |
| 4 | B7♭13 | and=F#4 · a=F#4 · sto=G4 · ry=F#4 · some=D#4 · one=E4 · told=F#4 |
| 5 | Em9 | I've=E4 · worn=G4 · a=G4 · hun=A4 · dred=G4 · names=E4 |
| 6 | A13 | but=E4 · I=F#4 · keep=A4 · this=G4 · one=F#4 · for=E4 · you=D4 |
| 7 | F#m9 | the=F#4 · one=A4 · you=A4 · called=B4 · me=A4 · soft=F#4 · ly=E4 |
| 8 | B7♭13 | when=D#4 · the=E4 · sum=F#4 · mer=E4 · was=D#4 · still=E4 · new=F#4 |

**Pre-Chorus** — 음역 D4~B4

| 마디 | 코드 | 멜로디 |
|---|---|---|
| 1 | Bm9 | Ev=F#4 · 'ry=F#4 · end=G4 · ing=F#4 · is=E4 · a=F#4 · door=D4 |
| 2 | C#m7♭5 → F#7♭9 | I've=E4 · walked=E4 · be=G4 · fore=F#4 · (ooh=A#4 · ooh)=B4 |
| 3 | G△9 | count=B4 · the=B4 · fea=B4 · thers=A4 · on=B4 · the=A4 · floor=G4 |
| 4 | A7sus4 → A7 | one=G4 · two=A4 · and=B4 · we=B4 · be=A4 · gin=B4 |

**Chorus** — 음역 D4~D5 (전조 후 E4~E5)

| 마디 | 코드 | 멜로디 |
|---|---|---|
| 1 | G△9 | O=A4 · pen=B4 · the=B4 · **moon=D5** · gate=B4 |
| 2 | A7 | let=A4 · the=B4 · eve=A4 · ning=F#4 · in=E4 |
| 3 | F#m9 | I=F#4 · have=F#4 · fal=A4 · len=A4 · a=A4 · thou=B4 · sand=A4 · times=F#4 |
| 4 | Bm9 | just=E4 · to=F#4 · land=A4 · here=F#4 · a=E4 · gain=D4 |
| 5 | G△9 | Give=G4 · me=A4 · one=B4 · white=B4 · fea=A4 · ther=G4 |
| 6 | A7 | and=A4 · a=B4 · rea=B4 · son=A4 · to=G4 · stay=F#4 |
| 7 | D6/9 | and=F#4 · I'll=A4 · find=B4 · you=A4 · in=F#4 · the=G4 · **mor=B4** (2박 롱톤, 마디 넘김) |
| 8 | A7sus4 | ning=A4 · of=F#4 · a=E4 · new=F#4 · day=D4 |

**Bridge** — 음역 D4~B4

| 마디 | 코드 | 멜로디 |
|---|---|---|
| 1 | Bm9 | If=D4 · this=F#4 · is=F#4 · the=F#4 · last=A4 · life=F#4 |
| 2 | G△9 | tell=G4 · me=A4 · now=B4 (2박) |
| 3 | D/F# | I'll=F#4 · spend=A4 · it=A4 · slow=B4 · er=A4 |
| 4 | **Gm6** | I'll=G4 · spend=**A#4** · it=A4 · loud=G4 ← 차용화음의 ♭3, 이 곡 유일의 눈물 음 |
| 5 | Em9 | No=E4 · more=F#4 · wait=G4 · ing=F#4 · for=E4 · the=F#4 · sky=G4 |
| 6 | A7sus4 | to=A4 · fall=B4 |
| 7 | A7 | you=A4 · were=B4 · the=A4 · rea=B4 · son=A4 · I=G4 · came=F#4 · back=E4 |
| 8 | A7 → G.P. | at=F#4 · all=A4 |

### STEP 7 — 보컬 하모니 스택과 포스트코러스 분리

**하모니는 규칙으로 만들었다** (`harmonize()`). 3도를 기계적으로 붙이면 회피음에 부딪히므로:

1. D major 음계 위 **3도** 위/아래.
2. 그 음이 해당 화음의 구성음(9th·13th 색채 포함)이 아니면 **한 음 더 비켜난다** → 4도가 된다.
   예) 7마디 클라이맥스 "mor"=B4 의 3도 아래는 G4 인데, D6/9 위의 G 는 회피음이라 F#4 로 비켜난다.
3. **위 성부에 한해** 회피음이라도 다음 음이 한 음 아래로 해결되면 그대로 매단다(4-3 지연해결).
   예) 6마디 "rea"=B4 → **D5**, 다음 "son" 에서 C#5 로 해결. A7 위 11도의 지연해결.
   단 울리는 로즈 보이싱과 반음이면 금지 — 5마디 "fea" 의 C#5 는 보이싱의 D5 와 반음이라 D5 로 물린다.
4. 반음 색채음(B7♭13 의 D#4 등)에는 하모니를 붙이지 않는다. 음역을 벗어나는 음은 **쉰다**
   (6마디 "a" 처럼 한 박 비는 자리가 생기는데, 그 다음 긴 음에서 들어오는 편이 자연스럽다).

> 결과적으로 모든 하모니 음정이 단3도~완전4도(3~5반음) 안에 들어오고 성부 교차가 없다.
> 남은 반음 rub(G△9 의 F#-G, Bm9 의 D-C#, F#m9 의 G#-A)은 **리드 멜로디도 이미 만드는** 관계로,
> 9th·maj7 보이싱의 색채이지 결함이 아니다. 고치지 않았다.

**배치 — 코러스마다 한 겹씩 쌓는다**

| 구간 | 아래 3도 | 위 3도 | 벨로시티 |
|---|---|---|---|
| Chorus 1 (17–24) | 후반 4마디만 | — | 66 |
| Chorus 2 (41–48) | 전체 | 후반 4마디 | 72 / 64 |
| Final 61–64 (낙사비) | — | — | 리드 단독 |
| Final 65–68 | 후반 4마디 | 후반 4마디 | 76 / 70 |

**포스트코러스 분리** — 1~3마디는 휘슬과 완전 유니즌 보칼리즈, **4마디에서 가사 태그로 갈라진다**:
`moon(A4) gate(B4) take(A4) me(F#4) home(E4·2박)`. 휘슬이 B4 롱톤을 붙잡고 있는 동안
보컬이 그 밑으로 내려가며, 마지막 E4 는 다음 섹션(Verse 2 의 Em9 / Bridge 의 Bm9) 첫 화음으로
그대로 물린다. 2회차(49–52)에만 아래 3도를 더해 반복을 지루하지 않게 한다.

> 애초 구상은 "2회차를 3도 **위**로 갈라기"였으나, 모티프 최고음 D5 의 3도 위는 F#5 —
> 지르지 않는다는 이 곡의 전제를 깬다. 아래로 갈라 같은 넓어짐을 얻었다.

### STEP 8 — 휴머나이즈 (악기 트랙만)

보컬에는 걸지 않는다. 피치 드리프트·비브라토는 SynthV 가 생성하므로 여기서 흔들면 이중으로 흔들린다.
린터 [9]가 이 규칙을 강제한다 — 보컬 노트가 16분 격자를 벗어나면 실패한다.

| 파트 | 처리 |
|---|---|
| 기타 커팅 | 16분 뒷박 스윙 57% · 길이 ±30% · 벨로시티 ±10 |
| 드럼 | 8분 뒷박 스윙 · 벨로시티 ±8 (격자 이탈 31% — 뒷박만 밀린다) |
| 베이스 | **-0.015박(앞으로)** · 벨로시티 ±7 |
| 로즈 | +0.010박 · **코드마다 서스테인 페달(CC64)** |
| 스트링스 | **+0.020박(뒤로)** — 현은 늦게 말한다 |
| 휘슬 | +0.012박 · 숨(CC11) · **롱톤에만 늦게 얹는 비브라토(CC1)** · **켈틱 컷** |

**켈틱 컷** — 2박 이상 롱톤 앞에 0.07박짜리 윗음(+2반음)을 붙였다. 휘슬 연주의 기본 장식으로,
이게 없으면 휘슬이 신스처럼 들린다. strike·roll 은 아직 남아 있다.

시드(`SEED = 20260830`)를 고정해 빌드는 재현 가능하다. 매번 달라지면 검사도 믹스도 의미가 없다.

## 모티프의 5회 등장 (라이트모티프 운용)

| 마디 | 편성 | 처리 |
|---|---|---|
| 1–4 | 휘슬 + 하프 | 원형 제시 |
| 25–28 / 49–52 | 휘슬 + **보컬 보칼리즈 유니즌** | 훅 각인 |
| 56 | 하프 | Gm6 위 하행 한숨을 **단조로**: D5–A#4–A4–G4 |
| 60 | 휘슬 | 머리 2음(B4–C#5)만 남겨 전조된 마지막 코러스로 밀어 넣음 |
| 69–76 | 휘슬 + 스트링스 | E major 로 2회 반복 후 페이드 |
