import { describe, expect, it } from 'vitest';
import { bpmForRpm, rpmForWatts, wattsForRpm } from '../src/domain/airbikeCurve';
import { PROGRAMS, fmtRpm, programById, resolveProgram, targetWatts, totalSeconds, workSeconds } from '../src/domain/programs';
import { locate } from '../src/domain/timeline';
import { WorkoutEngine } from '../src/domain/engine';
import { cueFor, pacerDecision, type PacerConfig } from '../src/domain/pacer';
import { classifyEvent } from '../src/intervals/parse';
import { buildTcx } from '../src/domain/tcx';

describe('Assault curve', () => {
  it('matches the documented targets', () => {
    expect(Math.round(wattsForRpm(54.5))).toBe(223);
    expect(Math.round(wattsForRpm(55))).toBe(228);
    expect(Math.round(wattsForRpm(56))).toBe(240);
    expect(Math.round(wattsForRpm(57))).toBe(252);
    expect(Math.round(wattsForRpm(40))).toBe(99);
    expect(Math.round(wattsForRpm(44))).toBe(127);
    expect(Math.round(wattsForRpm(100))).toBe(1205);
  });
  it('extrapolates cubically above 100 RPM', () => {
    expect(Math.round(wattsForRpm(110))).toBe(1604);
  });
  it('inverts', () => {
    for (const r of [20, 44.5, 54.5, 66, 95]) expect(rpmForWatts(wattsForRpm(r))).toBeCloseTo(r, 6);
    expect(rpmForWatts(0)).toBe(0);
  });
  it('metronome is 2 beats per revolution', () => {
    expect(bpmForRpm(55)).toBe(110);
  });
});

describe('programs', () => {
  const dur = (id: string) => totalSeconds(programById(id)!);
  it('totals match the Armory primer', () => {
    expect(dur('n4x4')).toBe(33 * 60);
    expect(dur('v5x3')).toBe(30 * 60);
    expect(dur('p6x2')).toBe(29 * 60);
    expect(dur('b6030')).toBe(33 * 60);
    expect(workSeconds(programById('n4x4')!)).toBe(16 * 60);
    expect(workSeconds(programById('v5x3')!)).toBe(15 * 60);
    expect(workSeconds(programById('p6x2')!)).toBe(12 * 60);
    expect(workSeconds(programById('b6030')!)).toBe(16 * 60);
  });
  it('4th recovery of Norwegian is the cool-down; Billat has 7 floats per set', () => {
    const n = programById('n4x4')!.segments;
    expect(n[n.length - 1].kind).toBe('cooldown');
    const b = programById('b6030')!.segments;
    expect(b.filter((g) => g.kind === 'float').length).toBe(14);
    expect(b.filter((g) => g.label === 'SET REST').length).toBe(1);
  });
  it('tabata / sprint are 4:00 and 4:00', () => {
    expect(dur('tabata')).toBe(240);
    expect(dur('sprint1020')).toBe(240);
  });
  it('ids are unique and Z5 protocols are ordered p6x2 -> v5x3 -> b6030 -> n4x4', () => {
    expect(new Set(PROGRAMS.map((p) => p.id)).size).toBe(PROGRAMS.length);
    expect(PROGRAMS.filter((p) => p.group === 'z5').map((p) => p.id)).toEqual(['p6x2', 'v5x3', 'b6030', 'n4x4']);
    expect([56, 55, 57, 54.5].map(fmtRpm)).toEqual(['56', '55', '57', '54.5']);
  });
  it('resolveProgram scales by FTP and shifts work watts cleanly with live +/-5 W trim', () => {
    const p = programById('p6x2')!;
    const baseWork = resolveProgram(p, 182, 0).segments.find((g) => g.kind === 'work')!;
    expect(targetWatts(baseWork)).toBe(240);
    const trimmedDown = resolveProgram(p, 182, -10).segments.find((g) => g.kind === 'work')!;
    expect(targetWatts(trimmedDown)).toBe(230);
    const trimmedUp = resolveProgram(p, 182, 5).segments.find((g) => g.kind === 'work')!;
    expect(targetWatts(trimmedUp)).toBe(245);
    const scaled = resolveProgram(p, 165, 0);
    const scaledWork = scaled.segments.find((g) => g.kind === 'work')!;
    expect(targetWatts(scaledWork)).toBe(Math.round(240 * (165 / 182)));
    expect(scaled.segments.find((g) => g.kind === 'rest')!.rpm!).toBeGreaterThanOrEqual(36);
  });
});

describe('timeline + engine', () => {
  const segs = programById('n4x4')!.segments;
  it('locates segments and the next transition', () => {
    const a = locate(segs, 0);
    expect(a.index).toBe(0);
    expect(a.remaining).toBe(300);
    const b = locate(segs, 300);
    expect(b.seg!.kind).toBe('work');
    expect(b.into).toBe(0);
    expect(b.next!.kind).toBe('rest');
    expect(locate(segs, 33 * 60).complete).toBe(true);
  });
  it('excludes paused time', () => {
    const e = new WorkoutEngine(programById('n4x4')!);
    e.start(1000);
    expect(e.elapsed(11000)).toBe(10);
    e.pause(11000);
    expect(e.elapsed(500000)).toBe(10);
    e.resume(500000);
    expect(e.elapsed(505000)).toBe(15);
    e.finish(505000);
    expect(e.state).toBe('finished');
    expect(e.elapsed(900000)).toBe(15);
  });
});

describe('pacer (metronome)', () => {
  const p = programById('n4x4')!;
  const cfg = (o: Partial<PacerConfig> = {}): PacerConfig => ({ mode: 'transitions', lead: 5, tail: 15, autoMute: false, ...o });
  const at = (t: number, c: PacerConfig, enabled = true, locked = false) => pacerDecision(p, locate(p.segments, t), c, enabled, locked);

  it('is silent when disabled', () => {
    expect(at(295, cfg(), false).play).toBe(false);
    expect(at(295, cfg({ mode: 'whole' }), false).play).toBe(false);
  });
  it('transitions mode: lead-in at the UPCOMING tempo, then 15 s after', () => {
    expect(at(100, cfg()).play).toBe(false); // mid warm-up
    expect(at(294, cfg()).play).toBe(false); // 6 s out
    const lead = at(295, cfg()); // 5 s out
    expect(lead.play).toBe(true);
    expect(lead.bpm).toBe(109); // 2 x 54.5, tempo of the work block that is coming
    expect(at(300, cfg()).bpm).toBe(109);
    expect(at(314.9, cfg()).play).toBe(true);
    expect(at(315.1, cfg()).play).toBe(false);
  });
  it('does not fire for the start of the workout or the end', () => {
    expect(at(0, cfg()).play).toBe(false);
    expect(at(5, cfg()).play).toBe(false);
    expect(at(33 * 60 - 2, cfg()).play).toBe(false);
  });
  it('auto-mute stops the tail once locked in, but never the lead-in', () => {
    expect(at(305, cfg({ autoMute: true }), true, true).play).toBe(false);
    expect(at(305, cfg({ autoMute: true }), true, false).play).toBe(true);
    expect(at(296, cfg({ autoMute: true }), true, true).play).toBe(true);
  });
  it('whole mode plays continuously and switches tempo in the lead window', () => {
    expect(at(100, cfg({ mode: 'whole' })).bpm).toBe(88); // warm-up 44 RPM
    expect(at(296, cfg({ mode: 'whole' })).bpm).toBe(109);
  });
  it('free ride only plays in whole mode', () => {
    const f = programById('free')!;
    const loc = locate(f.segments, 60);
    expect(pacerDecision(f, loc, cfg(), true, false).play).toBe(false);
    expect(pacerDecision(f, loc, cfg({ mode: 'whole' }), true, false).bpm).toBe(89);
  });
  it('no tempo for full-rest segments', () => {
    const b = programById('b6030')!;
    const idx = b.segments.findIndex((g) => g.label === 'SET REST');
    const start = b.segments.slice(0, idx).reduce((a, g) => a + g.seconds, 0);
    expect(pacerDecision(b, locate(b.segments, start + 2), cfg(), true, false).play).toBe(false);
  });
});

describe('cadence cues', () => {
  const work = programById('n4x4')!.segments[1];
  const float = programById('b6030')!.segments[2];
  it('on target / hot / low', () => {
    expect(cueFor(work, 54.5, 30).level).toBe('ok');
    expect(cueFor(work, 55.9, 30).level).toBe('ok');
    const hot = cueFor(work, 58, 5);
    expect(hot.level).toBe('bad');
    expect(hot.chirp).toBe(true);
    expect(hot.text).toContain('TOO HOT');
    expect(cueFor(work, 58, 60).chirp).toBe(false); // only the first 15 s
    expect(cueFor(work, 50, 30).level).toBe('warn');
  });
  it('float floor protects the moving fan', () => {
    expect(float.kind).toBe('float');
    expect(cueFor(float, 30, 5).text).toContain('KEEP FAN SPINNING');
    expect(cueFor(float, 40, 5).level).toBe('ok');
  });
  it('all-out blocks only demand a minimum', () => {
    const t = programById('tabata')!.segments[0];
    expect(cueFor(t, 90, 3).level).toBe('ok');
    expect(cueFor(t, 50, 3).level).toBe('warn');
  });
});

describe('calendar classification', () => {
  const c = (name: string, type?: string) => classifyEvent({ name, type });
  it('maps the user\'s naming convention', () => {
    expect(c('AirBike Z5 — Norwegian 4×4')).toEqual({ equipment: 'airbike', programId: 'n4x4', zone: 'Z5' });
    expect(c('AirBike Z2')).toEqual({ equipment: 'airbike', programId: 'z2', zone: 'Z2' });
    expect(c('Spin Bike Z2').equipment).toBe('spin');
    expect(c('Bowflex C7 Z5 — Cardiac Power 6×2').equipment).toBe('spin');
    expect(c('Treadmill Z2').equipment).toBe('treadmill');
    expect(c('Upper Body Strength').equipment).toBe('strength');
    expect(c('Morning thing', 'WeightTraining').equipment).toBe('strength');
  });
  it('plain "AirBike Z5" opens the bike but leaves the protocol to you', () => {
    expect(c('AirBike Z5')).toEqual({ equipment: 'airbike', programId: null, zone: 'Z5' });
  });
  it('other airbike protocols', () => {
    expect(c('AirBike Billat 60/30').programId).toBe('b6030');
    expect(c('AirBike Tabata 20/10').programId).toBe('tabata');
    expect(c('AirBike Sprint 10/20').programId).toBe('sprint1020');
    expect(c('AirBike Classic VO2 5×3').programId).toBe('v5x3');
  });
  it('unknown stays unmatched', () => {
    expect(c('Dentist').equipment).toBeNull();
  });
});

describe('TCX', () => {
  it('exports mechanical kJ as calories and schema-ordered trackpoints', () => {
    const xml = buildTcx(
      { startEpochMs: Date.UTC(2026, 10, 14, 20, 0, 0), seconds: 2, kj: 56.7, samples: [
        { t: 0, w: 100, cad: 40, hr: 90, dist: 0 }, { t: 1, w: 227.6, cad: 54.5, hr: 0, dist: 4.2 } ] },
      'AirBike Z5 <test>',
    );
    expect(xml).toContain('<Calories>57</Calories>');
    expect(xml).toContain('<ns3:Watts>228</ns3:Watts>');
    expect(xml).toContain('&lt;test&gt;');
    expect(xml.match(/<Trackpoint>/g)!.length).toBe(2);
    expect(xml.match(/<HeartRateBpm>/g)!.length).toBe(1); // hr=0 omitted
    expect(xml.indexOf('<DistanceMeters>4.2')).toBeLessThan(xml.indexOf('<Cadence>55'));
  });
});
