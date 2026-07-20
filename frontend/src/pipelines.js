/** Default a new opportunity to the first stage (by position) of its chosen
 * pipeline. Mutates and returns `body` — the shape the create-modal
 * transforms expect. No-op when no pipeline is chosen or it has no stages. */
export function applyFirstStage(body, pipelines) {
  if (body.pipeline_id) {
    const p = pipelines.find((x) => x.id === body.pipeline_id);
    const first = p?.stages?.slice().sort((a, b) => a.position - b.position)[0];
    if (first) body.stage_id = first.id;
  }
  return body;
}
