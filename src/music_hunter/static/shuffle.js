// Shuffle that remembers (feedback #33): the mode and the tracks already heard this round are kept per playlist on this
// device, so a reloaded page, a tapped track or the shuffle button doesn't bring a song round again until the rest of
// the playlist has had its turn. Once everything has been heard a new round starts, with the last track played last.
const ShuffleRound = {
  create(key, store) {
    const browser = () => store || window.localStorage;     // reading it can throw when site data is blocked
    const read = (name, fallback) => {
      try { const v = browser().getItem(`${name}:${key}`); return v === null ? fallback : JSON.parse(v); }
      catch (e) { return fallback; }
    };
    const write = (name, value) => { try { browser().setItem(`${name}:${key}`, JSON.stringify(value)); } catch (e) {} };
    let heard = read("shuffle-heard", []);
    const random = (list) => {
      const a = list.slice();
      for (let i = a.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [a[i], a[j]] = [a[j], a[i]];
      }
      return a;
    };
    return {
      on: () => read("shuffle-on", false) === true,
      setOn: (on) => write("shuffle-on", !!on),
      mark(id) {
        heard = heard.filter((h) => h !== id).concat(id);
        write("shuffle-heard", heard);
      },
      order(tracks) {                                   // tracks: objects with an id; not heard yet come first
        const fresh = tracks.filter((t) => !heard.includes(t.id));
        if (fresh.length) return [...random(fresh), ...random(tracks.filter((t) => heard.includes(t.id)))];
        const last = heard[heard.length - 1];
        heard = [];
        write("shuffle-heard", heard);
        return [...random(tracks.filter((t) => t.id !== last)), ...tracks.filter((t) => t.id === last)];
      },
    };
  },
};
if (typeof module !== "undefined") module.exports = ShuffleRound;
