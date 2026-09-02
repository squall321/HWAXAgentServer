#!/usr/bin/env python3
"""
"Moongate" 편곡 품질 계측기.  실행: python3 quality.py

check.py 가 "규격 위반"을 잡는다면, 이건 "규격은 지켰는데 심심한 것"을 잡는다.
지금까지 놓쳐 온 축들 — 마디 자기유사도, 섹션 진입 처리, 대역 점유, 프레이즈 호흡.
"""
import collections
import importlib.util
import os
import re
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('mg', os.path.join(HERE, 'moongate_build.py'))
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)

# check.py 의 검증된 parse() 를 실행 없이 소스에서 뽑아 쓴다 (방법론 14장: import 는 부작용을 부른다)
_src = open(os.path.join(HERE, 'check.py')).read()
_m = re.search(r"^def parse\(.*?(?=\ndef |\nprint\()", _src, re.S | re.M)
_ns = {'struct': struct}
exec(compile(_m.group(0), 'check.py', 'exec'), _ns)
parse = _ns['parse']

REV = f'_rev{mg.REV:02d}'
div, tracks = parse(os.path.join(HERE, f'03_full{REV}.mid'))
GRID = div // 4                                   # 16분음표


def onsets(name):
    for n, ev, _t, _l, _i in tracks:
        if n == name:
            return sorted((t, a, b2) for t, k, ch, a, b2 in ev if k == 0x90 and b2 > 0)
    return []


def notes_with_dur(name):
    """(시작틱, 피치, 지속틱) — 대역 점유는 개수가 아니라 '울린 시간'으로 재야 한다.
    4박 지속하는 로즈 왼손 한 음과 16분 기타 커팅 한 방을 똑같이 1로 세면 지속음이
    과소평가된다(rev03 에서 이 결함을 발견해 고쳤다)."""
    for n, ev, _t, _l, _i in tracks:
        if n != name:
            continue
        pend, out = {}, []
        for t, k, ch, a, b2 in sorted(ev, key=lambda e: (e[0], 0 if e[1] == 0x80 else 1)):
            if k == 0x90 and b2 > 0:
                pend.setdefault((ch, a), []).append(t)
            elif k == 0x80 or (k == 0x90 and b2 == 0):
                q = pend.get((ch, a))
                if q:
                    st = q.pop(0)
                    out.append((st, a, t - st))
        return sorted(out)
    return []


def bar_fingerprint(evs, bar):
    """한 마디의 리듬+음high 지문 — 16분 격자에 양자화해 (격자위치, 피치) 집합으로."""
    lo, hi = (bar - 1) * 4 * div, bar * 4 * div
    return frozenset((round((t - lo) / GRID), a) for t, a, _v in evs if lo <= t < hi)


print('=' * 66)
print('[A] 마디 자기유사도 — 연속한 두 마디가 얼마나 똑같은가 (100%=복붙)')
print()
for name in ('Drums', 'Bass', 'Guitar (16th chops)', 'Rhodes'):
    evs = onsets(name)
    if not evs:
        continue
    per_sec = collections.defaultdict(list)
    for label, rng in mg.SECTIONS.items():
        bars = [b for b in rng if bar_fingerprint(evs, b)]
        for i in range(len(bars) - 1):
            a, b2 = bar_fingerprint(evs, bars[i]), bar_fingerprint(evs, bars[i + 1])
            if a or b2:
                per_sec[label].append(len(a & b2) / max(len(a | b2), 1))
    if not per_sec:
        continue
    allv = [v for vs in per_sec.values() for v in vs]
    worst = max(per_sec.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
    print(f'  {name:22s} 평균 {sum(allv)/len(allv)*100:5.1f}%   '
          f'가장 반복적인 섹션: {worst[0]} {sum(worst[1])/len(worst[1])*100:.0f}%')

print()
print('=' * 66)
print('[B] 섹션 진입 처리 — 새 섹션 직전 마디에 무슨 준비가 있는가')
print()
starts = sorted(r[0] for r in mg.SECTIONS.values())[1:]     # 인트로 제외
for st in starts:
    prev = st - 1
    label = next(l for l, r in mg.SECTIONS.items() if st in r)
    devices = []
    dr = onsets('Drums')
    lo, hi = (prev - 1) * 4 * div, prev * 4 * div
    prev_hits = [(t, a) for t, a, _v in dr if lo <= t < hi]
    last_beat = [(t - lo) / div for t, a in prev_hits if (t - lo) / div >= 3.0]
    if any(a in (45, 47, 48, 50) for t, a in prev_hits):
        devices.append('톰 필')
    if len(last_beat) >= 5:
        devices.append('마지막 박 밀집')
    cr = [t for t, a, _v in dr if a == 49 and (st - 1) * 4 * div <= t < st * 4 * div]
    if cr:
        devices.append('크래시')
    # 직전 마디에 드럼이 아예 없으면(G.P.) 그것도 장치다
    if not prev_hits:
        devices.append('드럼 정지(G.P.)')
    print(f'  {prev:>2}마디 -> {st:>2}마디 ({label:9s}) : {", ".join(devices) if devices else "★없음"}')

print()
print('=' * 66)
print('[C] 대역 점유 — 울린 시간 기준 (개수 아님)')
print()
BANDS = [('저역 <D2', 0, 38), ('저중역 D2~D3', 38, 50), ('중역 D3~D4', 50, 62),
         ('중고역 D4~D5', 62, 74), ('고역 D5~', 74, 128)]
pitched = [(t[0], notes_with_dur(t[0])) for t in tracks
           if t[0] != 'Drums' and notes_with_dur(t[0])]
for label, rng in mg.SECTIONS.items():
    lo, hi = (rng[0] - 1) * 4 * div, rng[-1] * 4 * div
    counts = []
    for bn, blo, bhi in BANDS:
        c = sum(d for _n, evs in pitched for t, a, d in evs if lo <= t < hi and blo <= a < bhi)
        counts.append(c)
    total = max(sum(counts), 1)
    bars = ' '.join(f'{bn.split()[0]}:{c*100//total:>2}%' for (bn, _l, _h), c in zip(BANDS, counts))
    empty = [bn for (bn, _l, _h), c in zip(BANDS, counts) if c == 0]
    print(f'  {label:9s} {bars}' + (f'   ★빈 대역: {", ".join(empty)}' if empty else ''))
