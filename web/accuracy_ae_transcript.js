(function (root) {
  "use strict";

  function cleanText(value) {
    return String(value || "").replace(/<\|[^|]*\|>/g, "").trim();
  }

  function createBaselineState() {
    return { finals: [], partial: "" };
  }

  function baselineSnapshot(state) {
    const items = state.finals.map((text, index) => ({
      id: `a-final-${index + 1}`,
      text,
      final: true,
    }));
    if (state.partial) {
      items.push({ id: "a-partial", text: state.partial, final: false });
    }
    return { items, text: items.map(item => item.text).join("，") };
  }

  function applyBaseline(state, message) {
    if (!message || typeof message !== "object") return null;
    const text = cleanText(message.text || message.rec_text);
    if (!text) return null;
    const mode = String(message.mode || "").toLowerCase();
    const final = Boolean(message.is_final) || mode === "offline" || mode === "2pass-offline";
    if (final) {
      if (state.finals[state.finals.length - 1] !== text) state.finals.push(text);
      state.partial = "";
    } else {
      state.partial = text;
    }
    return baselineSnapshot(state);
  }

  function createEnhancedState() {
    return { order: [], segments: Object.create(null) };
  }

  function ensureSegment(state, id) {
    const segmentId = String(id || "__current__");
    if (!Object.prototype.hasOwnProperty.call(state.segments, segmentId)) {
      state.order.push(segmentId);
      state.segments[segmentId] = {
        id: segmentId,
        raw: "",
        corrected: "",
        final: false,
        correctionProvider: "",
      };
    }
    return state.segments[segmentId];
  }

  function enhancedSnapshot(state) {
    const items = state.order.map(id => state.segments[id]).filter(Boolean).map(item => ({
      id: item.id,
      raw: item.raw,
      corrected: item.corrected,
      text: item.corrected || item.raw,
      final: item.final,
      changed: Boolean(item.corrected && item.raw && item.corrected !== item.raw),
      correctionProvider: item.correctionProvider,
    }));
    return {
      items,
      text: items.map(item => item.text).filter(Boolean).join("，"),
      correctionCount: items.filter(item => item.changed).length,
    };
  }

  function isCorrection(message) {
    return message && (
      message.event === "call.corrected" || message.eventType === "call.corrected"
    );
  }

  function applyCorrection(state, message) {
    const turns = Array.isArray(message.turns) ? message.turns : [];
    let applied = false;
    for (const turn of turns) {
      const segmentId = turn && turn.segmentId;
      const corrected = cleanText(turn && turn.correctedText);
      if (!segmentId || !corrected) continue;
      const segment = ensureSegment(state, segmentId);
      segment.raw = segment.raw || cleanText(turn.originalText);
      segment.corrected = corrected;
      segment.final = true;
      segment.correctionProvider = String(message.correctionProvider || "");
      applied = true;
    }
    if (applied) return;

    const corrected = cleanText(message.correctedText);
    if (!corrected) return;
    const ids = Array.isArray(message.segmentIds) && message.segmentIds.length
      ? message.segmentIds
      : [message.segmentId || "__current__"];
    const segment = ensureSegment(state, ids[0]);
    segment.raw = segment.raw || cleanText(message.originalText);
    segment.corrected = corrected;
    segment.final = true;
    segment.correctionProvider = String(message.correctionProvider || "");
  }

  function applyEnhanced(state, message) {
    if (!message || typeof message !== "object") return null;
    if (isCorrection(message)) {
      applyCorrection(state, message);
      return enhancedSnapshot(state);
    }

    const payload = message.payload && typeof message.payload === "object"
      ? message.payload
      : {};
    const text = cleanText(message.text || payload.text);
    if (!text) return null;
    const segmentId = payload.segmentId || message.segmentId || "__current__";
    const segment = ensureSegment(state, segmentId);
    if (segment.corrected && text !== segment.raw && text !== segment.corrected) {
      // A late, more complete ASR final must not be hidden by an earlier
      // correction generated from a shorter streaming fallback.
      segment.corrected = "";
      segment.correctionProvider = "";
    }
    segment.raw = text;
    segment.final = message.eventType === "speech.final" || Boolean(message.is_final);
    return enhancedSnapshot(state);
  }

  root.AccuracyAETranscript = {
    applyBaseline,
    applyEnhanced,
    baselineSnapshot,
    createBaselineState,
    createEnhancedState,
    enhancedSnapshot,
  };
})(globalThis);
