#!/usr/bin/env python3
"""
"Moongate" 보컬-반주 상호작용 계측기.  실행: python3 interplay.py

quality.py 가 편곡 '자체'의 성질을 본다면, 이건 편곡이 보컬과 어떻게 '주고받는가'를 본다.
- 보컬이 쉴 때 반주가 대답하는가 (콜앤리스폰스)
- 보컬이 바쁠 때 반주가 물러나는가 (밀도 역상관)
- 필이 보컬 위에 떨어지는가, 빈자리에 떨어지는가
- 반주 온셋이 보컬 음절과 같은 순간을 치는가(마스킹), 사이를 메우는가
"""
import importlib.util, os, re, struct

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('mg', os.path.join(HERE, 'moongate_build.py'))
mg = importlib.util.module_from_spec(spec); spec.loader.exec_module(mg)

_src = open(os.path.join(HERE, 'check.py')).read()
_m = re.search(r"^def parse\(.*?(?=\ndef |\nprint\()", _src, re.S | re.M)
_ns = {'struct': struct}; exec(compile(_m.group(0), 'check.py', 'exec'), _ns)
parse = _ns['parse']

REV = f'_rev{mg.REV:02d}'
div, tracks = parse(os.path.join(HERE, f'03_full{REV}.mid'))
G = div // 4                                  # 16분 격자
NBARS = 76
NG = NBARS * 16                               # 전체 16분 칸 수


def notes(name):
    for n, ev, _t, _l, _i in tracks:
        if n != name:
            continue
        pend, out = {}, []
        for t, k, ch, a, b2 in sorted(ev, key=lambda e: (e[0], 0 if e[1] == 0x80 else 1)):
            if k == 0x90 and b2 > 0:
                pend.setdefault((ch, a), []).append((t, b2))
            elif k == 0x80 or (k == 0x90 and b2 == 0):
                q = pend.get((ch, a))
                if q:
                    st, v = q.pop(0)
                    out.append((st, t, a, v))
        return sorted(out)
    return []


VOC = notes('Lead Vocal (guide)')
ACC = {n: notes(n) for n in ('Drums', 'Bass', 'Rhodes', 'Guitar (16th chops)',
                              'Signature Whistle', 'Celtic Harp', 'Strings')}

# 격자별 보컬 '울림'(지속 포함)과 '온셋'
voc_sound = [0] * NG
voc_onset = [0] * NG
for st, en, a, v in VOC:
    for g in range(st // G, min(en // G + 1, NG)):
        voc_sound[g] = 1
    if st // G < NG:
        voc_onset[st // G] = 1

print('=' * 68)
print('[가] 보컬이 쉬는 자리를 누가 채우는가 — 1박 이상 공백만')
print()
gaps = []
g = 0
while g < NG:
    if not voc_sound[g]:
        s0 = g
        while g < NG and not voc_sound[g]:
            g += 1
        if (g - s0) >= 4 and s0 > 16:          # 1박(=16분 4칸) 이상, 인트로 제외
            gaps.append((s0, g))
    else:
        g += 1
sung_end = max(en for _s, en, _a, _v in VOC) // G
gaps = [(a, b) for a, b in gaps if a < sung_end]     # 노래가 끝난 뒤는 제외
answered = 0
for s0, e0 in gaps:
    lo, hi = s0 * G, e0 * G
    who = []
    for n, evs in ACC.items():
        hits = sum(1 for st, en, a, v in evs if lo <= st < hi)
        if n == 'Drums':
            continue                            # 드럼은 항상 치므로 '대답'으로 안 센다
        if hits >= 2:
            who.append(f'{n.split()[0]}×{hits}')
    bar = s0 // 16 + 1
    ok = bool(who)
    answered += ok
    print(f"  {bar:>2}마디 {(s0%16)/4:.2f}박부터 {(e0-s0)/4:.2f}박 : "
          f"{', '.join(who) if who else '★리듬 반주만 (선율 대답 없음)'}")
print(f"\n  1박 이상 공백 {len(gaps)}곳 중 선율이 대답하는 곳 {answered}곳 "
      f"({answered*100//max(len(gaps),1)}%)")

print()
print('=' * 68)
print('[나] 밀도 역상관 — 보컬이 바쁠 때 반주가 물러나는가')
print()
# 두 가지를 갈라 본다.
#  · 리듬 베드(드럼·기타 커팅·베이스)는 그루브라 일정한 게 맞다. 코러스에서 평평한 건 정상.
#  · 실제로 보컬과 자리를 다투는 건 선율 악기(휘슬·하프·스트링스·로즈)다.
# 그리고 드럼 스타일이 바뀌는 경계를 넘어 평균 내면 안 된다 — 브릿지 53~56(셰이커만)과
# 57~60(빌드)을 한 덩어리로 묶으면 '보컬 바쁠 때 더 두꺼움'이라는 가짜 신호가 나온다.
# 그건 보컬에 대한 반응이 아니라 섹션이 고조된 것이다. (rev03 계측의 오류였다.)
BED = ('Drums', 'Guitar (16th chops)', 'Bass')
LEAD = ('Signature Whistle', 'Celtic Harp', 'Strings', 'Rhodes')
STYLE_SPLIT = {'bridge': [range(53, 57), range(57, 60)]}   # 드럼 스타일 경계
# 60마디는 브릿지 끝 G.P.(총休) — 반주를 의도적으로 지운 마디다. 밀도 평균에 넣으면
# '한산한 마디의 반주가 얇다'가 되어 역상관이 뒤집힌 것처럼 보인다. 구조적 침묵은 제외한다.
GP_BARS = {60}

def _dens(names, b0, b1):
    return sum(1 for n in names for st, en, a, v in ACC[n] if b0 * G <= st < b1 * G)

for label, rng in mg.SECTIONS.items():
    lo, hi = (rng[0] - 1) * 16, rng[-1] * 16
    if not any(voc_onset[lo:hi]):
        continue
    for part in STYLE_SPLIT.get(label, [rng]):
        rows = []
        for bar in part:
            if bar in GP_BARS:
                continue
            b0, b1 = (bar - 1) * 16, bar * 16
            rows.append((sum(voc_onset[b0:b1]), _dens(BED, b0, b1), _dens(LEAD, b0, b1)))
        if len(rows) < 2:
            continue
        med = sorted(r[0] for r in rows)[len(rows) // 2]
        busy = [r for r in rows if r[0] > med]
        calm = [r for r in rows if r[0] <= med]
        if not busy or not calm:
            continue
        name = label if part is rng else f'{label} {part[0]}-{part[-1]}'
        out = []
        for i, tag in ((1, '베드'), (2, '선율')):
            bm = sum(r[i] for r in busy) / len(busy)
            cm = sum(r[i] for r in calm) / len(calm)
            # 절대 개수가 아니라 그 구간 밀도에 대한 비율로 본다 — 45음 짜리 반주에서
            # 2음 차이는 들리지 않는다. 10% 안쪽은 '평평'으로 센다.
            rel = abs(cm - bm) / max((cm + bm) / 2, 1)
            v = '—평평' if rel < 0.10 else ('✔물러남' if cm > bm else '★역방향')
            out.append(f'{tag} {bm:5.1f}/{cm:<5.1f} {v}')
        print(f'  {name:14s} ' + '   '.join(out))

print()
print('=' * 68)
print('[다] 필이 보컬 위에 떨어지는가')
print()
# 지속모음 위에 필이 얹히는 것은 정상적인 드러밍이다(프레이즈 끝 롱톤 밑에서 굴리는 자리).
# 실제로 말을 덮는 것은 '음절이 시작하는 순간'을 때릴 때뿐이다 — 둘을 갈라 센다.
# (rev03 까지 이 둘을 뭉뚱그려 세는 바람에 68마디처럼 뒤에 음절이 아예 없는 롱톤 필까지
#  '보컬과 겹침'으로 잡혔다. 분모를 잘못 잡은 계측이었다.)
for bar, fill in sorted(mg.FILLS.items()):
    if not fill:
        continue
    onset = sust = 0
    for o, d, note_, v in fill:
        gi = int((bar - 1) * 16 + o * 4)
        if not (0 <= gi < NG):
            continue
        if voc_onset[gi]:
            onset += 1
        elif voc_sound[gi]:
            sust += 1
    mark = ('★음절을 덮음' if onset else
            ('✔ 지속음 위(정상)' if sust else '✔ 빈자리'))
    print(f'  {bar:>2}마디 필 {len(fill)}타 중 음절 시작 위 {onset}타 · 지속음 위 {sust}타  {mark}')

print()
print('=' * 68)
print('[라] 반주 온셋이 보컬 음절과 같은 순간을 치는가 (마스킹)')
print()
for n in ('Bass', 'Guitar (16th chops)', 'Rhodes'):
    hit = tot = 0
    for st, en, a, v in ACC[n]:
        gi = st // G
        if gi < NG and voc_sound[gi]:
            tot += 1
            if voc_onset[gi]:
                hit += 1
    if tot:
        print(f'  {n:22s} 보컬이 울리는 동안 {tot:4d}타 중 '
              f'음절 시작과 동시 {hit:4d}타 ({hit*100//tot}%)')
