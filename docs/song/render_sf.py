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
    'Percussion':          (300, None,        +1.5),   # 햇·셰이커·탬버린은 저역이 필요 없다
    'Bass':                (35,  (250, -1.5),  0.0),   # 초저역 쓰레기만 걷고 250Hz 뭉침을 판다
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
    'Percussion':          (0.078, +0.12, 0.15),   # 하이햇·셰이커·탬버린·라이드
    'Bass':                (0.155, +0.00, 0.02),   # 저역은 모노·드라이로 둬야 카페 스피커에서 뭉치지 않는다
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
    env = np.max(np.abs(x), axis=1)
    need = np.minimum(1.0, ceiling / np.maximum(env, 1e-9))
    # 룩어헤드 구간의 최소 게인을 미리 적용 — 피크가 오기 전에 이미 내려가 있다
    pad = np.pad(need, (0, la - 1), constant_values=1.0)
    g = sliding_window_view(pad, la).min(axis=1)
    # 릴리즈: 이동평균으로 부드럽게. 평균은 값을 올릴 수 있으므로 다시 룩어헤드 최소와 겹쳐
    # 오버슛을 막되, 완전히 덮어쓰지 않도록 둘의 기하평균을 쓴다.
    k = np.ones(rel) / rel
    gs = np.convolve(g, k, mode='same')
    g = np.minimum(gs, np.sqrt(np.maximum(g * gs, 1e-12)))
    y = x * g[:, None]
    peak = float(np.max(np.abs(y)))
    if peak > ceiling:                      # 스무딩이 남긴 미세 오버슛만 정리한다
        y *= ceiling / peak
    return y


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
        print(f'  {name:22s} RMS {rms:.4f} -> gain {g:5.2f}  팬 {pan:+.2f}  센드 {send:.2f}'
              f'  EQ hp{_hp}' + (f' cut{_cut[0]}Hz{_cut[1]:+.1f}' if _cut else '')
              + (f' air{_air:+.1f}' if _air else ''))

    # 리버브는 센드 버스에 한 번만 건다 — 악기마다 따로 걸면 FFT 를 7번 돌리게 된다
    mix += reverb(wet_bus, SR, seconds=2.4) * 0.55

    # 버스 글루 압축 — 곡 전체가 한 덩어리로 숨쉬게
    env = rn.smooth(np.max(np.abs(mix), axis=1), int(SR * 0.03))
    thr = float(np.percentile(env, 88))
    gain = np.where(env > thr, (thr / np.maximum(env, 1e-9)) ** 0.30, 1.0)
    mix *= rn.smooth(gain, int(SR * 0.08))[:, None]

    mid_s = (mix[:, 0] + mix[:, 1]) * 0.5
    side = (mix[:, 0] - mix[:, 1]) * 0.5 * 1.15          # 모노 호환 유지
    mix = np.stack([mid_s + side, mid_s - side], axis=1)

    fade = min(int(SR * 1.5), n)
    mix[-fade:] *= np.linspace(1.0, 0.0, fade)[:, None]
    mix[:int(SR * 0.02)] *= np.linspace(0.0, 1.0, int(SR * 0.02))[:, None]

    peak = float(np.max(np.abs(mix)))
    if not np.isfinite(peak) or peak < 1e-9:
        raise SystemExit(f'렌더 실패 — peak={peak}')
    # 실제 샘플은 크레스트 팩터가 커서 같은 피크에서 RMS 가 낮게 나온다. 예전에는
    # tanh 새추레이션으로 밀어 넣었는데 THD 16.4% 를 만들고 있었다(limiter 주석 참고).
    # 대신 게인을 먼저 올려 놓고 리미터가 피크만 눌러내리게 한다 — 왜곡이 없다.
    mix = mix / peak * 2.1
    mix = limiter(mix, SR, ceiling=0.94)
    if int(np.sum(~np.isfinite(mix))):
        raise SystemExit('NaN/Inf 발생 — 렌더 중단')

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
    enc.set_bit_rate(224); enc.set_in_sample_rate(SR); enc.set_channels(2); enc.set_quality(2)
    data = enc.encode(pcm) + enc.flush()
    with open(dst, 'wb') as f:
        f.write(data)
    print(f'-> {dst}  ({len(data)/1024:.0f} KB)')


if __name__ == '__main__':
    main()
