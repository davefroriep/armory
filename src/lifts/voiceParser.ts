export type VoiceIntent =
  | { kind: 'arm_wake' }
  | { kind: 'cancel_listen' }
  | { kind: 'log_current'; weight?: number; reps?: number; seconds?: number; bodyweight?: boolean; rpe?: number }
  | { kind: 'set_only'; weight?: number; reps?: number }
  | { kind: 'start_hold'; seconds?: number }
  | { kind: 'next_ex' }
  | { kind: 'prev_ex' }
  | { kind: 'jump_ex'; target: string; setNum?: number }
  | { kind: 'jump_set'; setNum: number }
  | { kind: 'uncheck_set' }
  | { kind: 'clear_set' }
  | { kind: 'undo' }
  | { kind: 'rest_skip' }
  | { kind: 'rest_add'; deltaSec: number }
  | { kind: 'pause_workout' }
  | { kind: 'resume_workout' }
  | { kind: 'finish_workout' }
  | { kind: 'add_set' }
  | { kind: 'add_set_prev_ex' }
  | { kind: 'delete_empty_sets' }
  | { kind: 'cam_arm'; camera?: string }
  | { kind: 'cam_clip_last'; camera?: string }
  | { kind: 'cam_guest'; camera?: string }
  | { kind: 'buddy_mode'; on?: boolean }
  | { kind: 'wb_replay'; exQuery?: string; setNum?: number; slow?: boolean }
  | { kind: 'wb_switch_view' }
  | { kind: 'wb_close_replay' };

export type VoiceAliasSlot =
  | 'check_set'
  | 'reps_only'
  | 'undo'
  | 'rest_skip'
  | 'rest_more'
  | 'add_set'
  | 'next_ex'
  | 'cam_arm'
  | 'cam_clip_last'
  | 'wb_replay'
  | 'finish_workout';

export type CustomVoiceAliases = Partial<Record<VoiceAliasSlot, string[]>>;

export const VOICE_ALIAS_SLOTS: readonly { slot: VoiceAliasSlot; label: string; hint: string }[] = [
  { slot: 'check_set', label: 'Check / Complete Set', hint: 'e.g. crushed it, money, send it' },
  { slot: 'reps_only', label: 'Log Reps Only (use # for reps)', hint: 'e.g. pushed #, knocked out #' },
  { slot: 'undo', label: 'Undo Last Action', hint: 'e.g. take that back, oops' },
  { slot: 'rest_skip', label: 'Skip Rest Timer', hint: 'e.g. lets go, im ready' },
  { slot: 'rest_more', label: 'Add +30s Rest', hint: 'e.g. hold on, breather' },
  { slot: 'add_set', label: 'Add a Set', hint: 'e.g. bonus set, run it back' },
  { slot: 'next_ex', label: 'Next Exercise', hint: 'e.g. move on' },
  { slot: 'cam_arm', label: 'Record Set (Gym Cam)', hint: 'e.g. watch this, roll tape' },
  { slot: 'cam_clip_last', label: 'Clip Last Set', hint: 'e.g. save that, highlight' },
  { slot: 'wb_replay', label: 'Replay Last Set on TV', hint: 'e.g. instant replay, show that' },
  { slot: 'finish_workout', label: 'Finish Workout', hint: 'e.g. all done, wrap it up' },
];

export const VOICE_COMMAND_GROUPS: readonly { title: string; slot?: VoiceAliasSlot; examples: readonly string[]; note?: string }[] = [
  {
    title: 'Log Set (Weight + Reps)',
    examples: ['225 for 8', '225 by 8', '225 x 8', '8 reps at 225', '35s for 12', 'bodyweight for 12', '45 seconds', '185 for 10 rpe 8.5'],
    note: 'Supports spoken numbers ("two twenty five for eight", "thirty fives") and optional trailing RPE 6–10.',
  },
  {
    title: 'Log Reps Only (Keep Weight)',
    slot: 'reps_only',
    examples: ['for 10', 'by 10', 'did 5', 'got 8', 'hit 6', 'x 10', 'times 10', '10 reps'],
    note: 'Keeps the target set’s prefilled/current weight and updates reps only.',
  },
  {
    title: 'Check / Complete Set As-Is',
    slot: 'check_set',
    examples: ['check', 'done', 'log', 'got it', 'same', 'repeat', 'crushed it', 'nailed it', 'good set', 'easy', 'boom'],
  },
  {
    title: 'Preview Weight / Reps Only',
    examples: ['weight 225', 'set weight to 185', 'reps 10', 'set reps to 8'],
    note: 'Updates the input box without checking the set off yet.',
  },
  {
    title: 'Switch / Jump Exercise',
    slot: 'next_ex',
    examples: ['next', 'next exercise', 'previous exercise', 'go to bench', 'go to exercise 1, set 2', 'go to set 2', '2A', '2B'],
  },
  {
    title: 'Add / Remove Sets',
    slot: 'add_set',
    examples: ['add set', 'one more set', 'add set to last exercise', 'delete empty sets'],
  },
  {
    title: 'Rest & Hold Timers',
    slot: 'rest_skip',
    examples: ['skip rest', 'end rest', 'more rest', 'add 30 seconds', 'minus 15 seconds', 'start hold 45'],
  },
  {
    title: 'Gym Camera DVR, Buddy & Guest Clips',
    slot: 'cam_arm',
    examples: ['record', 'record this', 'record me', 'film this', 'camera', 'record rack', 'record smith', 'clip that', 'clip rack', 'clip smith', 'guest record', 'guest smith', 'buddy mode on', 'buddy mode off', 'buddy mode'],
  },
  {
    title: 'Wallboard TV Video Replay',
    slot: 'wb_replay',
    examples: ['replay', 'replay slow', 'play set 2', 'play bench set 3', 'switch views', 'close video'],
  },
  {
    title: 'Workout & Undo',
    slot: 'undo',
    examples: ['undo', 'uncheck', 'clear', 'cancel', 'nevermind', 'back', 'go back', 'pause workout', 'resume workout', 'finish workout'],
  },
];

const ONES: Record<string, number> = {
  zero: 0, oh: 0, o: 0, one: 1, two: 2, three: 3, four: 4, five: 5,
  six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12,
  thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19,
};
const TENS: Record<string, number> = {
  twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60, seventy: 70, eighty: 80, ninety: 90,
};
const PLURAL_TENS: Record<string, number> = {
  twenties: 20, thirties: 30, forties: 40, fifties: 50, sixties: 60, seventies: 70, eighties: 80, nineties: 90,
};
const DB_WEIGHT_CONNECTORS = new Set(['for', 'four', 'by', 'x', 'times', 'at', 'lb', 'lbs', 'pound', 'pounds', 'kg', 'kgs', 'kilo', 'kilos']);

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

  // "twenty five" -> 25, "thirty fives" -> 35
  if (TENS[t0] != null) {
    let val = TENS[t0];
    let idx = start + 1;
    const t1 = toks[idx];
    if (t1 === 'fives') {
      val += 5;
      idx++;
    } else if (t1 && ONES[t1] != null && ONES[t1] >= 1 && ONES[t1] <= 9) {
      val += ONES[t1];
      idx++;
    }
    return { value: val, end: idx };
  }

  if (PLURAL_TENS[t0] != null) {
    return { value: PLURAL_TENS[t0], end: start + 1 };
  }

  if ((t0 === 'fives' || t0 === 'tens' || t0 === 'fifteens') && toks[start + 1] && DB_WEIGHT_CONNECTORS.has(toks[start + 1])) {
    return { value: t0 === 'fives' ? 5 : t0 === 'tens' ? 10 : 15, end: start + 1 };
  }

  if (ONES[t0] != null && t0 !== 'oh' && t0 !== 'o') {
    return { value: ONES[t0], end: start + 1 };
  }
  return null;
}

/** Normalizes spoken numbers (`"two twenty five"`, `"eight point five"`) and Gym STT homophones (`"four 8"`, `"for ate"`). */
export function normalizeVoiceText(raw: string): string {
  // Preserve decimals like 8.5 / 22.5, strip stray sentence punctuation that would fuse tokens (e.g. "135. for" or "five.")
  let s = raw.toLowerCase()
    .replace(/[—–\-/,?!:;]+/g, ' ')
    .replace(/(?<!\d)\.|\.(?!\d)/g, ' ')
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
    const dbPluralM = t.match(/^(\d+(?:\.\d+)?)s$/);
    if (dbPluralM && toks[i + 1] && DB_WEIGHT_CONNECTORS.has(toks[i + 1])) {
      out.push(dbPluralM[1]);
      i++;
      continue;
    }
    // Leave "for", "four" alone here if acting as connector; convert explicit "ate" -> 8 after connector/verb
    if (t === 'ate' && out.length > 0 && ['for', 'by', 'x', 'times', 'did', 'got', 'hit'].includes(out[out.length - 1])) {
      out.push('8');
      i++;
      continue;
    }
    if (t === 'for') {
      out.push(t);
      i++;
      continue;
    }
    if (t === 'four') {
      const prevIsNum = out.length > 0 && /^\d+(?:\.\d+)?$/.test(out[out.length - 1]);
      const nextTok = toks[i + 1];
      const afterNext = toks[i + 2];
      const nextIsRep = !!nextTok && (
        /^\d+$/.test(nextTok) ||
        nextTok === 'ate' ||
        (ONES[nextTok] != null && nextTok !== 'oh' && nextTok !== 'o') ||
        TENS[nextTok] != null
      );
      if (prevIsNum && nextTok && (ONES[nextTok] != null || /^\d+$/.test(nextTok) || nextTok === 'ate')) {
        out.push('for');
        i++;
        continue;
      }
      if (!prevIsNum && nextIsRep && (!afterNext || afterNext === 'reps' || afterNext === 'rep' || afterNext === 'rpe')) {
        out.push('for');
        i++;
        continue;
      }
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

  // Separator homophones before numeric values
  joined = joined
    .replace(/\b(?:buy|bye)\s+(\d+)\b/g, 'by $1')
    .replace(/\b(\d+(?:\.\d+)?)\s*to\s+(\d+)\b(?![a-d])/g, '$1 for $2')
    .replace(/\bfor\s+ate\b/g, 'for 8');
  return joined;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function matchCustomVoiceAlias(
  tail: string,
  customAliases?: CustomVoiceAliases,
  rpe?: number,
): VoiceIntent | null {
  if (!customAliases) return null;
  const rpeSpread = rpe != null ? { rpe } : {};
  const ph = '__repnum__';
  for (const rawAlias of customAliases.reps_only ?? []) {
    const trimmed = rawAlias.trim();
    if (!trimmed) continue;
    const hasHash = trimmed.includes('#');
    const normAlias = normalizeVoiceText(trimmed.replace(/#/g, ` ${ph} `));
    if (!normAlias) continue;
    const re = hasHash
      ? new RegExp(`^${escapeRe(normAlias).replace(ph, '(\\d+)')}${normAlias.endsWith(ph) ? '(?:\\s*reps?)?' : ''}$`)
      : new RegExp(`^${escapeRe(normAlias)}\\s+(\\d+)(?:\\s*reps?)?$`);
    const m = tail.match(re);
    if (m) return { kind: 'log_current', reps: Number(m[1]), ...rpeSpread };
  }

  const fixedSlots: { slot: Exclude<VoiceAliasSlot, 'reps_only'>; make: () => VoiceIntent }[] = [
    { slot: 'check_set', make: () => ({ kind: 'log_current', ...rpeSpread }) },
    { slot: 'undo', make: () => ({ kind: 'undo' }) },
    { slot: 'rest_skip', make: () => ({ kind: 'rest_skip' }) },
    { slot: 'rest_more', make: () => ({ kind: 'rest_add', deltaSec: 30 }) },
    { slot: 'add_set', make: () => ({ kind: 'add_set' }) },
    { slot: 'next_ex', make: () => ({ kind: 'next_ex' }) },
    { slot: 'cam_arm', make: () => ({ kind: 'cam_arm' }) },
    { slot: 'cam_clip_last', make: () => ({ kind: 'cam_clip_last' }) },
    { slot: 'wb_replay', make: () => ({ kind: 'wb_replay' }) },
    { slot: 'finish_workout', make: () => ({ kind: 'finish_workout' }) },
  ];
  for (const { slot, make } of fixedSlots) {
    for (const rawAlias of customAliases[slot] ?? []) {
      const normAlias = normalizeVoiceText(rawAlias).replace(/^[.,!?:;\s]+|[.,!?:;\s]+$/g, '');
      if (normAlias && tail === normAlias) return make();
    }
  }
  return null;
}

function parseLeadingNoisyTail(tail: string, rpe?: number): VoiceIntent | null {
  const rpeOpt = (rawRpe?: string): { rpe?: number } => {
    if (rawRpe != null) {
      const v = Number(rawRpe);
      if (v >= 6 && v <= 10) return { rpe: Math.round(v * 2) / 2 };
    }
    return rpe != null ? { rpe } : {};
  };

  const wrLead = tail.match(
    /^(\d+(?:\.\d+)?)\s*(?:lb|lbs|pound|pounds|kg|kgs|kilo|kilos)?\s*(?:for|by|x|times)\s+(\d+)(?:\s*reps?)?(?:\s+(?:@|at\s+)?rpe\s*(\d+(?:\.\d+)?))?\b/,
  );
  if (wrLead) {
    return { kind: 'log_current', weight: Number(wrLead[1]), reps: Number(wrLead[2]), ...rpeOpt(wrLead[3]) };
  }

  const bwLead = tail.match(
    /^(?:body\s*weight|bw)\s*(?:for|by|x|times)\s*(\d+)(?:\s*reps?)?(?:\s+(?:@|at\s+)?rpe\s*(\d+(?:\.\d+)?))?\b/,
  );
  if (bwLead) {
    return { kind: 'log_current', bodyweight: true, weight: 0, reps: Number(bwLead[1]), ...rpeOpt(bwLead[2]) };
  }

  const rLead = tail.match(
    /^(?:for|by|x|times|did|got|hit)\s+(\d+)(?:\s*reps?)?(?:\s+(?:@|at\s+)?rpe\s*(\d+(?:\.\d+)?))?\b/,
  );
  if (rLead) {
    return { kind: 'log_current', reps: Number(rLead[1]), ...rpeOpt(rLead[2]) };
  }

  if (/^(?:skip|clear|end|stop)\s+rest\b/.test(tail)) return { kind: 'rest_skip' };
  if (/^more\s+rest\b/.test(tail)) return { kind: 'rest_add', deltaSec: 30 };
  if (/^next\s+exercise\b/.test(tail)) return { kind: 'next_ex' };
  if (/^(?:prev|previous)\s+exercise\b/.test(tail)) return { kind: 'prev_ex' };
  if (/^(?:add(?:\s+a|\s+1|\s+another)?\s+set|(?:1|one|another|extra)\s+(?:more\s+)?set)\s+(?:(?:to|on|of|for)\s+)?(?:the\s+)?(?:last|previous|prev|prior)\s+exercise\b/.test(tail)) {
    return { kind: 'add_set_prev_ex' };
  }
  if (/^(?:add(?:\s+a|\s+1|\s+another)?\s+set|one\s+more\s+set)\b/.test(tail)) return { kind: 'add_set' };
  if (/^(?:delete|remove|drop|clear)\s+(?:all\s+|the\s+)?(?:empty|unlogged|unfinished|remaining)\s+sets?\b/.test(tail)) {
    return { kind: 'delete_empty_sets' };
  }
  if (/^(?:cancel|never\s*mind|forget\s+it)\b/.test(tail)) return { kind: 'cancel_listen' };
  if (/^uncheck\b/.test(tail)) return { kind: 'uncheck_set' };
  if (/^clear(?:\s+(?:this|that|current)?(?:\s+(?:set|data|row|numbers?))?)?\b/.test(tail)) return { kind: 'clear_set' };
  if (/^pause\s+workout\b/.test(tail)) return { kind: 'pause_workout' };
  if (/^resume\s+workout\b/.test(tail)) return { kind: 'resume_workout' };
  if (/^(?:finish|end|complete)\s+workout\b/.test(tail)) return { kind: 'finish_workout' };
  if (/^clip\s+that\b/.test(tail)) return { kind: 'cam_clip_last' };

  const recCam = tail.match(
    /^(?:record|film)\s+(?:(?:me|this(?:\s+set)?|my\s+set|set)\s+)?(?:on\s+)?(?:the\s+)?(rack|smith\s+wide|smith|fly\s+machine|fly|leg\s+machine|leg)\b/,
  );
  if (recCam) {
    const k = recCam[1].replace(/\s+/g, ' ');
    const camMap: Record<string, string> = {
      rack: 'Rack Cam',
      smith: 'Smith Cam',
      'smith wide': 'Smith Wide',
      fly: 'Fly Machine Cam',
      'fly machine': 'Fly Machine Cam',
      leg: 'Leg Machine Cam',
      'leg machine': 'Leg Machine Cam',
    };
    return camMap[k] ? { kind: 'cam_arm', camera: camMap[k] } : { kind: 'cam_arm' };
  }
  if (/^(?:record|film)\s+(?:this(?:\s+set)?|my\s+set|me|set)\b/.test(tail)) return { kind: 'cam_arm' };
  if (/^(?:start|stop)\s+recording\b/.test(tail)) return { kind: 'cam_arm' };

  const buddyLead = tail.match(/^buddy\s+mode\s+(on|off)\b/);
  if (buddyLead) return { kind: 'buddy_mode', on: buddyLead[1] === 'on' };
  if (/^close\s+(?:the\s+)?(?:replay|video|clip|tv)\b/.test(tail)) return { kind: 'wb_close_replay' };
  if (/^(?:(?:switch|change|toggle|cycle|swap)\s+(?:the\s+)?(?:to\s+(?:the\s+)?)?(?:(?:next|other|another)\s+)?(?:views?|angles?|cameras?)|(?:next|other|another)\s+(?:view|angle|camera))\b/.test(tail)) {
    return { kind: 'wb_switch_view' };
  }
  if (/^replay\s+slow\b/.test(tail)) return { kind: 'wb_replay', slow: true };
  if (/^(?:instant\s+)?replay\b/.test(tail)) return { kind: 'wb_replay' };
  if (/^(?:undo|go\s+back)\b/.test(tail)) return { kind: 'undo' };
  if (/^(?:check|done|logged)\s+\S/.test(tail)) return { kind: 'log_current', ...(rpe != null ? { rpe } : {}) };
  return null;
}

function parseBodytail(tailRaw: string, customAliases?: CustomVoiceAliases, allowNoisy = true): VoiceIntent | null {
  let tail = tailRaw.trim().replace(/^[.,!?:;\s]+|[.,!?:;\s]+$/g, '');
  if (!tail) return { kind: 'arm_wake' };

  // Strip harmless conversational prefixes & politeness suffixes around commands
  tail = tail
    .replace(/^(?:please|uh|um|hey|ok|okay|alright|go\s+ahead(?:\s+and)?)\s+/i, '')
    .replace(/\s+(?:please|thanks|thank\s+you|now|alright|okay|ok|yeah|yep)$/i, '')
    .trim();
  if (!tail) return { kind: 'arm_wake' };

  // Non-destructively extract optional trailing/embedded RPE ("... rpe 8.5") for logging aliases & set commands
  let tailNoRpe = tail;
  let rpe: number | undefined;
  const rpeM = tail.match(/(?:^|\s)(?:@|at\s+)?rpe\s*(\d+(?:\.\d+)?)\b/);
  if (rpeM) {
    const val = Number(rpeM[1]);
    if (val >= 6 && val <= 10) rpe = Math.round(val * 2) / 2;
    tailNoRpe = (tail.slice(0, rpeM.index) + tail.slice((rpeM.index ?? 0) + rpeM[0].length)).trim();
  }

  const customHit = matchCustomVoiceAlias(tailNoRpe, customAliases, rpe);
  if (customHit) return customHit;

  if (/^(?:cancel(?:\s+(?:that|it|listening))?|never\s*mind(?:\s+(?:that|it))?|nvm|forget\s+it|disregard)$/.test(tail)) {
    return { kind: 'cancel_listen' };
  }
  if (/^(?:uncheck(?:\s+(?:this|that|current|last)?(?:\s+set)?)?|pull(?:\s+the)?\s+check|remove\s+check|mark\s+undone)$/.test(tail)) {
    return { kind: 'uncheck_set' };
  }
  if (/^(?:undo(?:\s+(?:that|last(?:\s+set)?|set))?|(?:go\s+|step\s+)?back(?:\s+(?:1|one|a\s+set|(?:1|one)\s+set|to\s+last(?:\s+set)?))?)$/.test(tail)) {
    return { kind: 'undo' };
  }
  if (/^(?:check|check\s+it|check\s+set|log|log\s+it|log\s+set|done|same|same\s+reps|same\s+weight|repeat|mark\s+done|complete(?:\s+set)?|yep|got\s+it|crushed\s+it|nailed\s+it|good(?:\s+set)?|easy|boom|locked\s+in)$/.test(tailNoRpe)) {
    return { kind: 'log_current', ...(rpe != null ? { rpe } : {}) };
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

  if (/^(?:close|stop|hide|exit|dismiss)\s+(?:replay|video|clip|tv)$/.test(tail)) {
    return { kind: 'wb_close_replay' };
  }
  if (/^(?:(?:switch|change|toggle|cycle|swap)\s+(?:the\s+)?(?:to\s+(?:the\s+)?)?(?:(?:next|other|another)\s+)?(?:views?|angles?|cameras?)|(?:next|other|another)\s+(?:view|angle|camera))$/.test(tail)) {
    return { kind: 'wb_switch_view' };
  }

  let repBody = tail;
  let slow = false;
  const repSlowEnd = repBody.match(/\s+(?:in\s+)?(?:slow\s*(?:mo|motion)?|half\s*speed)$/);
  if (repSlowEnd) {
    slow = true;
    repBody = repBody.slice(0, repSlowEnd.index).trim();
  } else {
    const repSlowStart = repBody.match(/^(?:slow\s*(?:mo|motion)?|half\s*speed)\s+(.+)$/);
    if (repSlowStart) {
      slow = true;
      repBody = repSlowStart[1].trim();
    }
  }
  if (/^(?:(?:instant\s+)?replay(?:\s+that|\s+last(?:\s+set)?)?|(?:play|show|watch)\s+last(?:\s+set)?)$/.test(repBody)) {
    return { kind: 'wb_replay', ...(slow ? { slow: true } : {}) };
  }
  const repSetOnly = repBody.match(/^(?:play|replay|show|watch)\s+(?:the\s+)?set\s+(\d+)$/);
  if (repSetOnly) {
    return { kind: 'wb_replay', setNum: Number(repSetOnly[1]), ...(slow ? { slow: true } : {}) };
  }
  const repExSet = repBody.match(/^(?:play|replay|show|watch)\s+(?:my\s+)?(.+?)\s+set\s+(\d+)$/);
  if (repExSet && repExSet[1].trim()) {
    return { kind: 'wb_replay', exQuery: repExSet[1].trim(), setNum: Number(repExSet[2]), ...(slow ? { slow: true } : {}) };
  }
  const repLastEx = repBody.match(/^(?:play|replay|show|watch)\s+(?:the\s+)?last\s+(.+?)(?:\s+set)?$/);
  if (repLastEx && repLastEx[1].trim()) {
    return { kind: 'wb_replay', exQuery: repLastEx[1].trim(), ...(slow ? { slow: true } : {}) };
  }

  if (/^(?:(?:turn\s+on|enable|start|activate)\s+buddy(?:\s+mode)?|buddy(?:\s+mode)?\s+on|lifting\s+with\s+(?:a\s+)?buddy)$/.test(tail)) {
    return { kind: 'buddy_mode', on: true };
  }
  if (/^(?:(?:turn\s+off|disable|stop|exit|end)\s+buddy(?:\s+mode)?|buddy(?:\s+mode)?\s+off|solo(?:\s+mode)?(?:\s+on)?|lifting\s+solo|lifting\s+alone)$/.test(tail)) {
    return { kind: 'buddy_mode', on: false };
  }
  if (/^(?:toggle\s+)?buddy(?:\s+mode)?$/.test(tail)) {
    return { kind: 'buddy_mode' };
  }

  const CAM_TOK_RE = 'smith\\s+wide|fly\\s+machine|chest\\s+fly|pec\\s+deck|leg\\s+machine|rack|smith|fly|leg';
  const mapCamTok = (tok?: string): string | undefined => {
    const t = (tok || '').trim().replace(/\s+/g, ' ');
    if (t === 'rack') return 'Rack Cam';
    if (t === 'smith wide') return 'Smith Wide';
    if (t === 'smith') return 'Smith Cam';
    if (t === 'fly machine' || t === 'chest fly' || t === 'pec deck' || t === 'fly') return 'Fly Machine Cam';
    if (t === 'leg machine' || t === 'leg') return 'Leg Machine Cam';
    return undefined;
  };
  const camGuestA = tail.match(
    new RegExp(`^(?:guest\\s+(?:record|camera|cam|clip)|(?:record|start|stop|arm)\\s+guest(?:\\s+clip|\\s+set|\\s+cam(?:era)?)?)(?:\\s+(?:on\\s+)?(?:the\\s+)?(${CAM_TOK_RE})(?:\\s+cam(?:era)?)?)?$`),
  );
  if (camGuestA) {
    const camera = mapCamTok(camGuestA[1]);
    return camera ? { kind: 'cam_guest', camera } : { kind: 'cam_guest' };
  }
  const camGuestB = tail.match(new RegExp(`^guest\\s+(${CAM_TOK_RE})(?:\\s+cam(?:era)?)?$`));
  if (camGuestB) {
    const camera = mapCamTok(camGuestB[1]);
    return camera ? { kind: 'cam_guest', camera } : { kind: 'cam_guest' };
  }
  const camArmA = tail.match(
    new RegExp(
      `^(?:(${CAM_TOK_RE})\\s+)?(?:camera|cam|(?:record|film)(?:\\s+(?:me|this(?:\\s+set)?|my\\s+set|that|set))?|(?:start|arm|stop)\\s+(?:camera|recording))(?:\\s+(?:on\\s+)?(?:the\\s+)?(${CAM_TOK_RE})(?:\\s+cam(?:era)?)?)?$`,
    ),
  );
  if (camArmA) {
    const camera = mapCamTok(camArmA[1] || camArmA[2]);
    return camera ? { kind: 'cam_arm', camera } : { kind: 'cam_arm' };
  }
  const camArmB = tail.match(new RegExp(`^(?:start|arm|record|film|camera)\\s+(?:on\\s+)?(?:the\\s+)?(${CAM_TOK_RE})(?:\\s+cam(?:era)?)?$`));
  if (camArmB) {
    const camera = mapCamTok(camArmB[1]);
    return camera ? { kind: 'cam_arm', camera } : { kind: 'cam_arm' };
  }
  const camClipA = tail.match(
    new RegExp(`^(?:clip\\s+that|save\\s+clip|clip\\s+last(?:\\s+set)?|clip\\s+set)(?:\\s+(?:on\\s+|from\\s+)?(?:the\\s+)?(${CAM_TOK_RE})(?:\\s+cam(?:era)?)?)?$`),
  );
  if (camClipA) {
    const camera = mapCamTok(camClipA[1]);
    return camera ? { kind: 'cam_clip_last', camera } : { kind: 'cam_clip_last' };
  }
  const camClipB = tail.match(new RegExp(`^clip\\s+(?:on\\s+|from\\s+)?(?:the\\s+)?(${CAM_TOK_RE})(?:\\s+cam(?:era)?)?$`));
  if (camClipB) {
    const camera = mapCamTok(camClipB[1]);
    return camera ? { kind: 'cam_clip_last', camera } : { kind: 'cam_clip_last' };
  }

  if (/^(?:(?:add(?:\s+a|\s+1|\s+another)?\s+set)|(?:(?:1|one|another|extra)\s+(?:more\s+)?set))\s+(?:(?:to|on|of|for)\s+)?(?:the\s+)?(?:last|previous|prev|prior)\s+exercise$/.test(tail)) {
    return { kind: 'add_set_prev_ex' };
  }
  if (/^(?:add(?:\s+a|\s+1|\s+another)?\s+set|(?:1|one|another|extra)\s+(?:more\s+)?set)(?:\s+(?:here|to\s+(?:this|current)(?:\s+exercise)?))?$/.test(tail)) {
    return { kind: 'add_set' };
  }
  if (/^(?:delete|remove|drop|clear)\s+(?:all\s+|the\s+)?(?:empty|unlogged|unfinished|remaining)\s+sets?$/.test(tail)) {
    return { kind: 'delete_empty_sets' };
  }
  if (/^(?:clear(?:\s+(?:this|that|current)?(?:\s+(?:set|data|row|numbers?))?)?|wipe(?:\s+(?:this|that)?(?:\s+set)?)?)$/.test(tail)) {
    return { kind: 'clear_set' };
  }

  if (/^(?:next|next\s+exercise|skip\s+exercise)$/.test(tail)) return { kind: 'next_ex' };
  if (/^(?:prev|previous|previous\s+exercise|back\s+exercise)$/.test(tail)) return { kind: 'prev_ex' };

  const jumpExSetM = tail.match(/^(?:go\s+to|jump\s+to|switch\s+to|move\s+to)\s+(?:exercise\s+)?(.+?)\s+set\s+(\d+)$/)
    ?? tail.match(/^exercise\s+(.+?)\s+set\s+(\d+)$/);
  if (jumpExSetM && jumpExSetM[1].trim()) {
    return { kind: 'jump_ex', target: jumpExSetM[1].trim(), setNum: Number(jumpExSetM[2]) };
  }
  const jumpSetOnlyM = tail.match(/^(?:go\s+to|jump\s+to|switch\s+to|move\s+to)\s+set\s+(\d+)$/)
    ?? tail.match(/^set\s+(\d+)$/);
  if (jumpSetOnlyM) {
    return { kind: 'jump_set', setNum: Number(jumpSetOnlyM[1]) };
  }

  const jumpM = tail.match(/^(?:go\s+to|jump\s+to|switch\s+to|move\s+to|exercise)\s+(.+)$/);
  if (jumpM && jumpM[1].trim()) return { kind: 'jump_ex', target: jumpM[1].trim() };
  const bareLabelM = tail.match(/^([1-9]\s*[a-d])$/);
  if (bareLabelM) return { kind: 'jump_ex', target: bareLabelM[1].replace(/\s+/g, '') };

  // Weight-only or Reps-only preview updates (no auto-check)
  const setW = tailNoRpe.match(/^(?:set\s+)?weight\s+(?:to\s+)?(\d+(?:\.\d+)?)(?:\s*(?:lb|lbs|pound|pounds|kg|kgs|kilo|kilos))?$/);
  if (setW) return { kind: 'set_only', weight: Number(setW[1]) };
  const setR = tailNoRpe.match(/^(?:set\s+)?reps\s+(?:to\s+)?(\d+)$/);
  if (setR) return { kind: 'set_only', reps: Number(setR[1]) };

  // Bodyweight + reps ("bodyweight for 12", "bw 10")
  const bwM = tailNoRpe.match(/^(?:body\s*weight|bw)\s*(?:for|by|x|times|reps)?\s*(\d+)(?:\s*reps)?$/);
  if (bwM) {
    return { kind: 'log_current', bodyweight: true, weight: 0, reps: Number(bwM[1]), ...(rpe != null ? { rpe } : {}) };
  }

  // Timed hold logging ("45 seconds", "hold 60 sec done")
  const secM = tailNoRpe.match(/^(?:hold\s+)?(\d+)\s*(?:s|sec|secs|seconds)(?:\s+(?:done|check|log))?$/);
  if (secM) {
    return { kind: 'log_current', seconds: Number(secM[1]), ...(rpe != null ? { rpe } : {}) };
  }

  // Weight + reps ("225 for 8", "225 by 8", "225 x 8", "225 lbs for 8 reps")
  const wrM = tailNoRpe.match(/^(\d+(?:\.\d+)?)\s*(?:lb|lbs|pound|pounds|kg|kgs|kilo|kilos)?\s*(?:for|by|x|times|at|@)?\s*(\d+)(?:\s*reps?)?$/);
  if (wrM) {
    return { kind: 'log_current', weight: Number(wrM[1]), reps: Number(wrM[2]), ...(rpe != null ? { rpe } : {}) };
  }

  // Reps at weight ("8 reps at 225", "10 at 185 lbs")
  const rwM = tailNoRpe.match(/^(\d+)\s*(?:reps?)?\s*(?:at|with|@|of)\s*(\d+(?:\.\d+)?)(?:\s*(?:lb|lbs|pound|pounds|kg|kgs|kilo|kilos))?$/);
  if (rwM) {
    return { kind: 'log_current', weight: Number(rwM[2]), reps: Number(rwM[1]), ...(rpe != null ? { rpe } : {}) };
  }

  // Reps-only logging using pre-filled / current weight ("for 10", "by 10", "did 5", "10 reps")
  const rOnlyM = tailNoRpe.match(/^(?:for|by|x|times|did|got|hit)?\s*(\d+)\s*reps?$/)
    ?? tailNoRpe.match(/^(?:for|by|x|times|did|got|hit)\s+(\d+)$/);
  if (rOnlyM) {
    return { kind: 'log_current', reps: Number(rOnlyM[1]), ...(rpe != null ? { rpe } : {}) };
  }

  if (allowNoisy) return parseLeadingNoisyTail(tail, rpe);
  return null;
}

function validateVoiceIntent(intent: VoiceIntent | null): VoiceIntent | null {
  if (!intent) return null;
  switch (intent.kind) {
    case 'log_current':
      if (intent.reps != null && (intent.reps < 1 || intent.reps > 100)) return null;
      if (intent.seconds != null && (intent.seconds < 1 || intent.seconds > 600)) return null;
      if (intent.weight != null && (intent.weight < 0 || intent.weight > 1500)) return null;
      return intent;
    case 'set_only':
      if (intent.reps != null && (intent.reps < 1 || intent.reps > 100)) return null;
      if (intent.weight != null && (intent.weight < 0 || intent.weight > 1500)) return null;
      return intent;
    case 'start_hold':
      if (intent.seconds != null && (intent.seconds < 1 || intent.seconds > 600)) return null;
      return intent;
    case 'rest_add':
      if (Math.abs(intent.deltaSec) < 1 || Math.abs(intent.deltaSec) > 600) return null;
      return intent;
    case 'jump_ex':
      if (intent.setNum != null && (intent.setNum < 1 || intent.setNum > 50)) return null;
      return intent;
    case 'jump_set':
      if (intent.setNum < 1 || intent.setNum > 50) return null;
      return intent;
    case 'wb_replay':
      if (intent.setNum != null && (intent.setNum < 1 || intent.setNum > 50)) return null;
      return intent;
    default:
      return intent;
  }
}

function buildWakeRegex(wakeWord?: string): RegExp {
  const wakeRaw = (wakeWord ?? 'armory').trim().toLowerCase() || 'armory';
  const wakeAlts = wakeRaw === 'armory'
    ? [
        'armory', 'armoury', 'armorys', 'armourys', 'armory fit', 'armoryfit',
        'amory', 'emory', 'army', 'arm re', 'arm ree',
        'armery', 'armary', 'ormory', 'armorie', 'armor', 'armored',
      ]
    : [wakeRaw];
  return new RegExp(`\\b(?:hey\\s+|ok\\s+|okay\\s+)?(?:${wakeAlts.map(escapeRe).join('|')})\\b`, 'gi');
}

/** True when `raw` contains the configured wake word (used to arm immediately on partial transcripts). */
export function hasWakeWord(raw: string, wakeWord?: string): boolean {
  const norm = normalizeVoiceText(raw);
  if (!norm) return false;
  return buildWakeRegex(wakeWord).test(norm);
}

/** True when user spoke something after the wake word (or while already armed) — gates "Didn't catch that" away from ambient chatter. */
export function hasCommandTailAfterWake(raw: string, wakeWord?: string, armed = false): boolean {
  const norm = normalizeVoiceText(raw);
  if (!norm) return false;
  const wakeRe = buildWakeRegex(wakeWord);
  let lastEnd = -1;
  let m: RegExpExecArray | null;
  while ((m = wakeRe.exec(norm)) !== null) {
    lastEnd = m.index + m[0].length;
  }
  if (lastEnd >= 0) {
    return norm.slice(lastEnd).replace(/^[.,!?:;\s]+|[.,!?:;\s]+$/g, '').length > 0;
  }
  if (armed) {
    return norm.replace(/^[.,!?:;\s]+|[.,!?:;\s]+$/g, '').length > 0;
  }
  return false;
}

export const SET_CONFIRM_TAGS = [
  'check',
  'got it',
] as const;

/** Picks a punchy set-logged confirmation tag at random without repeating `prevTag` back-to-back. */
export function pickSetConfirmTag(
  prevTag?: string | null,
  rng: () => number = Math.random,
): string {
  const pool = prevTag
    ? SET_CONFIRM_TAGS.filter((t) => t !== prevTag)
    : SET_CONFIRM_TAGS;
  const idx = Math.min(pool.length - 1, Math.max(0, Math.floor(rng() * pool.length)));
  return pool[idx] ?? SET_CONFIRM_TAGS[0];
}

/** Returns concise spoken readback confirmation text (or `null` for silent wake arming). */
export function defaultReadbackForIntent(intent: VoiceIntent, confirmTag = 'got it'): string | null {
  switch (intent.kind) {
    case 'arm_wake':
    case 'cancel_listen':
      return null;
    case 'log_current':
      if (intent.seconds != null) return `${intent.seconds} seconds, ${confirmTag}`;
      if (intent.bodyweight && intent.reps != null) return `Bodyweight for ${intent.reps}, ${confirmTag}`;
      if (intent.weight != null && intent.reps != null) return `${intent.weight} for ${intent.reps}, ${confirmTag}`;
      if (intent.reps != null) return `For ${intent.reps}, ${confirmTag}`;
      return confirmTag === 'check' ? 'Set checked' : `Checked, ${confirmTag}`;
    case 'set_only':
      if (intent.weight != null) return `Weight ${intent.weight}, got it`;
      if (intent.reps != null) return `Reps ${intent.reps}, got it`;
      return 'Updated';
    case 'start_hold':
      return intent.seconds != null ? `Hold ${intent.seconds} seconds` : 'Hold started';
    case 'next_ex':
      return 'Next exercise';
    case 'prev_ex':
      return 'Previous exercise';
    case 'jump_ex':
      return intent.setNum != null ? `Jumping to ${intent.target}, set ${intent.setNum}` : `Jumping to ${intent.target}`;
    case 'jump_set':
      return `Set ${intent.setNum}`;
    case 'uncheck_set':
      return 'Set unchecked';
    case 'clear_set':
      return 'Set cleared';
    case 'undo':
      return 'Undid last';
    case 'rest_skip':
      return 'Rest skipped';
    case 'rest_add':
      return intent.deltaSec >= 0 ? `Added ${intent.deltaSec} seconds` : `Minus ${Math.abs(intent.deltaSec)} seconds`;
    case 'pause_workout':
      return 'Workout paused';
    case 'resume_workout':
      return 'Workout resumed';
    case 'finish_workout':
      return 'Finishing workout';
    case 'add_set':
      return 'Set added';
    case 'add_set_prev_ex':
      return 'Added set to last exercise';
    case 'delete_empty_sets':
      return 'Deleted empty sets';
    case 'cam_arm':
      return intent.camera ? `Recording ${intent.camera.replace(/ cam$/i, '')}` : 'Recording';
    case 'cam_clip_last':
      return 'Clipping last set';
    case 'cam_guest':
      return intent.camera ? `Guest camera ${intent.camera.replace(/ cam$/i, '')}` : 'Guest recording';
    case 'buddy_mode':
      return intent.on === true ? 'Buddy mode on' : intent.on === false ? 'Buddy mode off' : 'Buddy mode toggled';
    case 'wb_replay':
      return intent.slow ? 'Replaying slow motion on TV' : 'Replaying on TV';
    case 'wb_switch_view':
      return 'Switching views';
    case 'wb_close_replay':
      return 'Closing video';
  }
}

/** Human-readable intent description shown in Voice tester / logs. */
export function describeVoiceIntent(intent: VoiceIntent): string {
  return defaultReadbackForIntent(intent) ?? 'Wake word armed (listening…)';
}

/**
 * Parses a speech transcript gated by the configured wake word (e.g. `"armory"`).
 * When `armed` is true (user already said the wake word within the active window),
 * bare command tails are accepted directly.
 */
export function parseVoiceTranscript(
  raw: string,
  opts: { wakeWord?: string; armed?: boolean; requireWake?: boolean; customAliases?: CustomVoiceAliases } = {},
): VoiceIntent | null {
  const norm = normalizeVoiceText(raw);
  if (!norm) return null;

  const wakeRe = buildWakeRegex(opts.wakeWord);

  let lastEnd = -1;
  let m: RegExpExecArray | null;
  while ((m = wakeRe.exec(norm)) !== null) {
    lastEnd = m.index + m[0].length;
  }

  if (lastEnd >= 0) {
    const tail = norm.slice(lastEnd).trim();
    if (!tail) return { kind: 'arm_wake' };
    return validateVoiceIntent(parseBodytail(tail, opts.customAliases, true));
  }

  if (opts.requireWake === false || opts.armed) {
    return validateVoiceIntent(parseBodytail(norm, opts.customAliases, opts.requireWake === false));
  }
  return null;
}

/** Deterministic canonical intent key used for dual-mic A/B comparison and cross-source dedup. */
export function voiceIntentKey(intent: VoiceIntent | null): string {
  if (!intent) return 'none';
  switch (intent.kind) {
    case 'arm_wake':
      return 'arm_wake';
    case 'log_current':
      return `log:${intent.bodyweight ? 'bw' : (intent.weight ?? '_')}:${intent.seconds != null ? `s${intent.seconds}` : (intent.reps ?? '_')}:${intent.rpe ?? '_'}`;
    case 'set_only':
      return `set:${intent.weight ?? '_'}:${intent.reps ?? '_'}`;
    case 'start_hold':
      return `hold:${intent.seconds ?? '_'}`;
    case 'jump_ex':
      return `jump:${normalizeVoiceText(intent.target)}${intent.setNum != null ? `:s${intent.setNum}` : ''}`;
    case 'jump_set':
      return `jump_set:${intent.setNum}`;
    case 'rest_add':
      return `rest_add:${intent.deltaSec}`;
    case 'cam_arm':
    case 'cam_clip_last':
    case 'cam_guest':
      return `${intent.kind}:${(intent.camera ?? 'auto').toLowerCase()}`;
    case 'buddy_mode':
      return `buddy_mode:${intent.on == null ? 'tog' : intent.on ? '1' : '0'}`;
    case 'wb_replay':
      return `wb_replay:${intent.exQuery ? normalizeVoiceText(intent.exQuery) : '_'}:${intent.setNum ?? '_'}:${intent.slow ? 'slow' : '1x'}`;
    default:
      return intent.kind;
  }
}

/** Normalized word-level F1 match percentage (0–100) between a transcript and target phrase. */
export function voiceTokenMatchPct(heard: string, expected: string): number {
  const a = normalizeVoiceText(heard).split(/\s+/).filter(Boolean);
  const b = normalizeVoiceText(expected).split(/\s+/).filter(Boolean);
  if (!a.length && !b.length) return 100;
  if (!a.length || !b.length) return 0;
  const counts = new Map<string, number>();
  for (const tok of b) counts.set(tok, (counts.get(tok) ?? 0) + 1);
  let overlap = 0;
  for (const tok of a) {
    const left = counts.get(tok) ?? 0;
    if (left > 0) {
      overlap += 1;
      counts.set(tok, left - 1);
    }
  }
  return Math.round(((2 * overlap) / (a.length + b.length)) * 1000) / 10;
}

export interface AbShootoutPhrase {
  phrase: string;
  expectedIntentKey: string;
  expectedLabel: string;
}

export const AB_SHOOTOUT_PHRASES: readonly AbShootoutPhrase[] = [
  { phrase: 'Armory 225 for 8', expectedIntentKey: 'log:225:8:_', expectedLabel: 'Log 225 × 8' },
  { phrase: 'Armory check', expectedIntentKey: 'log:_:_:_', expectedLabel: 'Check set as-is' },
  { phrase: 'Armory for 10', expectedIntentKey: 'log:_:10:_', expectedLabel: 'Log 10 reps' },
  { phrase: 'Armory 50s for 12', expectedIntentKey: 'log:50:12:_', expectedLabel: 'Log 50s × 12' },
  { phrase: 'Armory 185 for 10 rpe 8.5', expectedIntentKey: 'log:185:10:8.5', expectedLabel: 'Log 185 × 10 @ RPE 8.5' },
  { phrase: 'Armory skip rest', expectedIntentKey: 'rest_skip', expectedLabel: 'Skip rest' },
  { phrase: 'Armory next exercise', expectedIntentKey: 'next_ex', expectedLabel: 'Next exercise' },
  { phrase: 'Armory record smith', expectedIntentKey: 'cam_arm:smith cam', expectedLabel: 'Record Smith Cam' },
  { phrase: 'Armory clip that', expectedIntentKey: 'cam_clip_last:auto', expectedLabel: 'Clip last set' },
  { phrase: 'Armory replay slow', expectedIntentKey: 'wb_replay:_:_:slow', expectedLabel: 'Replay slow on TV' },
];
