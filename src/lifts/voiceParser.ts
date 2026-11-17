export type VoiceIntent =
  | { kind: 'arm_wake' }
  | { kind: 'log_current'; weight?: number; reps?: number; seconds?: number; bodyweight?: boolean; rpe?: number }
  | { kind: 'set_only'; weight?: number; reps?: number }
  | { kind: 'start_hold'; seconds?: number }
  | { kind: 'next_ex' }
  | { kind: 'prev_ex' }
  | { kind: 'jump_ex'; target: string }
  | { kind: 'undo' }
  | { kind: 'rest_skip' }
  | { kind: 'rest_add'; deltaSec: number }
  | { kind: 'pause_workout' }
  | { kind: 'resume_workout' }
  | { kind: 'finish_workout' }
  | { kind: 'add_set' }
  | { kind: 'add_set_prev_ex' }
  | { kind: 'delete_empty_sets' };

const ONES: Record<string, number> = {
  zero: 0, oh: 0, o: 0, one: 1, two: 2, three: 3, four: 4, five: 5,
  six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12,
  thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19,
};
const TENS: Record<string, number> = {
  twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60, seventy: 70, eighty: 80, ninety: 90,
};

function parseSpokenNumberTokens(toks: string[], start: number): { value: number; end: number } | null {
  const t0 = toks[start];
  if (!t0) return null;

  // "two twenty five" / "one eighty five" / "two oh five" -> 225 / 185 / 205
  if (ONES[t0] != null && ONES[t0] >= 1 && ONES[t0] <= 9) {
    const t1 = toks[start + 1];
    const t2 = toks[start + 2];
    if (t1 === 'hundred') {
      let val = ONES[t0] * 100;
      let idx = start + 2;
      if (toks[idx] === 'and') idx++;
      if (toks[idx] && TENS[toks[idx]] != null) {
        val += TENS[toks[idx]];
        idx++;
        if (toks[idx] && ONES[toks[idx]] != null && ONES[toks[idx]] < 10) {
          val += ONES[toks[idx]];
          idx++;
        }
      } else if (toks[idx] && ONES[toks[idx]] != null) {
        val += ONES[toks[idx]];
        idx++;
      }
      return { value: val, end: idx };
    }
    if (t1 && TENS[t1] != null) {
      let val = ONES[t0] * 100 + TENS[t1];
      let idx = start + 2;
      if (t2 && ONES[t2] != null && ONES[t2] >= 1 && ONES[t2] <= 9) {
        val += ONES[t2];
        idx = start + 3;
      }
      return { value: val, end: idx };
    }
    if ((t1 === 'oh' || t1 === 'o') && t2 && ONES[t2] != null && ONES[t2] >= 1 && ONES[t2] <= 9) {
      return { value: ONES[t0] * 100 + ONES[t2], end: start + 3 };
    }
    if (t1 && ['ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen'].includes(t1)) {
      return { value: ONES[t0] * 100 + ONES[t1], end: start + 2 };
    }
  }

  // "twenty five" -> 25
  if (TENS[t0] != null) {
    let val = TENS[t0];
    let idx = start + 1;
    const t1 = toks[idx];
    if (t1 && ONES[t1] != null && ONES[t1] >= 1 && ONES[t1] <= 9) {
      val += ONES[t1];
      idx++;
    }
    return { value: val, end: idx };
  }

  if (ONES[t0] != null && t0 !== 'oh' && t0 !== 'o') {
    return { value: ONES[t0], end: start + 1 };
  }
  return null;
}

/** Normalizes spoken numbers (`"two twenty five"`, `"eight point five"`) and Gym STT homophones (`"four 8"`, `"for ate"`). */
export function normalizeVoiceText(raw: string): string {
  let s = raw.toLowerCase()
    .replace(/[—–\-/,]+/g, ' ')
    .replace(/['’]/g, '')
    .replace(/\s+/g, ' ')
    .trim();

  // Preserve compound label words before number expansion when explicitly tagged (e.g. "exercise two b")
  s = s.replace(/\b(point|and a half)\b/g, (m) => (m === 'point' ? ' __pt__ ' : ' __half__ '));

  const toks = s.split(/\s+/).filter(Boolean);
  const out: string[] = [];
  for (let i = 0; i < toks.length; ) {
    const t = toks[i];
    if (/^\d+(?:\.\d+)?$/.test(t)) {
      out.push(t);
      i++;
      continue;
    }
    // Leave "for", "four" alone here if acting as connector; convert explicit "ate" -> 8 after "for/by/x"
    if (t === 'ate' && out.length > 0 && ['for', 'by', 'x', 'times', 'did', 'got'].includes(out[out.length - 1])) {
      out.push('8');
      i++;
      continue;
    }
    if (t === 'for') {
      out.push(t);
      i++;
      continue;
    }
    if (t === 'four' && out.length > 0 && /^\d+(?:\.\d+)?$/.test(out[out.length - 1]) && toks[i + 1] && (ONES[toks[i + 1]] != null || /^\d+$/.test(toks[i + 1]))) {
      out.push('for');
      i++;
      continue;
    }
    const parsed = parseSpokenNumberTokens(toks, i);
    if (parsed) {
      out.push(String(parsed.value));
      i = parsed.end;
      continue;
    }
    out.push(t);
    i++;
  }

  let joined = out.join(' ')
    .replace(/(\d+)\s+__pt__\s+(\d+)/g, '$1.$2')
    .replace(/(\d+)\s+__half__/g, '$1.5')
    .replace(/__pt__|__half__/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

  // Separator homophones strictly between two numeric values
  joined = joined
    .replace(/\b(\d+(?:\.\d+)?)\s*(?:buy|bye)\s+(\d+)\b/g, '$1 by $2')
    .replace(/\b(\d+(?:\.\d+)?)\s*to\s+(\d+)\b(?![a-d])/g, '$1 for $2')
    .replace(/\bfor\s+ate\b/g, 'for 8');
  return joined;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function parseBodytail(tailRaw: string): VoiceIntent | null {
  let tail = tailRaw.trim().replace(/^[.,!?:;\s]+|[.,!?:;\s]+$/g, '');
  if (!tail) return { kind: 'arm_wake' };

  if (/^(?:undo|uncheck|undo\s+last|cancel(?:\s+last)?)$/.test(tail)) return { kind: 'undo' };
  if (/^(?:check|log|done|log\s+set|check\s+set|mark\s+done|complete(?:\s+set)?|yep|got\s+it)$/.test(tail)) {
    return { kind: 'log_current' };
  }

  const holdM = tail.match(/^(?:start|begin)\s+(?:hold|timer|countdown)(?:\s+(\d+)\s*(?:s|sec|secs|seconds)?)?$/);
  if (holdM) {
    return { kind: 'start_hold', ...(holdM[1] ? { seconds: Number(holdM[1]) } : {}) };
  }

  if (/^(?:skip|clear|end|stop)\s+rest$/.test(tail)) return { kind: 'rest_skip' };
  const addRestM = tail.match(/^(?:add|plus)\s+(\d+)\s*(?:s|sec|secs|seconds)?(?:\s+rest)?$/);
  if (addRestM) return { kind: 'rest_add', deltaSec: Number(addRestM[1]) };
  if (/^more\s+rest$/.test(tail)) return { kind: 'rest_add', deltaSec: 30 };
  const subRestM = tail.match(/^(?:minus|subtract)\s+(\d+)\s*(?:s|sec|secs|seconds)?(?:\s+rest)?$/);
  if (subRestM) return { kind: 'rest_add', deltaSec: -Number(subRestM[1]) };

  if (/^pause(?:\s+workout)?$/.test(tail)) return { kind: 'pause_workout' };
  if (/^resume(?:\s+workout)?$/.test(tail)) return { kind: 'resume_workout' };
  if (/^(?:finish|end|complete)\s+workout$/.test(tail)) return { kind: 'finish_workout' };

  if (/^(?:(?:add(?:\s+a|\s+1|\s+another)?\s+set)|(?:(?:1|one|another|extra)\s+(?:more\s+)?set))\s+(?:(?:to|on|of|for)\s+)?(?:the\s+)?(?:last|previous|prev|prior)\s+exercise$/.test(tail)) {
    return { kind: 'add_set_prev_ex' };
  }
  if (/^(?:add(?:\s+a|\s+1|\s+another)?\s+set|(?:1|one|another|extra)\s+(?:more\s+)?set)(?:\s+(?:here|to\s+(?:this|current)(?:\s+exercise)?))?$/.test(tail)) {
    return { kind: 'add_set' };
  }
  if (/^(?:delete|remove|drop|clear)\s+(?:all\s+|the\s+)?(?:empty|unlogged|unfinished|remaining)\s+sets?$/.test(tail)) {
    return { kind: 'delete_empty_sets' };
  }

  if (/^(?:next|next\s+exercise|skip\s+exercise)$/.test(tail)) return { kind: 'next_ex' };
  if (/^(?:prev|previous|previous\s+exercise|back\s+exercise)$/.test(tail)) return { kind: 'prev_ex' };

  const jumpM = tail.match(/^(?:go\s+to|jump\s+to|switch\s+to|move\s+to|exercise)\s+(.+)$/);
  if (jumpM && jumpM[1].trim()) return { kind: 'jump_ex', target: jumpM[1].trim() };
  const bareLabelM = tail.match(/^([1-9]\s*[a-d])$/);
  if (bareLabelM) return { kind: 'jump_ex', target: bareLabelM[1].replace(/\s+/g, '') };

  // Extract optional trailing/embedded RPE ("... rpe 8.5")
  let rpe: number | undefined;
  const rpeM = tail.match(/(?:^|\s)(?:@|at\s+)?rpe\s*(\d+(?:\.\d+)?)\b/);
  if (rpeM) {
    const val = Number(rpeM[1]);
    if (val >= 6 && val <= 10) rpe = Math.round(val * 2) / 2;
    tail = (tail.slice(0, rpeM.index) + tail.slice((rpeM.index ?? 0) + rpeM[0].length)).trim();
  }

  // Weight-only or Reps-only preview updates (no auto-check)
  const setW = tail.match(/^(?:set\s+)?weight\s+(?:to\s+)?(\d+(?:\.\d+)?)(?:\s*(?:lb|lbs|pound|pounds|kg|kgs|kilo|kilos))?$/);
  if (setW) return { kind: 'set_only', weight: Number(setW[1]) };
  const setR = tail.match(/^(?:set\s+)?reps\s+(?:to\s+)?(\d+)$/);
  if (setR) return { kind: 'set_only', reps: Number(setR[1]) };

  // Bodyweight + reps ("bodyweight for 12", "bw 10")
  const bwM = tail.match(/^(?:body\s*weight|bw)\s*(?:for|by|x|times|reps)?\s*(\d+)(?:\s*reps)?$/);
  if (bwM) {
    return { kind: 'log_current', bodyweight: true, weight: 0, reps: Number(bwM[1]), ...(rpe != null ? { rpe } : {}) };
  }

  // Timed hold logging ("45 seconds", "hold 60 sec done")
  const secM = tail.match(/^(?:hold\s+)?(\d+)\s*(?:s|sec|secs|seconds)(?:\s+(?:done|check|log))?$/);
  if (secM) {
    return { kind: 'log_current', seconds: Number(secM[1]), ...(rpe != null ? { rpe } : {}) };
  }

  // Weight + reps ("225 for 8", "225 by 8", "225 x 8", "225 lbs for 8 reps")
  const wrM = tail.match(/^(\d+(?:\.\d+)?)\s*(?:lb|lbs|pound|pounds|kg|kgs|kilo|kilos)?\s*(?:for|by|x|times|at|@)?\s*(\d+)(?:\s*reps?)?$/);
  if (wrM) {
    return { kind: 'log_current', weight: Number(wrM[1]), reps: Number(wrM[2]), ...(rpe != null ? { rpe } : {}) };
  }

  // Reps at weight ("8 reps at 225", "10 at 185 lbs")
  const rwM = tail.match(/^(\d+)\s*(?:reps?)?\s*(?:at|with|@|of)\s*(\d+(?:\.\d+)?)(?:\s*(?:lb|lbs|pound|pounds|kg|kgs|kilo|kilos))?$/);
  if (rwM) {
    return { kind: 'log_current', weight: Number(rwM[2]), reps: Number(rwM[1]), ...(rpe != null ? { rpe } : {}) };
  }

  // Reps-only logging using pre-filled / current weight ("10 reps", "for 8", "got 12")
  const rOnlyM = tail.match(/^(?:for|did|got)?\s*(\d+)\s*reps$/) ?? tail.match(/^(?:for|did|got)\s+(\d+)$/);
  if (rOnlyM) {
    return { kind: 'log_current', reps: Number(rOnlyM[1]), ...(rpe != null ? { rpe } : {}) };
  }

  return null;
}

/**
 * Parses a speech transcript gated by the configured wake word (e.g. `"armory"`).
 * When `armed` is true (user already said the wake word within the active window),
 * bare command tails are accepted directly.
 */
export function parseVoiceTranscript(
  raw: string,
  opts: { wakeWord?: string; armed?: boolean; requireWake?: boolean } = {},
): VoiceIntent | null {
  const norm = normalizeVoiceText(raw);
  if (!norm) return null;

  const wakeRaw = (opts.wakeWord ?? 'armory').trim().toLowerCase() || 'armory';
  const wakeAlts = wakeRaw === 'armory' ? ['armory', 'armoury', 'armory fit'] : [wakeRaw];
  const wakeRe = new RegExp(`\\b(?:hey\\s+|ok\\s+|okay\\s+)?(?:${wakeAlts.map(escapeRe).join('|')})\\b`, 'gi');

  let lastEnd = -1;
  let m: RegExpExecArray | null;
  while ((m = wakeRe.exec(norm)) !== null) {
    lastEnd = m.index + m[0].length;
  }

  if (lastEnd >= 0) {
    const tail = norm.slice(lastEnd).trim();
    if (!tail) return { kind: 'arm_wake' };
    return parseBodytail(tail);
  }

  if (opts.requireWake === false || opts.armed) {
    return parseBodytail(norm);
  }
  return null;
}
