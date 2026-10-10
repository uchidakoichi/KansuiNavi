/**
 * Service Worker の登録と「新しいデータに更新」の告知（F-09）。
 *
 * ★**勝手に新版へ切り替えない。** 読み込み済みの区画と配布データの版が
 *   ずれうるので、利用者が押したときだけ交代し、ページを読み直す。
 *
 * ★★**告知の入口は3つある。3つとも同じ経路に通すこと。**
 *   ブラウザは画面遷移のたびに `sw.js` を取り直すので、その更新は
 *   こちらが `register()` の結果を受け取るより**先に走りうる**。
 *   `updatefound` だけを待つと**その回を丸ごと落とし、ボタンが出ないまま**になる
 *   （藤沢版で 2026-09-09 に実測）。
 *     (1) 既に `reg.waiting` に居る
 *     (2) いま `reg.installing` が走っている
 *     (3) これから `updatefound` が来る
 */
import { DATA_BASE } from "./data-base";

export function registerServiceWorker(button: HTMLButtonElement, note?: HTMLElement) {
  // ★dev では登録しない。ビルド前のファイルを掴むと、直しても反映されなくなる
  if (!import.meta.env.PROD) return;

  // ★★**Service Worker は「安全なコンテキスト」でしか動かない。**
  //   HTTPS か `localhost` / `127.0.0.1` のみ。**LAN の IP（http://192.168.x.x）では
  //   `navigator.serviceWorker` がそもそも存在しない。**
  //   2026-09-22 に実機で踏んだ: iPhone から `http://192.168.0.69:4173/` を開き、
  //   機内モードで再読み込みしたら「インターネットに接続されていないため
  //   ページを開くことができません」になった。
  //   ★登録を黙って飛ばしていたので、**オフラインに対応しているつもりで
  //     まったく効いていない**状態だった。
  //   → **画面に出す。** 黙って機能を落とさない。
  if (!("serviceWorker" in navigator)) {
    if (note) {
      note.textContent =
        window.isSecureContext
          ? "★この環境では Service Worker が使えないため、オフラインでは開けません。"
          : "★この URL は安全な接続ではない（HTTPS でも localhost でもない）ため、" +
            "オフライン動作は使えません。実機で試すには HTTPS が要ります。";
      note.hidden = false;
    }
    return;
  }

  navigator.serviceWorker
    // ★配布データが別のオリジンにあるとき、その置き場を Service Worker に教える（キャッシュの対象にするため）
    .register(`sw.js?data=${encodeURIComponent(new URL(DATA_BASE).origin)}`)
    .then((reg) => {
      const offer = (w: ServiceWorker | null) => {
        // ★`controller` が居ない＝初回訪問。まだ古い版は無いので告知しない
        if (!w || !navigator.serviceWorker.controller) return;
        if (w.state === "installed") button.hidden = false;
      };
      const track = (w: ServiceWorker | null) => {
        if (!w) return;
        offer(w); // 見に行った時点で既に installed かもしれない
        w.addEventListener("statechange", () => offer(w));
      };
      track(reg.waiting);
      track(reg.installing);
      reg.addEventListener("updatefound", () => track(reg.installing));

      button.onclick = () => {
        button.disabled = true;
        button.textContent = "更新しています…";
        (reg.waiting ?? reg.active)?.postMessage("skip-waiting");
        navigator.serviceWorker.addEventListener(
          "controllerchange",
          () => location.reload(),
          { once: true },
        );
      };
    })
    .catch((e) => console.warn("Service Worker を登録できませんでした", e));
}
