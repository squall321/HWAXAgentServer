#!/usr/bin/env python3
"""
"Moongate" 사운드폰트 렌더러.  실행: python3 render_sf.py

render.py 는 numpy 로 파형을 직접 합성한다(가산합성). 그건 배치·밸런스를 확인하는 데는
충분하지만, 정현파를 쌓아 만든 소리라 실제로 들으면 8비트 칩튠처럼 들린다 — 사용자가
"간주 악기가 촌스럽다"고 한 게 편곡이 아니라 이 합성 방식 때문이었다.

여기서는 실제 녹음 샘플이 든 사운드폰트(FluidR3_GM)를 fluidsynth 로 울린다.
악기별로 스템을 따로 뽑아 numpy 로 믹스하기 때문에 게인·팬·리버브 센드를 그대로 통제한다.
fluidsynth 자체 리버브/코러스는 끄고(-R 0 -C 0) 믹스단에서 건다.

의존성:  apt-get install fluidsynth fluid-soundfont-gm   /   pip install numpy lameenc
"""
import importlib.util, os, subprocess, sys, tempfile, wave
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('mg', os.path.join(HERE, 'moongate_build.py'))
mg = importlib.util.module_from_spec(spec); spec.loader.exec_module(mg)
rspec = importlib.util.spec_from_file_location('rn', os.path.join(HERE, 'render.py'))
rn = importlib.util.module_from_spec(rspec); rspec.loader.exec_module(rn)   # main() 은 __main__ 가드로 보호됨

SR = 44100
# 사운드폰트는 귀로 고른다. 배음 성분 개수 같은 스펙트럼 지표로는 우열이 안 갈린다 —
# 노이즈가 많아도 올라가는 값이라 '샘플이냐 가산합성이냐' 같은 범주 차이만 잡힌다.
# 그래서 후보를 골라 쓸 수 있게 --sf2 를 둔다.
SF2_CANDIDATES = ['/usr/share/sounds/sf2/MuseScore_General_Full.sf2',   # 489MB, 최신·용량 큼
                  '/usr/share/sounds/sf2/FluidR3_GM.sf2',              # 148MB, 2008년 고전
                  '/usr/share/sounds/sf2/default-GM.sf2',
                  '/usr/share/soundfonts/FluidR3_GM.sf2']

# 악기별 믹스 — (목표 RMS, 팬(-1왼쪽~+1오른쪽), 리버브 센드)
# 목표 RMS 는 절대값이 아니라 서로의 비율로 읽는다. 드럼·베이스가 바닥을 잡고,
# 로즈가 화성의 몸통, 기타는 그 사이를 긁고, 휘슬은 선율이라 앞에 나오되 리버브로 멀리 둔다.
# 악기별 보정 EQ — (high-pass Hz, (박스울림 중심Hz, dB), 8k 위 셸프 dB)
EQ = {
    'Drums':               (30,  None,         0.0),
    'Percussion':          (300, None,        +1.5),   # 햇·셰이커는 저역이 필요 없다
    'Percussion Hi':       (350, None,        +2.0),
    'Bass':                (35,  (250, -1.5),  0.0),   # 초저역 쓰레기만 걷고 250Hz 뭉침을 판다
    'Bass Character':      (250, None,        +1.0),   # ★250Hz 아래를 통째로 잘라 배음만 남긴다
    'Rhodes':              (90,  (380, -2.5),  0.0),   # 화성의 몸통이지만 박스 울림이 제일 심하다
    'Guitar (16th chops)': (140, (450, -2.0), +1.5),
    'Signature Whistle':   (250, None,        +1.0),
    'Celtic Harp':         (200, None,        +1.5),
    'Strings':             (200, (400, -2.0), +1.0),
}

MIX = {
    # 킥이 저역을 독점하고 있었다 — 20~60Hz 를 드럼 88.8% 대 베이스 11.2% 로 나눠 갖고 있어서
    # "베이스가 없다"고 들렸다. 킥은 최저역의 타격감만 유지하고, 음정이 들리는 60~250Hz 는
    # 베이스가 가져가도록 비율을 바꿨다. (근음 옥타브 정리와 함께 적용한 결과다)
    'Drums':               (0.120, +0.00, 0.07),   # 킥·스네어·림·탐·크래시
    # 퍼커션을 따로 뗀 이유가 이 줄이다 — 킷과 한 트랙이면 킥에 묻혀 고역이 안 올라간다.
    'Percussion':          (0.062, -0.30, 0.15),   # 햇·셰이커 — 왼쪽
    'Percussion Hi':       (0.052, +0.34, 0.22),   # 탬버린·라이드 — 오른쪽. 리버브를 더 줘 폭을 벌린다
    'Bass':                (0.150, +0.00, 0.02),
    'Bass Character':      (0.030, +0.00, 0.03),   # 배음만 — 저역은 Bass 가 맡는다   # 저역은 모노·드라이로 둬야 카페 스피커에서 뭉치지 않는다
    'Rhodes':              (0.080, -0.16, 0.17),
    'Guitar (16th chops)': (0.052, +0.26, 0.11),   # 로즈 반대편에 앉힌다
    'Signature Whistle':   (0.072, +0.08, 0.26),
    'Celtic Harp':         (0.050, -0.28, 0.26),
    'Strings':             (0.044, +0.00, 0.30),
}


def eq(x, sr, hp=0.0, cut=None, air=0.0):
    """보정 EQ — 주파수 영역에서 게인 곡선을 곱한다.

    ★rev10 에서 추가. 그때까지 EQ 가 **하나도 없었다**. GM 샘플은 쓸모없는 저역과
    300~500Hz 의 박스 울림을 달고 있어서, 여덟 트랙을 그냥 쌓으면 중저역이 뭉쳐
    "싸구려 미디" 소리가 난다. 악기마다 자기 자리만 남기고 비워 준다.

    hp   : 이 아래를 걷어낸다(옥타브당 -12dB 기울기)
    cut  : (중심주파수, dB) — 박스 울림 자리를 판다
    air  : 8kHz 위 셸프(dB)
    """
    n = len(x)
    f = np.fft.rfftfreq(n, 1 / sr)
    g = np.ones(len(f))
    if hp > 0:
        g *= 1.0 / np.sqrt(1.0 + (hp / np.maximum(f, 1e-6)) ** 4)
    if cut:
        fc, db = cut
        g *= 10 ** ((db / 20) * np.exp(-((np.log2(np.maximum(f, 1e-6) / fc)) ** 2) / (2 * 0.55 ** 2)))
    if air:
        g *= 10 ** ((air / 20) / (1.0 + (8000.0 / np.maximum(f, 1e-6)) ** 2))
    out = np.empty_like(x)
    for ch in range(x.shape[1]):
        out[:, ch] = np.fft.irfft(np.fft.rfft(x[:, ch], n) * g, n)
    return out


def reverb(mix, sr, seconds=1.8, seed=20260830):
    """컨볼루션 리버브 — 실제 공간에 가까운 임펄스 응답을 합성한다.

    ★rev10 에서 고친 것. 종전 IR 은 `백색잡음 × 지수감쇠` 하나였다. 두 가지가 빠져 있었다.

    1. **초기 반사가 없었다.** 방의 크기를 느끼게 하는 건 꼬리가 아니라 첫 10~70ms 의
       몇 개 탭이다. 그게 없으면 "공간"이 아니라 "잔향 효과"로만 들린다.
    2. **고역이 끝까지 살아 있었다.** 실제 공간은 고역이 먼저 죽는다. 감쇠를 한 속도로
       걸면 꼬리가 계속 밝아 금속성·모래알 같은 소리가 난다.

    좌우를 다른 난수로 만들어 스테레오 폭도 리버브에서 나오게 한다.
    """
    rng = np.random.default_rng(seed)
    n = int(sr * seconds)
    t = np.arange(n) / sr
    f = np.fft.rfftfreq(n, 1 / sr)
    irs = []
    for ch in range(2):
        N = np.fft.rfft(rng.standard_normal(n))
        out = np.zeros(n)
        for lo, hi, mult in ((0, 250, 1.15), (250, 1000, 1.0),
                             (1000, 4000, 0.72), (4000, sr / 2 + 1, 0.42)):
            band = np.fft.irfft(N * ((f >= lo) & (f < hi)), n)
            out += band * np.exp(-t / (seconds * mult / 4.5))
        for d_ms, g in ((11, 0.42), (19, -0.31), (27, 0.24),
                        (38, -0.18), (53, 0.13), (71, -0.09)):
            i = int(sr * d_ms / 1000) + ch * 13      # 좌우 탭을 어긋내 폭을 만든다
            if i < n:
                out[i] += g
        irs.append(out / (np.sqrt(np.sum(out ** 2)) + 1e-9))
    n_fft = 1
    while n_fft < len(mix) + n:
        n_fft *= 2
    wet = np.empty_like(mix)
    for ch in range(2):
        X = np.fft.rfft(mix[:, ch], n_fft)
        IR = np.fft.rfft(irs[ch], n_fft)
        wet[:, ch] = np.fft.irfft(X * IR, n_fft)[:len(mix)]
    return wet


def compressor(x, sr, thresh_db=-17.0, ratio=2.4, attack_ms=15.0, release_ms=160.0):
    """버스 컴프레서 — 어택/릴리즈가 있는 진짜 압축.

    ★rev11 에서 잡은 것. 종전 "글루 압축"은 88퍼센타일 위를 0.30 제곱으로 누르는
    임시방편이라 실제로는 거의 아무것도 하지 않았다. 측정값이 그걸 말한다:
    **통합 라우드니스 -17.4 LUFS, 크레스트 팩터 16.7dB.** 상업 팝은 -14~-8 LUFS,
    크레스트 8~14dB 다. 즉 **평균은 조용한데 피크만 튀는**, 마스터링을 안 한 소리였다.
    프로 마스터 옆에 놓으면 초라하게 들리는 이유가 이것이다.

    어택/릴리즈 비대칭은 표본 루프 없이 만든다 — 빠른 창과 느린 창으로 각각 뭉갠 뒤
    **더 많이 누르는 쪽**을 취하면 어택은 빠르고 릴리즈는 느려진다.
    """
    det = rn.smooth(np.max(np.abs(x), axis=1), max(1, int(sr * 0.002)))
    db = 20 * np.log10(det + 1e-9)
    gr = -np.maximum(db - thresh_db, 0.0) * (1.0 - 1.0 / ratio)
    fast = rn.smooth(gr, max(1, int(sr * attack_ms / 1000)))
    slow = rn.smooth(gr, max(1, int(sr * release_ms / 1000)))
    gr = np.minimum(fast, slow)
    return x * (10 ** (gr / 20.0))[:, None]


def widen(mix, sr, bands=((0, 150, 1.00), (150, 800, 1.35),
                          (800, 4000, 1.75), (4000, 30000, 2.30))):
    """대역별 스테레오 폭 — 저역은 모노로 묶고 고역으로 갈수록 넓힌다.

    ★rev11 에서 잡은 것. 종전에는 사이드 성분 전체에 1.15 를 곱하는 **광대역** 처리였다.
    측정해 보니 고역(4~16kHz) 좌우상관이 **0.96** — 거의 모노였다. 프로 트랙은 이 대역이
    0.0~0.7 이다. 고역이 모노면 이미지가 납작해지고, 그게 "구닥다리"로 들리는 큰 원인이다.

    저역을 모노로 두는 건 타협이 아니라 규칙이다 — 위상이 어긋난 저역은 카페 천장
    스피커에서 합산되며 사라진다. 넓힐 곳은 위쪽이다.
    """
    n = len(mix)
    f = np.fft.rfftfreq(n, 1 / sr)
    g = np.ones(len(f))
    for lo, hi, w in bands:
        g = np.where((f >= lo) & (f < hi), w, g)
    # 대역 경계에서 튀지 않게 살짝 뭉갠다
    k = max(3, int(len(f) / 4000)) | 1
    g = np.convolve(g, np.ones(k) / k, mode='same')
    mid = (mix[:, 0] + mix[:, 1]) * 0.5
    side = (mix[:, 0] - mix[:, 1]) * 0.5
    side = np.fft.irfft(np.fft.rfft(side, n) * g, n)
    return np.stack([mid + side, mid - side], axis=1)


def limiter(x, sr, ceiling=0.94, lookahead_ms=6.0, release_ms=140.0):
    """룩어헤드 피크 리미터 — 마스터 버스의 tanh 새추레이션을 대체한다.

    ★rev10 에서 잡은 것. 라우드니스를 맞추려고 `tanh(x*1.9)/tanh(1.9)` 를 걸고 있었는데,
    사인파를 통과시켜 재보니 **THD 16.4%** 였다. 퍼즈 페달이 10~20% 다 —
    마스터 버스에 퍼즈를 걸어 놓고 있었던 셈이고, 그게 "8비트처럼 촌스럽다"의 정체다.
    (rev03~05 의 drive=1.05 도 이미 7.25% 였다. 처음부터 걸려 있던 문제다.)

    리미터는 파형을 구부리는 대신 **게인을 시간축에서 줄인다**. 같은 라우드니스를
    왜곡 없이 얻는 방법이고, 실제 마스터링이 하는 일이다.

    - 룩어헤드: 미래 피크를 미리 보고 게인을 **먼저** 내린다(어택 왜곡이 없다)
    - 릴리즈: 천천히 되돌아온다(펌핑을 피한다)
    """
    from numpy.lib.stride_tricks import sliding_window_view
    la = max(1, int(sr * lookahead_ms / 1000))
    rel = max(1, int(sr * release_ms / 1000))
    y = x
    for _ in range(2):                      # 두 번 돌리면 잔여 오버슛까지 수렴한다
        env = np.max(np.abs(y), axis=1)
        need = np.minimum(1.0, ceiling / np.maximum(env, 1e-9))
        # 룩어헤드 구간의 최소 게인 — 피크가 오기 전에 이미 내려가 있다(어택 왜곡 없음)
        pad = np.pad(need, (0, la - 1), constant_values=1.0)
        safe = sliding_window_view(pad, la).min(axis=1)
        # 릴리즈는 이동평균으로 부드럽게 하되, **절대 safe 를 넘지 않게** 다시 겹친다.
        # ★rev11 에서 잡은 버그: 종전에는 기하평균을 쓴 뒤 오버슛이 남으면 곡 전체를
        #  `y *= ceiling/peak` 로 다시 줄였다. 단 한 표본의 피크가 3분짜리 곡 전체의
        #  라우드니스를 끌어내리고 있었다 — 게인을 아무리 밀어넣어도 -15 LUFS 에서
        #  올라가지 않던 이유가 이것이다.
        g = np.minimum(rn.smooth(safe, rel), safe)
        y = y * g[:, None]
    return np.clip(y, -ceiling, ceiling)


def find_sf2(argv):
    for i, a in enumerate(argv):
        if a == '--sf2' and i + 1 < len(argv):
            if not os.path.exists(argv[i + 1]):
                raise SystemExit(f'사운드폰트 없음: {argv[i + 1]}')
            return argv[i + 1]
    for p in SF2_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit('사운드폰트를 찾지 못했다. apt-get install fluid-soundfont-gm')


# fluidsynth 실행 파일 — 소스 빌드한 새 버전(/usr/local)이 있으면 그쪽을 쓴다.
# apt 의 노블 패키지는 2.3.4 에 묶여 있어, 2.6.0 은 직접 빌드했다.
FLUIDSYNTH = next((p for p in ('/usr/local/bin/fluidsynth', '/usr/bin/fluidsynth')
                   if os.path.exists(p)), 'fluidsynth')


def fluidsynth_version():
    try:
        out = subprocess.run([FLUIDSYNTH, '--version'], capture_output=True, text=True).stdout
        return out.splitlines()[0].split()[-1]
    except Exception:
        return '?'


def render_stem(mid_path, sf2, wav_path):
    subprocess.run([FLUIDSYNTH, '-ni', '-g', '0.8', '-r', str(SR),
                    '-R', '0', '-C', '0',              # 리버브·코러스는 믹스단에서 건다
                    '-F', wav_path, sf2, mid_path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with wave.open(wav_path) as w:
        n, ch = w.getnframes(), w.getnchannels()
        a = np.frombuffer(w.readframes(n), dtype='<i2').astype(np.float64) / 32768.0
    return a.reshape(-1, ch) if ch == 2 else np.stack([a, a], axis=1)


def build_mix(inst=False, sf2=None, quiet=False):
    """스템을 굽고 믹스해 최종 배열을 돌려준다. main() 과 audit.py 가 같이 쓴다.

    감사 도구가 파이프라인을 따로 베껴 쓰면 반드시 어긋난다 — 재는 대상과 굽는 대상이
    같은 코드여야 한다. (방법론 문서: "쓴 쪽과 읽는 쪽이 같은 코드였기 때문이다")
    """
    sf2 = sf2 or find_sf2([])
    if not quiet:
        print(f'fluidsynth {fluidsynth_version()}  ({FLUIDSYNTH})')
        print(f'사운드폰트: {sf2}   ({"인스트루멘털" if inst else "반주"})')
    return _mix_from_stems(inst, sf2, quiet)


def main():
    rev = mg.REV
    # --inst : 보컬 선율까지 악기가 이어받는 인스트루멘털판(06)을 굽는다
    argv = sys.argv[1:]
    inst = '--inst' in argv
    args, skip = [], False
    for a in argv:                          # --sf2 <경로> 의 경로를 출력 파일명으로 오해하지 않게
        if skip:
            skip = False; continue
        if a == '--sf2':
            skip = True; continue
        if not a.startswith('--'):
            args.append(a)
    default = f'0{"6_instrumental" if inst else "5_instruments"}_rev{rev:02d}.mp3'
    dst = args[0] if args else os.path.join(HERE, default)
    sf2 = find_sf2(argv)
    print(f'fluidsynth {fluidsynth_version()}  ({FLUIDSYNTH})')
    print(f'사운드폰트: {sf2}   ({"인스트루멘털" if inst else "반주"})')

    mix = _mix_from_stems(inst, sf2, quiet=False)
    n = len(mix)

    print(f'\n최종 RMS {20*np.log10(np.sqrt(np.mean(mix**2))+1e-12):.1f}dBFS')
    spb = 4 * 60.0 / mg.BPM
    print('\n섹션별 RMS')
    for label, rng in mg.SECTIONS.items():
        seg = mix[int(SR*(rng[0]-1)*spb):min(int(SR*rng[-1]*spb), n)]
        if not len(seg):
            continue
        db = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-12)
        print(f'  {label:9s} {db:6.1f}dBFS  ' + '#' * max(0, int(db + 40)))
    print(f'\n0.999 이상 샘플: {int(np.sum(np.abs(mix) >= 0.999))}개 / {mix.size}개')

    pcm = (mix * 32767.0).astype('<i2').tobytes()
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(320); enc.set_in_sample_rate(SR); enc.set_channels(2); enc.set_quality(0)
    data = enc.encode(pcm) + enc.flush()
    with open(dst, 'wb') as f:
        f.write(data)
    print(f'-> {dst}  ({len(data)/1024:.0f} KB)')


def _mix_from_stems(inst, sf2, quiet):
    tracks = [t for t in mg.make_tracks(topline=inst) if t.ev and 'Vocal' not in t.name]
    tmp = tempfile.mkdtemp(prefix='moongate_sf_')
    stems = {}
    for t in tracks:
        mid = os.path.join(tmp, f'{t.ch}.mid')
        mg.write_midi(mid, [t])
        stems[t.name] = render_stem(mid, sf2, os.path.join(tmp, f'{t.ch}.wav'))

    n = max(len(a) for a in stems.values())
    mix = np.zeros((n, 2))
    wet_bus = np.zeros((n, 2))
    if not quiet:
        print('\n악기별 스템')
    for name, a in stems.items():
        a = np.pad(a, ((0, n - len(a)), (0, 0)))
        target, pan, send = MIX.get(name, (0.05, 0.0, 0.15))
        _hp, _cut, _air = EQ.get(name, (0, None, 0.0))
        a = eq(a, SR, hp=_hp, cut=_cut, air=_air)      # 레벨을 맞추기 전에 EQ 를 건다
        rms = float(np.sqrt(np.mean(a ** 2)))
        g = target / max(rms, 1e-9)
        a = a * g
        lg, rg = rn.pan_gains(pan)
        a = a * np.array([lg, rg])
        mix += a
        wet_bus += a * send
        if quiet:
            continue
        print(f'  {name:22s} RMS {rms:.4f} -> gain {g:5.2f}  팬 {pan:+.2f}  센드 {send:.2f}'
              f'  EQ hp{_hp}' + (f' cut{_cut[0]}Hz{_cut[1]:+.1f}' if _cut else '')
              + (f' air{_air:+.1f}' if _air else ''))

    # 리버브는 센드 버스에 한 번만 건다 — 악기마다 따로 걸면 FFT 를 7번 돌리게 된다
    mix += reverb(wet_bus, SR, seconds=2.4) * 0.55

    # ★꼬리 무음 제거. fluidsynth 는 릴리즈용으로 스템을 넉넉히 패딩해 내보내고,
    # 그중 가장 긴 스템 길이가 곡 전체 길이가 된다. 그 결과 소리가 끝난 뒤로 **6.7초의
    # 완전 무음**이 붙어 있었고, 마지막 1.5초 페이드도 이미 무음인 자리에서 걸리고 있었다.
    # 리버브 꼬리까지 다 울린 지점(-60dB)에서 잘라 1.2초만 남긴다.
    _env = np.max(np.abs(mix), axis=1)
    _thr = float(_env.max()) * 1e-3                       # -60dB
    _nz = np.nonzero(_env > _thr)[0]
    if len(_nz):
        n = min(n, int(_nz[-1]) + int(SR * 1.2))
        mix = mix[:n]

    # 섹션 매크로 오토메이션 — 리미터 **앞에서** 낙차를 미리 벌려 둔다.
    # 컴프레서와 리미터는 큰 곳을 더 누르므로, 아무것도 안 하면 편곡의 낙차가 깎여 나간다
    # (실제로 3.95dB -> 2.71dB 로 무너졌다). 마스터링 엔지니어가 리미터 앞에서 볼륨을
    # 오토메이션하는 게 이 이유다. 편곡 의도대로의 곡선을 여기서 되돌려 놓는다.
    # 깊이는 측정으로 정했다: 리미터를 통과하며 낙차의 약 73% 만 살아남는다.
    # 출력 3.5dB 를 원하면 입력은 5dB 쯤 벌려 놔야 한다.
    SEC_DB = {'intro': -5.2, 'verse1': -2.5, 'pre1': -1.2, 'chorus1': +0.3,
              'post1': +0.8, 'verse2': -2.5, 'pre2': -1.2, 'chorus2': +0.3,
              'post2': +0.8, 'bridge': -2.2, 'final': -1.6, 'outro': +0.3}
    spb_ = 4 * 60.0 / mg.BPM
    curve = np.zeros(n)
    for label, rng in mg.SECTIONS.items():
        a0 = int(SR * (rng[0] - 1) * spb_)
        b0 = min(int(SR * rng[-1] * spb_), n)
        curve[a0:b0] = SEC_DB.get(label, 0.0)
    curve[b0:] = curve[b0 - 1] if b0 < n else 0.0
    curve = rn.smooth(curve, int(SR * 0.35))          # 경계를 부드럽게 — 계단이 들리면 안 된다
    mix *= (10 ** (curve / 20.0))[:, None]

    # 버스 컴프레서 — 곡 전체가 한 덩어리로 숨쉬게. 라우드니스는 여기서 만들어진다.
    mix = mix / max(float(np.max(np.abs(mix))), 1e-9) * 0.7      # 스레숄드 기준을 맞춰 둔다
    mix = compressor(mix, SR)

    mix = widen(mix, SR)
    # 마스터 틸트 — GM 샘플을 쌓으면 전체가 어두워진다(측정 -5.0dB/oct, 프로는 -5.0~-2.5).
    # 악기별 EQ 로는 모자라서 마스터에서 한 번 더 들어 올린다.
    mix = eq(mix, SR, air=3.1)

    fade = min(int(SR * 1.5), n)
    mix[-fade:] *= np.linspace(1.0, 0.0, fade)[:, None]
    mix[:int(SR * 0.02)] *= np.linspace(0.0, 1.0, int(SR * 0.02))[:, None]

    peak = float(np.max(np.abs(mix)))
    if not np.isfinite(peak) or peak < 1e-9:
        raise SystemExit(f'렌더 실패 — peak={peak}')
    # 실제 샘플은 크레스트 팩터가 커서 같은 피크에서 RMS 가 낮게 나온다. 예전에는
    # tanh 새추레이션으로 밀어 넣었는데 THD 16.4% 를 만들고 있었다(limiter 주석 참고).
    # 대신 게인을 먼저 올려 놓고 리미터가 피크만 눌러내리게 한다 — 왜곡이 없다.
    mix = mix / peak * 5.4
    mix = limiter(mix, SR, ceiling=0.94)
    if int(np.sum(~np.isfinite(mix))):
        raise SystemExit('NaN/Inf 발생 — 렌더 중단')

    return mix



if __name__ == '__main__':
    main()
