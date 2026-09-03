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

# 리비전 번호 — 산출물 파일명에 그대로 박힌다. REVISIONS.md 에 rev 를 추가할 때마다 올린다.
REV = 8

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
    'Percussion':                'Hi-hats, shaker, tambourine, ride - mixed separately from the kit',
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
        legato = bool(lyric) and lyric.endswith('~')      # 단어 안 음절 이음(레가토)
        if legato:
            lyric = lyric[:-1]
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
        end_tick = int(round((beat + dur) * PPQ))
        t1 = max(t0 + 1, end_tick) if legato else max(t0 + 20, end_tick - 8)
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
# (마디, 박) -> 새 음절. CHORUS_MEL 의 "Give me one white fea-ther and a rea-son to stay"
# 열두 음절 자리에 정확히 겹쳐 "This is my last white fea-ther and I'm here to stay" 를 얹는다.
FINAL_LINE3_OVERRIDE = {
    (4, 3.5): 'This', (5, 0.0): 'is', (5, 0.25): 'my', (5, 1.0): 'last',
    (5, 2.0): 'white', (5, 2.5): 'fea~', (6, 0.0): 'ther', (6, 0.5): 'and',
    (6, 0.75): "I'm", (6, 1.5): 'here', (6, 2.0): 'to', (6, 2.25): 'stay',
}

POST_TAG = [(12.0, 0.5, 69, 'moon~'), (12.5, 0.5, 71, 'gate'), (13.0, 0.5, 69, 'take'),
            (13.5, 0.5, 66, 'me'), (14.0, 2.0, 64, 'home')]

# ─────────────────────────────────────────────── STEP 3~5 · 멜로디
# (마디, 박, 길이, 음, 음절)
CHORUS_MEL = [
    # 당김은 '줄 안에서'만 쓴다(1->2, 3->4, 6->7). 줄이 끝나는 자리에는 숨을 남긴다.
    (1, 0.0, 0.5, 69, 'O~'), (1, 0.5, 0.25, 71, 'pen'), (1, 0.75, 0.25, 71, 'the'),
    (1, 1.0, 1.5, 74, 'moon~'), (1, 2.5, 0.5, 71, 'gate'),
    (1, 3.5, 0.5, 69, 'let'),                                     # 당김
    (2, 0.0, 0.25, 71, 'the'), (2, 0.25, 1.25, 69, 'eve~'),
    (2, 1.5, 0.5, 69, 'ning'), (2, 2.0, 1.5, 64, 'in'),           # -> 숨. 'eve'와 같은음(A7 근음)
    (3, 0.0, 0.5, 66, 'I'), (3, 0.5, 0.5, 66, 'have'),            # 제자리에서 새 줄
    (3, 1.0, 0.25, 69, 'fal~'), (3, 1.25, 0.25, 69, 'len'), (3, 1.5, 0.25, 69, 'a'),
    (3, 1.75, 0.75, 71, 'thou~'), (3, 2.5, 0.5, 69, 'sand'), (3, 3.0, 0.5, 66, 'times'),
    (3, 3.5, 0.5, 64, 'just'),                                    # 당김
    (4, 0.0, 0.25, 66, 'to'), (4, 0.25, 1.25, 69, 'land'), (4, 1.5, 0.5, 66, 'here'),
    (4, 2.0, 0.25, 64, 'a~'), (4, 2.25, 1.25, 62, 'gain'),
    (4, 3.5, 0.5, 67, 'Give'),                                    # 당김
    (5, 0.0, 0.25, 69, 'me'), (5, 0.25, 0.75, 71, 'one'), (5, 1.0, 1.0, 71, 'white'),
    (5, 2.0, 0.5, 69, 'fea~'), (5, 2.5, 1.0, 67, 'ther'),          # -> 숨
    (6, 0.0, 0.5, 69, 'and'), (6, 0.5, 0.25, 71, 'a'), (6, 0.75, 0.75, 71, 'rea~'),
    (6, 1.5, 0.5, 69, 'son'), (6, 2.0, 0.25, 67, 'to'), (6, 2.25, 1.25, 66, 'stay'),
    (6, 3.5, 0.5, 66, 'and'),                                     # 당김 — 킬 포인트로 밀어넣는다
    (7, 0.0, 0.5, 69, "I'll"), (7, 0.5, 0.5, 71, 'find'), (7, 1.0, 0.5, 69, 'you'),
    (7, 1.5, 0.25, 66, 'in'), (7, 1.75, 0.25, 67, 'the'),         # 16분으로 몰아치고
    (7, 2.0, 2.0, 74, 'mor~'),                                     # ★킬 포인트: 최고음 D5·2박·강박·5도 도약
    (8, 0.0, 0.5, 69, 'ning'), (8, 0.5, 0.25, 66, 'of'), (8, 0.75, 0.25, 64, 'a'),
    (8, 1.0, 1.0, 66, 'new'), (8, 2.0, 1.5, 62, 'day'),           # A7sus4 위 D = sus 여운
]

# 이 표의 키는 CHORUS_MEL 의 (마디, 박) 좌표다. 코러스 리듬을 손보면 조용히 어긋나
# 마지막 코러스의 가사 반전이 통째로 사라진다 — 빌드는 그대로 성공한다. 그래서 위험하다.
# rev05 에서 실제로 두 자리(5마디 0.5박, 6마디 2.5박)가 어긋났다. 빌드가 먼저 멈추게 못박는다.
_CH_SLOTS = {(bar, beat) for bar, beat, _d, _p, _s in CHORUS_MEL}
_orphan = sorted(set(FINAL_LINE3_OVERRIDE) - _CH_SLOTS)
assert not _orphan, f'FINAL_LINE3_OVERRIDE 가 CHORUS_MEL 에 없는 자리를 가리킨다: {_orphan}'

VERSE_MEL = [
    # 한 줄 = 2마디. 줄 안(홀수->짝수)에서만 당기고, 줄이 끝나는 짝수 마디 뒤에 1박 숨을 둔다.
    # 그 1박이 2절에서 휘슬이 대답할 자리가 된다.
    (1, 0.0, 0.5, 64, 'Rain'), (1, 0.5, 0.25, 67, 'on'), (1, 0.75, 0.25, 67, 'the'),
    (1, 1.0, 0.5, 69, 'lan~'), (1, 1.5, 0.5, 67, 'tern'), (1, 2.0, 1.0, 64, 'glass'),
    (1, 3.5, 0.5, 64, 'the'),                                     # 당김
    (2, 0.0, 0.5, 66, 'har~'), (2, 0.5, 0.5, 69, 'bor'), (2, 1.0, 0.25, 67, 'turn~'),
    (2, 1.25, 0.25, 66, 'ing'), (2, 1.5, 1.5, 64, 'gold'),        # -> 1박 숨
    (3, 0.0, 0.5, 66, 'a'), (3, 0.5, 0.25, 69, 'fid~'), (3, 0.75, 0.25, 69, 'dle'),
    (3, 1.0, 0.5, 67, 'in'), (3, 1.5, 0.25, 69, 'the'), (3, 1.75, 0.75, 71, 'mar~'),
    (3, 2.5, 1.0, 69, 'ket'),
    (3, 3.5, 0.5, 66, 'and'),                                     # 당김
    (4, 0.0, 0.5, 66, 'a'), (4, 0.5, 0.25, 67, 'sto~'), (4, 0.75, 0.25, 66, 'ry'),
    (4, 1.0, 0.5, 63, 'some~'), (4, 1.5, 0.5, 64, 'one'), (4, 2.0, 1.0, 66, 'told'),
    (5, 0.5, 0.5, 64, "I've"), (5, 1.0, 0.5, 67, 'worn'), (5, 1.5, 0.25, 67, 'a'),   # ★밀기
    (5, 1.75, 0.25, 69, 'hun~'), (5, 2.0, 0.5, 67, 'dred'), (5, 2.5, 1.0, 64, 'names'),
    (5, 3.5, 0.5, 64, 'but'),                                     # 당김
    (6, 0.0, 0.5, 66, 'I'), (6, 0.5, 0.5, 69, 'keep'), (6, 1.0, 0.25, 67, 'this'),
    (6, 1.25, 0.25, 66, 'one'), (6, 1.5, 0.25, 64, 'for'), (6, 1.75, 1.25, 62, 'you'),
    (7, 0.0, 0.5, 66, 'the'), (7, 0.5, 0.5, 69, 'one'), (7, 1.0, 0.25, 69, 'you'),
    (7, 1.25, 0.75, 71, 'called'), (7, 2.0, 0.25, 69, 'me'), (7, 2.25, 0.75, 66, 'soft~'),
    (7, 3.0, 0.5, 64, 'ly'),
    (7, 3.5, 0.5, 63, 'when'),                                    # 당김
    (8, 0.0, 0.25, 64, 'the'), (8, 0.25, 0.75, 66, 'sum~'), (8, 1.0, 0.25, 64, 'mer'),
    (8, 1.25, 0.25, 63, 'was'), (8, 1.5, 0.5, 64, 'still'), (8, 2.0, 1.5, 66, 'new'),
]

VERSE2_WORDS = ['Smoke', 'from', 'the', 'ket~', 'tle', 'rings',
                'the', 'mar~', 'ket', 'clos~', 'ing', 'down',
                'we', 'camp', 'be~', 'side', 'the', 'wa~', 'ter',
                'and', 'the', 'fire', 'keeps', 'the', 'cold', 'out',
                'The', 'world', 'can', 'end', 'Sun~', 'day',
                'and', 'o~', 'pen', 'up', 'on', 'Mon~', 'day',
                'and', "I'll", 'still', 'be', 'stand~', 'ing', 'here',
                'grow~', 'ing', 'old~', 'er', 'hold~', 'ing', 'on']

PRE_MEL = [
    # 영어 강세 규칙으로 리듬을 짠다: 약음절(관사·전치사·접미)은 16분으로 흘리고,
    # 뒤따르는 내용어는 박에 얹거나 16분 먼저 당겨 짚는다(anticipation).
    # 프레이즈의 시작/끝 시각은 그대로 둔다 — 숨자리와 화성 정렬이 그대로 살아 있어야 한다.
    (1, 0.0, 0.5, 66, 'Ev~'), (1, 0.5, 0.25, 66, "'ry"), (1, 0.75, 0.75, 67, 'end~'),
    (1, 1.5, 0.5, 66, 'ing'), (1, 2.0, 0.25, 64, 'is'), (1, 2.25, 0.25, 66, 'a'),
    (1, 2.5, 1.5, 62, 'door'),
    (2, 0.0, 0.5, 64, "I've"), (2, 0.5, 0.5, 64, 'walked'), (2, 1.0, 0.25, 67, 'be~'),
    (2, 1.25, 1.75, 66, 'fore'), (2, 3.0, 0.5, 70, '(ooh'), (2, 3.5, 0.5, 71, 'ooh)'),
    # 3마디 천장을 A4 로 낮춘다 — 프리가 코러스와 같은 높이면 코러스가 올라간 느낌이 나지 않는다
    # 1마디와 같은 자리의 대구라 리듬도 같은 꼴로 맞춘다.
    (3, 0.0, 0.5, 67, 'count'), (3, 0.5, 0.25, 67, 'the'), (3, 0.75, 0.75, 69, 'fea~'),
    (3, 1.5, 0.5, 67, 'thers'), (3, 2.0, 0.25, 69, 'on'), (3, 2.25, 0.25, 67, 'the'),
    (3, 2.5, 1.5, 66, 'floor'),
    # 4마디는 일부러 8분 정박으로 남긴다 — "one two and we be-gin" 은 카운트인이고,
    # 그 네 음절 사이에 드럼 필이 맞물리게 짜여 있다(interplay [다]).
    (4, 0.0, 1.0, 67, 'one'), (4, 1.0, 1.0, 69, 'two'), (4, 2.0, 0.5, 69, 'and'),
    (4, 2.5, 0.5, 71, 'we'), (4, 3.0, 0.5, 69, 'be~'), (4, 3.5, 0.5, 71, 'gin'),
]

BRIDGE_MEL = [
    # 브릿지는 Downer 모드의 독백이다 — 말하듯 붙는 약음절이 가장 잘 사는 자리라
    # 16분 배치를 여기서 제일 촘촘하게 쓴다. 대구가 되는 3·4마디는 같은 꼴로 맞춘다.
    (1, 0.0, 0.25, 62, 'If'), (1, 0.25, 0.75, 66, 'this'), (1, 1.0, 0.25, 66, 'is'),
    (1, 1.25, 0.25, 66, 'the'), (1, 1.5, 1.5, 69, 'last'), (1, 3.0, 1.0, 66, 'life'),
    (2, 0.0, 0.5, 67, 'tell'), (2, 0.5, 0.25, 69, 'me'), (2, 0.75, 2.25, 71, 'now'),
    (3, 0.0, 0.25, 66, "I'll"), (3, 0.25, 0.75, 69, 'spend'), (3, 1.0, 0.25, 69, 'it'),
    (3, 1.25, 1.25, 71, 'slow~'), (3, 2.5, 0.5, 69, 'er'),
    (4, 0.0, 0.25, 67, "I'll"), (4, 0.25, 0.75, 70, 'spend'),      # ★Bb4 = Gm6 의 눈물
    (4, 1.0, 0.25, 69, 'it'), (4, 1.25, 1.75, 67, 'loud'),      # 마침표 자리 — 쉼을 1박으로 벌린다
    (5, 0.0, 1.0, 64, 'No'), (5, 1.0, 0.5, 66, 'more'), (5, 1.5, 0.5, 67, 'wait~'),
    (5, 2.0, 0.25, 66, 'ing'), (5, 2.25, 0.25, 64, 'for'), (5, 2.5, 0.25, 64, 'the'),
    (5, 2.75, 0.75, 67, 'sky'),          # 반 박 숨을 남겨 "…the sky / to fall" 을 가른다
    (6, 0.0, 0.25, 69, 'to'), (6, 0.25, 1.75, 71, 'fall'),
    (7, 0.0, 0.5, 69, 'you'), (7, 0.5, 0.25, 71, 'were'), (7, 0.75, 0.25, 69, 'the'),
    (7, 1.0, 0.5, 71, 'rea~'), (7, 1.5, 0.25, 69, 'son'), (7, 1.75, 0.25, 67, 'I'),
    (7, 2.0, 0.5, 66, 'came'), (7, 2.5, 1.5, 64, 'back'),
    (8, 0.0, 0.25, 66, 'at'), (8, 0.25, 1.25, 69, 'all'),
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


# 69~72 는 전조 직후 풀밴드로 선언하고, 73마디부터 실제로 줄어든다.
# 이전엔 문서(README STEP 2)에만 "73마디부터 페이드"라고 적혀 있고 코드엔 구현이 없어서,
# 드럼·베이스·로즈가 76마디까지 풀파워로 가는 바람에 outro 가 곡에서 가장 시끄러운
# 섹션이 되는 사고가 났다(render.py 섹션별 RMS 리포트로 발견).
OUTRO_FADE = {73: 0.78, 74: 0.58, 75: 0.40, 76: 0.22}


# ── 베이스: 셀을 무작위로 순환시키지 않고 "역할"로 고정 배정한다 ─────────────
# POCKET = 벌스. 킥과 정확히 맞물려 자리를 지킨다(보컬이 앞에 있으니 그루브는 뒤에서).
# LIFT   = 프리코러스. 계단식 상행으로 코러스로 밀어 올린다.
# DRIVE  = 코러스·포스트·마지막 후반. 당김+옥타브 도약으로 에너지를 낸다.
# 각 역할 안에서도 4마디 프레이즈의 홀수/짝수 마디를 다르게 써서(앵커/변주) 완전 반복을 피한다.
BASS_ROLE = {
    'POCKET': [
        [(0.0, 0.75, 0), (1.5, 0.5, 0), (2.5, 0.5, 0), (3.0, 0.5, 7)],          # 앵커: 근음 중심
        [(0.0, 0.75, 0), (1.5, 0.25, 0), (1.75, 0.25, 2), (2.5, 0.5, 0), (3.0, 0.5, 7)],  # 변주: 경과음 하나
    ],
    'LIFT': [
        [(0.0, 0.75, 0), (1.0, 0.5, 2), (1.75, 0.5, 4), (2.5, 0.5, 5), (3.0, 0.5, 7)],    # 1-2-3-4-5 상행
        [(0.0, 0.75, 0), (1.0, 0.5, 4), (1.75, 0.5, 7), (2.5, 0.75, 0), (3.5, 0.5, 9)],   # 도약 상행
    ],
    'DRIVE': [
        [(0.0, 0.5, 0), (0.75, 0.25, 0), (1.0, 0.5, 7), (1.75, 0.5, 0), (2.5, 0.5, 0), (3.0, 0.25, 0), (3.5, 0.5, 12)],  # 당김+옥타브
        [(0.0, 0.75, 0), (1.0, 0.5, 0), (1.75, 0.25, 7), (2.0, 0.25, 9), (2.5, 0.5, 12), (3.0, 0.5, 7), (3.5, 0.5, 0)],  # 옥타브 경유 하행
    ],
}


def bass_role(beat):
    if in_(beat, 'verse1', 'verse2'):
        return 'POCKET'
    if in_(beat, 'pre1', 'pre2'):
        return 'LIFT'
    return 'DRIVE'


# 로즈는 CH 보이싱을 한 옥타브 내려 친다 — 보컬 음역(D4~E5)의 80%를 가리고 있었고,
# 음절 시작과 같은 순간을 78% 확률로 때리고 있었다(interplay.py [라]).
# CH 표 자체는 건드리지 않는다: 거기 성부 진행 설계(평균 3.8반음)가 들어 있다.
RH_DROP = 12


def _bass_octaves():
    """베이스 근음의 옥타브를 성부 진행으로 고른다.

    CH 표의 근음은 전부 E2~C#3(40~49)인데, 그 위에 셀이 +7·+12 를 얹으니 선율이
    C3~B3(131~247Hz)에서 33% 를 보냈다 — 베이스가 아니라 중역 악기 소리가 난다.
    옥타브를 통째로 내리면 코드마다 10반음씩 튀어 선율이 어지러워지므로,
    코드마다 root/root±12 중 **직전 음에서 가장 가까운 것**을 고른다.
    화음 보이싱에 쓴 것과 같은 원리다(거기선 평균 3.8반음).

    하한 E1(28)=41.2Hz — 기획서의 "40Hz 이하 없다".
    상한 A2(45)=110Hz — 셀이 +12 까지 얹으므로 여기가 근음의 천장이다.
    """
    LO, HI = 28, 45
    out, prev = {}, None
    for i, (_beat, _dur, name) in enumerate(PROG):
        r = CH[name][1]
        cands = [p for p in (r - 24, r - 12, r, r + 12) if LO <= p <= HI] or [r]
        out[i] = min(cands) if prev is None else min(cands, key=lambda p: (abs(p - prev), p))
        prev = out[i]
    return out


def build_chords(rhodes, gtr, bass):
    bass_root = _bass_octaves()
    for i, (beat, dur, name) in enumerate(PROG):
        voic_hi, root = CH[name]
        voic = [p - RH_DROP for p in voic_hi]
        bar = bar_of(beat)
        quiet = in_(beat, 'intro') or (61 <= bar <= 64) or (53 <= bar <= 56)
        fade = OUTRO_FADE.get(bar, 1.0)
        dense = in_(beat, 'chorus1', 'chorus2') or 65 <= bar <= 68

        # 로즈: 코드 보이싱은 항상 유지. 코러스에서는 배음 복제 대신 옥타브 아래 페달을 더해
        # "두껍다"가 아니라 "낮은 대역에 화성이 실려 있다"가 되게 한다.
        rhodes.cc(beat, 64, 127)                             # 코드마다 페달 밟고
        rhodes.cc(beat + dur - 0.08, 64, 0)                  # 다음 코드 직전에 뗀다
        rhodes.chord(beat, dur, voic, int((58 if quiet else 68) * fade))
        if not quiet and dur >= 4.0:
            # 컴프 자리는 보컬 음절이 '시작하지 않는' 칸으로 옮긴다. 16분 격자별 보컬 온셋을
            # 세어 보면 0.0~2.5 에 몰려 있고 3.0 은 벌스 2개·코러스 2개로 사실상 비어 있다
            # (같은 자리가 [가] 콜앤리스폰스 리포트의 공백 목록과 일치한다: 6·8·10·30·32·34마디
            #  3.00박). 종전 2.5 는 벌스 6개·코러스 10개를 정면으로 때리고 있었다.
            # 1.75 는 3박을 당겨 짚는 자리 — 온셋이 거의 없고, 네오소울 컴핑의 정석이다.
            # 2.5 는 코러스에서 음절 온셋 10개와 맞부딪혀 60% 가 겹쳤다(위치별로 세어 확인).
            # 2.25 는 같은 구간에서 사실상 비어 있다. 1.75 -> 2.25 -> 3.0 은 당겨 짚는
            # 싱코페이션 컴프가 되고, 셋 다 음절이 시작하지 않는 칸이다.
            comp = [(3.0, 1.0)] if not dense else [(1.75, 0.5), (2.25, 0.5), (3.0, 1.0)]
            for o, d in comp:
                rhodes.chord(beat + o, d, voic, int((58 if o != 2.25 else 52) * fade))
            if dense:                                            # 페달: root 는 베이스 저음역(28~49)이라 옥타브 올려야 로즈 하한(E2=40) 안이다
                rhodes.note(beat, dur, root + 12, int(38 * fade))
        # (rev03 초반에 넣었던 '로즈 왼손'은 제거했다 — 보이싱 자체를 한 옥타브 내리면서
        #  그 자리를 오른손이 직접 맡게 됐다. 두 개를 다 두면 중역이 뭉친다)

        # 기타 — Rhodes 와 다른 음(shell 보이싱, 한 옥타브 위)으로 대역을 겹치지 않는다.
        # 코드톤 커팅은 킥과 같은 자리를 치지 않고, 척은 업비트(킥이 없는 자리)에서 맞물린다.
        # ★프리코러스에 기타가 아예 없었다. 실제 렌더 스펙트럼에서 프리의 프레즌스(2~4kHz)가
        # 벌스보다 7dB 어두웠다 — 코러스로 밀어 올리는 구간이 오히려 어두워지고 있었다.
        # 프레즌스의 55%를 기타가 담당하는데 그게 통째로 빠져 있었던 게 원인이다.
        # 벌스와 같은 패턴이면 밋밋하니 16분 한 칸을 더 얹어 벌스보다 촘촘하게 — 그게 빌드다.
        pre = in_(beat, 'pre1', 'pre2')
        if pre or in_(beat, 'verse1', 'verse2', 'chorus1', 'chorus2', 'post1', 'post2') or 65 <= bar <= 68:
            # +12 클램프는 로즈의 상단(최대 F#5=78)과 정확히 겹치는 자리로 되돌아가는 문제가 있었다
            # (검증하다 발견 — 65~68마디에서 다섯 음이 그대로 겹쳤다). 대신 각 음을 목표 음역
            # 근처의 옥타브로 재배치해, 로즈가 어디에 있든 기타는 항상 그 위에 뜨게 한다.
            # voic[-1]은 정의상 그 코드의 최고음이라, "로즈보다 위"를 요구하면 옥타브를
            # 통째로 넘어야 했고 그러면 기타 상한(C6)을 넘었다(검증하다 발견). 위 확장음 대신
            # 코드의 낮은 두 음(근음+3도)을 옥타브 올린다 — 재즈/펑크 기타의 실제 "셸 보이싱"과
            # 같은 선택이고, 로즈의 최고음역과 구조적으로 겹칠 이유가 없어진다.
            shell = [voic[0] + 12 + RH_DROP, voic[1] + 12 + RH_DROP]
            if max(shell) > 83:                                   # 그래도 넘으면 페어 전체를 내린다
                shell = [p - 12 for p in shell]
            # (기타를 벌스에서 한 옥타브 내려 중역을 채우려 했으나, rev02 에서 해결한
            #  로즈와의 겹침이 100% 로 되살아났다 — 중역은 로즈 왼손이 맡는다)
            # k%4 는 0~3 만 나오므로 이전 버전의 (1,2,5,6) 은 5·6이 죽은 코드였다 —
            # 코러스 밀집 패턴이 실제로는 sparse 와 똑같이 나가고 있었다.
            # 절대 k 로 박2·박4 직전 16분에 당김을 얹는다 — 킥(박1~4)·하이햇(8분) 과
            # 겹치지 않는 자리(3, 11)라 새 리듬 정보가 실제로 추가된다.
            for k in range(int(dur * 4)):
                if k % 4 in (1, 2) or (pre and k % 4 == 3) or (dense and k in (3, 11)):
                    gtr.chord(beat + k * 0.25, 0.2, shell, 44 if k % 2 else 52)
            if dense:                                            # 척 = 8분 업비트(0.5·1.5·2.5·3.5) — 킥과 안 겹친다
                chuck_p = max(voic[0] - 12, 52)
                for o in (0.5, 1.5, 2.5, 3.5):
                    gtr.note(beat + o, 0.1, chuck_p, 34)

        # 베이스
        if quiet and not (53 <= bar <= 56):
            continue
        if 53 <= bar <= 56:
            bass.note(beat, dur, bass_root[i], 74)
            continue
        if bar == 76:              # 마지막 마디는 베이스를 빼고 로즈·휘슬·스트링스만 남긴다
            continue
        root = bass_root[i]              # 여기서부터는 옥타브가 정리된 근음
        role = bass_role(beat)
        variant = (bar - 1) % 2                                # 홀수 마디=앵커, 짝수 마디=변주
        cell = BASS_ROLE[role][variant % len(BASS_ROLE[role])]
        for j, (o, d, semi) in enumerate(cell):
            if o < dur:
                vel = 88 if o == 0.0 else (78 if semi == 0 else 84)
                p_ = root + semi
                # 마디 첫 박만 옥타브 아래로 — 저역(<D2)이 전 구간 0% 였다.
                # E1(28)=41.2Hz 를 하한으로 고정: 기획서의 "40Hz 이하 없다"를 지킨다.
                # 코러스(DRIVE)는 매 마디, 벌스(POCKET)는 앵커 마디(홀수)만 내린다 —
                # 벌스가 매 마디 저역을 깔면 카페 볼륨에서 둔해지고 코러스와의 낙차도 없어진다.
                # 짝수 마디는 그대로 둬 벌스 안에서도 무게가 두 마디 주기로 오르내린다.
                drop = role == 'DRIVE' or (role == 'POCKET' and variant == 0)
                if drop and o == 0.0 and p_ - 12 >= 28:
                    p_ -= 12
                bass.note(beat + o, d, p_, int(vel * fade))
        # 다음 코드로의 접근음 — 이 코드 마지막 8분을 다음 근음의 반음/온음 아래에서 접근시킨다
        nxt_root = None
        ni = i + 1
        if ni < len(PROG):
            nb, _nd, nname = PROG[ni]
            if bar_of(nb) == bar_of(beat + dur - 0.5) and not (nb - (beat + dur) > 0.01):
                nxt_root = bass_root[ni]
        if nxt_root is not None and dur >= 2.0:
            appr = nxt_root - 1 if nxt_root - 1 not in (root,) else nxt_root - 2
            bass.note(beat + dur - 0.5, 0.5, appr, int(72 * fade))


# 고스트 스네어 위치 — 마디 짝/홀로 갈아 끼워 두 마디가 같아지지 않게 한다
GHOST_VERSE = {0: (0.75, 2.25), 1: (1.75, 3.25)}
GHOST_FOUR = {0: (1.75, 3.75), 1: (0.75, 2.25)}

# 필 — 자리마다 성격이 다르다. (offset, dur, GM노트, velocity)
# 36=킥 37=림 38=스네어 42=클로즈햇 45=로우탐 46=오픈햇 47=미드탐 49=크래시 70=셰이커
FILLS = {
    12: [(3.0, 0.25, 38, 84), (3.25, 0.25, 38, 70), (3.5, 0.5, 47, 96)],       # 프리 진입: 스네어 굴림
    # ★16·40마디 가사는 "one two and we be-gin" — 박2.0/2.5/3.0/3.5 에 음절이 하나씩 박힌
    # 카운트인이다. rev03 까지 필이 그 네 음절 중 셋을 정확히 같은 순간에 때려 덮고 있었다.
    # 16분 뒷칸(2.25·2.75·3.25·3.75)으로 옮기면 음절 사이에 정확히 끼어 서로 맞물린다 —
    # 드러머가 보컬의 카운트에 대답하는 모양이 된다.
    16: [(2.25, 0.25, 47, 84), (2.75, 0.25, 47, 92), (3.25, 0.25, 45, 100),
         (3.75, 0.25, 38, 112)],                                                # 코러스 진입: 탐->스네어
    24: [(3.5, 0.5, 38, 104)],                                                  # 포스트 진입: 한 방만
    28: [(3.0, 0.5, 46, 88), (3.5, 0.5, 38, 100)],                              # 2절 진입: 오픈햇+스네어
    36: [(3.25, 0.25, 38, 76), (3.5, 0.25, 38, 90), (3.75, 0.25, 38, 104)],     # 프리2: 16분 밀어넣기
    40: [(2.25, 0.25, 47, 86), (2.75, 0.25, 45, 96), (3.25, 0.25, 38, 106),
         (3.75, 0.25, 45, 112)],                                                # 코러스2: 가장 큰 필
    48: [(3.5, 0.5, 38, 104)],
    4:  [(3.0, 0.25, 38, 44), (3.5, 0.25, 38, 58), (3.75, 0.25, 38, 70)],       # ★첫 보컬 진입: 여린 스네어 픽업 (rev02 까지 아무 준비도 없었다)
    52: [(3.0, 1.0, 46, 84)],                                                   # 브릿지 진입: 오픈햇을 길게 — 비우며 들어간다
    60: [],                                                                     # G.P. 는 아래에서 따로
    68: [(2.5, 0.25, 47, 92), (2.75, 0.25, 47, 98), (3.0, 0.25, 45, 104),
         (3.25, 0.25, 45, 108), (3.5, 0.5, 38, 116)],                           # 아웃트로 진입: 최대 필
}


def build_drums(dr, pc):
    # ★SHK 는 rev06 까지 70 이었는데 GM 70 은 셰이커가 아니라 마라카스다(82가 셰이커).
    #  이름만 SHK 였지 실제로는 마라카스를 치고 있었고, 마라카스가 더 어둡다.
    K, S, RIM, HH, OH, SHK, CR, T1, T2 = 36, 38, 37, 42, 46, 82, 49, 47, 45
    TAMB, RIDE = 54, 51                    # 탬버린·라이드 — 비어 있던 고역을 맡는다
    for bar in range(1, 77):
        beat = b(bar)
        fade = OUTRO_FADE.get(bar, 1.0)
        style = ('none' if bar <= 4 or 53 <= bar <= 56 or 61 <= bar <= 64 or bar >= 75 else
                 'verse' if in_(beat, 'verse1', 'verse2', 'pre1', 'pre2') else
                 'build' if 57 <= bar <= 60 else
                 'outro-thin' if bar in (73, 74) else 'four')
        ph = (bar - 1) % 4                       # 프레이즈 안 위치 (0~3)
        if style == 'none':
            # 조용한 구간도 마디마다 같으면 안 된다(인트로가 100% 복붙이었다).
            # 셰이커 격자를 8분/16분으로 번갈아 쓰고, 2마디마다 림을 하나 얹는다.
            if ph % 2 == 0:
                for k in range(8):
                    pc.note(beat + k * 0.5, 0.2, SHK, int((40 if k % 2 else 54) * fade))
            else:
                for k in range(16):
                    if k % 4 != 2:                       # 16분에서 한 칸씩 빼 숨을 만든다
                        pc.note(beat + k * 0.25, 0.12, SHK, int((34 if k % 2 else 48) * fade))
            if ph == 3:
                dr.note(beat + 3.5, 0.2, RIM, int(52 * fade))
            for o, d, note_, v in FILLS.get(bar, []):        # 조용한 구간도 진입 준비는 한다
                dr.note(beat + o, d, note_, int(v * fade))
            continue
        if style == 'outro-thin':                      # 73~74마디: 풀킷 -> 킥+셰이커만
            for o in (0.0, 2.0):
                dr.note(beat + o, 0.3, K, int(100 * fade))
            for k in range(8):
                pc.note(beat + k * 0.5, 0.2, SHK, int((42 if k % 2 else 56) * fade))
            continue
        # (ph = 프레이즈 안 위치. 실제 드러머는 4마디 단위로 호흡한다 —
        #  rev02 까지 이 개념이 없어서 드럼 자기유사도가 92% 였다)
        if style == 'verse':
            kicks = [0.0, 1.5, 2.5] if ph != 3 else [0.0, 1.5, 2.75, 3.5]
            for o in kicks:
                dr.note(beat + o, 0.3, K, 100)
            for o in (1.0, 3.0):
                dr.note(beat + o, 0.3, RIM, 92)
            for k in range(8):                    # 하이햇: 2마디 단위로 악센트 자리를 옮긴다
                acc = (k == 0 or k == 4) if ph % 2 == 0 else (k == 2 or k == 6)
                pc.note(beat + k * 0.5, 0.2, HH, 66 if acc else (44 if k % 2 else 56))
            for o in GHOST_VERSE[ph % 2]:         # 고스트 스네어 — 포켓은 여기서 나온다
                dr.note(beat + o, 0.1, S, 26)
        elif style == 'build':
            for o in (0.0, 1.0, 2.0, 3.0):
                dr.note(beat + o, 0.3, K, 104)
            for o in (1.0, 3.0):
                dr.note(beat + o, 0.3, S, 98)
            for k in range(16):
                pc.note(beat + k * 0.25, 0.15, HH, 42 + (18 if k % 4 == 0 else 0))
            for o in (0.75, 2.75):
                dr.note(beat + o, 0.1, S, 30)
        else:
            kicks = [0.0, 1.0, 2.0, 3.0] if ph != 3 else [0.0, 1.0, 1.75, 2.0, 3.0]
            for o in kicks:
                dr.note(beat + o, 0.3, K, 106)
            for o in (1.0, 3.0):
                dr.note(beat + o, 0.3, S, 100)
            # 오픈 하이햇은 매 업비트가 아니라 2마디 구절 끝에만 — rev02 는 마디당 4번이라 씻겨나갔다
            for k in range(8):
                is_open = (k == 7 and ph % 2 == 1)
                pc.note(beat + k * 0.5, 0.25, OH if is_open else HH,
                        62 if is_open else (48 if k % 2 else 72))
            for k in range(4):
                pc.note(beat + 0.25 + k, 0.15, SHK, 46)
            for o in GHOST_FOUR[ph % 2]:
                dr.note(beat + o, 0.1, S, 28)
        # 섹션 진입 크래시
        if bar in (17, 25, 41, 49, 61, 65, 69):
            dr.note(beat, 0.5, CR, 108)
        # 필 — 자리마다 다른 걸 친다 (rev02 까지는 전부 톰 2방으로 같았다)
        if bar in FILLS:
            for o, d, note_, v in FILLS[bar]:
                dr.note(beat + o, d, note_, v)
        if bar in (16, 40):                             # 코러스 직전: 필 앞을 비워 낙차를 만든다
            # 필이 2.25 로 당겨졌으니 비우는 창도 그만큼 앞으로 옮긴다(안 옮기면 필 첫 타가 지워진다)
            lo_, hi_ = b(bar, 1.5) * PPQ, b(bar, 2.25) * PPQ
            dr.ev = [e for e in dr.ev if not (lo_ <= e[0] < hi_)]
        # 고역 퍼커션 — 실제 렌더 스펙트럼에서 8~16kHz 가 전 구간 비어 있었다(어느 악기도
        # 0~1.4%). 탬버린과 라이드가 그 대역을 맡는다. 편성이 두꺼운 자리에만 넣어
        # 벌스의 성김은 그대로 둔다.
        if style == 'four':
            for o in (0.5, 1.5, 2.5, 3.5):              # 8분 업비트 — 킥·스네어 사이를 메운다
                pc.note(beat + o, 0.2, TAMB, int((54 if o in (1.5, 3.5) else 46) * fade))
        if 65 <= bar <= 72:                             # 마지막 코러스 후반~아웃트로: 라이드로 광채
            for k in range(8):
                pc.note(beat + k * 0.5, 0.3, RIDE, int((58 if k % 2 == 0 else 44) * fade))

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
    strs = Track('Strings', 49, 6)      # 48(String Ensemble 1)보다 어택이 느리고 따뜻하다
    dr = Track('Drums', None, 9)
    # 퍼커션을 킷에서 분리한다. 한 트랙이면 믹스에서 킥과 하이햇의 비율을 못 만진다 —
    # 실제 렌더 스펙트럼을 재 보니 드럼 에너지의 78%가 250Hz 아래였고(킥이 다 먹었다)
    # 에어 대역(8~16kHz)은 0.6% 였다. 갈라 놔야 위쪽만 따로 올릴 수 있다.
    pc = Track('Percussion', None, 9)

    if with_rhythm:
        build_chords(rho, gtr, bas)
        build_drums(dr, pc)

    if with_melody:
        # 인트로: 휘슬 모티프 + 하프 아르페지오
        put_motif(whi, 1, 84, expressive=True)
        for beat, dur, name in PROG:
            if in_(beat, 'intro', 'bridge') or 69 <= bar_of(beat) <= 76:
                voic = CH[name][0]
                for k in range(8):
                    hrp.note(beat + k * 0.5, 0.5, voic[k % 4] + (12 if k >= 4 else 0), 52)

        # 프리코러스 하프 — 이 구간은 D5 위가 통째로 비어 있었다(대역 점유 고역 0%).
        # 프리는 코러스로 밀어 올리는 자리인데 공기감이 없으면 코러스가 열리는 느낌이 안 난다.
        # 보컬(D4~A4)보다 한 옥타브 위에 두어 음역으로 갈라 놓기 때문에 마스킹이 아니라
        # 그 위에 얹히는 층이 된다. 온셋은 전부 0.0박을 피한다 — 보컬이 프리 온셋의 42%를
        # 마디 첫 박에서 시작하기 때문이다. 마디마다 음수를 4->6->8->8 로 늘려 빌드를 만든다.
        # 휘슬이 아니라 하프인 이유: 휘슬은 시그니처 모티프라 프리에서 미리 쓰면 코러스에서
        # 다시 등장할 때의 무게가 깎인다. 하프는 브릿지에만 있던 색이라 집을 하나 더 얻는다.
        PRE_ARP = [((0.5, 1.5, 2.5, 3.5), 52),
                   ((0.5, 1.0, 1.5, 2.5, 3.0, 3.5), 58),
                   ((0.25, 0.75, 1.25, 1.75, 2.25, 2.75, 3.25, 3.75), 64),
                   ((0.25, 0.75, 1.25, 1.75, 2.25, 2.75, 3.25, 3.75), 70)]
        for beat, dur, name in PROG:
            if not in_(beat, 'pre1', 'pre2'):
                continue
            bar = bar_of(beat)
            offs, vel = PRE_ARP[(bar - 13) % 4 if bar < 20 else (bar - 37) % 4]
            voic = CH[name][0]
            pool = sorted({p for p in [q + 12 for q in voic] + [voic[0] + 24, voic[1] + 24]
                           if p <= 93})                      # 하프 상한(C7=96) 안
            step = 0
            for o in offs:
                if o < beat + dur - beat:                    # 코드가 반 마디짜리면 그 안에서만
                    hrp.note(beat + o, 0.45, pool[step % len(pool)], vel)
                    step += 1

        # 벌스 / 프리 / 코러스 / 포스트 / 브릿지 / 마지막 코러스
        put_mel(voc, VERSE_MEL, 5)
        put_mel(voc, PRE_MEL, 13)
        put_mel(voc, CHORUS_MEL, 17)
        put_mel(voc, VERSE_MEL, 29, words=VERSE2_WORDS)      # 2절은 가사가 다르다
        put_mel(voc, PRE_MEL, 37)
        put_mel(voc, CHORUS_MEL, 41)
        put_mel(voc, BRIDGE_MEL, 53)
        final_words = [FINAL_LINE3_OVERRIDE.get((bar, beat), syl)
                       for bar, beat, _d, _p, syl in CHORUS_MEL]
        put_mel(voc, CHORUS_MEL, 61, transpose=2, words=final_words)   # ★전조 +2도 + 가사 반전
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

        ch_syl_final = dict(ch_syl)
        for (bar, beat), new_syl in FINAL_LINE3_OVERRIDE.items():
            ch_syl_final[round(b(17 + bar - 1, beat), 3)] = new_syl

        def syl_of(bt, shift_bars):
            table = ch_syl_final if shift_bars == 44 else ch_syl
            return table.get(round(bt - shift_bars * 4, 3), 'ah')
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
        # 스트링스: 코러스(옅게, 고음역 화성 보강) · 브릿지 후반 · 마지막 코러스 · 아웃트로
        # 정적인 2음 화음 대신 두 성부로 나눈다 — 위는 지속, 아래는 코드 중간에 온음계
        # 이웃음으로 한 번 움직인다(작은 서스펜션). 매 코드가 똑같은 딱딱한 화음으로 안 들리게.
        for beat, dur, name in PROG:
            bar = bar_of(beat)
            in_chorus = in_(beat, 'chorus1', 'chorus2')
            if bar <= 4:
                # 인트로에 아주 옅은 스트링 베드. 휘슬·하프·로즈 셋 다 감쇠가 빠른 악기라
                # 지속음이 하나도 없었고, 그래서 세 소리가 서로 붙지 않고 따로 놀았다.
                # 전면에 나오면 안 되므로 코러스(vel 40)보다도 훨씬 아래에 둔다.
                voic = CH[name][0]
                strs.note(beat, dur, voic[2], 30)
                strs.note(beat, dur, voic[0], 26)
                continue
            if in_chorus or 57 <= bar <= 60 or 65 <= bar <= 76:
                voic = CH[name][0]
                vel = 40 if in_chorus else (54 if bar >= 73 else 66)  # 코러스는 리드 아래로 옅게
                top = voic[2] + 12
                strs.note(beat, dur, top, vel)                        # 지속 성부
                low0 = voic[0] + 12
                if dur >= 2.0:
                    half = dur / 2
                    low1 = low0 + 2                      # 온음 위 — 전조 구간(E장조)도 있어 스케일 의존 안 함
                    strs.note(beat, half, low0, max(1, vel - 6))
                    strs.note(beat + half, dur - half, low1, max(1, vel - 6))
                else:
                    strs.note(beat, dur, low0, max(1, vel - 6))

    if topline:
        put_topline(whi, hrp, rho)
    return [voc, hlo, hhi, whi, rho, gtr, bas, hrp, strs, dr, pc]


if __name__ == '__main__':
    # STEP 1 — 모티프만
    m = Track('Feather Motif', 73, 0)
    mr = Track('Rhodes', 4, 1)
    for rep in range(2):
        put_motif(m, 1 + rep * 4, 92)
        for i, name in enumerate(POST):
            mr.chord(b(1 + rep * 4 + i), 4.0, CH[name][0], 64)
    R = f'_rev{REV:02d}'
    write_midi(os.path.join(OUT, f'01_motif{R}.mid'), [m, mr])

    # STEP 2 — 구조 골격
    write_midi(os.path.join(OUT, f'02_structure{R}.mid'),
               [t for t in make_tracks(with_melody=False) if t.ev])

    # STEP 3~6 — 전곡, 그리고 작업 흐름에 맞춘 분리본
    tracks = [t for t in make_tracks() if t.ev]
    write_midi(os.path.join(OUT, f'03_full{R}.mid'), tracks)
    write_midi(os.path.join(OUT, f'04_vocals{R}.mid'),          # -> Synthesizer V (Mai 2)
               [t for t in tracks if 'Vocal' in t.name])
    write_midi(os.path.join(OUT, f'05_instruments{R}.mid'),     # -> DAW (보컬판의 반주)
               [t for t in tracks if 'Vocal' not in t.name])
    write_midi(os.path.join(OUT, f'06_instrumental{R}.mid'),    # -> 인스트 (선율을 악기가 이어받는다)
               [t for t in make_tracks(topline=True) if t.ev and 'Vocal' not in t.name])

    # STEP 7 — DAW 용 트랙별 스템. Logic/GarageBand 에 파일 하나씩 끌어다 놓으면
    # 그 트랙에만 그 악기가 들어간다. 합본(03)을 통째로 임포트하면 DAW 가 채널을
    # 제 방식대로 배치해 버려 악기 배정을 다시 해야 하는 경우가 많다.
    stem_dir = os.path.join(OUT, f'stems{R}')
    os.makedirs(stem_dir, exist_ok=True)
    for i, t in enumerate(tracks, 1):
        safe = t.name.replace(' ', '_').replace('(', '').replace(')', '').replace('/', '-')
        write_midi(os.path.join(stem_dir, f'{i:02d}_{safe}.mid'), [t])
    print(f'  스템 {len(tracks)}개 -> {os.path.basename(stem_dir)}/')

    total = 76 * 4 * 60 / BPM
    KEYS = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
    key = KEYS[(2 + TRANSPOSE) % 12]
    print(f'wrote 01~06{R}.mid  —  76 bars, '
          f'{int(total // 60)}:{int(total % 60):02d} @ {BPM}BPM, '
          f'key {key} major (TRANSPOSE={TRANSPOSE:+d}, rev{REV:02d})')
