#!/usr/bin/env python3
"""
"Moongate" — MIDI -> 오디오 렌더러 (사운드폰트/DAW 없이, numpy 오실레이터만으로).

목적: 04_vocals.mid 는 SynthV(Mai 2)에 직접 임포트하고, 05_instruments.mid 는
그걸 감싸는 반주가 실제로 어떤 균형·질감인지 듣기 전에 확인할 수 있는 MP3 로 뽑는다.

정직하게 밝혀둘 것 — 이건 샘플 라이브러리가 아니라 트랙별로 손으로 짠 가산합성
오실레이터다. INSTRUMENTS.md 에 적은 실제 악기(Rhodes MK1, VSL 로우 D 휘슬 등)의
질감을 흉내낸 것이지 대체재가 아니다. 편곡 밸런스·구조·다이내믹을 미리 듣기 위한
작업용 렌더이지, 발매용 오디오가 아니다.

실행: python3 render.py [입력.mid] [출력.mp3]
기본값: 05_instruments.mid -> 05_instruments.mp3
"""
# 의존성: numpy(필수), lameenc(선택 — 없으면 WAV로 대체)
import struct
import sys
import numpy as np

SR = 44100
REV = 3   # moongate_build.py 의 REV 와 맞춰 둔다 — 산출물 파일명에 그대로 박힌다.
RNG = np.random.default_rng(20260830)   # moongate_build.py 와 같은 시드 계열


# ───────────────────────────────────────────── MIDI 파서 (렌더용 — 벨로시티·CC 포함)

def read_vlq(d, i):
    n = 0
    while True:
        b = d[i]; i += 1
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            return n, i


def parse_midi(path):
    d = open(path, 'rb').read()
    assert d[:4] == b'MThd'
    _, fmt, ntrk, div = struct.unpack('>IHHH', d[4:14])
    i = 14
    tempo_bpm = 112.0
    tracks = []
    for _ in range(ntrk):
        assert d[i:i + 4] == b'MTrk'
        ln = struct.unpack('>I', d[i + 4:i + 8])[0]
        i += 8; end = i + ln
        t = 0; name = ''; run = None
        notes = []            # (start_tick, end_tick, pitch, velocity)
        cc = {}                # cc_num -> [(tick, value), ...]
        pending = {}           # (ch, pitch) -> [(start_tick, vel), ...]  FIFO
        while i < end:
            dt, i = read_vlq(d, i); t += dt
            s = d[i]
            if s == 0xFF:
                mt = d[i + 1]; l, j = read_vlq(d, i + 2)
                if mt == 0x03:
                    name = d[j:j + l].decode('utf8', 'replace')
                elif mt == 0x51 and l == 3:
                    micro = (d[j] << 16) | (d[j + 1] << 8) | d[j + 2]
                    tempo_bpm = 60_000_000 / micro
                i = j + l
                continue
            if s < 0x80:
                s = run
            else:
                i += 1; run = s
            k = s & 0xF0; ch = s & 0x0F
            if k in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                a, b2 = d[i], d[i + 1]; i += 2
                if k == 0x90 and b2 > 0:
                    pending.setdefault((ch, a), []).append((t, b2))
                elif k == 0x80 or (k == 0x90 and b2 == 0):
                    q = pending.get((ch, a))
                    if q:
                        st, vel = q.pop(0)
                        notes.append((st, t, a, vel))
                elif k == 0xB0:
                    cc.setdefault(a, []).append((t, b2))
            elif k in (0xC0, 0xD0):
                i += 1
        i = end
        notes.sort()
        tracks.append(dict(name=name, notes=notes, cc=cc, channel=None))
    return div, tempo_bpm, tracks


# ───────────────────────────────────────────── 공용 DSP 유틸 (numpy 벡터 연산만 사용 — 샘플 루프 없음)

def midi_freq(pitch):
    return 440.0 * 2.0 ** ((pitch - 69) / 12.0)


def adsr(t, dur, a, d, s, r):
    """t: 노트 시작 기준 시간축(초), dur: 노트 길이(초). 벡터 연산으로 ADSR 포락선을 만든다."""
    a = max(1e-4, min(a, dur))
    e = np.empty_like(t)
    m_a = t < a
    e[m_a] = t[m_a] / a
    m_d = (~m_a) & (t < dur)
    dd = max(1e-4, min(d, dur - a))
    dt = t[m_d] - a
    e[m_d] = np.where(dt < dd, 1.0 + (s - 1.0) * (dt / dd), s)
    m_r = t >= dur
    rt = t[m_r] - dur
    e[m_r] = s * np.exp(-rt / max(r, 1e-4) * 3.0)
    return e


def highpass_diff(x, times=1):
    """차분을 반복해 값싼 고역통과 질감을 만든다(진짜 필터 대신 — scipy 없이도 벡터 연산 유지)."""
    for _ in range(times):
        x = np.diff(x, prepend=x[0:1])
    return x


def smooth(x, window):
    """누적합 트릭으로 만드는 이동평균(저역통과 질감), 샘플 루프 없이 벡터 연산."""
    if window <= 1:
        return x
    c = np.cumsum(np.insert(x, 0, 0.0))
    y = (c[window:] - c[:-window]) / window
    pad = len(x) - len(y)
    return np.concatenate([np.full(pad, y[0] if len(y) else 0.0), y])


def pan_gains(pan):
    """등파워 패닝. pan: -1(왼) ~ +1(오)"""
    th = (pan + 1) * np.pi / 4
    return np.cos(th), np.sin(th)


# ───────────────────────────────────────────── 악기별 음색 (가산합성)

def voice_rhodes(t, dur, freq, vel):
    e1 = adsr(t, dur, 0.006, 1.4, 0.42, 0.5)
    e2 = adsr(t, dur, 0.003, 0.30, 0.05, 0.15)          # 타인 어택(벨 성분)
    trem = 1.0 + 0.13 * np.sin(2 * np.pi * 4.6 * t) * np.clip(t / 0.3, 0, 1)
    sig = (np.sin(2 * np.pi * freq * t) * e1
           + 0.5 * np.sin(2 * np.pi * 2 * freq * t + 0.7) * e2
           + 0.12 * np.sin(2 * np.pi * 2.03 * freq * t) * e2)
    return sig * trem * (0.25 + 0.5 * vel / 127)


def voice_guitar(t, dur, freq, vel):
    e = adsr(t, dur, 0.002, min(0.18, dur), 0.0, 0.05)
    harm = sum(a * np.sin(2 * np.pi * n * freq * t + 0.3 * n)
               for n, a in ((1, 1.0), (2, 0.5), (3, 0.28), (4, 0.16), (5, 0.10)))
    pick = highpass_diff(RNG.standard_normal(len(t)), 2) * np.exp(-t / 0.006)
    return (harm * e + 0.35 * pick) * (0.18 + 0.4 * vel / 127)


def voice_bass(t, dur, freq, vel):
    e = adsr(t, dur, 0.008, 0.05, 0.85, 0.12)
    sig = (np.sin(2 * np.pi * freq * t)
           + 0.35 * np.sin(2 * np.pi * 2 * freq * t)
           + 0.12 * np.sin(2 * np.pi * 3 * freq * t))
    return sig * e * (0.35 + 0.55 * vel / 127)


def voice_harp(t, dur, freq, vel):
    e = adsr(t, dur, 0.003, min(1.2, dur + 0.6), 0.0, 0.6)
    harm = sum((1.0 / n) * np.sin(2 * np.pi * n * freq * t)
               * np.exp(-t * (1.2 + 0.5 * n)) for n in range(1, 7))
    return harm * (0.3 + 0.5 * vel / 127)


def voice_strings(t, dur, freq, vel, detune=0.0):
    e = adsr(t, dur, 0.35, 0.2, 0.85, 0.4)
    f = freq * (1.0 + detune)
    saw = sum(((-1) ** (n + 1)) / n * np.sin(2 * np.pi * n * f * t) for n in range(1, 9))
    return saw * e * (0.15 + 0.3 * vel / 127)


def voice_whistle(t, dur, freq, vel, vib_depth_t=None, breath_t=None):
    e = adsr(t, dur, 0.035, 0.08, 0.92, 0.10)
    vib = vib_depth_t if vib_depth_t is not None else np.zeros_like(t)
    inst_f = freq * (1.0 + (vib / 100.0) * np.sin(2 * np.pi * 5.6 * t))
    phase = 2 * np.pi * np.cumsum(inst_f) / SR
    sig = np.sin(phase) + 0.10 * np.sin(2 * phase)
    breath = breath_t if breath_t is not None else np.ones_like(t)
    hiss = highpass_diff(RNG.standard_normal(len(t)), 3) * 0.02
    return (sig * e * breath + hiss * e) * (0.28 + 0.45 * vel / 127)


# ── 드럼 (GM 드럼맵, 채널10) — 노이즈/사인 조합, 실시간 필터 없이 차분·이동평균으로 질감만

def voice_drum(pitch, dur, vel, n_extra=0.0):
    n = int(SR * max(dur, 0.03))
    t = np.arange(n) / SR
    g = 0.35 + 0.55 * vel / 127
    if pitch == 36:                                          # Kick — 클릭/바디/서브 3층
        f = 150 * np.exp(-t / 0.045) + 45
        body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.16)
        sub = np.sin(2 * np.pi * 47 * t) * np.exp(-t / 0.22) * 0.5      # 저역 무게
        click = highpass_diff(RNG.standard_normal(n), 2) * np.exp(-t / 0.004) * 0.6
        return (body + sub + click) * g * 1.3
    if pitch == 38:                                          # Snare — 몸통 두 음 + 스네어 와이어
        noise = smooth(RNG.standard_normal(n), 3) * np.exp(-t / 0.13)
        wire = highpass_diff(RNG.standard_normal(n), 1) * np.exp(-t / 0.09) * 0.45
        tone = (np.sin(2 * np.pi * 185 * t) + 0.6 * np.sin(2 * np.pi * 278 * t)) \
               * np.exp(-t / 0.07) * 0.4
        return (noise * 0.7 + wire + tone) * g
    if pitch == 37:                                          # Rim
        noise = highpass_diff(RNG.standard_normal(n), 2) * np.exp(-t / 0.03)
        tone = np.sin(2 * np.pi * 900 * t) * np.exp(-t / 0.02)
        return (noise * 0.7 + tone * 0.5) * g
    if pitch in (42, 70):                                    # Closed hihat / shaker
        noise = highpass_diff(RNG.standard_normal(n), 3) * np.exp(-t / (0.045 if pitch == 42 else 0.09))
        return noise * g * 0.8
    if pitch == 46:                                          # Open hihat
        noise = highpass_diff(RNG.standard_normal(n), 3) * np.exp(-t / 0.22)
        return noise * g * 0.75
    if pitch == 49:                                          # Crash
        partials = sum(np.sin(2 * np.pi * f * t) for f in (311, 467, 622, 883, 1109, 1400))
        noise = highpass_diff(RNG.standard_normal(n), 2)
        return (partials * 0.06 + noise * 0.18) * np.exp(-t / 1.3) * g * 1.1
    if pitch in (47, 45):                                    # Toms
        base = 180 if pitch == 47 else 140
        f = base * np.exp(-t / 0.08) + base * 0.55
        sig = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.35)
        return sig * g
    noise = smooth(RNG.standard_normal(n), 2) * np.exp(-t / 0.12)     # 그 외 폴백
    return noise * g * 0.6


# ───────────────────────────────────────────── 트랙 렌더

MELODIC_VOICE = {
    'Rhodes': voice_rhodes, 'Guitar (16th chops)': voice_guitar,
    'Bass': voice_bass, 'Celtic Harp': voice_harp,
}
PAN = {
    'Rhodes': -0.15, 'Guitar (16th chops)': 0.30, 'Bass': 0.0,
    'Celtic Harp': 0.35, 'Strings': 0.15, 'Signature Whistle': -0.05, 'Drums': 0.0,
}
TARGET_RMS = {                                # 상대 밸런스 (드럼·베이스가 뼈대, 나머지는 그 위에)
    'Drums': 0.22, 'Bass': 0.20, 'Rhodes': 0.11, 'Guitar (16th chops)': 0.085,
    'Celtic Harp': 0.075, 'Strings': 0.095, 'Signature Whistle': 0.13,
}


def cc_curve(cc_events, num, tick_to_sec, note_start_tick, note_end_tick, n_samples, sr, default=0.0):
    evs = cc_events.get(num, [])
    if not evs:
        return np.full(n_samples, default)
    times = np.array([tick_to_sec(tk) for tk, _v in evs])
    vals = np.array([v for _tk, v in evs], dtype=float)
    t0 = tick_to_sec(note_start_tick)
    local_t = np.arange(n_samples) / sr
    return np.interp(local_t, times - t0, vals, left=vals[0] if len(vals) else default,
                      right=vals[-1] if len(vals) else default)


def render_track(track, tick_to_sec, total_samples):
    name = track['name']
    buf = np.zeros((total_samples, 2), dtype=np.float64)
    if not track['notes']:
        return buf, name
    pan_l, pan_r = pan_gains(PAN.get(name, 0.0))

    for st, en, pitch, vel in track['notes']:
        t0 = tick_to_sec(st); t1 = tick_to_sec(en)
        dur = max(t1 - t0, 0.03)
        tail = 0.6 if name in ('Celtic Harp', 'Strings', 'Rhodes') else 0.15
        n = int(SR * (dur + tail))
        t = np.arange(n) / SR
        start_sample = int(round(t0 * SR))

        if name == 'Drums':
            sig = voice_drum(pitch, dur, vel)
            n = len(sig)
        elif name == 'Signature Whistle':
            vib = cc_curve(track['cc'], 1, tick_to_sec, st, en, n, SR, default=0.0)
            breath_raw = cc_curve(track['cc'], 11, tick_to_sec, st, en, n, SR, default=100.0)
            breath = 0.55 + 0.45 * (breath_raw / 127.0)
            freq = midi_freq(pitch)
            sig = voice_whistle(t, dur, freq, vel, vib_depth_t=vib, breath_t=breath)
        elif name == 'Strings':
            freq = midi_freq(pitch)
            sig = (voice_strings(t, dur, freq, vel, detune=+0.0015)
                   + voice_strings(t, dur, freq, vel, detune=-0.0015)) * 0.6
        else:
            fn = MELODIC_VOICE.get(name)
            if fn is None:
                continue
            freq = midi_freq(pitch)
            sig = fn(t, dur, freq, vel)

        end_sample = start_sample + n
        if end_sample > total_samples:
            n = total_samples - start_sample
            if n <= 0:
                continue
            sig = sig[:n]
            end_sample = total_samples
        buf[start_sample:end_sample, 0] += sig * pan_l
        buf[start_sample:end_sample, 1] += sig * pan_r
    return buf, name


def reverb(mix, sr, seconds=1.3, wet=0.16):
    """합성 임펄스 응답과의 FFT 컨볼루션 — 실시간 IIR 없이 전체를 한 번에 처리한다."""
    n_ir = int(sr * seconds)
    t = np.arange(n_ir) / sr
    ir = RNG.standard_normal(n_ir) * np.exp(-t / (seconds / 4.5))
    ir = smooth(ir, 6)
    ir[0] += 1.0                                   # dry 스파이크 보존
    ir /= np.sqrt(np.sum(ir ** 2)) + 1e-9
    n_fft = 1
    total = len(mix) + n_ir
    while n_fft < total:
        n_fft *= 2
    IR = np.fft.rfft(ir, n_fft)
    out = np.empty_like(mix)
    for ch in range(mix.shape[1]):
        X = np.fft.rfft(mix[:, ch], n_fft)
        wet_sig = np.fft.irfft(X * IR, n_fft)[:len(mix)]
        out[:, ch] = mix[:, ch] * (1 - wet) + wet_sig * wet
    return out


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else f'05_instruments_rev{REV:02d}.mid'
    dst = sys.argv[2] if len(sys.argv) > 2 else src.replace('.mid', '.mp3')

    div, bpm, tracks = parse_midi(src)
    sec_per_tick = 60.0 / bpm / div
    tick_to_sec = lambda tk: tk * sec_per_tick

    last_tick = max((en for tr in tracks for _s, en, _p, _v in tr['notes']), default=0)
    total_samples = int(SR * (tick_to_sec(last_tick) + 3.0))
    print(f'입력 {src}: {len(tracks)}트랙, {bpm:.0f}BPM, 길이 {tick_to_sec(last_tick):.1f}초 '
          f'-> 렌더 {total_samples/SR:.1f}초')

    mix = np.zeros((total_samples, 2), dtype=np.float64)
    for tr in tracks:
        buf, name = render_track(tr, tick_to_sec, total_samples)
        n_notes = len(tr['notes'])
        if n_notes == 0:
            continue
        active = buf[np.abs(buf).sum(axis=1) > 1e-9]
        rms = float(np.sqrt(np.mean(active ** 2))) if len(active) else 0.0
        target = TARGET_RMS.get(name)
        gain = (target / rms) if (target and rms > 1e-6) else 1.0
        gain = min(gain, 8.0)                       # 폭주 방지
        mix += buf * gain
        print(f'  {name:24s} 노트 {n_notes:5d}  RMS {rms:.4f} -> gain {gain:.2f}')

    mix = reverb(mix, SR)

    # 버스 글루 압축 — 파트가 따로 놀지 않고 한 밴드로 들리게. 포락선을 부드럽게 따라간다.
    env = smooth(np.abs(mix).max(axis=1), int(SR * 0.02))
    thr = np.percentile(env, 70)
    gain = np.where(env > thr, (thr / np.maximum(env, 1e-9)) ** 0.35, 1.0)
    mix *= smooth(gain, int(SR * 0.06))[:, None]

    # 스테레오 폭 — 사이드 성분만 아주 살짝 넓힌다(모노 호환 유지: 카페 천장 스피커 전제)
    mid = (mix[:, 0] + mix[:, 1]) * 0.5
    side = (mix[:, 0] - mix[:, 1]) * 0.5 * 1.18
    mix = np.stack([mid + side, mid - side], axis=1)

    # 아웃트로 자연 페이드 위에 마지막 1.5초 추가 페이드로 클릭 방지
    fade = min(int(SR * 1.5), total_samples)
    mix[-fade:] *= np.linspace(1.0, 0.0, fade)[:, None]
    mix[:int(SR * 0.02)] *= np.linspace(0.0, 1.0, int(SR * 0.02))[:, None]

    peak = np.max(np.abs(mix))
    if not np.isfinite(peak) or peak < 1e-9:
        raise SystemExit(f'렌더 실패 — peak={peak}')
    mix = mix / peak * 0.89                         # 피크 정규화 (-1dBFS 근처)
    mix = np.tanh(mix * 1.05) / np.tanh(1.05)        # 안전용 소프트 리미터
    nan = int(np.sum(~np.isfinite(mix)))
    if nan:
        raise SystemExit(f'NaN/Inf {nan}개 발생 — 렌더 중단')

    rms_db = 20 * np.log10(np.sqrt(np.mean(mix ** 2)) + 1e-12)
    print(f'최종 피크 {20*np.log10(peak+1e-12):.1f}dBFS(정규화 전) -> RMS 약 {rms_db:.1f}dBFS')

    # 섹션별 RMS — 편곡 의도(다이내믹 곡선)대로 실제로 움직였는지 인코딩 전에 확인한다
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('mg', 'moongate_build.py')
        mgmod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mgmod)
        spb = 4 * 60.0 / mgmod.BPM
        print('\n섹션별 RMS (정규화 후 기준)')
        for label, rng in mgmod.SECTIONS.items():
            a = int(SR * (rng[0] - 1) * spb)
            b2 = int(SR * rng[-1] * spb)
            seg = mix[a:min(b2, len(mix))]
            if len(seg) == 0:
                continue
            db = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-12)
            bar = '#' * max(0, int(db + 40))
            print(f'  {label:9s} {db:6.1f}dBFS  {bar}')
    except Exception as e:
        print(f'(섹션 리포트 생략: {e})')

    clipped = int(np.sum(np.abs(mix) >= 0.999))
    print(f'\n0.999 이상 샘플: {clipped}개 / {mix.size}개')

    pcm = (mix * 32767.0).astype('<i2').tobytes()
    try:
        import lameenc                        # requirements: pip install numpy lameenc
        enc = lameenc.Encoder()
        enc.set_bit_rate(192)
        enc.set_in_sample_rate(SR)
        enc.set_channels(2)
        enc.set_quality(2)
        data = enc.encode(pcm) + enc.flush()
        with open(dst, 'wb') as f:
            f.write(data)
        print(f'-> {dst}  ({len(data)/1024:.0f} KB)')
    except ImportError:
        import wave
        wav_dst = dst.rsplit('.', 1)[0] + '.wav'
        with wave.open(wav_dst, 'wb') as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(pcm)
        print(f'lameenc 없음 (pip install lameenc) — 대신 WAV 저장: {wav_dst} '
              f'({len(pcm)/1024:.0f} KB)')


if __name__ == '__main__':
    main()
