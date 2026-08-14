(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.MonitorTurns = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
  const DEFAULTS = {
    turnSwitchStableMs: 300,
    minInitialChars: 2,
    minSwitchChars: 3,
    requireVadForSwitch: true,
  };

  function isAgent(speaker) {
    return speaker === 'agent' || speaker === 'system';
  }

  function turnType(speaker) {
    return isAgent(speaker) ? 'Q' : 'A';
  }

  function speakerKey(speaker) {
    return isAgent(speaker) ? 'agent' : 'caller';
  }

  function normalizeProviderValues(provider, providers) {
    const source = Array.isArray(providers) && providers.length
      ? providers
      : (provider === 'mixed' ? ['funasr', 'xfyun'] : [provider]);
    const values = [];
    for (const item of source) {
      const value = String(item || '').trim().toLowerCase();
      if ((value === 'funasr' || value === 'xfyun') && !values.includes(value)) {
        values.push(value);
      }
    }
    return values;
  }

  function providerValuesForEvent(conv, callId, provider, providers) {
    const explicit = normalizeProviderValues(provider, providers);
    if (explicit.length) return explicit;
    const state = conv && conv.modelStates && conv.modelStates[callId];
    return normalizeProviderValues(state && state.currentProvider, []);
  }

  function applyTurnProviders(turn, providerValues, replace) {
    if (!turn) return;
    const values = replace ? [] : (Array.isArray(turn.providers) ? turn.providers.slice() : []);
    for (const provider of (Array.isArray(providerValues) ? providerValues : [])) {
      if (!values.includes(provider)) values.push(provider);
    }
    turn.providers = values;
    turn.provider = values.length > 1 ? 'mixed' : (values[0] || '');
  }

  function pairKey(callfrom, callto) {
    const a = (callfrom || '?').replace(/^micro$/, '?');
    const b = (callto || '?').replace(/^micro$/, '?');
    return a < b ? `${a}::${b}` : `${b}::${a}`;
  }

  function hasRealPhone(callfrom, callto) {
    return (callfrom && callfrom !== 'micro' && callfrom !== '?') ||
      (callto && callto !== 'micro' && callto !== '?');
  }

  function eventTimeMs(ev) {
    return Number.isFinite(ev.sendTimeMs) ? ev.sendTimeMs : Date.now();
  }

  function normalizeText(text) {
    return String(text || '')
      .toLowerCase()
      .replace(/[^0-9a-z一-鿿]/g, '');
  }

  function normLen(text) {
    return normalizeText(text).length;
  }

  function isMeaningful(text, minChars) {
    return normLen(text) >= minChars;
  }

  function buildNormMap(text) {
    const norm = [];
    const map = [];
    const raw = String(text || '');
    for (let i = 0; i < raw.length; i++) {
      const ch = raw[i].toLowerCase();
      if (/^[0-9a-z一-鿿]$/.test(ch)) {
        norm.push(ch);
        map.push(i);
      }
    }
    return { norm: norm.join(''), map };
  }

  function editDistanceWithin(a, b, maxAllowed) {
    if (Math.abs(a.length - b.length) > maxAllowed) return maxAllowed + 1;
    let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i];
      let rowMin = cur[0];
      for (let j = 1; j <= b.length; j++) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        const v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
        cur[j] = v;
        if (v < rowMin) rowMin = v;
      }
      if (rowMin > maxAllowed) return maxAllowed + 1;
      prev = cur;
    }
    return prev[b.length];
  }

  function consumedOriginalIndex(fullText, baselineText) {
    const full = String(fullText || '');
    const baseline = String(baselineText || '');
    if (!baseline) return 0;
    if (full.startsWith(baseline)) return baseline.length;

    const baseNorm = buildNormMap(baseline).norm;
    const fullNorm = buildNormMap(full);
    if (!baseNorm) return 0;
    if (!fullNorm.norm) return 0;

    if (fullNorm.norm.startsWith(baseNorm)) {
      return fullNorm.map[baseNorm.length - 1] + 1;
    }

    const maxDrift = Math.max(12, Math.ceil(baseNorm.length * 0.45));
    const maxAllowed = Math.max(4, Math.ceil(baseNorm.length * 0.35));
    const lo = Math.max(0, baseNorm.length - maxDrift);
    const hi = Math.min(fullNorm.norm.length, baseNorm.length + maxDrift);
    let bestJ = -1;
    let bestDistance = maxAllowed + 1;

    for (let j = lo; j <= hi; j++) {
      const distance = editDistanceWithin(baseNorm, fullNorm.norm.slice(0, j), maxAllowed);
      if (distance < bestDistance || (distance === bestDistance && j > bestJ)) {
        bestDistance = distance;
        bestJ = j;
      }
    }

    if (bestJ >= 0 && bestDistance <= maxAllowed) {
      return bestJ === 0 ? 0 : fullNorm.map[bestJ - 1] + 1;
    }

    return 0;
  }

  function deltaFromBaseline(fullText, baselineText) {
    const full = String(fullText || '').trim();
    const consumed = consumedOriginalIndex(full, baselineText);
    return full.slice(consumed).trim();
  }

  function lockTurn(conv, idx) {
    if (idx !== undefined && idx !== null && conv.turns[idx]) {
      conv.turns[idx].locked = true;
    }
  }

  function speakerState(conv, key) {
    if (!conv._speakers) conv._speakers = {};
    if (!conv._speakers[key]) {
      conv._speakers[key] = {
        baselineFullText: '',
        baselineEndTimeMs: 0,
        lastFullText: '',
        lastDeltaText: '',
        lastChangedAt: 0,
        lastStartTimeMs: 0,
        lastEndTimeMs: 0,
        vadState: 'unknown',
        stabilityStatus: 'waiting',
        silenceDurationMs: 0,
        audioLevel: 0,
        volumeDb: null,
        pendingAudioSegments: [],
      };
    }
    return conv._speakers[key];
  }

  function mergeConversation(dst, src) {
    const offset = dst.turns.length;
    dst.turns.push(...src.turns);
    dst._speakers = Object.assign(dst._speakers || {}, src._speakers || {});
    if (src._activeTurnIdx !== undefined && src._activeTurnIdx !== null) {
      dst._activeTurnIdx = offset + src._activeTurnIdx;
      const active = dst.turns[dst._activeTurnIdx];
      dst._activeSpeakerKey = active ? active._speakerKey : dst._activeSpeakerKey;
      dst._activeCallId = active ? active.callId : dst._activeCallId;
    }
    dst.status = src.status || dst.status;
    for (const callId of (src.activeCallIds || [])) dst.activeCallIds.add(callId);
    dst.modelStates = Object.assign(dst.modelStates || {}, src.modelStates || {});
    if (!dst.modelSwitchRequest && src.modelSwitchRequest) {
      dst.modelSwitchRequest = src.modelSwitchRequest;
    }
  }

  function createMonitorState(options) {
    const cfg = Object.assign({}, DEFAULTS, options || {});
    const convs = new Map();
    const callIdToPair = new Map();
    const endedCallIds = new Set();
    let turnCount = 0;

    function ensureConv(key, callfrom, callto) {
      let c = convs.get(key);
      if (!c) {
        c = {
          _key: key,
          status: 'active',
          callfrom: callfrom || '?',
          callto: callto || '?',
          turns: [],
          aiCorrections: [],
          activeCallIds: new Set(),
          modelStates: {},
          modelSwitchRequest: null,
          holdMessage: '',
          createdAt: new Date(),
          _activeTurnIdx: null,
          _activeSpeakerKey: null,
          _activeCallId: null,
          _speakers: {},
        };
        convs.set(key, c);
      }
      if (callfrom && callfrom !== 'micro' && callfrom !== '?') c.callfrom = callfrom;
      if (callto && callto !== 'micro' && callto !== '?') c.callto = callto;
      return c;
    }

    function resolveConv(callId, callfrom, callto) {
      const pk = pairKey(callfrom, callto);
      const hasReal = hasRealPhone(callfrom, callto);

      let bestKey = null;
      if (hasReal) {
        for (const [k, conv] of convs) {
          const samePair = k === pk || pairKey(conv.callfrom, conv.callto) === pk;
          const sameCall = callId && conv.activeCallIds && conv.activeCallIds.has(callId);
          if (samePair && (sameCall || conv.status !== 'ended')) {
            bestKey = k;
            break;
          }
        }
      }

      let oldKey = null;
      if (callId) {
        const mapped = callIdToPair.get(callId);
        if (mapped && convs.has(mapped)) oldKey = mapped;
      }

      let finalKey;
      if (!bestKey && !oldKey) {
        finalKey = hasReal
          ? (callId ? `${pk}::${callId}` : pk)
          : (callId || `x-${Math.random().toString(36).slice(2, 6)}`);
      } else if (bestKey && oldKey && bestKey !== oldKey) {
        const dst = ensureConv(bestKey, callfrom, callto);
        const src = convs.get(oldKey);
        if (src) {
          mergeConversation(dst, src);
          for (const [cid, k] of callIdToPair) {
            if (k === oldKey) callIdToPair.set(cid, bestKey);
          }
          convs.delete(oldKey);
        }
        finalKey = bestKey;
      } else if (bestKey) {
        finalKey = bestKey;
      } else {
        finalKey = oldKey;
        if (hasReal && finalKey !== pk) {
          const conv = convs.get(finalKey);
          convs.delete(finalKey);
          const dst = ensureConv(pk, callfrom, callto);
          if (conv) mergeConversation(dst, conv);
          for (const [cid, k] of callIdToPair) {
            if (k === finalKey) callIdToPair.set(cid, pk);
          }
          finalKey = pk;
        }
      }

      if (callId) callIdToPair.set(callId, finalKey);
      return finalKey;
    }

    function updateSpeakerProgress(conv, key, fullText, nowMs, startMs, endMs, segmentId) {
      const st = speakerState(conv, key);
      const cleanText = String(fullText || '').trim();
      if (!st.segmentTexts) st.segmentTexts = {};
      const previousSegmentText = segmentId ? (st.segmentTexts[segmentId] || '') : st.lastDeltaText;
      const delta = segmentId ? cleanText : deltaFromBaseline(cleanText, st.baselineFullText);
      const changed = normalizeText(delta) !== normalizeText(previousSegmentText);
      if (segmentId) st.segmentTexts[segmentId] = delta;
      st.lastFullText = cleanText;
      st.lastStartTimeMs = Number.isFinite(startMs) ? startMs : st.lastStartTimeMs;
      st.lastEndTimeMs = Number.isFinite(endMs) ? endMs : st.lastEndTimeMs;
      if (changed) {
        st.lastDeltaText = delta;
        st.lastChangedAt = nowMs;
        if (!st.hasVadEnded) st.stabilityStatus = 'waiting';
      }
      return { state: st, delta, changed };
    }

    function setVadState(conv, key, vadState, nowMs, silenceDurationMs, audioLevel, volumeDb) {
      const st = speakerState(conv, key);
      st.vadState = vadState || st.vadState || 'unknown';
      st.vadUpdatedAt = nowMs;
      if (Number.isFinite(silenceDurationMs)) st.silenceDurationMs = silenceDurationMs;
      if (Number.isFinite(audioLevel)) st.audioLevel = Math.max(0, Math.min(100, Math.round(audioLevel)));
      if (Number.isFinite(volumeDb)) st.volumeDb = volumeDb;
      if (vadState === 'speaking') {
        st.hasVadEnded = false;
        st.stabilityStatus = 'waiting';
      } else if (vadState === 'ended' || vadState === 'silence') {
        st.hasVadEnded = true;
        st.speechEndedAt = nowMs;
        st.stabilityStatus = 'stabilizing';
      }
    }

    function applySpeakerDebug(turn, st) {
      if (!turn || !st) return;
      turn.vadState = st.vadState || 'unknown';
      turn.stabilityStatus = st.stabilityStatus || 'waiting';
      turn.silenceDurationMs = Number.isFinite(st.silenceDurationMs) ? st.silenceDurationMs : 0;
      turn.audioLevel = Number.isFinite(st.audioLevel) ? st.audioLevel : 0;
      turn.volumeDb = Number.isFinite(st.volumeDb) ? st.volumeDb : null;
    }

    function turnContainsSegment(turn, segmentId) {
      if (!turn || !segmentId) return false;
      return Array.isArray(turn._segmentTexts) &&
        turn._segmentTexts.some(segment => segment.segmentId === segmentId);
    }

    function audioSegmentIds(audio) {
      const ids = [];
      if (audio && audio.segmentId) ids.push(audio.segmentId);
      if (audio && Array.isArray(audio.segmentIds)) {
        for (const id of audio.segmentIds) {
          if (id && !ids.includes(id)) ids.push(id);
        }
      }
      return ids;
    }

    function turnContainsAnyAudioSegment(turn, audio) {
      return audioSegmentIds(audio).some(segmentId => turnContainsSegment(turn, segmentId));
    }

    function setTurnSegmentText(turn, segmentId, text, providerValues) {
      if (!segmentId) {
        turn.text = text;
        applyTurnProviders(turn, providerValues, false);
        return;
      }
      if (!Array.isArray(turn._segmentTexts)) turn._segmentTexts = [];
      let segment = turn._segmentTexts.find(item => item.segmentId === segmentId);
      if (!segment) {
        segment = { segmentId, text: '' };
        turn._segmentTexts.push(segment);
      }
      segment.text = text;
      turn.segmentId = turn._segmentTexts[0].segmentId;
      turn.text = turn._segmentTexts.map(item => item.text).filter(Boolean).join(' ');
      applyTurnProviders(turn, providerValues, false);
    }

    function assignAudio(turn, audio) {
      if (!turn || !audio) return;
      if (!Array.isArray(turn.audioSegments)) turn.audioSegments = [];
      if (audio.segmentId) {
        let segment = turn.audioSegments.find(item => {
          const itemIds = audioSegmentIds(item);
          return audioSegmentIds(audio).some(id => itemIds.includes(id));
        });
        if (!segment) {
          // 每个 turn 只保留一条完整录音 — 替换而非追加
          turn.audioSegments = [{}];
          segment = turn.audioSegments[0];
        }
        Object.assign(segment, audio, { segmentIds: audioSegmentIds(audio) });
      } else if (turn.audioSegments.length === 0) {
        turn.audioSegments.push(audio);
      }
      const first = turn.audioSegments[0] || audio;
      turn.audioUrl = first.audioUrl || null;
      turn.audioDurationMs = Number.isFinite(first.audioDurationMs) ? first.audioDurationMs : 0;
      turn.audioStartTimeMs = Number.isFinite(first.startTimeMs) ? first.startTimeMs : 0;
      turn.audioEndTimeMs = Number.isFinite(first.endTimeMs) ? first.endTimeMs : 0;
      if (Number.isFinite(audio.startTimeMs)) {
        turn.startTimeMs = Math.min(turn.startTimeMs || audio.startTimeMs, audio.startTimeMs);
      }
      if (Number.isFinite(audio.endTimeMs)) turn.endTimeMs = Math.max(turn.endTimeMs || 0, audio.endTimeMs);
    }

    function attachAudioSegment(conv, key, audio) {
      if (audio.segmentId) {
        const exact = conv.turns.find(turn =>
          turn._speakerKey === key && turnContainsAnyAudioSegment(turn, audio)
        );
        if (exact) {
          assignAudio(exact, audio);
          return;
        }
        speakerState(conv, key).pendingAudioSegments.push(audio);
        return;
      }

      for (let i = 0; i < conv.turns.length; i++) {
        const turn = conv.turns[i];
        const sameStream = !audio.callId || !turn.callId || turn.callId === audio.callId;
        if (turn._speakerKey === key && sameStream && !turn.audioUrl) {
          assignAudio(turn, audio);
          return;
        }
      }
      speakerState(conv, key).pendingAudioSegments.push(audio);
    }

    function applyPendingAudio(conv, key, turn) {
      const pending = speakerState(conv, key).pendingAudioSegments;
      for (let idx = pending.length - 1; idx >= 0; idx--) {
        const audio = pending[idx];
        const exactSegment = audio.segmentId && turnContainsAnyAudioSegment(turn, audio);
        const legacyMatch = !audio.segmentId &&
          (!audio.callId || !turn.callId || turn.callId === audio.callId) && !turn.audioUrl;
        if (exactSegment || legacyMatch) {
          assignAudio(turn, audio);
          pending.splice(idx, 1);
        }
      }
    }

    function derivedTurnStartMs(conv, key, startMs, endMs) {
      const st = speakerState(conv, key);
      if (st.baselineEndTimeMs > 0 && st.baselineEndTimeMs <= endMs) {
        return st.baselineEndTimeMs;
      }
      return Number.isFinite(startMs) ? startMs : 0;
    }

    function updateActiveTurn(conv, text, speaker, callId, startMs, endMs, segmentId, providerValues) {
      const active = conv.turns[conv._activeTurnIdx];
      setTurnSegmentText(active, segmentId, text, providerValues);
      active.speaker = speaker;
      active.type = turnType(speaker);
      active.callId = callId;
      if (endMs > active.endTimeMs) active.endTimeMs = endMs;
      active.locked = false;
      applyPendingAudio(conv, speakerKey(speaker), active);
      applySpeakerDebug(active, speakerState(conv, speakerKey(speaker)));
    }

    function appendTurn(conv, text, speaker, callId, startMs, endMs, segmentId, providerValues) {
      const key = speakerKey(speaker);
      const turnStartMs = derivedTurnStartMs(conv, key, startMs, endMs);
      conv.turns.push({
        type: turnType(speaker),
        speaker,
        text,
        callId,
        segmentId: segmentId || null,
        provider: '',
        providers: [],
        _segmentTexts: segmentId ? [{ segmentId, text }] : [],
        audioSegments: [],
        startTimeMs: turnStartMs,
        endTimeMs: endMs,
        locked: false,
        stabilityStatus: 'waiting',
        vadState: 'unknown',
        silenceDurationMs: 0,
        audioLevel: 0,
        volumeDb: null,
        audioUrl: null,
        audioDurationMs: 0,
        audioStartTimeMs: 0,
        audioEndTimeMs: 0,
        _speakerKey: key,
      });
      const appended = conv.turns[conv.turns.length - 1];
      applyTurnProviders(appended, providerValues, true);
      applyPendingAudio(conv, key, appended);
      applySpeakerDebug(appended, speakerState(conv, key));
      conv._activeTurnIdx = conv.turns.length - 1;
      conv._activeSpeakerKey = key;
      conv._activeCallId = callId;
      turnCount++;
    }

    function buildTurn(text, speaker, callId, startMs, endMs, segmentIds, locked, providerValues) {
      const key = speakerKey(speaker);
      const ids = Array.isArray(segmentIds) ? segmentIds.filter(Boolean) : [];
      return {
        type: turnType(speaker),
        speaker,
        text,
        callId,
        segmentId: ids[0] || null,
        provider: Array.isArray(providerValues) && providerValues.length > 1
          ? 'mixed'
          : ((Array.isArray(providerValues) && providerValues[0]) || ''),
        providers: Array.isArray(providerValues) ? providerValues.slice() : [],
        _segmentTexts: ids.map((id, idx) => ({ segmentId: id, text: idx === 0 ? text : '' })),
        audioSegments: [],
        startTimeMs: Number.isFinite(startMs) ? startMs : 0,
        endTimeMs: Number.isFinite(endMs) ? endMs : 0,
        locked: Boolean(locked),
        stabilityStatus: locked ? 'stable' : 'waiting',
        vadState: locked ? 'ended' : 'unknown',
        silenceDurationMs: 0,
        audioLevel: 0,
        volumeDb: null,
        audioUrl: null,
        audioDurationMs: 0,
        audioStartTimeMs: 0,
        audioEndTimeMs: 0,
        _speakerKey: key,
      };
    }

    function insertStableTurn(conv, turn) {
      let insertAt = conv.turns.length;
      for (let i = 0; i < conv.turns.length; i++) {
        const current = conv.turns[i];
        if (Number.isFinite(current.startTimeMs) && current.startTimeMs > turn.startTimeMs) {
          insertAt = i;
          break;
        }
      }
      conv.turns.splice(insertAt, 0, turn);
      if (conv._activeTurnIdx !== null && conv._activeTurnIdx !== undefined && conv._activeTurnIdx >= insertAt) {
        conv._activeTurnIdx += 1;
      }
      turnCount++;
      return turn;
    }

    function reorderTurnByStartTime(conv, turn) {
      const currentIdx = conv.turns.indexOf(turn);
      if (currentIdx < 0) return turn;

      const activeTurn = conv._activeTurnIdx !== null && conv._activeTurnIdx !== undefined
        ? conv.turns[conv._activeTurnIdx]
        : null;

      conv.turns.splice(currentIdx, 1);

      let insertAt = conv.turns.length;
      for (let i = 0; i < conv.turns.length; i++) {
        const current = conv.turns[i];
        if (Number.isFinite(current.startTimeMs) && current.startTimeMs > turn.startTimeMs) {
          insertAt = i;
          break;
        }
      }

      conv.turns.splice(insertAt, 0, turn);
      conv._activeTurnIdx = activeTurn ? conv.turns.indexOf(activeTurn) : null;
      return turn;
    }

    function setStableTurnText(turn, segmentIds, text, providerValues) {
      const ids = Array.isArray(segmentIds) ? segmentIds.filter(Boolean) : [];
      turn.text = text;
      turn.segmentId = ids[0] || turn.segmentId || null;
      turn._segmentTexts = ids.map((id, idx) => ({ segmentId: id, text: idx === 0 ? text : '' }));
      turn.locked = true;
      turn.stabilityStatus = 'stable';
      turn.vadState = 'ended';
      if (Array.isArray(providerValues) && providerValues.length) {
        applyTurnProviders(turn, providerValues, true);
      }
    }

    function commitActiveSpeaker(conv) {
      if (conv._activeTurnIdx === null || conv._activeTurnIdx === undefined) return;
      lockTurn(conv, conv._activeTurnIdx);
      const key = conv._activeSpeakerKey;
      if (key) {
        const st = speakerState(conv, key);
        const turn = conv.turns[conv._activeTurnIdx];
        st.baselineFullText = st.lastFullText;
        st.baselineEndTimeMs = Math.max(
          st.baselineEndTimeMs || 0,
          turn && Number.isFinite(turn.endTimeMs) ? turn.endTimeMs : 0,
          Number.isFinite(st.lastEndTimeMs) ? st.lastEndTimeMs : 0
        );
        st.lastDeltaText = '';
        st.hasVadEnded = false;
        st.stabilityStatus = 'stable';
        if (turn) {
          applySpeakerDebug(turn, st);
          turn.stabilityStatus = 'stable';
        }
      }
    }

    function finalizeCallTurns(conv, callId) {
      if (!conv) return;
      for (const turn of conv.turns) {
        if (callId && turn.callId !== callId) continue;
        turn.locked = true;
        turn.stabilityStatus = 'stable';
        if (!turn.vadState || turn.vadState === 'unknown' || turn.vadState === 'speaking') {
          turn.vadState = 'ended';
        }
        if (turn._speakerKey) {
          const st = speakerState(conv, turn._speakerKey);
          st.hasVadEnded = true;
          st.stabilityStatus = 'stable';
          st.vadState = turn.vadState;
          if (Number.isFinite(turn.endTimeMs)) {
            st.baselineEndTimeMs = Math.max(st.baselineEndTimeMs || 0, turn.endTimeMs);
          }
          if (turn.text) {
            st.baselineFullText = st.lastFullText || turn.text;
          }
        }
      }
      if (callId && conv._activeCallId === callId) {
        conv._activeTurnIdx = null;
        conv._activeSpeakerKey = null;
        conv._activeCallId = null;
      }
    }

    function stashPendingSwitch(conv, key, speaker, callId, text, startMs, endMs, segmentId, providerValues) {
      conv._pendingSwitch = {
        key, speaker, callId, text, startMs, endMs, segmentId, providerValues
      };
    }

    function activeSpeakerCanYield(conv) {
      if (!cfg.requireVadForSwitch) return true;
      if (!conv._activeSpeakerKey) return true;
      return Boolean(speakerState(conv, conv._activeSpeakerKey).hasVadEnded);
    }

    function switchToPending(conv) {
      const pending = conv._pendingSwitch;
      if (!pending) return false;
      if (!activeSpeakerCanYield(conv)) return false;
      if (!isMeaningful(pending.text, cfg.minSwitchChars)) return false;

      commitActiveSpeaker(conv);
      appendTurn(
        conv, pending.text, pending.speaker, pending.callId,
        pending.startMs, pending.endMs, pending.segmentId, pending.providerValues
      );
      conv._pendingSwitch = null;
      return true;
    }

    function shouldSwitch(conv, nextKey, nextDelta, nowMs) {
      if (!isMeaningful(nextDelta, cfg.minSwitchChars)) return false;
      if (cfg.turnSwitchStableMs <= 0) return true;
      const activeKey = conv._activeSpeakerKey;
      if (!activeKey || activeKey === nextKey) return true;
      const activeState = speakerState(conv, activeKey);
      return nowMs - activeState.lastChangedAt >= cfg.turnSwitchStableMs;
    }

    function onStableSpeech(
      key, callId, segmentId, segmentIds, speaker, text, startMs, endMs,
      provider, providers
    ) {
      if (!text || !text.trim()) return;
      const conv = ensureConv(key);
      const cleanText = text.trim();
      const providerValues = providerValuesForEvent(conv, callId, provider, providers);
      const keyForSpeaker = speakerKey(speaker);
      const ids = Array.isArray(segmentIds) && segmentIds.length
        ? segmentIds.filter(Boolean)
        : (segmentId ? [segmentId] : []);
      if (!isMeaningful(cleanText, cfg.minInitialChars)) return;

      const existing = ids.length
        ? conv.turns.find(turn => turn._speakerKey === keyForSpeaker && ids.some(id => turnContainsSegment(turn, id)))
        : null;

      let turn = existing;
      if (turn) {
        setStableTurnText(turn, ids, cleanText, providerValues);
        if (Number.isFinite(startMs)) turn.startTimeMs = startMs;
        if (Number.isFinite(endMs)) turn.endTimeMs = endMs;
        reorderTurnByStartTime(conv, turn);
      } else {
        turn = buildTurn(
          cleanText, speaker, callId, startMs, endMs, ids, true, providerValues
        );
        insertStableTurn(conv, turn);
      }

      const st = speakerState(conv, keyForSpeaker);
      st.hasVadEnded = true;
      st.stabilityStatus = 'stable';
      st.vadState = 'ended';
      st.baselineEndTimeMs = Math.max(st.baselineEndTimeMs || 0, Number.isFinite(endMs) ? endMs : 0);
      if (cleanText) st.baselineFullText = cleanText;
      applyPendingAudio(conv, keyForSpeaker, turn);
      applySpeakerDebug(turn, st);
      conv.status = 'active';
    }

    function onSpeech(
      key, callId, segmentId, speaker, text, startMs, endMs, nowMs,
      provider, providers
    ) {
      if (!text || !text.trim()) return;

      const conv = ensureConv(key);
      const cleanText = text.trim();
      const providerValues = providerValuesForEvent(conv, callId, provider, providers);
      const keyForSpeaker = speakerKey(speaker);
      const progress = updateSpeakerProgress(
        conv, keyForSpeaker, cleanText, nowMs, startMs, endMs, segmentId
      );
      const delta = progress.delta;

      if (!isMeaningful(delta, cfg.minInitialChars)) return;

      if (segmentId) {
        const existing = conv.turns.find(turn => turnContainsSegment(turn, segmentId));
        if (existing) {
          setTurnSegmentText(existing, segmentId, delta, providerValues);
          if (endMs > existing.endTimeMs) existing.endTimeMs = endMs;
          applyPendingAudio(conv, keyForSpeaker, existing);
          applySpeakerDebug(existing, speakerState(conv, keyForSpeaker));
          conv.status = 'active';
          return;
        }
      }

      const active = conv.turns[conv._activeTurnIdx];
      const sameActiveTurn = Boolean(
        active &&
        !active.locked &&
        conv._activeSpeakerKey === keyForSpeaker
      );

      if (sameActiveTurn) {
        updateActiveTurn(
          conv, delta, speaker, callId, startMs, endMs, segmentId, providerValues
        );
        conv.status = 'active';
        return;
      }

      if (active && cfg.requireVadForSwitch) {
        stashPendingSwitch(
          conv, keyForSpeaker, speaker, callId, delta, startMs, endMs, segmentId,
          providerValues
        );
        switchToPending(conv);
        conv.status = 'active';
        return;
      }

      if (active && !shouldSwitch(conv, keyForSpeaker, delta, nowMs)) return;

      commitActiveSpeaker(conv);
      appendTurn(
        conv, delta, speaker, callId, startMs, endMs, segmentId, providerValues
      );
      conv.status = 'active';
    }

    function onAudio(key, callId, segmentId, speaker, audioUrl, audioDurationMs, startTimeMs, endTimeMs, segmentIds, localAudioUrl) {
      const playUrl = localAudioUrl || audioUrl;  // 前端优先用本地 HTTPS URL
      if (!playUrl) return;
      const conv = ensureConv(key);
      attachAudioSegment(conv, speakerKey(speaker), {
        callId,
        segmentId,
        segmentIds: Array.isArray(segmentIds) ? segmentIds : [],
        audioUrl: playUrl,
        audioDurationMs,
        startTimeMs,
        endTimeMs,
      });
      conv.status = 'active';
    }

    function onVad(key, callId, speaker, vadState, silenceDurationMs, audioLevel, volumeDb, nowMs) {
      const conv = ensureConv(key);
      const keyForSpeaker = speakerKey(speaker);
      setVadState(conv, keyForSpeaker, vadState, nowMs, silenceDurationMs, audioLevel, volumeDb);
      if (keyForSpeaker === conv._activeSpeakerKey) {
        applySpeakerDebug(conv.turns[conv._activeTurnIdx], speakerState(conv, keyForSpeaker));
        switchToPending(conv);
      }
      conv.status = 'active';
    }

    function onCallCorrected(key, ev) {
      const conv = ensureConv(key, ev.callfrom, ev.callto);
      if (!Array.isArray(conv.aiCorrections)) conv.aiCorrections = [];
      const correctionKey = [
        ev.callId || '',
        ev.segmentId || '',
        Array.isArray(ev.segmentIds) ? ev.segmentIds.join(',') : '',
      ].join('|');
      const idx = conv.aiCorrections.findIndex(item => item.correctionKey === correctionKey);
      const correction = {
        event: ev.event || 'call.corrected',
        callId: ev.callId || '',
        correctionKey,
        correctionScope: ev.correctionScope || '',
        segmentId: ev.segmentId || '',
        segmentIds: Array.isArray(ev.segmentIds) ? ev.segmentIds : [],
        callfrom: ev.callfrom || '',
        callto: ev.callto || '',
        originalText: ev.originalText || '',
        correctedText: ev.correctedText || '',
        turns: Array.isArray(ev.turns) ? ev.turns : [],
        llmElapsedMs: Number.isFinite(ev.llmElapsedMs) ? ev.llmElapsedMs : null,
        receivedAtMs: eventTimeMs(ev),
      };
      if (idx >= 0) conv.aiCorrections[idx] = correction;
      else conv.aiCorrections.push(correction);
    }

    function onModelEvent(key, ev) {
      const conv = ensureConv(key, ev.callfrom, ev.callto);
      const callId = String(ev.callId || '');
      if (!callId || endedCallIds.has(callId)) return;
      conv.activeCallIds.add(callId);
      if (!conv.modelStates) conv.modelStates = {};
      const previous = conv.modelStates[callId] || {};
      const availableProviders = Array.isArray(ev.availableProviders)
        ? ev.availableProviders.slice()
        : (previous.availableProviders || ['funasr']);
      const state = {
        callId,
        currentProvider: ev.currentProvider || previous.currentProvider || 'funasr',
        pendingProvider: ev.pendingProvider || null,
        availableProviders,
        requestId: ev.requestId || previous.requestId || null,
        status: ev.event === 'asr.model.switch.failed'
          ? 'failed'
          : (ev.pendingProvider ? 'pending' : 'active'),
        errorCode: ev.errorCode || '',
        message: ev.message || '',
        connectElapsedMs: Number.isFinite(ev.connectElapsedMs) ? ev.connectElapsedMs : null,
        bufferedAudioMs: Number.isFinite(ev.bufferedAudioMs) ? ev.bufferedAudioMs : null,
      };
      conv.modelStates[callId] = state;
      if (ev.event === 'asr.model.switch.pending') {
        conv.modelSwitchRequest = {
          requestId: ev.requestId || '',
          targetProvider: ev.targetProvider || ev.pendingProvider || '',
        };
      }
      const request = conv.modelSwitchRequest;
      if (request) {
        const ids = Array.from(conv.activeCallIds);
        const allComplete = ids.length > 0 && ids.every(id => {
          const item = conv.modelStates[id];
          return item && item.currentProvider === request.targetProvider && !item.pendingProvider;
        });
        if (allComplete) conv.modelSwitchRequest = null;
      }
    }

    function applyModelSnapshot(ev) {
      const sessions = Array.isArray(ev.sessions) ? ev.sessions : [];
      const activeIds = new Set(sessions.map(item => String(item.callId || '')).filter(Boolean));
      for (const session of sessions) {
        const callId = String(session.callId || '');
        if (!callId) continue;
        endedCallIds.delete(callId);
        const key = resolveConv(callId, session.callfrom, session.callto);
        onModelEvent(key, Object.assign({event: 'asr.model.state'}, session));
      }
      for (const conv of convs.values()) {
        for (const callId of Array.from(conv.activeCallIds)) {
          if (conv.modelStates && conv.modelStates[callId] && !activeIds.has(callId)) {
            conv.activeCallIds.delete(callId);
            delete conv.modelStates[callId];
          }
        }
        if (conv.activeCallIds.size === 0 && conv.status !== 'ended') {
          conv.status = 'ended';
        }
      }
    }

    function modelSummary(conv) {
      const activeIds = Array.from((conv && conv.activeCallIds) || []);
      const ids = activeIds.length
        ? activeIds
        : Object.keys((conv && conv.modelStates) || {});
      const states = ids.map(id => conv.modelStates && conv.modelStates[id]).filter(Boolean);
      const providers = Array.from(new Set(states.map(item => item.currentProvider || 'funasr')));
      const currentProvider = providers.length === 1 ? providers[0] : (providers.length ? 'mixed' : 'funasr');
      const request = conv && conv.modelSwitchRequest;
      const targetProvider = request && request.targetProvider;
      const total = ids.length;
      const completed = targetProvider
        ? ids.filter(id => {
            const item = conv.modelStates && conv.modelStates[id];
            return item && item.currentProvider === targetProvider && !item.pendingProvider;
          }).length
        : total;
      const failed = states.filter(item => item.status === 'failed').length;
      const pending = Boolean(request) || states.some(item => item.pendingProvider);
      const canUseXfyun = states.length > 0 && states.every(
        item => Array.isArray(item.availableProviders) && item.availableProviders.includes('xfyun')
      );
      return {
        currentProvider,
        targetProvider: targetProvider || currentProvider,
        status: failed ? 'failed' : (pending ? 'pending' : 'active'),
        completed,
        total,
        failed,
        canUseXfyun,
        details: states,
      };
    }

    function beginModelSwitch(key, requestId, targetProvider) {
      const conv = convs.get(key);
      if (!conv) return false;
      conv.modelSwitchRequest = {requestId, targetProvider};
      if (!conv.modelStates) conv.modelStates = {};
      for (const callId of conv.activeCallIds) {
        const previous = conv.modelStates[callId] || {
          callId,
          currentProvider: 'funasr',
          availableProviders: ['funasr'],
        };
        conv.modelStates[callId] = Object.assign({}, previous, {
          pendingProvider: targetProvider,
          requestId,
          status: 'pending',
        });
      }
      return true;
    }

    function rejectModelSwitch(requestId, message) {
      for (const conv of convs.values()) {
        if (!conv.modelSwitchRequest || conv.modelSwitchRequest.requestId !== requestId) continue;
        conv.modelSwitchRequest = null;
        for (const callId of conv.activeCallIds) {
          const state = conv.modelStates && conv.modelStates[callId];
          if (state && state.requestId === requestId) {
            state.pendingProvider = null;
            state.status = 'failed';
            state.message = message || '模型切换失败';
          }
        }
      }
    }

    function handleEvent(ev) {
      if (ev && ev.event === 'asr.model.state' && ev.snapshot) {
        applyModelSnapshot(ev);
        return;
      }
      const incomingEvent = String((ev && ev.event) || '');
      const incomingCallId = String((ev && ev.callId) || '');
      if (
        incomingCallId
        && endedCallIds.has(incomingCallId)
        && [
          'asr.model.state',
          'asr.model.switch.pending',
          'asr.model.changed',
          'asr.model.switch.failed',
        ].includes(incomingEvent)
      ) return;
      const {
        event,
        callId,
        segmentId,
        callfrom,
        callto,
        speaker,
        text,
        startTimeMs,
        endTimeMs,
        vadState,
        silenceDurationMs,
        audioLevel,
        volumeDb,
        audioUrl,
        audioDurationMs,
        segmentIds,
        finalSource,
        provider,
        providers,
      } = ev;
      const key = resolveConv(callId, callfrom, callto);

      switch (event) {
        case 'call.started': {
          const conv = ensureConv(key, callfrom, callto);
          conv.status = 'active';
          conv.holdMessage = '';
          if (callId) {
            endedCallIds.delete(String(callId));
            conv.activeCallIds.add(callId);
          }
          break;
        }
        case 'call.hold.started': {
          const conv = ensureConv(key, callfrom, callto);
          conv.status = 'holding';
          conv.holdMessage = ev.message || '通话保持中，转写暂停';
          if (callId) conv.activeCallIds.add(callId);
          break;
        }
        case 'call.hold.ended': {
          const conv = ensureConv(key, callfrom, callto);
          conv.status = 'active';
          conv.holdMessage = ev.message || '通话已恢复，转写继续';
          if (callId) conv.activeCallIds.add(callId);
          break;
        }
        case 'speech.final':
          if (finalSource) {
            onStableSpeech(
              key, callId, segmentId, Array.isArray(segmentIds) ? segmentIds : [], speaker, text,
              startTimeMs || 0, endTimeMs || 0, provider, providers
            );
          } else {
            onSpeech(
              key, callId, segmentId, speaker, text,
              startTimeMs || 0, endTimeMs || 0, eventTimeMs(ev), provider, providers
            );
          }
          break;
        case 'speech.vad':
          onVad(key, callId, speaker, vadState, silenceDurationMs, audioLevel, volumeDb, eventTimeMs(ev));
          break;
        case 'audio.segment':
          onAudio(
            key, callId, segmentId, speaker, ev.audioUrl, ev.audioDurationMs,
            startTimeMs || 0, endTimeMs || 0,
            Array.isArray(segmentIds) ? segmentIds : [],
            ev.localAudioUrl
          );
          break;
        case 'call.ended': {
          const conv = convs.get(key);
          if (conv) {
            finalizeCallTurns(conv, callId);
            if (callId) {
              endedCallIds.add(String(callId));
              conv.activeCallIds.delete(callId);
            }
            if (conv.activeCallIds.size === 0) {
              conv.status = 'ended';
              conv.modelSwitchRequest = null;
              commitActiveSpeaker(conv);
            }
          }
          break;
        }
        case 'call.corrected':
          onCallCorrected(key, ev);
          break;
        case 'asr.model.state':
        case 'asr.model.switch.pending':
        case 'asr.model.changed':
        case 'asr.model.switch.failed':
          onModelEvent(key, ev);
          break;
      }
    }

    function clearAll() {
      convs.clear();
      callIdToPair.clear();
      endedCallIds.clear();
      turnCount = 0;
    }

    return {
      convs,
      callIdToPair,
      handleEvent,
      clearAll,
      conversations: () => Array.from(convs.values()),
      totalTurns: () => turnCount,
      modelSummary,
      beginModelSwitch,
      rejectModelSwitch,
    };
  }

  return { createMonitorState };
});
