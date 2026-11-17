import { describe, expect, it } from 'vitest';
import { LineBuffer, parseBikeLine, parseHeartRate } from '../src/ble/parsers';
import { summarize } from '../src/workout/summary';

describe('BLE parsers', () => {
  it('Polar H10: uint8 and uint16 heart rate', () => {
    expect(parseHeartRate(new DataView(new Uint8Array([0x10, 142]).buffer))).toBe(142);
    expect(parseHeartRate(new DataView(new Uint8Array([0x11, 0x2c, 0x01]).buffer))).toBe(300);
  });
  it('NUS line buffer reassembles frames split across notifications', () => {
    const lb = new LineBuffer();
    const enc = (s: string) => new TextEncoder().encode(s);
    expect(lb.push(enc('{"dev":"airbike","w":12'))).toEqual([]);
    expect(lb.push(enc('7.0,"cad":44.0}\n{"dev":"ai'))).toEqual(['{"dev":"airbike","w":127.0,"cad":44.0}']);
    expect(lb.push(enc('rbike","w":1,"cad":2}\n'))).toEqual(['{"dev":"airbike","w":1,"cad":2}']);
  });
  it('AirBike frame', () => {
    const f = parseBikeLine('{"dev":"airbike","w":127.0,"cad":44.0,"fan":488.8,"spd":16.87,"dist":452.1,"kcal":15.42,"pls":1042}');
    expect(f).toEqual({ w: 127, cad: 44, fan: 488.8, dist: 452.1, kcal: 15.42, spd: 16.87, pls: 1042 });
    const full = parseBikeLine('{"dev":"airbike","w":200,"cad":52,"rfan":580,"dt":19500,"ratio":11.11}');
    expect(full).toEqual({ w: 200, cad: 52, rfan: 580, dist: 0, kcal: 0, spd: 0, dt: 19500, ratio: 11.11 });
    expect(parseBikeLine('garbage')).toBeNull();
    expect(parseBikeLine('{"dev":"treadmill","w":1,"cad":2}')).toBeNull();
    expect(parseBikeLine('{"foo":1}')).toBeNull();
  });
});

describe('summarize', () => {
  it('computes kJ, averages and maxima, ignoring HR dropouts', () => {
    const s = summarize([
      { t: 0, w: 100, cad: 40, hr: 100, dist: 0, kcal: 0, tr: 0 },
      { t: 1, w: 300, cad: 60, hr: 0, dist: 1, kcal: 0, tr: 0 },
      { t: 2, w: 200, cad: 50, hr: 140, dist: 2, kcal: 0, tr: 0 },
    ]);
    expect(s.seconds).toBe(3);
    expect(s.kj).toBeCloseTo(0.6);
    expect(s.avgW).toBeCloseTo(200);
    expect(s.maxW).toBe(300);
    expect(s.avgHr).toBeCloseTo(120);
    expect(s.maxHr).toBe(140);
  });
});
