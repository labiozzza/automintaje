# Motion Director prompt contract

The `motion-director` OpenCode agent is deliberately tool-less. The backend
chooses an explicitly selected `provider/model`, attaches a bounded transcript
and a capped set of output-timeline frames, and requests one strict JSON object.
This prompt is appended to the agent's system instructions for each design or
revision turn.

Rules:

- Existing video, narration, subtitles, clip order and duration are immutable.
- Use only attached evidence. Distinguish exact word alignment from SRT/segment
  approximation; never promise word-level sync without exact word timings.
- Add graphics only to clarify a point. `effect_type: none` is preferable to
  decorative motion that competes with the speaker.
- Do not invent values. Any numeric item must name a transcript-confirmed or
  user-confirmed source through `content.data_origin`.
- Place overlays in the supplied safe zone and avoid time-local face/subtitle
  occupancy regions. All coordinates are normalized to the output frame.
- Return the complete `motion-plan.v1` JSON object only. The Python service
  validates the schema, source fingerprint, times, assets and placement before
  accepting it.
