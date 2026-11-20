/* Guardian status chip in the header (T-050): read-only, nothing clickable.

   GET /api/guardian/state every 5 s while the page is visible (no polling in a
   hidden tab, one tick when it becomes visible again), and paint the `display`
   the backend derived from the Guardian service's state file (GUARDIAN.md §9).
   Until that service exists the chip says "Guardian · not installed"; a fetch
   that fails says "Guardian · no data" instead of keeping the last colour.
   The label and its class come from guardian-chip-format.js, the lamp from
   ui/kit.js — no drawing and no policy in this file. */

import { lamp } from "./ui/kit.js";
import { LAMP_STATE, NO_DATA, chipClass, chipDetail, chipLevel, chipText } from "./guardian-chip-format.js";

const host = document.getElementById("guardian-chip");
if (!host) throw new Error("missing #guardian-chip");

const POLL_MS = 5000;
const setLamp = lamp(host);
let levelClass = null;

function paint(display) {
  setLamp(LAMP_STATE[chipLevel(display)], chipText(display));
  if (levelClass) host.classList.remove(levelClass);
  levelClass = chipClass(display);
  host.classList.add(levelClass);
  const tooltip = chipDetail(display);
  if (tooltip) host.setAttribute("title", tooltip);
  else host.removeAttribute("title");
}

let inFlight = false;

async function tick() {
  if (inFlight || document.hidden) return;
  inFlight = true;
  try {
    const res = await fetch("/api/guardian/state", { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`/api/guardian/state -> HTTP ${res.status}`);
    const body = await res.json();
    paint(body && body.display);
  } catch {
    paint(NO_DATA);
  }
  inFlight = false;
}

tick();
setInterval(tick, POLL_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) tick();
});
