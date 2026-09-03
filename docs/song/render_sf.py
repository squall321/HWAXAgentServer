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
SF2_CANDIDATES = ['/usr/share/sounds/sf2/FluidR3_GM.sf2',
                  '/usr/share/sounds/sf2/default-GM.sf2',
                  '/usr/share/soundfonts/FluidR3_GM.sf2']

# 악기별 믹스 — (목표 RMS, 팬(-1왼쪽~+1오른쪽), 리버브 센드)
# 목표 RMS 는 절대값이 아니라 서로의 비율로 읽는다. 드럼·베이스가 바닥을 잡고,
# 로즈가 화성의 몸통, 기타는 그 사이를 긁고, 휘슬은 선율이라 앞에 나오되 리버브로 멀리 둔다.
MIX = {
    'Drums':               (0.150, +0.00, 0.07),
    'Bass':                (0.120, +0.00, 0.02),   # 저역은 모노·드라이로 둬야 카페 스피커에서 뭉치지 않는다
    'Rhodes':              (0.080, -0.16, 0.17),
    'Guitar (16th chops)': (0.052, +0.26, 0.11),   # 로즈 반대편에 앉힌다
    'Signature Whistle':   (0.072, +0.08, 0.26),
    'Celtic Harp':         (0.050, -0.28, 0.26),
    'Strings':             (0.044, +0.00, 0.30),
}


def find_sf2():
    for p in SF2_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit('사운드폰트를 찾지 못했다. apt-get install fluid-soundfont-gm')


def render_stem(mid_path, sf2, wav_path):
    subprocess.run(['fluidsynth', '-ni', '-g', '0.8', '-r', str(SR),
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
    inst = '--inst' in sys.argv[1:]
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    default = f'0{"6_instrumental" if inst else "5_instruments"}_rev{rev:02d}.mp3'
    dst = args[0] if args else os.path.join(HERE, default)
    sf2 = find_sf2()
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
        rms = float(np.sqrt(np.mean(a ** 2)))
        g = target / max(rms, 1e-9)
        a = a * g
        lg, rg = rn.pan_gains(pan)
        a = a * np.array([lg, rg])
        mix += a
        wet_bus += a * send
        print(f'  {name:22s} RMS {rms:.4f} -> gain {g:5.2f}  팬 {pan:+.2f}  센드 {send:.2f}')

    # 리버브는 센드 버스에 한 번만 건다 — 악기마다 따로 걸면 FFT 를 7번 돌리게 된다
    mix += rn.reverb(wet_bus, SR, seconds=1.5, wet=1.0) * 0.55

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
    # 실제 샘플은 가산합성보다 크레스트 팩터가 커서(트랜지언트가 살아 있다) 같은 피크에서
    # RMS 가 3dB 쯤 낮게 나온다. 소프트 새추레이션으로 밀어 넣어 라우드니스를 맞추되,
    # 카페 배경음악이니 브릭월까지 가지 않는다 — 목표 -17dBFS 근처.
    DRIVE = 1.9
    mix = mix / peak
    mix = np.tanh(mix * DRIVE) / np.tanh(DRIVE)
    mix = mix / float(np.max(np.abs(mix))) * 0.94
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
