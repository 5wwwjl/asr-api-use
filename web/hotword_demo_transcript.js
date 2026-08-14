(function (root) {
  "use strict";

  function createState() {
    return { order: [], segments: Object.create(null) };
  }

  function messageText(message) {
    const payload = message && message.payload || {};
    return String(message && message.text || payload.text || "").trim();
  }

  function isCorrectionMessage(message) {
    return Boolean(
      message
      && (message.event === "call.corrected" || message.eventType === "call.corrected")
    );
  }

  function segmentIds(message) {
    const payload = message && message.payload || {};
    const ids = Array.isArray(message && message.segmentIds)
      ? message.segmentIds.map(String).filter(Boolean)
      : [];
    const primary = String(
      payload.segmentId || message && message.segmentId || ids[0] || "__current__"
    );
    return { primary, ids };
  }

  function applyMessage(state, message) {
    // The hotword A/B demo compares raw ASR hypotheses only. The correction
    // service still runs for other consumers, but its result must not change
    // either side of this comparison.
    if (isCorrectionMessage(message)) return null;

    const text = messageText(message);
    if (!text) return null;

    const { primary, ids } = segmentIds(message);
    if (!Object.prototype.hasOwnProperty.call(state.segments, primary)) {
      state.order.push(primary);
    }
    state.segments[primary] = text;
    return state.order
      .filter(id => Object.prototype.hasOwnProperty.call(state.segments, id))
      .map(id => state.segments[id])
      .join("，");
  }

  root.HotwordTranscript = { createState, applyMessage };
})(globalThis);
