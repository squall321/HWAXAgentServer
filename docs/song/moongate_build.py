#!/usr/bin/env python3
"""
"Moongate" — 왕도적 작곡 순서대로 곡을 조립하는 빌더.

  STEP 1  모티프          -> 01_motif.mid
  STEP 2  구조/코드 골격   -> 02_structure.mid   (코드+베이스+드럼, 멜로디 없음)
  STEP 3~6 멜로디·편곡    -> 03_full.mid        (전 트랙)

D major / 112 BPM / 76마디(≈2:43), 마지막 코러스와 아웃트로만 E major.
의존성 없음(표준 라이브러리만). 실행: python3 moongate_build.py
"""
import os, random, struct

PPQ = 480
BPM = 112

# 곡 전체의 조옮김(반음). 0 = 원조 D major.
# 보컬이 정해지면 이 값 하나만 바꾼다 — 드럼을 뺀 모든 파트가 따라 움직인다.
# 어떤 값이 그 가수에게 맞는지는 `python3 check.py --fit <최저음> <최고음>` 이 알려준다.
TRANSPOSE = 0

# ── 의도한 악기 (MIDI 메타이벤트 FF 04 'Instrument Name') ────────
# GM 프로그램 번호는 "플루트 비슷한 것"까지밖에 전달하지 못한다. MIDI 규격에는
# 트랙 이름(FF 03)과 별개로 **악기 이름(FF 04)** 이 있어서, 어떤 음색을 의도했는지
# 파일 안에 남길 수 있다. GM 프로그램은 그대로 두어 폴백으로 쓴다.
# 자세한 후보 라이브러리는 INSTRUMENTS.md.
INSTRUMENTS = {
    'Lead Vocal (guide)':        'Synthesizer V AI - Mai 2 (English; vocal mode per section)',
    'Vocal Harmony (3rd below)': 'Synthesizer V AI - Mai 2 (Breathy, under lead)',
    'Vocal Harmony (3rd above)': 'Synthesizer V AI - Mai 2 (Breathy, under lead)',
    'Signature Whistle':         'LOW D WHISTLE (bottom note D4) - NOT a standard tin whistle',
    'Rhodes':                    'Rhodes MK1 electric piano, tremolo; DX7 EP layer for attack',
    'Guitar (16th chops)':       'Clean Strat + compressor, 16th-note chops, 9th/11th voicings',
    'Bass':                      'Fingered P-bass, or round analog synth bass',
    'Celtic Harp':               'Celtic lever harp',
    'Strings':                   'Small chamber string section (or synth strings)',
    'Drums':                     'Dry acoustic kit, minimal room (GM drum map)',
}

# ── 휴머나이즈 ────────────────────────────────────────────────
# 보컬 트랙에는 걸지 않는다. 피치 드리프트·비브라토는 Synthesizer V 가 생성하므로
# 여기서 흔들면 이중으로 흔들린다. 악기 트랙만 대상. (VOCAL-MAI2.md 참조)
# 시드를 고정해 빌드가 재현 가능하게 둔다 — 매번 달라지면 검사도 믹스도 의미가 없다.
SEED = 20260830
RNG = random.Random(SEED)

# shift: 박 단위 선후(+는 뒤로) / swing8·swing16: 뒷박 밀기 / vel: 벨로시티 흔들기 / len: 길이 흔들기
GROOVE = {
    'Guitar (16th chops)': dict(shift=+0.005, swing16=0.035, vel=10, len=0.30),
    'Drums':               dict(shift=0.0,    swing8=0.020,  vel=8,  len=0.10),
    'Bass':                dict(shift=-0.015,                vel=7,  len=0.12),
    'Rhodes':              dict(shift=+0.010,                vel=8,  len=0.15),
    'Celtic Harp':         dict(shift=+0.008,                vel=12, len=0.20),
    'Strings':             dict(shift=+0.020,                vel=6,  len=0.08),
    'Signature Whistle':   dict(shift=+0.012,                vel=6,  len=0.10),
}
OUT = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────── MIDI 최소 구현

def vlq(n):
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


class Track:
    def __init__(self, name, program=None, channel=0):
        self.name, self.program, self.ch = name, program, channel
        self.groove = GROOVE.get(name, {})
        self.ev = []          # (tick, order, data)

    def cc(self, beat, num, val):
        self.ev.append((max(0, int(round(beat * PPQ))), 0.4,
                        bytes([0xB0 | self.ch, num, max(0, min(127, int(val)))])))

    def note(self, beat, dur, pitch, vel=90, lyric=None):
        if pitch is None:
            return
        if self.ch != 9:                       # 드럼맵은 조옮김하지 않는다
            pitch += TRANSPOSE
        g = self.groove
        if g:                                  # 악기 트랙만 흔든다 (보컬은 GROOVE 에 없다)
            pos = round(beat % 1.0, 4)
            if pos == 0.5:
                beat += g.get('swing8', 0.0)
            elif pos in (0.25, 0.75):
                beat += g.get('swing16', 0.0)
            beat = max(0.0, beat + g.get('shift', 0.0))
            if g.get('vel'):
                vel = max(1, min(127, vel + RNG.randint(-g['vel'], g['vel'])))
            if g.get('len'):
                dur *= 1.0 + RNG.uniform(-g['len'], g['len'])
        t0 = max(0, int(round(beat * PPQ)))
        t1 = max(t0 + 20, int(round((beat + dur) * PPQ)) - 8)   # 살짝 띄어 레가토 방지
        self.ev.append((t1, 0, bytes([0x80 | self.ch, pitch, 0])))
        if lyric:                              # Synthesizer V 가 임포트 시 읽어가는 가사 이벤트
            w = lyric.strip('()').encode()
            self.ev.append((t0, 0.5, b'\xff\x05' + vlq(len(w)) + w))
        self.ev.append((t0, 1, bytes([0x90 | self.ch, pitch, vel])))

    def chord(self, beat, dur, pitches, vel=70):
        for p in pitches:
            self.note(beat, dur, p, vel)

    def to_bytes(self):
        data = bytearray()
        data += b'\x00\xff\x03' + vlq(len(self.name)) + self.name.encode()
        want = INSTRUMENTS.get(self.name)
        if want:                               # FF 04 = Instrument Name (의도한 음색)
            data += b'\x00\xff\x04' + vlq(len(want)) + want.encode()
        if self.program is not None:
            data += b'\x00' + bytes([0xC0 | self.ch, self.program])
        prev = 0
        for tick, _, ev in sorted(self.ev, key=lambda e: (e[0], e[1])):
            data += vlq(tick - prev) + ev
            prev = tick
        data += b'\x00\xff\x2f\x00'
        return b'MTrk' + struct.pack('>I', len(data)) + bytes(data)


def write_midi(path, tracks):
    head = b'MThd' + struct.pack('>IHHH', 6, 1, len(tracks) + 1, PPQ)
    meta = bytearray()
    meta += b'\x00\xff\x03\x08Moongate'
    meta += b'\x00\xff\x51\x03' + struct.pack('>I', int(60_000_000 / BPM))[1:]
    meta += b'\x00\xff\x58\x04\x04\x02\x18\x08'
    fifths = ((2 + 7 * TRANSPOSE + 5) % 12) - 5           # D major 기준 조표 재계산
    meta += b'\x00\xff\x59\x02' + bytes([fifths & 0xFF, 0])
    meta += b'\x00\xff\x2f\x00'
    conductor = b'MTrk' + struct.pack('>I', len(meta)) + bytes(meta)
    with open(path, 'wb') as f:
        f.write(head + conductor + b''.join(t.to_bytes() for t in tracks))


def b(bar, beat=0.0):
    """마디 번호(1-base) + 박(0-base) -> 절대 박"""
    return (bar - 1) * 4 + beat


# ─────────────────────────────────────────────── 코드 사전
# (로즈 보이싱 4성부, 베이스 루트)
CH = {
    # 보이싱은 '앞 코드에서 가장 적게 움직이는 자리'로 잡았다. 룩업이 아니라 성부 진행이다.
    # 특히 이 곡에서 제일 자주 쓰는 G△9->A7 이 매번 17반음씩 뛰던 것을 7반음으로 줄였고,
    # 전조점(60->61마디) A7 -> A△9 는 G->G# 반음 하나만 움직이는 피벗이 됐다.
    # 부수 효과: 최저성부가 C#4(277Hz)에서 F#4(370Hz)로 올라가 200~400Hz 가 덜 뭉친다(기획서 §7).
    'Em9':     ([67, 71, 74, 76], 40),   # G B D E    / E
    'A13':     ([67, 71, 73, 76], 45),   # G B C# E   / A   (13th 은 멜로디에 맡긴다)
    'F#m9':    ([66, 69, 73, 76], 42),   # F# A C# E  / F#
    'B7b13':   ([67, 69, 71, 75], 47),   # G A B D#   / B   ★b13 클러스터
    'Bm9':     ([66, 69, 73, 74], 47),   # F# A C# D  / B
    'C#m7b5':  ([67, 71, 73, 76], 49),   # G B C# E   / C#
    'F#7b9':   ([67, 70, 73, 76], 42),   # G A# C# E  / F#
    'Gmaj9':   ([66, 69, 71, 74], 43),   # F# A B D   / G
    'A7':      ([67, 71, 73, 76], 45),   # G B C# E   / A
    'A7sus4':  ([67, 69, 74, 76], 45),   # G A D E    / A
    'D69':     ([66, 69, 71, 76], 38),   # F# A B E   / D
    'D/F#':    ([66, 69, 74, 76], 42),   # F# A D E   / F#
    'Gm6':     ([67, 70, 74, 76], 43),   # G A# D E   / G   ★차용화음
    # ── E major (전조 구간). D 조 세트의 형태를 그대로 +2 로 옮겨 감촉을 유지한다.
    'Amaj9':   ([68, 71, 73, 76], 45),   # G# B C# E  / A   ← A7 에서 G->G# 하나만 움직인다
    'B7':      ([69, 73, 75, 78], 47),
    'G#m9':    ([68, 71, 75, 78], 44),
    'C#m9':    ([68, 71, 75, 76], 49),
    'E69':     ([68, 71, 73, 78], 40),
    'B7sus4':  ([69, 71, 76, 78], 47),
    'A#dim7':  ([67, 70, 73, 76], 46),
    'E/B':     ([68, 71, 76, 78], 47),
}

# ─────────────────────────────────────────────── 구조 (마디 -> 코드)
# (시작마디, 지속박, 코드)
PROG = []


def sec(start_bar, chords):
    """chords: 마디당 하나(4박) 또는 (코드,박) 튜플 리스트"""
    bar = start_bar
    for c in chords:
        if isinstance(c, tuple):
            beat = b(bar)
            for name, dur in c:
                PROG.append((beat, dur, name))
                beat += dur
            bar += 1
        else:
            PROG.append((b(bar), 4.0, c))
            bar += 1
    return bar


VERSE = ['Em9', 'A13', 'F#m9', 'B7b13']
PRE = ['Bm9', (('C#m7b5', 2.0), ('F#7b9', 2.0)), 'Gmaj9', (('A7sus4', 2.0), ('A7', 2.0))]
CHORUS = ['Gmaj9', 'A7', 'F#m9', 'Bm9', 'Gmaj9', 'A7', 'D69', 'A7sus4']
POST = ['Gmaj9', 'A7', 'F#m9', 'Bm9']

sec(1, POST)                                              # 1-4    Intro
sec(5, VERSE * 2)                                         # 5-12   Verse 1
sec(13, PRE)                                              # 13-16  Pre 1
sec(17, CHORUS)                                           # 17-24  Chorus 1
sec(25, POST)                                             # 25-28  Post 1
sec(29, VERSE * 2)                                        # 29-36  Verse 2
sec(37, PRE)                                              # 37-40  Pre 2
sec(41, CHORUS)                                           # 41-48  Chorus 2
sec(49, POST)                                             # 49-52  Post 2
sec(53, ['Bm9', 'Gmaj9', 'D/F#', 'Gm6',
         'Em9', 'A7sus4', 'A7', (('A7', 2.0),)])           # 53-60  Bridge (60마디 후반 G.P.)
sec(61, ['Amaj9', 'B7', 'G#m9', 'C#m9',
         'Amaj9', 'B7', 'E69', 'B7sus4'])                # 61-68  Final Chorus (E)
sec(69, ['Amaj9', 'A#dim7', 'E/B', 'B7sus4',
         'Amaj9', 'A#dim7', 'E69', 'E69'])                 # 69-76  Outro (E)

SECTIONS = {
    'intro': range(1, 5), 'verse1': range(5, 13), 'pre1': range(13, 17),
    'chorus1': range(17, 25), 'post1': range(25, 29), 'verse2': range(29, 37),
    'pre2': range(37, 41), 'chorus2': range(41, 49), 'post2': range(49, 53),
    'bridge': range(53, 61), 'final': range(61, 69), 'outro': range(69, 77),
}


def bar_of(beat):
    return int(beat // 4) + 1


def in_(beat, *names):
    bar = bar_of(beat)
    return any(bar in SECTIONS[n] for n in names)


# ─────────────────────────────────────────────── 화성(하모니) 생성기
# D major 음계 위의 3도 화음. 단, 3도가 그 자리 화음의 구성음이 아니면
# (예: D6/9 위의 G = 회피음) 한 음 더 비켜나 4도가 된다.

SCALE_D = {2, 4, 6, 7, 9, 11, 1}                       # D E F# G A B C#
SCALE_P = sorted(p for p in range(36, 96) if p % 12 in SCALE_D)

CHORD_PC = {                                            # 화음별 허용 음(9th·13th 색채 포함)
    'Gmaj9':  {7, 11, 2, 6, 9},   'A7':      {9, 1, 4, 7, 11},
    'F#m9':   {6, 9, 1, 4, 8},    'Bm9':     {11, 2, 6, 9, 1},
    'D69':    {2, 6, 9, 11, 4},   'A7sus4':  {9, 2, 4, 7, 11},
    'Em9':    {4, 7, 11, 2, 6},   'A13':     {9, 1, 4, 7, 11, 6},
    'B7b13':  {11, 3, 6, 9, 1, 7},'C#m7b5':  {1, 4, 7, 11},
    'F#7b9':  {6, 10, 1, 4, 7},   'D/F#':    {2, 6, 9, 4},
    'Gm6':    {7, 10, 2, 4},
}

_SPANS = [(bt, bt + dur, nm) for bt, dur, nm in PROG]


def chord_at(beat):
    for st, en, nm in _SPANS:
        if st <= beat < en:
            return nm
    return None


def _step(pitch, steps):
    return SCALE_P[SCALE_P.index(pitch) + steps]


def harmonize(events, below=True, ceiling=76, floor=57):
    """(beat, dur, pitch) 리스트 -> 3도 하모니. 붙일 수 없는 음은 조용히 뺀다.

    회피음이면 한 음 더 비켜나되, 상단 성부에 한해 **다음 음이 한 음 아래로 해결되면
    그대로 둔다**(4-3 지연해결). 하단 성부에 같은 규칙을 쓰면 근음과 ♭9로 부딪힌다.
    """
    d = -1 if below else 1
    src = [(bt, du, p) for bt, du, p in events if p % 12 in SCALE_D]  # 반음 색채음은 제외
    raw = [_step(p, 2 * d) for _bt, _du, p in src]
    fixed = []
    for (bt, _du, _p), h in zip(src, raw):
        tones = CHORD_PC.get(chord_at(bt))
        fixed.append(_step(h, d) if (tones and h % 12 not in tones) else h)

    out = []
    for i, ((bt, du, _p), h) in enumerate(zip(src, raw)):
        pick = fixed[i]
        if (not below and pick != h and i + 1 < len(fixed) and fixed[i + 1] == _step(h, -1)
                and all(abs(h - v) != 1 for v in CH[chord_at(bt)][0])):
            pick = h                           # 지연해결이 되고, 울리는 보이싱과 반음이 아니면 매단다
        tones = CHORD_PC.get(chord_at(bt))
        if pick == h and tones and h % 12 not in tones and pick == fixed[i]:
            continue                           # 비켜날 곳도 해결도 없으면 뺀다
        # 상한 76(E5): 리드 최고음 D5 보다 한 음 위까지 허용한다. 이 여유가 있어야
        # A7 위 B4 의 3도(D5)가 보이싱의 C#5 와 반음으로 부딪힐 때 4도(E5)로 비켜날 수 있다.
        # 킬 포인트(D5)의 3도 위 F#5 는 여전히 상한 밖이라, 정점에서는 리드가 혼자 남는다.
        if not (floor <= pick <= ceiling):     # 음역 밖이면 그 음은 쉰다
            continue
        out.append((bt, du, pick))
    return out


def mel_events(data, start_bar, transpose=0):
    return [(b(start_bar + bar - 1, beat), dur, pitch + transpose)
            for bar, beat, dur, pitch, _syl in data]


def move(events, d_bars, d_pitch=0):
    return [(bt + d_bars * 4, d, p + d_pitch) for bt, d, p in events]


def between(events, first_bar, last_bar):
    # +0.5 박: 앞 마디 끝에서 당겨 온 음은 뒤따르는 프레이즈에 속한다
    return [e for e in events if first_bar <= bar_of(e[0] + 0.5) <= last_bar]


# ─────────────────────────────────────────────── STEP 1 · 시그니처 모티프
# "Feather Motif" — G△7|A7|F#m7|Bm7 위 4마디. (beat, dur, pitch)
MOTIF = [
    (1.0, 0.5, 69), (1.5, 0.5, 71), (2.0, 2.0, 74),                    # A4 B4 D5(롱톤)
    (4.0, 1.0, 73), (5.0, 1.0, 71), (6.0, 1.0, 69), (7.0, 1.0, 66),    # C#5 B4 A4 F#4 (하행 한숨)
    (8.0, 1.5, 66), (9.5, 1.5, 74), (11.0, 0.5, 73), (11.5, 0.5, 71),  # F#4→D5 6도 도약
    (12.0, 1.0, 69), (13.0, 3.0, 71),                                  # A4 B4(롱톤)
]

# 포스트코러스: 1~3마디는 휘슬과 유니즌 보칼리즈, 4마디는 가사가 붙은 태그로 갈라진다.
# (휘슬은 B4 롱톤을 유지하고 보컬이 그 밑으로 내려간다 -> 다음 섹션 첫 음 E 로 연결)
POST_TAG = [(12.0, 0.5, 69, 'moon'), (12.5, 0.5, 71, 'gate'), (13.0, 0.5, 69, 'take'),
            (13.5, 0.5, 66, 'me'), (14.0, 2.0, 64, 'home')]

# ─────────────────────────────────────────────── STEP 3~5 · 멜로디
# (마디, 박, 길이, 음, 음절)
CHORUS_MEL = [
    # 당김은 '줄 안에서'만 쓴다(1->2, 3->4, 6->7). 줄이 끝나는 자리에는 숨을 남긴다.
    (1, 0.0, 0.5, 69, 'O'), (1, 0.5, 0.25, 71, 'pen'), (1, 0.75, 0.25, 71, 'the'),
    (1, 1.0, 1.5, 74, 'moon'), (1, 2.5, 0.5, 71, 'gate'),
    (1, 3.5, 0.5, 69, 'let'),                                     # 당김
    (2, 0.0, 0.5, 71, 'the'), (2, 0.5, 1.0, 69, 'eve'),
    (2, 1.5, 0.5, 66, 'ning'), (2, 2.0, 1.5, 64, 'in'),           # -> 숨
    (3, 0.0, 0.5, 66, 'I'), (3, 0.5, 0.5, 66, 'have'),            # 제자리에서 새 줄
    (3, 1.0, 0.25, 69, 'fal'), (3, 1.25, 0.25, 69, 'len'), (3, 1.5, 0.5, 69, 'a'),
    (3, 2.0, 0.5, 71, 'thou'), (3, 2.5, 0.5, 69, 'sand'), (3, 3.0, 0.5, 66, 'times'),
    (3, 3.5, 0.5, 64, 'just'),                                    # 당김
    (4, 0.0, 0.5, 66, 'to'), (4, 0.5, 1.0, 69, 'land'), (4, 1.5, 0.5, 66, 'here'),
    (4, 2.0, 0.5, 64, 'a'), (4, 2.5, 1.0, 62, 'gain'),
    (4, 3.5, 0.5, 67, 'Give'),                                    # 당김
    (5, 0.0, 0.5, 69, 'me'), (5, 0.5, 0.5, 71, 'one'), (5, 1.0, 1.0, 71, 'white'),
    (5, 2.0, 0.5, 69, 'fea'), (5, 2.5, 1.0, 67, 'ther'),          # -> 숨
    (6, 0.0, 0.5, 69, 'and'), (6, 0.5, 0.25, 71, 'a'), (6, 0.75, 0.75, 71, 'rea'),
    (6, 1.5, 0.5, 69, 'son'), (6, 2.0, 0.5, 67, 'to'), (6, 2.5, 1.0, 66, 'stay'),
    (6, 3.5, 0.5, 66, 'and'),                                     # 당김 — 킬 포인트로 밀어넣는다
    (7, 0.0, 0.5, 69, "I'll"), (7, 0.5, 0.5, 71, 'find'), (7, 1.0, 0.5, 69, 'you'),
    (7, 1.5, 0.25, 66, 'in'), (7, 1.75, 0.25, 67, 'the'),         # 16분으로 몰아치고
    (7, 2.0, 2.0, 74, 'mor'),                                     # ★킬 포인트: 최고음 D5·2박·강박·5도 도약
    (8, 0.0, 0.5, 69, 'ning'), (8, 0.5, 0.5, 66, 'of'), (8, 1.0, 0.5, 64, 'a'),
    (8, 1.5, 0.5, 66, 'new'), (8, 2.0, 1.5, 62, 'day'),           # A7sus4 위 D = sus 여운
]

VERSE_MEL = [
    # 한 줄 = 2마디. 줄 안(홀수->짝수)에서만 당기고, 줄이 끝나는 짝수 마디 뒤에 1박 숨을 둔다.
    # 그 1박이 2절에서 휘슬이 대답할 자리가 된다.
    (1, 0.0, 0.5, 64, 'Rain'), (1, 0.5, 0.25, 67, 'on'), (1, 0.75, 0.25, 67, 'the'),
    (1, 1.0, 0.5, 69, 'lan'), (1, 1.5, 0.5, 67, 'tern'), (1, 2.0, 1.0, 64, 'glass'),
    (1, 3.5, 0.5, 64, 'the'),                                     # 당김
    (2, 0.0, 0.5, 66, 'har'), (2, 0.5, 0.5, 69, 'bor'), (2, 1.0, 0.25, 67, 'turn'),
    (2, 1.25, 0.25, 66, 'ing'), (2, 1.5, 1.5, 64, 'gold'),        # -> 1박 숨
    (3, 0.0, 0.5, 66, 'a'), (3, 0.5, 0.25, 69, 'fid'), (3, 0.75, 0.25, 69, 'dle'),
    (3, 1.0, 0.5, 67, 'in'), (3, 1.5, 0.5, 69, 'the'), (3, 2.0, 0.5, 71, 'mar'),
    (3, 2.5, 1.0, 69, 'ket'),
    (3, 3.5, 0.5, 66, 'and'),                                     # 당김
    (4, 0.0, 0.5, 66, 'a'), (4, 0.5, 0.25, 67, 'sto'), (4, 0.75, 0.25, 66, 'ry'),
    (4, 1.0, 0.5, 63, 'some'), (4, 1.5, 0.5, 64, 'one'), (4, 2.0, 1.0, 66, 'told'),
    (5, 0.5, 0.5, 64, "I've"), (5, 1.0, 0.5, 67, 'worn'), (5, 1.5, 0.25, 67, 'a'),   # ★밀기
    (5, 1.75, 0.25, 69, 'hun'), (5, 2.0, 0.5, 67, 'dred'), (5, 2.5, 1.0, 64, 'names'),
    (5, 3.5, 0.5, 64, 'but'),                                     # 당김
    (6, 0.0, 0.5, 66, 'I'), (6, 0.5, 0.5, 69, 'keep'), (6, 1.0, 0.25, 67, 'this'),
    (6, 1.25, 0.25, 66, 'one'), (6, 1.5, 0.5, 64, 'for'), (6, 2.0, 1.0, 62, 'you'),
    (7, 0.0, 0.5, 66, 'the'), (7, 0.5, 0.5, 69, 'one'), (7, 1.0, 0.25, 69, 'you'),
    (7, 1.25, 0.75, 71, 'called'), (7, 2.0, 0.5, 69, 'me'), (7, 2.5, 0.5, 66, 'soft'),
    (7, 3.0, 0.5, 64, 'ly'),
    (7, 3.5, 0.5, 63, 'when'),                                    # 당김
    (8, 0.0, 0.5, 64, 'the'), (8, 0.5, 0.5, 66, 'sum'), (8, 1.0, 0.25, 64, 'mer'),
    (8, 1.25, 0.25, 63, 'was'), (8, 1.5, 0.5, 64, 'still'), (8, 2.0, 1.5, 66, 'new'),
]

VERSE2_WORDS = ['Smoke', 'from', 'the', 'ket', 'tle', 'rings',
                'the', 'mar', 'ket', 'clos', 'ing', 'down',
                'we', 'camp', 'be', 'side', 'the', 'wa', 'ter',
                'and', 'the', 'fire', 'keeps', 'the', 'cold', 'out',
                'The', 'world', 'can', 'end', 'Sun', 'day',
                'and', 'o', 'pen', 'up', 'on', 'Mon', 'day',
                'and', "I'll", 'still', 'be', 'stand', 'ing', 'here',
                'grow', 'ing', 'old', 'er', 'hold', 'ing', 'on']

PRE_MEL = [
    (1, 0.0, 0.5, 66, 'Ev'), (1, 0.5, 0.5, 66, "'ry"), (1, 1.0, 0.5, 67, 'end'),
    (1, 1.5, 0.5, 66, 'ing'), (1, 2.0, 0.5, 64, 'is'), (1, 2.5, 0.5, 66, 'a'),
    (1, 3.0, 1.0, 62, 'door'),
    (2, 0.0, 0.5, 64, "I've"), (2, 0.5, 0.5, 64, 'walked'), (2, 1.0, 0.5, 67, 'be'),
    (2, 1.5, 1.5, 66, 'fore'), (2, 3.0, 0.5, 70, '(ooh'), (2, 3.5, 0.5, 71, 'ooh)'),
    # 3마디 천장을 A4 로 낮춘다 — 프리가 코러스와 같은 높이면 코러스가 올라간 느낌이 나지 않는다
    (3, 0.0, 0.5, 67, 'count'), (3, 0.5, 0.5, 67, 'the'), (3, 1.0, 0.5, 69, 'fea'),
    (3, 1.5, 0.5, 67, 'thers'), (3, 2.0, 0.5, 69, 'on'), (3, 2.5, 0.5, 67, 'the'),
    (3, 3.0, 1.0, 66, 'floor'),
    (4, 0.0, 1.0, 67, 'one'), (4, 1.0, 1.0, 69, 'two'), (4, 2.0, 0.5, 69, 'and'),
    (4, 2.5, 0.5, 71, 'we'), (4, 3.0, 0.5, 69, 'be'), (4, 3.5, 0.5, 71, 'gin'),
]

BRIDGE_MEL = [
    (1, 0.0, 0.5, 62, 'If'), (1, 0.5, 0.5, 66, 'this'), (1, 1.0, 0.5, 66, 'is'),
    (1, 1.5, 0.5, 66, 'the'), (1, 2.0, 1.0, 69, 'last'), (1, 3.0, 1.0, 66, 'life'),
    (2, 0.0, 0.5, 67, 'tell'), (2, 0.5, 0.5, 69, 'me'), (2, 1.0, 2.0, 71, 'now'),
    (3, 0.0, 0.5, 66, "I'll"), (3, 0.5, 0.5, 69, 'spend'), (3, 1.0, 0.5, 69, 'it'),
    (3, 1.5, 1.0, 71, 'slow'), (3, 2.5, 0.5, 69, 'er'),
    (4, 0.0, 0.5, 67, "I'll"), (4, 0.5, 0.5, 70, 'spend'),          # ★Bb4 = Gm6 의 눈물
    (4, 1.0, 0.5, 69, 'it'), (4, 1.5, 2.0, 67, 'loud'),
    (5, 0.0, 1.0, 64, 'No'), (5, 1.0, 0.5, 66, 'more'), (5, 1.5, 0.5, 67, 'wait'),
    (5, 2.0, 0.5, 66, 'ing'), (5, 2.5, 0.5, 64, 'for'), (5, 3.0, 0.5, 66, 'the'),
    (5, 3.5, 0.5, 67, 'sky'),
    (6, 0.0, 0.5, 69, 'to'), (6, 0.5, 1.5, 71, 'fall'),
    (7, 0.0, 0.5, 69, 'you'), (7, 0.5, 0.5, 71, 'were'), (7, 1.0, 0.5, 69, 'the'),
    (7, 1.5, 0.5, 71, 'rea'), (7, 2.0, 0.5, 69, 'son'), (7, 2.5, 0.5, 67, 'I'),
    (7, 3.0, 0.5, 66, 'came'), (7, 3.5, 0.5, 64, 'back'),
    (8, 0.0, 0.5, 66, 'at'), (8, 0.5, 1.0, 69, 'all'),
]

# ─────────────────────────────────────────────── 트랙 조립 헬퍼

def put_mel(track, data, start_bar, vel=95, transpose=0, words=None, lyrics=True):
    for i, (bar, beat, dur, pitch, syl) in enumerate(data):
        track.note(b(start_bar + bar - 1, beat), dur, pitch + transpose, vel,
                   lyric=((words[i] if words else syl) if lyrics else None))


def put_topline(whi, hrp, rho):
    """인스트루멘털 전용 — 보컬이 빠진 자리에서 누가 선율을 이어받는가.

    휘슬에 전부 맡기면 3분 내내 시그니처가 노출돼 라이트모티프가 특별하지 않게 된다.
    벌스·프리는 하프(옥타브 위), 코러스는 휘슬(제자리), 브릿지는 로즈(옥타브 위),
    마지막 코러스 앞 4마디(낙사비)는 로즈 단독 — 보컬판의 편성 의도를 그대로 따른다.
    """
    sub = lambda data, bars: [d for d in data if d[0] in bars]
    for sb in (5, 29):
        put_mel(hrp, VERSE_MEL, sb, 68, transpose=12, lyrics=False)
    for sb in (13, 37):
        put_mel(hrp, PRE_MEL, sb, 70, transpose=12, lyrics=False)
    for sb in (17, 41):
        put_mel(whi, CHORUS_MEL, sb, 88, lyrics=False)
    put_mel(rho, BRIDGE_MEL, 53, 78, transpose=12, lyrics=False)
    put_mel(rho, sub(CHORUS_MEL, {1, 2, 3, 4}), 61, 76, transpose=2, lyrics=False)
    put_mel(whi, sub(CHORUS_MEL, {5, 6, 7, 8}), 61, 92, transpose=2, lyrics=False)


def put_motif(track, start_bar, vel=88, transpose=0, octave=0, expressive=False):
    for beat, dur, pitch in MOTIF:
        bt = b(start_bar, beat)
        p = pitch + transpose + 12 * octave
        if expressive and dur >= 2.0:                        # 켈틱 '컷': 롱톤 앞의 아주 짧은 윗음.
            track.note(bt, 0.07, p + 2, max(1, vel - 18))    # 이게 없으면 휘슬이 신스처럼 들린다
            bt += 0.07
            dur -= 0.07
        track.note(bt, dur, p, vel)
        if not expressive:
            continue
        track.cc(bt, 11, 88)                                 # 숨: 음 안에서 살짝 부풀린다
        track.cc(bt + dur * 0.55, 11, 108)
        if dur >= 1.5:                                       # 롱톤에만 비브라토를 늦게 얹는다
            for i in range(7):
                track.cc(bt + dur * 0.45 + dur * 0.5 * i / 6, 1, int(52 * i / 6))
            track.cc(bt + dur, 1, 0)


def build_chords(rhodes, gtr, bass):
    for beat, dur, name in PROG:
        voic, root = CH[name]
        bar = bar_of(beat)
        quiet = in_(beat, 'intro') or (61 <= bar <= 64) or (53 <= bar <= 56)
        # 로즈: 섹션 첫 박에 지속 + 8분 백비트 반복
        rhodes.cc(beat, 64, 127)                             # 코드마다 페달 밟고
        rhodes.cc(beat + dur - 0.08, 64, 0)                  # 다음 코드 직전에 뗀다
        rhodes.chord(beat, dur, voic, 58 if quiet else 68)
        if not quiet and dur >= 4.0:
            rhodes.chord(beat + 2.5, 1.0, voic, 52)
        # 기타 16비트 커팅 (벌스·코러스·포스트·마지막 후반)
        if in_(beat, 'verse1', 'verse2', 'chorus1', 'chorus2', 'post1', 'post2') or 65 <= bar <= 68:
            top = voic[1:]
            for k in range(int(dur * 4)):
                p = beat + k * 0.25
                if k % 4 in (1, 2):                 # 업비트 위주 커팅
                    gtr.chord(p, 0.2, top, 46 if k % 2 else 54)
        # 베이스
        if quiet and not (53 <= bar <= 56):
            continue
        if 53 <= bar <= 56:
            bass.note(beat, dur, root, 74)
            continue
        if in_(beat, 'verse1', 'verse2'):
            pat = [(0.0, 0.75), (1.5, 0.5), (2.5, 0.5), (3.0, 0.5)]
            for j, (o, d) in enumerate(pat):
                if o < dur:
                    bass.note(beat + o, d, root + (0 if j != 3 else 7), 82)
        else:
            steps = [(0.0, 0.75), (1.0, 0.5), (1.75, 0.5), (2.5, 0.5), (3.0, 0.5), (3.5, 0.5)]
            for j, (o, d) in enumerate(steps):
                if o < dur:
                    p = root + (12 if j == 4 else (7 if j == 2 else 0))
                    bass.note(beat + o, d, p, 86 if j == 0 else 76)


def build_drums(dr):
    K, S, RIM, HH, OH, SHK, CR, T1, T2 = 36, 38, 37, 42, 46, 70, 49, 47, 45
    for bar in range(1, 77):
        beat = b(bar)
        style = ('none' if bar <= 4 or 53 <= bar <= 56 or 61 <= bar <= 64 else
                 'verse' if in_(beat, 'verse1', 'verse2', 'pre1', 'pre2') else
                 'build' if 57 <= bar <= 60 else 'four')
        if style == 'none':
            for k in range(8):
                dr.note(beat + k * 0.5, 0.2, SHK, 40 if k % 2 else 52)
            continue
        if style == 'verse':
            for o in (0.0, 1.5, 2.5):
                dr.note(beat + o, 0.3, K, 100)
            for o in (1.0, 3.0):
                dr.note(beat + o, 0.3, RIM, 92)
            for k in range(8):
                dr.note(beat + k * 0.5, 0.2, HH, 44 if k % 2 else 62)
        elif style == 'build':
            for o in (0.0, 1.0, 2.0, 3.0):
                dr.note(beat + o, 0.3, K, 104)
            for o in (1.0, 3.0):
                dr.note(beat + o, 0.3, S, 98)
            for k in range(16):
                dr.note(beat + k * 0.25, 0.15, HH, 42 + (18 if k % 4 == 0 else 0))
        else:
            for o in (0.0, 1.0, 2.0, 3.0):
                dr.note(beat + o, 0.3, K, 106)
            for o in (1.0, 3.0):
                dr.note(beat + o, 0.3, S, 100)
            for k in range(8):
                dr.note(beat + k * 0.5, 0.25, OH if k % 2 else HH, 58 if k % 2 else 70)
            for k in range(4):
                dr.note(beat + 0.25 + k, 0.15, SHK, 46)
        # 섹션 진입 크래시 / 마디 끝 필
        if bar in (17, 25, 41, 49, 61, 65, 69):
            dr.note(beat, 0.5, CR, 108)
        if bar in (12, 16, 24, 28, 36, 40, 48, 52, 60, 68):
            dr.note(beat + 3.0, 0.25, T1, 96)
            dr.note(beat + 3.5, 0.25, T2, 100)
        if bar == 60:                                   # 브릿지 끝 G.P.
            dr.ev = [e for e in dr.ev if not (b(60, 2.0) * PPQ <= e[0] < b(61) * PPQ)]
            dr.note(b(60, 2.0), 0.25, T1, 100)
            dr.note(b(60, 2.5), 0.25, T2, 104)
            dr.note(b(60, 3.0), 0.5, S, 110)
            dr.note(b(60, 3.5), 0.5, S, 118)


def make_tracks(with_melody=True, with_rhythm=True, topline=False):
    RNG.seed(SEED)                                           # 호출마다 같은 흔들림
    voc = Track('Lead Vocal (guide)', 54, 0)
    hlo = Track('Vocal Harmony (3rd below)', 54, 7)
    hhi = Track('Vocal Harmony (3rd above)', 54, 8)
    whi = Track('Signature Whistle', 73, 1)
    rho = Track('Rhodes', 4, 2)
    gtr = Track('Guitar (16th chops)', 27, 3)
    bas = Track('Bass', 33, 4)
    hrp = Track('Celtic Harp', 46, 5)
    strs = Track('Strings', 48, 6)
    dr = Track('Drums', None, 9)

    if with_rhythm:
        build_chords(rho, gtr, bas)
        build_drums(dr)

    if with_melody:
        # 인트로: 휘슬 모티프 + 하프 아르페지오
        put_motif(whi, 1, 84, expressive=True)
        for beat, dur, name in PROG:
            if in_(beat, 'intro', 'bridge') or 69 <= bar_of(beat) <= 76:
                voic = CH[name][0]
                for k in range(8):
                    hrp.note(beat + k * 0.5, 0.5, voic[k % 4] + (12 if k >= 4 else 0), 52)
        # 벌스 / 프리 / 코러스 / 포스트 / 브릿지 / 마지막 코러스
        put_mel(voc, VERSE_MEL, 5)
        put_mel(voc, PRE_MEL, 13)
        put_mel(voc, CHORUS_MEL, 17)
        put_mel(voc, VERSE_MEL, 29, words=VERSE2_WORDS)      # 2절은 가사가 다르다
        put_mel(voc, PRE_MEL, 37)
        put_mel(voc, CHORUS_MEL, 41)
        put_mel(voc, BRIDGE_MEL, 53)
        put_mel(voc, CHORUS_MEL, 61, transpose=2)          # ★전조 +2도
        # 포스트코러스: 휘슬 + 보컬. 4마디째는 보칼리즈에서 가사 태그로 갈라진다.
        post_voc = [(b(25) + bt, d, p) for bt, d, p in MOTIF if bt < 12.0] \
                   + [(b(25) + bt, d, p) for bt, d, p, _s in POST_TAG]
        post_syl = {round(b(25) + bt, 3): sl for bt, _d, _p, sl in POST_TAG}
        for sb, evs in ((25, post_voc), (49, move(post_voc, 24))):
            put_motif(whi, sb, 92, expressive=True)
            for bt, d, p in evs:
                voc.note(bt, d, p, 84, lyric=post_syl.get(round(bt - (sb - 25) * 4, 3), 'oh'))
        # 2회차 포스트코러스만 3도 아래로 갈라 두께를 준다 (1회차는 유니즌으로 남긴다)
        for bt, d, p in move(harmonize(post_voc, below=True), 24):
            hlo.note(bt, d, p, 68, lyric=post_syl.get(round(bt - 96, 3), 'oh'))

        # 코러스 하모니 — 17~24마디 기준으로 계산해 두 번 옮겨 쓴다
        ch = mel_events(CHORUS_MEL, 17)
        lo, hi = harmonize(ch, below=True), harmonize(ch, below=False)
        ch_syl = {round(b(17 + bar - 1, beat), 3): sl
                  for bar, beat, _d, _p, sl in CHORUS_MEL}

        def syl_of(bt, shift_bars):
            return ch_syl.get(round(bt - shift_bars * 4, 3), 'ah')
        for bt, d, p in between(lo, 21, 24):                  # 코러스 1: 후반 4마디만
            hlo.note(bt, d, p, 66, lyric=syl_of(bt, 0))
        for bt, d, p in move(lo, 24):                         # 코러스 2: 전체
            hlo.note(bt, d, p, 72, lyric=syl_of(bt, 24))
        for bt, d, p in move(between(hi, 21, 24), 24):        # 코러스 2: 후반만 위로도
            hhi.note(bt, d, p, 64, lyric=syl_of(bt, 24))
        for bt, d, p in move(between(lo, 21, 24), 44, 2):     # 마지막 코러스: 65~68마디만
            hlo.note(bt, d, p, 76, lyric=syl_of(bt, 44))      # (61~64 낙사비는 리드 단독)
        for bt, d, p in move(between(hi, 21, 24), 44, 2):
            hhi.note(bt, d, p, 70, lyric=syl_of(bt, 44))
        # 2절의 줄 끝 1박 구멍에 휘슬이 대답한다 (1절은 비워 둬서 대비를 만든다).
        # 모티프의 하행 한숨 조각을 쓴다 — 새 선율이 아니라 같은 테마의 응답이어야 한다.
        for bar, figure in ((30, [(3.0, 0.5, 73), (3.5, 0.5, 71)]),
                            (32, [(3.0, 0.5, 75), (3.5, 0.5, 73)]),      # B7♭13 의 D#
                            (34, [(3.0, 0.25, 69), (3.25, 0.25, 71), (3.5, 0.5, 73)])):
            for beat, dur, pitch in figure:
                whi.note(b(bar, beat), dur, pitch, 74)

        # 브릿지 56마디: 모티프의 하행 한숨을 Gm6 위 단조로 (하프)
        for o, p in ((0.0, 74), (1.0, 70), (2.0, 69), (3.0, 67)):
            hrp.note(b(56, o), 1.0, p, 70)
        # 60마디: 마지막 코러스로 밀어넣는 모티프 머리(전조 조성)
        for o, d, p in ((3.0, 0.5, 71), (3.5, 0.5, 73)):
            whi.note(b(60, o), d, p, 96)
        # 아웃트로: 모티프 2회 (E major)
        put_motif(whi, 69, 94, transpose=2, expressive=True)
        put_motif(whi, 73, 84, transpose=2, expressive=True)
        # 스트링스: 브릿지 후반 · 마지막 코러스 · 아웃트로
        for beat, dur, name in PROG:
            bar = bar_of(beat)
            if 57 <= bar <= 60 or 65 <= bar <= 76:
                voic = CH[name][0]
                strs.chord(beat, dur, [voic[0] + 12, voic[2] + 12], 54 if bar >= 73 else 66)

    if topline:
        put_topline(whi, hrp, rho)
    return [voc, hlo, hhi, whi, rho, gtr, bas, hrp, strs, dr]


if __name__ == '__main__':
    # STEP 1 — 모티프만
    m = Track('Feather Motif', 73, 0)
    mr = Track('Rhodes', 4, 1)
    for rep in range(2):
        put_motif(m, 1 + rep * 4, 92)
        for i, name in enumerate(POST):
            mr.chord(b(1 + rep * 4 + i), 4.0, CH[name][0], 64)
    write_midi(os.path.join(OUT, '01_motif.mid'), [m, mr])

    # STEP 2 — 구조 골격
    write_midi(os.path.join(OUT, '02_structure.mid'),
               [t for t in make_tracks(with_melody=False) if t.ev])

    # STEP 3~6 — 전곡, 그리고 작업 흐름에 맞춘 분리본
    tracks = [t for t in make_tracks() if t.ev]
    write_midi(os.path.join(OUT, '03_full.mid'), tracks)
    write_midi(os.path.join(OUT, '04_vocals.mid'),          # -> Synthesizer V (Mai 2)
               [t for t in tracks if 'Vocal' in t.name])
    write_midi(os.path.join(OUT, '05_instruments.mid'),     # -> DAW (보컬판의 반주)
               [t for t in tracks if 'Vocal' not in t.name])
    write_midi(os.path.join(OUT, '06_instrumental.mid'),    # -> 인스트 (선율을 악기가 이어받는다)
               [t for t in make_tracks(topline=True) if t.ev and 'Vocal' not in t.name])

    total = 76 * 4 * 60 / BPM
    KEYS = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
    key = KEYS[(2 + TRANSPOSE) % 12]
    print(f'wrote 01~05.mid  —  76 bars, '
          f'{int(total // 60)}:{int(total % 60):02d} @ {BPM}BPM, '
          f'key {key} major (TRANSPOSE={TRANSPOSE:+d})')
