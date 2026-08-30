#!/usr/bin/env python3
"""
"Moongate" 사전 점검 린터.  실행: python3 check.py   (빌드 후)

빌드가 "돌아간다"와 "곡으로 성립한다"는 다르다. 이번 작업에서 뒤늦게 발견해
되돌아갔던 항목들을 전부 자동 검사로 고정해 둔다 — 다음 곡에서 다시 발견하지 않기 위해.
"""
import collections
import importlib.util
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('mg', os.path.join(HERE, 'moongate_build.py'))
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)

N = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
nm = lambda p: f'{N[p % 12]}{p // 12 - 1}'
FAIL, WARN = [], []


def check(ok, msg, hard=True):
    print(('  ✔ ' if ok else ('  ✘ ' if hard else '  ▲ ')) + msg)
    if not ok:
        (FAIL if hard else WARN).append(msg)


# ── 1. 편성 음역표 ─────────────────────────────────────────────
# (트랙, 이 곡에서 써도 되는 음역, 악기의 물리적 음역, 근거)
RANGES = {
    'Lead Vocal (guide)':        ((62, 76), (55, 79), '여성 메조: D4–E5. E5 는 전조 후 클라이맥스 한 음'),
    'Vocal Harmony (3rd below)': ((55, 71), (55, 79), '같은 가수가 겹녹음 — 저역은 흉성 한계 G3'),
    'Vocal Harmony (3rd above)': ((62, 76), (55, 79), '리드 상한을 넘지 않는다'),
    'Signature Whistle':         ((62, 86), (62, 86), '★로우 D 휘슬 D4–D6. 일반 틴휘슬은 D5 가 최저음'),
    'Rhodes':                    ((40, 88), (28, 100), 'MK1'),
    'Guitar (16th chops)':       ((52, 84), (40, 88), '6현 기타'),
    'Bass':                      ((33, 60), (28, 67), '★40Hz 하이패스 전제 — E1(28) 이하로 내려가면 매장에서 사라진다'),
    'Celtic Harp':               ((48, 96), (24, 103), ''),
    'Strings':                   ((55, 96), (55, 103), '소편성'),
    'Drums':                     ((35, 81), (35, 81), 'GM 드럼맵'),
}


def parse(path):
    d = open(path, 'rb').read()
    assert d[:4] == b'MThd', 'MThd 없음'
    _, _, ntrk, div = struct.unpack('>IHHH', d[4:14])
    i, out = 14, []

    def vlq(j):
        n = 0
        while True:
            byte = d[j]; j += 1; n = (n << 7) | (byte & 0x7F)
            if not byte & 0x80:
                return n, j

    for _ in range(ntrk):
        assert d[i:i + 4] == b'MTrk', 'MTrk 없음'
        ln = struct.unpack('>I', d[i + 4:i + 8])[0]; i += 8; end = i + ln
        t, name, run, ev, lyr = 0, '', None, [], []
        while i < end:
            dt, i = vlq(i); t += dt; s = d[i]
            if s == 0xFF:
                mt = d[i + 1]; l, j = vlq(i + 2)
                if mt == 0x03:
                    name = d[j:j + l].decode('utf8', 'replace')
                if mt == 0x05:
                    lyr.append(d[j:j + l].decode('utf8', 'replace'))
                i = j + l; continue
            if s < 0x80:
                s = run
            else:
                i += 1; run = s
            k = s & 0xF0
            if k in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                ev.append((t, k, s & 0xF, d[i], d[i + 1])); i += 2
            elif k in (0xC0, 0xD0):
                i += 1
        assert i == end, '트랙 길이 불일치'
        out.append((name, ev, t, lyr))
    return div, out


def lead_pitches():
    ps = [p for data in (mg.VERSE_MEL, mg.PRE_MEL, mg.CHORUS_MEL, mg.BRIDGE_MEL)
          for *_x, p, _s in [(d[0], d[1], d[2], d[3], d[4]) for d in data]]
    ps += [p + 2 for *_x, p, _s in [(d[0], d[1], d[2], d[3], d[4]) for d in mg.CHORUS_MEL]]
    ps += [p for _bt, _d, p in mg.MOTIF if _bt < 12.0] + [p for _bt, _d, p, _s in mg.POST_TAG]
    return ps


def fit_report(lo_in, hi_in):
    """가수 음역을 주면, 어느 조옮김이 그 안에 들어오는지 표로 보여준다."""
    def pitch(x):
        if x.lstrip('-').isdigit():
            return int(x)
        acc = 1 if '#' in x else (-1 if 'b' in x else 0)
        letter = x[0].upper()
        octv = int(x[-1] if x[-1] != '-' else x[-2:])
        return 12 * (octv + 1) + N.index(letter) + acc

    lo_s, hi_s = pitch(lo_in), pitch(hi_in)
    lead = lead_pitches()
    ch = mg.mel_events(mg.CHORUS_MEL, 17)
    harm = [p for _b, _d, p in mg.harmonize(ch, below=True)] + \
           [p + 2 for _b, _d, p in mg.harmonize(ch, below=True)] + \
           [p for _b, _d, p in mg.harmonize(ch, below=False)] + \
           [p + 2 for _b, _d, p in mg.harmonize(ch, below=False)]
    KEYS = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
    WHISTLE = (62, 86)                      # 로우 D 휘슬
    print(f'\n가수 음역 {nm(lo_s)}–{nm(hi_s)} 기준 조옮김 적합도\n')
    print('| TRANSPOSE | 조 | 리드 | 하모니 전체 | 가수 음역 | 로우휘슬 |')
    print('|---|---|---|---|---|---|')
    for t in range(-6, 7):
        l1, l2 = min(lead) + t, max(lead) + t
        h1, h2 = min(harm) + t, max(harm) + t
        v_ok = lo_s <= min(l1, h1) and max(l2, h2) <= hi_s
        w1, w2 = min(p for _b, _d, p in mg.MOTIF) + t, max(p for _b, _d, p in mg.MOTIF) + t + 2
        w_ok = WHISTLE[0] <= w1 and w2 <= WHISTLE[1]
        mark = '★' if t == mg.TRANSPOSE else ' '
        print(f'| {mark}{t:+d} | {KEYS[(2 + t) % 12]} | {nm(l1)}–{nm(l2)} | {nm(h1)}–{nm(h2)} | '
              f'{"✔" if v_ok else "✘"} | {"✔" if w_ok else "✘ 옥타브 위로 더블링 필요"} |')
    print('\n★ = 현재 설정. 두 열이 모두 ✔ 인 줄이 후보다.')


if len(sys.argv) > 1 and sys.argv[1] == '--fit':
    fit_report(sys.argv[2], sys.argv[3])
    sys.exit(0)

print('\n[1] MIDI 구조 · 미결 노트')
div, tracks = parse(os.path.join(HERE, '03_full.mid'))
played = {}
for name, ev, _, _l in tracks:
    hung = collections.Counter(); orphan = 0; ps = []
    for t, k, ch, a, b2 in ev:
        if k == 0x90 and b2 > 0:
            hung[(ch, a)] += 1; ps.append(a)
        elif k == 0x80 or (k == 0x90 and b2 == 0):
            if hung[(ch, a)] == 0:
                orphan += 1
            else:
                hung[(ch, a)] -= 1
    if ps:
        played[name] = ps
        check(sum(hung.values()) == 0 and orphan == 0, f'{name}: 노트 온/오프 정합')

print('\n[8] 가사 이벤트 커버리지 — Synthesizer V 임포트용')
for name, ev, _t, lyr in tracks:
    if 'Vocal' not in name:
        continue
    notes = sum(1 for t, k, ch, a, b2 in ev if k == 0x90 and b2 > 0)
    check(notes == len(lyr) and notes > 0,
          f'{name}: 노트 {notes}개 / 가사 {len(lyr)}개 — 전 노트에 음절이 붙어 있음')

print('\n[2] 음역 — 편곡 스펙 및 실제 악기 물리 음역')
for name, ps in played.items():
    lo, hi = min(ps), max(ps)
    (slo, shi), (ilo, ihi), why = RANGES[name]
    slo, shi = slo + mg.TRANSPOSE, shi + mg.TRANSPOSE   # 스펙은 조옮김을 따라간다
                                                        # (물리 음역은 따라가지 않는다 — 그게 요점)
    check(slo <= lo and hi <= shi,
          f'{name}: {nm(lo)}–{nm(hi)} (스펙 {nm(slo)}–{nm(shi)})' + (f' — {why}' if why else ''))
    check(ilo <= lo and hi <= ihi, f'{name}: 악기 물리 음역 안', hard=True)

print('\n[3] 섹션 타임코드 · 전체 길이')
sec_per_bar = 4 * 60 / mg.BPM
for label, rng in mg.SECTIONS.items():
    st = (rng[0] - 1) * sec_per_bar
    print(f'  · {label:9s} {rng[0]:>2}–{rng[-1]:<2}마디  {int(st // 60)}:{int(st % 60):02d}')
total = 76 * sec_per_bar
check(150 <= total <= 210, f'전체 {int(total // 60)}:{int(total % 60):02d} — 현대 팝 길이(2:30~3:30) 안')

print('\n[4] 보컬 하모니 — 음정 · 성부 교차')
ch = mg.mel_events(mg.CHORUS_MEL, 17)
lo_h = mg.harmonize(ch, below=True)
hi_h = mg.harmonize(ch, below=False)
lead = {round(b, 3): p for b, d, p in ch}
bad = []
for evs, below in ((lo_h, True), (hi_h, False)):
    for bt, d, p in evs:
        iv = (lead[round(bt, 3)] - p) if below else (p - lead[round(bt, 3)])
        if not 3 <= iv <= 5:
            bad.append((nm(p), iv, below))
check(not bad, f'모든 하모니가 단3도~완전4도(3~5반음) 안: {bad or "예외 없음"}')
pair = collections.defaultdict(dict)
for bt, d, p in lo_h:
    pair[round(bt, 3)]['lo'] = p
for bt, d, p in hi_h:
    pair[round(bt, 3)]['hi'] = p
cross = [k for k, v in pair.items() if 'lo' in v and 'hi' in v and v['hi'] - v['lo'] < 3]
check(not cross, '성부 교차/과밀 없음')

print('\n[5] 하모니 vs 로즈 보이싱 반음 충돌')
# 리드 멜로디 자신이 이미 만드는 rub 은 화성 언어이지 결함이 아니다 -> 승인 목록으로 고정
ACCEPTED = {
    ('Gmaj9', 'G', 'F#'): 'maj7 보이싱 위의 근음 rub — 리드도 같은 관계를 만든다',
    ('F#m9', 'A', 'G#'):  '9th 위의 ♭3 — 리드도 만든다',
    ('Bm9', 'C#', 'D'):   'm9 고유의 rub. 8분음 2개뿐이고 코러스 밀도 안에서 허용',
}
lead_rubs = {(mg.chord_at(bt), N[p % 12], N[v % 12])
             for bt, d, p in ch for v in mg.CH[mg.chord_at(bt)][0] if abs(p - v) == 1}
found = {(mg.chord_at(bt), N[p % 12], N[v % 12])
         for evs in (lo_h, hi_h) for bt, d, p in evs
         for v in mg.CH[mg.chord_at(bt)][0] if abs(p - v) == 1}
for key in sorted(found):
    if key in ACCEPTED:
        print(f'  ▷ 승인됨 {key[0]} {key[1]}–{key[2]}: {ACCEPTED[key]}')
    elif key in lead_rubs:
        print(f'  ▷ 리드도 만드는 rub {key[0]} {key[1]}–{key[2]}')
check(not (found - set(ACCEPTED) - lead_rubs), '미승인 반음 충돌 없음')

print('\n[6] 가사 음절 그리드 — 1절 대 2절')
per_bar = collections.Counter(bar for bar, *_ in mg.VERSE_MEL)
v2 = mg.VERSE2_WORDS[:]
ok = True
for bar in sorted(per_bar):
    want = per_bar[bar]
    got, v2 = v2[:want], v2[want:]
    if len(got) != want:
        ok = False
    print(f'  · {bar}마디 음절 {want}개  ←  {" ".join(got)}')
check(ok and not v2, f'2절이 1절 음절 그리드에 정확히 맞음 (남은 단어: {v2 or "없음"})')

print('\n[7] 보컬 최고음 — 전조를 반영한 실제 상한')
peak = max(p for _b, _d, p in ch)
check(peak + 2 <= 76, f'코러스 최고음 {nm(peak)} → 전조 후 {nm(peak + 2)} (상한 E5)')

print('\n' + ('✘ 실패 %d건' % len(FAIL) if FAIL else '✔ 전 항목 통과')
      + (f' / ▲ 경고 {len(WARN)}건' if WARN else ''))
sys.exit(1 if FAIL else 0)
