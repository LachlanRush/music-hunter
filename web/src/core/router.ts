// Screens by address, without leaving the page: `/`, `/playlist/<id>` (under BASE while it runs beside the old app).
import { Store } from "./store";

// While it runs beside the old pages it lives under /next; once it replaces them, at the top.
export const BASE = typeof location !== "undefined" && /^\/next(\/|$)/.test(location.pathname) ? "/next" : "";

export type Route =
  | {name: "home"}
  | {name: "playlist"; id: number}
  | {name: "results"; id: number}
  | {name: "missing"; path: string};

export function parse(pathname: string): Route {
  const p = pathname.startsWith(BASE) ? pathname.slice(BASE.length) : pathname;
  const clean = p.replace(/\/+$/, "") || "/";
  if (clean === "/") return {name: "home"};
  const m = clean.match(/^\/playlist\/(\d+)$/);
  if (m) return {name: "playlist", id: Number(m[1])};
  const r = clean.match(/^\/playlist\/(\d+)\/results$/);
  if (r) return {name: "results", id: Number(r[1])};
  return {name: "missing", path: clean};
}

export const route = new Store<Route>(typeof location === "undefined" ? {name: "home"} : parse(location.pathname));

export function href(path: string): string {
  // (At the top, home is "/": an empty address would leave the browser where it is, so back arrows did nothing.)
  return BASE + (path === "/" ? "" : path) || "/";
}

export function go(path: string, replace = false): void {
  const url = href(path);
  if (url !== location.pathname + location.search) {
    if (replace) history.replaceState({}, "", url);
    else history.pushState({}, "", url);
  }
  route.set(parse(location.pathname));
  window.scrollTo(0, 0);
}

if (typeof window !== "undefined") {
  window.addEventListener("popstate", () => route.set(parse(location.pathname)));
}
