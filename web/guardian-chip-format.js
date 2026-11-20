/* Guardian chip display logic (T-050): pure, DOM-free, tested with `node --test`.

   The backend derives `display` = {level, label, detail} from the Guardian
   service's state file (the Guardian milestone contract, §9); this decides how
   that reaches the header chip and what to show when it does not arrive intact.
   Nothing here invents a state: a level this file does not know is styled like
   "unknown", never like a colour some earlier answer once had, and a label that
   is not a usable string costs the label, not the chip.

   A label may name an e-stop scope: a worker id from the Guardian configuration,
   or `all`. This file knows no ids and never rewrites one — the text is passed
   through as it arrived, already capped by the backend, and the DOM glue writes
   it with `textContent`, so it is plain text and never HTML. */

/** The levels the backend may report, and the only ones that get a style. */
export const LEVELS = ["off", "ok", "warn", "hot", "unknown"];

/** The chip's own class prefix; theme.css colours one per level. */
export const CHIP_CLASS = "gchip";

/** What a chip with nothing to go on says: no data, never a guessed colour. */
export const NO_DATA = { level: "warn", label: "Guardian · no data", detail: "" };

/** The mark a grey UNKNOWN chip carries, per §9. */
export const UNKNOWN_MARK = "?";

/** Level -> the state `ui/kit.js` lamp() knows: hot is its error red,
    unknown the muted off lamp (the "?" in the text tells them apart). */
export const LAMP_STATE = { off: "off", ok: "ok", warn: "warn", hot: "error", unknown: "off" };

const text = (value) => (typeof value === "string" && value.trim() ? value.trim() : null);

/** One of LEVELS; anything missing, odd or unknown becomes "unknown". */
export function chipLevel(display) {
  const level = display && display.level;
  return LEVELS.includes(level) ? level : "unknown";
}

/** The label exactly as the backend worded it, or "Guardian · no data". */
export function chipLabel(display) {
  return text(display && display.label) || NO_DATA.label;
}

/** Screen-reader text: the label, plus "?" when the level is unknown. */
export function chipText(display) {
  const label = chipLabel(display);
  return chipLevel(display) === "unknown" ? `${label} ${UNKNOWN_MARK}` : label;
}

/** The chip's level class, e.g. "gchip-hot"; never a class theme.css lacks. */
export function chipClass(display) {
  return `${CHIP_CLASS}-${chipLevel(display)}`;
}

/** The tooltip: the backend's detail text, or nothing to say. */
export function chipDetail(display) {
  return text(display && display.detail) || "";
}

/** Everything the DOM glue needs, in one object. */
export function chipView(display) {
  return {
    level: chipLevel(display),
    lamp: LAMP_STATE[chipLevel(display)],
    text: chipText(display),
    cls: chipClass(display),
    tooltip: chipDetail(display),
  };
}
