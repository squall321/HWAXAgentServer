#!/usr/bin/env python3
"""
"Moongate" 프로덕션 감사.  실행: python3 audit.py

지금까지의 계측기는 **편곡**을 봤다(check/analyze/quality/interplay).
이건 완성된 **소리 자체**를 상업 팝 프로덕션의 규범과 대조한다.
"구닥다리로 들린다"가 어디서 오는지 감이 아니라 숫자로 찾기 위한 것.

파이프라인을 베껴 쓰지 않고 render_sf.build_mix() 를 그대로 호출한다 —
재는 대상과 굽는 대상이 같은 코드여야 한다.
"""
import importlib.util, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
def _load(name, fn):
    sp = importlib.util.spec_from_file_location(name, os.path.join(HERE, fn))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m
mg = _load('mg', 'moongate_build.py')
rs = _load('rs', 'render_sf.py')
SR = rs.SR

VERDICT = []
def judge(label, value, lo, hi, unit='', note=''):
    ok = (lo is None or value >= lo) and (hi is None or value <= hi)
    rng = f'{lo if lo is not None else "-"}~{hi if hi is not None else "-"}'
    VERDICT.append(ok)
    print(f'  {"✔" if ok else "★"} {label:34} {value:8.2f}{unit:4}  (프로 {rng}{unit}) {note}')


def kweight(x, sr):
    """BS.1770 K-가중의 근사 — 주파수 영역에서 게인 곡선으로 건다.
    biquad 를 표본마다 돌리려면 파이썬 루프라 1500만 표본에서 못 쓴다.
    정확한 규격값은 아니지만 '이 곡이 -17 이고 상업 팝이 -10' 같은 판단에는 충분하다."""
    n = len(x)
    f = np.maximum(np.fft.rfftfreq(n, 1 / sr), 1e-6)
    hp = f ** 2 / np.sqrt((f ** 2 - 38.0 ** 2) ** 2 + (38.0 * f / 0.5) ** 2)   # 38Hz 하이패스
    shelf = np.sqrt((1 + (f / 1681.0) ** 2 * 10 ** (4.0 / 10)) / (1 + (f / 1681.0) ** 2))
    g = hp * shelf
    return np.fft.irfft(np.fft.rfft(x, n) * g, n)


def lufs(mix, sr):
    m = np.mean([np.mean(kweight(mix[:, c], sr) ** 2) for c in range(2)])
    return -0.691 + 10 * np.log10(m + 1e-12)


def band_corr(mix, sr, lo, hi):
    n = len(mix)
    f = np.fft.rfftfreq(n, 1 / sr)
    m = (f >= lo) & (f < hi)
    a = np.fft.irfft(np.fft.rfft(mix[:, 0], n) * m, n)
    b = np.fft.irfft(np.fft.rfft(mix[:, 1], n) * m, n)
    d = np.sqrt(np.mean(a ** 2) * np.mean(b ** 2))
    return float(np.mean(a * b) / d) if d > 1e-18 else 1.0


def main():
    inst = '--inst' in sys.argv[1:]
    print('믹스를 굽는 중…')
    mix = rs.build_mix(inst=inst, quiet=True)
    mono = mix.mean(axis=1)
    n = len(mix)

    print('\n' + '=' * 72)
    print('[A] 라우드니스 · 다이내믹 — 스트리밍 팝 규범과 대조')
    print()
    L = lufs(mix, SR)
    # 반주판(05)과 완성곡판(06)의 목표가 다르다. 05 는 그 위에 보컬이 올라가므로
    # 일부러 4dB 쯤 비워 둔다 — 완성곡 레벨로 주면 사용자가 결국 다시 내려야 한다.
    lo, hi = (-14.0, -8.0) if inst else (-16.0, -12.5)
    judge('통합 라우드니스 (근사 LUFS)', L, lo, hi, 'LU',
          '완성곡 기준' if inst else '반주 기준 — 보컬 얹을 헤드룸을 남긴다')
    peak_db = 20 * np.log10(np.max(np.abs(mix)) + 1e-12)
    rms_db = 20 * np.log10(np.sqrt(np.mean(mix ** 2)) + 1e-12)
    judge('크레스트 팩터 (피크-RMS)', peak_db - rms_db, 8.0, 14.0, 'dB')
    judge('트루피크 여유', -peak_db, 0.3 if inst else 2.0, None, 'dB',
          '' if inst else '보컬이 올라갈 자리')
    spb = 4 * 60.0 / mg.BPM
    secs = {}
    for label, rng in mg.SECTIONS.items():
        seg = mix[int(SR * (rng[0] - 1) * spb):min(int(SR * rng[-1] * spb), n)]
        if len(seg):
            secs[label] = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-12)
    judge('섹션 간 다이내믹 폭', max(secs.values()) - min(secs.values()), 3.0, 9.0, 'dB')

    print('\n' + '=' * 72)
    print('[B] 스펙트럼 기울기 — 마스터된 팝은 일정한 틸트를 갖는다')
    print()
    S = np.abs(np.fft.rfft(mono[:SR * 60] * np.hanning(min(SR * 60, n)))) ** 2
    f = np.fft.rfftfreq(min(SR * 60, n), 1 / SR)
    m = (f >= 100) & (f <= 10000)
    x = np.log2(f[m]); y = 10 * np.log10(S[m] + 1e-18)
    # 옥타브 밴드로 뭉쳐 회귀 — 원시 FFT 는 점이 너무 많아 저역에 가중이 쏠린다
    edges = 2.0 ** np.arange(np.log2(100), np.log2(10000) + 0.001, 1.0)
    bx, by = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mm = (f >= lo) & (f < hi)
        if mm.sum():
            bx.append(np.log2(np.sqrt(lo * hi))); by.append(10 * np.log10(S[mm].mean() + 1e-18))
    slope = np.polyfit(bx, by, 1)[0]
    # 틸트 규범은 **완성 마스터** 기준이다. 반주판(05)은 보컬 포켓으로 1~4kHz 를 일부러
    # 비워 두므로 그만큼 어둡게 측정된다 — 그 자리는 보컬이 채운다. 완성곡 기준을 그대로
    # 들이대면 "너무 어둡다"는 가짜 신호가 나온다(실제로 -5.46 로 잡혔다).
    tlo, thi = (-5.0, -2.5) if inst else (-6.2, -3.0)
    judge('스펙트럼 틸트', slope, tlo, thi, 'dB/oct',
          '완성곡 기준' if inst else '반주 기준 — 보컬 대역을 비워 둔 만큼 어둡게 나온다')

    print('\n' + '=' * 72)
    print('[C] 스테레오 이미지 — 대역마다 넓이가 달라야 한다')
    print()
    for lo, hi, lbl, tlo, thi in ((20, 150, '저역 20-150Hz', 0.85, 1.0),
                                  (150, 800, '중저역 150-800Hz', 0.5, 0.95),
                                  (800, 4000, '중고역 0.8-4kHz', 0.15, 0.8),
                                  (4000, 16000, '고역 4-16kHz', 0.0, 0.7)):
        judge(lbl + ' 좌우상관', band_corr(mix, SR, lo, hi), tlo, thi, '')
    loss = 20 * np.log10((np.sqrt(np.mean(mono ** 2)) + 1e-12) /
                         (np.sqrt(np.mean(mix ** 2)) + 1e-12))
    judge('모노 합산 손실', -loss, 0.0, 1.5, 'dB', '(클럽·카페 모노 스피커 대비)')

    print('\n' + '=' * 72)
    print('[D] 트랜지언트 — 어택이 살아 있는가')
    print()
    w = int(SR * 0.05)
    fr = mono[:n // w * w].reshape(-1, w)
    cr = 20 * np.log10((np.max(np.abs(fr), axis=1) + 1e-12) /
                       (np.sqrt(np.mean(fr ** 2, axis=1)) + 1e-12))
    judge('50ms 창 평균 크레스트', float(np.mean(cr)), 8.0, 16.0, 'dB',
          '낮으면 눌려 있다(펀치 없음)')

    print('\n' + '=' * 72)
    print('[E] 편곡 레이어 — 프로 트랙은 한 역할을 여러 겹으로 쌓는다')
    print()
    ROLE = {'저역': ('Bass', 'Bass Character'),
            '드럼': ('Drums', 'Percussion', 'Percussion Hi'),
            '화성': ('Rhodes', 'Guitar (16th chops)', 'Strings'),
            '선율/색': ('Signature Whistle', 'Celtic Harp')}
    names = {t.name for t in mg.make_tracks() if t.ev and 'Vocal' not in t.name}
    for role, members in ROLE.items():
        have = [m for m in members if m in names]
        judge(f'{role} 레이어 수', float(len(have)), 2, None, '겹',
              '· '.join(have))

    print('\n' + '=' * 72)
    print('[F] 음색 변화 — 섹션마다 소리가 달라지는가 (자동화의 흔적)')
    print()
    cents = {}
    for label, rng in mg.SECTIONS.items():
        seg = mono[int(SR * (rng[0] - 1) * spb):min(int(SR * rng[-1] * spb), n)]
        if len(seg) < 4096:
            continue
        Sx = np.abs(np.fft.rfft(seg)) ** 2
        fx = np.fft.rfftfreq(len(seg), 1 / SR)
        cents[label] = float(np.sum(fx * Sx) / max(np.sum(Sx), 1e-18))
    lo_s = min(cents, key=cents.get); hi_s = max(cents, key=cents.get)
    judge('스펙트럼 중심 변화폭', cents[hi_s] / cents[lo_s], 1.5, None, '배',
          f'가장 어두움 {lo_s} {cents[lo_s]:.0f}Hz / 가장 밝음 {hi_s} {cents[hi_s]:.0f}Hz')

    print('\n' + '=' * 72)
    bad = VERDICT.count(False)
    print(f'{"✔ 규범 안" if not bad else f"★ 규범을 벗어난 항목 {bad}개 / {len(VERDICT)}개"}')


if __name__ == '__main__':
    main()
